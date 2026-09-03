"""
Unit tests del EventBus in-process (sin Mongo ni HTTP).

Cubre el filtrado por company_id (escenario 5 de add-realtime-notifications):
- subscribe("A") recibe solo eventos de A
- subscribe("B") NO recibe eventos de A
- subscribe(None) (comodín, lo del SSE admin) recibe todos los del tenant
"""
import asyncio

import pytest

from api.models.notifications import RealtimeEvent
from api.services.event_bus import EventBus


def _event(company: str) -> RealtimeEvent:
    return RealtimeEvent(type="fichaje.created", payload={"company_id": company})


class TestEventBus:

    @pytest.mark.asyncio
    async def test_publish_filters_by_company_id(self):
        bus = EventBus()
        sub_a = bus.subscribe("A")
        sub_b = bus.subscribe("B")
        sub_all = bus.subscribe(None)

        await bus.publish(_event("A"), company_id="A")

        assert sub_a.queue.qsize() == 1
        assert sub_b.queue.qsize() == 0
        assert sub_all.queue.qsize() == 1
        event = await asyncio.wait_for(sub_a.queue.get(), timeout=1)
        assert event.type == "fichaje.created"
        assert event.payload["company_id"] == "A"

    @pytest.mark.asyncio
    async def test_unsubscribe_stops_delivery_and_closes_iterator(self):
        bus = EventBus()
        sub = bus.subscribe(None)
        bus.unsubscribe(sub)

        await bus.publish(_event("A"), company_id="A")
        assert sub.queue.qsize() == 1  # solo el sentinel None del cierre

        with pytest.raises(StopAsyncIteration):
            await sub.__anext__()
        assert bus.subscriber_count == 0

    @pytest.mark.asyncio
    async def test_full_queue_drops_event_without_blocking(self):
        bus = EventBus(max_queue_size=1)
        sub = bus.subscribe("A")

        await bus.publish(_event("A"), company_id="A")
        await bus.publish(_event("A"), company_id="A")  # cola llena -> descarta

        assert sub.queue.qsize() == 1
