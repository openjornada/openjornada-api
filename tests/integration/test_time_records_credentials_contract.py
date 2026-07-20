"""
Contrato de credenciales de /time-records/.

Regresión: TimeRecordWorkerCredentials lo comparten create_time_record y
get_current_status. `action` debe ser OBLIGATORIO solo al crear un fichaje
(CreateTimeRecordCredentials) y OPCIONAL al consultar el estado — de lo
contrario current-status devuelve 422 y la webapp se rompe ("Cargando estado...").
"""
import pytest
from bson import ObjectId
from httpx import AsyncClient
from typing import Dict


class TestCredentialsContract:

    @pytest.mark.asyncio
    async def test_current_status_works_without_action(
        self,
        async_client: AsyncClient,
        admin_headers: Dict[str, str],
        test_db,
    ):
        """POST /time-records/current-status SIN action → 200 (no 422)."""
        email = "contract.status@test.com"
        password = "ContractPass123!"
        company_id = None
        worker_id = None
        try:
            resp = await async_client.post(
                "/api/companies/",
                json={"name": "Contract Status Company"},
                headers=admin_headers,
            )
            assert resp.status_code == 201, resp.text
            company_id = resp.json()["id"]

            resp = await async_client.post(
                "/api/workers/",
                json={
                    "first_name": "Contract",
                    "last_name": "Status",
                    "email": email,
                    "phone_number": "+34600000002",
                    "id_number": "88888888X",
                    "password": password,
                    "company_ids": [company_id],
                },
                headers=admin_headers,
            )
            assert resp.status_code == 201, resp.text
            worker_id = resp.json()["id"]

            # current-status NO envía action
            resp = await async_client.post(
                "/api/time-records/current-status",
                json={"email": email, "password": password, "company_id": company_id},
                headers=admin_headers,
            )
            assert resp.status_code == 200, f"current-status debería ser 200, fue {resp.status_code}: {resp.text}"
            assert resp.json()["status"] == "logged_out"

        finally:
            if worker_id:
                await test_db.WorkerShiftStates.delete_many({"worker_id": worker_id})
                await test_db.Workers.delete_one({"_id": ObjectId(worker_id)})
            if company_id:
                await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_create_time_record_requires_action(
        self,
        async_client: AsyncClient,
        admin_headers: Dict[str, str],
        test_db,
    ):
        """POST /time-records/ SIN action → 422 (contrato honesto de fichaje)."""
        email = "contract.create@test.com"
        password = "ContractPass123!"
        company_id = None
        worker_id = None
        try:
            resp = await async_client.post(
                "/api/companies/",
                json={"name": "Contract Create Company"},
                headers=admin_headers,
            )
            assert resp.status_code == 201, resp.text
            company_id = resp.json()["id"]

            resp = await async_client.post(
                "/api/workers/",
                json={
                    "first_name": "Contract",
                    "last_name": "Create",
                    "email": email,
                    "phone_number": "+34600000003",
                    "id_number": "77777777X",
                    "password": password,
                    "company_ids": [company_id],
                },
                headers=admin_headers,
            )
            assert resp.status_code == 201, resp.text
            worker_id = resp.json()["id"]

            resp = await async_client.post(
                "/api/time-records/",
                json={"email": email, "password": password, "company_id": company_id},
                headers=admin_headers,
            )
            assert resp.status_code == 422, f"crear sin action debería ser 422, fue {resp.status_code}: {resp.text}"

        finally:
            if worker_id:
                await test_db.WorkerShiftStates.delete_many({"worker_id": worker_id})
                await test_db.TimeRecords.delete_many({"worker_id": worker_id})
                await test_db.Workers.delete_one({"_id": ObjectId(worker_id)})
            if company_id:
                await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})
