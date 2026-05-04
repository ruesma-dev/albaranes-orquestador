# infrastructure/clients/http_persister_client.py
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from application.retry.http_retry_policy import HttpRetryPolicy
from domain.models.step_results import PersistResult
from domain.ports.persister_port import PersistenceError, PersisterClient

logger = logging.getLogger(__name__)


class HttpPersisterClient(PersisterClient):
    """Adapter HTTP del puerto PersisterClient → sv3.

    IMPORTANTE — nombres de campos del Form:
      sv3 espera ``extraction_json`` y ``context_json`` (no
      ``extraction_envelope`` ni ``context``). Si renombras esos
      Form fields en sv3, ajusta también aquí.
    """

    def __init__(
        self,
        *,
        base_url: str,
        path_persist: str,
        timeout_s: float,
        retry_policy: HttpRetryPolicy,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._path = path_persist
        self._timeout_s = timeout_s
        self._retry = retry_policy

    def persist(
        self,
        *,
        file_path: Path,
        filename: str,
        content_type: str,
        extraction_envelope: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> PersistResult:
        url = f"{self._base_url}{self._path}"

        def _do() -> httpx.Response:
            with file_path.open("rb") as fp:
                files = {"file": (filename, fp, content_type)}
                # ATENCIÓN: nombres EXACTOS que espera sv3 en su
                # endpoint /v1/albaranes/persist:
                #   - extraction_json (Form, requerido)
                #   - context_json    (Form, opcional, default "{}")
                data: dict[str, str] = {
                    "extraction_json": json.dumps(
                        extraction_envelope, ensure_ascii=False
                    ),
                    "context_json": json.dumps(
                        context or {}, ensure_ascii=False
                    ),
                }
                with httpx.Client(timeout=self._timeout_s) as client:
                    return client.post(url, files=files, data=data)

        try:
            response = self._retry.execute(
                _do,
                operation_name=f"sv3 POST {self._path}",
            )
        except RuntimeError as exc:
            raise PersistenceError(str(exc)) from exc

        try:
            payload = response.json()
        except Exception as exc:
            raise PersistenceError(f"sv3 devolvió payload no-JSON: {exc}") from exc

        # ----------------------------------------------------------- #
        # Mapeo defensivo: tolera distintos nombres de campos en la
        # respuesta de sv3 (puede variar según versión del pipeline).
        # Si tu sv3 devuelve nombres distintos, ajusta los .get() aquí.
        # ----------------------------------------------------------- #
        document_id = (
            payload.get("document_id")
            or payload.get("id")
            or payload.get("merge_document_id")
        )
        if not document_id:
            raise PersistenceError(
                f"sv3 no devolvió document_id en la respuesta: {payload!r}"
            )

        contratos_count_value = (
            payload.get("contratos_count")
            or payload.get("num_contratos")
            or len(payload.get("contratos") or [])
        )

        return PersistResult(
            document_id=str(document_id),
            duplicate=bool(payload.get("duplicate", False)),
            contratos_count=int(contratos_count_value or 0),
            selected_contrato_codigo=payload.get("selected_contrato_codigo"),
            has_existing_valuation=bool(
                payload.get("has_existing_valuation", False)
            ),
            existing_valuation_status=payload.get("existing_valuation_status"),
        )
