# application/pipelines/bootstrap_schema_pipeline.py
"""Pipeline de arranque del schema multi-servicio.

Pasos:

    1. Para cada URL de contributor configurada, llamar a
       ``GET /schema/ddl`` y descargar su DDL.
    2. Pasar todas las contribuciones al
       :class:`SchemaOrchestrator`, que las ordena topológicamente
       y las aplica en autocommit.

Si cualquiera de los contributors NO responde (timeout, 5xx
sostenido, JSON malformado), el pipeline FALLA en arranque. Es
intencional: mejor reventar pronto que arrancar el sv7 con un
schema parcial.

Encaja con el patrón pipeline del resto del sv7. ``run()`` es
síncrono y no devuelve nada porque el caller solo necesita saber si
falló o no — los detalles se loguean.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List

from sqlalchemy.engine import Engine

from application.services.schema_orchestrator import (
    SchemaOrchestrator,
    SchemaOrchestratorReport,
)
from infrastructure.clients.http_schema_ddl_client import (
    HttpSchemaDdlClient,
    SchemaContribution,
    SchemaDdlClientError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BootstrapSchemaRequest:
    """Configuración mínima para correr el pipeline."""

    contributor_urls: List[str]
    """URLs base de cada servicio (sin /schema/ddl). El cliente añade
    el path por sí mismo."""


class BootstrapSchemaError(RuntimeError):
    """Falló el bootstrap del schema (descarga o aplicación)."""


class BootstrapSchemaPipeline:
    """Orquesta la descarga de DDL y su aplicación en BBDD.

    Diseño Hexagonal:
      * cliente HTTP inyectado (port: ``HttpSchemaDdlClient``).
      * orquestador inyectado.

    Esto facilita testear el pipeline con mocks o stubs.
    """

    def __init__(
        self,
        *,
        ddl_client: HttpSchemaDdlClient,
        orchestrator: SchemaOrchestrator,
    ) -> None:
        self._client = ddl_client
        self._orchestrator = orchestrator

    def run(
        self, request: BootstrapSchemaRequest,
    ) -> SchemaOrchestratorReport:
        """Ejecuta el pipeline completo.

        Returns
        -------
        SchemaOrchestratorReport
            Reporte con qué contributors se aplicaron y cuántas
            sentencias en total. Útil para logs y health checks.

        Raises
        ------
        BootstrapSchemaError
            Si cualquier contributor falla al responder, o si el
            orquestador falla aplicando una sentencia.
        """
        if not request.contributor_urls:
            logger.warning(
                "[bootstrap-schema] no hay contributors configurados — "
                "el sv7 arrancará sin schema externo. ¿Es esto intencional?"
            )
            return SchemaOrchestratorReport()

        logger.info(
            "[bootstrap-schema] descubriendo schema de %d contributor(s): %s",
            len(request.contributor_urls),
            request.contributor_urls,
        )

        contributions: List[SchemaContribution] = []
        for url in request.contributor_urls:
            try:
                contribution = self._client.fetch(url)
                logger.info(
                    "[bootstrap-schema] %s → %s v%d (%d sentencias, "
                    "depends_on=%s)",
                    url,
                    contribution.schema_name,
                    contribution.schema_version,
                    len(contribution.ddl_statements),
                    contribution.depends_on,
                )
                contributions.append(contribution)
            except SchemaDdlClientError as exc:
                raise BootstrapSchemaError(
                    f"Fallo descubriendo schema de {url}: {exc}. "
                    "Asegúrate de que el servicio está arriba y expone "
                    "GET /schema/ddl."
                ) from exc

        try:
            return self._orchestrator.apply(contributions)
        except Exception as exc:
            raise BootstrapSchemaError(
                f"Fallo aplicando DDL contribuido: {exc}"
            ) from exc


# --------------------------------------------------------------------------- #
# Helper de composición — facilita el wiring desde app.py / main.
# --------------------------------------------------------------------------- #

def build_bootstrap_pipeline(
    *,
    engine: Engine,
    ddl_client: HttpSchemaDdlClient,
) -> BootstrapSchemaPipeline:
    """Construye el pipeline con el orquestador por defecto.

    Pequeña factoría — evita que el caller tenga que conocer los
    detalles del orquestador.
    """
    return BootstrapSchemaPipeline(
        ddl_client=ddl_client,
        orchestrator=SchemaOrchestrator(engine=engine),
    )
