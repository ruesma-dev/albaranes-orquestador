# domain/ports/workflow_repository.py
from __future__ import annotations

from abc import ABC, abstractmethod

from domain.models.workflow import StepHistoryEntry, WorkflowRun, WorkflowState


class WorkflowRepository(ABC):
    """Puerto de persistencia de workflow_runs y workflow_step_history.

    Idempotencia clave: ``find_by_correlation_key`` permite a sv7
    detectar reentradas del mismo email/página antes de crear un
    workflow nuevo (UNIQUE en correlation_key).
    """

    @abstractmethod
    def initialize(self) -> None:
        """Aplica DDL idempotente (CREATE TABLE IF NOT EXISTS + índices)."""
        raise NotImplementedError

    @abstractmethod
    def insert(self, run: WorkflowRun) -> None:
        raise NotImplementedError

    @abstractmethod
    def update(self, run: WorkflowRun) -> None:
        raise NotImplementedError

    @abstractmethod
    def find_by_id(self, workflow_id: str) -> WorkflowRun | None:
        raise NotImplementedError

    @abstractmethod
    def find_by_correlation_key(self, correlation_key: str) -> WorkflowRun | None:
        raise NotImplementedError

    @abstractmethod
    def find_latest_by_attachment_sha256(
        self, attachment_sha256: str
    ) -> WorkflowRun | None:
        """Ultimo run NO fallido con esa huella de PDF (o None)."""
        ...

    @abstractmethod
    def find_active_by_document_id(self, document_id: str) -> WorkflowRun | None:
        """Busca el último workflow asociado al document_id que esté activo,
        en estado pasivo o terminal-failed (NO devuelve approved/duplicate)."""
        raise NotImplementedError

    @abstractmethod
    def find_latest_by_document_id(self, document_id: str) -> WorkflowRun | None:
        """Busca el último workflow asociado al document_id sin filtros."""
        raise NotImplementedError

    @abstractmethod
    def find_all_by_document_id(self, document_id: str) -> list[WorkflowRun]:
        """TODOS los workflows ligados a un documento (cualquier estado).
        Usado por el evento document-purged."""
        raise NotImplementedError

    @abstractmethod
    def find_all_by_attachment_sha256(
        self, attachment_sha256: str
    ) -> list[WorkflowRun]:
        """TODOS los workflows con esa huella de PDF (cualquier estado).
        Red de seguridad del purge para runs sin document_id."""
        raise NotImplementedError

    @abstractmethod
    def list_in_states(
        self,
        states: list[WorkflowState],
        *,
        limit: int = 100,
    ) -> list[WorkflowRun]:
        raise NotImplementedError

    @abstractmethod
    def list_failed_for_auto_retry(
        self,
        *,
        max_retry_count: int,
        min_age_seconds: int,
        limit: int = 50,
    ) -> list[WorkflowRun]:
        """Devuelve *_failed con retry_count < max y updated_at_utc <
        now - min_age_seconds (para evitar reintentos en bucle inmediato)."""
        raise NotImplementedError

    @abstractmethod
    def append_step(self, entry: StepHistoryEntry) -> int:
        """Inserta una entrada y devuelve el id autogenerado."""
        raise NotImplementedError

    @abstractmethod
    def update_step(self, entry: StepHistoryEntry) -> None:
        """Cierra una entrada (status, completed_at_utc, output_payload, error)."""
        raise NotImplementedError
