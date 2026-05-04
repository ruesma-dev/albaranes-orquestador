# domain/models/workflow.py
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal


class WorkflowState(str, Enum):
    """Estados posibles del workflow albaran_e2e.

    Activos (orquestador trabajando):
      - email_received        — recién creado, transita inmediato a extracting
      - extracting            — llamada a sv2 fase 1 en curso
      - reviewing             — llamada a sv2 fase 2 en curso (NUEVO)
      - persisting            — llamada a sv3 en curso
      - valuing               — llamada a sv6 en curso

    Pasivos (esperando evento del revisor):
      - awaiting_contract_selection
      - awaiting_approval

    Terminales OK:
      - approved
      - completed_duplicate

    Terminales con fallo (retryable, manual o auto):
      - extraction_failed
      - review_failed         (NUEVO — fase 2 inutilizable)
      - persistence_failed
      - valuation_failed
    """

    EMAIL_RECEIVED = "email_received"
    EXTRACTING = "extracting"
    REVIEWING = "reviewing"
    PERSISTING = "persisting"
    AWAITING_CONTRACT_SELECTION = "awaiting_contract_selection"
    VALUING = "valuing"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    COMPLETED_DUPLICATE = "completed_duplicate"
    EXTRACTION_FAILED = "extraction_failed"
    REVIEW_FAILED = "review_failed"
    PERSISTENCE_FAILED = "persistence_failed"
    VALUATION_FAILED = "valuation_failed"


# Estados que pueden retomarse automáticamente al arrancar (proceso
# reiniciado mientras el workflow estaba activo) o tras un *_failed
# si el retrier los reabre.
ACTIVE_STATES = frozenset({
    WorkflowState.EMAIL_RECEIVED,
    WorkflowState.EXTRACTING,
    WorkflowState.REVIEWING,
    WorkflowState.PERSISTING,
    WorkflowState.VALUING,
})

# Estados pasivos: no se hacen nada, esperan evento externo.
WAITING_STATES = frozenset({
    WorkflowState.AWAITING_CONTRACT_SELECTION,
    WorkflowState.AWAITING_APPROVAL,
})

# Estados terminales OK.
TERMINAL_OK_STATES = frozenset({
    WorkflowState.APPROVED,
    WorkflowState.COMPLETED_DUPLICATE,
})

# Estados terminales con fallo (retryable).
FAILED_STATES = frozenset({
    WorkflowState.EXTRACTION_FAILED,
    WorkflowState.REVIEW_FAILED,
    WorkflowState.PERSISTENCE_FAILED,
    WorkflowState.VALUATION_FAILED,
})

TERMINAL_STATES = TERMINAL_OK_STATES | FAILED_STATES


# Mapa de qué estado activo retoma el retrier dado un estado fallido.
RETRY_TARGET_STATE: dict[WorkflowState, WorkflowState] = {
    WorkflowState.EXTRACTION_FAILED: WorkflowState.EXTRACTING,
    WorkflowState.REVIEW_FAILED: WorkflowState.REVIEWING,
    WorkflowState.PERSISTENCE_FAILED: WorkflowState.PERSISTING,
    WorkflowState.VALUATION_FAILED: WorkflowState.VALUING,
}


WorkflowKind = Literal["albaran_e2e", "albaran_revaluation"]


@dataclass
class WorkflowRun:
    """Entidad principal — representa un workflow en curso."""

    id: str
    kind: WorkflowKind
    correlation_key: str
    current_state: WorkflowState
    payload: dict
    started_at_utc: str
    updated_at_utc: str
    parent_workflow_id: str | None = None
    document_id: str | None = None
    completed_at_utc: str | None = None
    last_error: str | None = None
    retry_count: int = 0
    pending_event: dict | None = None


@dataclass
class StepHistoryEntry:
    """Entrada en workflow_step_history (auditoría)."""

    workflow_run_id: str
    step_name: str
    status: Literal["started", "success", "failure", "skipped"]
    attempt: int
    started_at_utc: str
    input_payload: dict | None = None
    output_payload: dict | None = None
    error: str | None = None
    completed_at_utc: str | None = None
    duration_ms: int | None = None
    id: int | None = None
