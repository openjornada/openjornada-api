"""
Integration tests for the shift-planning capability (routers, gating, publish
flow, worker view, emails and worker lifecycle).
"""
import uuid
from datetime import datetime, timezone as dt_timezone
from typing import Dict
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

WORKER_PASSWORD = "WorkerPass123!"
INSPECTOR_PASSWORD = "InspectorPass123!"
D1, D2 = "2099-03-02", "2099-03-03"  # consecutive days
RANGE = {"start_date": "2099-03-02", "end_date": "2099-03-08"}


def _uid() -> str:
    return uuid.uuid4().hex[:8]


async def _company(client: AsyncClient, headers, enabled: bool = True, **extra) -> str:
    resp = await client.post("/api/companies/", json={"name": f"Co {_uid()}"}, headers=headers)
    assert resp.status_code == 201, resp.text
    cid = resp.json()["id"]
    assert resp.json()["shift_planning_enabled"] is False  # default
    if enabled or extra:
        resp = await client.patch(
            f"/api/companies/{cid}",
            json={"shift_planning_enabled": enabled, **extra},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["shift_planning_enabled"] is enabled
    return cid


async def _worker(client: AsyncClient, headers, company_ids: list) -> dict:
    uid = _uid()
    email = f"w{uid}@test.com"
    resp = await client.post(
        "/api/workers/",
        json={
            "first_name": "Ana",
            "last_name": "Turnos",
            "email": email,
            "phone_number": "+34600000099",
            "id_number": f"ID{uid}",
            "password": WORKER_PASSWORD,
            "company_ids": company_ids,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return {"id": resp.json()["id"], "email": email}


async def _inspector_headers(client: AsyncClient, db) -> Dict[str, str]:
    from api.auth.auth_handler import get_password_hash

    email = "inspector_sp@test.com"
    await db.APIUsers.delete_one({"email": email})
    await db.APIUsers.insert_one({
        "username": "inspector_sp",
        "email": email,
        "hashed_password": get_password_hash(INSPECTOR_PASSWORD),
        "role": "inspector",
        "is_active": True,
        "created_at": datetime.now(dt_timezone.utc),
    })
    resp = await client.post(
        "/api/token",
        data={"username": email, "password": INSPECTOR_PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _type(client, headers, cid, code="M", start="06:00", end="14:00") -> dict:
    resp = await client.post(
        "/api/shift-types",
        json={"company_id": cid, "name": f"Tipo {code}", "short_code": code,
              "color": "#112233", "start_time": start, "end_time": end, "break_minutes": 30},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _cell(client, headers, cid, wid, day, shifts):
    return await client.put(
        "/api/shift-plans/cell",
        json={"company_id": cid, "worker_id": wid, "date": day, "shifts": shifts},
        headers=headers,
    )


async def _me(client, headers, cid, email, password=WORKER_PASSWORD, **rng):
    return await client.post(
        "/api/shift-plans/me",
        json={"company_id": cid, "email": email, "password": password, **(rng or RANGE)},
        headers=headers,
    )


async def _publish(client, headers, cid, **kw):
    return await client.post(
        "/api/shift-plans/publish",
        json={"company_id": cid, **RANGE, **kw},
        headers=headers,
    )


async def _pending_dates(client, headers, cid, wid):
    resp = await client.get(
        f"/api/shift-plans?company_id={cid}&start_date={RANGE['start_date']}&end_date={RANGE['end_date']}",
        headers=headers,
    )
    return next(w for w in resp.json()["workers"] if w["id"] == wid)["pending_dates"]


def _error_code(resp) -> str:
    return resp.json()["detail"]["error_code"]


class TestFullFlow:
    async def test_draft_publish_modify_republish(self, async_client, admin_headers):
        cid = await _company(async_client, admin_headers)
        worker = await _worker(async_client, admin_headers, [cid])
        wid = worker["id"]
        morning = await _type(async_client, admin_headers, cid, "M", "06:00", "14:00")
        evening = await _type(async_client, admin_headers, cid, "T", "14:00", "22:00")

        # Types: list + response shape
        resp = await async_client.get(f"/api/shift-types?company_id={cid}", headers=admin_headers)
        assert resp.status_code == 200 and len(resp.json()) == 2
        assert set(morning) >= {"id", "company_id", "name", "short_code", "color",
                                "start_time", "end_time", "break_minutes"}

        # Evening on D1 then morning on D2 -> 8 h rest -> rest_under_12h on D2
        resp = await _cell(async_client, admin_headers, cid, wid, D1, [{"shift_type_id": evening["id"]}])
        assert resp.status_code == 200, resp.text
        assert resp.json()[0]["short_code"] == "T"
        resp = await _cell(async_client, admin_headers, cid, wid, D2, [{"shift_type_id": morning["id"]}])
        assert resp.status_code == 200

        resp = await async_client.get(
            f"/api/shift-plans?company_id={cid}&start_date={RANGE['start_date']}&end_date={RANGE['end_date']}",
            headers=admin_headers,
        )
        roster = resp.json()
        assert roster["pending_changes"] == 2
        assert [(w["worker_id"], w["date"], w["code"]) for w in roster["warnings"]] == [
            (wid, D2, "rest_under_12h")
        ]
        row = next(w for w in roster["workers"] if w["id"] == wid)
        assert row["planned_minutes"] == (480 - 30) * 2
        assert set(row["cells"]) == {D1, D2}

        # Worker sees nothing yet (drafts only)
        resp = await _me(async_client, admin_headers, cid, worker["email"])
        assert resp.status_code == 200 and resp.json()["days"] == []

        # Publish without ack -> 409
        resp = await _publish(async_client, admin_headers, cid)
        assert resp.status_code == 409
        assert _error_code(resp) == "shift_plan.warnings_not_acknowledged"

        # Publish with ack
        with patch(
            "api.routers.shift_plans.EmailService.send_shift_plan_published_email",
            new_callable=AsyncMock,
        ) as send:
            resp = await _publish(async_client, admin_headers, cid, acknowledge_warnings=True)
        assert resp.status_code == 200, resp.text
        assert resp.json()["changed_cells"] == 2 and resp.json()["publication_id"]
        assert send.await_count == 1

        resp = await _me(async_client, admin_headers, cid, worker["email"])
        days = resp.json()["days"]
        assert [d["date"] for d in days] == [D1, D2]
        assert days[0]["shifts"][0]["short_code"] == "T"

        assert await _pending_dates(async_client, admin_headers, cid, wid) == []
        # Modify D2 draft -> worker still sees the old version
        resp = await _cell(async_client, admin_headers, cid, wid, D2,
                           [{"start_time": "08:00", "end_time": "16:00"}])
        assert resp.status_code == 200
        days = (await _me(async_client, admin_headers, cid, worker["email"])).json()["days"]
        assert days[1]["shifts"][0]["start_time"] == "06:00"
        assert await _pending_dates(async_client, admin_headers, cid, wid) == [D2]

        # Republish -> new version
        with patch("api.routers.shift_plans.EmailService.send_shift_plan_published_email",
                   new_callable=AsyncMock):
            resp = await _publish(async_client, admin_headers, cid, acknowledge_warnings=True)
        assert resp.json()["changed_cells"] == 1
        days = (await _me(async_client, admin_headers, cid, worker["email"])).json()["days"]
        assert days[1]["shifts"][0]["start_time"] == "08:00"
        assert await _pending_dates(async_client, admin_headers, cid, wid) == []

    async def test_removed_shift_visible_until_republish_and_drafts_hidden(
        self, async_client, admin_headers
    ):
        cid = await _company(async_client, admin_headers)
        worker = await _worker(async_client, admin_headers, [cid])
        wid = worker["id"]
        custom = [{"start_time": "09:00", "end_time": "17:00"}]
        await _cell(async_client, admin_headers, cid, wid, D1, custom)
        with patch("api.routers.shift_plans.EmailService.send_shift_plan_published_email",
                   new_callable=AsyncMock):
            await _publish(async_client, admin_headers, cid, notify=False)
        # Remove published shift, add a draft-only one on D2
        await _cell(async_client, admin_headers, cid, wid, D1, [])
        await _cell(async_client, admin_headers, cid, wid, D2, custom)

        days = (await _me(async_client, admin_headers, cid, worker["email"])).json()["days"]
        assert [d["date"] for d in days] == [D1]  # removed still shown, draft-only hidden
        assert days[0]["shifts"][0]["start_time"] == "09:00"

        with patch("api.routers.shift_plans.EmailService.send_shift_plan_published_email",
                   new_callable=AsyncMock):
            await _publish(async_client, admin_headers, cid, notify=False)
        days = (await _me(async_client, admin_headers, cid, worker["email"])).json()["days"]
        assert [d["date"] for d in days] == [D2]


class TestPermissionsAndGating:
    async def test_inspector_rejected(self, async_client, admin_headers, test_db):
        cid = await _company(async_client, admin_headers)
        headers = await _inspector_headers(async_client, test_db)
        resp = await async_client.get(f"/api/shift-types?company_id={cid}", headers=headers)
        assert resp.status_code == 403
        resp = await async_client.get(
            f"/api/shift-plans?company_id={cid}&start_date=2099-03-02&end_date=2099-03-08", headers=headers
        )
        assert resp.status_code == 403
        assert (await _publish(async_client, headers, cid)).status_code == 403
        assert (await _me(async_client, headers, cid, "x@y.com")).status_code == 403

    async def test_nonexistent_company_404(self, async_client, admin_headers):
        fake = "0" * 24
        resp = await async_client.get(f"/api/shift-types?company_id={fake}", headers=admin_headers)
        assert resp.status_code == 404
        assert _error_code(resp) == "shift_plan.company_not_found"
        resp = await _publish(async_client, admin_headers, fake)
        assert resp.status_code == 404 and _error_code(resp) == "shift_plan.company_not_found"

    async def test_module_disabled_403_on_every_endpoint(self, async_client, admin_headers):
        cid = await _company(async_client, admin_headers, enabled=False)
        worker = await _worker(async_client, admin_headers, [cid])
        tid = "0" * 24
        q = f"company_id={cid}"
        calls = [
            async_client.get(f"/api/shift-types?{q}", headers=admin_headers),
            async_client.post("/api/shift-types", headers=admin_headers, json={
                "company_id": cid, "name": "x", "short_code": "X", "color": "#112233",
                "start_time": "06:00", "end_time": "14:00"}),
            async_client.put(f"/api/shift-types/{tid}?{q}", json={"name": "y"}, headers=admin_headers),
            async_client.delete(f"/api/shift-types/{tid}?{q}", headers=admin_headers),
            async_client.get(f"/api/shift-plans?{q}&start_date=2099-03-02&end_date=2099-03-08",
                             headers=admin_headers),
            _cell(async_client, admin_headers, cid, worker["id"], D1, []),
            _publish(async_client, admin_headers, cid),
            async_client.post("/api/shift-plans/copy-week", headers=admin_headers, json={
                "company_id": cid, "source_week_start": "2099-03-02", "target_week_start": "2099-03-09"}),
            _me(async_client, admin_headers, cid, worker["email"]),
        ]
        for call in calls:
            resp = await call
            assert resp.status_code == 403, resp.text
            assert _error_code(resp) == "shift_plan.module_disabled"

    async def test_me_auth_errors(self, async_client, admin_headers):
        cid = await _company(async_client, admin_headers)
        other = await _company(async_client, admin_headers)
        worker = await _worker(async_client, admin_headers, [cid])
        resp = await _me(async_client, admin_headers, cid, worker["email"], password="wrong-pass")
        assert resp.status_code == 401 and _error_code(resp) == "worker.invalid_credentials"
        resp = await _me(async_client, admin_headers, other, worker["email"])
        assert resp.status_code == 403 and _error_code(resp) == "worker.company_access_denied"

    async def test_validation_errors(self, async_client, admin_headers):
        cid = await _company(async_client, admin_headers)
        worker = await _worker(async_client, admin_headers, [cid])
        resp = await async_client.get(
            f"/api/shift-plans?company_id={cid}&start_date=2099-01-01&end_date=2099-03-01",
            headers=admin_headers,
        )
        assert resp.status_code == 422 and _error_code(resp) == "shift_plan.range_too_long"
        shifts = [{"start_time": "08:00", "end_time": "16:00"}, {"start_time": "15:00", "end_time": "20:00"}]
        resp = await _cell(async_client, admin_headers, cid, worker["id"], D1, shifts)
        assert resp.status_code == 409 and _error_code(resp) == "shift_plan.overlap"
        resp = await _cell(async_client, admin_headers, cid, "0" * 24, D1, [])
        assert resp.status_code == 404 and _error_code(resp) == "shift_plan.worker_not_found"

    async def test_copy_week(self, async_client, admin_headers):
        cid = await _company(async_client, admin_headers)
        worker = await _worker(async_client, admin_headers, [cid])
        await _cell(async_client, admin_headers, cid, worker["id"], D1,
                    [{"start_time": "08:00", "end_time": "16:00"}])
        resp = await async_client.post("/api/shift-plans/copy-week", headers=admin_headers, json={
            "company_id": cid, "source_week_start": D1, "target_week_start": "2099-03-09",
            "conflict": "skip"})
        assert resp.status_code == 200 and resp.json() == {"copied": 1, "skipped": 0}
        resp = await async_client.post("/api/shift-plans/copy-week", headers=admin_headers, json={
            "company_id": cid, "source_week_start": D1, "target_week_start": "2099-03-09",
            "conflict": "skip"})
        assert resp.json() == {"copied": 0, "skipped": 1}


def _sh(start, end):
    return [{"start_time": start, "end_time": end}]


class TestOverlapAcrossDays:
    async def test_night_shift_into_next_day_cell_rejected(self, async_client, admin_headers):
        cid = await _company(async_client, admin_headers)
        wid = (await _worker(async_client, admin_headers, [cid]))["id"]
        resp = await _cell(async_client, admin_headers, cid, wid, D2, _sh("05:00", "13:00"))
        assert resp.status_code == 200
        resp = await _cell(async_client, admin_headers, cid, wid, D1, _sh("22:00", "06:00"))
        assert resp.status_code == 409 and _error_code(resp) == "shift_plan.overlap"


async def _copy(client, headers, cid, source, target, conflict):
    return await client.post("/api/shift-plans/copy-week", headers=headers, json={
        "company_id": cid, "source_week_start": source, "target_week_start": target,
        "conflict": conflict})


class TestCopyWeekOverwrite:
    T_MON, T_TUE, T_SUN, N_MON = "2099-03-09", "2099-03-10", "2099-03-15", "2099-03-16"

    async def test_overwrite_clears_target_cells_empty_in_source(self, async_client, admin_headers, test_db):
        cid = await _company(async_client, admin_headers)
        wid = (await _worker(async_client, admin_headers, [cid]))["id"]
        await _cell(async_client, admin_headers, cid, wid, D1, _sh("08:00", "16:00"))
        await _cell(async_client, admin_headers, cid, wid, self.T_MON, _sh("09:00", "17:00"))
        await _cell(async_client, admin_headers, cid, wid, self.T_TUE, _sh("09:00", "17:00"))
        resp = await _copy(async_client, admin_headers, cid, D1, self.T_MON, "overwrite")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"copied": 2, "skipped": 0}  # 1 replaced + 1 cleared
        cells = {d["date"]: d["shifts"] async for d in test_db.PlannedShifts.find({"worker_id": wid})}
        assert cells[self.T_MON][0]["start_time"] == "08:00"
        assert self.T_TUE not in cells  # never published -> deleted
        # skip mode leaves target cells that have no source untouched
        await _cell(async_client, admin_headers, cid, wid, self.T_TUE, _sh("09:00", "17:00"))
        resp = await _copy(async_client, admin_headers, cid, D1, self.T_MON, "skip")
        assert resp.json() == {"copied": 0, "skipped": 1}
        assert await test_db.PlannedShifts.count_documents({"worker_id": wid, "date": self.T_TUE}) == 1

    async def test_overwrite_valid_only_at_the_end_succeeds(self, async_client, admin_headers, test_db):
        cid = await _company(async_client, admin_headers)
        wid = (await _worker(async_client, admin_headers, [cid]))["id"]
        await _cell(async_client, admin_headers, cid, wid, D1, _sh("22:00", "06:00"))
        await _cell(async_client, admin_headers, cid, wid, self.T_TUE, _sh("05:00", "13:00"))
        resp = await _copy(async_client, admin_headers, cid, D1, self.T_MON, "overwrite")
        assert resp.status_code == 200, resp.text
        assert await test_db.PlannedShifts.count_documents({"worker_id": wid, "date": self.T_TUE}) == 0
        assert await test_db.PlannedShifts.count_documents({"worker_id": wid, "date": self.T_MON}) == 1

    async def test_overlap_with_following_week_writes_nothing(self, async_client, admin_headers, test_db):
        cid = await _company(async_client, admin_headers)
        wid = (await _worker(async_client, admin_headers, [cid]))["id"]
        await _cell(async_client, admin_headers, cid, wid, D1, _sh("08:00", "16:00"))
        await _cell(async_client, admin_headers, cid, wid, "2099-03-08", _sh("22:00", "06:00"))
        await _cell(async_client, admin_headers, cid, wid, self.N_MON, _sh("05:00", "13:00"))
        before = await test_db.PlannedShifts.count_documents({"worker_id": wid})
        resp = await _copy(async_client, admin_headers, cid, D1, self.T_MON, "overwrite")
        assert resp.status_code == 409 and _error_code(resp) == "shift_plan.overlap"
        assert await test_db.PlannedShifts.count_documents({"worker_id": wid}) == before


class TestPublicationEmails:
    async def test_email_lists_all_published_shifts_with_company_locale(
        self, async_client, admin_headers, test_db
    ):
        cid = await _company(async_client, admin_headers, notification_language="ca")
        worker = await _worker(async_client, admin_headers, [cid])
        wid = worker["id"]
        await _cell(async_client, admin_headers, cid, wid, D1, [{"start_time": "08:00", "end_time": "16:00"}])
        await _cell(async_client, admin_headers, cid, wid, D2, [{"start_time": "08:00", "end_time": "16:00"}])
        with patch("api.routers.shift_plans.EmailService.send_shift_plan_published_email",
                   new_callable=AsyncMock) as send:
            await _publish(async_client, admin_headers, cid)
            assert send.await_count == 1
            # Change only D2: email must still list D1 and D2 (published shifts)
            await _cell(async_client, admin_headers, cid, wid, D2, [{"start_time": "09:00", "end_time": "17:00"}])
            await _publish(async_client, admin_headers, cid)
        kwargs = send.await_args.kwargs
        assert kwargs["locale"] == "ca"
        assert kwargs["to_email"] == worker["email"]
        assert [d["date"] for d in kwargs["days"]] == [D1, D2]
        assert kwargs["days"][1]["shifts"][0]["start_time"] == "09:00"

    async def test_smtp_failure_does_not_undo_publish(self, async_client, admin_headers, test_db):
        cid = await _company(async_client, admin_headers)
        worker = await _worker(async_client, admin_headers, [cid])
        await _cell(async_client, admin_headers, cid, worker["id"], D1,
                    [{"start_time": "08:00", "end_time": "16:00"}])
        with patch("api.services.email_service.EmailService._send_email_sync",
                   side_effect=RuntimeError("smtp down")):
            resp = await _publish(async_client, admin_headers, cid)
        assert resp.status_code == 200
        assert (await _me(async_client, admin_headers, cid, worker["email"])).json()["days"]
        pub = await test_db.ShiftPlanPublications.find_one({"company_id": cid})
        assert pub["notified"] is True

    async def test_notify_false_sends_nothing(self, async_client, admin_headers, test_db):
        cid = await _company(async_client, admin_headers)
        worker = await _worker(async_client, admin_headers, [cid])
        await _cell(async_client, admin_headers, cid, worker["id"], D1,
                    [{"start_time": "08:00", "end_time": "16:00"}])
        with patch("api.routers.shift_plans.EmailService.send_shift_plan_published_email",
                   new_callable=AsyncMock) as send:
            resp = await _publish(async_client, admin_headers, cid, notify=False)
        assert resp.status_code == 200 and send.await_count == 0
        pub = await test_db.ShiftPlanPublications.find_one({"company_id": cid})
        assert pub["notified"] is False

    async def test_publish_without_changes_creates_nothing(self, async_client, admin_headers, test_db):
        cid = await _company(async_client, admin_headers)
        with patch("api.routers.shift_plans.EmailService.send_shift_plan_published_email",
                   new_callable=AsyncMock) as send:
            resp = await _publish(async_client, admin_headers, cid)
        assert resp.status_code == 200
        assert resp.json() == {"publication_id": None, "changed_cells": 0}
        assert send.await_count == 0
        assert await test_db.ShiftPlanPublications.count_documents({"company_id": cid}) == 0


class TestWorkerLifecycle:
    async def test_delete_worker_keeps_past_removes_future(self, async_client, admin_headers, test_db):
        cid = await _company(async_client, admin_headers)
        worker = await _worker(async_client, admin_headers, [cid])
        wid = worker["id"]
        shift = [{"start_time": "08:00", "end_time": "16:00"}]
        await _cell(async_client, admin_headers, cid, wid, "2020-01-06", shift)
        await _cell(async_client, admin_headers, cid, wid, D1, shift)
        resp = await async_client.delete(f"/api/workers/{wid}", headers=admin_headers)
        assert resp.status_code == 204
        dates = [d["date"] async for d in test_db.PlannedShifts.find({"worker_id": wid})]
        assert dates == ["2020-01-06"]

    async def test_anonymize_removes_all(self, async_client, admin_headers, test_db):
        cid = await _company(async_client, admin_headers)
        worker = await _worker(async_client, admin_headers, [cid])
        wid = worker["id"]
        shift = [{"start_time": "08:00", "end_time": "16:00"}]
        await _cell(async_client, admin_headers, cid, wid, "2020-01-06", shift)
        await _cell(async_client, admin_headers, cid, wid, D1, shift)
        # Called directly: no role in the system holds `manage_workers`
        # (pre-existing), so the HTTP route is unreachable in tests.
        from api.routers.gdpr import AnonymizeRequest, anonymize_worker_data

        await anonymize_worker_data(wid, AnonymizeRequest(reason="Solicitud del trabajador"), _=None)
        assert await test_db.PlannedShifts.count_documents({"worker_id": wid}) == 0
        # Anonymized workers break the shared workers listing in other tests
        from bson import ObjectId
        await test_db.Workers.delete_one({"_id": ObjectId(wid)})

    async def test_worker_companies_exposes_flag(self, async_client, admin_headers):
        cid = await _company(async_client, admin_headers)
        worker = await _worker(async_client, admin_headers, [cid])
        resp = await async_client.post(
            "/api/workers/my-companies",
            json={"email": worker["email"], "password": WORKER_PASSWORD},
            headers=admin_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()[0]["shift_planning_enabled"] is True
