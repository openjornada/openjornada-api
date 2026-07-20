"""
Tests de concurrencia para el guard atómico CAS de fichaje.

Verifica que 10 peticiones de entrada concurrentes para el mismo
trabajador/empresa producen exactamente 1×201 + 9×409, con un único
TimeRecord de tipo "entry" y un único doc WorkerShiftStates en estado
"logged_in" en la base de datos.

Requiere MongoDB de test real (time_tracking_test_db).
Se ejecuta con: pytest tests/integration/test_time_records_concurrency.py -v
"""
import asyncio
import pytest
from bson import ObjectId
from httpx import AsyncClient
from typing import Dict


class TestConcurrentEntry:
    """Guard atómico: N entradas concurrentes → 1 éxito, N-1 conflictos."""

    @pytest.mark.asyncio
    async def test_concurrent_entries_race_condition(
        self,
        async_client: AsyncClient,
        admin_headers: Dict[str, str],
        test_db,
    ):
        """
        10 peticiones entry concurrentes → exactamente 1×201 + 9×409.

        Postcondiciones en BD:
        - Exactamente 1 TimeRecord de tipo "entry" para el worker/company.
        - Exactamente 1 doc WorkerShiftStates con state="logged_in".
        """
        worker_email = "concurrency.test.worker@test.com"
        worker_password = "ConcurrencyPass123!"
        company_id = None
        worker_id = None

        try:
            # ------------------------------------------------------------------
            # Setup: empresa + trabajador
            # ------------------------------------------------------------------
            resp = await async_client.post(
                "/api/companies/",
                json={"name": "Concurrency Test Company"},
                headers=admin_headers,
            )
            assert resp.status_code == 201, f"company create failed: {resp.text}"
            company_id = resp.json()["id"]

            resp = await async_client.post(
                "/api/workers/",
                json={
                    "first_name": "Concurrency",
                    "last_name": "Test Worker",
                    "email": worker_email,
                    "phone_number": "+34600000001",
                    "id_number": "99999999X",
                    "password": worker_password,
                    "company_ids": [company_id],
                },
                headers=admin_headers,
            )
            assert resp.status_code == 201, f"worker create failed: {resp.text}"
            worker_id = resp.json()["id"]

            # ------------------------------------------------------------------
            # Ensure the unique index exists on the test DB.
            # In production init_db() creates it at startup, but the ASGITransport
            # test client does not run the app lifespan — so we create it here to
            # mirror production. Without this index the CAS guard's safety net
            # (DuplicateKeyError on concurrent first clock-in) cannot fire.
            # ------------------------------------------------------------------
            await test_db.WorkerShiftStates.create_index(
                [("worker_id", 1), ("company_id", 1)],
                unique=True,
                name="worker_company_unique",
            )

            # ------------------------------------------------------------------
            # Race: 10 concurrent entry requests
            # ------------------------------------------------------------------
            entry_payload = {
                "email": worker_email,
                "password": worker_password,
                "company_id": company_id,
                "action": "entry",
            }

            async def do_entry() -> int:
                r = await async_client.post(
                    "/api/time-records/",
                    json=entry_payload,
                    headers=admin_headers,
                )
                return r.status_code

            N = 10
            statuses = await asyncio.gather(*[do_entry() for _ in range(N)])

            # ------------------------------------------------------------------
            # HTTP response assertions
            # ------------------------------------------------------------------
            count_201 = statuses.count(201)
            count_409 = statuses.count(409)

            assert count_201 == 1, (
                f"Expected exactly 1 success (201), got {count_201}. "
                f"All statuses: {sorted(statuses)}"
            )
            assert count_409 == N - 1, (
                f"Expected {N - 1} conflicts (409), got {count_409}. "
                f"All statuses: {sorted(statuses)}"
            )

            # ------------------------------------------------------------------
            # Database state assertions
            # ------------------------------------------------------------------
            # Exactly 1 TimeRecord of type "entry"
            entry_records = await test_db.TimeRecords.find(
                {"worker_id": worker_id, "company_id": company_id, "type": "entry"}
            ).to_list(length=100)
            assert len(entry_records) == 1, (
                f"Expected exactly 1 'entry' TimeRecord in DB, found {len(entry_records)}"
            )

            # Exactly 1 WorkerShiftStates doc with state="logged_in"
            shift_docs = await test_db.WorkerShiftStates.find(
                {"worker_id": worker_id, "company_id": company_id}
            ).to_list(length=10)
            assert len(shift_docs) == 1, (
                f"Expected exactly 1 WorkerShiftStates doc, found {len(shift_docs)}"
            )
            assert shift_docs[0]["state"] == "logged_in", (
                f"Expected state='logged_in', got '{shift_docs[0]['state']}'"
            )
            assert "entry_time" in shift_docs[0], (
                "WorkerShiftStates doc should contain 'entry_time'"
            )
            assert "version" in shift_docs[0], (
                "WorkerShiftStates doc should contain 'version' fencing token"
            )

        finally:
            # ------------------------------------------------------------------
            # Cleanup (always runs)
            # ------------------------------------------------------------------
            if worker_id:
                await test_db.TimeRecords.delete_many({"worker_id": worker_id})
                await test_db.WorkerShiftStates.delete_many({"worker_id": worker_id})
                await test_db.Workers.delete_one({"_id": ObjectId(worker_id)})
            if company_id:
                await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})
