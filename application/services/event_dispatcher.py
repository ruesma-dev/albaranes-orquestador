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

        # Caso 3: estado activo o waiting_approval → encolar evento.
        # Cuando llegue a un estado donde tenga sentido aplicarlo, se
        # consumirá. (En esta primera versión, solo es relevante cuando
        # estaba en awaiting_approval — significa que el revisor cambia
        # el contrato sin aprobar todavía.)
        if run.current_state == WorkflowState.AWAITING_APPROVAL:
            return self._spawn_revaluation(run, event)

        # En cualquier otro estado activo: dejamos pendiente. Tras
        # terminar la transición actual, si llega a un estado relevante
        # se aplicará. (Por ahora lo guardamos como pending_event_json.)
        run.pending_event = {
            "type": "contract-selected",
            "payload": event.model_dump(),
        }
        self._repo.update(run)
        return (
            EventApplyResult(
                workflow_id=run.id,
                previous_state=prev_state.value,
                new_state=prev_state.value,
                action="event_queued",
                detail=f"evento aplicado al llegar a estado pasivo desde {prev_state.value}",
            ),
            None,
        )

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
