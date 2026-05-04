# application/services/failed_workflow_retrier.py
"""Job interno periódico que reabre workflows en estado *_failed.

Se ejecuta como asyncio task lanzada en el lifespan de FastAPI. Cada
``interval_s`` segundos:
  1. Busca workflows en (extraction_failed | persistence_failed |
     valuation_failed) con retry_count < max_auto_retries y
     updated_at_utc anterior a now - min_age_s.
  2. Para cada uno: incrementa retry_count, transita al estado activo
     correspondiente, y dispara run_until_passive en un thread.

Si max_auto_retries = 0, el job no hace nada (retry solo manual).

CRÍTICO: el motor (WorkflowEngine.run_until_passive) es síncrono. Lo
ejecutamos en un thread (asyncio.to_thread) para no bloquear el loop.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from application.services.workflow_engine import WorkflowEngine
from domain.models.workflow import RETRY_TARGET_STATE, WorkflowRun, WorkflowState
from domain.ports.workflow_repository import WorkflowRepository

logger = logging.getLogger(__name__)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class FailedWorkflowRetrier:
    def __init__(
        self,
        *,
        repository: WorkflowRepository,
        engine: WorkflowEngine,
        interval_s: int,
        min_age_s: int,
        max_retries: int,
    ) -> None:
        self._repo = repository
        self._engine = engine
        self._interval_s = max(5, interval_s)
        self._min_age_s = max(0, min_age_s)
        self._max_retries = max(0, max_retries)
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    @property
    def enabled(self) -> bool:
        return self._max_retries > 0

    async def start(self) -> None:
        if not self.enabled:
            logger.info(
                "FailedWorkflowRetrier DESACTIVADO (max_auto_retries=0)"
            )
            return
        if self._task is not None:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="failed-wf-retrier")
        logger.info(
            "FailedWorkflowRetrier ACTIVO interval=%ss min_age=%ss max_retries=%d",
            self._interval_s,
            self._min_age_s,
            self._max_retries,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stopping.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            self._task.cancel()
        self._task = None
        logger.info("FailedWorkflowRetrier detenido")

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await asyncio.to_thread(self._tick)
            except Exception:
                logger.exception("error en tick del retrier")
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=self._interval_s,
                )
            except asyncio.TimeoutError:
                pass

    def _tick(self) -> None:
        candidates = self._repo.list_failed_for_auto_retry(
            max_retry_count=self._max_retries,
            min_age_seconds=self._min_age_s,
            limit=20,
        )
        if not candidates:
            return
        logger.info("retrier: %d workflow(s) elegibles para auto-retry", len(candidates))
        for run in candidates:
            self._reopen_one(run)

    def _reopen_one(self, run: WorkflowRun) -> None:
        target = RETRY_TARGET_STATE.get(run.current_state)
        if target is None:
            logger.warning(
                "retrier: estado no mapeado %s (skip)",
                run.current_state.value,
                extra={"workflow_id": run.id},
            )
            return

        previous = run.current_state
        run.retry_count += 1
        run.current_state = target
        run.updated_at_utc = _utc_iso()
        run.last_error = None
        self._repo.update(run)
        logger.info(
            "retrier: state %s → %s (auto retry #%d)",
            previous.value,
            target.value,
            run.retry_count,
            extra={"workflow_id": run.id},
        )
        # Ejecutamos sincrónicamente el motor en este mismo thread.
        # _tick() ya corre en to_thread(), así que no bloqueamos el loop.
        try:
            self._engine.run_until_passive(run.id)
        except Exception:
            logger.exception(
                "retrier: error ejecutando workflow tras reabrir",
                extra={"workflow_id": run.id},
            )
