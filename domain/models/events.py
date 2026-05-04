# domain/models/events.py
"""Eventos que recibe sv7 vía POST /v1/events/*.

Son DTOs Pydantic. Cada uno mapea a un endpoint en interface_adapters/api/app.py.
La trazabilidad la garantiza ``correlation_key`` (en email-received) o
``document_id`` (en contract-selected y document-approved).
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class EmailReceivedEvent(BaseModel):
    """Evento desde sv1 al detectar un adjunto en el buzón.

    El binario llega en multipart aparte; este DTO viaja en la parte
    ``meta`` del multipart (parsed como JSON).
    """

    email_message_id: str = Field(..., min_length=1)
    email_received_at_utc: str
    from_address: str
    subject: Optional[str] = None
    attachment_filename: str
    attachment_sha256: str
    attachment_content_type: str
    attachment_size_bytes: int = Field(..., ge=0)
    page_number: int = Field(..., ge=1)
    total_pages: int = Field(..., ge=1)
    page_sha256: str

    @property
    def correlation_key(self) -> str:
        """Idempotencia a nivel sv7. Mismo email + misma página = mismo workflow."""
        return f"email:{self.email_message_id}:{self.page_sha256}"


class ContractSelectedEvent(BaseModel):
    """Evento desde sv4 cuando el revisor cambia/elige el contrato."""

    document_id: str
    codigo_contrato: str
    selected_by: Optional[str] = None
    selected_at_utc: str


class DocumentApprovedEvent(BaseModel):
    """Evento desde sv4 cuando el revisor aprueba el documento."""

    document_id: str
    approved_by: Optional[str] = None
    approved_at_utc: str
    review_notes: Optional[str] = None


class EmailReceivedAck(BaseModel):
    """Respuesta del POST /v1/events/email-received."""

    accepted: bool
    workflow_id: str
    correlation_key: str
    duplicate: bool
    message: str


class EventApplyResult(BaseModel):
    """Respuesta de los endpoints contract-selected y document-approved."""

    workflow_id: str
    previous_state: str
    new_state: str
    action: str  # 'transition_applied' | 'spawned_revaluation' | 'event_queued' | 'no_op'
    detail: Optional[str] = None
