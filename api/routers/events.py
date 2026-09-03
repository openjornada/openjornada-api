"""
SSE: GET /events/stream — eventos en tiempo real del tenant.

Auth: Bearer JWT + permiso `view_all_time_records` (solo rol `admin`):
401 si falta/inválido/expira el token, 403 si el rol no tiene el permiso.
Ambos se resuelven ANTES de abrir el stream.

Por qué el stream NO se filtra a una única company_id: `APIUser` no tiene
empresa y el aislamiento entre tenants es arquitectónico (cada tenant tiene
su propia pila Docker, su propia BD Mongo y su propio proceso de API con
1 worker). El admin (único rol con `view_all_time_records`) ve todas las
empresas de su tenant en `GET /time-records/`, así que el stream usa
`subscribe(None)` = "todas las empresas de ESTE tenant". No es posible la
fuga entre tenants porque nunca comparten proceso ni bus.
"""
import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ..auth.permissions import PermissionChecker
from ..models.auth import APIUser
from ..services.event_bus import event_bus

router = APIRouter()

# Segundos entre heartbeats `:keep-alive` (comentario SSE: mantiene viva la
# conexión y evita que proxies cierren streams silenciosos).
HEARTBEAT_SECONDS = 15


@router.get("/events/stream")
async def event_stream(
    request: Request,
    current_user: APIUser = Depends(PermissionChecker("view_all_time_records")),
):
    """Server-Sent Events: `data: {"type":..., "payload":...}\\n\\n` por evento."""

    async def generator():
        sub = event_bus.subscribe(None)  # todo el tenant
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(sub.queue.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ":keep-alive\n\n"
                    continue
                if event is None:  # bus cerró la suscripción
                    break
                yield f"data: {event.model_dump_json(exclude_none=True)}\n\n"
        finally:
            event_bus.unsubscribe(sub)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # evita buffering en Nginx/Caddy
        },
    )
