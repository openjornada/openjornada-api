"""
Unit tests for the OpenJornada reports module.

All tests are pure unit tests — no MongoDB or HTTP server required.
External dependencies (database, authentication) are fully mocked.
"""

import hashlib
import json
from datetime import date, datetime, timezone as dt_timezone
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytz
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# Module imports
# ---------------------------------------------------------------------------
from api.auth.permissions import ROLE_PERMISSIONS, has_permission
from api.models.auth import APIUser
from api.models.reports import (
    DailyWorkSummary,
    ExportFormat,
    ReportFilters,
    WorkerMonthlySummary,
    CompanyMonthlySummary,
    WorkerReportRequest,
)
from api.services.export_service import ExportService
from api.services.integrity_service import IntegrityService
from api.services.report_service import ReportService, ensure_utc_aware


# ===========================================================================
# Helpers / Fixtures
# ===========================================================================


def _make_utc(year: int, month: int, day: int, hour: int = 8, minute: int = 0) -> datetime:
    """Create a UTC-aware datetime for a given date and time."""
    return datetime(year, month, day, hour, minute, 0, tzinfo=dt_timezone.utc)


def _make_daily_summary(
    work_date: date = date(2026, 1, 15),
    total_worked_minutes: float = 480.0,
    total_pause_minutes: float = 30.0,
    total_break_minutes: float = 0.0,
    has_open_session: bool = False,
    is_modified: bool = False,
    first_entry: Optional[datetime] = None,
    last_exit: Optional[datetime] = None,
) -> DailyWorkSummary:
    """Create a DailyWorkSummary with sensible test defaults."""
    if first_entry is None:
        first_entry = _make_utc(2026, 1, 15, 8, 0)
    if last_exit is None:
        last_exit = _make_utc(2026, 1, 15, 16, 30)
    return DailyWorkSummary(
        date=work_date,
        worker_id="worker_001",
        worker_name="Ana García",
        worker_id_number="12345678A",
        company_id="company_001",
        company_name="Empresa Test SL",
        first_entry=first_entry,
        last_exit=last_exit,
        total_worked_minutes=total_worked_minutes,
        total_pause_minutes=total_pause_minutes,
        total_break_minutes=total_break_minutes,
        has_open_session=has_open_session,
        is_modified=is_modified,
    )


def _make_worker_summary(
    daily_details: Optional[list] = None,
    total_worked_minutes: float = 960.0,
    total_days_worked: int = 2,
) -> WorkerMonthlySummary:
    """Create a WorkerMonthlySummary for testing."""
    if daily_details is None:
        daily_details = [_make_daily_summary()]
    return WorkerMonthlySummary(
        worker_id="worker_001",
        worker_name="Ana García",
        worker_id_number="12345678A",
        company_id="company_001",
        company_name="Empresa Test SL",
        year=2026,
        month=1,
        total_days_worked=total_days_worked,
        total_worked_minutes=total_worked_minutes,
        total_pause_minutes=30.0,
        total_overtime_minutes=0.0,
        daily_details=daily_details,
        signature_status="pending",
        generated_at=_make_utc(2026, 2, 1, 12, 0),
    )


def _make_company_summary(
    workers: Optional[list] = None,
) -> CompanyMonthlySummary:
    """Create a CompanyMonthlySummary for testing."""
    if workers is None:
        workers = [_make_worker_summary()]
    return CompanyMonthlySummary(
        company_id="company_001",
        company_name="Empresa Test SL",
        year=2026,
        month=1,
        total_workers=len(workers),
        workers=workers,
        generated_at=_make_utc(2026, 2, 1, 12, 0),
    )


# ===========================================================================
# TestIntegrityService
# ===========================================================================


class TestIntegrityService:
    """Tests for IntegrityService SHA-256 hashing logic."""

    def test_compute_record_hash_deterministic(self):
        """Same record always produces the same hash."""
        record = {
            "worker_id": "abc123",
            "company_id": "comp456",
            "type": "entry",
            "timestamp": _make_utc(2026, 1, 15, 8, 0),
            "duration_minutes": None,
            "created_at": _make_utc(2026, 1, 15, 8, 0),
        }
        hash1 = IntegrityService.compute_record_hash(record)
        hash2 = IntegrityService.compute_record_hash(record)
        assert hash1 == hash2

    def test_compute_record_hash_different_records(self):
        """Different records produce different hashes."""
        base = {
            "worker_id": "abc123",
            "company_id": "comp456",
            "type": "entry",
            "timestamp": _make_utc(2026, 1, 15, 8, 0),
            "duration_minutes": None,
            "created_at": _make_utc(2026, 1, 15, 8, 0),
        }
        record_exit = dict(base, type="exit", duration_minutes=480.0)
        assert IntegrityService.compute_record_hash(base) != IntegrityService.compute_record_hash(record_exit)

    def test_compute_record_hash_handles_none_fields(self):
        """Records with missing/None fields still produce a valid hex digest."""
        record: dict = {}
        result = IntegrityService.compute_record_hash(record)
        # Must be a valid 64-char lowercase hex string (SHA-256)
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_compute_record_hash_is_sha256(self):
        """Verify the hash matches a manually computed SHA-256 for known input."""
        record = {
            "worker_id": "w1",
            "company_id": "c1",
            "type": "entry",
            "timestamp": None,
            "duration_minutes": None,
            "created_at": None,
        }
        # Build the expected canonical JSON the same way the service does
        payload = {field: record.get(field) for field in (
            "worker_id", "company_id", "type", "timestamp", "duration_minutes", "created_at"
        )}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        assert IntegrityService.compute_record_hash(record) == expected

    def test_compute_report_hash(self):
        """PDF/CSV bytes produce a valid 64-char hex SHA-256 string."""
        data = b"fake pdf content"
        result = IntegrityService.compute_report_hash(data)
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)
        # Verify correctness
        assert result == hashlib.sha256(data).hexdigest()

    def test_compute_report_hash_different_data(self):
        """Different byte content produces different hashes."""
        hash1 = IntegrityService.compute_report_hash(b"content_A")
        hash2 = IntegrityService.compute_report_hash(b"content_B")
        assert hash1 != hash2

    def test_compute_report_hash_empty_bytes(self):
        """Empty bytes produce the SHA-256 of empty string (not an error)."""
        result = IntegrityService.compute_report_hash(b"")
        expected = hashlib.sha256(b"").hexdigest()
        assert result == expected


# ===========================================================================
# TestReportModels
# ===========================================================================


class TestReportModels:
    """Tests for Pydantic model validation and computed properties."""

    def test_daily_work_summary_defaults(self):
        """DailyWorkSummary has correct zero-value defaults."""
        summary = DailyWorkSummary(
            date=date(2026, 1, 1),
            worker_id="w1",
            worker_name="Worker One",
            worker_id_number="11111111A",
            company_id="c1",
            company_name="Company One",
        )
        assert summary.total_worked_minutes == 0.0
        assert summary.total_pause_minutes == 0.0
        assert summary.total_break_minutes == 0.0
        assert summary.records_count == 0
        assert summary.has_open_session is False
        assert summary.is_modified is False
        assert summary.first_entry is None
        assert summary.last_exit is None

    def test_worker_monthly_summary_total_worked_hours(self):
        """total_worked_hours property converts minutes to hours correctly."""
        summary = _make_worker_summary(total_worked_minutes=480.0)
        assert summary.total_worked_hours == 8.0

    def test_worker_monthly_summary_total_worked_hours_rounding(self):
        """total_worked_hours rounds to 2 decimal places."""
        summary = _make_worker_summary(total_worked_minutes=100.0)
        # 100 / 60 = 1.6666... -> rounds to 1.67
        assert summary.total_worked_hours == 1.67

    def test_worker_monthly_summary_zero_minutes(self):
        """total_worked_hours returns 0.0 when total_worked_minutes is 0."""
        summary = _make_worker_summary(total_worked_minutes=0.0)
        assert summary.total_worked_hours == 0.0

    def test_export_format_values(self):
        """ExportFormat enum contains csv, xlsx, and pdf values."""
        assert ExportFormat.CSV.value == "csv"
        assert ExportFormat.XLSX.value == "xlsx"
        assert ExportFormat.PDF.value == "pdf"

    def test_export_format_is_string_enum(self):
        """ExportFormat is a str subclass — can be compared to strings."""
        assert ExportFormat.CSV == "csv"
        assert ExportFormat.PDF == "pdf"

    def test_report_filters_valid(self):
        """ReportFilters accepts valid year and month."""
        rf = ReportFilters(company_id="c1", year=2026, month=3)
        assert rf.year == 2026
        assert rf.month == 3
        assert rf.timezone == "Europe/Madrid"

    def test_report_filters_year_too_low(self):
        """ReportFilters rejects year below 2020."""
        with pytest.raises(ValidationError):
            ReportFilters(company_id="c1", year=2019, month=1)

    def test_report_filters_year_too_high(self):
        """ReportFilters rejects year above 2035."""
        with pytest.raises(ValidationError):
            ReportFilters(company_id="c1", year=2036, month=1)

    def test_report_filters_month_out_of_range(self):
        """ReportFilters rejects month=0 and month=13."""
        with pytest.raises(ValidationError):
            ReportFilters(company_id="c1", year=2026, month=0)
        with pytest.raises(ValidationError):
            ReportFilters(company_id="c1", year=2026, month=13)

    def test_worker_report_request_email_validation(self):
        """WorkerReportRequest rejects invalid email addresses."""
        with pytest.raises(ValidationError):
            WorkerReportRequest(
                email="not-an-email",
                password="secret",
                company_id="c1",
                year=2026,
                month=1,
            )

    def test_worker_report_request_valid(self):
        """WorkerReportRequest accepts a valid email."""
        req = WorkerReportRequest(
            email="worker@example.com",
            password="secret",
            company_id="c1",
            year=2026,
            month=1,
        )
        assert str(req.email) == "worker@example.com"


# ===========================================================================
# TestEnsureUtcAware
# ===========================================================================


class TestEnsureUtcAware:
    """Tests for the ensure_utc_aware helper in report_service."""

    def test_none_returns_none(self):
        assert ensure_utc_aware(None) is None

    def test_naive_datetime_gets_utc_tzinfo(self):
        naive = datetime(2026, 1, 15, 8, 0, 0)
        result = ensure_utc_aware(naive)
        assert result.tzinfo == dt_timezone.utc

    def test_aware_datetime_unchanged(self):
        tz = pytz.timezone("Europe/Madrid")
        aware = tz.localize(datetime(2026, 1, 15, 9, 0, 0))
        result = ensure_utc_aware(aware)
        # Must preserve original tzinfo, not strip it
        assert result.tzinfo is not None
        assert result == aware


# ===========================================================================
# TestProcessDayRecords
# ===========================================================================


class TestProcessDayRecords:
    """Tests for ReportService._process_day_records (private method, tested directly)."""

    _worker_info = {"worker_id": "w1", "worker_name": "Test Worker", "worker_id_number": "11111111A"}
    _company_info = {"company_id": "c1", "company_name": "Test Company"}

    def _call(self, records: list[dict], target_date: date = date(2026, 1, 15)) -> DailyWorkSummary:
        svc = ReportService()
        return svc._process_day_records(records, target_date, self._worker_info, self._company_info)

    def test_simple_entry_exit(self):
        """Single entry-exit pair sets first_entry, last_exit and total_worked_minutes."""
        entry_ts = _make_utc(2026, 1, 15, 8, 0)
        exit_ts = _make_utc(2026, 1, 15, 16, 0)
        records = [
            {"type": "entry", "timestamp": entry_ts},
            {"type": "exit", "timestamp": exit_ts, "duration_minutes": 480.0},
        ]
        result = self._call(records)
        assert result.first_entry == entry_ts
        assert result.last_exit == exit_ts
        assert result.total_worked_minutes == 480.0
        assert result.has_open_session is False
        assert result.is_modified is False
        assert result.records_count == 2

    def test_entry_with_pause(self):
        """A pause_end record with pause_counts_as_work=False adds to total_pause_minutes."""
        records = [
            {"type": "entry", "timestamp": _make_utc(2026, 1, 15, 8, 0)},
            {"type": "pause_start", "timestamp": _make_utc(2026, 1, 15, 10, 0)},
            {
                "type": "pause_end",
                "timestamp": _make_utc(2026, 1, 15, 10, 30),
                "duration_minutes": 30.0,
                "pause_counts_as_work": False,
            },
            {"type": "exit", "timestamp": _make_utc(2026, 1, 15, 16, 0), "duration_minutes": 450.0},
        ]
        result = self._call(records)
        assert result.total_pause_minutes == 30.0
        assert result.total_break_minutes == 0.0
        assert result.total_worked_minutes == 450.0

    def test_entry_with_break_counted_as_work(self):
        """A pause_end with pause_counts_as_work=True adds to total_break_minutes, not pause."""
        records = [
            {"type": "entry", "timestamp": _make_utc(2026, 1, 15, 8, 0)},
            {
                "type": "pause_end",
                "timestamp": _make_utc(2026, 1, 15, 10, 30),
                "duration_minutes": 15.0,
                "pause_counts_as_work": True,
            },
            {"type": "exit", "timestamp": _make_utc(2026, 1, 15, 16, 0), "duration_minutes": 480.0},
        ]
        result = self._call(records)
        assert result.total_break_minutes == 15.0
        assert result.total_pause_minutes == 0.0

    def test_open_session(self):
        """Entry without exit sets has_open_session=True."""
        records = [
            {"type": "entry", "timestamp": _make_utc(2026, 1, 15, 8, 0)},
        ]
        result = self._call(records)
        assert result.has_open_session is True
        assert result.last_exit is None
        assert result.total_worked_minutes == 0.0

    def test_open_session_with_pause(self):
        """Entry + pause_end without exit is still an open session."""
        records = [
            {"type": "entry", "timestamp": _make_utc(2026, 1, 15, 8, 0)},
            {
                "type": "pause_end",
                "timestamp": _make_utc(2026, 1, 15, 10, 30),
                "duration_minutes": 30.0,
                "pause_counts_as_work": False,
            },
        ]
        result = self._call(records)
        assert result.has_open_session is True

    def test_modified_record(self):
        """Any record with modified_by_admin_id sets is_modified=True."""
        records = [
            {"type": "entry", "timestamp": _make_utc(2026, 1, 15, 8, 0), "modified_by_admin_id": "admin_99"},
            {"type": "exit", "timestamp": _make_utc(2026, 1, 15, 16, 0), "duration_minutes": 480.0},
        ]
        result = self._call(records)
        assert result.is_modified is True

    def test_unmodified_record(self):
        """Records without modified_by_admin_id keep is_modified=False."""
        records = [
            {"type": "entry", "timestamp": _make_utc(2026, 1, 15, 8, 0)},
            {"type": "exit", "timestamp": _make_utc(2026, 1, 15, 16, 0), "duration_minutes": 480.0},
        ]
        result = self._call(records)
        assert result.is_modified is False

    def test_multiple_sessions_in_day(self):
        """Multiple entry-exit pairs: worked minutes are summed; first_entry is earliest entry."""
        records = [
            {"type": "entry", "timestamp": _make_utc(2026, 1, 15, 8, 0)},
            {"type": "exit", "timestamp": _make_utc(2026, 1, 15, 12, 0), "duration_minutes": 240.0},
            {"type": "entry", "timestamp": _make_utc(2026, 1, 15, 13, 0)},
            {"type": "exit", "timestamp": _make_utc(2026, 1, 15, 17, 0), "duration_minutes": 240.0},
        ]
        result = self._call(records)
        assert result.total_worked_minutes == 480.0
        assert result.first_entry == _make_utc(2026, 1, 15, 8, 0)
        assert result.last_exit == _make_utc(2026, 1, 15, 17, 0)
        assert result.has_open_session is False

    def test_empty_records_list(self):
        """Empty records list produces a zeroed-out DailyWorkSummary."""
        result = self._call([])
        assert result.total_worked_minutes == 0.0
        assert result.records_count == 0
        assert result.has_open_session is False

    def test_exit_without_duration_is_ignored(self):
        """An exit record without duration_minutes does not add worked time."""
        records = [
            {"type": "entry", "timestamp": _make_utc(2026, 1, 15, 8, 0)},
            {"type": "exit", "timestamp": _make_utc(2026, 1, 15, 16, 0)},  # no duration_minutes
        ]
        result = self._call(records)
        assert result.total_worked_minutes == 0.0
        assert result.has_open_session is False


# ===========================================================================
# TestGroupRecordsByDay
# ===========================================================================


class TestGroupRecordsByDay:
    """Tests for ReportService._group_records_by_day timezone handling."""

    def _call(self, records: list[dict], tz_name: str = "Europe/Madrid") -> dict:
        svc = ReportService()
        tz = pytz.timezone(tz_name)
        return svc._group_records_by_day(records, tz)

    def test_groups_by_local_date(self):
        """Records on the same UTC day are grouped under the same local date."""
        records = [
            {"type": "entry", "timestamp": _make_utc(2026, 1, 15, 8, 0)},
            {"type": "exit", "timestamp": _make_utc(2026, 1, 15, 16, 0), "duration_minutes": 480.0},
        ]
        grouped = self._call(records, "UTC")
        assert date(2026, 1, 15) in grouped
        assert len(grouped[date(2026, 1, 15)]) == 2

    def test_midnight_crossing(self):
        """UTC 23:30 on Jan 14 is Jan 15 in CET (UTC+1)."""
        records = [
            # 23:30 UTC on Jan 14 = 00:30 CET on Jan 15
            {"type": "entry", "timestamp": _make_utc(2026, 1, 14, 23, 30)},
        ]
        grouped = self._call(records, "Europe/Madrid")
        # In CET (UTC+1 in January), 23:30 UTC = 00:30 next day
        assert date(2026, 1, 15) in grouped
        assert date(2026, 1, 14) not in grouped

    def test_same_utc_date_different_local_dates(self):
        """UTC midnight record in UTC+1 timezone falls on the previous local day."""
        records = [
            # 23:00 UTC = 00:00 CET next day
            {"type": "entry", "timestamp": _make_utc(2026, 1, 14, 23, 0)},
            # 08:00 UTC next day = 09:00 CET same day
            {"type": "exit", "timestamp": _make_utc(2026, 1, 15, 8, 0), "duration_minutes": 540.0},
        ]
        grouped = self._call(records, "Europe/Madrid")
        # Entry at 23:00 UTC on Jan 14 → local 00:00 on Jan 15 (CET)
        # Exit at 08:00 UTC on Jan 15 → local 09:00 on Jan 15 (CET)
        assert date(2026, 1, 15) in grouped
        assert len(grouped[date(2026, 1, 15)]) == 2

    def test_record_missing_timestamp_is_skipped(self):
        """Records without a timestamp field are silently skipped."""
        records = [
            {"type": "entry"},  # no timestamp key
            {"type": "exit", "timestamp": _make_utc(2026, 1, 15, 16, 0), "duration_minutes": 480.0},
        ]
        grouped = self._call(records, "UTC")
        # Only the record with a timestamp should appear
        total_records = sum(len(v) for v in grouped.values())
        assert total_records == 1

    def test_multiple_days_separated(self):
        """Records on different local days end up in separate groups."""
        records = [
            {"type": "entry", "timestamp": _make_utc(2026, 1, 15, 8, 0)},
            {"type": "exit", "timestamp": _make_utc(2026, 1, 15, 16, 0), "duration_minutes": 480.0},
            {"type": "entry", "timestamp": _make_utc(2026, 1, 16, 8, 0)},
            {"type": "exit", "timestamp": _make_utc(2026, 1, 16, 16, 0), "duration_minutes": 480.0},
        ]
        grouped = self._call(records, "UTC")
        assert date(2026, 1, 15) in grouped
        assert date(2026, 1, 16) in grouped
        assert len(grouped) == 2


# ===========================================================================
# TestExportService
# ===========================================================================


class TestExportService:
    """Tests for ExportService CSV/XLSX/PDF generation."""

    @pytest.mark.asyncio
    async def test_export_csv_returns_bytes(self):
        """CSV export returns a non-empty BytesIO buffer."""
        import io
        svc = ExportService()
        summary = _make_worker_summary()
        result = await svc.export_monthly_csv(summary)
        assert isinstance(result, io.BytesIO)
        content = result.read()
        assert len(content) > 0

    @pytest.mark.asyncio
    async def test_export_csv_semicolon_separator(self):
        """CSV uses semicolon as column separator and UTF-8 BOM."""
        svc = ExportService()
        summary = _make_worker_summary()
        buf = await svc.export_monthly_csv(summary)
        raw_bytes = buf.read()
        # UTF-8 BOM is the first 3 bytes: EF BB BF
        assert raw_bytes[:3] == b"\xef\xbb\xbf"
        # Decode and verify semicolons in the header
        text = raw_bytes.decode("utf-8-sig")
        header_line = text.splitlines()[0]
        assert ";" in header_line

    @pytest.mark.asyncio
    async def test_export_csv_header_columns(self):
        """CSV header contains expected Spanish column names."""
        svc = ExportService()
        summary = _make_worker_summary()
        buf = await svc.export_monthly_csv(summary)
        text = buf.read().decode("utf-8-sig")
        header = text.splitlines()[0]
        assert "Fecha" in header
        assert "Nombre" in header
        assert "Horas Trabajadas" in header

    @pytest.mark.asyncio
    async def test_export_csv_data_row_present(self):
        """CSV contains at least one data row for a summary with daily_details."""
        svc = ExportService()
        daily = _make_daily_summary()
        summary = _make_worker_summary(daily_details=[daily])
        buf = await svc.export_monthly_csv(summary)
        text = buf.read().decode("utf-8-sig")
        lines = [l for l in text.splitlines() if l.strip()]
        # header + at least 1 data row
        assert len(lines) >= 2

    @pytest.mark.asyncio
    async def test_export_csv_company_summary(self):
        """CSV export also works for CompanyMonthlySummary input."""
        import io
        svc = ExportService()
        summary = _make_company_summary()
        result = await svc.export_monthly_csv(summary)
        assert isinstance(result, io.BytesIO)
        assert result.read() != b""

    @pytest.mark.asyncio
    async def test_export_xlsx_returns_bytes(self):
        """XLSX export returns a non-empty BytesIO buffer."""
        import io
        svc = ExportService()
        summary = _make_worker_summary()
        result = await svc.export_monthly_xlsx(summary)
        assert isinstance(result, io.BytesIO)
        content = result.read()
        assert len(content) > 0

    @pytest.mark.asyncio
    async def test_export_xlsx_valid_zip_signature(self):
        """XLSX is a ZIP file — verify the PK magic bytes."""
        svc = ExportService()
        summary = _make_worker_summary()
        buf = await svc.export_monthly_xlsx(summary)
        content = buf.read()
        # XLSX (ZIP) starts with PK\x03\x04
        assert content[:4] == b"PK\x03\x04"

    @pytest.mark.asyncio
    async def test_export_xlsx_company_summary(self):
        """XLSX export works for CompanyMonthlySummary input."""
        import io
        svc = ExportService()
        summary = _make_company_summary()
        result = await svc.export_monthly_xlsx(summary)
        assert isinstance(result, io.BytesIO)
        assert result.read() != b""

    @pytest.mark.asyncio
    async def test_export_pdf_returns_bytes(self):
        """PDF export returns a non-empty BytesIO buffer."""
        import io
        svc = ExportService()
        summary = _make_worker_summary()
        result = await svc.export_monthly_pdf(summary)
        assert isinstance(result, io.BytesIO)
        content = result.read()
        assert len(content) > 0

    @pytest.mark.asyncio
    async def test_export_pdf_starts_with_pdf_magic(self):
        """PDF output starts with the %PDF magic header."""
        svc = ExportService()
        summary = _make_worker_summary()
        buf = await svc.export_monthly_pdf(summary)
        content = buf.read()
        assert content[:4] == b"%PDF"

    @pytest.mark.asyncio
    async def test_export_pdf_company_summary(self):
        """PDF export works for CompanyMonthlySummary input."""
        import io
        svc = ExportService()
        summary = _make_company_summary()
        result = await svc.export_monthly_pdf(summary)
        assert isinstance(result, io.BytesIO)
        content = result.read()
        assert content[:4] == b"%PDF"

    @pytest.mark.asyncio
    async def test_export_csv_buffer_seeked_to_zero(self):
        """CSV BytesIO buffer is positioned at byte 0 after export."""
        svc = ExportService()
        summary = _make_worker_summary()
        buf = await svc.export_monthly_csv(summary)
        assert buf.tell() == 0

    @pytest.mark.asyncio
    async def test_export_xlsx_buffer_seeked_to_zero(self):
        """XLSX BytesIO buffer is positioned at byte 0 after export."""
        svc = ExportService()
        summary = _make_worker_summary()
        buf = await svc.export_monthly_xlsx(summary)
        assert buf.tell() == 0

    @pytest.mark.asyncio
    async def test_export_pdf_buffer_seeked_to_zero(self):
        """PDF BytesIO buffer is positioned at byte 0 after export."""
        svc = ExportService()
        summary = _make_worker_summary()
        buf = await svc.export_monthly_pdf(summary)
        assert buf.tell() == 0


# ===========================================================================
# TestReportPermissions
# ===========================================================================


class TestReportPermissions:
    """Tests for ROLE_PERMISSIONS and has_permission for report-related permissions."""

    def _make_user(self, role: str) -> APIUser:
        return APIUser(username="testuser", email="test@example.com", role=role)

    # --- Admin ---

    def test_admin_has_view_reports(self):
        """Admin role includes view_reports permission."""
        assert "view_reports" in ROLE_PERMISSIONS["admin"]

    def test_admin_has_export_reports(self):
        """Admin role includes export_reports permission."""
        assert "export_reports" in ROLE_PERMISSIONS["admin"]

    def test_admin_has_manage_inspection(self):
        """Admin role includes manage_inspection permission."""
        assert "manage_inspection" in ROLE_PERMISSIONS["admin"]

    def test_admin_has_permission_view_reports(self):
        """has_permission returns True for admin + view_reports."""
        user = self._make_user("admin")
        assert has_permission(user, "view_reports") is True

    # --- Inspector ---

    def test_inspector_has_view_reports(self):
        """Inspector role includes view_reports permission."""
        assert "view_reports" in ROLE_PERMISSIONS["inspector"]

    def test_inspector_has_export(self):
        """Inspector role includes export_reports permission."""
        assert "export_reports" in ROLE_PERMISSIONS["inspector"]

    def test_inspector_has_view_companies(self):
        """Inspector role includes view_companies permission."""
        assert "view_companies" in ROLE_PERMISSIONS["inspector"]

    def test_inspector_no_manage_inspection(self):
        """Inspector role does NOT include manage_inspection."""
        assert "manage_inspection" not in ROLE_PERMISSIONS["inspector"]

    def test_inspector_no_create_users(self):
        """Inspector role does NOT include create_users permission."""
        assert "create_users" not in ROLE_PERMISSIONS["inspector"]

    def test_inspector_no_delete_workers(self):
        """Inspector role does NOT include delete_workers permission."""
        assert "delete_workers" not in ROLE_PERMISSIONS["inspector"]

    def test_inspector_has_permission_view_reports(self):
        """has_permission returns True for inspector + view_reports."""
        user = self._make_user("inspector")
        assert has_permission(user, "view_reports") is True

    def test_inspector_has_permission_export_reports(self):
        """has_permission returns True for inspector + export_reports."""
        user = self._make_user("inspector")
        assert has_permission(user, "export_reports") is True

    def test_inspector_no_permission_manage_inspection(self):
        """has_permission returns False for inspector + manage_inspection."""
        user = self._make_user("inspector")
        assert has_permission(user, "manage_inspection") is False

    # --- Tracker ---

    def test_tracker_no_view_reports(self):
        """Tracker role does NOT include view_reports."""
        assert "view_reports" not in ROLE_PERMISSIONS["tracker"]

    def test_tracker_no_export_reports(self):
        """Tracker role does NOT include export_reports."""
        assert "export_reports" not in ROLE_PERMISSIONS["tracker"]

    def test_tracker_has_permission_false_view_reports(self):
        """has_permission returns False for tracker + view_reports."""
        user = self._make_user("tracker")
        assert has_permission(user, "view_reports") is False

    # --- Unknown role ---

    def test_unknown_role_has_no_permissions(self):
        """A user with an unknown role has no permissions."""
        user = APIUser(username="x", email="x@example.com", role="tracker")
        # Monkey-patch role to simulate unknown (bypass Literal validation)
        user.__dict__["role"] = "hacker"
        assert has_permission(user, "view_reports") is False
        assert has_permission(user, "export_reports") is False
