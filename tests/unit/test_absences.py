"""
Unit tests for the absence & vacation management module (Fase 1 / MVP).

All tests are pure unit tests — no MongoDB or HTTP server required. A small
in-memory fake Mongo collection (``FakeCollection``) stands in for
``motor``/``pymongo`` to exercise atomic-transition semantics end to end.
"""
from datetime import date, datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from bson import ObjectId
from fastapi import HTTPException
from pymongo import ReturnDocument

from api.auth.permissions import ROLE_PERMISSIONS, has_permission
from api.models.absences import AbsenceStatus, AbsenceUpdate
from api.models.auth import APIUser
from api.services.absence_balance_service import get_absence_balance, resolve_reference_period
from api.services.absence_days_service import compute_absence_days
from api.services.absence_gating import ensure_absence_module_enabled
from api.services.absence_validator import AbsenceValidator


# ===========================================================================
# Fake Mongo — minimal in-memory double for motor's AsyncIOMotorCollection
# ===========================================================================


def _matches(doc: dict, query: dict) -> bool:
    for key, expected in query.items():
        actual = doc.get(key)
        if isinstance(expected, dict) and any(op.startswith("$") for op in expected):
            for op, val in expected.items():
                if op == "$in" and actual not in val:
                    return False
                elif op == "$ne" and actual == val:
                    return False
                elif op == "$gte" and not (actual is not None and actual >= val):
                    return False
                elif op == "$lte" and not (actual is not None and actual <= val):
                    return False
                elif op == "$lt" and not (actual is not None and actual < val):
                    return False
                elif op == "$gt" and not (actual is not None and actual > val):
                    return False
        else:
            if actual != expected:
                return False
    return True


class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def __aiter__(self):
        self._iter = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration

    async def to_list(self, n=None):
        return list(self._docs)


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = docs if docs is not None else []

    async def find_one(self, query=None):
        query = query or {}
        for doc in self.docs:
            if _matches(doc, query):
                return doc
        return None

    def find(self, query=None):
        query = query or {}
        return FakeCursor([d for d in self.docs if _matches(d, query)])

    async def distinct(self, field, query=None):
        query = query or {}
        return list({d[field] for d in self.docs if _matches(d, query) and field in d})

    async def insert_one(self, doc):
        doc = dict(doc)
        doc["_id"] = doc.get("_id", ObjectId())
        self.docs.append(doc)
        return SimpleNamespace(inserted_id=doc["_id"])

    async def update_one(self, query, update):
        for doc in self.docs:
            if _matches(doc, query):
                doc.update(update.get("$set", {}))
                return SimpleNamespace(matched_count=1)
        return SimpleNamespace(matched_count=0)

    async def find_one_and_update(self, query, update, return_document=None):
        for doc in self.docs:
            if _matches(doc, query):
                before = dict(doc)
                doc.update(update.get("$set", {}))
                return before if return_document == ReturnDocument.BEFORE else dict(doc)
        return None


class FakeDB:
    def __init__(self):
        self.Absences = FakeCollection()
        self.AbsencePolicies = FakeCollection()
        self.Companies = FakeCollection()
        self.Workers = FakeCollection()
        self.TimeRecords = FakeCollection()
        self.MonthlySignatures = FakeCollection()


# ===========================================================================
# AbsenceDaysService
# ===========================================================================


class TestComputeAbsenceDays:
    def test_calendar_days_full_range(self):
        days = compute_absence_days(date(2026, 3, 2), date(2026, 3, 6), "calendar_days", False, "full")
        assert days == 5.0

    def test_business_days_skips_weekend(self):
        # Mon 2026-03-02 .. Sun 2026-03-08 → 5 business days
        days = compute_absence_days(date(2026, 3, 2), date(2026, 3, 8), "business_days", False, "full")
        assert days == 5.0

    def test_business_days_single_weekend_day_is_zero(self):
        # Saturday only
        days = compute_absence_days(date(2026, 3, 7), date(2026, 3, 7), "business_days", False, "full")
        assert days == 0.0

    def test_half_day_morning(self):
        days = compute_absence_days(date(2026, 3, 2), date(2026, 3, 2), "business_days", True, "morning")
        assert days == 0.5

    def test_half_day_afternoon(self):
        days = compute_absence_days(date(2026, 3, 2), date(2026, 3, 2), "calendar_days", True, "afternoon")
        assert days == 0.5

    def test_hourly_request_consumes_zero_days(self):
        days = compute_absence_days(date(2026, 3, 2), date(2026, 3, 2), "business_days", False, "full", is_hourly=True)
        assert days == 0.0


# ===========================================================================
# AbsenceBalanceService
# ===========================================================================


class TestResolveReferencePeriod:
    def test_calendar_mode_uses_given_year(self):
        policy = {"reference_year": "calendar"}
        start, end = resolve_reference_period(policy, worker={}, year=2026)
        assert start == date(2026, 1, 1)
        assert end == date(2026, 12, 31)

    def test_hire_date_mode_before_anniversary(self):
        policy = {"reference_year": "hire_date"}
        worker = {"created_at": datetime(2020, 6, 15)}
        start, end = resolve_reference_period(policy, worker, as_of=date(2026, 3, 1))
        # Anniversary this year (2026-06-15) hasn't happened yet → cycle started last year.
        assert start == date(2025, 6, 15)
        assert end == date(2026, 6, 14)

    def test_hire_date_mode_after_anniversary(self):
        policy = {"reference_year": "hire_date"}
        worker = {"created_at": datetime(2020, 6, 15)}
        start, end = resolve_reference_period(policy, worker, as_of=date(2026, 9, 1))
        assert start == date(2026, 6, 15)
        assert end == date(2027, 6, 14)


@pytest.mark.asyncio
class TestGetAbsenceBalance:
    async def test_balance_counts_accepted_and_pending_separately(self):
        db = FakeDB()
        db.Absences.docs = [
            {
                "worker_id": "w1", "company_id": "c1", "deducts_balance": True,
                "status": "accepted", "days_computed": 5.0,
                "start_date": datetime(2026, 2, 1), "end_date": datetime(2026, 2, 5),
            },
            {
                "worker_id": "w1", "company_id": "c1", "deducts_balance": True,
                "status": "pending", "days_computed": 3.0,
                "start_date": datetime(2026, 6, 1), "end_date": datetime(2026, 6, 3),
            },
            {
                # Different worker — must not count.
                "worker_id": "w2", "company_id": "c1", "deducts_balance": True,
                "status": "accepted", "days_computed": 10.0,
                "start_date": datetime(2026, 3, 1), "end_date": datetime(2026, 3, 1),
            },
            {
                # deducts_balance False — must not count.
                "worker_id": "w1", "company_id": "c1", "deducts_balance": False,
                "status": "accepted", "days_computed": 2.0,
                "start_date": datetime(2026, 4, 1), "end_date": datetime(2026, 4, 1),
            },
        ]
        policy = {"annual_vacation_days": 22, "reference_year": "calendar"}
        worker = {"created_at": datetime(2020, 1, 1)}

        balance = await get_absence_balance(db, "w1", "c1", policy, worker, year=2026)

        assert balance.total_days == 22
        assert balance.taken_days == 5.0
        assert balance.pending_days == 3.0
        assert balance.available_days == 14.0

    async def test_exclude_absence_id_removes_it_from_sums(self):
        db = FakeDB()
        target_id = ObjectId()
        db.Absences.docs = [
            {
                "_id": target_id,
                "worker_id": "w1", "company_id": "c1", "deducts_balance": True,
                "status": "pending", "days_computed": 5.0,
                "start_date": datetime(2026, 2, 1), "end_date": datetime(2026, 2, 5),
            },
        ]
        policy = {"annual_vacation_days": 22, "reference_year": "calendar"}
        worker = {"created_at": datetime(2020, 1, 1)}

        balance = await get_absence_balance(
            db, "w1", "c1", policy, worker, year=2026, exclude_absence_id=str(target_id)
        )
        assert balance.pending_days == 0.0
        assert balance.available_days == 22.0


# ===========================================================================
# AbsenceValidator
# ===========================================================================


def _policy(**overrides) -> dict:
    base = {
        "min_advance_days": 0,
        "allow_half_day": True,
        "allow_hourly": False,
        "max_overlap_per_company": None,
        "blackout_periods": [],
        "annual_vacation_days": 22,
        "reference_year": "calendar",
        "computation": "business_days",
    }
    base.update(overrides)
    return base


def _vacation_type(**overrides) -> dict:
    base = {
        "code": "vacation", "name": "Vacaciones",
        "deducts_balance": True, "is_paid": True, "requires_attachment": False,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
class TestAbsenceValidator:
    async def test_valid_request_has_no_blocking_issues(self):
        db = FakeDB()
        db.Workers.docs = [{"_id": ObjectId(), "created_at": datetime(2020, 1, 1)}]
        validator = AbsenceValidator()

        is_valid, issues = await validator.validate(
            db,
            worker_id="w1", company_id="c1",
            start_date=date(2026, 3, 2), end_date=date(2026, 3, 6),
            days_computed=5.0,
            absence_type=_vacation_type(),
            attachment_id=None,
            policy=_policy(),
            worker={"created_at": datetime(2020, 1, 1)},
        )
        assert is_valid is True
        assert issues == []

    async def test_overlap_with_own_absence_blocks(self):
        db = FakeDB()
        db.Absences.docs = [{
            "worker_id": "w1", "company_id": "c1", "status": "accepted",
            "start_date": datetime(2026, 3, 4), "end_date": datetime(2026, 3, 8),
        }]
        validator = AbsenceValidator()

        is_valid, issues = await validator.validate(
            db,
            worker_id="w1", company_id="c1",
            start_date=date(2026, 3, 2), end_date=date(2026, 3, 6),
            days_computed=5.0,
            absence_type=_vacation_type(deducts_balance=False),
            attachment_id=None,
            policy=_policy(),
            worker={"created_at": datetime(2020, 1, 1)},
        )
        assert is_valid is False
        codes = [i.code for i in issues]
        assert "OVERLAP_ABSENCE" in codes
        assert next(i for i in issues if i.code == "OVERLAP_ABSENCE").blocking is True

    async def test_blackout_period_blocks(self):
        db = FakeDB()
        policy = _policy(blackout_periods=[
            {"name": "Cierre Navidad", "start_date": date(2026, 12, 20), "end_date": date(2026, 12, 31)}
        ])
        validator = AbsenceValidator()

        is_valid, issues = await validator.validate(
            db,
            worker_id="w1", company_id="c1",
            start_date=date(2026, 12, 22), end_date=date(2026, 12, 26),
            days_computed=3.0,
            absence_type=_vacation_type(deducts_balance=False),
            attachment_id=None,
            policy=policy,
            worker={"created_at": datetime(2020, 1, 1)},
        )
        assert is_valid is False
        assert any(i.code == "BLACKOUT_PERIOD" and i.blocking for i in issues)

    async def test_missing_required_attachment_blocks(self):
        db = FakeDB()
        validator = AbsenceValidator()

        is_valid, issues = await validator.validate(
            db,
            worker_id="w1", company_id="c1",
            start_date=date(2026, 3, 2), end_date=date(2026, 3, 2),
            days_computed=1.0,
            absence_type=_vacation_type(deducts_balance=False, requires_attachment=True),
            attachment_id=None,
            policy=_policy(),
            worker={"created_at": datetime(2020, 1, 1)},
        )
        assert is_valid is False
        assert any(i.code == "ATTACHMENT_REQUIRED" and i.blocking for i in issues)

    async def test_attachment_present_does_not_block(self):
        db = FakeDB()
        validator = AbsenceValidator()

        is_valid, issues = await validator.validate(
            db,
            worker_id="w1", company_id="c1",
            start_date=date(2026, 3, 2), end_date=date(2026, 3, 2),
            days_computed=1.0,
            absence_type=_vacation_type(deducts_balance=False, requires_attachment=True),
            attachment_id="507f1f77bcf86cd799439011",
            policy=_policy(),
            worker={"created_at": datetime(2020, 1, 1)},
        )
        assert is_valid is True
        assert not any(i.code == "ATTACHMENT_REQUIRED" for i in issues)

    async def test_insufficient_balance_blocks(self):
        db = FakeDB()
        db.Absences.docs = [{
            "worker_id": "w1", "company_id": "c1", "deducts_balance": True,
            "status": "accepted", "days_computed": 20.0,
            "start_date": datetime(2026, 1, 5), "end_date": datetime(2026, 1, 25),
        }]
        validator = AbsenceValidator()

        is_valid, issues = await validator.validate(
            db,
            worker_id="w1", company_id="c1",
            start_date=date(2026, 3, 2), end_date=date(2026, 3, 6),
            days_computed=5.0,
            absence_type=_vacation_type(deducts_balance=True),
            attachment_id=None,
            policy=_policy(annual_vacation_days=22),
            worker={"created_at": datetime(2020, 1, 1)},
        )
        assert is_valid is False
        assert any(i.code == "INSUFFICIENT_BALANCE" and i.blocking for i in issues)

    async def test_min_advance_not_met_warns_but_does_not_block(self):
        db = FakeDB()
        validator = AbsenceValidator()

        is_valid, issues = await validator.validate(
            db,
            worker_id="w1", company_id="c1",
            start_date=date(2026, 3, 3), end_date=date(2026, 3, 3),
            days_computed=1.0,
            absence_type=_vacation_type(deducts_balance=False),
            attachment_id=None,
            policy=_policy(min_advance_days=15),
            worker={"created_at": datetime(2020, 1, 1)},
            created_at=datetime(2026, 3, 1),
        )
        assert is_valid is True  # non-blocking: admin can approve urgent requests
        issue = next(i for i in issues if i.code == "MIN_ADVANCE_NOT_MET")
        assert issue.blocking is False

    async def test_max_overlap_exceeded_warns_but_does_not_block(self):
        db = FakeDB()
        db.Absences.docs = [{
            "worker_id": "w2", "company_id": "c1", "status": "accepted",
            "start_date": datetime(2026, 3, 2), "end_date": datetime(2026, 3, 6),
        }]
        validator = AbsenceValidator()

        is_valid, issues = await validator.validate(
            db,
            worker_id="w1", company_id="c1",
            start_date=date(2026, 3, 2), end_date=date(2026, 3, 6),
            days_computed=5.0,
            absence_type=_vacation_type(deducts_balance=False),
            attachment_id=None,
            policy=_policy(max_overlap_per_company=1),
            worker={"created_at": datetime(2020, 1, 1)},
        )
        assert is_valid is True  # non-blocking
        issue = next(i for i in issues if i.code == "MAX_OVERLAP_EXCEEDED")
        assert issue.blocking is False

    async def test_time_records_exist_warns_but_does_not_block(self):
        db = FakeDB()
        db.TimeRecords.docs = [{
            "worker_id": "w1", "company_id": "c1",
            "created_at": datetime(2026, 3, 3, 9, 0),
        }]
        validator = AbsenceValidator()

        is_valid, issues = await validator.validate(
            db,
            worker_id="w1", company_id="c1",
            start_date=date(2026, 3, 2), end_date=date(2026, 3, 6),
            days_computed=5.0,
            absence_type=_vacation_type(deducts_balance=False),
            attachment_id=None,
            policy=_policy(),
            worker={"created_at": datetime(2020, 1, 1)},
        )
        assert is_valid is True  # non-blocking
        issue = next(i for i in issues if i.code == "TIME_RECORDS_EXIST")
        assert issue.blocking is False


# ===========================================================================
# Absence gating (D9 opt-in module)
# ===========================================================================


@pytest.mark.asyncio
class TestAbsenceGating:
    async def test_company_not_found_raises_404(self):
        with patch("api.services.absence_gating.db") as mock_db:
            mock_db.Companies.find_one = AsyncMock(return_value=None)
            with pytest.raises(HTTPException) as exc_info:
                await ensure_absence_module_enabled(str(ObjectId()))
            assert exc_info.value.status_code == 404

    async def test_module_disabled_raises_403(self):
        with patch("api.services.absence_gating.db") as mock_db:
            mock_db.Companies.find_one = AsyncMock(return_value={
                "_id": ObjectId(), "absence_management_enabled": False,
            })
            with pytest.raises(HTTPException) as exc_info:
                await ensure_absence_module_enabled(str(ObjectId()))
            assert exc_info.value.status_code == 403

    async def test_module_enabled_passes(self):
        with patch("api.services.absence_gating.db") as mock_db:
            company_doc = {"_id": ObjectId(), "absence_management_enabled": True}
            mock_db.Companies.find_one = AsyncMock(return_value=company_doc)
            result = await ensure_absence_module_enabled(str(ObjectId()))
            assert result == company_doc

    async def test_invalid_company_id_raises_404(self):
        with pytest.raises(HTTPException) as exc_info:
            await ensure_absence_module_enabled("not-a-valid-object-id")
        assert exc_info.value.status_code == 404


# ===========================================================================
# Atomic transition — PATCH /absences/{id}
# ===========================================================================


def _make_absence_doc(company_id: str, worker_id: str) -> dict:
    now = datetime.now(dt_timezone.utc).replace(tzinfo=None)
    return {
        "_id": ObjectId(),
        "worker_id": worker_id,
        "worker_email": "worker@test.com",
        "worker_first_name": "Ana",
        "worker_last_name": "Garcia",
        "company_id": company_id,
        "company_name": "ACME",
        "absence_type_code": "unjustified_absence",
        "absence_type_name": "Ausencia no justificada",
        "deducts_balance": False,
        "start_date": datetime(2026, 3, 10),
        "end_date": datetime(2026, 3, 10),
        "is_partial": False,
        "day_portion": "full",
        "start_time": None,
        "end_time": None,
        "worker_comment": None,
        "attachment_id": None,
        "days_computed": 1.0,
        "status": AbsenceStatus.PENDING.value,
        "created_at": now - timedelta(days=5),
        "created_by": worker_id,
        "updated_at": now - timedelta(days=5),
        "updated_by": worker_id,
        "reviewed_by_admin_id": None,
        "reviewed_by_admin_email": None,
        "reviewed_at": None,
        "admin_internal_notes": None,
        "admin_public_comment": None,
    }


@pytest.mark.asyncio
class TestAtomicApprovalTransition:
    async def test_second_concurrent_approval_is_rejected(self):
        from api.routers import absences as absences_router

        company_id = str(ObjectId())
        worker_id = str(ObjectId())
        absence_doc = _make_absence_doc(company_id, worker_id)
        absence_id = str(absence_doc["_id"])

        fake_db = FakeDB()
        fake_db.Absences.docs = [absence_doc]
        fake_db.Companies.docs = [{
            "_id": ObjectId(company_id), "deleted_at": None, "absence_management_enabled": True,
        }]
        fake_db.AbsencePolicies.docs = [{
            "company_id": company_id,
            "absence_types": [{
                "code": "unjustified_absence", "name": "Ausencia no justificada",
                "deducts_balance": False, "requires_attachment": False,
            }],
            "blackout_periods": [], "min_advance_days": 0, "max_overlap_per_company": None,
        }]
        fake_db.Workers.docs = [{"_id": ObjectId(worker_id), "created_at": datetime(2020, 1, 1)}]

        admin_user = APIUser(username="admin", email="admin@test.com", role="admin", id=str(ObjectId()))
        update_body = AbsenceUpdate(status=AbsenceStatus.ACCEPTED, admin_public_comment="OK")

        with patch("api.routers.absences.db", fake_db), \
             patch("api.services.absence_gating.db", fake_db), \
             patch.object(absences_router.EmailService, "send_absence_approved_email", new=AsyncMock(return_value=True)):

            # First approval succeeds.
            result = await absences_router.update_absence(absence_id, update_body, current_user=admin_user)
            assert result.status == AbsenceStatus.ACCEPTED.value

            # Second, concurrent approval attempt must be rejected (already processed).
            with pytest.raises(HTTPException) as exc_info:
                await absences_router.update_absence(absence_id, update_body, current_user=admin_user)
            assert exc_info.value.status_code == 409

    async def test_cannot_cancel_already_accepted_absence(self):
        from api.routers import absences as absences_router
        from api.models.absences import WorkerAbsenceCancelRequest

        company_id = str(ObjectId())
        worker_id = str(ObjectId())
        absence_doc = _make_absence_doc(company_id, worker_id)
        absence_doc["status"] = AbsenceStatus.ACCEPTED.value
        absence_id = str(absence_doc["_id"])

        fake_db = FakeDB()
        fake_db.Absences.docs = [absence_doc]
        fake_db.Companies.docs = [{
            "_id": ObjectId(company_id), "deleted_at": None, "absence_management_enabled": True,
        }]
        fake_db.Workers.docs = [{
            "_id": ObjectId(worker_id), "email": "worker@test.com",
            "hashed_password": "irrelevant", "deleted_at": None,
        }]

        with patch("api.routers.absences.db", fake_db), \
             patch("api.services.absence_gating.db", fake_db), \
             patch("api.utils.worker_auth.db", fake_db), \
             patch("api.utils.worker_auth.verify_password", return_value=True):
            with pytest.raises(HTTPException) as exc_info:
                await absences_router.cancel_absence_request(
                    absence_id, WorkerAbsenceCancelRequest(email="worker@test.com", password="x")
                )
            assert exc_info.value.status_code == 400


# ===========================================================================
# Permissions (D8)
# ===========================================================================


# ===========================================================================
# Report integration regression (D6 / absence-reporting spec)
# ===========================================================================


@pytest.mark.asyncio
class TestReportAbsenceIntegration:
    async def test_absence_day_without_records_appears_as_zero_hours_absence(self):
        from api.services.report_service import ReportService

        company_id = str(ObjectId())
        worker_id = str(ObjectId())

        fake_db = FakeDB()
        fake_db.Companies.docs = [{
            "_id": ObjectId(company_id), "deleted_at": None, "name": "ACME",
            "absence_management_enabled": True,
        }]
        fake_db.Workers.docs = [{
            "_id": ObjectId(worker_id), "deleted_at": None,
            "first_name": "Ana", "last_name": "Garcia", "id_number": "12345678A",
        }]
        fake_db.Absences.docs = [{
            "worker_id": worker_id, "company_id": company_id, "status": "accepted",
            "absence_type_name": "Vacaciones",
            "start_date": datetime(2026, 3, 10), "end_date": datetime(2026, 3, 12),
            "days_computed": 3.0,
        }]

        with patch("api.services.report_service.db", fake_db):
            summary = await ReportService().get_worker_monthly_summary(
                company_id=company_id, worker_id=worker_id, year=2026, month=3,
            )

        absence_days = [d for d in summary.daily_details if d.is_absence]
        assert {d.date for d in absence_days} == {date(2026, 3, 10), date(2026, 3, 11), date(2026, 3, 12)}
        assert all(d.total_worked_minutes == 0 for d in absence_days)
        assert all(d.absence_type == "Vacaciones" for d in absence_days)

        # Absence days must not count as worked days / generate overtime.
        assert summary.total_days_worked == 0
        assert summary.total_overtime_minutes == 0

        assert len(summary.absences) == 1
        assert summary.absences[0].absence_type == "Vacaciones"
        assert summary.absences[0].start_date == date(2026, 3, 10)
        assert summary.absences[0].end_date == date(2026, 3, 12)
        assert summary.absences[0].days_computed == 3.0

    async def test_company_without_module_report_is_unaffected(self):
        from api.services.report_service import ReportService

        company_id = str(ObjectId())
        worker_id = str(ObjectId())

        fake_db = FakeDB()
        fake_db.Companies.docs = [{
            "_id": ObjectId(company_id), "deleted_at": None, "name": "ACME",
            "absence_management_enabled": False,
        }]
        fake_db.Workers.docs = [{
            "_id": ObjectId(worker_id), "deleted_at": None,
            "first_name": "Ana", "last_name": "Garcia", "id_number": "12345678A",
        }]
        # Even though an ACCEPTED absence exists, it must be ignored entirely.
        fake_db.Absences.docs = [{
            "worker_id": worker_id, "company_id": company_id, "status": "accepted",
            "absence_type_name": "Vacaciones",
            "start_date": datetime(2026, 3, 10), "end_date": datetime(2026, 3, 12),
            "days_computed": 3.0,
        }]

        with patch("api.services.report_service.db", fake_db):
            summary = await ReportService().get_worker_monthly_summary(
                company_id=company_id, worker_id=worker_id, year=2026, month=3,
            )

        assert summary.daily_details == []
        assert summary.absences == []


# ===========================================================================
# POST /absences/me/types — worker-facing absence type catalogue
# ===========================================================================


@pytest.mark.asyncio
class TestWorkerAbsenceTypesEndpoint:
    async def test_worker_receives_company_catalogue_including_custom_type(self):
        from api.models.absences import WorkerAbsenceTypesRequest
        from api.routers import absences as absences_router

        company_id = str(ObjectId())
        worker_id = str(ObjectId())

        fake_db = FakeDB()
        fake_db.Companies.docs = [{
            "_id": ObjectId(company_id), "deleted_at": None, "absence_management_enabled": True,
        }]
        fake_db.Workers.docs = [{
            "_id": ObjectId(worker_id), "email": "worker@test.com",
            "hashed_password": "irrelevant", "deleted_at": None,
            "company_ids": [company_id],
        }]
        custom_type = {
            "code": "sabbatical", "name": "Excedencia", "deducts_balance": False,
            "is_paid": False, "requires_attachment": True, "max_days": 30, "color": "#000000",
        }
        fake_db.AbsencePolicies.docs = [{
            "company_id": company_id,
            "absence_types": [custom_type],
            "blackout_periods": [], "annual_vacation_days": 22,
            "computation": "business_days", "reference_year": "calendar",
            "min_advance_days": 0, "allow_half_day": True, "allow_hourly": False,
            "max_overlap_per_company": None,
        }]

        with patch("api.routers.absences.db", fake_db), \
             patch("api.services.absence_gating.db", fake_db), \
             patch("api.utils.worker_auth.db", fake_db), \
             patch("api.routers.absence_policies.db", fake_db), \
             patch("api.utils.worker_auth.verify_password", return_value=True):
            result = await absences_router.get_worker_absence_types(
                WorkerAbsenceTypesRequest(email="worker@test.com", password="x", company_id=company_id)
            )

        assert len(result) == 1
        assert result[0].code == "sabbatical"
        assert result[0].name == "Excedencia"
        assert result[0].requires_attachment is True

    async def test_worker_lazily_seeds_default_types_when_no_policy_exists(self):
        from api.models.absences import WorkerAbsenceTypesRequest, default_absence_types
        from api.routers import absences as absences_router

        company_id = str(ObjectId())
        worker_id = str(ObjectId())

        fake_db = FakeDB()
        fake_db.Companies.docs = [{
            "_id": ObjectId(company_id), "deleted_at": None, "absence_management_enabled": True,
        }]
        fake_db.Workers.docs = [{
            "_id": ObjectId(worker_id), "email": "worker@test.com",
            "hashed_password": "irrelevant", "deleted_at": None,
            "company_ids": [company_id],
        }]
        # No AbsencePolicies document yet — must be lazily seeded.
        assert fake_db.AbsencePolicies.docs == []

        with patch("api.routers.absences.db", fake_db), \
             patch("api.services.absence_gating.db", fake_db), \
             patch("api.utils.worker_auth.db", fake_db), \
             patch("api.routers.absence_policies.db", fake_db), \
             patch("api.utils.worker_auth.verify_password", return_value=True):
            result = await absences_router.get_worker_absence_types(
                WorkerAbsenceTypesRequest(email="worker@test.com", password="x", company_id=company_id)
            )

        assert {t.code for t in result} == {t.code for t in default_absence_types()}
        # The seeded policy must now be persisted for the company.
        assert len(fake_db.AbsencePolicies.docs) == 1
        assert fake_db.AbsencePolicies.docs[0]["company_id"] == company_id

    async def test_company_without_module_returns_403(self):
        from api.models.absences import WorkerAbsenceTypesRequest
        from api.routers import absences as absences_router

        company_id = str(ObjectId())
        worker_id = str(ObjectId())

        fake_db = FakeDB()
        fake_db.Companies.docs = [{
            "_id": ObjectId(company_id), "deleted_at": None, "absence_management_enabled": False,
        }]
        fake_db.Workers.docs = [{
            "_id": ObjectId(worker_id), "email": "worker@test.com",
            "hashed_password": "irrelevant", "deleted_at": None,
            "company_ids": [company_id],
        }]

        with patch("api.routers.absences.db", fake_db), \
             patch("api.services.absence_gating.db", fake_db), \
             patch("api.utils.worker_auth.db", fake_db), \
             patch("api.utils.worker_auth.verify_password", return_value=True):
            with pytest.raises(HTTPException) as exc_info:
                await absences_router.get_worker_absence_types(
                    WorkerAbsenceTypesRequest(email="worker@test.com", password="x", company_id=company_id)
                )
            assert exc_info.value.status_code == 403

    async def test_company_not_found_returns_404(self):
        from api.models.absences import WorkerAbsenceTypesRequest
        from api.routers import absences as absences_router

        company_id = str(ObjectId())
        worker_id = str(ObjectId())

        fake_db = FakeDB()
        # No matching Companies document — company doesn't exist.
        fake_db.Workers.docs = [{
            "_id": ObjectId(worker_id), "email": "worker@test.com",
            "hashed_password": "irrelevant", "deleted_at": None,
            "company_ids": [company_id],
        }]

        with patch("api.routers.absences.db", fake_db), \
             patch("api.services.absence_gating.db", fake_db), \
             patch("api.utils.worker_auth.db", fake_db), \
             patch("api.utils.worker_auth.verify_password", return_value=True):
            with pytest.raises(HTTPException) as exc_info:
                await absences_router.get_worker_absence_types(
                    WorkerAbsenceTypesRequest(email="worker@test.com", password="x", company_id=company_id)
                )
            assert exc_info.value.status_code == 404


# ===========================================================================
# POST /absences/me/balance and POST /absences/ — lazy policy seeding
# (regression: these used to 404 instead of seeding, unlike /me/types)
# ===========================================================================


@pytest.mark.asyncio
class TestWorkerLazySeedingConsistency:
    async def test_balance_seeds_default_policy_when_none_exists(self):
        from api.models.absences import WorkerAbsenceBalanceRequest
        from api.routers import absences as absences_router

        company_id = str(ObjectId())
        worker_id = str(ObjectId())

        fake_db = FakeDB()
        fake_db.Companies.docs = [{
            "_id": ObjectId(company_id), "deleted_at": None, "absence_management_enabled": True,
        }]
        fake_db.Workers.docs = [{
            "_id": ObjectId(worker_id), "email": "worker@test.com",
            "hashed_password": "irrelevant", "deleted_at": None,
            "company_ids": [company_id],
        }]
        # No AbsencePolicies document yet — must be lazily seeded, not 404.
        assert fake_db.AbsencePolicies.docs == []

        with patch("api.routers.absences.db", fake_db), \
             patch("api.services.absence_gating.db", fake_db), \
             patch("api.utils.worker_auth.db", fake_db), \
             patch("api.routers.absence_policies.db", fake_db), \
             patch("api.utils.worker_auth.verify_password", return_value=True):
            result = await absences_router.get_worker_absence_balance(
                WorkerAbsenceBalanceRequest(email="worker@test.com", password="x", company_id=company_id)
            )

        assert result.total_days == 22
        assert result.taken_days == 0
        assert result.pending_days == 0
        assert result.available_days == 22
        # The seeded policy must now be persisted for the company.
        assert len(fake_db.AbsencePolicies.docs) == 1
        assert fake_db.AbsencePolicies.docs[0]["company_id"] == company_id

    async def test_create_request_seeds_default_policy_when_none_exists(self):
        from api.models.absences import AbsenceRequestCreate
        from api.routers import absences as absences_router

        company_id = str(ObjectId())
        worker_id = str(ObjectId())

        fake_db = FakeDB()
        fake_db.Companies.docs = [{
            "_id": ObjectId(company_id), "deleted_at": None, "absence_management_enabled": True, "name": "ACME",
        }]
        fake_db.Workers.docs = [{
            "_id": ObjectId(worker_id), "email": "worker@test.com",
            "hashed_password": "irrelevant", "deleted_at": None,
            "company_ids": [company_id], "first_name": "Ana", "last_name": "Garcia",
        }]
        # No AbsencePolicies document yet — must be lazily seeded, not 404.
        assert fake_db.AbsencePolicies.docs == []

        request_data = AbsenceRequestCreate(
            email="worker@test.com", password="x", company_id=company_id,
            absence_type_code="vacation",
            start_date=date(2026, 6, 8), end_date=date(2026, 6, 10),
        )
        dummy_admin = APIUser(username="worker", email="worker@test.com", role="tracker")

        with patch("api.routers.absences.db", fake_db), \
             patch("api.services.absence_gating.db", fake_db), \
             patch("api.utils.worker_auth.db", fake_db), \
             patch("api.routers.absence_policies.db", fake_db), \
             patch("api.utils.worker_auth.verify_password", return_value=True):
             result = await absences_router.create_absence_request(request_data)

        assert result.absence_type_code == "vacation"
        assert result.status == AbsenceStatus.PENDING.value
        # The seeded policy must now be persisted for the company.
        assert len(fake_db.AbsencePolicies.docs) == 1
        assert fake_db.AbsencePolicies.docs[0]["company_id"] == company_id
        assert len(fake_db.Absences.docs) == 1

    async def test_balance_module_disabled_returns_403(self):
        from api.models.absences import WorkerAbsenceBalanceRequest
        from api.routers import absences as absences_router

        company_id = str(ObjectId())
        worker_id = str(ObjectId())

        fake_db = FakeDB()
        fake_db.Companies.docs = [{
            "_id": ObjectId(company_id), "deleted_at": None, "absence_management_enabled": False,
        }]
        fake_db.Workers.docs = [{
            "_id": ObjectId(worker_id), "email": "worker@test.com",
            "hashed_password": "irrelevant", "deleted_at": None,
            "company_ids": [company_id],
        }]

        with patch("api.routers.absences.db", fake_db), \
             patch("api.services.absence_gating.db", fake_db), \
             patch("api.utils.worker_auth.db", fake_db), \
             patch("api.utils.worker_auth.verify_password", return_value=True):
            with pytest.raises(HTTPException) as exc_info:
                await absences_router.get_worker_absence_balance(
                    WorkerAbsenceBalanceRequest(email="worker@test.com", password="x", company_id=company_id)
                )
            assert exc_info.value.status_code == 403
        # No policy must be created when the module is disabled — the gate
        # runs before any lazy seeding.
        assert fake_db.AbsencePolicies.docs == []

    async def test_create_request_module_disabled_returns_403(self):
        from api.models.absences import AbsenceRequestCreate
        from api.routers import absences as absences_router

        company_id = str(ObjectId())
        worker_id = str(ObjectId())

        fake_db = FakeDB()
        fake_db.Companies.docs = [{
            "_id": ObjectId(company_id), "deleted_at": None, "absence_management_enabled": False, "name": "ACME",
        }]
        fake_db.Workers.docs = [{
            "_id": ObjectId(worker_id), "email": "worker@test.com",
            "hashed_password": "irrelevant", "deleted_at": None,
            "company_ids": [company_id],
        }]

        request_data = AbsenceRequestCreate(
            email="worker@test.com", password="x", company_id=company_id,
            absence_type_code="vacation",
            start_date=date(2026, 6, 8), end_date=date(2026, 6, 10),
        )
        dummy_admin = APIUser(username="worker", email="worker@test.com", role="tracker")

        with patch("api.routers.absences.db", fake_db), \
             patch("api.services.absence_gating.db", fake_db), \
             patch("api.utils.worker_auth.db", fake_db), \
             patch("api.utils.worker_auth.verify_password", return_value=True):
            with pytest.raises(HTTPException) as exc_info:
                 await absences_router.create_absence_request(request_data)
            assert exc_info.value.status_code == 403
        assert fake_db.AbsencePolicies.docs == []


class TestAbsencePermissions:
    def test_admin_has_all_absence_permissions(self):
        for perm in ("view_absences", "create_absences", "manage_absences", "manage_absence_policies"):
            assert perm in ROLE_PERMISSIONS["admin"]
            assert has_permission(APIUser(username="a", email="a@test.com", role="admin"), perm)

    def test_tracker_can_only_create_absences(self):
        assert "create_absences" in ROLE_PERMISSIONS["tracker"]
        for perm in ("view_absences", "manage_absences", "manage_absence_policies"):
            assert perm not in ROLE_PERMISSIONS["tracker"]

    def test_inspector_has_no_absence_permissions(self):
        for perm in ("view_absences", "create_absences", "manage_absences", "manage_absence_policies"):
            assert perm not in ROLE_PERMISSIONS["inspector"]
