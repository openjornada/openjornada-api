"""
In-process event bus para notificaciones en tiempo real (Fase 1).

Transporte abstracto (interfaz `publish`/`subscribe`) para poder sustituirlo
más adelante por MongoDB Change Streams o Redis Pub/Sub sin tocar llamadores.

Decision de diseño (company_id): `APIUser` no tiene `company_id`, y el
aislamiento entre tenants es arquitectónico (cada tenant = su propia pila
Docker = su propia BD Mongo = su propio proceso de API, `replicas:1` con un
solo worker de uvicorn). Por eso el bus SOLO contiene eventos del tenant
actual: `subscribe(None)` significa "todas las empresas de este tenant"
(es lo que usa el endpoint SSE para un admin `tracker`, coherente con
`view_all_time_records`). No hay fuga entre tenants porque nunca comparten
proceso ni BD.

Backpressure: colas acotadas (`max_queue_size`). Si un suscriptor lento llena
su cola se DESCARTA el evento para ese suscriptor (se registra en log): el
SSE es best-effort; la fuente de verdad es la colección `notifications`
(outbox), que el cliente re-sincroniza vía `GET /notifications` al reconectar.
"""
import asyncio
import logging
import uuid
from typing import AsyncIterator, Dict, Optional

from ..models.notifications import RealtimeEvent

logger = logging.getLogger(__name__)

# Cota de eventos en buffer por suscriptor. SSE es best-effort: si se llena,
# se descartan eventos de ese suscriptor (ver docstring del módulo).
DEFAULT_MAX_QUEUE_SIZE = 100


class Subscription:
    """Suscriptor individual con su propia cola. Iterable asíncrono."""

    def __init__(self, company_id: Optional[str], max_queue_size: int):
        self.id = uuid.uuid4().hex
        # None = comodín: recibe todos los eventos del tenant.
        self.company_id = company_id
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue_size)

    def matches(self, company_id: Optional[str]) -> bool:
        return self.company_id is None or self.company_id == company_id

    def __aiter__(self) -> AsyncIterator[RealtimeEvent]:
        return self

    async def __anext__(self) -> RealtimeEvent:
        event = await self.queue.get()
        if event is None:  # sentinel de cierre (unsubscribe)
            raise StopAsyncIteration
        return event


class EventBus:
    """Bus in-process: registro de suscriptores + cola asyncio por suscriptor."""

    def __init__(self, max_queue_size: int = DEFAULT_MAX_QUEUE_SIZE):
        self._max_queue_size = max_queue_size
        self._subscribers: Dict[str, Subscription] = {}

    def subscribe(self, company_id: Optional[str] = None) -> Subscription:
        """Registra un suscriptor. `company_id=None` = todas las empresas del tenant."""
        sub = Subscription(company_id, self._max_queue_size)
        self._subscribers[sub.id] = sub
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        """Desregistra un suscriptor (el generador SSE DEBE llamarlo en `finally`)."""
        self._subscribers.pop(sub.id, None)
        try:
            sub.queue.put_nowait(None)  # libera a quien esté iterando la cola
        except asyncio.QueueFull:
            pass

    async def publish(self, event: RealtimeEvent, company_id: Optional[str] = None) -> None:
        """Entrega el evento a cada suscriptor cuyo filtro coincida. Nunca bloquea."""
        for sub in list(self._subscribers.values()):
            if not sub.matches(company_id):
                continue
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(
                    "Cola de suscriptor SSE llena; evento descartado "
                    f"(type={event.type}, company_id={company_id}, sub={sub.id})"
                )

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


# Singleton usado por publishers (outbox) y por el endpoint SSE.
event_bus = EventBus()
