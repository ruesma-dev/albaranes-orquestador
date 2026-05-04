# application/services/existing_doc_resolver.py
"""Resolver del estado de un documento existente (caso duplicado).

Usado por el handle_persisting cuando sv3 devuelve duplicate=true:
mira en BBDD si el documento ya está aprobado, valorado, etc., y
devuelve el dict que el workflow concreto necesita para decidir el
siguiente estado.

Lee directamente de las tablas de sv3/sv4/sv6 (BBDD compartida). Es
LECTURA pura: no escribe.
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from infrastructure.database.session_factory import SessionFactory

logger = logging.getLogger(__name__)


class ExistingDocResolver:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._sf = session_factory

    def resolve(self, document_id: str) -> dict | None:
        """Devuelve dict con el estado del documento existente o None
        si no existe."""
        with self._sf.create_session() as session:
            try:
                doc_row = session.execute(
                    text(
                        "SELECT approved, selected_contrato_codigo "
                        "FROM albaran_documents_merge "
                        "WHERE id = :id LIMIT 1"
                    ),
                    {"id": document_id},
                ).mappings().first()
            except Exception:
                logger.exception(
                    "ExistingDocResolver: error leyendo albaran_documents_merge"
                )
                return None

            if doc_row is None:
                return None

            try:
                val_row = session.execute(
                    text(
                        "SELECT status FROM albaran_valuations "
                        "WHERE document_id = :id LIMIT 1"
                    ),
                    {"id": document_id},
                ).mappings().first()
            except Exception:
                # Tabla puede no existir en BBDD muy antigua.
                val_row = None

            return {
                "is_approved": bool(doc_row["approved"]),
                "selected_contrato_codigo": doc_row["selected_contrato_codigo"],
                "has_valuation": val_row is not None,
                "valuation_status": val_row["status"] if val_row else None,
            }
