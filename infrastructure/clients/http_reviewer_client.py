# infrastructure/clients/http_reviewer_client.py
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from application.retry.http_retry_policy import HttpRetryPolicy
from domain.models.step_results import ReviewResult
from domain.ports.reviewer_port import ReviewError, ReviewerClient

logger = logging.getLogger(__name__)


class HttpReviewerClient(ReviewerClient):
    """Adapter HTTP del puerto ReviewerClient → sv2 /extract/phase-2."""

    def __init__(
        self,
        *,
        base_url: str,
        path_review: str,
        timeout_s: float,
        retry_policy: HttpRetryPolicy,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._path = path_review
        self._timeout_s = timeout_s
        self._retry = retry_policy

    def review(
        self,
        *,
        file_path: Path,
        filename: str,
        content_type: str,
        phase_1_data: dict[str, Any],
        sigrid_context: dict[str, Any] | None = None,
    ) -> ReviewResult:
        url = f"{self._base_url}{self._path}"

        def _do() -> httpx.Response:
            with file_path.open("rb") as fp:
                files = {"file": (filename, fp, content_type)}
                # sv2 espera el JSON de fase 1 como Form field 'phase_1_json'.
                data = {
                    "phase_1_json": json.dumps(
                        phase_1_data, ensure_ascii=False
                    ),
                }
                # Grounding Sigrid (jun 2026): opcional. sv2 lo inyecta
                # en el prompt de fase 2. Si no viaja, fase 2 funciona
                # como siempre.
                if sigrid_context:
                    data["sigrid_context_json"] = json.dumps(
                        sigrid_context, ensure_ascii=False
                    )
                with httpx.Client(timeout=self._timeout_s) as client:
                    return client.post(url, files=files, data=data)

        try:
            response = self._retry.execute(
                _do,
                operation_name=f"sv2 POST {self._path}",
            )
        except RuntimeError as exc:
            raise ReviewError(str(exc)) from exc

        try:
            payload = response.json()
        except Exception as exc:
            raise ReviewError(f"sv2 phase-2 devolvió payload no-JSON: {exc}") from exc

        # El envelope de fase 2 tiene la forma {meta, data, debug} igual
        # que el de fase 1. Aquí solo extraemos campos clave para
        # logging; el envelope completo se conserva tal cual.
        data_block = payload.get("data") or {}
        provider_used = (payload.get("meta") or {}).get("provider", "?")
        review_status = str(data_block.get("review_status", "?"))
        explicacion = data_block.get("explicacion_global")
        cambios = data_block.get("cambios") or []

        return ReviewResult(
            raw_envelope=payload,
            provider_used=str(provider_used),
            review_status=review_status,
            explicacion_global=(
                str(explicacion) if explicacion is not None else None
            ),
            changes_count=len(cambios),
        )
