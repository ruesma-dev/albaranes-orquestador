# infrastructure/clients/http_grounding_client.py
"""Adapter HTTP del puerto GroundingClient → sv3 /v1/sigrid/header-grounding.

Best-effort por diseño: cualquier fallo (red, 4xx/5xx, JSON inválido,
sv3 sin Sigrid cableado) devuelve ``None`` y se loguea como warning.
La fase 2 sigue funcionando sin grounding — exactamente igual que
antes de esta mejora.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from domain.ports.grounding_port import GroundingClient, HeaderGroundingResult

logger = logging.getLogger(__name__)


class HttpGroundingClient(GroundingClient):
    def __init__(
        self,
        *,
        base_url: str,
        path_grounding: str,
        timeout_s: float,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._path = path_grounding
        self._timeout_s = float(timeout_s)

    def ground_header(
        self,
        *,
        proveedor_cif: str | None,
        proveedor_nombre: str | None,
        obra_codigo: str | None,
        obra_nombre: str | None,
        obra_direccion: str | None,
    ) -> HeaderGroundingResult | None:
        url = f"{self._base_url}{self._path}"
        body = {
            "proveedor_cif": proveedor_cif,
            "proveedor_nombre": proveedor_nombre,
            "obra_codigo": obra_codigo,
            "obra_nombre": obra_nombre,
            "obra_direccion": obra_direccion,
        }
        try:
            with httpx.Client(timeout=self._timeout_s) as client:
                response = client.post(url, json=body)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning(
                "[grounding] sv3 inaccesible (%s: %s); fase 2 sin grounding.",
                type(exc).__name__,
                exc,
            )
            return None

        if response.status_code >= 400:
            logger.warning(
                "[grounding] sv3 devolvió %s body=%s; fase 2 sin grounding.",
                response.status_code,
                (response.text or "")[:300],
            )
            return None

        try:
            payload: dict[str, Any] = response.json()
        except Exception:
            logger.warning(
                "[grounding] respuesta no-JSON de sv3; fase 2 sin grounding."
            )
            return None

        proveedor = payload.get("proveedor") or {}
        obra = payload.get("obra") or {}
        result = HeaderGroundingResult(
            proveedor_status=str(proveedor.get("status") or "skipped"),
            proveedor_cif=proveedor.get("cif"),
            proveedor_nombre_canonico=proveedor.get("nombre_canonico"),
            obra_status=str(obra.get("status") or "skipped"),
            obra_codigo=obra.get("codigo"),
            obra_nombre=obra.get("nombre"),
            obra_direccion=obra.get("direccion"),
            raw=payload,
        )
        logger.info(
            "[grounding] proveedor=%s (cif=%s) obra=%s (codigo=%s) "
            "candidatos_obras=%d candidatos_proveedores=%d",
            result.proveedor_status,
            result.proveedor_cif,
            result.obra_status,
            result.obra_codigo,
            len(payload.get("obras_candidatas") or []),
            len(payload.get("proveedores_candidatos") or []),
        )
        return result
