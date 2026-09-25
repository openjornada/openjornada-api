"""
Unit tests for the OpenJornada reports module.

All tests are pure unit tests — no MongoDB or HTTP server required.
External dependencies (database, authentication) are fully mocked.
"""

import hashlib
import json
import logging
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
    MonthlySignatureRequest,
    MonthlySignatureVerification,
    ReportFilters,
    WorkerMonthlySummary,
    CompanyMonthlySummary,
    WorkerExportRequest,
    WorkerReportRequest,
)
from api.services.export_service import ExportService
from api.services.integrity_service import MONTHLY_DIGEST_VERSION, IntegrityService
from api.services.report_service import ReportService, ensure_utc_aware
from api.utils.timezones import DEFAULT_REPORT_TIMEZONE, resolve_worker_timezone


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

    def test_compute_record_hash_naive_and_aware_utc_equal(self):
        """The same instant hashes identically whether naive or UTC-aware.

        Regression: Motor may return naive datetimes for values that were
        written as UTC-aware (datetime.now(timezone.utc)). Both must produce
        the same digest or the hash breaks on every write->read round-trip.
        """
        aware = datetime(2026, 1, 15, 8, 0, 0, tzinfo=dt_timezone.utc)
        naive = datetime(2026, 1, 15, 8, 0, 0)  # same instant, no tzinfo
        record_aware = {
            "worker_id": "w1", "company_id": "c1", "type": "entry",
            "timestamp": aware, "duration_minutes": None, "created_at": aware,
        }
        record_naive = {
            "worker_id": "w1", "company_id": "c1", "type": "entry",
            "timestamp": naive, "duration_minutes": None, "created_at": naive,
        }
        assert IntegrityService.compute_record_hash(record_aware) == IntegrityService.compute_record_hash(record_naive)

    def test_compute_record_hash_stable_across_mongo_round_trip(self):
        """Hash computed pre-insert equals hash recomputed post read-back.

        Simulates MongoDB's actual behaviour: BSON dates only have
        millisecond precision, and Motor returns naive UTC datetimes. A
        microsecond-precision, timezone-aware Python datetime (as produced by
        datetime.now(timezone.utc)) must still hash the same before and
        after that round-trip.
        """
        write_time = datetime(2026, 1, 15, 8, 0, 0, 123456, tzinfo=dt_timezone.utc)
        record_at_write = {
            "worker_id": "w1", "company_id": "c1", "type": "entry",
            "timestamp": write_time, "duration_minutes": None, "created_at": write_time,
        }
        hash_at_write = IntegrityService.compute_record_hash(record_at_write)

        # MongoDB truncates to millisecond precision and returns naive UTC.
        read_back_time = datetime(2026, 1, 15, 8, 0, 0, 123000)
        record_after_read = {
            "worker_id": "w1", "company_id": "c1", "type": "entry",
            "timestamp": read_back_time, "duration_minutes": None, "created_at": read_back_time,
        }
        hash_after_read = IntegrityService.compute_record_hash(record_after_read)

        assert hash_at_write == hash_after_read

    async def test_verify_record_integrity_verified(self):
        """Untampered record with a matching stored hash verifies true."""
        record = {
            "_id": "fake_object_id",
            "worker_id": "w1", "company_id": "c1", "type": "entry",
            "timestamp": _make_utc(2026, 1, 15, 8, 0), "duration_minutes": None,
            "created_at": _make_utc(2026, 1, 15, 8, 0),
        }
        record["integrity_hash"] = IntegrityService.compute_record_hash(record)

        with patch("api.services.integrity_service.db") as mock_db, \
             patch("api.services.integrity_service.ObjectId", return_value="fake_object_id"):
            mock_db.TimeRecords.find_one = AsyncMock(return_value=record)
            result = await IntegrityService.verify_record_integrity("507f1f77bcf86cd799439011")

        assert result["verified"] is True
        assert result["status"] == "verified"
        assert result["stored_hash"] == result["computed_hash"]

    async def test_verify_record_integrity_tampered(self):
        """A stored hash that no longer matches the recomputed one reports tampered."""
        record = {
            "_id": "fake_object_id",
            "worker_id": "w1", "company_id": "c1", "type": "entry",
            "timestamp": _make_utc(2026, 1, 15, 8, 0), "duration_minutes": None,
            "created_at": _make_utc(2026, 1, 15, 8, 0),
            "integrity_hash": "deadbeef" * 8,  # deliberately wrong
        }

        with patch("api.services.integrity_service.db") as mock_db, \
             patch("api.services.integrity_service.ObjectId", return_value="fake_object_id"):
            mock_db.TimeRecords.find_one = AsyncMock(return_value=record)
            result = await IntegrityService.verify_record_integrity("507f1f77bcf86cd799439011")

        assert result["verified"] is False
        assert result["status"] == "tampered"

    async def test_verify_record_integrity_legacy(self):
        """A record with no stored integrity_hash reports legacy, not tampered."""
        record = {
            "_id": "fake_object_id",
            "worker_id": "w1", "company_id": "c1", "type": "entry",
            "timestamp": _make_utc(2026, 1, 15, 8, 0), "duration_minutes": None,
            "created_at": _make_utc(2026, 1, 15, 8, 0),
            # no "integrity_hash" key at all
        }

        with patch("api.services.integrity_service.db") as mock_db, \
             patch("api.services.integrity_service.ObjectId", return_value="fake_object_id"):
            mock_db.TimeRecords.find_one = AsyncMock(return_value=record)
            result = await IntegrityService.verify_record_integrity("507f1f77bcf86cd799439011")

        assert result["verified"] is False
        assert result["status"] == "legacy"
        assert result["stored_hash"] == ""

    async def test_verify_record_integrity_not_found(self):
        """Verifying a non-existent record raises 404."""
        from fastapi import HTTPException

        with patch("api.services.integrity_service.db") as mock_db, \
             patch("api.services.integrity_service.ObjectId", return_value="fake_object_id"):
            mock_db.TimeRecords.find_one = AsyncMock(return_value=None)
            with pytest.raises(HTTPException) as exc_info:
                await IntegrityService.verify_record_integrity("507f1f77bcf86cd799439011")

        assert exc_info.value.status_code == 404

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

    # -- monthly digest -----------------------------------------------------

    @staticmethod
    def _monthly_records() -> list[dict]:
        """Two distinct time records plus one pause, as raw Mongo documents."""
        return [
            {
                "worker_id": "w1", "company_id": "c1", "type": "entry",
                "timestamp": _make_utc(2026, 1, 15, 8, 0), "duration_minutes": None,
                "created_at": _make_utc(2026, 1, 15, 8, 0),
            },
            {
                "worker_id": "w1", "company_id": "c1", "type": "exit",
                "timestamp": _make_utc(2026, 1, 15, 16, 30), "duration_minutes": 480.0,
                "created_at": _make_utc(2026, 1, 15, 16, 30),
            },
            {
                "worker_id": "w1", "company_id": "c1", "type": "pause_start",
                "timestamp": _make_utc(2026, 1, 14, 13, 0), "duration_minutes": None,
                "created_at": _make_utc(2026, 1, 14, 13, 0),
            },
        ]

    def test_compute_month_digest_deterministic(self):
        """Same record set always produces the same digest and count."""
        records = self._monthly_records()
        digest1, count1 = IntegrityService.compute_month_digest(records)
        digest2, count2 = IntegrityService.compute_month_digest([dict(r) for r in records])
        assert digest1 == digest2
        assert count1 == count2 == 3
        assert len(digest1) == 64

        # Preimage is the version prefix + sorted leaf digests joined by \n
        leaves = sorted(IntegrityService.compute_record_hash(r) for r in records)
        preimage = f"{MONTHLY_DIGEST_VERSION}\n" + "\n".join(leaves)
        assert digest1 == hashlib.sha256(preimage.encode("utf-8")).hexdigest()

    def test_compute_month_digest_order_independent(self):
        """Retrieval order never changes the digest."""
        records = self._monthly_records()
        digest, count = IntegrityService.compute_month_digest(records)
        assert IntegrityService.compute_month_digest(list(reversed(records))) == (digest, count)
        # A different permutation (sorted by timestamp descending) too.
        permuted = sorted(records, key=lambda r: r["timestamp"], reverse=True)
        assert IntegrityService.compute_month_digest(permuted) == (digest, count)

    def test_compute_month_digest_empty_set(self):
        """The empty set hashes the bare version prefix; count is 0."""
        digest, count = IntegrityService.compute_month_digest([])
        assert count == 0
        assert digest == hashlib.sha256(f"{MONTHLY_DIGEST_VERSION}\n".encode("utf-8")).hexdigest()

    def test_compute_month_digest_ignores_stored_integrity_hash(self):
        """Leaves are recomputed from current values, never read from the
        stored integrity_hash — a tampered record whose stored hash was also
        rewritten still produces the same digest as one without any hash."""
        base = self._monthly_records()[1]  # the exit record, has duration_minutes
        without_hash = IntegrityService.compute_month_digest([base])
        with_stale_hash = IntegrityService.compute_month_digest(
            [dict(base, integrity_hash="0" * 64)]
        )
        with_recomputed_hash = IntegrityService.compute_month_digest(
            [dict(base, integrity_hash=IntegrityService.compute_record_hash(base))]
        )
        assert without_hash == with_stale_hash == with_recomputed_hash

    def test_compute_month_digest_detects_field_edit(self):
        """Editing a hashed field of any record changes the digest."""
        records = self._monthly_records()
        digest, _ = IntegrityService.compute_month_digest(records)
        edited = [dict(records[1], duration_minutes=999.0), records[0], records[2]]
        edited_digest, edited_count = IntegrityService.compute_month_digest(edited)
        assert edited_digest != digest
        assert edited_count == 3

    async def test_get_month_records_uses_utc_window(self):
        """The fetcher queries worker+company over the month's UTC window,
        delegating the range to ReportService._month_utc_range."""
        captured: dict = {}

        class _Cursor:
            def sort(self, *_args, **_kwargs):
                return self

            async def to_list(self, _length):
                return []

        with patch("api.services.integrity_service.db") as mock_db:
            mock_db.TimeRecords.find = MagicMock(side_effect=lambda q: captured.update(query=q) or _Cursor())
            records = await IntegrityService.get_month_records(
                worker_id="w1", company_id="c1", year=2026, month=6, timezone="Europe/Madrid",
            )

        assert records == []
        query = captured["query"]
        assert query["worker_id"] == "w1"
        assert query["company_id"] == "c1"
        # June 2026 in Europe/Madrid (CEST, UTC+2): 2026-05-31T22:00Z .. 2026-06-30T22:00Z
        assert query["timestamp"]["$gte"] == datetime(2026, 5, 31, 22, 0, tzinfo=dt_timezone.utc)
        assert query["timestamp"]["$lt"] == datetime(2026, 6, 30, 22, 0, tzinfo=dt_timezone.utc)

    async def test_verify_monthly_signature_legacy(self):
        """A signature without content_hash reports legacy and recomputes nothing."""
        signature = {
            "_id": "fake_object_id",
            "worker_id": "w1", "company_id": "c1", "year": 2026, "month": 1,
            "signed_at": _make_utc(2026, 2, 1, 12, 0),
            # No content_hash: pre-capability signature.
        }

        with patch("api.services.integrity_service.db") as mock_db, \
             patch("api.services.integrity_service.ObjectId", return_value="fake_object_id"):
            mock_db.MonthlySignatures.find_one = AsyncMock(return_value=signature)
            # Legacy must not touch TimeRecords at all: any find() blows up.
            mock_db.TimeRecords.find = MagicMock(side_effect=AssertionError("legacy recomputed records"))
            result = await IntegrityService.verify_monthly_signature("507f1f77bcf86cd799439011")

        assert result["status"] == "legacy"
        assert result["content_hash"] == ""
        assert result["computed_hash"] == ""
        assert result["signed_record_count"] is None
        assert result["current_record_count"] is None
        assert result["audited_corrections"] == []

    async def test_verify_monthly_signature_verified(self):
        """Untouched month: recomputed digest matches, status verified."""
        records = self._monthly_records()
        digest, count = IntegrityService.compute_month_digest(records)
        signature = {
            "_id": "fake_object_id",
            "worker_id": "w1", "company_id": "c1", "year": 2026, "month": 1,
            "signed_at": _make_utc(2026, 2, 1, 12, 0),
            "content_hash": digest,
            "content_hash_version": MONTHLY_DIGEST_VERSION,
            "record_count": count,
            "timezone": "Europe/Madrid",
        }

        class _Cursor:
            def sort(self, *_args, **_kwargs):
                return self

            async def to_list(self, _length):
                return records

        with patch("api.services.integrity_service.db") as mock_db, \
             patch("api.services.integrity_service.ObjectId", return_value="fake_object_id"):
            mock_db.MonthlySignatures.find_one = AsyncMock(return_value=signature)
            mock_db.TimeRecords.find = MagicMock(return_value=_Cursor())
            result = await IntegrityService.verify_monthly_signature("507f1f77bcf86cd799439011")

        assert result["status"] == "verified"
        assert result["content_hash"] == result["computed_hash"] == digest
        assert result["signed_record_count"] == result["current_record_count"] == 3
        assert result["audited_corrections"] == []

    def test_compute_month_digest_rejects_an_unsupported_version(self):
        """A version this build cannot compute is a programming error, not a
        silently different digest."""
        with pytest.raises(ValueError, match="v99"):
            IntegrityService.compute_month_digest(self._monthly_records(), "v99")

    def test_compute_month_digest_version_is_the_preimage_prefix(self):
        """The version argument prefixes the preimage: the same records under a
        different version could never collide."""
        records = self._monthly_records()
        digest, _ = IntegrityService.compute_month_digest(records, MONTHLY_DIGEST_VERSION)
        leaves = sorted(IntegrityService.compute_record_hash(r) for r in records)
        preimage = f"{MONTHLY_DIGEST_VERSION}\n" + "\n".join(leaves)
        assert digest == hashlib.sha256(preimage.encode("utf-8")).hexdigest()
        # Same leaves, hypothetical future prefix -> different digest.
        other = hashlib.sha256(("v2\n" + "\n".join(leaves)).encode("utf-8")).hexdigest()
        assert digest != other

    async def test_verify_monthly_signature_unsupported_version(self):
        """A digest written by an unknown algorithm version reports
        unsupported_version and nothing is recomputed: calling it a mismatch
        would read as tampering that never happened."""
        signature = {
            "_id": "fake_object_id",
            "worker_id": "w1", "company_id": "c1", "year": 2026, "month": 1,
            "signed_at": _make_utc(2026, 2, 1, 12, 0),
            "content_hash": "a" * 64,
            "content_hash_version": "v99",
            "record_count": 3,
            "timezone": "Europe/Madrid",
        }

        with patch("api.services.integrity_service.db") as mock_db, \
             patch("api.services.integrity_service.ObjectId", return_value="fake_object_id"):
            mock_db.MonthlySignatures.find_one = AsyncMock(return_value=signature)
            mock_db.TimeRecords.find = MagicMock(side_effect=AssertionError("records recomputed"))
            result = await IntegrityService.verify_monthly_signature("507f1f77bcf86cd799439011")

        assert result["status"] == "unsupported_version"
        assert result["content_hash"] == "a" * 64
        assert result["computed_hash"] == ""
        assert result["content_hash_version"] == "v99"
        assert result["signed_record_count"] == 3
        assert result["current_record_count"] is None
        assert result["audited_corrections"] == []

    async def test_verify_monthly_signature_without_version_assumes_v1(self):
        """A signature with a digest but no version field was written by this
        same code before the field existed, so v1 is recomputed."""
        records = self._monthly_records()
        digest, count = IntegrityService.compute_month_digest(records)
        signature = {
            "_id": "fake_object_id",
            "worker_id": "w1", "company_id": "c1", "year": 2026, "month": 1,
            "signed_at": _make_utc(2026, 2, 1, 12, 0),
            "content_hash": digest,
            "record_count": count,
            "timezone": "Europe/Madrid",
        }

        class _Cursor:
            def sort(self, *_args, **_kwargs):
                return self

            async def to_list(self, _length):
                return records

        with patch("api.services.integrity_service.db") as mock_db, \
             patch("api.services.integrity_service.ObjectId", return_value="fake_object_id"):
            mock_db.MonthlySignatures.find_one = AsyncMock(return_value=signature)
            mock_db.TimeRecords.find = MagicMock(return_value=_Cursor())
            result = await IntegrityService.verify_monthly_signature("507f1f77bcf86cd799439011")

        assert result["status"] == "verified"
        assert result["computed_hash"] == digest

    async def test_find_audited_corrections_matches_local_month_bounds(self):
        """ChangeRequests.date is a naive local calendar date at midnight, so it is
        matched against the local month bounds, not against the UTC window: under a
        negative-offset zone the latter drops the 1st and pulls in the next month's.
        Both windows are widened a day per side and the corrected instants are
        matched too, so a correction affecting an adjacent month is not missed."""
        captured: dict = {}

        class _Cursor:
            def sort(self, *_args, **_kwargs):
                return self

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        signature = {
            "worker_id": "w1", "company_id": "c1", "year": 2026, "month": 3,
            "signed_at": _make_utc(2026, 4, 2, 12, 0),
            "timezone": "America/New_York",
        }

        with patch("api.services.integrity_service.db") as mock_db:
            mock_db.ChangeRequests.find = MagicMock(
                side_effect=lambda q: captured.update(query=q) or _Cursor()
            )
            corrections = await IntegrityService._find_audited_corrections(signature)

        assert corrections == []
        query = captured["query"]
        # Naive local date bounds, one day wider on each side.
        assert {"date": {"$gte": datetime(2026, 2, 28), "$lt": datetime(2026, 4, 2)}} in query["$or"]
        # March 2026 in America/New_York spans 2026-03-01T05:00Z..2026-04-01T04:00Z;
        # the corrected instants are matched over that window, widened a day per side.
        utc_window = {"$gte": _make_utc(2026, 2, 28, 5, 0), "$lt": _make_utc(2026, 4, 2, 4, 0)}
        assert {"original_timestamp": utc_window} in query["$or"]
        assert {"new_timestamp": utc_window} in query["$or"]
        # The reviewed_at bound is a true UTC instant and stays as it was.
        assert query["reviewed_at"] == {"$gt": _make_utc(2026, 4, 2, 12, 0)}
        assert query["status"] == "accepted"
        assert query["worker_id"] == "w1"
        assert query["company_id"] == "c1"

    async def test_verify_monthly_signature_not_found(self):
        """Verifying a non-existent signature raises 404."""
        from fastapi import HTTPException

        with patch("api.services.integrity_service.db") as mock_db, \
             patch("api.services.integrity_service.ObjectId", return_value="fake_object_id"):
            mock_db.MonthlySignatures.find_one = AsyncMock(return_value=None)
            with pytest.raises(HTTPException) as exc_info:
                await IntegrityService.verify_monthly_signature("507f1f77bcf86cd799439011")

        assert exc_info.value.status_code == 404


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
        # No zone is echoed back: the server resolves the window itself.
        assert req.timezone is None

    def test_worker_request_models_never_reject_a_timezone(self):
        """The worker surface accepts any zone — including the CLDR
        "Etc/Unknown" fallback a browser may report, or one from a tzdata newer
        than the bundled pytz — because it ignores it: a 422 here would leave
        the worker unable to view or sign their own month."""
        for model in (WorkerReportRequest, MonthlySignatureRequest, WorkerExportRequest):
            for zone in ("Etc/Unknown", "Mars/Olympus_Mons", "America/New_York", ""):
                req = model(
                    email="worker@example.com", password="secret", company_id="c1",
                    year=2026, month=1, timezone=zone,
                )
                assert req.timezone == zone

    def test_signature_verification_timezone_is_not_validated(self):
        """The timezone persisted on an existing signature is a response field:
        it must stay serialisable even if the stored name is unknown to pytz."""
        verification = MonthlySignatureVerification(
            signature_id="s1", status="verified",
            content_hash="a" * 64, computed_hash="a" * 64,
            timezone="America/Coyhaique",
            worker_id="w1", company_id="c1", year=2026, month=1,
            signed_at=_make_utc(2026, 2, 1, 12, 0),
        )
        assert verification.timezone == "America/Coyhaique"

    def test_signature_verification_accepts_unsupported_version_status(self):
        """A digest made by an algorithm version this build cannot compute is a
        fourth state of its own, not a mismatch."""
        verification = MonthlySignatureVerification(
            signature_id="s1", status="unsupported_version",
            content_hash="a" * 64, computed_hash="",
            content_hash_version="v99",
            worker_id="w1", company_id="c1", year=2026, month=1,
            signed_at=_make_utc(2026, 2, 1, 12, 0),
        )
        assert verification.status == "unsupported_version"


# ===========================================================================
# TestResolveWorkerTimezone
# ===========================================================================


class TestResolveWorkerTimezone:
    """The month window of the worker surface comes from the worker's record."""

    def test_configured_zone_is_honoured(self):
        assert resolve_worker_timezone({"default_timezone": "Atlantic/Canary"}) == "Atlantic/Canary"

    def test_utc_falls_back_to_the_default(self):
        """"UTC" is the WorkerModel default, not a choice: honouring it would
        move the month window of every existing worker."""
        assert resolve_worker_timezone({"default_timezone": "UTC"}) == DEFAULT_REPORT_TIMEZONE
        assert DEFAULT_REPORT_TIMEZONE == "Europe/Madrid"

    def test_missing_or_empty_falls_back_to_the_default(self):
        assert resolve_worker_timezone({}) == DEFAULT_REPORT_TIMEZONE
        assert resolve_worker_timezone({"default_timezone": None}) == DEFAULT_REPORT_TIMEZONE
        assert resolve_worker_timezone({"default_timezone": "  "}) == DEFAULT_REPORT_TIMEZONE

    def test_unknown_zone_warns_and_falls_back(self, caplog):
        """A zone this pytz build does not know degrades with a warning instead
        of leaving the worker unable to view or sign the month."""
        with caplog.at_level(logging.WARNING, logger="api.utils.timezones"):
            resolved = resolve_worker_timezone(
                {"_id": "w1", "default_timezone": "Mars/Olympus_Mons"}
            )
        assert resolved == DEFAULT_REPORT_TIMEZONE
        assert "Mars/Olympus_Mons" in caplog.text


# ===========================================================================
# TestAdminTimezoneQueryParam
# ===========================================================================


@pytest.fixture()
def admin_reports_client():
    """TestClient over the reports router with an admin user injected.

    Only request validation is exercised here: a 422 is answered before the
    service layer, so no database is involved.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.auth.auth_handler import get_current_active_user
    from api.routers import reports as reports_router

    app = FastAPI()
    app.include_router(reports_router.router, prefix="/api")
    app.dependency_overrides[get_current_active_user] = lambda: APIUser(
        username="admin", email="admin@example.com", role="admin",
    )
    return TestClient(app)


class TestAdminTimezoneQueryParam:
    """Unlike the worker surface, the admin/inspector query params validate the
    zone: letting an unknown one through raised UnknownTimeZoneError deep in the
    report pipeline, i.e. an opaque 500."""

    _ENDPOINTS = (
        "/api/reports/monthly",
        "/api/reports/monthly/worker/w1",
        "/api/reports/overtime",
        "/api/reports/export/monthly",
        "/api/reports/export/overtime",
    )

    @pytest.mark.parametrize("zone", ["Etc/Unknown", "Mars/Olympus_Mons", ""])
    def test_unknown_timezone_is_rejected(self, admin_reports_client, zone):
        for url in self._ENDPOINTS:
            resp = admin_reports_client.get(
                url,
                params={"company_id": "c1", "year": 2026, "month": 1, "timezone": zone},
            )
            assert resp.status_code == 422, f"{url} with {zone!r}: {resp.text}"

    def test_known_timezone_reaches_the_service(self, admin_reports_client):
        """A valid zone is forwarded untouched (the validator only rejects)."""
        with patch.object(
            ReportService,
            "get_company_monthly_summary",
            AsyncMock(return_value=_make_company_summary()),
        ) as mocked:
            resp = admin_reports_client.get(
                "/api/reports/monthly",
                params={
                    "company_id": "c1", "year": 2026, "month": 1,
                    "timezone": "Atlantic/Canary",
                },
            )
        assert resp.status_code == 200, resp.text
        assert mocked.await_args.kwargs["timezone"] == "Atlantic/Canary"


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

    def test_work_center_name_single_center(self):
        """A day spent in a single center keeps just that center's name."""
        records = [
            {"type": "entry", "timestamp": _make_utc(2026, 1, 15, 8, 0), "work_center_name": "Central"},
            {"type": "exit", "timestamp": _make_utc(2026, 1, 15, 16, 0), "duration_minutes": 480.0},
        ]
        result = self._call(records)
        assert result.work_center_name == "Central"

    def test_work_center_name_concatenates_distinct_centers(self):
        """A mid-day center change attributes the day to both centers, in order."""
        records = [
            {"type": "entry", "timestamp": _make_utc(2026, 1, 15, 8, 0), "work_center_name": "A"},
            {"type": "exit", "timestamp": _make_utc(2026, 1, 15, 12, 0), "duration_minutes": 240.0},
            {"type": "entry", "timestamp": _make_utc(2026, 1, 15, 13, 0), "work_center_name": "B"},
            {"type": "exit", "timestamp": _make_utc(2026, 1, 15, 17, 0), "duration_minutes": 240.0},
        ]
        result = self._call(records)
        assert result.work_center_name == "A / B"

    def test_work_center_name_deduplicates_repeated_centers(self):
        """Repeated snapshots of the same center collapse to a single name."""
        records = [
            {"type": "entry", "timestamp": _make_utc(2026, 1, 15, 8, 0), "work_center_name": "A"},
            {"type": "exit", "timestamp": _make_utc(2026, 1, 15, 12, 0), "duration_minutes": 240.0, "work_center_name": "A"},
            {"type": "entry", "timestamp": _make_utc(2026, 1, 15, 13, 0), "work_center_name": "B"},
            {"type": "exit", "timestamp": _make_utc(2026, 1, 15, 17, 0), "duration_minutes": 240.0, "work_center_name": "B"},
        ]
        result = self._call(records)
        assert result.work_center_name == "A / B"

    def test_work_center_name_none_when_records_have_no_snapshot(self):
        """Records without a work_center_name leave the day's snapshot null."""
        records = [
            {"type": "entry", "timestamp": _make_utc(2026, 1, 15, 8, 0)},
            {"type": "exit", "timestamp": _make_utc(2026, 1, 15, 16, 0), "duration_minutes": 480.0},
        ]
        result = self._call(records)
        assert result.work_center_name is None

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
    async def test_export_csv_includes_work_center_column(self):
        """CSV header includes 'Centro de trabajo' and rows carry the snapshot."""
        svc = ExportService()
        daily = _make_daily_summary()
        daily.work_center_name = "Central"
        summary = _make_worker_summary(daily_details=[daily])
        buf = await svc.export_monthly_csv(summary)
        text = buf.read().decode("utf-8-sig")
        lines = [line for line in text.splitlines() if line.strip()]
        assert "Centro de trabajo" in lines[0]
        assert "Central" in lines[1]

    @pytest.mark.asyncio
    async def test_export_csv_work_center_empty_when_null(self):
        """CSV row shows an empty cell when the snapshot is null."""
        svc = ExportService()
        daily = _make_daily_summary()
        daily.work_center_name = None
        summary = _make_worker_summary(daily_details=[daily])
        buf = await svc.export_monthly_csv(summary)
        text = buf.read().decode("utf-8-sig")
        data_row = [line for line in text.splitlines() if line.strip()][1]
        # "Empresa;Centro de trabajo;Entrada" -> empty center cell
        assert "Empresa Test SL;;" in data_row

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
    async def test_export_xlsx_detail_sheet_includes_work_center_column(self):
        """XLSX 'Detalle Diario' sheet has the 'Centro de trabajo' header and row value."""
        from openpyxl import load_workbook
        svc = ExportService()
        daily = _make_daily_summary()
        daily.work_center_name = "Central"
        summary = _make_worker_summary(daily_details=[daily])
        buf = await svc.export_monthly_xlsx(summary)
        wb = load_workbook(buf)
        ws = wb["Detalle Diario"]

        headers = [cell.value for cell in ws[1]]
        assert "Centro de trabajo" in headers
        # Positioned right after "Empresa", mirroring the CSV layout.
        assert headers[headers.index("Empresa") + 1] == "Centro de trabajo"

        row_values = [cell.value for cell in ws[2]]
        assert "Central" in row_values

    @pytest.mark.asyncio
    async def test_export_xlsx_detail_sheet_work_center_empty_when_null(self):
        """XLSX 'Detalle Diario' row shows an empty cell when the snapshot is null."""
        from openpyxl import load_workbook
        svc = ExportService()
        daily = _make_daily_summary()
        daily.work_center_name = None
        summary = _make_worker_summary(daily_details=[daily])
        buf = await svc.export_monthly_xlsx(summary)
        wb = load_workbook(buf)
        ws = wb["Detalle Diario"]

        headers = [cell.value for cell in ws[1]]
        center_col = headers.index("Centro de trabajo")
        assert ws.cell(row=2, column=center_col + 1).value in (None, "")

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

    def test_admin_has_work_center_permissions(self):
        """Admin role includes all work-center permissions."""
        for perm in ("view_work_centers", "create_work_centers", "update_work_centers", "delete_work_centers"):
            assert perm in ROLE_PERMISSIONS["admin"]

    def test_inspector_has_view_work_centers(self):
        """Inspector role includes view_work_centers permission."""
        assert "view_work_centers" in ROLE_PERMISSIONS["inspector"]

    def test_inspector_no_create_work_centers(self):
        """Inspector role does NOT include create_work_centers."""
        assert "create_work_centers" not in ROLE_PERMISSIONS["inspector"]

    def test_tracker_no_work_center_permissions(self):
        """Tracker role has no work-center permissions."""
        for perm in ("view_work_centers", "create_work_centers", "update_work_centers", "delete_work_centers"):
            assert perm not in ROLE_PERMISSIONS["tracker"]

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
