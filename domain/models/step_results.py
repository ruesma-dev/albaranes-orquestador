# domain/models/step_results.py
"""DTOs que devuelven los clientes HTTP a los workers (sv2, sv3, sv6)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ExtractResult:
    """Lo que devuelve sv2 /v1/albaranes/extract/phase-1.

    El envelope tiene la forma:
        { "meta": {...}, "data": {...DocumentoAlbaran...}, "debug": {...} }
    Conservamos el envelope completo (raw_envelope) porque sv7 lo
    necesita íntegro para pasarlo a fase 2 y luego a sv3.
    """
    raw_envelope: dict[str, Any]
    provider_used: str
    confidence_pct: float | None


@dataclass(frozen=True)
class ReviewResult:
    """Lo que devuelve sv2 /v1/albaranes/extract/phase-2.

    El envelope tiene la forma:
        { "meta": {...}, "data": {...RevisionAlbaranFase2...}, "debug": {...} }

    El campo ``data`` contiene:
        - review_status: 'ok' | 'ok_with_changes' | 'inconsistent'
        - explicacion_global: str | None
        - cambios: list[CambioPropuesto]

    sv7 no aplica el patch aquí: lo aplica en una utilidad
    `apply_patch_to_envelope` cuando va a hacer el merge entre fase 1
    y fase 2 para mandárselo a sv3.
    """
    raw_envelope: dict[str, Any]
    provider_used: str
    review_status: str
    explicacion_global: str | None
    changes_count: int


@dataclass(frozen=True)
class PersistResult:
    """Lo que devuelve sv3 /v1/albaranes/persist."""

    document_id: str
    duplicate: bool
    contratos_count: int
    selected_contrato_codigo: str | None
    has_existing_valuation: bool = False
    existing_valuation_status: str | None = None  # 'ok' | 'no_contract' | None


@dataclass(frozen=True)
class ValuationResult:
    """Lo que devuelve sv6 /v1/valuation/run o /re-run."""

    valuation_id: str
    status: str  # 'ok' | 'no_contract' | otros
    total_valorado: float
    total_lines: int
    review_required: bool
    duplicate: bool = False
