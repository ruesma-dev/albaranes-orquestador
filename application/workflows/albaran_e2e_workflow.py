# application/workflows/albaran_e2e_workflow.py
"""Workflow concreto: ingesta end-to-end de un albarán con DOS fases IA.

Estados activos manejados:
  - email_received  → extracting (transición trivial, prepara contexto)
  - extracting      → llama sv2 phase-1 → reviewing | extraction_failed
  - reviewing       → llama sv2 phase-2 → persisting | review_failed
  - persisting      → llama sv3 → valuing | awaiting_contract_selection |
                                  completed_duplicate | persistence_failed
  - valuing         → llama sv6 → awaiting_approval | valuation_failed

FLAG ``apply_phase_2_patch`` (configurable por env APPLY_PHASE_2_PATCH):
  - true  → tras fase 2, los cambios propuestos se APLICAN sobre el JSON
            de fase 1 antes de mandarlo a sv3. Las líneas modificadas
            quedan marcadas con source_phase='phase_2' en BBDD.
  - false → SHADOW MODE: fase 2 se ejecuta igual y los cambios se
            persisten como AUDITORÍA en review_phase2_payload_json,
            pero el data que se manda a sv3 es el de fase 1 SIN tocar.
            source_phase queda 'phase_1' en todas las líneas.
            Útil para validar fase 2 sin contaminar BBDD.

REGLA CRÍTICA del cliente sobre duplicados (mantenida sin cambios):
  Si sv3 devuelve duplicate=true, sv7 NO termina automáticamente en
  completed_duplicate: mira si el documento existente ya tiene
  valoración OK / aprobación, y decide siguiente paso.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from application.utils.apply_patch_to_envelope import (
    build_review_metadata,
    compute_phase2_line_indices,
    extract_documento_revisado,
)
from domain.models.step_results import (
    ExtractResult,
    PersistResult,
    ReviewResult,
    ValuationResult,
)
from domain.models.workflow import WorkflowRun, WorkflowState
from domain.ports.extractor_port import ExtractionError, ExtractorClient
from domain.ports.persister_port import PersisterClient, PersistenceError
from domain.ports.reviewer_port import ReviewerClient, ReviewError
from domain.ports.valuator_port import ValuationError, ValuatorClient

logger = logging.getLogger(__name__)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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
        reviewer: ReviewerClient,
        persister: PersisterClient,
        valuator: ValuatorClient,
        apply_phase_2_patch: bool = True,
    ) -> None:
        self._extractor = extractor
        self._reviewer = reviewer
        self._persister = persister
        self._valuator = valuator
        self._apply_phase_2_patch = apply_phase_2_patch
        logger.info(
            "[wf] AlbaranE2EWorkflow construido. apply_phase_2_patch=%s",
            apply_phase_2_patch,
        )

    # ----------------------------------------------------------- #
    # email_received → extracting
    # ----------------------------------------------------------- #
    def handle_email_received(self, run: WorkflowRun) -> StepOutcome:
        logger.info(
            "decision: email_received OK → extracting",
            extra={"workflow_id": run.id},
        )
        return StepOutcome(next_state=WorkflowState.EXTRACTING)

    # ----------------------------------------------------------- #
    # extracting → reviewing | extraction_failed
    # ----------------------------------------------------------- #
    def handle_extracting(self, run: WorkflowRun) -> StepOutcome:
        payload = run.payload
        file_path = Path(payload["file_path"])
        filename = payload["attachment_filename"]
        content_type = payload["attachment_content_type"]

        logger.info(
            "→ POST sv2 /v1/albaranes/extract/phase-1 file=%s size=%s",
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
                "← sv2 phase-1 FAILED: %s",
                exc,
                extra={"workflow_id": run.id},
            )
            return StepOutcome(
                next_state=WorkflowState.EXTRACTION_FAILED,
                error=str(exc),
            )

        logger.info(
            "← sv2 phase-1 OK provider=%s confidence_pct=%s",
            result.provider_used,
            result.confidence_pct,
            extra={"workflow_id": run.id},
        )

        # Guardamos el envelope de fase 1 en el payload del workflow.
        run.payload["phase_1_envelope"] = result.raw_envelope

        return StepOutcome(
            next_state=WorkflowState.REVIEWING,
            output_payload={
                "provider_used": result.provider_used,
                "confidence_pct": result.confidence_pct,
            },
        )

    # ----------------------------------------------------------- #
    # reviewing → persisting | review_failed
    #
    # Política ante fallo de fase 2:
    #   - Si sv2 phase-2 falla en HTTP/red → review_failed (retryable).
    #   - Si fase 2 devuelve review_status="inconsistent" → persistimos
    #     IGUALMENTE (decisión validada con el usuario: "siempre
    #     persistimos"). Si el flag apply_phase_2_patch=true, se aplican
    #     los cambios propuestos. Si false, no.
    #
    # Política según el flag apply_phase_2_patch:
    #   - true  → merged_data = fase_1_data CON el patch aplicado;
    #             líneas tocadas marcadas source_phase='phase_2'.
    #   - false → merged_data = fase_1_data SIN tocar (shadow mode);
    #             review_phase2_metadata se incluye igualmente para
    #             auditoría en sv3.
    # ----------------------------------------------------------- #
    def handle_reviewing(self, run: WorkflowRun) -> StepOutcome:
        payload = run.payload
        file_path = Path(payload["file_path"])
        filename = payload["attachment_filename"]
        content_type = payload["attachment_content_type"]

        envelope_phase_1 = payload.get("phase_1_envelope")
        if not envelope_phase_1:
            return StepOutcome(
                next_state=WorkflowState.REVIEW_FAILED,
                error="phase_1_envelope ausente al entrar en reviewing",
            )
        phase_1_data = envelope_phase_1.get("data") or {}

        logger.info(
            "→ POST sv2 /v1/albaranes/extract/phase-2 file=%s",
            filename,
            extra={"workflow_id": run.id},
        )
        try:
            review: ReviewResult = self._reviewer.review(
                file_path=file_path,
                filename=filename,
                content_type=content_type,
                phase_1_data=phase_1_data,
            )
        except ReviewError as exc:
            logger.error(
                "← sv2 phase-2 FAILED: %s",
                exc,
                extra={"workflow_id": run.id},
            )
            return StepOutcome(
                next_state=WorkflowState.REVIEW_FAILED,
                error=str(exc),
            )

        logger.info(
            "← sv2 phase-2 OK provider=%s status=%s changes=%d",
            review.provider_used,
            review.review_status,
            review.changes_count,
            extra={"workflow_id": run.id},
        )

        # ----- Nuevo flujo: la fase 2 devuelve documento_revisado completo ----- #
        documento_revisado = extract_documento_revisado(review.raw_envelope)
        if documento_revisado is None:
            return StepOutcome(
                next_state=WorkflowState.REVIEW_FAILED,
                error=(
                    "Fase 2 no devolvió 'documento_revisado' válido en el "
                    "envelope. Revisa el prompt o la respuesta de la IA."
                ),
            )

        # Decidir qué data se manda a sv3 según el flag.
        if self._apply_phase_2_patch:
            # Modo merge: el documento revisado SUSTITUYE al de fase 1.
            data_for_sv3 = documento_revisado
            phase_2_line_indices = compute_phase2_line_indices(
                data_phase_1=phase_1_data,
                data_revised=documento_revisado,
            )
            mode_label = "merge"
        else:
            # Modo shadow: NO aplicamos los cambios, mandamos fase 1.
            # Pero los razonamientos de fase 2 viajan en metadata para
            # auditoría.
            data_for_sv3 = phase_1_data
            phase_2_line_indices = []
            mode_label = "shadow"

        # Construir el envelope final que se le pasará a sv3.
        review_metadata = build_review_metadata(
            review_envelope=review.raw_envelope,
            phase_2_line_indices=phase_2_line_indices,
        )

        # SANEO DEL META para que cumpla con el schema estricto
        # ExtractionMeta de sv3 (extra='forbid'). sv2 fase-1 añade
        # 'phase' y 'provider' al meta — son info INTERNA del flujo,
        # no del documento. Las quitamos antes de cruzar la frontera
        # hacia sv3.
        meta_for_sv3 = {
            k: v
            for k, v in (envelope_phase_1.get("meta") or {}).items()
            if k not in ("phase", "provider")
        }

        merged_envelope = {
            "meta": meta_for_sv3,
            "data": data_for_sv3,
            "debug": envelope_phase_1.get("debug") or {},
            "review_phase2_metadata": review_metadata,
        }
        run.payload["merged_envelope_for_persist"] = merged_envelope

        decision = (
            f"phase_2 status={review.review_status} mode={mode_label} "
            f"razonamientos={review.changes_count} "
            f"lines_phase2={len(phase_2_line_indices)}"
        )
        logger.info(
            "decision: %s → persisting",
            decision,
            extra={"workflow_id": run.id},
        )

        return StepOutcome(
            next_state=WorkflowState.PERSISTING,
            output_payload={
                "review_status": review.review_status,
                "review_provider": review.provider_used,
                "review_mode": mode_label,
                "changes_count": review.changes_count,
                "lines_phase2_count": len(phase_2_line_indices),
            },
            decision_log=decision,
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
        payload = run.payload
        file_path = Path(payload["file_path"])
        filename = payload["attachment_filename"]
        content_type = payload["attachment_content_type"]
        envelope = payload.get("merged_envelope_for_persist")
        if envelope is None:
            return StepOutcome(
                next_state=WorkflowState.PERSISTENCE_FAILED,
                error=(
                    "merged_envelope_for_persist ausente — fase 2 no produjo "
                    "envelope final"
                ),
            )

        context = {
            "email_message_id": payload.get("email_message_id"),
            "from_address": payload.get("from_address"),
            "subject": payload.get("subject"),
            "page_number": payload.get("page_number"),
            "total_pages": payload.get("total_pages"),
        }

        logger.info(
            "→ POST sv3 /v1/albaranes/persist file=%s "
            "review_phase2_status=%s changes_count=%d",
            filename,
            (envelope.get("review_phase2_metadata") or {}).get("review_phase2_status"),
            (envelope.get("review_phase2_metadata") or {}).get("review_phase2_changes_count"),
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

        # CASO DUPLICADO — regla del cliente.
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
                logger.info(
                    "decision: %s",
                    decision,
                    extra={"workflow_id": run.id},
                )
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
                logger.info(
                    "decision: %s",
                    decision,
                    extra={"workflow_id": run.id},
                )
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
                logger.info(
                    "decision: %s",
                    decision,
                    extra={"workflow_id": run.id},
                )
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

            decision = (
                f"duplicate y document {result.document_id} con contrato "
                f"{selected} pero sin valoración → valuing"
            )
            logger.info(
                "decision: %s",
                decision,
                extra={"workflow_id": run.id},
            )
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

        # CASO NO DUPLICADO — flujo normal.
        if result.selected_contrato_codigo:
            decision = (
                f"contrato auto-seleccionado ({result.selected_contrato_codigo}) "
                "→ valuing directo"
            )
            logger.info(
                "decision: %s",
                decision,
                extra={"workflow_id": run.id},
            )
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

        decision = (
            f"contratos={result.contratos_count} sin auto-selección "
            "→ awaiting_contract_selection"
        )
        logger.info(
            "decision: %s",
            decision,
            extra={"workflow_id": run.id},
        )
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
    # (sin cambios respecto a la versión anterior)
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

        return StepOutcome(
            next_state=WorkflowState.VALUATION_FAILED,
            error=f"sv6 devolvió status={result.status}",
            output_payload={
                "valuation_id": result.valuation_id,
                "status": result.status,
            },
        )
