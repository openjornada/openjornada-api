"""
Helper de outbox para notificaciones en tiempo real.

`emit_notification` es la ÚNICA llamada que necesita una operación de dominio
para (a) persistir la notificación en `notifications` (outbox, fuente de
verdad para re-sincronización) y (b) publicar el evento en el bus in-process
(SSE). Es *failure-safe*: nunca lanza excepción al llamador — un fallo al
notificar no debe abortar el fichaje.
"""
import logging
from datetime import datetime, timezone
from typing import Dict, Optional

from ..database import db
from ..models.notifications import RealtimeEvent
from .event_bus import event_bus

logger = logging.getLogger(__name__)


async def emit_notification(
    *,
    event_type: str,
    company_id: str,
    payload: Dict,
    target_role: Optional[str] = None,
) -> Optional[str]:
    """Persiste la notificación y publica el evento. Devuelve el id del doc o None."""
    try:
        doc = {
            "type": event_type,
            "company_id": company_id,
            "payload": payload,
            "target_role": target_role,
            "read": False,
            "created_at": datetime.now(timezone.utc),
        }
        result = await db.notifications.insert_one(doc)
        await event_bus.publish(
            RealtimeEvent(
                type=event_type,
                payload=payload,
                notification_id=str(result.inserted_id),
                company_id=company_id,
                created_at=doc["created_at"].isoformat(),
            ),
            company_id=company_id,
        )
        return str(result.inserted_id)
    except Exception as exc:
        # Best-effort: el dominio (p.ej. el fichaje) ya se ha commitado y NO
        # debe fallar por un problema de notificación.
        logger.error(
            f"emit_notification falló (type={event_type}, company_id={company_id}): {exc!r}"
        )
        return None
