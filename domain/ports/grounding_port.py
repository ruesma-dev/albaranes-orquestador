# domain/ports/grounding_port.py
"""Puerto de salida hacia sv3 POST /v1/sigrid/header-grounding.

Antes de lanzar la fase 2 (IA de revisión), el orquestador pide a sv3
una validación DETERMINISTA de la cabecera extraída por la fase 1
contra el ERP Sigrid:

  - Proveedor: lookup exacto por CIF. Si el CIF existe en Sigrid, el
    proveedor queda VALIDADO y se devuelve la razón social canónica
    (``prv.raz``). La IA de fase 2 NO debe revisar ese bloque.
  - Obra: lookup exacto por código normalizado. Si existe, queda
    VALIDADA con nombre + dirección canónicos.
  - Cuando algo NO valida, sv3 adjunta listas de candidatos (obras
    activas; proveedores con contrato en la obra validada) para que
    la IA de fase 2 intente casar el texto leído con el ERP.

El resultado viaja a sv2 como contexto del prompt de fase 2 y,
ADEMÁS, sv7 aplica las partes validadas de forma determinista sobre
el documento revisado (cinturón y tirantes: aunque la IA ignore la
instrucción, la cabecera final lleva los datos canónicos del ERP).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class HeaderGroundingResult:
    """Resultado del grounding de cabecera contra Sigrid.

    ``raw`` conserva el JSON íntegro devuelto por sv3 — es lo que se
    inyecta en el prompt de fase 2 (sv2) sin re-modelar, para que el
    contrato entre sv3 y el prompt evolucione sin tocar sv7.
    """

    proveedor_status: str          # 'validated' | 'not_found' | 'skipped'
    proveedor_cif: str | None
    proveedor_nombre_canonico: str | None
    obra_status: str               # 'validated' | 'not_found' | 'skipped'
    obra_codigo: str | None
    obra_nombre: str | None
    obra_direccion: str | None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def proveedor_validado(self) -> bool:
        return self.proveedor_status == "validated"

    @property
    def obra_validada(self) -> bool:
        return self.obra_status == "validated"


class GroundingClient(ABC):
    """Cliente del endpoint de grounding de sv3."""

    @abstractmethod
    def ground_header(
        self,
        *,
        proveedor_cif: str | None,
        proveedor_nombre: str | None,
        obra_codigo: str | None,
        obra_nombre: str | None,
        obra_direccion: str | None,
    ) -> HeaderGroundingResult | None:
        """Devuelve el grounding o ``None`` si sv3 no está disponible.

        NUNCA lanza: el grounding es best-effort. Sin él, la fase 2
        funciona como hasta ahora (solo prompt + checklist).
        """
        raise NotImplementedError
