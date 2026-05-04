# domain/ports/extractor_port.py
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from domain.models.step_results import ExtractResult


class ExtractorClient(ABC):
    """Puerto de salida hacia sv2 (albaranes-extractor-api)."""

    @abstractmethod
    def extract(
        self,
        *,
        file_path: Path,
        filename: str,
        content_type: str,
    ) -> ExtractResult:
        """Llama a sv2 /v1/albaranes/extract.

        Lanza:
          - ExtractionError tras agotar reintentos HTTP (3 attempts).
          - ValidationError si la respuesta no encaja con el schema.
        """
        raise NotImplementedError


class ExtractionError(RuntimeError):
    """Error tras agotar reintentos contra sv2."""
