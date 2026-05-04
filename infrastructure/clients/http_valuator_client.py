# infrastructure/clients/http_valuator_client.py
from __future__ import annotations

import logging

import httpx

from application.retry.http_retry_policy import HttpRetryPolicy
from domain.models.step_results import ValuationResult
from domain.ports.valuator_port import ValuationError, ValuatorClient

logger = logging.getLogger(__name__)


class HttpValuatorClient(ValuatorClient):
    """Adapter HTTP del puerto ValuatorClient → sv6 (síncrono)."""

    def __init__(
        self,
        *,
        base_url: str,
        path_run: str,
        path_rerun: str,
        timeout_s: float,
        retry_policy: HttpRetryPolicy,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._path_run = path_run
        self._path_rerun = path_rerun
        self._timeout_s = timeout_s
        self._retry = retry_policy

    def run(
        self,
        *,
        document_id: str,
        codigo_contrato: str | None = None,
        force: bool = False,
        line_already_valued: bool = False,
    ) -> ValuationResult:
        url = f"{self._base_url}{self._path_run}"
        body = {
            "document_id": document_id,
            "force": force,
        }
        if codigo_contrato:
            body["codigo_contrato"] = codigo_contrato

        def _do() -> httpx.Response:
            with httpx.Client(timeout=self._timeout_s) as client:
                return client.post(url, json=body)

        try:
            response = self._retry.execute(_do, operation_name=f"sv6 POST {self._path_run}")
        except RuntimeError as exc:
            raise ValuationError(str(exc)) from exc

        return self._parse(response)

    def rerun(
        self,
        *,
        document_id: str,
        codigo_contrato: str | None = None,
    ) -> ValuationResult:
        path = self._path_rerun.format(document_id=document_id)
        url = f"{self._base_url}{path}"
        body = {}
        if codigo_contrato:
            body["codigo_contrato"] = codigo_contrato

        def _do() -> httpx.Response:
            with httpx.Client(timeout=self._timeout_s) as client:
                return client.post(url, json=body)

        try:
            response = self._retry.execute(_do, operation_name=f"sv6 POST {path}")
        except RuntimeError as exc:
            raise ValuationError(str(exc)) from exc

        return self._parse(response)

    @staticmethod
    def _parse(response: httpx.Response) -> ValuationResult:
        try:
            payload = response.json()
        except Exception as exc:
            raise ValuationError(f"sv6 devolvió payload no-JSON: {exc}") from exc

        return ValuationResult(
            valuation_id=str(payload.get("valuation_id", "")),
            status=str(payload.get("status", "unknown")),
            total_valorado=float(payload.get("total_valorado") or 0.0),
            total_lines=int(payload.get("total_lines") or 0),
            review_required=bool(payload.get("review_required", False)),
            duplicate=bool(payload.get("duplicate", False)),
        )
