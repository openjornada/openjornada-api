"""
Unit tests for SMS scheduler logic in SchedulerService.

Covers _check_open_shifts() and _process_company_sms() in isolation.
No MongoDB or APScheduler instance is started; all external dependencies
are replaced with unittest.mock objects.

Run with:
    python -m pytest tests/unit/test_scheduler_sms.py --noconftest -v
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

UTC = timezone.utc

# A fixed "now" anchored to 2026-03-14 12:00 UTC.
# In Europe/Madrid that is 13:00 (UTC+1 winter), well inside 08:00-23:00.
NOW_UTC = datetime(2026, 3, 14, 12, 0, 0, tzinfo=UTC)

# Valid 24-character hex ObjectId strings used as stable IDs across tests.
COMPANY_OID = "aaaaaaaaaaaaaaaaaaaaaaaa"
WORKER_OID  = "bbbbbbbbbbbbbbbbbbbbbbbb"
ENTRY_OID   = "cccccccccccccccccccccccc"


# ---------------------------------------------------------------------------
# Document factory helpers
# ---------------------------------------------------------------------------


def _make_company(
    sms_enabled: bool = True,
    first_reminder_minutes: int = 240,   # 4 h
    reminder_frequency_minutes: int = 60,
    max_reminders_per_day: int = 5,
    active_hours_start: str = "08:00",
    active_hours_end: str = "23:00",
    timezone_name: str = "Europe/Madrid",
) -> dict:
    """Build a minimal company document with SMS config."""
    return {
        "_id": ObjectId(COMPANY_OID),
        "name": "Test Company SL",
        "deleted_at": None,
        "sms_config": {
            "enabled": sms_enabled,
            "first_reminder_minutes": first_reminder_minutes,
            "reminder_frequency_minutes": reminder_frequency_minutes,
            "max_reminders_per_day": max_reminders_per_day,
            "active_hours_start": active_hours_start,
            "active_hours_end": active_hours_end,
            "timezone": timezone_name,
        },
    }


def _make_entry_record(
    worker_id: str = WORKER_OID,
    company_id: str = COMPANY_OID,
    hours_ago: float = 5.0,
) -> dict:
    """Build a minimal 'entry' TimeRecord document."""
    return {
        "_id": ObjectId(ENTRY_OID),
        "worker_id": worker_id,
        "company_id": company_id,
        "type": "entry",
        "timestamp": NOW_UTC - timedelta(hours=hours_ago),
    }


def _make_worker(
    sms_enabled: bool = True,
    phone_number="+34600000001",
    first_name: str = "Ana",
    last_name: str = "Garcia",
) -> dict:
    """Build a minimal worker document."""
    return {
        "_id": ObjectId(WORKER_OID),
        "first_name": first_name,
        "last_name": last_name,
        "phone_number": phone_number,
        "deleted_at": None,
        "sms_config": {
            "sms_enabled": sms_enabled,
        },
    }


# ---------------------------------------------------------------------------
# Async cursor helper
# ---------------------------------------------------------------------------


def _async_cursor(documents: list) -> MagicMock:
    """
    Return a MagicMock that behaves like an async Motor cursor, so that
    ``async for doc in cursor`` yields each item in *documents*.
    The mock also supports ``.sort(...)`` chaining.
    """
    cursor = MagicMock()
    cursor.__aiter__ = MagicMock(return_value=_alist(documents))
    cursor.sort = MagicMock(return_value=cursor)
    return cursor


def _alist(items: list):
    """Minimal async generator wrapping a plain list."""

    async def _gen():
        for item in items:
            yield item

    return _gen()


# ---------------------------------------------------------------------------
# datetime.now patcher
# ---------------------------------------------------------------------------


def _patch_datetime_now(pinned_utc: datetime):
    """
    Return a context manager that replaces ``datetime`` in the
    scheduler_service module with a subclass whose ``now()`` is frozen to
    *pinned_utc*, while every other behaviour (constructors, arithmetic, ...)
    is inherited from the real ``datetime`` class.

    The production code calls:
      * ``datetime.now(company_tz)``     -> local time for active-hours check
      * ``datetime.now(timezone.utc)``   -> UTC "now" for elapsed-time maths
    """
    import api.services.scheduler_service as _sched

    real_datetime = datetime  # capture the real class before patching

    class _FrozenDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            if tz is None:
                return pinned_utc
            return pinned_utc.astimezone(tz)

    return patch.object(_sched, "datetime", _FrozenDatetime)


# ---------------------------------------------------------------------------
# Fixture: SchedulerService with APScheduler mocked out
# ---------------------------------------------------------------------------


@pytest.fixture
def scheduler():
    """
    Instantiate SchedulerService without touching APScheduler.

    AsyncIOScheduler is patched so its constructor/start/shutdown are no-ops.
    """
    with patch("api.services.scheduler_service.AsyncIOScheduler") as mock_cls:
        mock_cls.return_value = MagicMock()
        from api.services.scheduler_service import SchedulerService

        yield SchedulerService()


# ---------------------------------------------------------------------------
# Helper: inject a fake sms_service module for _check_open_shifts tests
# ---------------------------------------------------------------------------


def _inject_sms_module(mock_sms: MagicMock):
    """
    Insert a fake ``api.services.sms_service`` module into sys.modules so
    that the ``from .sms_service import sms_service`` statement inside
    _check_open_shifts resolves to *mock_sms*.

    Returns the original module (or None) so the caller can restore it.
    """
    fake_mod = types.ModuleType("api.services.sms_service")
    fake_mod.sms_service = mock_sms  # type: ignore[attr-defined]
    original = sys.modules.get("api.services.sms_service")
    sys.modules["api.services.sms_service"] = fake_mod
    return original


def _restore_sms_module(original) -> None:
    if original is None:
        sys.modules.pop("api.services.sms_service", None)
    else:
        sys.modules["api.services.sms_service"] = original


# ===========================================================================
# Tests: _check_open_shifts()
# ===========================================================================


class TestCheckOpenShifts:
    """Gateway tests for the top-level _check_open_shifts() dispatcher."""

    # ------------------------------------------------------------------
    # 1. SMS disabled -> immediate return, no DB access
    # ------------------------------------------------------------------

    async def test_sms_disabled_returns_immediately_no_db_query(self, scheduler):
        """
        When sms_service.is_enabled() returns False the method must bail out
        before issuing any query to db.Companies.
        """
        mock_sms = MagicMock()
        mock_sms.is_enabled.return_value = False

        original = _inject_sms_module(mock_sms)
        try:
            with patch("api.services.scheduler_service.db") as mock_db:
                await scheduler._check_open_shifts()
        finally:
            _restore_sms_module(original)

        mock_db.Companies.find.assert_not_called()
        mock_sms.send_shift_reminder.assert_not_called()

    # ------------------------------------------------------------------
    # 2. SMS enabled, no companies with sms_config.enabled=True
    # ------------------------------------------------------------------

    async def test_sms_enabled_no_matching_companies_no_sms(self, scheduler):
        """
        When SMS is enabled but the cursor returns no companies (empty DB
        result), send_shift_reminder must never be called.
        """
        mock_sms = MagicMock()
        mock_sms.is_enabled.return_value = True

        original = _inject_sms_module(mock_sms)
        try:
            with patch("api.services.scheduler_service.db") as mock_db:
                mock_db.Companies.find.return_value = _async_cursor([])
                await scheduler._check_open_shifts()
        finally:
            _restore_sms_module(original)

        mock_sms.send_shift_reminder.assert_not_called()


# ===========================================================================
# Tests: _process_company_sms()
# ===========================================================================


class TestProcessCompanySms:
    """
    Behaviour tests for _process_company_sms().

    The sms_service is passed as a direct argument, so no sys.modules surgery
    is required in this class.  ``datetime.now`` is frozen via
    _patch_datetime_now and DB collections are patched at the module level.
    """

    def _make_sms(self) -> MagicMock:
        sms = MagicMock()
        sms.send_shift_reminder = AsyncMock(return_value=True)
        return sms

    # ------------------------------------------------------------------
    # 3. Outside active hours (before start)
    # ------------------------------------------------------------------

    async def test_outside_active_hours_before_start_no_sms(self, scheduler):
        """
        Current local time is before active_hours_start -> return early;
        no TimeRecords query and no SMS.

        Pinned UTC: 06:00  ->  07:00 Europe/Madrid (before 08:00 start).
        """
        early_utc = datetime(2026, 3, 14, 6, 0, 0, tzinfo=UTC)
        company = _make_company(
            active_hours_start="08:00",
            active_hours_end="23:00",
            timezone_name="Europe/Madrid",
        )
        mock_sms = self._make_sms()

        with patch("api.services.scheduler_service.db") as mock_db, \
                _patch_datetime_now(early_utc):
            await scheduler._process_company_sms(company, mock_sms)

        mock_db.TimeRecords.find.assert_not_called()
        mock_sms.send_shift_reminder.assert_not_called()

    async def test_outside_active_hours_after_end_no_sms(self, scheduler):
        """
        Current local time is after active_hours_end -> return early.

        Pinned UTC: 23:00  ->  00:00 next-day Europe/Madrid (after 23:00 end).
        """
        late_utc = datetime(2026, 3, 14, 23, 0, 0, tzinfo=UTC)  # 00:00 Madrid
        company = _make_company(
            active_hours_start="08:00",
            active_hours_end="23:00",
            timezone_name="Europe/Madrid",
        )
        mock_sms = self._make_sms()

        with patch("api.services.scheduler_service.db") as mock_db, \
                _patch_datetime_now(late_utc):
            await scheduler._process_company_sms(company, mock_sms)

        mock_db.TimeRecords.find.assert_not_called()
        mock_sms.send_shift_reminder.assert_not_called()

    # ------------------------------------------------------------------
    # 4. Within active hours -- entry too recent
    # ------------------------------------------------------------------

    async def test_entry_too_recent_no_sms(self, scheduler):
        """
        Entry is only 1 h old; first_reminder threshold is 4 h -> skip.
        """
        company = _make_company(first_reminder_minutes=240)
        entry = _make_entry_record(hours_ago=1.0)
        mock_sms = self._make_sms()

        with patch("api.services.scheduler_service.db") as mock_db, \
                _patch_datetime_now(NOW_UTC):
            mock_db.TimeRecords.find.return_value = _async_cursor([entry])
            mock_db.TimeRecords.find_one = AsyncMock(return_value=None)

            await scheduler._process_company_sms(company, mock_sms)

        mock_sms.send_shift_reminder.assert_not_called()

    # ------------------------------------------------------------------
    # 5. Within active hours -- first reminder threshold reached
    # ------------------------------------------------------------------

    async def test_first_reminder_sent_when_threshold_reached(self, scheduler):
        """
        Entry is 5 h old, threshold is 4 h, 0 reminders sent -> send #1.
        """
        company = _make_company(first_reminder_minutes=240)
        entry = _make_entry_record(hours_ago=5.0)
        worker = _make_worker()
        mock_sms = self._make_sms()

        with patch("api.services.scheduler_service.db") as mock_db, \
                _patch_datetime_now(NOW_UTC):
            mock_db.TimeRecords.find.return_value = _async_cursor([entry])
            mock_db.TimeRecords.find_one = AsyncMock(return_value=None)  # no exit
            mock_db.SmsLogs.count_documents = AsyncMock(return_value=0)
            mock_db.Workers.find_one = AsyncMock(return_value=worker)

            await scheduler._process_company_sms(company, mock_sms)

        mock_sms.send_shift_reminder.assert_awaited_once()
        kwargs = mock_sms.send_shift_reminder.call_args.kwargs
        assert kwargs["worker_id"] == WORKER_OID
        assert kwargs["time_record_entry_id"] == ENTRY_OID
        assert kwargs["phone_number"] == "+34600000001"
        assert kwargs["reminder_number"] == 1
        assert kwargs["company_id"] == COMPANY_OID

    # ------------------------------------------------------------------
    # 6. Max reminders already reached
    # ------------------------------------------------------------------

    async def test_max_reminders_reached_no_sms(self, scheduler):
        """
        reminders_sent >= max_reminders_per_day -> no SMS sent regardless of
        elapsed time.
        """
        max_r = 3
        company = _make_company(
            first_reminder_minutes=240,
            max_reminders_per_day=max_r,
        )
        entry = _make_entry_record(hours_ago=10.0)
        worker = _make_worker()
        mock_sms = self._make_sms()

        with patch("api.services.scheduler_service.db") as mock_db, \
                _patch_datetime_now(NOW_UTC):
            mock_db.TimeRecords.find.return_value = _async_cursor([entry])
            mock_db.TimeRecords.find_one = AsyncMock(return_value=None)
            mock_db.SmsLogs.count_documents = AsyncMock(return_value=max_r)
            mock_db.Workers.find_one = AsyncMock(return_value=worker)

            await scheduler._process_company_sms(company, mock_sms)

        mock_sms.send_shift_reminder.assert_not_called()

    # ------------------------------------------------------------------
    # 7. Shift already closed
    # ------------------------------------------------------------------

    async def test_shift_closed_no_sms(self, scheduler):
        """
        A matching exit record exists for the worker -> shift is closed ->
        no SMS sent.
        """
        company = _make_company(first_reminder_minutes=240)
        entry = _make_entry_record(hours_ago=5.0)
        exit_doc = {
            "_id": ObjectId(),
            "worker_id": WORKER_OID,
            "company_id": COMPANY_OID,
            "type": "exit",
            "timestamp": NOW_UTC - timedelta(hours=1),
        }
        mock_sms = self._make_sms()

        with patch("api.services.scheduler_service.db") as mock_db, \
                _patch_datetime_now(NOW_UTC):
            mock_db.TimeRecords.find.return_value = _async_cursor([entry])
            mock_db.TimeRecords.find_one = AsyncMock(return_value=exit_doc)

            await scheduler._process_company_sms(company, mock_sms)

        mock_sms.send_shift_reminder.assert_not_called()

    # ------------------------------------------------------------------
    # 8. Worker opted out
    # ------------------------------------------------------------------

    async def test_worker_opted_out_no_sms(self, scheduler):
        """Worker sms_config.sms_enabled=False -> no SMS."""
        company = _make_company(first_reminder_minutes=240)
        entry = _make_entry_record(hours_ago=5.0)
        worker = _make_worker(sms_enabled=False)
        mock_sms = self._make_sms()

        with patch("api.services.scheduler_service.db") as mock_db, \
                _patch_datetime_now(NOW_UTC):
            mock_db.TimeRecords.find.return_value = _async_cursor([entry])
            mock_db.TimeRecords.find_one = AsyncMock(return_value=None)
            mock_db.SmsLogs.count_documents = AsyncMock(return_value=0)
            mock_db.Workers.find_one = AsyncMock(return_value=worker)

            await scheduler._process_company_sms(company, mock_sms)

        mock_sms.send_shift_reminder.assert_not_called()

    # ------------------------------------------------------------------
    # 9. Worker has no phone number
    # ------------------------------------------------------------------

    async def test_worker_no_phone_number_no_sms(self, scheduler):
        """Worker has phone_number=None -> no SMS."""
        company = _make_company(first_reminder_minutes=240)
        entry = _make_entry_record(hours_ago=5.0)
        worker = _make_worker(phone_number=None)
        mock_sms = self._make_sms()

        with patch("api.services.scheduler_service.db") as mock_db, \
                _patch_datetime_now(NOW_UTC):
            mock_db.TimeRecords.find.return_value = _async_cursor([entry])
            mock_db.TimeRecords.find_one = AsyncMock(return_value=None)
            mock_db.SmsLogs.count_documents = AsyncMock(return_value=0)
            mock_db.Workers.find_one = AsyncMock(return_value=worker)

            await scheduler._process_company_sms(company, mock_sms)

        mock_sms.send_shift_reminder.assert_not_called()

    # ------------------------------------------------------------------
    # Edge: repeat interval not yet reached for second reminder
    # ------------------------------------------------------------------

    async def test_second_reminder_not_yet_due(self, scheduler):
        """
        One reminder already sent; repeat_interval=60 min.
        Entry is 4 h 30 min old -> next threshold = 4 h + 1 h = 5 h -> skip.
        """
        company = _make_company(
            first_reminder_minutes=240,
            reminder_frequency_minutes=60,
        )
        entry = _make_entry_record(hours_ago=4.5)
        worker = _make_worker()
        mock_sms = self._make_sms()

        with patch("api.services.scheduler_service.db") as mock_db, \
                _patch_datetime_now(NOW_UTC):
            mock_db.TimeRecords.find.return_value = _async_cursor([entry])
            mock_db.TimeRecords.find_one = AsyncMock(return_value=None)
            mock_db.SmsLogs.count_documents = AsyncMock(return_value=1)
            mock_db.Workers.find_one = AsyncMock(return_value=worker)

            await scheduler._process_company_sms(company, mock_sms)

        mock_sms.send_shift_reminder.assert_not_called()

    # ------------------------------------------------------------------
    # Edge: second reminder is due
    # ------------------------------------------------------------------

    async def test_second_reminder_sent_when_due(self, scheduler):
        """
        One reminder already sent; entry is 5 h 30 min old.
        Threshold for reminder #2 = 4 h + 1 h = 5 h -> due -> send.
        """
        company = _make_company(
            first_reminder_minutes=240,
            reminder_frequency_minutes=60,
        )
        entry = _make_entry_record(hours_ago=5.5)
        worker = _make_worker()
        mock_sms = self._make_sms()

        with patch("api.services.scheduler_service.db") as mock_db, \
                _patch_datetime_now(NOW_UTC):
            mock_db.TimeRecords.find.return_value = _async_cursor([entry])
            mock_db.TimeRecords.find_one = AsyncMock(return_value=None)
            mock_db.SmsLogs.count_documents = AsyncMock(return_value=1)
            mock_db.Workers.find_one = AsyncMock(return_value=worker)

            await scheduler._process_company_sms(company, mock_sms)

        mock_sms.send_shift_reminder.assert_awaited_once()
        kwargs = mock_sms.send_shift_reminder.call_args.kwargs
        assert kwargs["reminder_number"] == 2
        assert kwargs["time_record_entry_id"] == ENTRY_OID

    # ------------------------------------------------------------------
    # Edge: naive entry timestamp treated as UTC
    # ------------------------------------------------------------------

    async def test_naive_entry_timestamp_treated_as_utc(self, scheduler):
        """
        A naive (tz-unaware) entry timestamp must be treated as UTC without
        raising, and elapsed-time calculation must remain correct.
        """
        company = _make_company(first_reminder_minutes=240)
        entry = _make_entry_record(hours_ago=5.0)
        # Strip tzinfo to simulate a document stored without timezone info.
        entry["timestamp"] = entry["timestamp"].replace(tzinfo=None)
        worker = _make_worker()
        mock_sms = self._make_sms()

        with patch("api.services.scheduler_service.db") as mock_db, \
                _patch_datetime_now(NOW_UTC):
            mock_db.TimeRecords.find.return_value = _async_cursor([entry])
            mock_db.TimeRecords.find_one = AsyncMock(return_value=None)
            mock_db.SmsLogs.count_documents = AsyncMock(return_value=0)
            mock_db.Workers.find_one = AsyncMock(return_value=worker)

            await scheduler._process_company_sms(company, mock_sms)

        # 5 h old, 4 h threshold -> reminder must be sent.
        mock_sms.send_shift_reminder.assert_awaited_once()

    # ------------------------------------------------------------------
    # Edge: entry missing worker_id is skipped silently
    # ------------------------------------------------------------------

    async def test_entry_missing_worker_id_skipped(self, scheduler):
        """
        An entry document with no worker_id field must be silently skipped
        without raising an exception.
        """
        company = _make_company(first_reminder_minutes=240)
        entry = _make_entry_record(hours_ago=5.0)
        del entry["worker_id"]  # simulate incomplete/corrupt document
        mock_sms = self._make_sms()

        with patch("api.services.scheduler_service.db") as mock_db, \
                _patch_datetime_now(NOW_UTC):
            mock_db.TimeRecords.find.return_value = _async_cursor([entry])
            mock_db.TimeRecords.find_one = AsyncMock(return_value=None)

            await scheduler._process_company_sms(company, mock_sms)

        mock_sms.send_shift_reminder.assert_not_called()

    # ------------------------------------------------------------------
    # Edge: worker not found in DB
    # ------------------------------------------------------------------

    async def test_worker_not_found_no_sms(self, scheduler):
        """
        When db.Workers.find_one returns None (worker deleted or missing),
        no SMS must be sent.
        """
        company = _make_company(first_reminder_minutes=240)
        entry = _make_entry_record(hours_ago=5.0)
        mock_sms = self._make_sms()

        with patch("api.services.scheduler_service.db") as mock_db, \
                _patch_datetime_now(NOW_UTC):
            mock_db.TimeRecords.find.return_value = _async_cursor([entry])
            mock_db.TimeRecords.find_one = AsyncMock(return_value=None)
            mock_db.SmsLogs.count_documents = AsyncMock(return_value=0)
            mock_db.Workers.find_one = AsyncMock(return_value=None)  # not found

            await scheduler._process_company_sms(company, mock_sms)

        mock_sms.send_shift_reminder.assert_not_called()

    # ------------------------------------------------------------------
    # Edge: exactly at the first_reminder boundary sends SMS
    # ------------------------------------------------------------------

    async def test_entry_exactly_at_threshold_sends_sms(self, scheduler):
        """
        Elapsed time equals first_reminder_minutes exactly (4.0 h).
        The guard is ``hours_elapsed < first_after_hours`` (strict less-than),
        so an entry that is precisely 4 h old with a 4 h threshold passes and
        an SMS must be sent.
        """
        company = _make_company(first_reminder_minutes=240)
        entry = _make_entry_record(hours_ago=4.0)
        worker = _make_worker()
        mock_sms = self._make_sms()

        with patch("api.services.scheduler_service.db") as mock_db, \
                _patch_datetime_now(NOW_UTC):
            mock_db.TimeRecords.find.return_value = _async_cursor([entry])
            mock_db.TimeRecords.find_one = AsyncMock(return_value=None)
            mock_db.SmsLogs.count_documents = AsyncMock(return_value=0)
            mock_db.Workers.find_one = AsyncMock(return_value=worker)

            await scheduler._process_company_sms(company, mock_sms)

        mock_sms.send_shift_reminder.assert_awaited_once()

    # ------------------------------------------------------------------
    # Edge: worker_name and company_name forwarded correctly
    # ------------------------------------------------------------------

    async def test_send_reminder_uses_full_worker_name(self, scheduler):
        """
        The worker_name passed to send_shift_reminder must be
        ``f"{first_name} {last_name}".strip()`` and company_name must match.
        """
        company = _make_company(first_reminder_minutes=240)
        entry = _make_entry_record(hours_ago=5.0)
        worker = _make_worker(first_name="Carlos", last_name="Lopez")
        mock_sms = self._make_sms()

        with patch("api.services.scheduler_service.db") as mock_db, \
                _patch_datetime_now(NOW_UTC):
            mock_db.TimeRecords.find.return_value = _async_cursor([entry])
            mock_db.TimeRecords.find_one = AsyncMock(return_value=None)
            mock_db.SmsLogs.count_documents = AsyncMock(return_value=0)
            mock_db.Workers.find_one = AsyncMock(return_value=worker)

            await scheduler._process_company_sms(company, mock_sms)

        kwargs = mock_sms.send_shift_reminder.call_args.kwargs
        assert kwargs["worker_name"] == "Carlos Lopez"
        assert kwargs["company_name"] == "Test Company SL"
