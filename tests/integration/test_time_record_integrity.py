"""
Integration tests for the SHA-256 integrity hash on TimeRecords.

Regression coverage for the bug where create_time_record never persisted
integrity_hash, so verify_record_integrity could never return verified: true.
"""
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Dict

import pytest
from bson import ObjectId
from httpx import AsyncClient


class TestTimeRecordIntegrity:

    @pytest.mark.asyncio
    async def test_create_time_record_persists_and_verifies_integrity_hash(
        self,
        async_client: AsyncClient,
        admin_headers: Dict[str, str],
        test_db,
    ):
        """
        REGRESSION: a time record created via POST /time-records/ must carry a
        non-empty integrity_hash and verify as "verified" — before this fix,
        the hash was never stored and verification always failed.
        """
        email = "integrity.regression@test.com"
        password = "IntegrityPass123!"
        company_id = None
        worker_id = None
        try:
            resp = await async_client.post(
                "/api/companies/",
                json={"name": "Integrity Regression Company"},
                headers=admin_headers,
            )
            assert resp.status_code == 201, resp.text
            company_id = resp.json()["id"]

            resp = await async_client.post(
                "/api/workers/",
                json={
                    "first_name": "Integrity",
                    "last_name": "Regression",
                    "email": email,
                    "phone_number": "+34600000010",
                    "id_number": "10101010A",
                    "password": password,
                    "company_ids": [company_id],
                },
                headers=admin_headers,
            )
            assert resp.status_code == 201, resp.text
            worker_id = resp.json()["id"]

            resp = await async_client.post(
                "/api/time-records/",
                json={"email": email, "password": password, "company_id": company_id, "action": "entry"},
                headers=admin_headers,
            )
            assert resp.status_code == 201, resp.text
            record_id = resp.json()["id"]

            # Stored document must carry a non-empty integrity_hash
            stored = await test_db.TimeRecords.find_one({"_id": ObjectId(record_id)})
            assert stored.get("integrity_hash"), "integrity_hash was not persisted at creation"

            # Verification endpoint must report verified: true / status: verified
            resp = await async_client.get(
                f"/api/reports/integrity/{record_id}",
                headers=admin_headers,
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["verified"] is True
            assert body["status"] == "verified"
            assert body["integrity_hash"] == body["computed_hash"]

        finally:
            if worker_id:
                await test_db.WorkerShiftStates.delete_many({"worker_id": worker_id})
                await test_db.TimeRecords.delete_many({"worker_id": worker_id})
                await test_db.Workers.delete_one({"_id": ObjectId(worker_id)})
            if company_id:
                await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_all_record_types_get_a_hash(
        self,
        async_client: AsyncClient,
        admin_headers: Dict[str, str],
        test_db,
    ):
        """entry, pause_start, pause_end and exit all get an integrity_hash, even pause_start/entry which lack duration_minutes."""
        email = "integrity.types@test.com"
        password = "IntegrityPass123!"
        company_id = None
        worker_id = None
        try:
            resp = await async_client.post(
                "/api/companies/",
                json={"name": "Integrity Types Company"},
                headers=admin_headers,
            )
            company_id = resp.json()["id"]

            resp = await async_client.post(
                "/api/pause-types/",
                json={"name": "Descanso", "type": "outside_shift", "company_ids": [company_id]},
                headers=admin_headers,
            )
            assert resp.status_code == 201, resp.text
            pause_type_id = resp.json()["id"]

            resp = await async_client.post(
                "/api/workers/",
                json={
                    "first_name": "Integrity",
                    "last_name": "Types",
                    "email": email,
                    "phone_number": "+34600000011",
                    "id_number": "11111111B",
                    "password": password,
                    "company_ids": [company_id],
                },
                headers=admin_headers,
            )
            worker_id = resp.json()["id"]

            for action, extra in (
                ("entry", {}),
                ("pause_start", {"pause_type_id": pause_type_id}),
                ("pause_end", {}),
                ("exit", {}),
            ):
                payload = {"email": email, "password": password, "company_id": company_id, "action": action}
                payload.update(extra)
                resp = await async_client.post("/api/time-records/", json=payload, headers=admin_headers)
                assert resp.status_code == 201, f"{action}: {resp.text}"

            async for record in test_db.TimeRecords.find({"worker_id": worker_id}):
                assert record.get("integrity_hash"), f"record type={record.get('type')} has no integrity_hash"

        finally:
            if worker_id:
                await test_db.WorkerShiftStates.delete_many({"worker_id": worker_id})
                await test_db.TimeRecords.delete_many({"worker_id": worker_id})
                await test_db.Workers.delete_one({"_id": ObjectId(worker_id)})
            if company_id:
                await test_db.PauseTypes.delete_many({"company_ids": company_id})
                await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_direct_db_tamper_is_detected(
        self,
        async_client: AsyncClient,
        admin_headers: Dict[str, str],
        test_db,
    ):
        """Editing a hashed field directly in MongoDB, bypassing the API, must report status: tampered."""
        email = "integrity.tampered@test.com"
        password = "IntegrityPass123!"
        company_id = None
        worker_id = None
        try:
            resp = await async_client.post(
                "/api/companies/",
                json={"name": "Integrity Tampered Company"},
                headers=admin_headers,
            )
            company_id = resp.json()["id"]

            resp = await async_client.post(
                "/api/workers/",
                json={
                    "first_name": "Integrity",
                    "last_name": "Tampered",
                    "email": email,
                    "phone_number": "+34600000012",
                    "id_number": "12121212C",
                    "password": password,
                    "company_ids": [company_id],
                },
                headers=admin_headers,
            )
            worker_id = resp.json()["id"]

            resp = await async_client.post(
                "/api/time-records/",
                json={"email": email, "password": password, "company_id": company_id, "action": "entry"},
                headers=admin_headers,
            )
            record_id = resp.json()["id"]

            # Tamper directly in the database, bypassing the API entirely.
            await test_db.TimeRecords.update_one(
                {"_id": ObjectId(record_id)},
                {"$set": {"timestamp": datetime.now(dt_timezone.utc) + timedelta(hours=5)}},
            )

            resp = await async_client.get(
                f"/api/reports/integrity/{record_id}",
                headers=admin_headers,
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["verified"] is False
            assert body["status"] == "tampered"

        finally:
            if worker_id:
                await test_db.WorkerShiftStates.delete_many({"worker_id": worker_id})
                await test_db.TimeRecords.delete_many({"worker_id": worker_id})
                await test_db.Workers.delete_one({"_id": ObjectId(worker_id)})
            if company_id:
                await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_legacy_record_without_hash_reports_legacy(
        self,
        async_client: AsyncClient,
        admin_headers: Dict[str, str],
        test_db,
    ):
        """A record inserted without integrity_hash (as pre-fix records were) reports status: legacy, not tampered."""
        worker_id = "legacy_worker_id"
        now = datetime.now(dt_timezone.utc)
        legacy_record = {
            "worker_id": worker_id,
            "worker_name": "Legacy Worker",
            "timestamp": now,
            "type": "entry",
            "recorded_by": "system",
            "company_id": "legacy_company_id",
            "company_name": "Legacy Co",
            "created_at": now,
            # No integrity_hash: simulates a record created before this fix.
        }
        result = await test_db.TimeRecords.insert_one(legacy_record)
        record_id = str(result.inserted_id)
        try:
            resp = await async_client.get(
                f"/api/reports/integrity/{record_id}",
                headers=admin_headers,
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["verified"] is False
            assert body["status"] == "legacy"
            assert body["integrity_hash"] == ""
        finally:
            await test_db.TimeRecords.delete_one({"_id": result.inserted_id})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_approved_correction_recomputes_hash(
        self,
        async_client: AsyncClient,
        admin_headers: Dict[str, str],
        test_db,
    ):
        """
        An admin-approved change-request that updates a record's timestamp must
        recompute and persist integrity_hash, so the corrected record still
        verifies as "verified" (a legitimate, audited change is not tampering).
        """
        email = "integrity.correction@test.com"
        password = "IntegrityPass123!"
        company_id = None
        worker_id = None
        try:
            resp = await async_client.post(
                "/api/companies/",
                json={"name": "Integrity Correction Company"},
                headers=admin_headers,
            )
            company_id = resp.json()["id"]

            resp = await async_client.post(
                "/api/workers/",
                json={
                    "first_name": "Integrity",
                    "last_name": "Correction",
                    "email": email,
                    "phone_number": "+34600000013",
                    "id_number": "13131313D",
                    "password": password,
                    "company_ids": [company_id],
                },
                headers=admin_headers,
            )
            worker_id = resp.json()["id"]

            resp = await async_client.post(
                "/api/time-records/",
                json={"email": email, "password": password, "company_id": company_id, "action": "entry"},
                headers=admin_headers,
            )
            assert resp.status_code == 201, resp.text
            entry_id = resp.json()["id"]
            # Python 3.10's datetime.fromisoformat() can't parse a trailing "Z"
            # (support for that was added in 3.11); normalize to an explicit
            # UTC offset first.
            entry_timestamp = datetime.fromisoformat(resp.json()["timestamp"].replace("Z", "+00:00"))

            resp = await async_client.post(
                "/api/time-records/",
                json={"email": email, "password": password, "company_id": company_id, "action": "exit"},
                headers=admin_headers,
            )
            assert resp.status_code == 201, resp.text
            exit_id = resp.json()["id"]

            new_entry_timestamp = entry_timestamp - timedelta(minutes=5)

            resp = await async_client.post(
                "/api/change-requests/",
                json={
                    "email": email,
                    "password": password,
                    "date": entry_timestamp.date().isoformat(),
                    "company_id": company_id,
                    "time_record_id": entry_id,
                    "new_timestamp": new_entry_timestamp.isoformat(),
                    "reason": "Olvide fichar a la hora correcta esta mañana",
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

            # The corrected record's stored timestamp changed; its hash must
            # have been recomputed so verification still passes.
            resp = await async_client.get(
                f"/api/reports/integrity/{entry_id}",
                headers=admin_headers,
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["verified"] is True, f"corrected record failed verification: {body}"
            assert body["status"] == "verified"

            resp = await async_client.get(
                f"/api/reports/integrity/{exit_id}",
                headers=admin_headers,
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["verified"] is True, f"paired exit record failed verification: {body}"
            assert body["status"] == "verified"

        finally:
            if worker_id:
                await test_db.WorkerShiftStates.delete_many({"worker_id": worker_id})
                await test_db.TimeRecords.delete_many({"worker_id": worker_id})
                await test_db.ChangeRequests.delete_many({"worker_id": worker_id})
                await test_db.Workers.delete_one({"_id": ObjectId(worker_id)})
            if company_id:
                await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})
