"""
Integration tests for the workers capability.

Covers: index-alignment of ``company_names`` with ``company_ids`` in the
workers listing (even when an associated company is soft-deleted) and the
populated ``company_names`` / ``work_center_names`` on the
``GET /workers/id_number/{id_number}`` endpoint.
"""
from datetime import datetime, timezone as dt_timezone
from typing import Dict

import pytest
from bson import ObjectId
from httpx import AsyncClient


async def _create_company(client: AsyncClient, headers: Dict[str, str], name: str) -> str:
    resp = await client.post("/api/companies/", json={"name": name}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _create_worker(
    client: AsyncClient, headers: Dict[str, str], email: str, id_number: str, company_ids: list
) -> str:
    resp = await client.post(
        "/api/workers/",
        json={
            "first_name": "Work",
            "last_name": "Center",
            "email": email,
            "phone_number": "+34600000099",
            "id_number": id_number,
            "password": "WorkerPass123!",
            "company_ids": company_ids,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _create_center(
    client: AsyncClient, headers: Dict[str, str], name: str, company_id: str
) -> str:
    resp = await client.post(
        "/api/work-centers/", json={"name": name, "company_id": company_id}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _create_tracker_headers(client: AsyncClient, db) -> Dict[str, str]:
    """Crea un APIUser con role=tracker (sin update_workers) y devuelve sus headers."""
    from api.auth.auth_handler import get_password_hash
    from datetime import datetime, timezone

    tracker_email = "tracker@test.com"
    await db.APIUsers.delete_one({"email": tracker_email})
    await db.APIUsers.insert_one({
        "username": "tracker_test",
        "email": tracker_email,
        "hashed_password": get_password_hash("TrackerPass123!"),
        "role": "tracker",
        "is_active": True,
        "created_at": datetime.now(timezone.utc),
    })
    resp = await client.post(
        "/api/token",
        data={"username": tracker_email, "password": "TrackerPass123!"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


class TestCompanyNamesAlignment:

    @pytest.mark.asyncio
    async def test_company_names_aligned_when_company_soft_deleted(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db
    ):
        company_a = None
        company_b = None
        worker_id = None
        try:
            company_a = await _create_company(async_client, admin_headers, "Workers Align A")
            company_b = await _create_company(async_client, admin_headers, "Workers Align B")
            worker_id = await _create_worker(
                async_client, admin_headers, "workers.align@test.com", "30303030A",
                [company_a, company_b],
            )

            # Soft-delete company B directly in the DB (the API refuses to
            # delete a company that still has associated workers).
            await test_db.Companies.update_one(
                {"_id": ObjectId(company_b)},
                {"$set": {"deleted_at": datetime.now(dt_timezone.utc), "deleted_by": "admin_test"}},
            )

            resp = await async_client.get("/api/workers/", headers=admin_headers)
            assert resp.status_code == 200, resp.text
            worker = next(w for w in resp.json() if w["id"] == worker_id)

            # company_names must stay index-aligned with company_ids: same
            # length and order, with the soft-deleted company's name preserved.
            assert worker["company_ids"] == [company_a, company_b]
            assert worker["company_names"] == ["Workers Align A", "Workers Align B"]
            assert len(worker["company_names"]) == len(worker["company_ids"])
        finally:
            if worker_id:
                await test_db.Workers.delete_one({"_id": ObjectId(worker_id)})
            if company_a:
                await test_db.Companies.delete_one({"_id": ObjectId(company_a)})
            if company_b:
                await test_db.Companies.delete_one({"_id": ObjectId(company_b)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})


class TestGetWorkerByIdNumber:

    @pytest.mark.asyncio
    async def test_get_worker_by_id_number_populates_names(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db
    ):
        company_id = None
        worker_id = None
        try:
            company_id = await _create_company(async_client, admin_headers, "Workers ByIdn Company")
            worker_id = await _create_worker(
                async_client, admin_headers, "workers.byidn@test.com", "30303030C", [company_id]
            )
            center_id = await _create_center(async_client, admin_headers, "Central", company_id)
            resp = await async_client.put(
                f"/api/workers/{worker_id}/work-center",
                json={"company_id": company_id, "work_center_id": center_id},
                headers=admin_headers,
            )
            assert resp.status_code == 200, resp.text

            resp = await async_client.get(
                "/api/workers/id_number/30303030C", headers=admin_headers
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["id"] == worker_id
            assert data["company_ids"] == [company_id]
            assert data["company_names"] == ["Workers ByIdn Company"]
            assert data["work_center_assignments"] == {company_id: center_id}
            assert data["work_center_names"] == {company_id: "Central"}
        finally:
            if worker_id:
                await test_db.Workers.delete_one({"_id": ObjectId(worker_id)})
            if company_id:
                await test_db.WorkCenters.delete_many({"company_id": company_id})
                await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})


class TestBulkWorkCenter:

    @pytest.mark.asyncio
    async def test_bulk_assign_filters_by_company(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db
    ):
        """Assign solo toca a los trabajadores de la empresa del centro."""
        company_a = company_b = center_id = None
        worker_ids = []
        try:
            company_a = await _create_company(async_client, admin_headers, "Bulk WC A")
            company_b = await _create_company(async_client, admin_headers, "Bulk WC B")
            w_a1 = await _create_worker(
                async_client, admin_headers, "bulk.wc.a1@test.com", "30303031A", [company_a]
            )
            w_a2 = await _create_worker(
                async_client, admin_headers, "bulk.wc.a2@test.com", "30303032A", [company_a]
            )
            w_b = await _create_worker(
                async_client, admin_headers, "bulk.wc.b@test.com", "30303033A", [company_b]
            )
            worker_ids = [w_a1, w_a2, w_b]
            center_id = await _create_center(async_client, admin_headers, "Bulk Central", company_a)

            resp = await async_client.post(
                "/api/workers/bulk-work-center",
                json={
                    "worker_ids": worker_ids,
                    "action": "assign",
                    "company_id": company_a,
                    "work_center_id": center_id,
                },
                headers=admin_headers,
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["total"] == 3
            assert data["updated"] == 2
            assert data["skipped"] == 1
            assert data["detail"] == "Trabajadores que no pertenecen a la empresa del centro omitidos"

            for wid in (w_a1, w_a2):
                doc = await test_db.Workers.find_one({"_id": ObjectId(wid)})
                assert doc["work_center_assignments"] == {company_a: center_id}
            doc_b = await test_db.Workers.find_one({"_id": ObjectId(w_b)})
            assert doc_b.get("work_center_assignments", {}) == {}
        finally:
            for wid in worker_ids:
                await test_db.Workers.delete_one({"_id": ObjectId(wid)})
            if center_id:
                await test_db.WorkCenters.delete_one({"_id": ObjectId(center_id)})
            if company_a:
                await test_db.Companies.delete_one({"_id": ObjectId(company_a)})
            if company_b:
                await test_db.Companies.delete_one({"_id": ObjectId(company_b)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_bulk_assign_idempotent(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db
    ):
        """Reasignar el mismo centro devuelve updated = matched count, sin errores."""
        company_id = center_id = None
        worker_ids = []
        try:
            company_id = await _create_company(async_client, admin_headers, "Bulk WC Idem")
            w1 = await _create_worker(
                async_client, admin_headers, "bulk.wc.idem1@test.com", "30303034A", [company_id]
            )
            w2 = await _create_worker(
                async_client, admin_headers, "bulk.wc.idem2@test.com", "30303035A", [company_id]
            )
            worker_ids = [w1, w2]
            center_id = await _create_center(async_client, admin_headers, "Idem Central", company_id)

            payload = {
                "worker_ids": worker_ids,
                "action": "assign",
                "company_id": company_id,
                "work_center_id": center_id,
            }
            resp = await async_client.post(
                "/api/workers/bulk-work-center", json=payload, headers=admin_headers
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["updated"] == 2

            resp = await async_client.post(
                "/api/workers/bulk-work-center", json=payload, headers=admin_headers
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["total"] == 2
            assert data["updated"] == 2
            assert data["skipped"] == 0
        finally:
            for wid in worker_ids:
                await test_db.Workers.delete_one({"_id": ObjectId(wid)})
            if center_id:
                await test_db.WorkCenters.delete_one({"_id": ObjectId(center_id)})
            if company_id:
                await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_bulk_clear_wipes_all_companies(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db
    ):
        """clear elimina TODAS las asignaciones del trabajador, no solo una empresa."""
        company_a = company_b = None
        worker_id = None
        try:
            company_a = await _create_company(async_client, admin_headers, "Bulk Clear A")
            company_b = await _create_company(async_client, admin_headers, "Bulk Clear B")
            worker_id = await _create_worker(
                async_client, admin_headers, "bulk.wc.clear@test.com", "30303036A",
                [company_a, company_b],
            )
            center_a = await _create_center(async_client, admin_headers, "Clear Central A", company_a)
            center_b = await _create_center(async_client, admin_headers, "Clear Central B", company_b)

            # Asignar en las dos empresas directamente en BD (el endpoint single
            # solo permite una empresa por request).
            await test_db.Workers.update_one(
                {"_id": ObjectId(worker_id)},
                {"$set": {
                    "work_center_assignments": {company_a: center_a, company_b: center_b},
                }},
            )

            resp = await async_client.post(
                "/api/workers/bulk-work-center",
                json={"worker_ids": [worker_id], "action": "clear"},
                headers=admin_headers,
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["total"] == 1
            assert data["updated"] == 1
            assert data["skipped"] == 0

            doc = await test_db.Workers.find_one({"_id": ObjectId(worker_id)})
            assert doc["work_center_assignments"] == {}
        finally:
            if worker_id:
                await test_db.Workers.delete_one({"_id": ObjectId(worker_id)})
            if company_a:
                await test_db.WorkCenters.delete_many({"company_id": company_a})
                await test_db.Companies.delete_one({"_id": ObjectId(company_a)})
            if company_b:
                await test_db.WorkCenters.delete_many({"company_id": company_b})
                await test_db.Companies.delete_one({"_id": ObjectId(company_b)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_bulk_clear_scoped_to_company(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db
    ):
        """clear con company_id solo borra la asignación de esa empresa."""
        company_a = company_b = None
        worker_id = None
        try:
            company_a = await _create_company(async_client, admin_headers, "Bulk Clear Scoped A")
            company_b = await _create_company(async_client, admin_headers, "Bulk Clear Scoped B")
            worker_id = await _create_worker(
                async_client, admin_headers, "bulk.wc.clear.scoped@test.com", "30303038A",
                [company_a, company_b],
            )
            center_a = await _create_center(async_client, admin_headers, "Clear Scoped Central A", company_a)
            center_b = await _create_center(async_client, admin_headers, "Clear Scoped Central B", company_b)

            await test_db.Workers.update_one(
                {"_id": ObjectId(worker_id)},
                {"$set": {
                    "work_center_assignments": {company_a: center_a, company_b: center_b},
                }},
            )

            resp = await async_client.post(
                "/api/workers/bulk-work-center",
                json={"worker_ids": [worker_id], "action": "clear", "company_id": company_a},
                headers=admin_headers,
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["total"] == 1
            assert data["updated"] == 1
            assert data["skipped"] == 0

            doc = await test_db.Workers.find_one({"_id": ObjectId(worker_id)})
            assert doc["work_center_assignments"] == {company_b: center_b}
        finally:
            if worker_id:
                await test_db.Workers.delete_one({"_id": ObjectId(worker_id)})
            if company_a:
                await test_db.WorkCenters.delete_many({"company_id": company_a})
                await test_db.Companies.delete_one({"_id": ObjectId(company_a)})
            if company_b:
                await test_db.WorkCenters.delete_many({"company_id": company_b})
                await test_db.Companies.delete_one({"_id": ObjectId(company_b)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_bulk_clear_invalid_company_id_rejected(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db
    ):
        """clear con company_id mal formado → 400 worker.invalid_company_id."""
        try:
            resp = await async_client.post(
                "/api/workers/bulk-work-center",
                json={
                    "worker_ids": ["507f1f77bcf86cd799439011"],
                    "action": "clear",
                    "company_id": "not-an-object-id",
                },
                headers=admin_headers,
            )
            assert resp.status_code == 400, resp.text
            assert resp.json()["detail"]["error_code"] == "worker.invalid_company_id"
        finally:
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_bulk_assign_duplicate_worker_ids_deduplicated(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db
    ):
        """IDs duplicados en worker_ids no inflan total/skipped."""
        company_id = center_id = None
        worker_id = None
        try:
            company_id = await _create_company(async_client, admin_headers, "Bulk WC Dedup")
            worker_id = await _create_worker(
                async_client, admin_headers, "bulk.wc.dedup@test.com", "30303039A", [company_id]
            )
            center_id = await _create_center(async_client, admin_headers, "Dedup Central", company_id)

            resp = await async_client.post(
                "/api/workers/bulk-work-center",
                json={
                    "worker_ids": [worker_id, worker_id],
                    "action": "assign",
                    "company_id": company_id,
                    "work_center_id": center_id,
                },
                headers=admin_headers,
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["total"] == 1
            assert data["updated"] == 1
            assert data["skipped"] == 0
        finally:
            if worker_id:
                await test_db.Workers.delete_one({"_id": ObjectId(worker_id)})
            if center_id:
                await test_db.WorkCenters.delete_one({"_id": ObjectId(center_id)})
            if company_id:
                await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_bulk_assign_center_of_another_company_rejected(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db
    ):
        """Centro de otra empresa → 400 work_center.not_found."""
        company_a = company_b = center_b = None
        worker_id = None
        try:
            company_a = await _create_company(async_client, admin_headers, "Bulk Reject A")
            company_b = await _create_company(async_client, admin_headers, "Bulk Reject B")
            worker_id = await _create_worker(
                async_client, admin_headers, "bulk.wc.reject@test.com", "30303037A", [company_a]
            )
            center_b = await _create_center(async_client, admin_headers, "Reject Central B", company_b)

            resp = await async_client.post(
                "/api/workers/bulk-work-center",
                json={
                    "worker_ids": [worker_id],
                    "action": "assign",
                    "company_id": company_a,
                    "work_center_id": center_b,
                },
                headers=admin_headers,
            )
            assert resp.status_code == 400, resp.text
            assert resp.json()["detail"]["error_code"] == "work_center.not_found"
        finally:
            if worker_id:
                await test_db.Workers.delete_one({"_id": ObjectId(worker_id)})
            if center_b:
                await test_db.WorkCenters.delete_one({"_id": ObjectId(center_b)})
            if company_a:
                await test_db.Companies.delete_one({"_id": ObjectId(company_a)})
            if company_b:
                await test_db.Companies.delete_one({"_id": ObjectId(company_b)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_bulk_assign_missing_company_or_center_rejected(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db
    ):
        """Faltan company_id/work_center_id para assign → 400 work_center.no_update_data."""
        try:
            for payload in (
                {"worker_ids": ["507f1f77bcf86cd799439011"], "action": "assign"},
                {
                    "worker_ids": ["507f1f77bcf86cd799439011"],
                    "action": "assign",
                    "company_id": "507f1f77bcf86cd799439012",
                },
                {
                    "worker_ids": ["507f1f77bcf86cd799439011"],
                    "action": "assign",
                    "work_center_id": "507f1f77bcf86cd799439013",
                },
            ):
                resp = await async_client.post(
                    "/api/workers/bulk-work-center", json=payload, headers=admin_headers
                )
                assert resp.status_code == 400, resp.text
                assert resp.json()["detail"]["error_code"] == "work_center.no_update_data"
        finally:
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_bulk_assign_empty_worker_ids_rejected(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db
    ):
        """worker_ids vacío → 422 (validación de esquema)."""
        try:
            resp = await async_client.post(
                "/api/workers/bulk-work-center",
                json={"worker_ids": [], "action": "clear"},
                headers=admin_headers,
            )
            assert resp.status_code == 422, resp.text
        finally:
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_bulk_assign_requires_update_workers_permission(
        self, async_client: AsyncClient, test_db
    ):
        """Rol tracker (sin update_workers) → 403."""
        tracker_headers = await _create_tracker_headers(async_client, test_db)
        try:
            resp = await async_client.post(
                "/api/workers/bulk-work-center",
                json={"worker_ids": ["507f1f77bcf86cd799439011"], "action": "clear"},
                headers=tracker_headers,
            )
            assert resp.status_code == 403, resp.text
            assert resp.json()["detail"]["error_code"] == "auth.insufficient_permissions"
        finally:
            await test_db.APIUsers.delete_one({"email": "tracker@test.com"})