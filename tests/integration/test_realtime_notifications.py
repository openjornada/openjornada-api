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
from datetime import datetime, timezone as dt_timezone
from typing import Dict
from unittest.mock import patch

import pytest
from bson import ObjectId
from httpx import AsyncClient

import api.services.report_service as report_service_module
from api.routers.notifications import NOTIFICATIONS_LIST_LIMIT
from api.services.event_bus import event_bus
from api.services.time_calculation_service import TimeCalculationService

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
    async def test_fichaje_event_payload_includes_work_center_snapshot(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db
    ):
        """El evento realtime 'fichaje.created' lleva el snapshot del centro para
        que el listado del admin lo muestre en vivo (sin recargar)."""
        company_id = worker_id = center_id = None
        sub = None
        try:
            company_id, email, worker_id = await _create_company_and_worker(
                async_client, admin_headers, test_db, "wc"
            )

            resp = await async_client.post(
                "/api/work-centers/",
                json={"name": "Planta Sur", "company_id": company_id},
                headers=admin_headers,
            )
            assert resp.status_code == 201, resp.text
            center_id = resp.json()["id"]

            resp = await async_client.put(
                f"/api/workers/{worker_id}/work-center",
                json={"company_id": company_id, "work_center_id": center_id},
                headers=admin_headers,
            )
            assert resp.status_code == 200, resp.text

            sub = event_bus.subscribe(company_id)
            resp = await _fichaje(async_client, admin_headers, company_id, email)
            assert resp.status_code == 201, resp.text

            event = await asyncio.wait_for(sub.queue.get(), timeout=2)
            assert event.type == "fichaje.created"
            assert event.payload["work_center_id"] == center_id
            assert event.payload["work_center_name"] == "Planta Sur"

            # El payload de la notificación persistida también lo lleva.
            doc = await test_db.notifications.find_one({"company_id": company_id})
            assert doc is not None
            assert doc["payload"]["work_center_id"] == center_id
            assert doc["payload"]["work_center_name"] == "Planta Sur"
        finally:
            if sub is not None:
                event_bus.unsubscribe(sub)
            await test_db.notifications.delete_many({"company_id": company_id})
            if worker_id:
                await test_db.WorkerShiftStates.delete_many({"worker_id": worker_id})
                await test_db.TimeRecords.delete_many({"worker_id": worker_id})
                await test_db.Workers.delete_one({"_id": ObjectId(worker_id)})
            if center_id:
                await test_db.WorkCenters.delete_one({"_id": ObjectId(center_id)})
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
            # `unread_count` es global al tenant: otros tests pueden dejar
            # notificaciones no leídas en la BD compartida. Leemos la línea
            # base ANTES de insertar para que el assert no dependa del orden
            # de ejecución ni de restos de otras ejecuciones.
            resp = await async_client.get("/api/notifications", headers=admin_headers)
            assert resp.status_code == 200, resp.text
            baseline_unread = resp.json()["unread_count"]

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
            assert data["unread_count"] == baseline_unread + NOTIFICATIONS_LIST_LIMIT + 5
        finally:
            await test_db.notifications.delete_many({"company_id": company_id})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})


def _utc(year: int, month: int, day: int, hour: int = 8, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, 0, tzinfo=dt_timezone.utc)


async def _insert_exit(test_db, worker_id, company_id, worker_name, ts, duration):
    await test_db.TimeRecords.insert_one({
        "worker_id": worker_id,
        "worker_name": worker_name,
        "type": "exit",
        "timestamp": ts,
        "created_at": ts,
        "company_id": company_id,
        "recorded_by": "fichaje_event_totals_test",
        "duration_minutes": duration,
    })


def _duration(value: float):
    """Async replacement for TimeCalculationService.calculate_duration_with_pauses."""

    async def _fake(*args, **kwargs):
        return value

    return _fake


async def _cleanup(test_db, company_id, worker_id):
    if company_id:
        await test_db.notifications.delete_many({"company_id": company_id})
    if worker_id:
        await test_db.WorkerShiftStates.delete_many({"worker_id": worker_id})
        await test_db.TimeRecords.delete_many({"worker_id": worker_id})
        await test_db.Workers.delete_one({"_id": ObjectId(worker_id)})
    if company_id:
        await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
    await test_db.APIUsers.delete_one({"email": "admin@test.com"})


class TestFichajeEventTotals:
    """`fichaje.created` carries the worker's day/week/month totals.

    The listing inserts the new row from the event without refetching, so the
    total columns must be present in the payload. The totals are computed in
    the worker's `default_timezone` (fallback UTC) and are best-effort: a
    failure must not abort the already-committed fichaje.
    """

    @pytest.mark.asyncio
    async def test_exit_event_carries_period_totals(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db
    ):
        """Two exits the same day plus a month-crossing ISO week."""
        company_id = worker_id = None
        try:
            company_id, email, worker_id = await _create_company_and_worker(
                async_client, admin_headers, test_db, "evt"
            )
            await test_db.notifications.delete_many({"company_id": company_id})

            worker_name = "Realtime evt"
            # ISO week 2026-06-29 (Mon) .. 2026-07-05 (Sun) crosses June/July.
            await _insert_exit(test_db, worker_id, company_id, worker_name, _utc(2026, 6, 29), 100.0)
            await _insert_exit(test_db, worker_id, company_id, worker_name, _utc(2026, 7, 1), 50.0)
            await _insert_exit(test_db, worker_id, company_id, worker_name, _utc(2026, 7, 5), 80.0)
            # Same July month, later week (only counts towards the month total).
            await _insert_exit(test_db, worker_id, company_id, worker_name, _utc(2026, 7, 20), 200.0)

            fixed_now = _utc(2026, 7, 1, 10, 0)
            with patch("api.routers.time_records.datetime") as mock_dt, patch.object(
                TimeCalculationService, "calculate_duration_with_pauses", _duration(90.0)
            ):
                mock_dt.now.return_value = fixed_now
                entry = await _fichaje(async_client, admin_headers, company_id, email, action="entry")
                assert entry.status_code == 201, entry.text
                exit_resp = await _fichaje(async_client, admin_headers, company_id, email, action="exit")
                assert exit_resp.status_code == 201, exit_resp.text

            doc = await test_db.notifications.find_one(
                {"company_id": company_id, "payload.record_type": "exit"}
            )
            assert doc is not None, "no se publicó el evento del exit"
            payload = doc["payload"]
            # The freshly created exit (patched to 90 min) is already committed
            # and must be included in its own totals.
            assert payload["daily_total_minutes"] == 140.0    # 50 + 90
            assert payload["weekly_total_minutes"] == 320.0   # 100 + 50 + 80 + 90
            assert payload["monthly_total_minutes"] == 420.0  # 50 + 80 + 200 + 90
        finally:
            await _cleanup(test_db, company_id, worker_id)

    @pytest.mark.asyncio
    async def test_totals_use_worker_default_timezone(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db
    ):
        """A worker in Europe/Madrid gets day boundaries in local time."""
        company_id = worker_id = None
        try:
            company_id, email, worker_id = await _create_company_and_worker(
                async_client, admin_headers, test_db, "tz"
            )
            await test_db.notifications.delete_many({"company_id": company_id})
            await test_db.Workers.update_one(
                {"_id": ObjectId(worker_id)},
                {"$set": {"default_timezone": "Europe/Madrid"}},
            )

            worker_name = "Realtime tz"
            # Madrid is UTC+2: 20:00 UTC Jul 1 -> 22:00 local Jul 1;
            # 23:30 UTC Jul 1 -> 01:30 local Jul 2.
            await _insert_exit(test_db, worker_id, company_id, worker_name, _utc(2026, 7, 1, 20), 60.0)
            await _insert_exit(test_db, worker_id, company_id, worker_name, _utc(2026, 7, 1, 23, 30), 120.0)

            # 22:30 UTC -> 00:30 local Jul 2, i.e. the new exit's local day.
            fixed_now = _utc(2026, 7, 1, 22, 30)
            with patch("api.routers.time_records.datetime") as mock_dt, patch.object(
                TimeCalculationService, "calculate_duration_with_pauses", _duration(30.0)
            ):
                mock_dt.now.return_value = fixed_now
                entry = await _fichaje(async_client, admin_headers, company_id, email, action="entry")
                assert entry.status_code == 201, entry.text
                exit_resp = await _fichaje(async_client, admin_headers, company_id, email, action="exit")
                assert exit_resp.status_code == 201, exit_resp.text

            doc = await test_db.notifications.find_one(
                {"company_id": company_id, "payload.record_type": "exit"}
            )
            assert doc is not None
            payload = doc["payload"]
            # Local day = Jul 2 -> 120 (23:30 UTC) + 30. A UTC day would be
            # Jul 1 -> 60 + 30 = 90, so 150 proves the worker tz was applied.
            assert payload["daily_total_minutes"] == 150.0
            assert payload["weekly_total_minutes"] == 210.0   # 60 + 120 + 30
            assert payload["monthly_total_minutes"] == 210.0
        finally:
            await _cleanup(test_db, company_id, worker_id)

    @pytest.mark.asyncio
    async def test_totals_fall_back_to_utc_when_unset(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db
    ):
        """A worker with no `default_timezone` groups days in UTC."""
        company_id = worker_id = None
        try:
            company_id, email, worker_id = await _create_company_and_worker(
                async_client, admin_headers, test_db, "utz"
            )
            await test_db.notifications.delete_many({"company_id": company_id})
            await test_db.Workers.update_one(
                {"_id": ObjectId(worker_id)}, {"$unset": {"default_timezone": ""}}
            )

            worker_name = "Realtime utz"
            await _insert_exit(test_db, worker_id, company_id, worker_name, _utc(2026, 7, 1, 10), 60.0)
            # Next UTC day: must not count towards the Jul 1 daily total.
            await _insert_exit(test_db, worker_id, company_id, worker_name, _utc(2026, 7, 2, 1), 50.0)

            fixed_now = _utc(2026, 7, 1, 23, 30)
            with patch("api.routers.time_records.datetime") as mock_dt, patch.object(
                TimeCalculationService, "calculate_duration_with_pauses", _duration(30.0)
            ):
                mock_dt.now.return_value = fixed_now
                entry = await _fichaje(async_client, admin_headers, company_id, email, action="entry")
                assert entry.status_code == 201, entry.text
                exit_resp = await _fichaje(async_client, admin_headers, company_id, email, action="exit")
                assert exit_resp.status_code == 201, exit_resp.text

            doc = await test_db.notifications.find_one(
                {"company_id": company_id, "payload.record_type": "exit"}
            )
            assert doc is not None
            payload = doc["payload"]
            # UTC local day = Jul 1 -> 60 + 30 = 90 (the 01:00 UTC Jul 2 exit
            # is a different day). With Madrid it would be 50 + 30 = 80.
            assert payload["daily_total_minutes"] == 90.0
            assert payload["weekly_total_minutes"] == 140.0   # 60 + 50 + 30
            assert payload["monthly_total_minutes"] == 140.0
        finally:
            await _cleanup(test_db, company_id, worker_id)

    @pytest.mark.asyncio
    async def test_totals_failure_still_emits_event_with_none(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db
    ):
        """A totals error is best-effort: fichaje 201 + event with None totals."""
        company_id = worker_id = None
        try:
            company_id, email, worker_id = await _create_company_and_worker(
                async_client, admin_headers, test_db, "fail"
            )
            await test_db.notifications.delete_many({"company_id": company_id})

            async def _boom(self, *args, **kwargs):
                raise RuntimeError("aggregation down")

            with patch.object(
                report_service_module.ReportService,
                "get_exit_minutes_by_day_multi",
                _boom,
            ):
                resp = await _fichaje(async_client, admin_headers, company_id, email, action="entry")
            assert resp.status_code == 201, resp.text

            doc = await test_db.notifications.find_one(
                {"company_id": company_id, "payload.record_type": "entry"}
            )
            assert doc is not None, "el fichaje no publicó la notificación"
            payload = doc["payload"]
            assert payload["daily_total_minutes"] is None
            assert payload["weekly_total_minutes"] is None
            assert payload["monthly_total_minutes"] is None
        finally:
            await _cleanup(test_db, company_id, worker_id)
