# infrastructure/clients/http_schema_ddl_client.py
"""Cliente HTTP que descarga el DDL público de un servicio contribuyente.

Cada microservicio "schema contributor" expone:

    GET /schema/ddl

con la siguiente forma de respuesta JSON::

    {
        "schema_name": "albaran_persist",
        "schema_version": 1,
        "depends_on": [],
        "owned_tables": [...],
        "external_table_dependencies": [...],
        "ddl_statements": [{"label": "...", "sql": "..."}, ...],
        "total_statements": N
    }

Este cliente es un adapter pasivo: hace la llamada HTTP, valida el
shape básico de la respuesta y devuelve un DTO ya parseado.

NO toca la BBDD. NO ordena nada. NO aplica DDL. Solo "trae el dato".
La aplicación (``SchemaOrchestrator``) decide qué hacer con lo que
devuelva.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Tuple

import httpx

from application.retry.http_retry_policy import HttpRetryPolicy

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SchemaContribution:
    """DTO con el contenido completo de ``GET /schema/ddl`` ya parseado.

    El orden de ``ddl_statements`` se respeta tal cual lo entregue el
    contributor: él sabe en qué orden hay que aplicar sus sentencias.
    """

    schema_name: str
    schema_version: int
    depends_on: List[str]
    owned_tables: List[str]
    external_table_dependencies: List[str]
    ddl_statements: List[Tuple[str, str]]  # [(label, sql), ...]
    contributor_base_url: str  # útil para mensajes de log y diagnóstico


class SchemaDdlClientError(RuntimeError):
    """Wrapper para fallos al recuperar el DDL de un contributor."""


class HttpSchemaDdlClient:
    """Adapter HTTP que recupera el DDL público de un servicio.

    Sigue el mismo patrón que los demás clientes del sv7
    (``HttpPersisterClient`` etc.): inyectamos ``HttpRetryPolicy`` y
    ``timeout_s``, y delegamos los reintentos en la política. Si tras
    los reintentos el endpoint sigue fallando, levantamos
    ``SchemaDdlClientError`` con un mensaje claro.
    """

    def __init__(
        self,
        *,
        timeout_s: float,
        retry_policy: HttpRetryPolicy,
        path: str = "/schema/ddl",
    ) -> None:
        self._timeout_s = timeout_s
        self._retry = retry_policy
        self._path = path

    def fetch(self, base_url: str) -> SchemaContribution:
        """Descarga el DDL del contributor en ``base_url``.

        Raises
        ------
        SchemaDdlClientError
            Si la llamada HTTP falla tras los reintentos, si el
            endpoint devuelve un status >= 400 final, o si el JSON
            no respeta el shape esperado.
        """
        normalized = base_url.rstrip("/")
        url = f"{normalized}{self._path}"

        def _do() -> httpx.Response:
            with httpx.Client(timeout=self._timeout_s) as client:
                return client.get(url)

        try:
            response = self._retry.execute(
                _do,
                operation_name=f"GET {url}",
            )
        except RuntimeError as exc:
            raise SchemaDdlClientError(
                f"No se pudo obtener el DDL de {url}: {exc}"
            ) from exc

        try:
            payload = response.json()
        except Exception as exc:
            raise SchemaDdlClientError(
                f"{url} devolvió payload no-JSON: {exc}"
            ) from exc

        return self._parse(payload, contributor_base_url=normalized)

    @staticmethod
    def _parse(
        payload: dict, *, contributor_base_url: str,
    ) -> SchemaContribution:
        """Valida el shape del payload y construye el DTO.

        Validación intencionalmente estricta: si falta cualquier campo
        requerido, fallamos pronto y con error claro. Mejor reventar
        en arranque que aplicar un DDL parcial silenciosamente.
        """
        try:
            schema_name = str(payload["schema_name"])
            schema_version = int(payload.get("schema_version", 0))
            depends_on = [str(x) for x in (payload.get("depends_on") or [])]
            owned_tables = [str(x) for x in (payload.get("owned_tables") or [])]
            external_deps = [
                str(x) for x in
                (payload.get("external_table_dependencies") or [])
            ]
            raw_statements = payload.get("ddl_statements") or []
            ddl_statements: List[Tuple[str, str]] = []
            for item in raw_statements:
                if not isinstance(item, dict):
                    raise SchemaDdlClientError(
                        "Cada elemento de ddl_statements debe ser un dict "
                        f"con 'label' y 'sql'. Recibido: {type(item).__name__}"
                    )
                label = str(item.get("label") or "<sin-label>")
                sql = item.get("sql")
                if not isinstance(sql, str) or not sql.strip():
                    raise SchemaDdlClientError(
                        f"Sentencia DDL '{label}' sin SQL válido."
                    )
                ddl_statements.append((label, sql))
        except KeyError as exc:
            raise SchemaDdlClientError(
                f"Falta campo requerido en payload de schema/ddl: {exc}"
            ) from exc

        return SchemaContribution(
            schema_name=schema_name,
            schema_version=schema_version,
            depends_on=depends_on,
            owned_tables=owned_tables,
            external_table_dependencies=external_deps,
            ddl_statements=ddl_statements,
            contributor_base_url=contributor_base_url,
        )
