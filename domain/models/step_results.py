# domain/models/step_results.py
"""DTOs que devuelven los clientes HTTP a los workers (sv2, sv3, sv6).

Aislan al motor de los detalles de cada API: el motor solo ve
ExtractResult / PersistResult / ValuationResult con campos planos.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ExtractResult:
    """Lo que devuelve sv2 /v1/albaranes/extract.

    Conservamos el envelope completo (raw_envelope) porque sv3 lo
    necesita íntegro al persistir; ``confidence_pct`` y ``providers_used``
    son convenientes para logging/auditoría.
    """

    raw_envelope: dict[str, Any]
    providers_used: list[str]
    confidence_pct: float | None


@dataclass(frozen=True)
class PersistResult:
    """Lo que devuelve sv3 /v1/albaranes/persist.

    - ``duplicate``: True si sv3 detectó un fichero ya persistido por
      sha256. En tal caso, ``document_id`` referencia al existente.
    - ``selected_contrato_codigo``: None si 0 ó >1 contratos sin elegir.
    - ``has_existing_valuation`` y ``existing_valuation_status``: solo
      tienen valor cuando es duplicado y permitimos al motor decidir
      a qué estado saltar (regla del cliente: si ya existe, no lo baja
      y pasa al siguiente paso).
    - ``existing_workflow_state_hint``: si el motor consulta sv7 para
      saber si hay workflow ya cerrado para ese document_id, lo trae
      aquí — pero esa consulta la hace el orquestador, no sv3.
    """

    document_id: str
    duplicate: bool
    contratos_count: int
    selected_contrato_codigo: str | None
    has_existing_valuation: bool = False
    existing_valuation_status: str | None = None  # 'ok' | 'no_contract' | None


@dataclass(frozen=True)
class ValuationResult:
    """Lo que devuelve sv6 /v1/valuation/run o /re-run.

    ``status`` puede ser 'ok' (valoración generada) o 'no_contract'
    (sv6 marca cabecera vacía y no llama a IA). En caso 'no_contract'
    el motor lo trata como valuation_failed para que el revisor
    intervenga.
    """

    valuation_id: str
    status: str  # 'ok' | 'no_contract' | otros
    total_valorado: float
    total_lines: int
    review_required: bool
    duplicate: bool = False
