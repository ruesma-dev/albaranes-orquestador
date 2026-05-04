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

# =============================================================== #
# Imports de los schemas REPLICADOS desde sv3.
# Necesarios para que Base.metadata.create_all() conozca todas las
# tablas merge y las cree si no existen. Ver
# external_schemas/__init__.py para la regla de mantenimiento.
# =============================================================== #
from infrastructure.database.external_schemas.orm_models import (
    Base as ExternalSchemasBase,
)
# Estos imports son requeridos para registrar las clases en
# ExternalSchemasBase.metadata aunque luego no se usen explícitamente.
import infrastructure.database.external_schemas.orm_models  # noqa: F401
import infrastructure.database.external_schemas.orm_contrato_models  # noqa: F401
import infrastructure.database.external_schemas.orm_contrato_cache_models  # noqa: F401

logger = logging.getLogger(__name__)


# =============================================================== #
# DDL replicado de sv6 (vía sv3). Crea las tablas de valoración
# que no son ORM en sv3 (albaran_valuations, albaran_line_valuations,
# contrato_lines_derived) y aplica los ALTER de sub-tandas 2C/2D.
#
# Copiado literalmente del bloque _VALUATION_DDL del sv3
# (sqlalchemy_albaran_repository.py). Mantener sincronizado.
# =============================================================== #
_VALUATION_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS albaran_valuations (
        id                        VARCHAR(36) PRIMARY KEY,
        document_id               VARCHAR(36) NOT NULL UNIQUE
            REFERENCES albaran_documents_merge(id) ON DELETE CASCADE,
        contrato_codigo           VARCHAR(64),
        status                    VARCHAR(32) NOT NULL,
        provider_ia               VARCHAR(32),
        model_name                VARCHAR(100),
        prompt_key                VARCHAR(100),
        total_valorado            DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        total_lines               INTEGER NOT NULL DEFAULT 0,
        lines_matched_exact       INTEGER NOT NULL DEFAULT 0,
        lines_matched_semantic    INTEGER NOT NULL DEFAULT 0,
        lines_matched_price_only  INTEGER NOT NULL DEFAULT 0,
        lines_unmatched           INTEGER NOT NULL DEFAULT 0,
        review_required           BOOLEAN NOT NULL DEFAULT FALSE,
        review_reasons_json       TEXT,
        raw_ia_envelope_json      TEXT,
        created_at_utc            VARCHAR(64) NOT NULL,
        updated_at_utc            VARCHAR(64)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_albaran_valuations_document_id "
    "ON albaran_valuations(document_id)",
    "CREATE INDEX IF NOT EXISTS ix_albaran_valuations_status "
    "ON albaran_valuations(status)",
    "CREATE INDEX IF NOT EXISTS ix_albaran_valuations_contrato_codigo "
    "ON albaran_valuations(contrato_codigo)",
    """
    CREATE TABLE IF NOT EXISTS contrato_lines_derived (
        id                        SERIAL PRIMARY KEY,
        created_by_valuation_id   VARCHAR(36) NOT NULL
            REFERENCES albaran_valuations(id) ON DELETE CASCADE,
        source_document_id        VARCHAR(36) NOT NULL,
        codigo_contrato           VARCHAR(64) NOT NULL,
        codigo_producto           VARCHAR(64),
        descripcion_linea         TEXT,
        unidad_medida             VARCHAR(32),
        precio_unitario           DOUBLE PRECISION,
        codigo_partida            VARCHAR(64),
        origen                    VARCHAR(32) NOT NULL,
        created_at_utc            VARCHAR(64) NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_contrato_lines_derived_created_by "
    "ON contrato_lines_derived(created_by_valuation_id)",
    "CREATE INDEX IF NOT EXISTS ix_contrato_lines_derived_source_doc "
    "ON contrato_lines_derived(source_document_id)",
    "CREATE INDEX IF NOT EXISTS ix_contrato_lines_derived_producto_partida "
    "ON contrato_lines_derived(codigo_contrato, codigo_producto, codigo_partida)",
    """
    CREATE TABLE IF NOT EXISTS albaran_line_valuations (
        id                             SERIAL PRIMARY KEY,
        valuation_id                   VARCHAR(36) NOT NULL
            REFERENCES albaran_valuations(id) ON DELETE CASCADE,
        merge_line_id                  INTEGER NOT NULL
            REFERENCES albaran_lines_merge(id) ON DELETE CASCADE,
        matched_contrato_line_id       INTEGER
            REFERENCES albaran_contrato_lines_merge(id) ON DELETE SET NULL,
        derived_contrato_line_id       INTEGER
            REFERENCES contrato_lines_derived(id) ON DELETE SET NULL,
        precio_unitario_contrato_db    DOUBLE PRECISION,
        precio_unitario_pdf_inferido   DOUBLE PRECISION,
        precio_unitario_final          DOUBLE PRECISION,
        precio_unitario_source         VARCHAR(32) NOT NULL,
        precio_unitario_agreement      VARCHAR(32) NOT NULL,
        unidad_albaran                 VARCHAR(32),
        unidad_contrato                VARCHAR(32),
        unidad_categoria               VARCHAR(32) NOT NULL,
        unidad_category_match          BOOLEAN NOT NULL,
        cantidad_albaran               DOUBLE PRECISION,
        cantidad_convertida            DOUBLE PRECISION,
        factor_conversion              DOUBLE PRECISION,
        importe_calculado              DOUBLE PRECISION,
        importe_albaran_declarado      DOUBLE PRECISION,
        importe_source                 VARCHAR(32) NOT NULL,
        codigo_partida_albaran         VARCHAR(64),
        codigo_partida_final           VARCHAR(64),
        partida_action                 VARCHAR(32) NOT NULL,
        match_confidence_pct           DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        match_method                   VARCHAR(32) NOT NULL,
        review_required                BOOLEAN NOT NULL DEFAULT FALSE,
        review_reasons_json            TEXT,
        ia_reasoning                   TEXT,
        created_at_utc                 VARCHAR(64) NOT NULL,
        CONSTRAINT uq_albaran_line_valuations_val_line
            UNIQUE (valuation_id, merge_line_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_albaran_line_valuations_valuation_id "
    "ON albaran_line_valuations(valuation_id)",
    "CREATE INDEX IF NOT EXISTS ix_albaran_line_valuations_merge_line_id "
    "ON albaran_line_valuations(merge_line_id)",
    "CREATE INDEX IF NOT EXISTS ix_albaran_line_valuations_matched_contrato "
    "ON albaran_line_valuations(matched_contrato_line_id)",
    "CREATE INDEX IF NOT EXISTS ix_albaran_line_valuations_derived_contrato "
    "ON albaran_line_valuations(derived_contrato_line_id)",
    "CREATE INDEX IF NOT EXISTS ix_albaran_line_valuations_match_method "
    "ON albaran_line_valuations(match_method)",
    # ALTER de sub-tanda 2C (idempotentes)
    "ALTER TABLE albaran_line_valuations "
    "ADD COLUMN IF NOT EXISTS rol_linea VARCHAR(32)",
    "ALTER TABLE albaran_line_valuations "
    "ADD COLUMN IF NOT EXISTS ref_linea_base_merge_id INTEGER",
    "ALTER TABLE albaran_line_valuations "
    "ADD COLUMN IF NOT EXISTS tarifa_pdf_encontrada BOOLEAN",
    "ALTER TABLE albaran_line_valuations "
    "ADD COLUMN IF NOT EXISTS modifiers_applied_json TEXT",
    # ALTER de sub-tanda 2D (idempotentes)
    "ALTER TABLE albaran_line_valuations "
    "ALTER COLUMN merge_line_id DROP NOT NULL",
    "ALTER TABLE albaran_line_valuations "
    "ADD COLUMN IF NOT EXISTS line_kind VARCHAR(32) "
    "NOT NULL DEFAULT 'from_albaran'",
    "ALTER TABLE albaran_line_valuations "
    "ADD COLUMN IF NOT EXISTS parent_merge_line_id INTEGER",
    "ALTER TABLE albaran_line_valuations "
    "ADD COLUMN IF NOT EXISTS modifier_source VARCHAR(32)",
    "ALTER TABLE albaran_line_valuations "
    "ADD COLUMN IF NOT EXISTS modifier_reason TEXT",
    "ALTER TABLE albaran_line_valuations "
    "ADD COLUMN IF NOT EXISTS descripcion_linea TEXT",
    "CREATE INDEX IF NOT EXISTS ix_albaran_line_valuations_parent "
    "ON albaran_line_valuations(parent_merge_line_id)",
    "CREATE INDEX IF NOT EXISTS ix_albaran_line_valuations_line_kind "
    "ON albaran_line_valuations(line_kind)",
)


# =============================================================== #
# DDL replicado de sv4. Añade las columnas de revisión humana
# (approved, selected_contrato_codigo, etc.) a las tablas merge.
# Copiado de review_repository.py de sv4. Mantener sincronizado.
# =============================================================== #
_REVIEW_DDL: tuple[str, ...] = (
    "ALTER TABLE albaran_documents_merge ADD COLUMN IF NOT EXISTS approved BOOLEAN",
    "UPDATE albaran_documents_merge SET approved = FALSE WHERE approved IS NULL",
    "ALTER TABLE albaran_documents_merge ALTER COLUMN approved SET DEFAULT FALSE",
    "ALTER TABLE albaran_documents_merge ALTER COLUMN approved SET NOT NULL",
    "ALTER TABLE albaran_documents_merge ADD COLUMN IF NOT EXISTS approved_at_utc VARCHAR(64)",
    "ALTER TABLE albaran_documents_merge ADD COLUMN IF NOT EXISTS approved_by VARCHAR(255)",
    "ALTER TABLE albaran_documents_merge ADD COLUMN IF NOT EXISTS reviewed_at_utc VARCHAR(64)",
    "ALTER TABLE albaran_documents_merge ADD COLUMN IF NOT EXISTS last_modified_at_utc VARCHAR(64)",
    "ALTER TABLE albaran_documents_merge ADD COLUMN IF NOT EXISTS review_notes TEXT",
    "ALTER TABLE albaran_documents_merge ADD COLUMN IF NOT EXISTS selected_contrato_codigo VARCHAR(64)",
    "ALTER TABLE albaran_contratos_merge ADD COLUMN IF NOT EXISTS gra_rep_ide INTEGER",
    "ALTER TABLE albaran_contratos_merge "
    "ADD COLUMN IF NOT EXISTS pdf_sharepoint_relative_path VARCHAR(1024)",
    "ALTER TABLE albaran_contratos_merge "
    "ADD COLUMN IF NOT EXISTS pdf_sharepoint_web_url VARCHAR(1024)",
    "ALTER TABLE albaran_contrato_lines_merge "
    "ADD COLUMN IF NOT EXISTS codigo_partida VARCHAR(64)",
    "ALTER TABLE albaran_contrato_lines_merge "
    "ADD COLUMN IF NOT EXISTS descripcion_partida TEXT",
    "CREATE INDEX IF NOT EXISTS ix_albaran_documents_merge_approved "
    "ON albaran_documents_merge(approved)",
    "CREATE INDEX IF NOT EXISTS ix_albaran_documents_merge_conf_calc "
    "ON albaran_documents_merge(confidence_pct_calc)",
)


# =============================================================== #
# DDL replicado de sv3 — bloque NUEVO de fase 2.
#
# Añade columnas que sv3 (Phase2PersistenceService) escribirá tras el
# persist principal, y que sv4 leerá para mostrar al revisor el
# estado de la revisión IA.
#
# Copiado literalmente del bloque _PHASE2_DDL del sv3
# (infrastructure/database/phase2_ddl.py). Mantener sincronizado:
# si en sv3 se añaden / cambian columnas, replicar aquí también.
# =============================================================== #
_PHASE2_DDL: tuple[str, ...] = (
    # albaran_documents_merge — metadatos a nivel documento.
    "ALTER TABLE albaran_documents_merge "
    "ADD COLUMN IF NOT EXISTS review_phase2_status VARCHAR(32)",
    "ALTER TABLE albaran_documents_merge "
    "ADD COLUMN IF NOT EXISTS review_phase2_summary TEXT",
    "ALTER TABLE albaran_documents_merge "
    "ADD COLUMN IF NOT EXISTS review_phase2_changes_count INTEGER",
    "ALTER TABLE albaran_documents_merge "
    "ADD COLUMN IF NOT EXISTS review_phase2_payload_json TEXT",
    "CREATE INDEX IF NOT EXISTS ix_albaran_documents_merge_review_phase2_status "
    "ON albaran_documents_merge(review_phase2_status)",
    # albaran_lines_merge — marca por línea de qué fase proviene.
    "ALTER TABLE albaran_lines_merge "
    "ADD COLUMN IF NOT EXISTS source_phase VARCHAR(16) "
    "NOT NULL DEFAULT 'phase_1'",
    "CREATE INDEX IF NOT EXISTS ix_albaran_lines_merge_source_phase "
    "ON albaran_lines_merge(source_phase)",
)


class SqlAlchemyWorkflowRepository(WorkflowRepository):
    """Implementación SQLAlchemy del WorkflowRepository.

    BOOTSTRAP COMPLETO al inicializar:
      1. Crea las TABLAS PROPIAS de sv7 (workflow_runs, workflow_step_history).
      2. Crea las TABLAS REPLICADAS DE OTROS SERVICIOS si no existen:
         - sv3: albaran_documents, albaran_lines, albaran_documents_merge,
                albaran_lines_merge, albaran_contratos_merge,
                albaran_contrato_lines_merge, contratos_cache,
                contrato_cache_lines.
         - sv6: albaran_valuations, albaran_line_valuations,
                contrato_lines_derived (con ALTER de sub-tandas 2C/2D).
         - sv4: ALTER de columnas de revisión humana sobre las tablas merge.
         - sv3 (NUEVO fase 2): ALTER de columnas review_phase2_* en
                albaran_documents_merge y source_phase en
                albaran_lines_merge.
      3. Todo es idempotente: si las tablas ya existen (porque sv3/sv4/sv6
         arrancaron antes), no las toca.

    Esto permite arrancar el sistema en cualquier orden — incluso sv7
    primero — sin que el primer evento de email falle por tablas
    o columnas inexistentes.

    REGLA DE MANTENIMIENTO:
      Cuando alguien cambie un schema en sv3/sv4/sv6, debe replicar el
      cambio aquí (en external_schemas/ o en _VALUATION_DDL /
      _REVIEW_DDL / _PHASE2_DDL según corresponda).
      Ver external_schemas/__init__.py.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._sf = session_factory
        self._initialized = False

    # ----------------------------------------------------------- #
    # Bootstrap de schema (idempotente).
    # ----------------------------------------------------------- #
    def initialize(self) -> None:
        if self._initialized:
            return

        # Paso 1: crear todas las tablas ORM (sv7 propias + sv3 réplicas).
        # Both sets of tables share the same SQLAlchemy MetaData via the
        # ``Base`` declarativos. Como el ``Base`` de workflow_models es
        # distinto del de external_schemas, los creamos por separado.
        self._create_workflow_tables()
        self._create_external_schemas_tables()

        # Paso 2: ejecutar el DDL crudo (valoración + revisión).
        self._execute_external_ddl()

        self._initialized = True
        logger.info(
            "Bootstrap completo: workflow_runs + workflow_step_history + "
            "tablas merge (sv3) + valoración (sv6) + columnas revisión "
            "humana (sv4) + columnas revisión IA fase 2 (sv3 nuevo)"
        )

    def _create_workflow_tables(self) -> None:
        """Crea las 2 tablas propias de sv7 con DDL crudo (idempotente)."""
        with self._sf.create_session() as session:
            for ddl in self._workflow_ddl_statements():
                session.execute(text(ddl))
            session.commit()
        logger.info("workflow_runs y workflow_step_history listas (DDL idempotente OK)")

    def _create_external_schemas_tables(self) -> None:
        """Crea las tablas merge replicadas (sv3) vía SQLAlchemy.

        ``Base.metadata.create_all()`` con ``checkfirst=True`` (default)
        es idempotente: solo crea las tablas que no existen ya.
        """
        try:
            ExternalSchemasBase.metadata.create_all(
                self._sf.engine,
                checkfirst=True,
            )
            logger.info(
                "Tablas merge (sv3 schema replicado) creadas o verificadas: %s",
                sorted(ExternalSchemasBase.metadata.tables.keys()),
            )
        except Exception:
            logger.exception(
                "Error creando tablas del schema replicado (sv3). "
                "Continuamos: si sv3 las crea después, no rompemos nada."
            )

    def _execute_external_ddl(self) -> None:
        """Ejecuta el DDL crudo de valoración (sv6), revisión humana (sv4)
        y revisión IA fase 2 (sv3 nuevo)."""
        with self._sf.create_session() as session:
            # Valoración (sv6).
            try:
                logger.info(
                    "Ejecutando %d sentencias DDL de valoración (sv6)…",
                    len(_VALUATION_DDL),
                )
                for stmt in _VALUATION_DDL:
                    session.execute(text(stmt))
                session.commit()
            except Exception:
                session.rollback()
                logger.exception(
                    "Fallo aplicando DDL de valoración. Continuamos."
                )

            # Revisión humana (sv4).
            try:
                logger.info(
                    "Ejecutando %d sentencias DDL de revisión (sv4)…",
                    len(_REVIEW_DDL),
                )
                for stmt in _REVIEW_DDL:
                    session.execute(text(stmt))
                session.commit()
            except Exception:
                session.rollback()
                logger.exception(
                    "Fallo aplicando DDL de revisión. Continuamos."
                )

            # Revisión IA — fase 2 (sv3 nuevo).
            try:
                logger.info(
                    "Ejecutando %d sentencias DDL de revisión IA fase 2 (sv3)…",
                    len(_PHASE2_DDL),
                )
                for stmt in _PHASE2_DDL:
                    session.execute(text(stmt))
                session.commit()
            except Exception:
                session.rollback()
                logger.exception(
                    "Fallo aplicando DDL de revisión IA fase 2. Continuamos."
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
