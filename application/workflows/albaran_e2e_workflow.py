# application/workflows/albaran_e2e_workflow.py
"""Workflow concreto: ingesta end-to-end de un albarán.

Modelado como state machine. El motor (WorkflowEngine) llama a
``advance(state)`` para ejecutar la transición que toque desde el
estado activo actual.

Estados activos manejados:
  - email_received  → extracting (transición trivial, prepara contexto)
  - extracting      → llama sv2 → persisting | extraction_failed
  - persisting      → llama sv3 → valuing | awaiting_contract_selection |
                                  completed_duplicate | persistence_failed
  - valuing         → llama sv6 → awaiting_approval | valuation_failed

Estados pasivos:
  - awaiting_contract_selection: el motor lo activa al recibir
    contract-selected (ver event_dispatcher).
  - awaiting_approval: idem con document-approved.

REGLA CRÍTICA del cliente sobre duplicados:
  "Si el contrato ya existe el orquestador no lo baja y pasa al
   siguiente paso." → cuando sv3 devuelve duplicate=true, sv7 NO
  termina automáticamente en completed_duplicate. Mira si el
  documento existente ya tiene valoración OK / aprobación, y decide:
   - documento aprobado / valoración OK aprobada → completed_duplicate
     terminal (no hay nada que hacer).
   - documento con valoración OK pero sin aprobar → awaiting_approval
     (saltarse extracción y persistencia, pasar al siguiente paso).
   - documento sin valoración → valuing (re-aprovechar lo extraído y
     persistido, valorar con el contrato seleccionado).
   - documento sin contrato → awaiting_contract_selection.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from domain.models.step_results import (
    ExtractResult,
    PersistResult,
    ValuationResult,
)
from domain.models.workflow import WorkflowRun, WorkflowState
from domain.ports.extractor_port import ExtractionError, ExtractorClient
from domain.ports.persister_port import PersisterClient, PersistenceError
from domain.ports.valuator_port import ValuationError, ValuatorClient

logger = logging.getLogger(__name__)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------- #
# Resultado de un step que el engine usa para tomar la decisión
# de la siguiente transición. Se devuelve junto al output_payload
# para auditoría.
# --------------------------------------------------------------- #
class StepOutcome:
    def __init__(
        self,
        *,
        next_state: WorkflowState,
        output_payload: dict[str, Any] | None = None,
        document_id: str | None = None,
        error: str | None = None,
        decision_log: str | None = None,
    ) -> None:
        self.next_state = next_state
        self.output_payload = output_payload or {}
        self.document_id = document_id
        self.error = error
        self.decision_log = decision_log


class AlbaranE2EWorkflow:
    """Handlers de las transiciones activas del workflow albaran_e2e."""

    def __init__(
        self,
        *,
        extractor: ExtractorClient,
        persister: PersisterClient,
        valuator: ValuatorClient,
    ) -> None:
        self._extractor = extractor
        self._persister = persister
        self._valuator = valuator

    # ----------------------------------------------------------- #
    # email_received → extracting (trivial)
    # ----------------------------------------------------------- #
    def handle_email_received(self, run: WorkflowRun) -> StepOutcome:
        logger.info(
            "decision: email_received OK → extracting",
            extra={"workflow_id": run.id},
        )
        return StepOutcome(next_state=WorkflowState.EXTRACTING)

    # ----------------------------------------------------------- #
    # extracting → persisting | extraction_failed
    # ----------------------------------------------------------- #
    def handle_extracting(self, run: WorkflowRun) -> StepOutcome:
        payload = run.payload
        file_path = Path(payload["file_path"])
        filename = payload["attachment_filename"]
        content_type = payload["attachment_content_type"]

        logger.info(
            "→ POST sv2 /v1/albaranes/extract file=%s size=%s",
            filename,
            payload.get("attachment_size_bytes"),
            extra={"workflow_id": run.id},
        )

        try:
            result: ExtractResult = self._extractor.extract(
                file_path=file_path,
                filename=filename,
                content_type=content_type,
            )
        except ExtractionError as exc:
            logger.error(
                "← sv2 FAILED: %s",
                exc,
                extra={"workflow_id": run.id},
            )
            return StepOutcome(
                next_state=WorkflowState.EXTRACTION_FAILED,
                error=str(exc),
            )

        logger.info(
            "← sv2 OK providers=%s confidence_pct=%s",
            result.providers_used,
            result.confidence_pct,
            extra={"workflow_id": run.id},
        )

        # Guardamos el envelope en el payload del workflow para que el
        # siguiente step (persisting) lo encuentre. Esto es importante
        # porque persisting puede ejecutarse en otro proceso si sv7
        # se reinicia (tras leer el estado de BBDD).
        run.payload["extraction_envelope"] = result.raw_envelope

        return StepOutcome(
            next_state=WorkflowState.PERSISTING,
            output_payload={
                "providers_used": result.providers_used,
                "confidence_pct": result.confidence_pct,
            },
        )

    # ----------------------------------------------------------- #
    # persisting → valuing | awaiting_contract_selection |
    #              completed_duplicate | persistence_failed
    # ----------------------------------------------------------- #
    def handle_persisting(
        self,
        run: WorkflowRun,
        *,
        existing_doc_resolver,
    ) -> StepOutcome:
        """``existing_doc_resolver`` es una callable inyectada por el
        engine que, dado un document_id, devuelve un dict con:
            {
                "is_approved": bool,
                "has_valuation": bool,
                "valuation_status": str | None,
                "selected_contrato_codigo": str | None,
            }
        Lo usamos cuando sv3 devuelve duplicate=true para decidir
        a qué estado saltar (regla del cliente: no bajar de nuevo, pasar
        al siguiente paso).
        """
        payload = run.payload
        file_path = Path(payload["file_path"])
        filename = payload["attachment_filename"]
        content_type = payload["attachment_content_type"]
        envelope = payload.get("extraction_envelope")
        if envelope is None:
            return StepOutcome(
                next_state=WorkflowState.PERSISTENCE_FAILED,
                error="envelope de extracción ausente en payload del workflow",
            )

        context = {
            "email_message_id": payload.get("email_message_id"),
            "from_address": payload.get("from_address"),
            "subject": payload.get("subject"),
            "page_number": payload.get("page_number"),
            "total_pages": payload.get("total_pages"),
        }

        logger.info(
            "→ POST sv3 /v1/albaranes/persist file=%s",
            filename,
            extra={"workflow_id": run.id},
        )

        try:
            result: PersistResult = self._persister.persist(
                file_path=file_path,
                filename=filename,
                content_type=content_type,
                extraction_envelope=envelope,
                context=context,
            )
        except PersistenceError as exc:
            logger.error("← sv3 FAILED: %s", exc, extra={"workflow_id": run.id})
            return StepOutcome(
                next_state=WorkflowState.PERSISTENCE_FAILED,
                error=str(exc),
            )

        logger.info(
            "← sv3 OK document_id=%s contratos=%s selected=%s duplicate=%s",
            result.document_id,
            result.contratos_count,
            result.selected_contrato_codigo,
            result.duplicate,
            extra={"workflow_id": run.id},
        )

        # ============================================================ #
        # CASO DUPLICADO — regla del cliente
        # ============================================================ #
        if result.duplicate:
            existing = existing_doc_resolver(result.document_id) or {}

            is_approved = bool(existing.get("is_approved"))
            has_valuation = bool(
                existing.get("has_valuation")
                or result.has_existing_valuation
            )
            val_status = (
                existing.get("valuation_status")
                or result.existing_valuation_status
            )
            selected = (
                existing.get("selected_contrato_codigo")
                or result.selected_contrato_codigo
            )

            if is_approved:
                decision = (
                    f"duplicate y document {result.document_id} ya APROBADO "
                    "→ completed_duplicate (terminal ok)"
                )
                logger.info("decision: %s", decision, extra={"workflow_id": run.id})
                return StepOutcome(
                    next_state=WorkflowState.COMPLETED_DUPLICATE,
                    output_payload={
                        "duplicate": True,
                        "reason": "already_approved",
                        "document_id": result.document_id,
                    },
                    document_id=result.document_id,
                    decision_log=decision,
                )

            if has_valuation and val_status == "ok":
                decision = (
                    f"duplicate y document {result.document_id} ya VALORADO "
                    "OK pero sin aprobar → awaiting_approval"
                )
                logger.info("decision: %s", decision, extra={"workflow_id": run.id})
                return StepOutcome(
                    next_state=WorkflowState.AWAITING_APPROVAL,
                    output_payload={
                        "duplicate": True,
                        "reason": "valuation_already_ok",
                        "document_id": result.document_id,
                        "selected_contrato_codigo": selected,
                    },
                    document_id=result.document_id,
                    decision_log=decision,
                )

            if not selected:
                decision = (
                    f"duplicate y document {result.document_id} SIN contrato "
                    "seleccionado → awaiting_contract_selection"
                )
                logger.info("decision: %s", decision, extra={"workflow_id": run.id})
                return StepOutcome(
                    next_state=WorkflowState.AWAITING_CONTRACT_SELECTION,
                    output_payload={
                        "duplicate": True,
                        "reason": "no_contract_selected",
                        "document_id": result.document_id,
                    },
                    document_id=result.document_id,
                    decision_log=decision,
                )

            # Tiene contrato seleccionado pero NO valoración OK → vamos a valuing.
            decision = (
                f"duplicate y document {result.document_id} con contrato "
                f"{selected} pero sin valoración → valuing"
            )
            logger.info("decision: %s", decision, extra={"workflow_id": run.id})
            run.payload["selected_contrato_codigo"] = selected
            return StepOutcome(
                next_state=WorkflowState.VALUING,
                output_payload={
                    "duplicate": True,
                    "reason": "needs_valuation",
                    "document_id": result.document_id,
                    "selected_contrato_codigo": selected,
                },
                document_id=result.document_id,
                decision_log=decision,
            )

        # ============================================================ #
        # CASO NO DUPLICADO — flujo normal
        # ============================================================ #
        if result.selected_contrato_codigo:
            decision = (
                f"contrato auto-seleccionado ({result.selected_contrato_codigo}) "
                "→ valuing directo"
            )
            logger.info("decision: %s", decision, extra={"workflow_id": run.id})
            run.payload["selected_contrato_codigo"] = result.selected_contrato_codigo
            return StepOutcome(
                next_state=WorkflowState.VALUING,
                output_payload={
                    "document_id": result.document_id,
                    "contratos_count": result.contratos_count,
                    "selected_contrato_codigo": result.selected_contrato_codigo,
                },
                document_id=result.document_id,
                decision_log=decision,
            )

        # 0 ó >1 contratos sin auto-selección.
        decision = (
            f"contratos={result.contratos_count} sin auto-selección "
            "→ awaiting_contract_selection"
        )
        logger.info("decision: %s", decision, extra={"workflow_id": run.id})
        return StepOutcome(
            next_state=WorkflowState.AWAITING_CONTRACT_SELECTION,
            output_payload={
                "document_id": result.document_id,
                "contratos_count": result.contratos_count,
            },
            document_id=result.document_id,
            decision_log=decision,
        )

    # ----------------------------------------------------------- #
    # valuing → awaiting_approval | valuation_failed
    # ----------------------------------------------------------- #
    def handle_valuing(self, run: WorkflowRun) -> StepOutcome:
        document_id = run.document_id
        if not document_id:
            return StepOutcome(
                next_state=WorkflowState.VALUATION_FAILED,
                error="document_id ausente al entrar a valuing",
            )

        codigo = run.payload.get("selected_contrato_codigo")
        is_revaluation = run.kind == "albaran_revaluation"

        logger.info(
            "→ POST sv6 %s document_id=%s codigo=%s",
            "/v1/valuation/{id}/re-run" if is_revaluation else "/v1/valuation/run",
            document_id,
            codigo,
            extra={"workflow_id": run.id},
        )

        try:
            if is_revaluation:
                result: ValuationResult = self._valuator.rerun(
                    document_id=document_id,
                    codigo_contrato=codigo,
                )
            else:
                result = self._valuator.run(
                    document_id=document_id,
                    codigo_contrato=codigo,
                    force=False,
                )
        except ValuationError as exc:
            logger.error("← sv6 FAILED: %s", exc, extra={"workflow_id": run.id})
            return StepOutcome(
                next_state=WorkflowState.VALUATION_FAILED,
                error=str(exc),
            )

        logger.info(
            "← sv6 status=%s valuation_id=%s total=%s lines=%s review_required=%s",
            result.status,
            result.valuation_id,
            result.total_valorado,
            result.total_lines,
            result.review_required,
            extra={"workflow_id": run.id},
        )

        if result.status == "ok":
            return StepOutcome(
                next_state=WorkflowState.AWAITING_APPROVAL,
                output_payload={
                    "valuation_id": result.valuation_id,
                    "total_valorado": result.total_valorado,
                    "total_lines": result.total_lines,
                    "review_required": result.review_required,
                },
            )

        # 'no_contract' u otros: marcar fallo y dejar que el revisor
        # corrija (o el retrier reintente si el contrato cambió).
        return StepOutcome(
            next_state=WorkflowState.VALUATION_FAILED,
            error=f"sv6 devolvió status={result.status}",
            output_payload={
                "valuation_id": result.valuation_id,
                "status": result.status,
            },
        )
