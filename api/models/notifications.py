"""Modelos Pydantic para notificaciones y eventos en tiempo real (Fase 1).

Contrato en el cable (compartido con el frontend):
    { "type": string, "payload": object, "notification_id"?: string,
      "company_id"?: string, "created_at"?: string }   — p.ej. type = "fichaje.created"
"""
from pydantic import BaseModel, AwareDatetime
from typing import Dict, List, Optional


class RealtimeEvent(BaseModel):
    """Evento tal y como viaja por SSE (`data: {...}`)."""
    type: str
    payload: Dict
    notification_id: Optional[str] = None
    company_id: Optional[str] = None
    created_at: Optional[str] = None  # ISO string


class NotificationDocument(BaseModel):
    """Documento de la colección `notifications` (outbox persistido en Mongo)."""
    id: Optional[str] = None
    type: str
    company_id: str
    payload: Dict
    target_role: Optional[str] = None
    read: bool = False
    created_at: Optional[AwareDatetime] = None


class MarkReadRequest(BaseModel):
    """Ids de notificaciones a marcar como leídas (uno o varios)."""
    ids: List[str]


class MarkReadResponse(BaseModel):
    updated: int


class NotificationListResponse(BaseModel):
    items: List[NotificationDocument]
    unread_count: int
