# config/settings.py
from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"

LogFormat = Literal["text", "json"]


class Settings(BaseSettings):
    """Configuración del servicio 7 (orchestrator-api).

    Coordina sv2 (extractor), sv3 (persister) y sv6 (valuation) según
    una state machine persistida en BBDD compartida. No persiste datos
    de albarán/contrato — solo metadatos del workflow.
    """

    # ------------------------------------------------------------ #
    # BBDD compartida (mismas credenciales que sv3/sv4/sv5/sv6).
    # sv7 escribe en sus 2 tablas (workflow_runs, workflow_step_history).
    #
    # AUTO-CREATE: igual que sv3/sv4, sv7 se conecta primero como
    # admin (PG_ADMIN_*) y, si AUTO_CREATE_DATABASE=true y la BBDD
    # ``albaranes`` no existe, la crea. Después se reconecta a la
    # BBDD recién creada y aplica el DDL idempotente de sus tablas.
    # ------------------------------------------------------------ #
    pg_host: str = Field("localhost", alias="PG_HOST")
    pg_port: int = Field(5432, alias="PG_PORT")
    pg_db: str = Field("albaranes", alias="PG_DB")
    pg_user: str = Field("postgres", alias="PG_USER")
    pg_password: str = Field(..., alias="PG_PASSWORD")

    pg_admin_db: str = Field("postgres", alias="PG_ADMIN_DB")
    pg_admin_user: str = Field("postgres", alias="PG_ADMIN_USER")
    pg_admin_password: str = Field(..., alias="PG_ADMIN_PASSWORD")
    auto_create_database: bool = Field(True, alias="AUTO_CREATE_DATABASE")

    # ------------------------------------------------------------ #
    # Clientes HTTP a otros servicios.
    # Timeouts pensados para los peores casos:
    #   sv2: 180s (extractor multi-LLM con PDFs grandes).
    #   sv3: 120s (persist + enrich + SharePoint upload).
    #   sv6: 300s (valoración IA con PDF de contrato).
    # ------------------------------------------------------------ #
    sv2_base_url: str = Field("http://127.0.0.1:8000", alias="SV2_BASE_URL")
    sv2_timeout_s: float = Field(180.0, alias="SV2_TIMEOUT_S")
    sv2_path_extract: str = Field(
        "/v1/albaranes/extract",
        alias="SV2_PATH_EXTRACT",
    )

    sv3_base_url: str = Field("http://127.0.0.1:8001", alias="SV3_BASE_URL")
    sv3_timeout_s: float = Field(120.0, alias="SV3_TIMEOUT_S")
    sv3_path_persist: str = Field(
        "/v1/albaranes/persist",
        alias="SV3_PATH_PERSIST",
    )

    sv6_base_url: str = Field("http://127.0.0.1:8003", alias="SV6_BASE_URL")
    sv6_timeout_s: float = Field(300.0, alias="SV6_TIMEOUT_S")
    sv6_path_run: str = Field("/v1/valuation/run", alias="SV6_PATH_RUN")
    sv6_path_rerun: str = Field(
        "/v1/valuation/{document_id}/re-run",
        alias="SV6_PATH_RERUN",
    )

    # ------------------------------------------------------------ #
    # Política de reintentos a nivel HTTP (cada llamada a
    # sv2/sv3/sv6 reintenta hasta http_max_retries veces antes de
    # marcar el step como FAILED).
    # ------------------------------------------------------------ #
    http_max_retries: int = Field(3, alias="HTTP_MAX_RETRIES")
    http_backoff_base_s: float = Field(2.0, alias="HTTP_BACKOFF_BASE_S")
    http_backoff_cap_s: float = Field(30.0, alias="HTTP_BACKOFF_CAP_S")

    # ------------------------------------------------------------ #
    # Reintentos automáticos de workflows en estado *_failed.
    # ------------------------------------------------------------ #
    workflow_max_auto_retries: int = Field(
        3,
        alias="WORKFLOW_MAX_AUTO_RETRIES",
    )
    workflow_retrier_interval_s: int = Field(
        60,
        alias="WORKFLOW_RETRIER_INTERVAL_S",
        description="Cada cuántos segundos corre el job de reintentos.",
    )
    workflow_retrier_min_age_s: int = Field(
        120,
        alias="WORKFLOW_RETRIER_MIN_AGE_S",
        description=(
            "Edad mínima (segundos desde updated_at_utc) de un *_failed "
            "antes de que el retrier lo reabra. Evita reintentar inmediato."
        ),
    )

    # ------------------------------------------------------------ #
    # Working dir para guardar adjuntos temporalmente entre el evento
    # email-received (multipart) y la llamada a sv2/sv3 en el
    # BackgroundTask. Se limpia al terminar el workflow.
    # ------------------------------------------------------------ #
    tmpdir_path: str = Field("/tmp/sv7", alias="TMPDIR_PATH")

    # ------------------------------------------------------------ #
    # API
    # ------------------------------------------------------------ #
    api_host: str = Field("127.0.0.1", alias="API_HOST")
    api_port: int = Field(8005, alias="API_PORT")
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    log_dir: str = Field("logs", alias="LOG_DIR")
    log_format: LogFormat = Field("text", alias="LOG_FORMAT")
    service_version: str = Field("1.0.0", alias="SERVICE_VERSION")

    resume_active_workflows_on_boot: bool = Field(
        True,
        alias="RESUME_ACTIVE_WORKFLOWS_ON_BOOT",
    )

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def _ensure_sane_retry_config(self) -> "Settings":
        if self.http_max_retries < 1:
            raise ValueError("HTTP_MAX_RETRIES debe ser >= 1.")
        if self.workflow_max_auto_retries < 0:
            raise ValueError(
                "WORKFLOW_MAX_AUTO_RETRIES debe ser >= 0 "
                "(0 = desactiva los reintentos automáticos)."
            )
        return self

    @property
    def database_url(self) -> str:
        user = quote_plus(self.pg_user)
        password = quote_plus(self.pg_password)
        database = quote_plus(self.pg_db)
        return (
            f"postgresql+psycopg://{user}:{password}"
            f"@{self.pg_host}:{self.pg_port}/{database}"
        )

    @property
    def admin_database_url(self) -> str:
        user = quote_plus(self.pg_admin_user)
        password = quote_plus(self.pg_admin_password)
        database = quote_plus(self.pg_admin_db)
        return (
            f"postgresql+psycopg://{user}:{password}"
            f"@{self.pg_host}:{self.pg_port}/{database}"
        )