# application/services/schema_orchestrator.py
"""Aplica el DDL de varios contributors en orden topológico.

Recibe una colección de ``SchemaContribution`` (cada una con un
``schema_name`` y un ``depends_on``), las ordena de modo que las
dependencias se apliquen ANTES que sus dependientes, y ejecuta cada
sentencia DDL en autocommit contra el engine SQLAlchemy provisto.

Garantías:

* **Orden topológico determinista**. Si A depende de B, B se aplica
  antes que A. Si hay un ciclo (no debería ocurrir nunca por diseño,
  pero defensivo), levantamos ``SchemaCycleError`` con la lista de
  nodos involucrados.

* **Sentencia a sentencia en autocommit**. Cada DDL se compromete
  independientemente, así un fallo puntual no aborta el resto. Si
  una sentencia falla, levantamos error con label explícito (ya
  vimos lo que cuesta diagnosticar fallos silenciosos en
  transacciones abortadas — el caso del bug de
  ``descuento_albaran_aplicado`` precisamente venía de eso).

* **Idempotente**. La idempotencia la garantizan los contributors
  (todas sus sentencias usan ``IF NOT EXISTS`` o
  ``DROP NOT NULL``). Este orquestador no lo verifica, pero
  re-aplicar todo es seguro por construcción.

NO descarga nada. NO sabe HTTP. Solo lógica pura sobre los DTOs y
ejecución contra el engine. El descubrimiento (HTTP) vive en el
pipeline.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Set

from sqlalchemy import text
from sqlalchemy.engine import Engine

from infrastructure.clients.http_schema_ddl_client import SchemaContribution

logger = logging.getLogger(__name__)


class SchemaCycleError(RuntimeError):
    """Se ha detectado un ciclo de dependencias entre contributors."""


class SchemaApplyError(RuntimeError):
    """Una sentencia DDL ha fallado al aplicarse."""


@dataclass(frozen=True)
class AppliedSchema:
    """Resultado de aplicar un contributor concreto."""

    schema_name: str
    schema_version: int
    contributor_base_url: str
    statements_applied: int


@dataclass
class SchemaOrchestratorReport:
    """Reporte agregado tras aplicar todos los contributors."""

    applied: List[AppliedSchema] = field(default_factory=list)
    total_statements: int = 0


class SchemaOrchestrator:
    """Servicio que ordena y aplica DDL de varios contributors.

    Dependencias se inyectan en construcción (engine). Operación
    principal: ``apply(contributions)``.
    """

    def __init__(self, *, engine: Engine) -> None:
        self._engine = engine

    def apply(
        self,
        contributions: List[SchemaContribution],
    ) -> SchemaOrchestratorReport:
        """Ordena topológicamente y aplica todas las contribuciones.

        Si hay un fallo en una sentencia concreta, levanta
        ``SchemaApplyError`` con el label, el contributor y la
        excepción original.
        """
        if not contributions:
            logger.info(
                "[schema-orchestrator] sin contributors → no-op."
            )
            return SchemaOrchestratorReport()

        ordered = self._topological_sort(contributions)

        logger.info(
            "[schema-orchestrator] orden de aplicación: %s",
            " → ".join(c.schema_name for c in ordered),
        )

        report = SchemaOrchestratorReport()
        autocommit_engine = self._engine.execution_options(
            isolation_level="AUTOCOMMIT",
        )
        for contribution in ordered:
            applied_count = 0
            n = len(contribution.ddl_statements)
            logger.info(
                "[schema-orchestrator][%s v%d] aplicando %d sentencias "
                "desde %s ...",
                contribution.schema_name,
                contribution.schema_version,
                n,
                contribution.contributor_base_url,
            )
            for idx, (label, sql) in enumerate(
                contribution.ddl_statements, start=1,
            ):
                try:
                    with autocommit_engine.connect() as conn:
                        conn.execute(text(sql))
                    applied_count += 1
                    logger.debug(
                        "[schema-orchestrator][%s] [%2d/%2d] OK %s",
                        contribution.schema_name, idx, n, label,
                    )
                except Exception as exc:
                    logger.error(
                        "[schema-orchestrator][%s] [%2d/%2d] FALLO %s -> %s",
                        contribution.schema_name, idx, n, label, exc,
                    )
                    raise SchemaApplyError(
                        f"Fallo aplicando DDL del contributor "
                        f"'{contribution.schema_name}' "
                        f"(stmt {idx}/{n}, label='{label}'): {exc}"
                    ) from exc

            report.applied.append(
                AppliedSchema(
                    schema_name=contribution.schema_name,
                    schema_version=contribution.schema_version,
                    contributor_base_url=contribution.contributor_base_url,
                    statements_applied=applied_count,
                )
            )
            report.total_statements += applied_count
            logger.info(
                "[schema-orchestrator][%s v%d] OK %d/%d sentencias.",
                contribution.schema_name,
                contribution.schema_version,
                applied_count,
                n,
            )

        logger.info(
            "[schema-orchestrator] DONE — %d contributors, %d sentencias "
            "totales aplicadas.",
            len(report.applied),
            report.total_statements,
        )
        return report

    # ----------------------------------------------------------- #
    # Topología.
    # ----------------------------------------------------------- #
    @staticmethod
    def _topological_sort(
        contributions: List[SchemaContribution],
    ) -> List[SchemaContribution]:
        """Kahn-like sort: nodos sin dependencias primero.

        Si un contributor depende de un schema que NO está en el
        conjunto recibido, ignoramos esa dependencia con un warning
        (probablemente un schema externo o uno aún no migrado), pero
        seguimos. Si hay ciclos reales, levanta
        ``SchemaCycleError``.
        """
        by_name: Dict[str, SchemaContribution] = {
            c.schema_name: c for c in contributions
        }
        # Validar nombres únicos.
        if len(by_name) != len(contributions):
            duplicates = [
                c.schema_name for c in contributions
                if [c2.schema_name for c2 in contributions].count(c.schema_name) > 1
            ]
            raise SchemaCycleError(
                "Hay schema_name duplicados entre contributors: "
                f"{set(duplicates)}. Cada contributor debe tener un "
                "nombre único."
            )

        # Filtrar dependencias inexistentes con un warning.
        effective_deps: Dict[str, Set[str]] = {}
        for contribution in contributions:
            kept: Set[str] = set()
            for dep in contribution.depends_on:
                if dep in by_name:
                    kept.add(dep)
                else:
                    logger.warning(
                        "[schema-orchestrator][%s] declara depender de "
                        "'%s' pero ese contributor no está en el "
                        "conjunto recibido. Lo ignoramos en el orden — "
                        "asumimos que existe ya en BBDD por otra vía.",
                        contribution.schema_name, dep,
                    )
            effective_deps[contribution.schema_name] = kept

        # Algoritmo de Kahn: ir sacando nodos con in-degree 0.
        ordered: List[SchemaContribution] = []
        remaining = dict(effective_deps)  # copia mutable
        # Para determinismo (stable order), procesamos por orden
        # alfabético de schema_name.
        while remaining:
            ready = sorted(
                name for name, deps in remaining.items() if not deps
            )
            if not ready:
                # Ciclo: lo que queda forma uno o varios ciclos.
                raise SchemaCycleError(
                    "Ciclo de dependencias entre contributors. "
                    f"Nodos atascados: {sorted(remaining.keys())}"
                )
            for name in ready:
                ordered.append(by_name[name])
                del remaining[name]
                # Quitar este nodo de las deps de los demás.
                for deps in remaining.values():
                    deps.discard(name)

        return ordered
