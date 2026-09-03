"""
Integración: notificaciones en tiempo real (add-realtime-notifications, Fase 1 API).

Cubre (escenarios del change):
1. POST /api/time-records/ persiste el doc en `notifications` Y publica en el bus
   (evento enriquecido: notification_id, company_id, created_at).
2. El fichaje tiene éxito sin suscriptores conectados (emisión segura).
3. GET /api/notifications → unread_count correcto; ?unread=true filtra;
   lista acotada a las últimas NOTIFICATIONS_LIST_LIMIT.
4. POST /api/notifications/mark-read decrementa unread_count.
5. Auth: sin token → 401 en /events/stream; rol `tracker` (sin
   `view_all_time_records`) → 403 en stream, listado y mark-read.
6. GET /api/events/stream sin Authorization → 401 (antes de abrir el stream).

El aislamiento por empresa del bus se prueba en tests/unit/test_event_bus.py;
el aislamiento cross-tenant es arquitectónico (BD/proceso por tenant).
"""
import asyncio
from typing import Dict

import pytest
from bson import ObjectId
from httpx import AsyncClient

from api.routers.notifications import NOTIFICATIONS_LIST_LIMIT
from api.services.event_bus import event_bus

PASSWORD = "RealtimePass123!"
TRACKER_PASSWORD = "TrackerPass123!"


async def _create_tracker_headers(client: AsyncClient, db) -> Dict[str, str]:
    """Crea un APIUser con role=tracker (sin view_all_time_records) y devuelve
    sus headers de autorización, siguiendo el patrón del fixture admin_token."""
    from api.auth.auth_handler import get_password_hash
    from datetime import datetime, timezone

    tracker_email = "tracker@test.com"
    await db.APIUsers.delete_one({"email": tracker_email})
    await db.APIUsers.insert_one({
        "username": "tracker_test",
        "email": tracker_email,
        "hashed_password": get_password_hash(TRACKER_PASSWORD),
        "role": "tracker",
        "is_active": True,
        "created_at": datetime.now(timezone.utc),
    })
    resp = await client.post(
        "/api/token",
        data={"username": tracker_email, "password": TRACKER_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _create_company_and_worker(
    client: AsyncClient, headers: Dict[str, str], db, tag: str
) -> tuple:
    """Crea empresa + worker vía API (igual que test_time_records_credentials_contract)."""
    resp = await client.post(
        "/api/companies/", json={"name": f"RT Company {tag}"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    company_id = resp.json()["id"]

    email = f"rt.{tag}@test.com"
    resp = await client.post(
        "/api/workers/",
        json={
            "first_name": "Realtime",
            "last_name": tag,
            "email": email,
            "phone_number": "+34611000001",
            "id_number": f"9000000{len(tag)}X",
            "password": PASSWORD,
            "company_ids": [company_id],
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return company_id, email, resp.json()["id"]


async def _fichaje(client: AsyncClient, headers, company_id: str, email: str, action="entry"):
    return await client.post(
        "/api/time-records/",
        json={"email": email, "password": PASSWORD, "company_id": company_id, "action": action},
        headers=headers,
    )


class TestRealtimeNotifications:

    @pytest.mark.asyncio
    async def test_fichaje_persists_notification_and_publishes_event(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db
    ):
        company_id = worker_id = None
        sub = None
        try:
            company_id, email, worker_id = await _create_company_and_worker(
                async_client, admin_headers, test_db, "pub"
            )
            await test_db.notifications.delete_many({"company_id": company_id})

            # Nos suscribimos ANTES del fichaje para capturar el evento del bus.
            sub = event_bus.subscribe(company_id)

            resp = await _fichaje(async_client, admin_headers, company_id, email)
            assert resp.status_code == 201, resp.text

            # (a) outbox persistido
            doc = await test_db.notifications.find_one({"company_id": company_id})
            assert doc is not None, "el fichaje no creó la notificación"
            assert doc["type"] == "fichaje.created"
            assert doc["read"] is False
            assert doc["payload"]["worker_id"] == worker_id
            assert doc["payload"]["record_type"] == "entry"
            assert doc["payload"]["company_id"] == company_id

            # (b) publicado en el bus, con metadatos para que la campana no
            # necesite re-fetch (contrato compartido con el frontend).
            event = await asyncio.wait_for(sub.queue.get(), timeout=2)
            assert event.type == "fichaje.created"
            assert event.payload["worker_id"] == worker_id
            assert event.payload["company_name"] == "RT Company pub"
            assert event.payload["company_id"] == company_id
            assert event.notification_id, "falta notification_id en el evento SSE"
            assert event.created_at, "falta created_at en el evento SSE"
            assert event.company_id == company_id
        finally:
            if sub is not None:
                event_bus.unsubscribe(sub)
            await test_db.notifications.delete_many({"company_id": company_id})
            if worker_id:
                await test_db.WorkerShiftStates.delete_many({"worker_id": worker_id})
                await test_db.TimeRecords.delete_many({"worker_id": worker_id})
                await test_db.Workers.delete_one({"_id": ObjectId(worker_id)})
            if company_id:
                await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_fichaje_succeeds_without_subscribers(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db
    ):
        """Sin ningún suscriptor en el bus, el dominio no se ve afectado."""
        company_id = worker_id = None
        try:
            company_id, email, worker_id = await _create_company_and_worker(
                async_client, admin_headers, test_db, "nosub"
            )
            await test_db.notifications.delete_many({"company_id": company_id})

            resp = await _fichaje(async_client, admin_headers, company_id, email)
            assert resp.status_code == 201, resp.text

            count = await test_db.notifications.count_documents({"company_id": company_id})
            assert count == 1
        finally:
            await test_db.notifications.delete_many({"company_id": company_id})
            if worker_id:
                await test_db.WorkerShiftStates.delete_many({"worker_id": worker_id})
                await test_db.TimeRecords.delete_many({"worker_id": worker_id})
                await test_db.Workers.delete_one({"_id": ObjectId(worker_id)})
            if company_id:
                await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_list_and_mark_read(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db
    ):
        company_id = worker_id = None
        try:
            company_id, email, worker_id = await _create_company_and_worker(
                async_client, admin_headers, test_db, "list"
            )
            await test_db.notifications.delete_many({"company_id": company_id})

            for _ in range(2):
                resp = await _fichaje(async_client, admin_headers, company_id, email,
                                      action="entry" if _ == 0 else "exit")
                assert resp.status_code == 201, resp.text

            resp = await async_client.get("/api/notifications", headers=admin_headers)
            assert resp.status_code == 200, resp.text
            data = resp.json()
            mine = [i for i in data["items"] if i["company_id"] == company_id]
            assert len(mine) == 2
            unread_before = data["unread_count"]
            assert unread_before >= 2
            # orden created_at desc
            dates = [i["created_at"] for i in data["items"]]
            assert dates == sorted(dates, reverse=True)

            # marcar la primera como leída → unread_count baja
            target = mine[0]
            resp = await async_client.post(
                "/api/notifications/mark-read",
                json={"ids": [target["id"]]},
                headers=admin_headers,
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["updated"] == 1

            resp = await async_client.get("/api/notifications", headers=admin_headers)
            data = resp.json()
            assert data["unread_count"] == unread_before - 1
            mine_after = [i for i in data["items"] if i["company_id"] == company_id]
            read_flags = {i["id"]: i["read"] for i in mine_after}
            assert read_flags[target["id"]] is True

            # ?unread=true solo devuelve no leídas
            resp = await async_client.get(
                "/api/notifications?unread=true", headers=admin_headers
            )
            assert resp.status_code == 200
            unread_items = resp.json()["items"]
            assert all(i["read"] is False for i in unread_items)
            assert target["id"] not in [i["id"] for i in unread_items]

            # id inexistente / inválido → ignorado, updated=0
            resp = await async_client.post(
                "/api/notifications/mark-read",
                json={"ids": ["507f1f77bcf86cd799439011", "not-an-objectid"]},
                headers=admin_headers,
            )
            assert resp.status_code == 200
            assert resp.json()["updated"] == 0
        finally:
            await test_db.notifications.delete_many({"company_id": company_id})
            if worker_id:
                await test_db.WorkerShiftStates.delete_many({"worker_id": worker_id})
                await test_db.TimeRecords.delete_many({"worker_id": worker_id})
                await test_db.Workers.delete_one({"_id": ObjectId(worker_id)})
            if company_id:
                await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_events_stream_requires_auth(self, async_client: AsyncClient):
        """Sin Authorization → 401 (se devuelve antes de abrir el stream SSE)."""
        resp = await async_client.get("/api/events/stream")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_tracker_forbidden_on_realtime_endpoints(
        self, async_client: AsyncClient, test_db
    ):
        """Rol `tracker` (sin `view_all_time_records`) → 403 en los tres endpoints,
        resuelto por la dependencia ANTES de abrir el stream / tocar la BD."""
        tracker_headers = await _create_tracker_headers(async_client, test_db)
        try:
            resp = await async_client.get(
                "/api/events/stream", headers=tracker_headers
            )
            assert resp.status_code == 403, resp.text

            resp = await async_client.get(
                "/api/notifications", headers=tracker_headers
            )
            assert resp.status_code == 403, resp.text

            resp = await async_client.post(
                "/api/notifications/mark-read",
                json={"ids": ["507f1f77bcf86cd799439011"]},
                headers=tracker_headers,
            )
            assert resp.status_code == 403, resp.text
        finally:
            await test_db.APIUsers.delete_one({"email": "tracker@test.com"})

    @pytest.mark.asyncio
    async def test_list_notifications_is_limited_but_count_is_not(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db
    ):
        """GET /notifications devuelve como mucho las últimas
        NOTIFICATIONS_LIST_LIMIT, pero unread_count sigue siendo total."""
        from datetime import datetime, timedelta, timezone

        company_id = "limit-test-company"
        now = datetime.now(timezone.utc)
        try:
            await test_db.notifications.delete_many({"company_id": company_id})
            await test_db.notifications.insert_many([
                {
                    "type": "fichaje.created",
                    "company_id": company_id,
                    "payload": {"n": i},
                    "target_role": None,
                    "read": False,
                    # creada en el futuro relativo para que sea lo más reciente
                    "created_at": now + timedelta(seconds=i),
                }
                for i in range(NOTIFICATIONS_LIST_LIMIT + 5)
            ])

            resp = await async_client.get("/api/notifications", headers=admin_headers)
            assert resp.status_code == 200, resp.text
            data = resp.json()
            mine = [i for i in data["items"] if i["company_id"] == company_id]
            assert len(data["items"]) == NOTIFICATIONS_LIST_LIMIT
            assert len(mine) == NOTIFICATIONS_LIST_LIMIT
            dates = [i["created_at"] for i in data["items"]]
            assert dates == sorted(dates, reverse=True)
            assert data["unread_count"] == NOTIFICATIONS_LIST_LIMIT + 5
        finally:
            await test_db.notifications.delete_many({"company_id": company_id})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})
