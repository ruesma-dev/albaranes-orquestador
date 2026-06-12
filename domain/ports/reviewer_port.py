# domain/ports/reviewer_port.py
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from domain.models.step_results import ReviewResult


class ReviewerClient(ABC):
    """Puerto de salida hacia sv2 /v1/albaranes/extract/phase-2.

    sv2 expone dos endpoints separados (phase-1 y phase-2) y el
    orquestador trata cada fase con un puerto distinto. El motivo
    es que aunque ambos viven en sv2, semánticamente son dos pasos
    del workflow muy diferentes (extracción vs. revisión) y cada uno
    tiene su propio handler en el state machine.
    """

    @abstractmethod
    def review(
        self,
        *,
        file_path: Path,
        filename: str,
        content_type: str,
        phase_1_data: dict[str, Any],
        sigrid_context: dict[str, Any] | None = None,
    ) -> ReviewResult:
        """Llama a sv2 fase 2 enviando imagen + JSON de fase 1.

        ``phase_1_data`` es el campo ``data`` del envelope que devolvió
        la fase 1 (es decir, el ``DocumentoAlbaran`` ya parseado, NO
        el envelope completo con meta/debug).

        ``sigrid_context`` (jun 2026, opcional): resultado del grounding
        determinista de cabecera contra Sigrid (sv3). Si está presente,
        viaja a sv2 como Form field ``sigrid_context_json`` y se inyecta
        en el prompt de fase 2 para que la IA NO toque lo ya validado y
        use las listas de candidatos para casar lo no validado.

        Lanza ReviewError tras agotar reintentos HTTP.
        """
        raise NotImplementedError


class ReviewError(RuntimeError):
    """Error tras agotar reintentos contra sv2 fase 2."""
