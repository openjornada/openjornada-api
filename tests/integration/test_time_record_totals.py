"""
Integration tests for per-worker day/week/month totals on the time-records
listing and per-worker history endpoints.

These tests insert TimeRecords directly into the test database and assert the
totals computed on the fly by the API. They require MongoDB.
"""
from datetime import date, datetime, timezone as dt_timezone
from unittest.mock import patch

import pytest
from bson import ObjectId


def _utc(year: int, month: int, day: int, hour: int = 8, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, 0, tzinfo=dt_timezone.utc)


async def _insert_record(test_db, worker_id, company_id, worker_name, rtype, ts, duration=None):
    doc = {
        "worker_id": worker_id,
        "worker_name": worker_name,
        "type": rtype,
        "timestamp": ts,
        "created_at": ts,
        "company_id": company_id,
        "recorded_by": "totals_test",
    }
    if duration is not None:
        doc["duration_minutes"] = duration
    await test_db.TimeRecords.insert_one(doc)


class TestListingTotals:
    """GET /api/time-records/ returns day/week/month totals per worker."""

    @pytest.mark.asyncio
    async def test_two_exits_same_day_and_period_totals(
        self, async_client, admin_headers, test_db
    ):
        company_id = str(ObjectId())
        worker_id = str(ObjectId())
        worker_name = "Totals Worker Same Day"
        try:
            # Week of 2026-04-13 .. 2026-04-19 (April).
            await _insert_record(
                test_db, worker_id, company_id, worker_name, "entry", _utc(2026, 4, 15, 7, 50)
            )
            await _insert_record(
                test_db, worker_id, company_id, worker_name, "exit",
                _utc(2026, 4, 15, 8, 0), 120.0,
            )
            await _insert_record(
                test_db, worker_id, company_id, worker_name, "exit",
                _utc(2026, 4, 15, 14, 0), 90.0,
            )
            # Same ISO week but previous day (outside the filter).
            await _insert_record(
                test_db, worker_id, company_id, worker_name, "exit",
                _utc(2026, 4, 14, 9, 0), 300.0,
            )
            # Same calendar month but later week (outside the filter).
            await _insert_record(
                test_db, worker_id, company_id, worker_name, "exit",
                _utc(2026, 4, 20, 9, 0), 60.0,
            )

            resp = await async_client.get(
                "/api/time-records/",
                params={
                    "company_id": company_id,
                    "start_date": "2026-04-15",
                    "end_date": "2026-04-15",
                    "timezone": "UTC",
                },
                headers=admin_headers,
            )
            assert resp.status_code == 200, resp.text
            rows = resp.json()
            assert len(rows) == 3  # entry + two exits on Apr 15

            for row in rows:
                assert row["daily_total_minutes"] == 210.0
                assert row["weekly_total_minutes"] == 510.0   # 300 + 210
                assert row["monthly_total_minutes"] == 570.0  # 300 + 210 + 60
        finally:
            await test_db.TimeRecords.delete_many({"company_id": company_id})

    @pytest.mark.asyncio
    async def test_iso_week_crossing_month_and_short_filter(
        self, async_client, admin_headers, test_db
    ):
        company_id = str(ObjectId())
        worker_id = str(ObjectId())
        worker_name = "Totals Worker Cross Month"
        try:
            # Week 2026-06-29 (Mon) .. 2026-07-05 (Sun) crosses June/July.
            await _insert_record(
                test_db, worker_id, company_id, worker_name, "exit",
                _utc(2026, 6, 29, 8, 0), 100.0,
            )
            await _insert_record(
                test_db, worker_id, company_id, worker_name, "entry", _utc(2026, 7, 1, 7, 50)
            )
            await _insert_record(
                test_db, worker_id, company_id, worker_name, "exit",
                _utc(2026, 7, 1, 8, 0), 120.0,
            )
            await _insert_record(
                test_db, worker_id, company_id, worker_name, "exit",
                _utc(2026, 7, 5, 8, 0), 80.0,
            )
            # Same July month, later week (outside filter).
            await _insert_record(
                test_db, worker_id, company_id, worker_name, "exit",
                _utc(2026, 7, 20, 8, 0), 200.0,
            )

            # Filter only Wed-Thu of that week.
            resp = await async_client.get(
                "/api/time-records/",
                params={
                    "company_id": company_id,
                    "start_date": "2026-07-01",
                    "end_date": "2026-07-02",
                    "timezone": "UTC",
                },
                headers=admin_headers,
            )
            assert resp.status_code == 200, resp.text
            rows = resp.json()
            assert len(rows) == 2  # entry + exit on Jul 1

            for row in rows:
                assert row["daily_total_minutes"] == 120.0
                # Whole ISO week, including Jun 29 and Jul 5 outside the filter.
                assert row["weekly_total_minutes"] == 300.0
                # Whole calendar month of July, including Jul 20 outside the filter.
                assert row["monthly_total_minutes"] == 400.0
        finally:
            await test_db.TimeRecords.delete_many({"company_id": company_id})

    @pytest.mark.asyncio
    async def test_month_total_includes_days_before_first_week(
        self, async_client, admin_headers, test_db
    ):
        company_id = str(ObjectId())
        worker_id = str(ObjectId())
        worker_name = "Totals Worker Early Month"
        try:
            # Earlier in the same month but before the ISO week of the returned
            # row (Mar 31 -> week starts Mon Mar 30), so a range that only went
            # back to Monday would miss it.
            await _insert_record(
                test_db, worker_id, company_id, worker_name, "exit",
                _utc(2026, 3, 10, 8, 0), 500.0,
            )
            await _insert_record(
                test_db, worker_id, company_id, worker_name, "exit",
                _utc(2026, 3, 31, 8, 0), 120.0,
            )

            resp = await async_client.get(
                "/api/time-records/",
                params={
                    "company_id": company_id,
                    "start_date": "2026-03-31",
                    "end_date": "2026-03-31",
                    "timezone": "UTC",
                },
                headers=admin_headers,
            )
            assert resp.status_code == 200, resp.text
            rows = resp.json()
            assert len(rows) == 1
            assert rows[0]["daily_total_minutes"] == 120.0
            assert rows[0]["weekly_total_minutes"] == 120.0  # week Mar 30 - Apr 5
            assert rows[0]["monthly_total_minutes"] == 620.0  # whole March
        finally:
            await test_db.TimeRecords.delete_many({"company_id": company_id})

    @pytest.mark.asyncio
    async def test_open_shift_excluded_from_totals(
        self, async_client, admin_headers, test_db
    ):
        company_id = str(ObjectId())
        worker_id = str(ObjectId())
        worker_name = "Totals Worker Open Shift"
        try:
            # Open shift: entry with no exit -> contributes zero minutes.
            await _insert_record(
                test_db, worker_id, company_id, worker_name, "entry", _utc(2026, 5, 11, 8, 0)
            )
            # Closed shift the next day: 45 minutes.
            await _insert_record(
                test_db, worker_id, company_id, worker_name, "entry", _utc(2026, 5, 12, 8, 0)
            )
            await _insert_record(
                test_db, worker_id, company_id, worker_name, "exit",
                _utc(2026, 5, 12, 9, 0), 45.0,
            )

            resp = await async_client.get(
                "/api/time-records/",
                params={
                    "company_id": company_id,
                    "start_date": "2026-05-11",
                    "end_date": "2026-05-12",
                    "timezone": "UTC",
                },
                headers=admin_headers,
            )
            assert resp.status_code == 200, resp.text
            rows = resp.json()
            by_day = {}
            for row in rows:
                day = row["timestamp"][:10]
                by_day.setdefault(day, []).append(row)

            # The open shift's day has zero daily minutes; the week/month still
            # include the closed shift on the next day.
            for row in by_day["2026-05-11"]:
                assert row["daily_total_minutes"] == 0.0
                assert row["weekly_total_minutes"] == 45.0
                assert row["monthly_total_minutes"] == 45.0

            for row in by_day["2026-05-12"]:
                assert row["daily_total_minutes"] == 45.0
        finally:
            await test_db.TimeRecords.delete_many({"company_id": company_id})

    @pytest.mark.asyncio
    async def test_timezone_other_than_utc(
        self, async_client, admin_headers, test_db
    ):
        company_id = str(ObjectId())
        worker_id = str(ObjectId())
        worker_name = "Totals Worker Timezone"
        try:
            # Madrid is UTC+2 (CEST) on these dates:
            #  20:00 UTC Mar 31 -> 22:00 local Mar 31
            #  23:30 UTC Mar 31 -> 01:30 local Apr 1
            await _insert_record(
                test_db, worker_id, company_id, worker_name, "exit",
                _utc(2026, 3, 31, 20, 0), 60.0,
            )
            await _insert_record(
                test_db, worker_id, company_id, worker_name, "exit",
                _utc(2026, 3, 31, 23, 30), 120.0,
            )

            resp = await async_client.get(
                "/api/time-records/",
                params={
                    "company_id": company_id,
                    "start_date": "2026-04-01",
                    "end_date": "2026-04-01",
                    "timezone": "Europe/Madrid",
                },
                headers=admin_headers,
            )
            assert resp.status_code == 200, resp.text
            rows = resp.json()
            # Only the 23:30 UTC shift lands on local Apr 1.
            assert len(rows) == 1
            row = rows[0]
            assert row["timestamp"].startswith("2026-03-31T23:30")
            assert row["daily_total_minutes"] == 120.0
            # The 20:00 UTC shift is Mar 31 local, same ISO week -> weekly=180,
            # but a different calendar month -> monthly=120.
            assert row["weekly_total_minutes"] == 180.0
            assert row["monthly_total_minutes"] == 120.0
        finally:
            await test_db.TimeRecords.delete_many({"company_id": company_id})


class TestListingTotalsAggregation:
    """The listing computes every worker's totals with one aggregation."""

    @pytest.mark.asyncio
    async def test_multiple_workers_keep_their_own_totals(
        self, async_client, admin_headers, test_db
    ):
        company_id = str(ObjectId())
        worker_a = str(ObjectId())
        worker_b = str(ObjectId())
        try:
            # worker_a works twice on Apr 15 (200 min); worker_b once on Apr 16
            # (50 min). Both days share the same ISO week (Apr 13-19) and month,
            # so any cross-worker leakage would show up in the weekly/monthly
            # totals of the other worker.
            await _insert_record(
                test_db, worker_a, company_id, "Worker A", "exit",
                _utc(2026, 4, 15, 8, 0), 120.0,
            )
            await _insert_record(
                test_db, worker_a, company_id, "Worker A", "exit",
                _utc(2026, 4, 15, 14, 0), 80.0,
            )
            await _insert_record(
                test_db, worker_b, company_id, "Worker B", "exit",
                _utc(2026, 4, 16, 8, 0), 50.0,
            )

            resp = await async_client.get(
                "/api/time-records/",
                params={
                    "company_id": company_id,
                    "start_date": "2026-04-15",
                    "end_date": "2026-04-16",
                    "timezone": "UTC",
                },
                headers=admin_headers,
            )
            assert resp.status_code == 200, resp.text
            rows = resp.json()
            assert len(rows) == 3

            rows_by_worker = {}
            for row in rows:
                rows_by_worker.setdefault(row["worker_id"], []).append(row)
            assert set(rows_by_worker) == {worker_a, worker_b}

            for row in rows_by_worker[worker_a]:
                assert row["daily_total_minutes"] == 200.0
                # Apr 16 belongs to worker_b; it must not leak into worker_a.
                assert row["weekly_total_minutes"] == 200.0
                assert row["monthly_total_minutes"] == 200.0

            for row in rows_by_worker[worker_b]:
                assert row["daily_total_minutes"] == 50.0
                # Apr 15 belongs to worker_a; it must not leak into worker_b.
                assert row["weekly_total_minutes"] == 50.0
                assert row["monthly_total_minutes"] == 50.0
        finally:
            await test_db.TimeRecords.delete_many({"company_id": company_id})

    @pytest.mark.asyncio
    async def test_company_filter_restricts_totals(
        self, async_client, admin_headers, test_db
    ):
        company_a = str(ObjectId())
        company_b = str(ObjectId())
        worker_id = str(ObjectId())
        try:
            # Same worker, same local day, two different companies. Filtering by
            # company A must exclude company B's minutes from the totals.
            await _insert_record(
                test_db, worker_id, company_a, "Totals Worker Two Companies", "exit",
                _utc(2026, 4, 15, 9, 0), 100.0,
            )
            await _insert_record(
                test_db, worker_id, company_b, "Totals Worker Two Companies", "exit",
                _utc(2026, 4, 15, 14, 0), 999.0,
            )

            resp = await async_client.get(
                "/api/time-records/",
                params={
                    "company_id": company_a,
                    "start_date": "2026-04-15",
                    "end_date": "2026-04-15",
                    "timezone": "UTC",
                },
                headers=admin_headers,
            )
            assert resp.status_code == 200, resp.text
            rows = resp.json()
            assert len(rows) == 1
            assert rows[0]["daily_total_minutes"] == 100.0
            assert rows[0]["weekly_total_minutes"] == 100.0
            assert rows[0]["monthly_total_minutes"] == 100.0
        finally:
            await test_db.TimeRecords.delete_many({"company_id": {"$in": [company_a, company_b]}})

    @pytest.mark.asyncio
    async def test_listing_issues_one_aggregation_for_all_workers(
        self, async_client, admin_headers, test_db
    ):
        import api.services.report_service as report_service_module

        company_id = str(ObjectId())
        worker_a = str(ObjectId())
        worker_b = str(ObjectId())
        original = report_service_module.ReportService.get_exit_minutes_by_day_multi
        calls = []

        async def counted(self, *args, **kwargs):
            calls.append((args, kwargs))
            return await original(self, *args, **kwargs)

        try:
            for worker_id, name in ((worker_a, "Worker A"), (worker_b, "Worker B")):
                await _insert_record(
                    test_db, worker_id, company_id, name, "exit",
                    _utc(2026, 4, 15, 8, 0), 60.0,
                )

            with patch.object(
                report_service_module.ReportService,
                "get_exit_minutes_by_day_multi",
                counted,
            ):
                resp = await async_client.get(
                    "/api/time-records/",
                    params={
                        "company_id": company_id,
                        "start_date": "2026-04-15",
                        "end_date": "2026-04-15",
                        "timezone": "UTC",
                    },
                    headers=admin_headers,
                )
            assert resp.status_code == 200, resp.text
            assert len(resp.json()) == 2
            # A single aggregation call covering both workers (would be 2 before).
            assert len(calls) == 1
            assert set(calls[0][1]["worker_ids"]) == {worker_a, worker_b}
        finally:
            await test_db.TimeRecords.delete_many({"company_id": company_id})


class TestWorkerHistoryTotals:
    """GET /api/time-records/worker/{worker_id} also returns the totals."""

    @pytest.mark.asyncio
    async def test_worker_history_totals(
        self, async_client, admin_headers, test_db
    ):
        company_id = str(ObjectId())
        worker_oid = ObjectId()
        worker_id = str(worker_oid)
        worker_name = "Totals History Worker"
        try:
            await test_db.Workers.insert_one({
                "_id": worker_oid,
                "first_name": "Totals",
                "last_name": "History Worker",
                "email": f"totals.history.{worker_id}@test.com",
                "id_number": f"TOT{worker_id[-8:]}",
                "company_ids": [company_id],
                "hashed_password": "not-used",
                "deleted_at": None,
            })
            # Mon 2026-06-29 .. Sun 2026-07-05 week.
            await _insert_record(
                test_db, worker_id, company_id, worker_name, "exit",
                _utc(2026, 6, 29, 8, 0), 100.0,
            )
            await _insert_record(
                test_db, worker_id, company_id, worker_name, "exit",
                _utc(2026, 7, 1, 8, 0), 120.0,
            )

            resp = await async_client.get(
                f"/api/time-records/worker/{worker_id}",
                params={
                    "start_date": "2026-07-01",
                    "end_date": "2026-07-01",
                    "timezone": "UTC",
                },
                headers=admin_headers,
            )
            assert resp.status_code == 200, resp.text
            rows = resp.json()
            assert len(rows) == 1
            assert rows[0]["daily_total_minutes"] == 120.0
            assert rows[0]["weekly_total_minutes"] == 220.0  # 100 + 120
            assert rows[0]["monthly_total_minutes"] == 120.0  # July only
        finally:
            await test_db.TimeRecords.delete_many({"worker_id": worker_id})
            await test_db.Workers.delete_one({"_id": worker_oid})
