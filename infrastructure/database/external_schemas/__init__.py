# infrastructure/database/external_schemas/__init__.py
"""Schemas REPLICADOS desde otros microservicios.

⚠️ ATENCIÓN ⚠️

Estos archivos son copia literal de los modelos ORM de sv3
(albaranes-persistence-api). sv7 los necesita SOLO para poder
crear las tablas correspondientes en BBDD si no existen
(``Base.metadata.create_all()``), de modo que el sistema arranque
limpio sin requerir un orden estricto de servicios.

REGLA DE MANTENIMIENTO:
  Cuando alguien modifique un modelo ORM en sv3 (añadir columna,
  cambiar tipo, etc.), DEBE actualizar también el archivo
  correspondiente aquí. De lo contrario, si sv7 arranca primero
  contra una BBDD vacía, creará la tabla con el schema viejo y sv3
  no podrá actualizarla porque sv3 usa CREATE IF NOT EXISTS.

Los archivos aquí presentes:
  - orm_models.py
  - orm_contrato_models.py
  - orm_contrato_cache_models.py

provienen de sv3 → infrastructure/database/. Mantenerlos
SINCRONIZADOS byte a byte siempre que sea posible.
"""
