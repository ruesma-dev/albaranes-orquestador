# domain/ports/valuator_port.py
from __future__ import annotations

from abc import ABC, abstractmethod

from domain.models.step_results import ValuationResult


class ValuatorClient(ABC):
    """Puerto de salida hacia sv6 (albaranes-valuation-persistence-api).

    Nota: el orquestador llama a sv6 en modo SÍNCRONO (/v1/valuation/run),
    no a /run-async. La razón: sv7 ya gestiona asincronía propia con
    BackgroundTasks; encadenar dos capas asíncronas complica trazabilidad.
    """

    @abstractmethod
    def run(
        self,
        *,
        document_id: str,
        codigo_contrato: str | None = None,
        force: bool = False,
        line_already_valued: bool = False,
    ) -> ValuationResult:
        """Dispara la valoración (síncrona) y devuelve el resultado.

        Si ``force=True``, sv6 ignora valoración previa y re-ejecuta.

        Lanza ValuationError tras agotar reintentos HTTP.
        """
        raise NotImplementedError

    @abstractmethod
    def rerun(
        self,
        *,
        document_id: str,
        codigo_contrato: str | None = None,
    ) -> ValuationResult:
        """Atajo /re-run que sv6 expone (force=True implícito).

        Usado para el workflow ``albaran_revaluation`` cuando el revisor
        cambia el contrato tras aprobar.
        """
        raise NotImplementedError


class ValuationError(RuntimeError):
    """Error tras agotar reintentos contra sv6."""
