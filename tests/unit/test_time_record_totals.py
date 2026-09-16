"""
Unit tests for the per-worker day/week/month totals helpers.

These tests are pure unit tests (no MongoDB, no HTTP). The DB-backed
aggregation method is exercised with a mocked Motor collection.
"""

from datetime import date, datetime, timezone as dt_timezone
from unittest.mock import MagicMock, patch

import pytest
import pytz

from api.services.report_service import (
    ReportService,
    derive_period_totals,
    iso_week_range,
    month_range,
    totals_query_range,
)


# ===========================================================================
# Helpers
# ===========================================================================


async def _async_records(records):
    """Async generator standing in for a Motor cursor."""
    for record in records:
        yield record


def _make_utc(year: int, month: int, day: int, hour: int = 8, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, 0, tzinfo=dt_timezone.utc)


# ===========================================================================
# iso_week_range
# ===========================================================================


class TestIsoWeekRange:
    def test_midweek_returns_monday_and_sunday(self):
        # 2026-04-15 is a Wednesday.
        assert iso_week_range(date(2026, 4, 15)) == (date(2026, 4, 13), date(2026, 4, 19))

    def test_monday_maps_to_itself(self):
        assert iso_week_range(date(2026, 4, 13)) == (date(2026, 4, 13), date(2026, 4, 19))

    def test_sunday_maps_to_same_week_monday(self):
        assert iso_week_range(date(2026, 4, 19)) == (date(2026, 4, 13), date(2026, 4, 19))

    def test_week_crossing_year_boundary(self):
        # 2026-01-01 is a Thursday, so its ISO week started on 2025-12-29.
        assert iso_week_range(date(2026, 1, 1)) == (date(2025, 12, 29), date(2026, 1, 4))

    def test_week_crossing_month_boundary(self):
        # 2026-06-30 is a Tuesday, within the week 2026-06-29 .. 2026-07-05.
        assert iso_week_range(date(2026, 6, 30)) == (date(2026, 6, 29), date(2026, 7, 5))


# ===========================================================================
# month_range
# ===========================================================================


class TestMonthRange:
    def test_february_non_leap(self):
        assert month_range(date(2026, 2, 14)) == (date(2026, 2, 1), date(2026, 2, 28))

    def test_february_leap(self):
        assert month_range(date(2024, 2, 10)) == (date(2024, 2, 1), date(2024, 2, 29))

    def test_december_rolls_to_next_year(self):
        assert month_range(date(2026, 12, 20)) == (date(2026, 12, 1), date(2026, 12, 31))


# ===========================================================================
# totals_query_range
# ===========================================================================


class TestTotalsQueryRange:
    def test_single_midweek_day_covers_full_month(self):
        # Month start (Apr 1) is earlier than the week's Monday (Apr 13).
        assert totals_query_range([date(2026, 4, 15)]) == (date(2026, 4, 1), date(2026, 4, 30))

    def test_spans_two_months(self):
        # First day 2026-03-31: its month starts Mar 1, earlier than its Monday
        # (Mar 30). Last day 2026-04-02: its month ends Apr 30, later than its
        # Sunday (Apr 5).
        assert totals_query_range([date(2026, 3, 31), date(2026, 4, 2)]) == (
            date(2026, 3, 1),
            date(2026, 4, 30),
        )

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            totals_query_range([])


# ===========================================================================
# derive_period_totals
# ===========================================================================


class TestDerivePeriodTotals:
    # ISO week 2026-06-29 (Mon) .. 2026-07-05 (Sun) crosses the month boundary.
    _DAILY = {
        date(2026, 6, 29): 100.0,  # Monday, June
        date(2026, 7, 1): 120.0,   # Wednesday, July
        date(2026, 7, 5): 80.0,    # Sunday, July
        date(2026, 7, 20): 200.0,  # Later July day, different week
    }

    def test_week_crossing_month(self):
        daily, weekly, monthly = derive_period_totals(self._DAILY, date(2026, 7, 1))
        assert daily == 120.0
        assert weekly == 300.0  # 100 + 120 + 80, including June days
        assert monthly == 400.0  # 120 + 80 + 200, whole calendar month

    def test_daily_is_exact_day_only(self):
        daily, _, _ = derive_period_totals(self._DAILY, date(2026, 6, 29))
        assert daily == 100.0

    def test_month_is_isolated_per_record_day(self):
        _, _, monthly_june = derive_period_totals(self._DAILY, date(2026, 6, 29))
        assert monthly_june == 100.0

    def test_missing_day_is_zero(self):
        daily, weekly, monthly = derive_period_totals(self._DAILY, date(2026, 6, 30))
        assert daily == 0.0
        assert weekly == 300.0
        assert monthly == 100.0

    def test_empty_map_is_all_zero(self):
        assert derive_period_totals({}, date(2026, 7, 1)) == (0.0, 0.0, 0.0)


# ===========================================================================
# ReportService.get_exit_minutes_by_day_multi
# ===========================================================================


class TestGetExitMinutesByDayMulti:
    async def _call(
        self,
        docs,
        worker_ids=("w1",),
        start=date(2026, 4, 15),
        end=date(2026, 4, 16),
        tz_name="UTC",
        company_id="comp-1",
    ):
        tz = pytz.timezone(tz_name)
        with patch("api.services.report_service.db") as mock_db:
            mock_db.TimeRecords.aggregate = MagicMock(return_value=_async_records(docs))
            result = await ReportService().get_exit_minutes_by_day_multi(
                worker_ids=list(worker_ids),
                company_id=company_id,
                start_date=start,
                end_date=end,
                tz=tz,
            )
            return result, mock_db.TimeRecords.aggregate.call_args[0][0]

    async def test_groups_rows_per_worker_and_local_day(self):
        # The aggregation already returns one flat row per (worker, local day).
        docs = [
            {"worker_id": "w1", "date": "2026-04-15", "minutes": 210.0},
            {"worker_id": "w1", "date": "2026-04-16", "minutes": 60.0},
            {"worker_id": "w2", "date": "2026-04-15", "minutes": 45.0},
        ]
        result, _ = await self._call(docs, worker_ids=("w1", "w2"))
        assert result == {
            "w1": {date(2026, 4, 15): 210.0, date(2026, 4, 16): 60.0},
            "w2": {date(2026, 4, 15): 45.0},
        }

    async def test_single_aggregation_for_all_workers(self):
        with patch("api.services.report_service.db") as mock_db:
            aggregate = MagicMock(return_value=_async_records([]))
            mock_db.TimeRecords.aggregate = aggregate
            await ReportService().get_exit_minutes_by_day_multi(
                worker_ids=["w1", "w2", "w3"],
                company_id="comp-1",
                start_date=date(2026, 4, 15),
                end_date=date(2026, 4, 16),
                tz=pytz.UTC,
            )
        assert aggregate.call_count == 1

    async def test_match_filters_exit_range_and_company(self):
        _, pipeline = await self._call([], start=date(2026, 4, 15), end=date(2026, 4, 16))
        match = pipeline[0]["$match"]
        # Only closed shifts contribute and the query is bounded to one range.
        assert match["type"] == "exit"
        assert match["worker_id"] == {"$in": ["w1"]}
        assert match["company_id"] == "comp-1"
        assert match["timestamp"] == {
            "$gte": _make_utc(2026, 4, 15, 0, 0),
            "$lt": _make_utc(2026, 4, 17, 0, 0),
        }

    async def test_company_id_omitted_when_none(self):
        _, pipeline = await self._call([], company_id=None)
        assert "company_id" not in pipeline[0]["$match"]

    async def test_group_uses_requested_timezone_for_local_day(self):
        _, pipeline = await self._call([], tz_name="Europe/Madrid")
        date_to_string = pipeline[1]["$group"]["_id"]["local_day"]["$dateToString"]
        assert date_to_string["format"] == "%Y-%m-%d"
        assert date_to_string["timezone"] == "Europe/Madrid"

    async def test_utc_timezone_is_serialized_as_utc(self):
        _, pipeline = await self._call([], tz_name="UTC")
        assert pipeline[1]["$group"]["_id"]["local_day"]["$dateToString"]["timezone"] == "UTC"

    async def test_project_trims_payload(self):
        _, pipeline = await self._call([])
        assert pipeline[2]["$project"] == {
            "_id": 0,
            "worker_id": "$_id.worker_id",
            "date": "$_id.local_day",
            "minutes": 1,
        }

    async def test_empty_worker_ids_skips_the_query(self):
        with patch("api.services.report_service.db") as mock_db:
            aggregate = MagicMock(return_value=_async_records([]))
            mock_db.TimeRecords.aggregate = aggregate
            result = await ReportService().get_exit_minutes_by_day_multi(
                worker_ids=[],
                company_id=None,
                start_date=date(2026, 4, 15),
                end_date=date(2026, 4, 15),
                tz=pytz.UTC,
            )
        assert result == {}
        assert aggregate.call_count == 0
