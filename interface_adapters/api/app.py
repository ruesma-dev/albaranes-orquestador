# interface_adapters/api/app.py
from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
)

from application.pipelines.bootstrap_schema_pipeline import (
    BootstrapSchemaError,
    BootstrapSchemaRequest,
    build_bootstrap_pipeline,
)
from application.retry.http_retry_policy import HttpRetryPolicy
from application.services.event_dispatcher import EventDispatcher
from application.services.existing_doc_resolver import ExistingDocResolver
from application.services.failed_workflow_retrier import FailedWorkflowRetrier
from application.services.idempotency_guard import IdempotencyGuard
from application.services.workflow_engine import WorkflowEngine
from application.workflows.albaran_e2e_workflow import AlbaranE2EWorkflow
from config.settings import Settings
from domain.models.events import (
    ContractSelectedEvent,
    DocumentApprovedEvent,
    EmailReceivedEvent,
)
from domain.models.workflow import (
    ACTIVE_STATES,
    WorkflowState,
)
from infrastructure.clients.http_extractor_client import HttpExtractorClient
from infrastructure.clients.http_persister_client import HttpPersisterClient
from infrastructure.clients.http_reviewer_client import HttpReviewerClient
from infrastructure.clients.http_schema_ddl_client import HttpSchemaDdlClient
from infrastructure.clients.http_valuator_client import HttpValuatorClient
from infrastructure.database.session_factory import SessionFactory
from infrastructure.database.sqlalchemy_workflow_repository import (
    SqlAlchemyWorkflowRepository,
)

logger = logging.getLogger(__name__)


def _save_upload_to_tmp(*, upload: UploadFile, tmpdir: Path) -> Path:
    tmpdir.mkdir(parents=True, exist_ok=True)
    suffix = Path(upload.filename or "attachment").suffix or ".bin"
    target = tmpdir / f"{uuid.uuid4()}{suffix}"
    with target.open("wb") as fp:
        while True:
            chunk = upload.file.read(64 * 1024)
            if not chunk:
                break
            fp.write(chunk)
    return target


def _cleanup_tmpfile(file_path: str) -> None:
    try:
        os.unlink(file_path)
    except FileNotFoundError:
        pass


def build_app(settings: Settings) -> FastAPI:
    # ----------------------------------------------------------- #
    # Composition root.
    #
    # ORDEN DE BOOTSTRAP (importante):
    #   1. SessionFactory (crea la BBDD si no existe — auto_create_database).
    #   2. BootstrapSchemaPipeline → descubre y aplica el DDL contribuido
    #      por sv3 y sv6 vía GET /schema/ddl. Esto crea las tablas que
    #      esos servicios "poseen" en la BBDD compartida.
    #   3. SqlAlchemyWorkflowRepository.initialize() → crea las tablas
    #      propias del sv7 (workflow_runs, workflow_step_history).
    #   4. Resto del wiring (clientes HTTP, workflow engine, etc.).
    #
    # IMPORTANTE: el sv3 y sv6 deben estar ARRIBA antes de arrancar
    # el sv7. El bootstrap-pipeline llama a sus endpoints
    # /schema/ddl. Si no responden, el sv7 falla en arranque
    # explícitamente (mejor reventar pronto que aplicar schema parcial).
    # ----------------------------------------------------------- #
    sf = SessionFactory(
        database_url=settings.database_url,
        admin_database_url=settings.admin_database_url,
        target_database_name=settings.pg_db,
        auto_create_database=settings.auto_create_database,
    )

    # Política de reintentos compartida (también la usan los demás clientes).
    retry_policy = HttpRetryPolicy(
        max_attempts=settings.http_max_retries,
        backoff_base_s=settings.http_backoff_base_s,
        backoff_cap_s=settings.http_backoff_cap_s,
    )

    # ----- 2. Schema bootstrap (DDL contribuido por sv3 y sv6) ----- #
    schema_ddl_client = HttpSchemaDdlClient(
        timeout_s=settings.schema_ddl_timeout_s,
        retry_policy=retry_policy,
    )
    bootstrap_pipeline = build_bootstrap_pipeline(
        engine=sf.engine,
        ddl_client=schema_ddl_client,
    )
    try:
        bootstrap_report = bootstrap_pipeline.run(
            BootstrapSchemaRequest(
                contributor_urls=settings.schema_contributor_urls,
            )
        )
        logger.info(
            "[sv7][wiring] bootstrap-schema OK: %d contributors, "
            "%d sentencias DDL aplicadas.",
            len(bootstrap_report.applied),
            bootstrap_report.total_statements,
        )
    except BootstrapSchemaError:
        logger.exception(
            "[sv7][wiring] FALLO en bootstrap-schema. El sv7 NO puede "
            "arrancar sin un schema válido en la BBDD compartida. "
            "Asegúrate de que sv3 y sv6 están arriba y exponen "
            "GET /schema/ddl, y revisa SCHEMA_CONTRIBUTOR_URLS en .env."
        )
        raise

    # ----- 3. Tablas propias del sv7 ------------------------------ #
    repo = SqlAlchemyWorkflowRepository(sf)
    repo.initialize()

    # ----- 4. Resto del wiring ------------------------------------ #
    # sv2 fase 1 → ExtractorClient
    extractor_client = HttpExtractorClient(
        base_url=settings.sv2_base_url,
        path_extract=settings.sv2_path_extract_phase_1,
        timeout_s=settings.sv2_timeout_s,
        retry_policy=retry_policy,
    )
    # sv2 fase 2 → ReviewerClient
    reviewer_client = HttpReviewerClient(
        base_url=settings.sv2_base_url,
        path_review=settings.sv2_path_extract_phase_2,
        timeout_s=settings.sv2_review_timeout_s,
        retry_policy=retry_policy,
    )
    persister_client = HttpPersisterClient(
        base_url=settings.sv3_base_url,
        path_persist=settings.sv3_path_persist,
        timeout_s=settings.sv3_timeout_s,
        retry_policy=retry_policy,
    )
    valuator_client = HttpValuatorClient(
        base_url=settings.sv6_base_url,
        path_run=settings.sv6_path_run,
        path_rerun=settings.sv6_path_rerun,
        timeout_s=settings.sv6_timeout_s,
        retry_policy=retry_policy,
    )

    workflow = AlbaranE2EWorkflow(
        extractor=extractor_client,
        reviewer=reviewer_client,
        persister=persister_client,
        valuator=valuator_client,
        apply_phase_2_patch=settings.apply_phase_2_patch,
    )
    existing_resolver = ExistingDocResolver(sf)

    engine = WorkflowEngine(
        repository=repo,
        workflow=workflow,
        existing_doc_resolver=existing_resolver.resolve,
        tmpdir_cleanup=_cleanup_tmpfile,
    )
    guard = IdempotencyGuard(repo)
    dispatcher = EventDispatcher(
        repository=repo,
        engine=engine,
        guard=guard,
    )
    retrier = FailedWorkflowRetrier(
        repository=repo,
        engine=engine,
        interval_s=settings.workflow_retrier_interval_s,
        min_age_s=settings.workflow_retrier_min_age_s,
        max_retries=settings.workflow_max_auto_retries,
    )

    tmpdir = Path(settings.tmpdir_path)
    tmpdir.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.resume_active_workflows_on_boot:
            _resume_active_workflows(engine=engine, repository=repo)
        await retrier.start()
        try:
            yield
        finally:
            await retrier.stop()

    app = FastAPI(
        title="albaranes-orchestrator-api",
        version=settings.service_version,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.repo = repo
    app.state.engine = engine
    app.state.dispatcher = dispatcher
    app.state.bootstrap_report = bootstrap_report

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "albaranes-orchestrator-api",
            "version": settings.service_version,
            "retrier_enabled": retrier.enabled,
            "phase_2_mode": (
                "merge" if settings.apply_phase_2_patch else "shadow"
            ),
            "downstream_paths": {
                "sv2_phase_1": settings.sv2_path_extract_phase_1,
                "sv2_phase_2": settings.sv2_path_extract_phase_2,
                "sv3_persist": settings.sv3_path_persist,
                "sv6_run": settings.sv6_path_run,
            },
            "schema_contributors": [
                {
                    "schema_name": a.schema_name,
                    "schema_version": a.schema_version,
                    "contributor_base_url": a.contributor_base_url,
                    "statements_applied": a.statements_applied,
                }
                for a in bootstrap_report.applied
            ],
        }

    @app.post("/v1/events/email-received")
    async def email_received(
        background_tasks: BackgroundTasks,
        meta: str = Form(...),
        file: UploadFile = File(...),
    ):
        try:
            event = EmailReceivedEvent.model_validate_json(meta)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"meta inválido: {exc}") from exc

        logger.info(
            "POST /v1/events/email-received correlation=%s size=%s",
            event.correlation_key,
            event.attachment_size_bytes,
        )

        saved_path = _save_upload_to_tmp(upload=file, tmpdir=tmpdir)

        ack, workflow_id_to_run = dispatcher.handle_email_received(
            event,
            file_path=str(saved_path),
        )

        if workflow_id_to_run is not None:
            background_tasks.add_task(
                _run_engine_safely,
                engine=engine,
                workflow_id=workflow_id_to_run,
            )
        else:
            _cleanup_tmpfile(str(saved_path))

        return ack

    @app.post("/v1/events/contract-selected")
    async def contract_selected(
        background_tasks: BackgroundTasks,
        event: ContractSelectedEvent,
    ):
        logger.info(
            "POST /v1/events/contract-selected doc=%s codigo=%s",
            event.document_id,
            event.codigo_contrato,
        )
        result, workflow_id_to_run = dispatcher.handle_contract_selected(event)
        if workflow_id_to_run is not None:
            background_tasks.add_task(
                _resume_engine_safely,
                engine=engine,
                workflow_id=workflow_id_to_run,
                next_state=WorkflowState.VALUING,
                event_payload={
                    "selected_contrato_codigo": event.codigo_contrato,
                    "contract_selected_by": event.selected_by,
                    "contract_selected_at_utc": event.selected_at_utc,
                },
            )
        return result

    @app.post("/v1/events/document-approved")
    async def document_approved(event: DocumentApprovedEvent):
        logger.info(
            "POST /v1/events/document-approved doc=%s by=%s",
            event.document_id,
            event.approved_by,
        )
        return dispatcher.handle_document_approved(event)

    @app.get("/v1/workflows/{workflow_id}")
    def get_workflow(workflow_id: str):
        run = repo.find_by_id(workflow_id)
        if run is None:
            raise HTTPException(status_code=404, detail="workflow no encontrado")
        return {
            "id": run.id,
            "kind": run.kind,
            "current_state": run.current_state.value,
            "document_id": run.document_id,
            "correlation_key": run.correlation_key,
            "started_at_utc": run.started_at_utc,
            "updated_at_utc": run.updated_at_utc,
            "completed_at_utc": run.completed_at_utc,
            "retry_count": run.retry_count,
            "last_error": run.last_error,
            "parent_workflow_id": run.parent_workflow_id,
            "payload_keys": list(run.payload.keys()) if run.payload else [],
        }

    @app.post("/v1/workflows/{workflow_id}/retry-from-step")
    def retry_from_step(
        workflow_id: str,
        background_tasks: BackgroundTasks,
        step: str,
    ):
        run = repo.find_by_id(workflow_id)
        if run is None:
            raise HTTPException(status_code=404, detail="workflow no encontrado")

        try:
            target_state = WorkflowState(step)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"estado inválido: {step}") from exc

        if target_state not in {
            WorkflowState.EXTRACTING,
            WorkflowState.REVIEWING,
            WorkflowState.PERSISTING,
            WorkflowState.VALUING,
        }:
            raise HTTPException(
                status_code=422,
                detail=f"step '{step}' no es retomable manualmente",
            )

        run.current_state = target_state
        run.retry_count += 1
        run.last_error = None
        repo.update(run)
        background_tasks.add_task(
            _run_engine_safely,
            engine=engine,
            workflow_id=run.id,
        )
        return {
            "workflow_id": run.id,
            "new_state": target_state.value,
            "retry_count": run.retry_count,
        }

    return app


# --------------------------------------------------------------- #
# Helpers de ejecución segura.
# --------------------------------------------------------------- #
def _run_engine_safely(*, engine: WorkflowEngine, workflow_id: str) -> None:
    try:
        engine.run_until_passive(workflow_id)
    except Exception:
        logger.exception(
            "engine.run_until_passive falló para workflow_id=%s",
            workflow_id,
        )


def _resume_engine_safely(
    *,
    engine: WorkflowEngine,
    workflow_id: str,
    next_state: WorkflowState,
    event_payload: dict | None,
) -> None:
    try:
        engine.resume_from_passive(
            workflow_id,
            next_state=next_state,
            event_payload=event_payload,
        )
    except Exception:
        logger.exception(
            "engine.resume_from_passive falló para workflow_id=%s",
            workflow_id,
        )


def _resume_active_workflows(
    *,
    engine: WorkflowEngine,
    repository,
) -> None:
    candidates = repository.list_in_states(list(ACTIVE_STATES), limit=200)
    if not candidates:
        return
    logger.info("boot: %d workflow(s) en estado activo, retomando…", len(candidates))
    for run in candidates:
        try:
            engine.run_until_passive(run.id)
        except Exception:
            logger.exception(
                "boot: fallo retomando workflow %s",
                run.id,
                extra={"workflow_id": run.id},
            )
