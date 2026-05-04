# infrastructure/database/orm_contrato_cache_models.py
"""Tablas de caché de contratos.

Estas tablas guardan contratos de Sigrid de forma desacoplada de un
``document_id`` concreto. Sirven como caché compartida entre albaranes:
cuando llega un albarán nuevo y existe un contrato en caché que cubra
su fecha (vigencia_desde ≤ fecha_albaran ≤ vigencia_hasta) para la
combinación ``(codigo_obra, cif_proveedor)``, se reutiliza sin volver
a llamar a la Function App de Sigrid.

Diferencias respecto a ``AlbaranContratoMergeOrm`` /
``AlbaranContratoLineMergeOrm``:
  - No hay FK a ``albaran_documents_merge`` (la caché es global).
  - UNIQUE cuádruple
    ``(codigo_obra, cif_proveedor, codigo_contrato, fecha_alta_contrato)``
    para permitir distintas versiones del mismo contrato lógico.
  - El resto de columnas (datos del contrato y de las líneas) son
    espejos de los modelos de merge.
"""
from __future__ import annotations

from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from infrastructure.database.external_schemas.orm_models import Base


class ContratoCacheOrm(Base):
    """Cabecera de un contrato cacheado.

    Discriminador de versión: ``fecha_alta_contrato``. Si Sigrid
    devuelve dos versiones del mismo ``codigo_contrato`` (porque hubo
    addenda/revisión), tendremos dos filas distintas en esta tabla y
    elegiremos en lookup la cuya vigencia cubra la fecha del albarán.
    """

    __tablename__ = "contratos_cache"

    id = Column(Integer, primary_key=True, autoincrement=True)

    codigo_obra = Column(String(32), nullable=False)
    cif_proveedor = Column(String(32), nullable=False)
    codigo_contrato = Column(String(64), nullable=False)
    fecha_alta_contrato = Column(Integer, nullable=True)

    # Datos del contrato (espejo de albaran_contratos_merge sin doc_id).
    nombre_contrato = Column(Text, nullable=True)
    fecha_contrato = Column(Integer, nullable=True)
    vigencia_desde = Column(Integer, nullable=True)
    vigencia_hasta = Column(Integer, nullable=True)
    importe_total = Column(Float, nullable=True)
    nombre_proveedor = Column(Text, nullable=True)
    nombre_obra = Column(Text, nullable=True)

    # Datos del PDF en SharePoint.
    gra_rep_ide = Column(Integer, nullable=True)
    pdf_sharepoint_relative_path = Column(String(1024), nullable=True)
    pdf_sharepoint_web_url = Column(String(1024), nullable=True)

    # Auditoría.
    fetched_at_utc = Column(String(64), nullable=False)

    lines = relationship(
        "ContratoCacheLineOrm",
        back_populates="contrato",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "codigo_obra",
            "cif_proveedor",
            "codigo_contrato",
            "fecha_alta_contrato",
            name="uq_contratos_cache_quad",
        ),
        Index(
            "ix_contratos_cache_lookup",
            "codigo_obra",
            "cif_proveedor",
        ),
        Index(
            "ix_contratos_cache_vigencia",
            "vigencia_desde",
            "vigencia_hasta",
        ),
    )


class ContratoCacheLineOrm(Base):
    """Línea de un contrato cacheado (espejo de las del merge)."""

    __tablename__ = "contrato_cache_lines"

    id = Column(Integer, primary_key=True, autoincrement=True)
    contrato_cache_id = Column(
        Integer,
        ForeignKey("contratos_cache.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    codigo_contrato = Column(String(64), nullable=False)
    linea = Column(Integer, nullable=True)
    numero_linea = Column(Integer, nullable=True)
    codigo_producto = Column(String(64), nullable=True)
    codigo_alternativo = Column(String(64), nullable=True)
    unidad_medida = Column(String(32), nullable=True)
    descripcion_linea = Column(Text, nullable=True)
    uds = Column(Float, nullable=True)
    cantidad_servida = Column(Float, nullable=True)
    cantidad_facturada = Column(Float, nullable=True)
    pendiente_servir = Column(Float, nullable=True)
    precio_unitario = Column(Float, nullable=True)
    precio_bruto = Column(Float, nullable=True)
    descuentos = Column(Float, nullable=True)
    importe_linea = Column(Float, nullable=True)
    cuota_iva = Column(Float, nullable=True)
    doc_origen = Column(String(64), nullable=True)
    codigo_partida = Column(String(64), nullable=True)
    descripcion_partida = Column(Text, nullable=True)
    fetched_at_utc = Column(String(64), nullable=False)

    contrato = relationship(
        "ContratoCacheOrm",
        back_populates="lines",
    )

    __table_args__ = (
        Index(
            "ix_contrato_cache_lines_codigo",
            "codigo_contrato",
        ),
    )
