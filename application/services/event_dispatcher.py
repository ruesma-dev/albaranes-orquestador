# application/services/event_dispatcher.py
"""Mapea eventos del API → transiciones en el WorkflowEngine.

3 eventos manejados:
  - email-received   → crea workflow nuevo (idempotente).
  - contract-selected → reanuda awaiting_contract_selection o spawn
                        revaluation si el workflow ya está terminal.
  - document-approved → cierra awaiting_approval → approved.

Decisión clave A (validada): cuando el revisor cambia el contrato
después de aprobar un documento, NO se reabre el workflow viejo. Se
crea un nuevo workflow de tipo ``albaran_revaluation``. Esto preserva
el histórico íntegro.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from application.services.idempotency_guard import IdempotencyGuard
from application.services.workflow_engine import WorkflowEngine
from domain.models.events import (
    ContractSelectedEvent,
    DocumentApprovedEvent,
    DocumentPurgedEvent,
    EmailReceivedEvent,
    EmailReceivedAck,
    EventApplyResult,
)
from domain.models.workflow import (
    FAILED_STATES,
    TERMINAL_OK_STATES,
    WAITING_STATES,
    WorkflowRun,
    WorkflowState,
)
from domain.ports.workflow_repository import WorkflowRepository

logger = logging.getLogger(__name__)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class EventDispatcher:
    def __init__(
        self,
        *,
        repository: WorkflowRepository,
        engine: WorkflowEngine,
        guard: IdempotencyGuard,
    ) -> None:
        self._repo = repository
        self._engine = engine
        self._guard = guard

    # ----------------------------------------------------------- #
    # email-received
    # ----------------------------------------------------------- #
    def handle_email_received(
        self,
        event: EmailReceivedEvent,
        *,
        file_path: str,
    ) -> tuple[EmailReceivedAck, str | None]:
        """Devuelve el ack y el workflow_id a ejecutar (None si duplicado
        o ya terminal). El caller (API) lanza el BackgroundTask con el id."""
        correlation_key = event.correlation_key

        # Gate 1 (contenido): mismo PDF (attachment_sha256) ya procesado,
        # aunque venga en otro correo (otra correlation_key). Cancela aqui,
        # ANTES de crear el workflow -> no se gasta IA (sv2) ni contrato
        # (sv3) ni valoracion (sv5/sv6). Si solo hubo intentos fallidos, el
        # guard devuelve None y se reprocesa.
        dup_content = self._guard.check_attachment(event.attachment_sha256)
        if dup_content is not None:
            return (
                EmailReceivedAck(
                    accepted=True,
                    workflow_id=dup_content.id,
                    correlation_key=correlation_key,
                    duplicate=True,
                    message=(
                        "PDF ya procesado (mismo contenido) en estado="
                        f"{dup_content.current_state.value}; no se reprocesa"
                    ),
                ),
                None,
            )

        # Gate 2 (mismo correo): correlation_key exacta ya vista.
        existing = self._guard.check_correlation(correlation_key)
        if existing is not None:
            return (
                EmailReceivedAck(
                    accepted=True,
                    workflow_id=existing.id,
                    correlation_key=correlation_key,
                    duplicate=True,
                    message=(
                        f"workflow ya existente en estado={existing.current_state.value}"
                    ),
                ),
                None,
            )

        payload = event.model_dump()
        payload["file_path"] = file_path

        run = self._engine.create_workflow(
            kind="albaran_e2e",
            correlation_key=correlation_key,
            payload=payload,
            attachment_sha256=event.attachment_sha256,
        )

        return (
            EmailReceivedAck(
                accepted=True,
                workflow_id=run.id,
                correlation_key=correlation_key,
                duplicate=False,
                message=f"workflow encolado: estado inicial = {run.current_state.value}",
            ),
            run.id,
        )

    # ----------------------------------------------------------- #
    # contract-selected
    # ----------------------------------------------------------- #
    def handle_contract_selected(
        self,
        event: ContractSelectedEvent,
    ) -> tuple[EventApplyResult, str | None]:
        run = self._repo.find_active_by_document_id(event.document_id)
        if run is None:
            # No hay workflow activo. ¿Existe uno terminal-ok? → spawn
            # revaluation. Si no existe ninguno, log y no-op.
            latest = self._repo.find_latest_by_document_id(event.document_id)
            if latest is None:
                logger.warning(
                    "contract-selected para document_id=%s sin workflow asociado",
                    event.document_id,
                )
                return (
                    EventApplyResult(
                        workflow_id="",
                        previous_state="",
                        new_state="",
                        action="no_op",
                        detail="no existe workflow para ese document_id",
                    ),
                    None,
                )
            return self._spawn_revaluation(latest, event)

        prev_state = run.current_state

        # Caso 1: estado pasivo de "elige contrato" → transitar a valuing.
        if run.current_state == WorkflowState.AWAITING_CONTRACT_SELECTION:
            run.payload["selected_contrato_codigo"] = event.codigo_contrato
            run.payload["contract_selected_by"] = event.selected_by
            run.payload["contract_selected_at_utc"] = event.selected_at_utc
            self._repo.update(run)
            return (
                EventApplyResult(
                    workflow_id=run.id,
                    previous_state=prev_state.value,
                    new_state=WorkflowState.VALUING.value,
                    action="transition_applied",
                ),
                run.id,
            )

        # Caso 2: estado de fallo de valoración → reabrir.
        if run.current_state == WorkflowState.VALUATION_FAILED:
            run.payload["selected_contrato_codigo"] = event.codigo_contrato
            run.retry_count += 1
            self._repo.update(run)
            return (
                EventApplyResult(
                    workflow_id=run.id,
                    previous_state=prev_state.value,
                    new_state=WorkflowState.VALUING.value,
                    action="transition_applied",
                    detail="reabriendo valuation_failed",
                ),
                run.id,
            )

        # Caso 3: estado de espera de aprobación → el revisor cambia el
        # contrato sin aprobar todavía. Spawn revaluation (decisión A).
        if run.current_state == WorkflowState.AWAITING_APPROVAL:
            return self._spawn_revaluation(run, event)

        # Caso 4 (jun 2026): CUALQUIER otro estado (activo en vuelo o
        # *_failed distinto de valuation_failed).
        #
        # ANTES aquí se encolaba el evento en ``pending_event`` "para
        # consumirlo al llegar a un estado pasivo"... pero NINGÚN código
        # consumía pending_event jamás: el evento moría en BBDD y la
        # valoración nunca arrancaba aunque el portal decía "Valoración
        # encolada" (bug reportado: "dice que lanza valorar pero no lo
        # hace"). Como el evento viene de sv4 (que LEE el document_id de
        # BBDD), el documento EXISTE y tiene contrato seleccionado, así
        # que lo correcto es spawn de un workflow albaran_revaluation
        # independiente: no toca el run padre (que seguirá su curso o se
        # quedará fallido para el retrier) y garantiza que la valoración
        # se ejecuta YA contra el contrato recién elegido. Si el padre
        # también termina valorando, la última escritura gana
        # (replace_valuation en sv6) — resultado idéntico y consistente.
        logger.info(
            "contract-selected con workflow %s en estado %s → "
            "spawn revaluation independiente",
            run.id,
            run.current_state.value,
        )
        return self._spawn_revaluation(run, event)

    def _spawn_revaluation(
        self,
        parent: WorkflowRun,
        event: ContractSelectedEvent,
    ) -> tuple[EventApplyResult, str | None]:
        correlation_key = (
            f"revaluation:{parent.document_id}:"
            f"{event.codigo_contrato}:{event.selected_at_utc}"
        )
        existing = self._repo.find_by_correlation_key(correlation_key)
        if existing is not None:
            return (
                EventApplyResult(
                    workflow_id=existing.id,
                    previous_state=existing.current_state.value,
                    new_state=existing.current_state.value,
                    action="no_op",
                    detail="revaluation ya creado para esa selección",
                ),
                None,
            )
        new_run = self._engine.create_workflow(
            kind="albaran_revaluation",
            correlation_key=correlation_key,
            payload={
                "selected_contrato_codigo": event.codigo_contrato,
                "contract_selected_by": event.selected_by,
                "contract_selected_at_utc": event.selected_at_utc,
            },
            document_id=parent.document_id,
            parent_workflow_id=parent.id,
            initial_state=WorkflowState.VALUING,
        )
        return (
            EventApplyResult(
                workflow_id=new_run.id,
                previous_state=parent.current_state.value,
                new_state=WorkflowState.VALUING.value,
                action="spawned_revaluation",
                detail=f"parent={parent.id}",
            ),
            new_run.id,
        )

    # ----------------------------------------------------------- #
    # document-approved
    # ----------------------------------------------------------- #
    def handle_document_approved(
        self,
        event: DocumentApprovedEvent,
    ) -> EventApplyResult:
        run = self._repo.find_active_by_document_id(event.document_id)
        if run is None:
            logger.warning(
                "document-approved sin workflow activo doc=%s",
                event.document_id,
            )
            return EventApplyResult(
                workflow_id="",
                previous_state="",
                new_state="",
                action="no_op",
                detail="no hay workflow activo para ese document_id",
            )

        prev_state = run.current_state
        if run.current_state != WorkflowState.AWAITING_APPROVAL:
            logger.warning(
                "document-approved aplicado a estado %s (esperaba awaiting_approval)",
                run.current_state.value,
                extra={"workflow_id": run.id},
            )

        run.current_state = WorkflowState.APPROVED
        run.completed_at_utc = _utc_iso()
        run.updated_at_utc = run.completed_at_utc
        run.payload["approved_by"] = event.approved_by
        run.payload["approved_at_utc"] = event.approved_at_utc
        run.payload["review_notes"] = event.review_notes
        self._repo.update(run)

        logger.info(
            "state: %s → approved (event document-approved)",
            prev_state.value,
            extra={"workflow_id": run.id},
        )
        return EventApplyResult(
            workflow_id=run.id,
            previous_state=prev_state.value,
            new_state=WorkflowState.APPROVED.value,
            action="transition_applied",
        )

    # ----------------------------------------------------------- #
    # document-purged (jun 2026)
    # ----------------------------------------------------------- #
    def handle_document_purged(
        self,
        event: DocumentPurgedEvent,
    ) -> EventApplyResult:
        """Marca como ``purged`` todos los workflows del documento.

        Motivación (bug del hard-delete): el sv4 borraba físicamente el
        albarán de BBDD, pero los ``workflow_runs`` del sv7 seguían
        existiendo con estado no-fallido. Al reenviar el MISMO PDF, el
        guard de idempotencia (Gate 1, dedup por ``attachment_sha256``)
        encontraba el run antiguo y respondía "PDF ya procesado", aunque
        el documento ya no existía en el portal. Resultado: albaranes
        imposibles de reprocesar tras una purga.

        Acción:
          1. TODOS los runs con ``document_id`` == purgado → estado
             ``purged`` (incluye approved/duplicate: el documento ya no
             existe, su workflow no debe bloquear nada).
          2. Red de seguridad: runs cuyo ``attachment_sha256`` ==
             ``source_sha256`` del documento y SIN ``document_id``
             (fallaron antes de persistir) → también ``purged``. Para
             PDFs de UNA página (caso mayoritario), el sha del adjunto
             coincide con el sha de la página, así que esto cubre
             además el caso "el workflow nunca enlazó el documento".

        Limitación documentada: en PDFs multipágina, purgar UNA página
        no purga los workflows de las páginas hermanas (sus documentos
        siguen vivos). Si se reenvía el PDF completo, el Gate 1 seguirá
        bloqueándolo mientras exista alguna página hermana no purgada;
        purga todas las páginas para reprocesar el adjunto entero.
        """
        runs = list(self._repo.find_all_by_document_id(event.document_id))

        sha = (event.source_sha256 or "").strip()
        if sha:
            seen_ids = {r.id for r in runs}
            for extra_run in self._repo.find_all_by_attachment_sha256(sha):
                if extra_run.id in seen_ids:
                    continue
                # Solo la red de seguridad: runs huérfanos (sin doc) o
                # ligados a ESTE mismo documento.
                if extra_run.document_id in (None, "", event.document_id):
                    runs.append(extra_run)
                    seen_ids.add(extra_run.id)

        if not runs:
            logger.info(
                "document-purged doc=%s sin workflows asociados (no-op)",
                event.document_id,
            )
            return EventApplyResult(
                workflow_id="",
                previous_state="",
                new_state="",
                action="no_op",
                detail="no existen workflows para ese document_id",
            )

        now = _utc_iso()
        purged_count = 0
        last_run = runs[-1]
        for run in runs:
            if run.current_state == WorkflowState.PURGED:
                continue
            prev = run.current_state
            run.current_state = WorkflowState.PURGED
            run.completed_at_utc = now
            run.updated_at_utc = now
            run.pending_event = None
            run.payload["purged_by"] = event.purged_by
            run.payload["purged_at_utc"] = event.purged_at_utc
            self._repo.update(run)
            purged_count += 1
            logger.info(
                "state: %s → purged (event document-purged doc=%s)",
                prev.value,
                event.document_id,
                extra={"workflow_id": run.id},
            )

        return EventApplyResult(
            workflow_id=last_run.id,
            previous_state="",
            new_state=WorkflowState.PURGED.value,
            action="transition_applied",
            detail=f"{purged_count} workflow(s) marcados como purged",
        )
