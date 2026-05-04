# infrastructure/database/orm_models.py
from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class _DocumentColumnsMixin:
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    provider_origin: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source_document_id: Mapped[str | None] = mapped_column(String(64), index=True)
    document_storage_ref: Mapped[str | None] = mapped_column(String(1024))
    source_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    source_mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_attachment_filename: Mapped[str | None] = mapped_column(String(255))
    source_attachment_mime_type: Mapped[str | None] = mapped_column(String(255))
    source_attachment_sha256: Mapped[str | None] = mapped_column(String(64))
    page_number: Mapped[int | None] = mapped_column(Integer)
    page_count: Mapped[int | None] = mapped_column(Integer)
    prompt_key: Mapped[str] = mapped_column(String(100), nullable=False)
    schema_name: Mapped[str] = mapped_column(String(100), nullable=False)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    proveedor_nombre: Mapped[str | None] = mapped_column(String(255))
    proveedor_cif: Mapped[str | None] = mapped_column(String(64))
    fecha: Mapped[str | None] = mapped_column(String(32))
    numero_albaran: Mapped[str | None] = mapped_column(String(128))
    forma_pago: Mapped[str | None] = mapped_column(String(128))
    obra_codigo: Mapped[str | None] = mapped_column(String(128))
    obra_nombre: Mapped[str | None] = mapped_column(String(255))
    obra_direccion: Mapped[str | None] = mapped_column(String(255))
    sharepoint_drive_id: Mapped[str | None] = mapped_column(String(255))
    sharepoint_item_id: Mapped[str | None] = mapped_column(String(255))
    sharepoint_relative_path: Mapped[str | None] = mapped_column(String(1024))
    sharepoint_web_url: Mapped[str | None] = mapped_column(String(1024))
    sharepoint_share_url: Mapped[str | None] = mapped_column(String(1024))
    ia_input_json: Mapped[str | None] = mapped_column(Text)
    ia_output_json: Mapped[str | None] = mapped_column(Text)
    ia_input_relative_path: Mapped[str | None] = mapped_column(String(1024))
    ia_input_web_url: Mapped[str | None] = mapped_column(String(1024))
    ia_output_relative_path: Mapped[str | None] = mapped_column(String(1024))
    ia_output_web_url: Mapped[str | None] = mapped_column(String(1024))
    email_id: Mapped[str | None] = mapped_column(String(255))
    email_subject: Mapped[str | None] = mapped_column(String(512))
    email_sender: Mapped[str | None] = mapped_column(String(255))
    email_received_datetime: Mapped[str | None] = mapped_column(String(64))
    raw_context_json: Mapped[str | None] = mapped_column(Text)
    raw_extraction_json: Mapped[str] = mapped_column(Text, nullable=False)
    confidence_pct_calc: Mapped[float | None] = mapped_column(Float)
    review_required: Mapped[bool | None] = mapped_column(Boolean)
    review_reasons_json: Mapped[str | None] = mapped_column(Text)
    comparison_summary_json: Mapped[str | None] = mapped_column(Text)
    created_at_utc: Mapped[str] = mapped_column(String(64), nullable=False)


class _LineColumnsMixin:
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider_origin: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    line_index: Mapped[int] = mapped_column(Integer, nullable=False)
    external_line_id: Mapped[str | None] = mapped_column(String(64))
    cabecera_id: Mapped[str | None] = mapped_column(String(64))
    codigo: Mapped[str | None] = mapped_column(String(64))
    cantidad: Mapped[float | None] = mapped_column(Float)
    concepto: Mapped[str | None] = mapped_column(Text)
    # -----------------------------------------------------------------
    # Unidad de medida tal cual aparece en el albarán ('m3', 'kg',
    # 'ud', 'min', 'h', etc.). String corto. Usado por svc5 para
    # clasificar la categoría y por svc6 para convertir cantidades.
    # -----------------------------------------------------------------
    unidad_medida: Mapped[str | None] = mapped_column(String(32))
    precio: Mapped[float | None] = mapped_column(Float)
    descuento: Mapped[float | None] = mapped_column(Float)
    precio_neto: Mapped[float | None] = mapped_column(Float)
    codigo_imputacion: Mapped[str | None] = mapped_column(String(128))
    confianza_pct: Mapped[float | None] = mapped_column(Float)
    confidence_pct_calc: Mapped[float | None] = mapped_column(Float)
    line_match_score: Mapped[float | None] = mapped_column(Float)
    comparison_status_json: Mapped[str | None] = mapped_column(Text)
    field_scores_json: Mapped[str | None] = mapped_column(Text)

    # -----------------------------------------------------------------
    # Contexto estructural de la línea (familia hormigón / combustible /
    # alquiler_maquinaria / otro). Viene del OCR por el envelope y se
    # persiste serializado como JSON. Null cuando la línea no es de
    # familia compleja.
    # Estructura: {tipo_familia, rol_linea, descripcion_extendida,
    #              notas_tiempo, ref_linea_base}.
    # -----------------------------------------------------------------
    contexto_linea_json: Mapped[str | None] = mapped_column(Text)


class AlbaranDocumentOrm(_DocumentColumnsMixin, Base):
    __tablename__ = "albaran_documents"
    __table_args__ = (
        UniqueConstraint(
            "source_sha256",
            "provider_origin",
            name="uq_albaran_documents_sha_provider",
        ),
    )

    lines: Mapped[list["AlbaranLineOrm"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
    )


class AlbaranLineOrm(_LineColumnsMixin, Base):
    __tablename__ = "albaran_lines"

    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("albaran_documents.id"),
        nullable=False,
        index=True,
    )

    document: Mapped[AlbaranDocumentOrm] = relationship(back_populates="lines")


class AlbaranDocumentMergeOrm(_DocumentColumnsMixin, Base):
    __tablename__ = "albaran_documents_merge"
    __table_args__ = (
        UniqueConstraint(
            "source_sha256",
            name="uq_albaran_documents_merge_sha",
        ),
    )

    lines: Mapped[list["AlbaranLineMergeOrm"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
    )


class AlbaranLineMergeOrm(_LineColumnsMixin, Base):
    __tablename__ = "albaran_lines_merge"

    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("albaran_documents_merge.id"),
        nullable=False,
        index=True,
    )

    document: Mapped[AlbaranDocumentMergeOrm] = relationship(
        back_populates="lines"
    )


# Alias de compatibilidad con nombres anteriores.
AlbaranDocumentGemOrm = AlbaranDocumentMergeOrm
AlbaranLineGemOrm = AlbaranLineMergeOrm
