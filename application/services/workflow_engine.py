# application/services/workflow_engine.py
"""Motor genérico de ejecución de workflows.

Responsabilidades:
  - Cargar el WorkflowRun de BBDD por id.
  - Ejecutar la transición que toque para su current_state activo,
    delegando al workflow concreto (AlbaranE2EWorkflow).
  - Persistir el cambio de estado y registrar workflow_step_history
    para auditoría.
  - Encadenar transiciones automáticas mientras current_state ∈ ACTIVE_STATES.
  - Detenerse cuando entra en estado pasivo (waiting_*) o terminal.

NO conoce los workflows concretos: solo conoce el patrón de ejecución.
La selección de qué método invocar la hace por el ``current_state`` y
una tabla interna de handlers que se registra al construir el engine.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from application.workflows.albaran_e2e_workflow import (
    AlbaranE2EWorkflow,
    StepOutcome,
)
from domain.models.workflow import (
    ACTIVE_STATES,
    StepHistoryEntry,
    TERMINAL_STATES,
    WorkflowRun,
    WorkflowState,
)
from domain.ports.workflow_repository import WorkflowRepository

logger = logging.getLogger(__name__)


# Nombre del step que se registra en step_history por cada transición activa.
_STEP_NAME_BY_STATE = {
    WorkflowState.EMAIL_RECEIVED: "ingest_event",
    WorkflowState.EXTRACTING: "extract_with_llm",
    WorkflowState.PERSISTING: "persist_albaran",
    WorkflowState.VALUING: "run_valuation",
}


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class WorkflowEngine:
    """Motor que ejecuta transiciones activas hasta llegar a un estado
    pasivo o terminal."""

    def __init__(
        self,
        *,
        repository: WorkflowRepository,
        workflow: AlbaranE2EWorkflow,
        existing_doc_resolver: Callable[[str], dict | None],
        tmpdir_cleanup: Callable[[str], None] | None = None,
    ) -> None:
        self._repo = repository
        self._workflow = workflow
        self._existing_doc_resolver = existing_doc_resolver
        self._tmpdir_cleanup = tmpdir_cleanup

    # ----------------------------------------------------------- #
    # Crear un workflow nuevo (lo invoca el dispatcher al recibir
    # email-received tras pasar el guard de idempotencia).
    # ----------------------------------------------------------- #
    def create_workflow(
        self,
        *,
        kind: str,
        correlation_key: str,
        payload: dict,
        document_id: str | None = None,
        parent_workflow_id: str | None = None,
        initial_state: WorkflowState = WorkflowState.EMAIL_RECEIVED,
    ) -> WorkflowRun:
        now = _utc_iso()
        run = WorkflowRun(
            id=str(uuid.uuid4()),
            kind=kind,  # type: ignore[arg-type]
            correlation_key=correlation_key,
            current_state=initial_state,
            payload=payload,
            started_at_utc=now,
            updated_at_utc=now,
            parent_workflow_id=parent_workflow_id,
            document_id=document_id,
        )
        self._repo.insert(run)
        logger.info(
            "CREATED state=%s kind=%s correlation_key=%s",
            run.current_state.value,
            run.kind,
            correlation_key,
            extra={"workflow_id": run.id},
        )
        return run

    # ----------------------------------------------------------- #
    # Loop de ejecución: ejecuta transiciones mientras el estado sea
    # activo. Se ejecuta dentro de un BackgroundTask de FastAPI.
    # ----------------------------------------------------------- #
    def run_until_passive(self, workflow_id: str) -> None:
        run = self._repo.find_by_id(workflow_id)
        if run is None:
            logger.error("workflow %s no encontrado", workflow_id)
            return
        self._loop(run)

    def _loop(self, run: WorkflowRun) -> None:
        max_steps = 10  # safety: evita bucles si hay un bug.
        steps_done = 0

        while run.current_state in ACTIVE_STATES and steps_done < max_steps:
            steps_done += 1
            self._execute_one_step(run)
            run = self._repo.find_by_id(run.id) or run

        if run.current_state in TERMINAL_STATES:
            self._on_terminal(run)
        else:
            logger.info(
                "WAITING for event in state=%s",
                run.current_state.value,
                extra={"workflow_id": run.id},
            )

    def _execute_one_step(self, run: WorkflowRun) -> None:
        state = run.current_state
        step_name = _STEP_NAME_BY_STATE.get(state, state.value)
        attempt = run.retry_count + 1

        logger.info(
            "step=%s STARTED attempt=%d",
            step_name,
            attempt,
            extra={"workflow_id": run.id, "step_name": step_name},
        )
        step = StepHistoryEntry(
            workflow_run_id=run.id,
            step_name=step_name,
            status="started",
            attempt=attempt,
            started_at_utc=_utc_iso(),
        )
        step.id = self._repo.append_step(step)

        t0 = time.time()
        try:
            outcome = self._dispatch_handler(run)
        except Exception as exc:  # pragma: no cover (defensivo)
            logger.exception(
                "step=%s CRASHED: %s",
                step_name,
                exc,
                extra={"workflow_id": run.id},
            )
            outcome = StepOutcome(
                next_state=self._failure_state_for(state),
                error=f"crash: {type(exc).__name__}: {exc}",
            )

        duration_ms = int((time.time() - t0) * 1000)

        # Cerramos el step en step_history.
        step.status = "success" if outcome.error is None else "failure"
        step.completed_at_utc = _utc_iso()
        step.duration_ms = duration_ms
        step.output_payload = outcome.output_payload
        step.error = outcome.error
        self._repo.update_step(step)

        if outcome.error is None:
            logger.info(
                "step=%s SUCCESS duration=%.3fs",
                step_name,
                duration_ms / 1000.0,
                extra={"workflow_id": run.id},
            )
        else:
            logger.error(
                "step=%s FAILED duration=%.3fs error=%s",
                step_name,
                duration_ms / 1000.0,
                outcome.error,
                extra={"workflow_id": run.id},
            )

        # Aplicamos transición.
        previous = run.current_state
        run.current_state = outcome.next_state
        run.updated_at_utc = _utc_iso()
        if outcome.document_id and not run.document_id:
            run.document_id = outcome.document_id
            logger.info(
                "linking workflow.document_id=%s",
                outcome.document_id,
                extra={"workflow_id": run.id},
            )
        if outcome.error:
            run.last_error = outcome.error
        else:
            run.last_error = None  # limpia error previo si hubo retry exitoso

        logger.info(
            "state: %s → %s",
            previous.value,
            run.current_state.value,
            extra={"workflow_id": run.id},
        )

        self._repo.update(run)

    def _dispatch_handler(self, run: WorkflowRun) -> StepOutcome:
        state = run.current_state
        if state == WorkflowState.EMAIL_RECEIVED:
            return self._workflow.handle_email_received(run)
        if state == WorkflowState.EXTRACTING:
            return self._workflow.handle_extracting(run)
        if state == WorkflowState.PERSISTING:
            return self._workflow.handle_persisting(
                run,
                existing_doc_resolver=self._existing_doc_resolver,
            )
        if state == WorkflowState.VALUING:
            return self._workflow.handle_valuing(run)
        raise RuntimeError(
            f"_dispatch_handler() invocado con estado no activo: {state}"
        )

    @staticmethod
    def _failure_state_for(state: WorkflowState) -> WorkflowState:
        return {
            WorkflowState.EXTRACTING: WorkflowState.EXTRACTION_FAILED,
            WorkflowState.PERSISTING: WorkflowState.PERSISTENCE_FAILED,
            WorkflowState.VALUING: WorkflowState.VALUATION_FAILED,
        }.get(state, WorkflowState.EXTRACTION_FAILED)

    def _on_terminal(self, run: WorkflowRun) -> None:
        run.completed_at_utc = _utc_iso()
        run.updated_at_utc = run.completed_at_utc
        self._repo.update(run)
        try:
            duration_s = (
                datetime.fromisoformat(run.completed_at_utc.replace("Z", "+00:00"))
                - datetime.fromisoformat(run.started_at_utc.replace("Z", "+00:00"))
            ).total_seconds()
        except Exception:
            duration_s = 0.0
        logger.info(
            "COMPLETED state=%s total_duration=%.3fs",
            run.current_state.value,
            duration_s,
            extra={"workflow_id": run.id},
        )
        # Limpiamos el adjunto temporal si lo había.
        file_path = run.payload.get("file_path") if run.payload else None
        if file_path and self._tmpdir_cleanup is not None:
            try:
                self._tmpdir_cleanup(file_path)
            except Exception:
                logger.warning(
                    "no se pudo limpiar tmpfile=%s",
                    file_path,
                    extra={"workflow_id": run.id},
                )

    # ----------------------------------------------------------- #
    # Reanudar workflows tras un evento pasivo (contract-selected,
    # document-approved). Lo llama el event_dispatcher.
    # ----------------------------------------------------------- #
    def resume_from_passive(
        self,
        workflow_id: str,
        *,
        next_state: WorkflowState,
        event_payload: dict | None = None,
    ) -> None:
        run = self._repo.find_by_id(workflow_id)
        if run is None:
            return
        previous = run.current_state
        run.current_state = next_state
        run.updated_at_utc = _utc_iso()
        if event_payload:
            run.payload.update(
                {k: v for k, v in event_payload.items() if v is not None}
            )
        self._repo.update(run)
        logger.info(
            "state: %s → %s (resumed by event)",
            previous.value,
            next_state.value,
            extra={"workflow_id": run.id},
        )
        if next_state in ACTIVE_STATES:
            self._loop(run)
        elif next_state in TERMINAL_STATES:
            self._on_terminal(run)
