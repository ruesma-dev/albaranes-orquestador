# infrastructure/database/session_factory.py
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)


class SessionFactory:
    """Factoría de sesiones SQLAlchemy con auto-creación de BBDD.

    Replica el patrón de sv3/sv4: si ``auto_create_database=True`` y la
    BBDD ``target_database_name`` no existe, se conecta como admin
    (``admin_database_url``, normalmente apuntando a ``postgres``) y la
    crea con ``CREATE DATABASE ...``. Después construye el engine
    contra la BBDD recién creada (o existente).

    Esto resuelve el caso de "primer arranque del sistema en limpio":
    si sv7 arranca antes que sv3/sv4 y la BBDD ``albaranes`` no existe
    aún, sv7 la crea él mismo y aplica su DDL idempotente sin esperar.

    El método ``create_session()`` se mantiene como context manager
    para alinearse con cómo lo usa el resto del sv7 (con ``with``).
    """

    def __init__(
        self,
        *,
        database_url: str,
        admin_database_url: str | None = None,
        target_database_name: str | None = None,
        auto_create_database: bool = True,
    ) -> None:
        self._database_url = database_url
        self._admin_database_url = admin_database_url
        self._target_database_name = target_database_name
        self._auto_create_database = auto_create_database
        self._lock = threading.RLock()
        self._engine: Engine | None = None
        self._sessionmaker: sessionmaker[Session] | None = None
        self._generation = 0
        self._ensure_database_and_engine()

    # ----------------------------------------------------------- #
    # Bootstrap (BBDD + engine).
    # ----------------------------------------------------------- #
    def _ensure_database_and_engine(self) -> None:
        with self._lock:
            database_created = False
            if self._auto_create_database and self._can_admin():
                database_created = self._ensure_database_exists()

            if self._engine is None or self._sessionmaker is None:
                self._rebuild_engine()
                return

            if database_created:
                self._rebuild_engine()

    def _can_admin(self) -> bool:
        return bool(
            self._admin_database_url
            and self._target_database_name
        )

    def _ensure_database_exists(self) -> bool:
        """Devuelve True si la BBDD se ha creado en esta llamada."""
        admin_engine = create_engine(
            self._admin_database_url,
            future=True,
            isolation_level="AUTOCOMMIT",
            pool_pre_ping=True,
        )
        try:
            exists_sql = text(
                "SELECT 1 FROM pg_database WHERE datname = :database_name"
            )
            with admin_engine.connect() as connection:
                exists = connection.execute(
                    exists_sql,
                    {"database_name": self._target_database_name},
                ).scalar()
                if exists:
                    return False

                safe_db_name = self._target_database_name.replace('"', '""')
                logger.warning(
                    "BBDD '%s' no existe. Creándola con admin…",
                    self._target_database_name,
                )
                connection.execute(text(f'CREATE DATABASE "{safe_db_name}"'))
                logger.info(
                    "BBDD '%s' creada correctamente",
                    self._target_database_name,
                )
                return True
        finally:
            admin_engine.dispose()

    def _rebuild_engine(self) -> None:
        if self._engine is not None:
            self._engine.dispose()

        self._engine = create_engine(
            self._database_url,
            future=True,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
        self._sessionmaker = sessionmaker(
            bind=self._engine,
            expire_on_commit=False,
            future=True,
        )
        self._generation += 1

    # ----------------------------------------------------------- #
    # API pública.
    # ----------------------------------------------------------- #
    @property
    def engine(self) -> Engine:
        self._ensure_database_and_engine()
        assert self._engine is not None
        return self._engine

    @property
    def generation(self) -> int:
        return self._generation

    @contextmanager
    def create_session(self) -> Iterator[Session]:
        """Context manager para compatibilidad con el resto del sv7."""
        self._ensure_database_and_engine()
        assert self._sessionmaker is not None
        session = self._sessionmaker()
        try:
            yield session
        finally:
            session.close()