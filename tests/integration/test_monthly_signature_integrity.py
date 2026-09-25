"""
Integration tests for binding monthly signatures to a content digest.

Covers the whole lifecycle: signing persists a SHA-256 digest of the month's
time records (plus version, record count and timezone), and
GET /api/reports/integrity/monthly-signature/{id} recomputes it over the
current data, reporting "verified" / "mismatch" / "legacy" and attaching the
approved change-requests applied after signing when the digest no longer
matches.
"""
import hashlib
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Dict, Optional, Tuple

import pytest
import pytz
from bson import ObjectId
from httpx import AsyncClient

from api.services.integrity_service import IntegrityService, MONTHLY_DIGEST_VERSION

PASSWORD = "MonthlySign123!"
MADRID = "Europe/Madrid"


def _local_year_month(dt_utc: datetime, tz_name: str = MADRID) -> Tuple[int, int]:
    """Year/month of a UTC instant expressed in the given local timezone."""
    local = dt_utc.astimezone(pytz.timezone(tz_name))
    return local.year, local.month


async def _create_company_and_worker(
    async_client: AsyncClient,
    admin_headers: Dict[str, str],
    *,
    company_name: str,
    email: str,
    phone: str,
    id_number: str,
) -> Tuple[str, str]:
    """Create a company and one worker in it via the API. Returns (company_id, worker_id)."""
    resp = await async_client.post(
        "/api/companies/", json={"name": company_name}, headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    company_id = resp.json()["id"]

    resp = await async_client.post(
        "/api/workers/",
        json={
            "first_name": "Firma",
            "last_name": "Mensual",
            "email": email,
            "phone_number": phone,
            "id_number": id_number,
            "password": PASSWORD,
            "company_ids": [company_id],
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    return company_id, resp.json()["id"]


async def _punch(
    async_client: AsyncClient,
    admin_headers: Dict[str, str],
    email: str,
    company_id: str,
    action: str,
) -> dict:
    """Create a time record via POST /time-records/. Returns the response JSON."""
    resp = await async_client.post(
        "/api/time-records/",
        json={"email": email, "password": PASSWORD, "company_id": company_id, "action": action},
        headers=admin_headers,
    )
    assert resp.status_code == 201, f"{action}: {resp.text}"
    return resp.json()


async def _sign(
    async_client: AsyncClient,
    email: str,
    company_id: str,
    year: int,
    month: int,
    timezone: Optional[str] = None,
):
    """Call the sign endpoint. Returns the raw httpx response."""
    body = {"email": email, "password": PASSWORD, "company_id": company_id, "year": year, "month": month}
    if timezone is not None:
        body["timezone"] = timezone
    return await async_client.post("/api/reports/worker/monthly/sign", json=body)


async def _view(
    async_client: AsyncClient,
    email: str,
    company_id: str,
    year: int,
    month: int,
    timezone: Optional[str] = None,
):
    """Call the worker's own monthly report endpoint. Returns the raw httpx response."""
    body = {"email": email, "password": PASSWORD, "company_id": company_id, "year": year, "month": month}
    if timezone is not None:
        body["timezone"] = timezone
    return await async_client.post("/api/reports/worker/monthly", json=body)


def _records_in(summary: dict) -> int:
    """Total number of time records covered by a monthly summary response."""
    return sum(day["records_count"] for day in summary["daily_details"])


async def _verify(
    async_client: AsyncClient,
    admin_headers: Dict[str, str],
    signature_id: str,
) -> dict:
    """Verify a signature via the admin endpoint and return the JSON body."""
    resp = await async_client.get(
        f"/api/reports/integrity/monthly-signature/{signature_id}",
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _cleanup(test_db, worker_id: str = None, company_id: str = None):
    if worker_id:
        await test_db.WorkerShiftStates.delete_many({"worker_id": worker_id})
        await test_db.TimeRecords.delete_many({"worker_id": worker_id})
        await test_db.ChangeRequests.delete_many({"worker_id": worker_id})
        await test_db.MonthlySignatures.delete_many({"worker_id": worker_id})
        await test_db.Workers.delete_one({"_id": ObjectId(worker_id)})
    if company_id:
        await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
    await test_db.APIUsers.delete_one({"email": "admin@test.com"})


def _previous_year_month(tz_name: str = MADRID) -> Tuple[int, int]:
    """Most recent calendar month that has already ended in ``tz_name``.

    Signing is restricted to closed months, so tests that exercise the sign
    endpoint must target this month rather than the current one.
    """
    now_local = datetime.now(dt_timezone.utc).astimezone(pytz.timezone(tz_name))
    first_of_month = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    previous = first_of_month - timedelta(days=1)
    return previous.year, previous.month


def _previous_of(year: int, month: int) -> Tuple[int, int]:
    """Calendar month immediately before ``(year, month)``."""
    return (year - 1, 12) if month == 1 else (year, month - 1)


async def _seed_record(
    test_db,
    worker_id: str,
    company_id: str,
    year: int,
    month: int,
    record_type: str = "entry",
    *,
    tz_name: str = MADRID,
    day: int = 15,
    hour: int = 9,
    created_at: Optional[datetime] = None,
) -> dict:
    """Insert a time record directly inside an arbitrary (closed) month.

    ``POST /time-records/`` always stamps "now", so a test that must sign an
    already-closed month seeds its records here instead. ``created_at`` may be
    passed to keep the ordering the change-request pairing relies on.
    """
    local = pytz.timezone(tz_name).localize(datetime(year, month, day, hour, 0))
    doc = {
        "worker_id": worker_id,
        "company_id": company_id,
        "type": record_type,
        "timestamp": local.astimezone(dt_timezone.utc),
        "duration_minutes": None,
        "created_at": created_at or datetime.now(dt_timezone.utc),
    }
    inserted = await test_db.TimeRecords.insert_one(doc)
    doc["_id"] = inserted.inserted_id
    return doc


class TestMonthlySignatureSigning:

    @pytest.mark.asyncio
    async def test_sign_persists_digest_and_intact_month_verifies(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db,
    ):
        """Signing stores content_hash/version/count/timezone; an untouched month verifies."""
        email = "msign.intact@test.com"
        company_id = worker_id = None
        try:
            company_id, worker_id = await _create_company_and_worker(
                async_client, admin_headers,
                company_name="Monthly Sign Intact Co", email=email,
                phone="+34600000020", id_number="20202020E",
            )
            # Sign a closed month: the current/future month is rejected.
            year, month = _previous_year_month()
            created = datetime.now(dt_timezone.utc)
            await _seed_record(
                test_db, worker_id, company_id, year, month, "entry", created_at=created,
            )
            await _seed_record(
                test_db, worker_id, company_id, year, month, "exit",
                hour=17, created_at=created + timedelta(minutes=1),
            )

            resp = await _sign(async_client, email, company_id, year, month)
            assert resp.status_code == 201, resp.text
            body = resp.json()
            content_hash = body["content_hash"]
            assert len(content_hash) == 64
            assert all(c in "0123456789abcdef" for c in content_hash)

            stored = await test_db.MonthlySignatures.find_one({"_id": ObjectId(body["id"])})
            assert stored["content_hash"] == content_hash
            assert stored["content_hash_version"] == MONTHLY_DIGEST_VERSION == "v1"
            assert stored["record_count"] == 2
            # Request omitted the timezone: the default must be persisted.
            assert stored["timezone"] == "Europe/Madrid"

            result = await _verify(async_client, admin_headers, body["id"])
            assert result["status"] == "verified"
            assert result["content_hash"] == result["computed_hash"] == content_hash
            assert result["signed_record_count"] == result["current_record_count"] == 2
            assert result["audited_corrections"] == []
        finally:
            await _cleanup(test_db, worker_id, company_id)

    @pytest.mark.asyncio
    async def test_sign_uses_worker_timezone_over_request(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db,
    ):
        """The worker's configured zone (Atlantic/Canary) delimits the month and is
        persisted: a conflicting request timezone is accepted and ignored."""
        email = "msign.canary@test.com"
        company_id = worker_id = None
        try:
            company_id, worker_id = await _create_company_and_worker(
                async_client, admin_headers,
                company_name="Monthly Sign Canary Co", email=email,
                phone="+34600000021", id_number="21212121F",
            )
            await test_db.Workers.update_one(
                {"_id": ObjectId(worker_id)}, {"$set": {"default_timezone": "Atlantic/Canary"}},
            )
            year, month = _previous_year_month("Atlantic/Canary")
            await _seed_record(
                test_db, worker_id, company_id, year, month, "entry", tz_name="Atlantic/Canary",
            )

            # Conflicting request zone: the worker's configured zone must win.
            resp = await _sign(async_client, email, company_id, year, month, timezone="Europe/Madrid")
            assert resp.status_code == 201, resp.text
            body = resp.json()

            stored = await test_db.MonthlySignatures.find_one({"_id": ObjectId(body["id"])})
            assert stored["timezone"] == "Atlantic/Canary"

            # Verification delimits the month with the persisted zone, not the request.
            result = await _verify(async_client, admin_headers, body["id"])
            assert result["status"] == "verified"
            assert result["timezone"] == "Atlantic/Canary"
        finally:
            await _cleanup(test_db, worker_id, company_id)

    @pytest.mark.asyncio
    async def test_sign_empty_month_and_later_insertion_detected(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db,
    ):
        """An empty month signs with the constant empty-set digest; a later record breaks it."""
        email = "msign.empty@test.com"
        company_id = worker_id = None
        try:
            company_id, worker_id = await _create_company_and_worker(
                async_client, admin_headers,
                company_name="Monthly Sign Empty Co", email=email,
                phone="+34600000022", id_number="22222222G",
            )
            year, month = _previous_year_month()

            resp = await _sign(async_client, email, company_id, year, month)
            assert resp.status_code == 201, resp.text
            body = resp.json()
            assert body["content_hash"] == hashlib.sha256(b"v1\n").hexdigest()

            stored = await test_db.MonthlySignatures.find_one({"_id": ObjectId(body["id"])})
            assert stored["record_count"] == 0

            result = await _verify(async_client, admin_headers, body["id"])
            assert result["status"] == "verified"
            assert result["signed_record_count"] == result["current_record_count"] == 0

            # Back-dated insertion into the signed (closed) empty month is
            # detected. POST /time-records/ stamps "now", so it is seeded
            # directly inside the signed month.
            await _seed_record(test_db, worker_id, company_id, year, month, "entry")
            result = await _verify(async_client, admin_headers, body["id"])
            assert result["status"] == "mismatch"
            assert result["current_record_count"] == 1 > result["signed_record_count"]
        finally:
            await _cleanup(test_db, worker_id, company_id)


class TestSigningRestrictedToClosedMonths:
    """The signing endpoint only accepts months that have already ended, closing
    the TOCTOU window between reading the month's records and storing the digest."""

    @pytest.mark.asyncio
    async def test_current_month_is_rejected_and_not_stored(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db,
    ):
        email = "msign.curmonth@test.com"
        company_id = worker_id = None
        try:
            company_id, worker_id = await _create_company_and_worker(
                async_client, admin_headers,
                company_name="Monthly Sign Current Co", email=email,
                phone="+34600000032", id_number="32323232R",
            )
            year, month = _local_year_month(datetime.now(dt_timezone.utc))
            before = await test_db.MonthlySignatures.count_documents({"worker_id": worker_id})

            resp = await _sign(async_client, email, company_id, year, month)
            assert resp.status_code == 400, resp.text
            assert f"El mes {month}/{year}" in resp.json()["detail"]

            after = await test_db.MonthlySignatures.count_documents({"worker_id": worker_id})
            assert after == before == 0
        finally:
            await _cleanup(test_db, worker_id, company_id)

    @pytest.mark.asyncio
    async def test_future_month_is_rejected_and_not_stored(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db,
    ):
        email = "msign.futuremonth@test.com"
        company_id = worker_id = None
        try:
            company_id, worker_id = await _create_company_and_worker(
                async_client, admin_headers,
                company_name="Monthly Sign Future Co", email=email,
                phone="+34600000033", id_number="33333333S",
            )
            year, month = _local_year_month(datetime.now(dt_timezone.utc))
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
            before = await test_db.MonthlySignatures.count_documents({"worker_id": worker_id})

            resp = await _sign(async_client, email, company_id, year, month)
            assert resp.status_code == 400, resp.text

            after = await test_db.MonthlySignatures.count_documents({"worker_id": worker_id})
            assert after == before == 0
        finally:
            await _cleanup(test_db, worker_id, company_id)

    @pytest.mark.asyncio
    async def test_closed_month_is_accepted(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db,
    ):
        email = "msign.closed@test.com"
        company_id = worker_id = None
        try:
            company_id, worker_id = await _create_company_and_worker(
                async_client, admin_headers,
                company_name="Monthly Sign Closed Co", email=email,
                phone="+34600000034", id_number="34343434T",
            )
            year, month = _previous_year_month()

            resp = await _sign(async_client, email, company_id, year, month)
            assert resp.status_code == 201, resp.text
            stored = await test_db.MonthlySignatures.find_one({
                "worker_id": worker_id,
                "company_id": company_id,
                "year": year,
                "month": month,
            })
            assert stored is not None
        finally:
            await _cleanup(test_db, worker_id, company_id)


class TestMonthlySignatureVerification:

    @pytest.mark.asyncio
    async def test_edit_with_recomputed_record_hash_is_detected(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db,
    ):
        """Direct edit of a hashed field breaks the month digest even if the
        record's stored integrity_hash is recomputed to match the edit."""
        email = "msign.edit@test.com"
        company_id = worker_id = None
        try:
            company_id, worker_id = await _create_company_and_worker(
                async_client, admin_headers,
                company_name="Monthly Sign Edit Co", email=email,
                phone="+34600000023", id_number="23232323H",
            )
            year, month = _previous_year_month()
            created = datetime.now(dt_timezone.utc)
            await _seed_record(
                test_db, worker_id, company_id, year, month, "entry", created_at=created,
            )
            exit_rec = await _seed_record(
                test_db, worker_id, company_id, year, month, "exit",
                hour=17, created_at=created + timedelta(minutes=1),
            )

            resp = await _sign(async_client, email, company_id, year, month)
            sig_id = resp.json()["id"]
            signed_hash = resp.json()["content_hash"]

            # Tamper: change a hashed field AND rewrite the stored per-record
            # hash so per-record verification would still pass.
            tampered = ObjectId(exit_rec["_id"])
            await test_db.TimeRecords.update_one(
                {"_id": tampered}, {"$set": {"duration_minutes": 999.0}},
            )
            record = await test_db.TimeRecords.find_one({"_id": tampered})
            await test_db.TimeRecords.update_one(
                {"_id": tampered},
                {"$set": {"integrity_hash": IntegrityService.compute_record_hash(record)}},
            )

            result = await _verify(async_client, admin_headers, sig_id)
            assert result["status"] == "mismatch"
            assert result["content_hash"] == signed_hash
            assert result["computed_hash"] != signed_hash
            # Same set size: only the content changed.
            assert result["current_record_count"] == result["signed_record_count"] == 2
            # No change-request: the alteration has no audit trail.
            assert result["audited_corrections"] == []
        finally:
            await _cleanup(test_db, worker_id, company_id)

    @pytest.mark.asyncio
    async def test_deleted_record_is_detected(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db,
    ):
        """Deleting a covered record reports mismatch with a lower record count."""
        email = "msign.delete@test.com"
        company_id = worker_id = None
        try:
            company_id, worker_id = await _create_company_and_worker(
                async_client, admin_headers,
                company_name="Monthly Sign Delete Co", email=email,
                phone="+34600000024", id_number="24242424J",
            )
            year, month = _previous_year_month()
            created = datetime.now(dt_timezone.utc)
            await _seed_record(
                test_db, worker_id, company_id, year, month, "entry", created_at=created,
            )
            exit_rec = await _seed_record(
                test_db, worker_id, company_id, year, month, "exit",
                hour=17, created_at=created + timedelta(minutes=1),
            )

            resp = await _sign(async_client, email, company_id, year, month)
            sig_id = resp.json()["id"]

            await test_db.TimeRecords.delete_one({"_id": ObjectId(exit_rec["_id"])})

            result = await _verify(async_client, admin_headers, sig_id)
            assert result["status"] == "mismatch"
            assert result["signed_record_count"] == 2
            assert result["current_record_count"] == 1
        finally:
            await _cleanup(test_db, worker_id, company_id)

    @pytest.mark.asyncio
    async def test_backdated_insertion_is_detected(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db,
    ):
        """A record inserted directly into the DB after signing reports mismatch with a higher count."""
        email = "msign.insert@test.com"
        company_id = worker_id = None
        try:
            company_id, worker_id = await _create_company_and_worker(
                async_client, admin_headers,
                company_name="Monthly Sign Insert Co", email=email,
                phone="+34600000025", id_number="25252525K",
            )
            year, month = _previous_year_month()
            await _seed_record(test_db, worker_id, company_id, year, month, "entry")

            resp = await _sign(async_client, email, company_id, year, month)
            sig_id = resp.json()["id"]

            # Back-dated insertion: mid-month timestamp, no API, no integrity_hash.
            await _seed_record(test_db, worker_id, company_id, year, month, "exit", hour=12)

            result = await _verify(async_client, admin_headers, sig_id)
            assert result["status"] == "mismatch"
            assert result["signed_record_count"] == 1
            assert result["current_record_count"] == 2
            assert result["audited_corrections"] == []
        finally:
            await _cleanup(test_db, worker_id, company_id)

    @pytest.mark.asyncio
    async def test_legacy_signature_reports_legacy_and_is_not_backfilled(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db,
    ):
        """A pre-capability signature (no content_hash) reports legacy; verification never
        mutates it — there is no backfill."""
        email = "msign.legacy@test.com"
        company_id = worker_id = None
        try:
            company_id, worker_id = await _create_company_and_worker(
                async_client, admin_headers,
                company_name="Monthly Sign Legacy Co", email=email,
                phone="+34600000026", id_number="26262626L",
            )
            # Insert a record so the month is not empty: legacy must not recompute anyway.
            await _punch(async_client, admin_headers, email, company_id, "entry")

            # Old-format signature document, exactly as created before this capability.
            legacy_doc = {
                "worker_id": worker_id,
                "company_id": company_id,
                "year": 2026,
                "month": 1,
                "signed_at": datetime(2026, 2, 1, 12, 0, tzinfo=dt_timezone.utc),
            }
            inserted = await test_db.MonthlySignatures.insert_one(dict(legacy_doc))
            sig_id = str(inserted.inserted_id)
            try:
                result = await _verify(async_client, admin_headers, sig_id)
                assert result["status"] == "legacy"
                assert result["content_hash"] == ""
                assert result["computed_hash"] == ""
                assert result["signed_record_count"] is None
                assert result["current_record_count"] is None

                # REGRESSION (no backfill): the document is untouched after verification.
                stored = await test_db.MonthlySignatures.find_one({"_id": ObjectId(sig_id)})
                assert "content_hash" not in stored
                assert "content_hash_version" not in stored
                assert "record_count" not in stored
                assert "timezone" not in stored
                assert stored["worker_id"] == legacy_doc["worker_id"]
                assert stored["year"] == legacy_doc["year"]
                assert stored["month"] == legacy_doc["month"]
            finally:
                await test_db.MonthlySignatures.delete_one({"_id": ObjectId(sig_id)})
        finally:
            await _cleanup(test_db, worker_id, company_id)

    @pytest.mark.asyncio
    async def test_unresolvable_stored_timezone_reports_legacy(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db,
    ):
        """A signature carrying a content_hash but an unresolvable stored timezone
        degrades to legacy instead of raising a 500 (nothing is recomputed)."""
        stored_hash = hashlib.sha256(b"valid-looking").hexdigest()
        inserted = await test_db.MonthlySignatures.insert_one({
            "worker_id": "badtz_worker_id",
            "company_id": "badtz_company_id",
            "year": 2026,
            "month": 1,
            "signed_at": datetime(2026, 2, 1, 12, 0, tzinfo=dt_timezone.utc),
            "content_hash": stored_hash,
            "content_hash_version": MONTHLY_DIGEST_VERSION,
            "record_count": 3,
            "timezone": "Mars/Olympus_Mons",
        })
        sig_id = str(inserted.inserted_id)
        try:
            result = await _verify(async_client, admin_headers, sig_id)
            assert result["status"] == "legacy"
            assert result["content_hash"] == stored_hash
            assert result["computed_hash"] == ""
            assert result["current_record_count"] is None
            assert result["audited_corrections"] == []
        finally:
            await test_db.MonthlySignatures.delete_one({"_id": ObjectId(sig_id)})

    @pytest.mark.asyncio
    async def test_audited_correction_after_signing_is_listed_on_mismatch(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db,
    ):
        """An admin-approved change-request applied after signing produces a mismatch
        whose response lists the correction with approver and application time."""
        email = "msign.correction@test.com"
        company_id = worker_id = None
        try:
            company_id, worker_id = await _create_company_and_worker(
                async_client, admin_headers,
                company_name="Monthly Sign Correction Co", email=email,
                phone="+34600000027", id_number="27272727M",
            )
            year, month = _previous_year_month()
            created = datetime.now(dt_timezone.utc)
            entry = await _seed_record(
                test_db, worker_id, company_id, year, month, "entry", created_at=created,
            )
            await _seed_record(
                test_db, worker_id, company_id, year, month, "exit",
                hour=17, created_at=created + timedelta(minutes=1),
            )
            entry_ts = entry["timestamp"]

            resp = await _sign(async_client, email, company_id, year, month)
            sig_id = resp.json()["id"]

            # Worker requests a correction, admin approves it after the signature.
            new_entry_ts = entry_ts - timedelta(minutes=5)
            local_date = entry_ts.astimezone(pytz.timezone(MADRID)).date()
            resp = await async_client.post(
                "/api/change-requests/",
                json={
                    "email": email,
                    "password": PASSWORD,
                    "date": local_date.isoformat(),
                    "company_id": company_id,
                    "time_record_id": str(entry["_id"]),
                    "new_timestamp": new_entry_ts.isoformat(),
                    "reason": "Olvide fichar a la hora correcta",
                },
                headers=admin_headers,
            )
            assert resp.status_code == 201, resp.text
            change_request_id = resp.json()["id"]

            resp = await async_client.patch(
                f"/api/change-requests/{change_request_id}",
                json={"status": "accepted", "admin_public_comment": "Aprobado"},
                headers=admin_headers,
            )
            assert resp.status_code == 200, resp.text

            result = await _verify(async_client, admin_headers, sig_id)
            assert result["status"] == "mismatch"
            corrections = result["audited_corrections"]
            assert len(corrections) == 1
            cr = corrections[0]
            assert cr["change_request_id"] == change_request_id
            assert cr["reviewed_by_admin_email"] == "admin@test.com"
            assert cr["reviewed_at"] is not None
            assert cr["date"] == local_date.isoformat()
            assert cr["reason"] == "Olvide fichar a la hora correcta"
        finally:
            await _cleanup(test_db, worker_id, company_id)

    @pytest.mark.asyncio
    async def test_verification_requires_view_reports_permission(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db,
    ):
        """Anonymous requests get 401; authenticated users without view_reports get 403
        and no digest information is disclosed."""
        from api.auth.auth_handler import get_password_hash

        inserted = await test_db.MonthlySignatures.insert_one({
            "worker_id": "perm_worker_id",
            "company_id": "perm_company_id",
            "year": 2026,
            "month": 1,
            "signed_at": datetime(2026, 2, 1, 12, 0, tzinfo=dt_timezone.utc),
        })
        sig_id = str(inserted.inserted_id)
        url = f"/api/reports/integrity/monthly-signature/{sig_id}"
        try:
            # No credentials at all -> 401.
            resp = await async_client.get(url)
            assert resp.status_code == 401, resp.text

            # Authenticated "tracker" role (no view_reports) -> 403.
            tracker_email = "tracker@test.com"
            await test_db.APIUsers.delete_one({"email": tracker_email})
            await test_db.APIUsers.insert_one({
                "username": "tracker_test",
                "email": tracker_email,
                "hashed_password": get_password_hash("TestTracker123!"),
                "role": "tracker",
                "is_active": True,
                "created_at": datetime.now(dt_timezone.utc),
            })
            resp = await async_client.post(
                "/api/token",
                data={"username": tracker_email, "password": "TestTracker123!"},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            assert resp.status_code == 200, resp.text
            tracker_headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}

            resp = await async_client.get(url, headers=tracker_headers)
            assert resp.status_code == 403, resp.text
            assert "content_hash" not in resp.text
        finally:
            await test_db.MonthlySignatures.delete_one({"_id": inserted.inserted_id})
            await test_db.APIUsers.delete_one({"email": "tracker@test.com"})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_unknown_or_malformed_signature_id_returns_404(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db,
    ):
        """A valid-but-missing ObjectId and a malformed id both report 404, like the
        per-record integrity endpoint."""
        resp = await async_client.get(
            "/api/reports/integrity/monthly-signature/507f1f77bcf86cd799439011",
            headers=admin_headers,
        )
        assert resp.status_code == 404, resp.text

        resp = await async_client.get(
            "/api/reports/integrity/monthly-signature/not-an-object-id",
            headers=admin_headers,
        )
        assert resp.status_code == 404, resp.text

        await test_db.APIUsers.delete_one({"email": "admin@test.com"})


class TestMonthDigestMongoRoundTrip:

    @pytest.mark.asyncio
    async def test_digest_stable_across_mongo_write_read(
        self, test_db,
    ):
        """Digest computed pre-insert (UTC-aware, microsecond precision) equals the one
        recomputed from documents read back through MongoDB (naive, millisecond precision)."""
        worker_id = "roundtrip_worker_id"
        company_id = "roundtrip_company_id"
        now = datetime.now(dt_timezone.utc)
        year, month = _local_year_month(now)
        # Mid-month timestamps: always inside the local calendar month window.
        base = pytz.timezone(MADRID).localize(datetime(year, month, 15, 8, 0, 0, 123456))
        records = [
            {
                "worker_id": worker_id, "company_id": company_id, "type": "entry",
                "timestamp": base, "duration_minutes": None, "created_at": now,
            },
            {
                "worker_id": worker_id, "company_id": company_id, "type": "exit",
                "timestamp": base + timedelta(hours=8), "duration_minutes": 480.0, "created_at": now,
            },
        ]
        digest_at_write, count_at_write = IntegrityService.compute_month_digest(records)
        try:
            await test_db.TimeRecords.insert_many([dict(r) for r in records])
            fetched = await IntegrityService.get_month_records(
                worker_id, company_id, year, month, MADRID,
            )
            digest_after_read, count_after_read = IntegrityService.compute_month_digest(fetched)
            assert count_after_read == count_at_write == 2
            assert digest_after_read == digest_at_write
        finally:
            await test_db.TimeRecords.delete_many({"worker_id": worker_id})


class TestMonthlySignatureTimezoneWindow:

    @pytest.mark.asyncio
    async def test_unknown_request_timezone_is_accepted_and_ignored(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db,
    ):
        """The worker request ``timezone`` is accepted and ignored: an unknown zone
        reaches the endpoint without a 422, and the month is delimited server-side
        from the worker's record (defaulting to Europe/Madrid) instead of the request."""
        email = "msign.badtz@test.com"
        company_id = worker_id = None
        try:
            company_id, worker_id = await _create_company_and_worker(
                async_client, admin_headers,
                company_name="Monthly Sign Bad TZ Co", email=email,
                phone="+34600000030", id_number="30303030P",
            )
            # Sign a closed month: the current/future month is rejected.
            year, month = _previous_year_month()

            # Unknown zones on the read surface are accepted, not rejected.
            for unknown in ("Etc/Unknown", "Mars/Olympus_Mons"):
                resp = await _view(async_client, email, company_id, year, month, timezone=unknown)
                assert resp.status_code == 200, resp.text
                assert resp.json()["month"] == month

            # An unknown request zone does not leak into the signature: the worker
            # has no deliberate zone, so Europe/Madrid is persisted.
            resp = await _sign(async_client, email, company_id, year, month, timezone="Etc/Unknown")
            assert resp.status_code == 201, resp.text
            stored = await test_db.MonthlySignatures.find_one({"_id": ObjectId(resp.json()["id"])})
            assert stored["timezone"] == "Europe/Madrid"

            # A known request zone still signs normally (different closed month: a
            # month can only be signed once).
            prev_year, prev_month = _previous_of(year, month)
            resp = await _sign(
                async_client, email, company_id, prev_year, prev_month, timezone="Atlantic/Canary",
            )
            assert resp.status_code == 201, resp.text

            # An unknown worker zone degrades to Europe/Madrid instead of locking
            # the worker out (resolve_worker_timezone logs the warning).
            await test_db.Workers.update_one(
                {"_id": ObjectId(worker_id)}, {"$set": {"default_timezone": "Mars/Olympus_Mons"}},
            )
            resp = await _view(async_client, email, company_id, year, month)
            assert resp.status_code == 200, resp.text
        finally:
            await _cleanup(test_db, worker_id, company_id)

    @pytest.mark.asyncio
    async def test_view_and_signature_cover_the_same_records(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db,
    ):
        """The month the worker reads is delimited with the worker's configured zone,
        so a boundary punch cannot be shown in the report and left out of the digest;
        a conflicting request zone does not move that window."""
        email = "msign.window@test.com"
        company_id = worker_id = None
        try:
            company_id, worker_id = await _create_company_and_worker(
                async_client, admin_headers,
                company_name="Monthly Sign Window Co", email=email,
                phone="+34600000031", id_number="31313131Q",
            )
            await test_db.Workers.update_one(
                {"_id": ObjectId(worker_id)}, {"$set": {"default_timezone": "Atlantic/Canary"}},
            )
            # 31 December at 23:30Z: already January in Europe/Madrid (UTC+1),
            # still December in Atlantic/Canary (UTC+0).
            boundary = datetime(2025, 12, 31, 23, 30, tzinfo=dt_timezone.utc)
            await test_db.TimeRecords.insert_one({
                "worker_id": worker_id, "company_id": company_id, "type": "entry",
                "timestamp": boundary, "duration_minutes": None, "created_at": boundary,
            })

            # The worker is in Atlantic/Canary: the punch belongs to December, so
            # January is empty.
            resp = await _view(async_client, email, company_id, 2026, 1)
            assert resp.status_code == 200, resp.text
            assert _records_in(resp.json()) == 0

            # A conflicting request zone (Europe/Madrid) does not move the window:
            # the digest must cover exactly the records the view showed.
            resp = await _sign(async_client, email, company_id, 2026, 1, timezone="Europe/Madrid")
            assert resp.status_code == 201, resp.text
            stored = await test_db.MonthlySignatures.find_one({"_id": ObjectId(resp.json()["id"])})
            assert stored["record_count"] == 0
            assert stored["content_hash"] == hashlib.sha256(b"v1\n").hexdigest()
            assert stored["timezone"] == "Atlantic/Canary"
        finally:
            await _cleanup(test_db, worker_id, company_id)


class TestAuditedCorrectionsMonthBounds:

    @pytest.mark.asyncio
    async def test_corrections_matched_on_local_month_bounds(self, test_db):
        """Under a negative-offset timezone, a correction dated the 1st of the signed
        month is listed, one dated the 1st of the next month is also listed (both
        windows are widened a day per side so a correction affecting an adjacent
        month is not missed), and one clearly beyond the widened window is not: the
        stored ``date`` is a naive local calendar date, not a UTC instant."""
        worker_id = "month_bounds_worker_id"
        company_id = "month_bounds_company_id"
        signed_at = datetime(2026, 4, 2, 12, 0, tzinfo=dt_timezone.utc)
        base = {
            "worker_id": worker_id,
            "company_id": company_id,
            "status": "accepted",
            "reviewed_by_admin_email": "admin@test.com",
            "reviewed_at": signed_at + timedelta(hours=1),
        }
        try:
            inside = await test_db.ChangeRequests.insert_one(
                {**base, "date": datetime(2026, 3, 1), "reason": "Primer dia del mes firmado"}
            )
            # The date clause is widened a day per side (for March 2026:
            # {"$gte": 2026-02-28, "$lt": 2026-04-02}), so the 1st of the next
            # month is still attached: a shift crossing midnight on the last day
            # can change the signed month's digest.
            adjacent = await test_db.ChangeRequests.insert_one(
                {**base, "date": datetime(2026, 4, 1), "reason": "Primer dia del mes siguiente"}
            )
            await test_db.ChangeRequests.insert_one(
                {**base, "date": datetime(2026, 4, 3), "reason": "Fuera de la ventana ampliada"}
            )

            corrections = await IntegrityService._find_audited_corrections({
                "worker_id": worker_id, "company_id": company_id,
                "year": 2026, "month": 3, "signed_at": signed_at,
                "timezone": "America/New_York",
            })

            by_id = {c["change_request_id"]: c for c in corrections}
            assert set(by_id) == {str(inside.inserted_id), str(adjacent.inserted_id)}
            assert by_id[str(inside.inserted_id)]["date"] == "2026-03-01"
            assert by_id[str(adjacent.inserted_id)]["date"] == "2026-04-01"
        finally:
            await test_db.ChangeRequests.delete_many({"worker_id": worker_id})
