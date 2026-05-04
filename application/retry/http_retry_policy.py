# application/retry/http_retry_policy.py
"""Política de reintentos para llamadas HTTP a sv2/sv3/sv6.

Backoff exponencial truncado:
    delay(attempt) = min(base * 2^(attempt-1), cap)

Por defecto base=2s, cap=30s, max_attempts=3 → 0s, 2s, 4s.
Se reintenta sobre:
  - httpx.TimeoutException
  - httpx.NetworkError
  - 5xx (excepto 501)
  - 429
NO se reintenta sobre 4xx (es un error del cliente, no se va a arreglar reintentando).
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")


_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class HttpRetryPolicy:
    def __init__(
        self,
        *,
        max_attempts: int,
        backoff_base_s: float,
        backoff_cap_s: float,
    ) -> None:
        self._max_attempts = max(1, max_attempts)
        self._base = backoff_base_s
        self._cap = backoff_cap_s

    def execute(
        self,
        operation: Callable[[], httpx.Response],
        *,
        operation_name: str,
        workflow_id: str | None = None,
    ) -> httpx.Response:
        """Ejecuta ``operation`` reintentando hasta max_attempts.

        Devuelve la primera Response exitosa (status < 400) o lanza
        la última excepción / RuntimeError(status, body) si se
        agotaron los reintentos.
        """
        last_exc: Exception | None = None
        last_status: int | None = None
        last_body: str | None = None

        for attempt in range(1, self._max_attempts + 1):
            try:
                response = operation()
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                self._log_retry(
                    f"{operation_name} {type(exc).__name__}: {exc}",
                    attempt,
                    workflow_id,
                )
                if attempt < self._max_attempts:
                    time.sleep(self._delay_for(attempt))
                    continue
                raise RuntimeError(
                    f"{operation_name} falló tras {self._max_attempts} "
                    f"reintentos: {type(exc).__name__}: {exc}"
                ) from exc

            if response.status_code < 400:
                return response

            last_status = response.status_code
            last_body = response.text[:500]

            if response.status_code in _RETRYABLE_STATUSES and attempt < self._max_attempts:
                self._log_retry(
                    f"{operation_name} HTTP {response.status_code}: {last_body}",
                    attempt,
                    workflow_id,
                )
                time.sleep(self._delay_for(attempt))
                continue

            # 4xx no-retryable o último intento.
            break

        raise RuntimeError(
            f"{operation_name} falló: HTTP {last_status} body={last_body!r}"
        )

    def _delay_for(self, attempt: int) -> float:
        return min(self._base * (2 ** (attempt - 1)), self._cap)

    @staticmethod
    def _log_retry(msg: str, attempt: int, workflow_id: str | None) -> None:
        extra = {"workflow_id": workflow_id} if workflow_id else {}
        logger.warning("%s (attempt %d failed, will retry)", msg, attempt, extra=extra)
