# infrastructure/clients/http_extractor_client.py
from __future__ import annotations

import logging
from pathlib import Path

import httpx

from application.retry.http_retry_policy import HttpRetryPolicy
from domain.models.step_results import ExtractResult
from domain.ports.extractor_port import ExtractionError, ExtractorClient

logger = logging.getLogger(__name__)


class HttpExtractorClient(ExtractorClient):
    """Adapter HTTP del puerto ExtractorClient → sv2."""

    def __init__(
        self,
        *,
        base_url: str,
        path_extract: str,
        timeout_s: float,
        retry_policy: HttpRetryPolicy,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._path = path_extract
        self._timeout_s = timeout_s
        self._retry = retry_policy

    def extract(
        self,
        *,
        file_path: Path,
        filename: str,
        content_type: str,
    ) -> ExtractResult:
        url = f"{self._base_url}{self._path}"

        def _do() -> httpx.Response:
            with file_path.open("rb") as fp:
                files = {"file": (filename, fp, content_type)}
                with httpx.Client(timeout=self._timeout_s) as client:
                    return client.post(url, files=files)

        try:
            response = self._retry.execute(_do, operation_name=f"sv2 POST {self._path}")
        except RuntimeError as exc:
            raise ExtractionError(str(exc)) from exc

        try:
            payload = response.json()
        except Exception as exc:
            raise ExtractionError(f"sv2 devolvió payload no-JSON: {exc}") from exc

        # El envelope de sv2 incluye 'merged' y 'providers'; extraemos
        # confianza del merged si está, y la lista de providers usados.
        merged = payload.get("merged", {}) if isinstance(payload, dict) else {}
        providers = list((payload.get("providers") or {}).keys()) if isinstance(payload, dict) else []
        confidence_pct = None
        if isinstance(merged, dict):
            confidence_pct = merged.get("confidence_pct_calc")

        return ExtractResult(
            raw_envelope=payload,
            providers_used=providers,
            confidence_pct=confidence_pct,
        )
