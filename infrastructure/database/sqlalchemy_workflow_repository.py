# infrastructure/database/sqlalchemy_workflow_repository.py
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, select, text
from sqlalchemy.orm import Session

from domain.models.workflow import (
    StepHistoryEntry,
    WorkflowRun,
    WorkflowState,
)
from domain.ports.workflow_repository import WorkflowRepository
from infrastructure.database.orm_workflow_models import (
    StepHistoryOrm,
    WorkflowRunOrm,
)
from infrastructure.database.session_factory import SessionFactory

logger = logging.getLogger(__name__)


# =============================================================== #
# REFACTOR SCHEMA CONTRIBUTORS (mayo 2026):
#
# Eliminados de este archivo:
#   - _VALUATION_DDL      → ahora vive en sv6 (schema_contribution.py)
#   - _REVIEW_DDL         → ahora vive en sv3 (schema_contribution.py)
#   - _PHASE2_DDL         → ahora vive en sv3 (schema_contribution.py)
#   - imports de external_schemas/* → ya no se usa (deprecado)
#
# El sv7 ya NO replica el schema de otros servicios. En su lugar, el
# pipeline ``BootstrapSchemaPipeline`` descubre el DDL de cada
# contributor vía GET /schema/ddl al arrancar y lo aplica antes de
# que este repositorio inicialice las tablas propias del sv7.
#
# Este repositorio se queda SOLO con sus 2 tablas propias:
#   - workflow_runs
#   - workflow_step_history
# =============================================================== #


class SqlAlchemyWorkflowRepository(WorkflowRepository):
    """Implementación SQLAlchemy del WorkflowRepository del sv7.

    Bootstrap (idempotente): crea ``workflow_runs`` y
    ``workflow_step_history``. Las tablas de otros servicios (sv3,
    sv6) las habrá creado el ``BootstrapSchemaPipeline`` antes de
    construir este repositorio.

    Si por alguna razón el bootstrap-pipeline no se ejecutó (modo
    test, fallback manual, etc.), las consultas que hagan FK a esas
    tablas fallarán claramente — pero las tablas propias del sv7
    seguirán funcionando.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._sf = session_factory
        self._initialized = False

    # ----------------------------------------------------------- #
    # Bootstrap de schema PROPIO (idempotente).
    # ----------------------------------------------------------- #
    def initialize(self) -> None:
        if self._initialized:
            return

        with self._sf.create_session() as session:
            for ddl in self._workflow_ddl_statements():
                session.execute(text(ddl))
            session.commit()

        self._initialized = True
        logger.info(
            "Bootstrap del sv7 completo: workflow_runs + workflow_step_history"
        )

    @staticmethod
    def _workflow_ddl_statements() -> list[str]:
        return [
            """
            CREATE TABLE IF NOT EXISTS workflow_runs (
                id                   VARCHAR(36) PRIMARY KEY,
                kind                 VARCHAR(64) NOT NULL,
                parent_workflow_id   VARCHAR(36),
                document_id          VARCHAR(36),
                current_state        VARCHAR(64) NOT NULL,
                correlation_key      VARCHAR(255) NOT NULL UNIQUE,
                payload_json         TEXT NOT NULL,
                started_at_utc       VARCHAR(64) NOT NULL,
                updated_at_utc       VARCHAR(64) NOT NULL,
                completed_at_utc     VARCHAR(64),
                last_error           TEXT,
                retry_count          INTEGER NOT NULL DEFAULT 0,
                pending_event_json   TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS ix_workflow_runs_state    ON workflow_runs (current_state)",
            "CREATE INDEX IF NOT EXISTS ix_workflow_runs_doc      ON workflow_runs (document_id)",
            "CREATE INDEX IF NOT EXISTS ix_workflow_runs_kind     ON workflow_runs (kind)",
            "CREATE INDEX IF NOT EXISTS ix_workflow_runs_started  ON workflow_runs (started_at_utc DESC)",
            """
            CREATE TABLE IF NOT EXISTS workflow_step_history (
                id                   SERIAL PRIMARY KEY,
                workflow_run_id      VARCHAR(36) NOT NULL
                    REFERENCES workflow_runs(id) ON DELETE CASCADE,
                step_name            VARCHAR(64) NOT NULL,
                status               VARCHAR(32) NOT NULL,
                attempt              INTEGER NOT NULL DEFAULT 1,
                input_json           TEXT,
                output_json          TEXT,
                error                TEXT,
                started_at_utc       VARCHAR(64) NOT NULL,
                completed_at_utc     VARCHAR(64),
                duration_ms          INTEGER
            )
            """,
            "CREATE INDEX IF NOT EXISTS ix_step_history_wf  ON workflow_step_history (workflow_run_id)",
            "CREATE INDEX IF NOT EXISTS ix_step_history_st  ON workflow_step_history (step_name, status)",
        ]

    # ----------------------------------------------------------- #
    # workflow_runs
    # ----------------------------------------------------------- #
    def insert(self, run: WorkflowRun) -> None:
        with self._sf.create_session() as session:
            session.add(self._to_orm(run))
            session.commit()

    def update(self, run: WorkflowRun) -> None:
        with self._sf.create_session() as session:
            orm = session.get(WorkflowRunOrm, run.id)
            if orm is None:
                raise KeyError(f"workflow {run.id} no existe")
            self._copy_to_orm(run, orm)
            session.commit()

    def find_by_id(self, workflow_id: str) -> WorkflowRun | None:
        with self._sf.create_session() as session:
            orm = session.get(WorkflowRunOrm, workflow_id)
            return self._from_orm(orm) if orm is not None else None

    def find_by_correlation_key(self, correlation_key: str) -> WorkflowRun | None:
        with self._sf.create_session() as session:
            orm = session.scalars(
                select(WorkflowRunOrm).where(
                    WorkflowRunOrm.correlation_key == correlation_key
                )
            ).first()
            return self._from_orm(orm) if orm is not None else None

    def find_active_by_document_id(self, document_id: str) -> WorkflowRun | None:
        excluded = {
            WorkflowState.APPROVED.value,
            WorkflowState.COMPLETED_DUPLICATE.value,
        }
        with self._sf.create_session() as session:
            orm = session.scalars(
                select(WorkflowRunOrm)
                .where(
                    and_(
                        WorkflowRunOrm.document_id == document_id,
                        WorkflowRunOrm.current_state.notin_(excluded),
                    )
                )
                .order_by(WorkflowRunOrm.started_at_utc.desc())
            ).first()
            return self._from_orm(orm) if orm is not None else None

    def find_latest_by_document_id(self, document_id: str) -> WorkflowRun | None:
        with self._sf.create_session() as session:
            orm = session.scalars(
                select(WorkflowRunOrm)
                .where(WorkflowRunOrm.document_id == document_id)
                .order_by(WorkflowRunOrm.started_at_utc.desc())
            ).first()
            return self._from_orm(orm) if orm is not None else None

    def list_in_states(
        self,
        states: list[WorkflowState],
        *,
        limit: int = 100,
    ) -> list[WorkflowRun]:
        state_values = [s.value for s in states]
        with self._sf.create_session() as session:
            rows = session.scalars(
                select(WorkflowRunOrm)
                .where(WorkflowRunOrm.current_state.in_(state_values))
                .order_by(WorkflowRunOrm.started_at_utc.asc())
                .limit(limit)
            ).all()
        return [self._from_orm(r) for r in rows if r is not None]

    def list_failed_for_auto_retry(
        self,
        *,
        max_retry_count: int,
        min_age_seconds: int,
        limit: int = 50,
    ) -> list[WorkflowRun]:
        threshold = (datetime.now(timezone.utc) - timedelta(seconds=min_age_seconds))
        threshold_iso = threshold.isoformat().replace("+00:00", "Z")
        failed_states = [
            WorkflowState.EXTRACTION_FAILED.value,
            WorkflowState.PERSISTENCE_FAILED.value,
            WorkflowState.VALUATION_FAILED.value,
        ]
        with self._sf.create_session() as session:
            rows = session.scalars(
                select(WorkflowRunOrm)
                .where(
                    and_(
                        WorkflowRunOrm.current_state.in_(failed_states),
                        WorkflowRunOrm.retry_count < max_retry_count,
                        WorkflowRunOrm.updated_at_utc < threshold_iso,
                    )
                )
                .order_by(WorkflowRunOrm.updated_at_utc.asc())
                .limit(limit)
            ).all()
        return [self._from_orm(r) for r in rows if r is not None]

    # ----------------------------------------------------------- #
    # workflow_step_history
    # ----------------------------------------------------------- #
    def append_step(self, entry: StepHistoryEntry) -> int:
        with self._sf.create_session() as session:
            orm = StepHistoryOrm(
                workflow_run_id=entry.workflow_run_id,
                step_name=entry.step_name,
                status=entry.status,
                attempt=entry.attempt,
                input_json=json.dumps(entry.input_payload, ensure_ascii=False) if entry.input_payload else None,
                output_json=json.dumps(entry.output_payload, ensure_ascii=False) if entry.output_payload else None,
                error=entry.error,
                started_at_utc=entry.started_at_utc,
                completed_at_utc=entry.completed_at_utc,
                duration_ms=entry.duration_ms,
            )
            session.add(orm)
            session.commit()
            return int(orm.id)

    def update_step(self, entry: StepHistoryEntry) -> None:
        if entry.id is None:
            raise ValueError("StepHistoryEntry.id es requerido para update_step()")
        with self._sf.create_session() as session:
            orm = session.get(StepHistoryOrm, entry.id)
            if orm is None:
                raise KeyError(f"step_history #{entry.id} no existe")
            orm.status = entry.status
            orm.completed_at_utc = entry.completed_at_utc
            orm.duration_ms = entry.duration_ms
            if entry.output_payload is not None:
                orm.output_json = json.dumps(entry.output_payload, ensure_ascii=False)
            if entry.error is not None:
                orm.error = entry.error
            session.commit()

    # ----------------------------------------------------------- #
    # Mappers
    # ----------------------------------------------------------- #
    @staticmethod
    def _to_orm(run: WorkflowRun) -> WorkflowRunOrm:
        orm = WorkflowRunOrm(
            id=run.id,
            kind=run.kind,
            parent_workflow_id=run.parent_workflow_id,
            document_id=run.document_id,
            current_state=run.current_state.value,
            correlation_key=run.correlation_key,
            payload_json=json.dumps(run.payload, ensure_ascii=False),
            started_at_utc=run.started_at_utc,
            updated_at_utc=run.updated_at_utc,
            completed_at_utc=run.completed_at_utc,
            last_error=run.last_error,
            retry_count=run.retry_count,
            pending_event_json=(
                json.dumps(run.pending_event, ensure_ascii=False)
                if run.pending_event is not None else None
            ),
        )
        return orm

    @staticmethod
    def _copy_to_orm(run: WorkflowRun, orm: WorkflowRunOrm) -> None:
        orm.kind = run.kind
        orm.parent_workflow_id = run.parent_workflow_id
        orm.document_id = run.document_id
        orm.current_state = run.current_state.value
        orm.correlation_key = run.correlation_key
        orm.payload_json = json.dumps(run.payload, ensure_ascii=False)
        orm.updated_at_utc = run.updated_at_utc
        orm.completed_at_utc = run.completed_at_utc
        orm.last_error = run.last_error
        orm.retry_count = run.retry_count
        orm.pending_event_json = (
            json.dumps(run.pending_event, ensure_ascii=False)
            if run.pending_event is not None else None
        )

    @staticmethod
    def _from_orm(orm: WorkflowRunOrm) -> WorkflowRun:
        return WorkflowRun(
            id=orm.id,
            kind=orm.kind,  # type: ignore[arg-type]
            parent_workflow_id=orm.parent_workflow_id,
            document_id=orm.document_id,
            current_state=WorkflowState(orm.current_state),
            correlation_key=orm.correlation_key,
            payload=json.loads(orm.payload_json) if orm.payload_json else {},
            started_at_utc=orm.started_at_utc,
            updated_at_utc=orm.updated_at_utc,
            completed_at_utc=orm.completed_at_utc,
            last_error=orm.last_error,
            retry_count=orm.retry_count or 0,
            pending_event=(
                json.loads(orm.pending_event_json) if orm.pending_event_json else None
            ),
        )
