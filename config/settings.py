# config/settings.py
from __future__ import annotations

from pathlib import Path
from typing import List, Literal
from urllib.parse import quote_plus

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"

LogFormat = Literal["text", "json"]


class Settings(BaseSettings):
    """Configuración del servicio 7 (orchestrator-api).

    CAMBIOS RESPECTO A LA VERSIÓN ANTERIOR (DOS FASES):
      - SV2_PATH_EXTRACT renombrado a SV2_PATH_EXTRACT_PHASE_1.
      - Nuevo SV2_PATH_EXTRACT_PHASE_2 + SV2_REVIEW_TIMEOUT_S.
      - Nuevo flag APPLY_PHASE_2_PATCH (default true) — controla si los
        cambios propuestos por la fase 2 se APLICAN sobre el JSON de
        fase 1 antes de mandarlo a sv3.
        - true  → sv7 fusiona fase 1 + patch fase 2 → sv3 persiste el
                  resultado fusionado. Las líneas modificadas quedan
                  marcadas con source_phase='phase_2'.
        - false → sv7 manda a sv3 el JSON de fase 1 SIN modificar.
                  Pero igual incluye en el envelope el bloque
                  review_phase2_metadata para que sv3 lo persista
                  como AUDITORÍA en las columnas review_phase2_* de
                  albaran_documents_merge. source_phase queda 'phase_1'
                  en todas las líneas. Útil para shadow-mode: dejas
                  fase 2 corriendo y la observas sin que afecte a la
                  BBDD principal.

    REFACTOR SCHEMA CONTRIBUTORS (mayo 2026):
      - Nuevo SCHEMA_CONTRIBUTOR_URLS — lista de URLs base de los
        servicios que CONTRIBUYEN tablas a la BBDD compartida (sv3 y
        sv6). El sv7 al arrancar descubre su DDL vía GET /schema/ddl
        y lo aplica en orden topológico.
      - Eliminado el DDL replicado y desactualizado de sv3/sv4/sv6
        que vivía dentro del sv7. Ahora cada servicio es la única
        fuente de verdad para sus propias tablas.
    """

    # ------------------------------------------------------------ #
    # BBDD compartida.
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
    # sv2 — extractor (fase 1) y reviewer (fase 2).
    # ------------------------------------------------------------ #
    sv2_base_url: str = Field("http://127.0.0.1:8000", alias="SV2_BASE_URL")

    sv2_timeout_s: float = Field(180.0, alias="SV2_TIMEOUT_S")
    sv2_path_extract_phase_1: str = Field(
        "/v1/albaranes/extract/phase-1",
        alias="SV2_PATH_EXTRACT_PHASE_1",
    )

    sv2_review_timeout_s: float = Field(
        180.0,
        alias="SV2_REVIEW_TIMEOUT_S",
        description="Timeout para fase 2.",
    )
    sv2_path_extract_phase_2: str = Field(
        "/v1/albaranes/extract/phase-2",
        alias="SV2_PATH_EXTRACT_PHASE_2",
    )

    # ------------------------------------------------------------ #
    # FLAG DE MERGE FASE 1 + FASE 2.
    #
    # Default true (comportamiento "natural" de fase 2: aplicar los
    # cambios). Pon a false para shadow-mode: ejecuta fase 2 igualmente
    # y guarda los cambios propuestos como auditoría, pero NO modifica
    # los datos persistidos.
    # ------------------------------------------------------------ #
    apply_phase_2_patch: bool = Field(
        True,
        alias="APPLY_PHASE_2_PATCH",
        description=(
            "Si true, sv7 aplica el patch propuesto por fase 2 sobre el "
            "JSON de fase 1 antes de enviarlo a sv3 (líneas tocadas se "
            "marcan source_phase='phase_2'). Si false, manda fase 1 sin "
            "modificar pero conserva los cambios propuestos como auditoría "
            "en review_phase2_payload_json."
        ),
    )

    # ------------------------------------------------------------ #
    # sv3 — persister.
    # ------------------------------------------------------------ #
    sv3_base_url: str = Field("http://127.0.0.1:8001", alias="SV3_BASE_URL")
    sv3_timeout_s: float = Field(120.0, alias="SV3_TIMEOUT_S")
    sv3_path_persist: str = Field(
        "/v1/albaranes/persist",
        alias="SV3_PATH_PERSIST",
    )

    # ------------------------------------------------------------ #
    # sv6 — valuation.
    # ------------------------------------------------------------ #
    sv6_base_url: str = Field("http://127.0.0.1:8003", alias="SV6_BASE_URL")
    sv6_timeout_s: float = Field(300.0, alias="SV6_TIMEOUT_S")
    sv6_path_run: str = Field("/v1/valuation/run", alias="SV6_PATH_RUN")
    sv6_path_rerun: str = Field(
        "/v1/valuation/{document_id}/re-run",
        alias="SV6_PATH_RERUN",
    )

    # ------------------------------------------------------------ #
    # Schema contributors.
    #
    # Lista de URLs base de los servicios que contribuyen tablas a la
    # BBDD compartida. El sv7 al arrancar descubre su DDL vía
    # GET /schema/ddl y lo aplica en orden topológico (resolviendo
    # dependencias declaradas con SCHEMA_DEPENDS_ON).
    #
    # Por defecto apuntamos a los URLs locales de sv3 y sv6. En
    # producción Azure se sobrescribe por .env.
    # ------------------------------------------------------------ #
    schema_contributor_urls_csv: str = Field(
        "http://127.0.0.1:8001,http://127.0.0.1:8003",
        alias="SCHEMA_CONTRIBUTOR_URLS",
        description=(
            "Lista CSV de URLs base de servicios que exponen "
            "GET /schema/ddl. Ej: 'http://localhost:8001,http://localhost:8003'."
        ),
    )
    schema_ddl_timeout_s: float = Field(
        30.0,
        alias="SCHEMA_DDL_TIMEOUT_S",
        description="Timeout HTTP para descargar /schema/ddl.",
    )

    @property
    def schema_contributor_urls(self) -> List[str]:
        """Parsea la CSV en lista limpia (sin entradas vacías)."""
        if not self.schema_contributor_urls_csv:
            return []
        return [
            url.strip()
            for url in self.schema_contributor_urls_csv.split(",")
            if url.strip()
        ]

    # ------------------------------------------------------------ #
    # Política de reintentos HTTP.
    # ------------------------------------------------------------ #
    http_max_retries: int = Field(3, alias="HTTP_MAX_RETRIES")
    http_backoff_base_s: float = Field(2.0, alias="HTTP_BACKOFF_BASE_S")
    http_backoff_cap_s: float = Field(30.0, alias="HTTP_BACKOFF_CAP_S")

    # ------------------------------------------------------------ #
    # Auto-retry de workflows en *_failed.
    # ------------------------------------------------------------ #
    workflow_max_auto_retries: int = Field(3, alias="WORKFLOW_MAX_AUTO_RETRIES")
    workflow_retrier_interval_s: int = Field(60, alias="WORKFLOW_RETRIER_INTERVAL_S")
    workflow_retrier_min_age_s: int = Field(120, alias="WORKFLOW_RETRIER_MIN_AGE_S")

    # ------------------------------------------------------------ #
    # tmpdir + API + logging.
    # ------------------------------------------------------------ #
    tmpdir_path: str = Field("/tmp/sv7", alias="TMPDIR_PATH")
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
