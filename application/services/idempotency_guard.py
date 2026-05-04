# application/services/idempotency_guard.py
from __future__ import annotations

import logging

from domain.models.workflow import WorkflowRun
from domain.ports.workflow_repository import WorkflowRepository

logger = logging.getLogger(__name__)


class IdempotencyGuard:
    """Centraliza la comprobación de duplicados a nivel sv7.

    Antes de crear un workflow nuevo desde un evento email-received,
    se busca por ``correlation_key`` (UNIQUE en BBDD). Si ya existe,
    se devuelve el workflow_id existente sin reprocesar.
    """

    def __init__(self, repository: WorkflowRepository) -> None:
        self._repo = repository

    def check_correlation(self, correlation_key: str) -> WorkflowRun | None:
        """Devuelve el workflow existente o None si es nuevo."""
        existing = self._repo.find_by_correlation_key(correlation_key)
        if existing is not None:
            logger.info(
                "correlation_key=%s ya existe → wf=%s state=%s",
                correlation_key,
                existing.id,
                existing.current_state.value,
                extra={"workflow_id": existing.id},
            )
        return existing
