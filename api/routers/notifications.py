"""
Notificaciones (outbox): GET /notifications y POST /notifications/mark-read.

Auth: permiso `view_all_time_records` (solo rol `admin`); 401 si falta/inválido
el token, 403 si el rol no tiene el permiso. Igual que el stream SSE, el
alcance es el tenant completo (cada tenant = su propia BD/proceso): los admins
ven todas las notificaciones de su tenant, coherente con
`view_all_time_records`. `mark-read` consulta por `_id` DENTRO de la BD del
tenant: es imposible marcar una notificación de otro tenant; los ids
inexistentes o inválidos se ignoran.
"""
import logging
from datetime import datetime, timezone
from typing import List, Optional

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, Query

from ..auth.permissions import PermissionChecker
from ..database import convert_id, db
from ..models.auth import APIUser
from ..models.notifications import (
    MarkReadRequest,
    MarkReadResponse,
    NotificationDocument,
    NotificationListResponse,
)

router = APIRouter()
logger = logging.getLogger(__name__)

# Máximo de notificaciones devueltas por `GET /notifications` (las más
# recientes). La lista está acotada; el contador de no leídas NO.
NOTIFICATIONS_LIST_LIMIT = 50


def _to_document(doc: dict) -> NotificationDocument:
    """Doc Mongo -> modelo. MongoDB devuelve datetimes naive (UTC)."""
    data = convert_id(doc)
    created_at = data.get("created_at")
    if isinstance(created_at, datetime) and created_at.tzinfo is None:
        data["created_at"] = created_at.replace(tzinfo=timezone.utc)
    return NotificationDocument(**data)


@router.get("/notifications", response_model=NotificationListResponse)
async def list_notifications(
    unread: Optional[bool] = Query(None, description="Si es true, solo no leídas"),
    current_user: APIUser = Depends(PermissionChecker("view_all_time_records")),
):
    """Notificaciones del tenant (últimas N), ordenadas por created_at desc, + contador de no leídas."""
    query = {"read": False} if unread else {}
    items: List[NotificationDocument] = []
    async for doc in db.notifications.find(query).sort("created_at", -1).limit(NOTIFICATIONS_LIST_LIMIT):
        items.append(_to_document(doc))

    unread_count = await db.notifications.count_documents({"read": False})
    return NotificationListResponse(items=items, unread_count=unread_count)


@router.post("/notifications/mark-read", response_model=MarkReadResponse)
async def mark_read(
    body: MarkReadRequest,
    current_user: APIUser = Depends(PermissionChecker("view_all_time_records")),
):
    """Marca como leídas las notificaciones indicadas (ids válidos y existentes en este tenant)."""
    object_ids = []
    for raw_id in body.ids:
        try:
            object_ids.append(ObjectId(raw_id))
        except InvalidId:
            logger.warning(f"mark-read: id ignorado (no es un ObjectId válido): {raw_id}")

    if not object_ids:
        return MarkReadResponse(updated=0)

    result = await db.notifications.update_many(
        {"_id": {"$in": object_ids}},
        {"$set": {"read": True}},
    )
    return MarkReadResponse(updated=result.modified_count)
