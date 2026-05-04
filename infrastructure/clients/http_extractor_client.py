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
    """Adapter HTTP del puerto ExtractorClient → sv2 /extract/phase-1.

    CAMBIO RESPECTO A LA VERSIÓN ANTERIOR:
      - Antes el sv2 devolvía un envelope multi-proveedor con bloques
        ``providers`` y ``merged``. Ahora la fase-1 devuelve UN solo
        proveedor con la forma {meta: {...provider, model...}, data,
        debug}. Ya no hay 'merged' ni lista de providers.
      - ExtractResult.provider_used es ahora un string (no lista).
        Lo extraemos de envelope.meta.provider.
      - confidence_pct se intenta leer de envelope.data si existe;
        si no, queda None (sv7 no lo necesita para decidir, solo es
        telemetría en logs).
    """

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
            response = self._retry.execute(
                _do,
                operation_name=f"sv2 POST {self._path}",
            )
        except RuntimeError as exc:
            raise ExtractionError(str(exc)) from exc

        try:
            payload = response.json()
        except Exception as exc:
            raise ExtractionError(
                f"sv2 devolvió payload no-JSON: {exc}"
            ) from exc

        # Schema esperado del envelope phase-1:
        #   { "meta": {provider, model, prompt_key, ...},
        #     "data": {DocumentoAlbaran...},
        #     "debug": {...} }
        meta = payload.get("meta") or {} if isinstance(payload, dict) else {}
        data = payload.get("data") or {} if isinstance(payload, dict) else {}

        provider_used = str(meta.get("provider", "?"))
        # confidence_pct es opcional en el data (depende del schema).
        # Lo intentamos leer pero no obligamos.
        confidence_pct = None
        if isinstance(data, dict):
            cp = data.get("confidence_pct") or data.get("confidence_pct_calc")
            if isinstance(cp, (int, float)):
                confidence_pct = float(cp)

        return ExtractResult(
            raw_envelope=payload,
            provider_used=provider_used,
            confidence_pct=confidence_pct,
        )
