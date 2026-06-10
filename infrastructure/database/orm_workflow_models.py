# infrastructure/database/orm_workflow_models.py
from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class WorkflowRunOrm(Base):
    __tablename__ = "workflow_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    parent_workflow_id: Mapped[str | None] = mapped_column(String(36))
    document_id: Mapped[str | None] = mapped_column(String(36), index=True)
    current_state: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    attachment_sha256: Mapped[str | None] = mapped_column(
        String(64), index=True
    )
    correlation_key: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
    )
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    started_at_utc: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    updated_at_utc: Mapped[str] = mapped_column(String(64), nullable=False)
    completed_at_utc: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending_event_json: Mapped[str | None] = mapped_column(Text)


class StepHistoryOrm(Base):
    __tablename__ = "workflow_step_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workflow_run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_name: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    input_json: Mapped[str | None] = mapped_column(Text)
    output_json: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    started_at_utc: Mapped[str] = mapped_column(String(64), nullable=False)
    completed_at_utc: Mapped[str | None] = mapped_column(String(64))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
