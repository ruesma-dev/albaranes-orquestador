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

    def check_attachment(self, attachment_sha256: str) -> WorkflowRun | None:
        """Dedup por CONTENIDO del PDF: devuelve un run anterior con la
        misma huella cuyo estado NO sea fallido (en curso o completado-ok),
        o None si no hay (o solo hay fallidos -> se permite reprocesar)."""
        if not attachment_sha256:
            return None
        existing = self._repo.find_latest_by_attachment_sha256(attachment_sha256)
        if existing is not None:
            logger.info(
                "attachment_sha256=%s ya procesado -> wf=%s state=%s (dedup)",
                attachment_sha256,
                existing.id,
                existing.current_state.value,
                extra={"workflow_id": existing.id},
            )
        return existing

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
