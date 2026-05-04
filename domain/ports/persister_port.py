# domain/ports/persister_port.py
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from domain.models.step_results import PersistResult


class PersisterClient(ABC):
    """Puerto de salida hacia sv3 (albaranes-persistence-api)."""

    @abstractmethod
    def persist(
        self,
        *,
        file_path: Path,
        filename: str,
        content_type: str,
        extraction_envelope: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> PersistResult:
        """Llama a sv3 /v1/albaranes/persist con multipart.

        ``context`` es un dict con metadatos del email (from_address,
        subject, etc.) que sv3 persiste como traza en
        albaran_documents_merge.context_json — opcional.

        Lanza PersistenceError tras agotar reintentos HTTP.
        """
        raise NotImplementedError


class PersistenceError(RuntimeError):
    """Error tras agotar reintentos contra sv3."""
