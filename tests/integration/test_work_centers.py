"""
Integration tests for the work-centers capability.

Covers: CRUD of work centers (company filter, invalid company), soft-delete
with auto-unassignment and snapshot preservation, worker->center assignment
(valid/invalid/null), the work_center_id filter on workers, the historical
snapshot on time records (with/without assignment, not rewritten), and
per-endpoint permission authorization.
"""
from datetime import datetime, timezone as dt_timezone
from typing import Dict
from unittest.mock import patch

import pytest
from bson import ObjectId
from httpx import AsyncClient

TRACKER_PASSWORD = "TrackerPass123!"
INSPECTOR_PASSWORD = "InspectorPass123!"


async def _create_role_headers(client: AsyncClient, db, role: str, password: str) -> Dict[str, str]:
    """Create an APIUser with the given role and return auth headers."""
    from api.auth.auth_handler import get_password_hash

    email = f"{role}@test.com"
    await db.APIUsers.delete_one({"email": email})
    await db.APIUsers.insert_one({
        "username": f"{role}_test",
        "email": email,
        "hashed_password": get_password_hash(password),
        "role": role,
        "is_active": True,
        "created_at": datetime.now(dt_timezone.utc),
    })
    resp = await client.post(
        "/api/token",
        data={"username": email, "password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


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
    client: AsyncClient, headers: Dict[str, str], name: str, company_id: str, **extra
) -> str:
    payload = {"name": name, "company_id": company_id}
    payload.update(extra)
    resp = await client.post("/api/work-centers/", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


class TestWorkCenterCrud:

    @pytest.mark.asyncio
    async def test_create_center_in_existing_company(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db
    ):
        company_id = None
        try:
            company_id = await _create_company(async_client, admin_headers, "WC Create Company")
            resp = await async_client.post(
                "/api/work-centers/",
                json={"name": "Central", "code": "MAD-01", "address": "Calle Mayor 1", "company_id": company_id},
                headers=admin_headers,
            )
            assert resp.status_code == 201, resp.text
            data = resp.json()
            assert data["name"] == "Central"
            assert data["code"] == "MAD-01"
            assert data["address"] == "Calle Mayor 1"
            assert data["company_id"] == company_id
            assert data["company_name"] == "WC Create Company"
            assert data["created_at"] is not None
            assert data["deleted_at"] is None

            stored = await test_db.WorkCenters.find_one({"_id": ObjectId(data["id"])})
            assert stored is not None
            assert stored["deleted_at"] is None
        finally:
            if company_id:
                await test_db.WorkCenters.delete_many({"company_id": company_id})
                await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_create_center_with_nonexistent_company_rejected(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db
    ):
        try:
            resp = await async_client.post(
                "/api/work-centers/",
                json={"name": "Central", "company_id": str(ObjectId())},
                headers=admin_headers,
            )
            assert resp.status_code == 400, resp.text
            assert resp.json()["detail"]["error_code"] == "work_center.company_not_found"
            assert await test_db.WorkCenters.count_documents({}) == 0
        finally:
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_create_center_with_deleted_company_rejected(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db
    ):
        company_id = None
        try:
            company_id = await _create_company(async_client, admin_headers, "WC Deleted Company")
            await async_client.delete(f"/api/companies/{company_id}", headers=admin_headers)

            resp = await async_client.post(
                "/api/work-centers/",
                json={"name": "Central", "company_id": company_id},
                headers=admin_headers,
            )
            assert resp.status_code == 400, resp.text
            assert resp.json()["detail"]["error_code"] == "work_center.company_not_found"
        finally:
            if company_id:
                await test_db.WorkCenters.delete_many({"company_id": company_id})
                await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_duplicate_names_allowed(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db
    ):
        company_id = None
        try:
            company_id = await _create_company(async_client, admin_headers, "WC Dup Company")
            first = await _create_center(async_client, admin_headers, "Central", company_id)
            second = await _create_center(async_client, admin_headers, "Central", company_id)
            assert first != second
        finally:
            if company_id:
                await test_db.WorkCenters.delete_many({"company_id": company_id})
                await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_list_centers_sorted_with_company_name_and_filter(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db
    ):
        company_a = None
        company_b = None
        try:
            company_a = await _create_company(async_client, admin_headers, "WC List A")
            company_b = await _create_company(async_client, admin_headers, "WC List B")
            await _create_center(async_client, admin_headers, "Zeta", company_a)
            await _create_center(async_client, admin_headers, "Alfa", company_a)
            await _create_center(async_client, admin_headers, "Beta", company_b)

            resp = await async_client.get("/api/work-centers/", headers=admin_headers)
            assert resp.status_code == 200, resp.text
            all_centers = resp.json()
            assert len(all_centers) == 3
            assert [c["name"] for c in all_centers] == ["Alfa", "Beta", "Zeta"]
            assert all(c["company_name"] in ("WC List A", "WC List B") for c in all_centers)

            resp = await async_client.get(
                f"/api/work-centers/?company_id={company_a}", headers=admin_headers
            )
            assert resp.status_code == 200, resp.text
            filtered = resp.json()
            assert [c["name"] for c in filtered] == ["Alfa", "Zeta"]
            assert all(c["company_id"] == company_a for c in filtered)
        finally:
            if company_a:
                await test_db.WorkCenters.delete_many({"company_id": company_a})
                await test_db.Companies.delete_one({"_id": ObjectId(company_a)})
            if company_b:
                await test_db.WorkCenters.delete_many({"company_id": company_b})
                await test_db.Companies.delete_one({"_id": ObjectId(company_b)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_get_center_by_id_and_not_found(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db
    ):
        company_id = None
        try:
            company_id = await _create_company(async_client, admin_headers, "WC Get Company")
            center_id = await _create_center(async_client, admin_headers, "Central", company_id)

            resp = await async_client.get(f"/api/work-centers/{center_id}", headers=admin_headers)
            assert resp.status_code == 200, resp.text
            assert resp.json()["name"] == "Central"
            assert resp.json()["company_name"] == "WC Get Company"

            resp = await async_client.get(
                f"/api/work-centers/{str(ObjectId())}", headers=admin_headers
            )
            assert resp.status_code == 404, resp.text
            assert resp.json()["detail"]["error_code"] == "work_center.not_found"
        finally:
            if company_id:
                await test_db.WorkCenters.delete_many({"company_id": company_id})
                await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_update_center_editable_fields_only(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db
    ):
        company_a = None
        company_b = None
        try:
            company_a = await _create_company(async_client, admin_headers, "WC Update A")
            company_b = await _create_company(async_client, admin_headers, "WC Update B")
            center_id = await _create_center(async_client, admin_headers, "Central", company_a)

            resp = await async_client.put(
                f"/api/work-centers/{center_id}",
                json={"name": "Norte", "code": "N-01", "address": "Av. Norte 2"},
                headers=admin_headers,
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["name"] == "Norte"
            assert data["code"] == "N-01"
            assert data["address"] == "Av. Norte 2"
            # company_id is immutable: sending it must be ignored
            assert data["company_id"] == company_a

            resp = await async_client.put(
                f"/api/work-centers/{center_id}",
                json={"name": "Oeste", "company_id": company_b},
                headers=admin_headers,
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["name"] == "Oeste"
            assert resp.json()["company_id"] == company_a

            resp = await async_client.put(
                f"/api/work-centers/{center_id}", json={}, headers=admin_headers
            )
            assert resp.status_code == 400, resp.text
            assert resp.json()["detail"]["error_code"] == "work_center.no_update_data"
        finally:
            if company_a:
                await test_db.WorkCenters.delete_many({"company_id": company_a})
                await test_db.Companies.delete_one({"_id": ObjectId(company_a)})
            if company_b:
                await test_db.Companies.delete_one({"_id": ObjectId(company_b)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_delete_center_soft_deletes_and_excludes_from_list(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db
    ):
        company_id = None
        try:
            company_id = await _create_company(async_client, admin_headers, "WC Delete Company")
            center_id = await _create_center(async_client, admin_headers, "Central", company_id)

            resp = await async_client.delete(f"/api/work-centers/{center_id}", headers=admin_headers)
            assert resp.status_code == 204, resp.text

            stored = await test_db.WorkCenters.find_one({"_id": ObjectId(center_id)})
            assert stored["deleted_at"] is not None
            assert stored["deleted_by"] == "admin_test"

            resp = await async_client.get("/api/work-centers/", headers=admin_headers)
            assert resp.status_code == 200, resp.text
            assert all(c["id"] != center_id for c in resp.json())

            resp = await async_client.get(f"/api/work-centers/{center_id}", headers=admin_headers)
            assert resp.status_code == 404, resp.text
        finally:
            if company_id:
                await test_db.WorkCenters.delete_many({"company_id": company_id})
                await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_create_center_rejects_overlong_code_and_address(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db
    ):
        """code > 50 chars or address > 300 chars is rejected with 422."""
        company_id = None
        try:
            company_id = await _create_company(async_client, admin_headers, "WC Limits Company")

            resp = await async_client.post(
                "/api/work-centers/",
                json={"name": "Central", "code": "C" * 51, "company_id": company_id},
                headers=admin_headers,
            )
            assert resp.status_code == 422, resp.text

            resp = await async_client.post(
                "/api/work-centers/",
                json={"name": "Central", "address": "A" * 301, "company_id": company_id},
                headers=admin_headers,
            )
            assert resp.status_code == 422, resp.text

            assert await test_db.WorkCenters.count_documents({"company_id": company_id}) == 0
        finally:
            if company_id:
                await test_db.WorkCenters.delete_many({"company_id": company_id})
                await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_create_center_accepts_max_length_code_and_address(
        self, async_client: AsyncClient, admin_headers: Dict[str, str], test_db
    ):
        """code of exactly 50 chars and address of exactly 300 chars are accepted."""
        company_id = None
        try:
            company_id = await _create_company(async_client, admin_headers, "WC MaxLen Company")
            code = "C" * 50
            address = "A" * 300

            resp = await async_client.post(
                "/api/work-centers/",
                json={"name": "Central", "code": code, "address": address, "company_id": company_id},
                headers=admin_headers,
            )
            assert resp.status_code == 201, resp.text
            data = resp.json()
            assert data["code"] == code
            assert data["address"] == address
        finally:
            if company_id:
                await test_db.WorkCenters.delete_many({"company_id": company_id})
                await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})


class TestWorkCenterAssignment:

    @pytest.mark.asyncio
    async def test_assign_valid_center(self, async_client, admin_headers, test_db):
        company_id = None
        worker_id = None
        try:
            company_id = await _create_company(async_client, admin_headers, "WC Assign Company")
            worker_id = await _create_worker(
                async_client, admin_headers, "wc.assign@test.com", "20202020A", [company_id]
            )
            center_id = await _create_center(async_client, admin_headers, "Central", company_id)

            resp = await async_client.put(
                f"/api/workers/{worker_id}/work-center",
                json={"company_id": company_id, "work_center_id": center_id},
                headers=admin_headers,
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["work_center_assignments"] == {company_id: center_id}
            assert data["work_center_names"] == {company_id: "Central"}

            stored = await test_db.Workers.find_one({"_id": ObjectId(worker_id)})
            assert stored["work_center_assignments"] == {company_id: center_id}
        finally:
            if worker_id:
                await test_db.Workers.delete_one({"_id": ObjectId(worker_id)})
            if company_id:
                await test_db.WorkCenters.delete_many({"company_id": company_id})
                await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_assign_company_not_of_worker_rejected(
        self, async_client, admin_headers, test_db
    ):
        company_a = None
        company_b = None
        worker_id = None
        try:
            company_a = await _create_company(async_client, admin_headers, "WC Assign A")
            company_b = await _create_company(async_client, admin_headers, "WC Assign B")
            worker_id = await _create_worker(
                async_client, admin_headers, "wc.assign.bad@test.com", "20202020B", [company_a]
            )
            center_b = await _create_center(async_client, admin_headers, "Beta", company_b)

            resp = await async_client.put(
                f"/api/workers/{worker_id}/work-center",
                json={"company_id": company_b, "work_center_id": center_b},
                headers=admin_headers,
            )
            assert resp.status_code == 400, resp.text
            assert resp.json()["detail"]["error_code"] == "worker.company_not_associated"
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
    async def test_assign_center_of_another_company_rejected(
        self, async_client, admin_headers, test_db
    ):
        company_a = None
        company_b = None
        worker_id = None
        try:
            company_a = await _create_company(async_client, admin_headers, "WC Assign C")
            company_b = await _create_company(async_client, admin_headers, "WC Assign D")
            worker_id = await _create_worker(
                async_client, admin_headers, "wc.assign.mismatch@test.com", "20202020C", [company_a]
            )
            center_b = await _create_center(async_client, admin_headers, "Beta", company_b)

            resp = await async_client.put(
                f"/api/workers/{worker_id}/work-center",
                json={"company_id": company_a, "work_center_id": center_b},
                headers=admin_headers,
            )
            assert resp.status_code == 400, resp.text
            assert resp.json()["detail"]["error_code"] == "work_center.not_found"
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
    async def test_assign_deleted_center_rejected(
        self, async_client, admin_headers, test_db
    ):
        company_id = None
        worker_id = None
        try:
            company_id = await _create_company(async_client, admin_headers, "WC Assign Deleted")
            worker_id = await _create_worker(
                async_client, admin_headers, "wc.assign.deleted@test.com", "20202020D", [company_id]
            )
            center_id = await _create_center(async_client, admin_headers, "Central", company_id)
            await async_client.delete(f"/api/work-centers/{center_id}", headers=admin_headers)

            resp = await async_client.put(
                f"/api/workers/{worker_id}/work-center",
                json={"company_id": company_id, "work_center_id": center_id},
                headers=admin_headers,
            )
            assert resp.status_code == 400, resp.text
            assert resp.json()["detail"]["error_code"] == "work_center.not_found"
        finally:
            if worker_id:
                await test_db.Workers.delete_one({"_id": ObjectId(worker_id)})
            if company_id:
                await test_db.WorkCenters.delete_many({"company_id": company_id})
                await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_null_clears_assignment(self, async_client, admin_headers, test_db):
        company_id = None
        worker_id = None
        try:
            company_id = await _create_company(async_client, admin_headers, "WC Clear Company")
            worker_id = await _create_worker(
                async_client, admin_headers, "wc.clear@test.com", "20202020E", [company_id]
            )
            center_id = await _create_center(async_client, admin_headers, "Central", company_id)

            await async_client.put(
                f"/api/workers/{worker_id}/work-center",
                json={"company_id": company_id, "work_center_id": center_id},
                headers=admin_headers,
            )

            resp = await async_client.put(
                f"/api/workers/{worker_id}/work-center",
                json={"company_id": company_id, "work_center_id": None},
                headers=admin_headers,
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["work_center_assignments"] == {}
            assert resp.json()["work_center_names"] == {}

            stored = await test_db.Workers.find_one({"_id": ObjectId(worker_id)})
            assert stored.get("work_center_assignments", {}) == {}
        finally:
            if worker_id:
                await test_db.Workers.delete_one({"_id": ObjectId(worker_id)})
            if company_id:
                await test_db.WorkCenters.delete_many({"company_id": company_id})
                await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_worker_in_two_companies_keeps_independent_assignments(
        self, async_client, admin_headers, test_db
    ):
        company_a = None
        company_b = None
        worker_id = None
        try:
            company_a = await _create_company(async_client, admin_headers, "WC Multi A")
            company_b = await _create_company(async_client, admin_headers, "WC Multi B")
            worker_id = await _create_worker(
                async_client, admin_headers, "wc.multi@test.com", "20202020F", [company_a, company_b]
            )
            center_a = await _create_center(async_client, admin_headers, "Alfa", company_a)
            center_b = await _create_center(async_client, admin_headers, "Beta", company_b)

            resp = await async_client.put(
                f"/api/workers/{worker_id}/work-center",
                json={"company_id": company_a, "work_center_id": center_a},
                headers=admin_headers,
            )
            assert resp.status_code == 200, resp.text
            resp = await async_client.put(
                f"/api/workers/{worker_id}/work-center",
                json={"company_id": company_b, "work_center_id": center_b},
                headers=admin_headers,
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["work_center_assignments"] == {company_a: center_a, company_b: center_b}
            assert data["work_center_names"] == {company_a: "Alfa", company_b: "Beta"}
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
    async def test_assign_does_not_clobber_other_company_assignment(
        self, async_client, admin_headers, test_db
    ):
        """Assigning company A keeps company B's existing assignment intact."""
        company_a = None
        company_b = None
        worker_id = None
        try:
            company_a = await _create_company(async_client, admin_headers, "WC Clobber A")
            company_b = await _create_company(async_client, admin_headers, "WC Clobber B")
            worker_id = await _create_worker(
                async_client, admin_headers, "wc.clobber@test.com", "20202020P", [company_a, company_b]
            )
            center_a = await _create_center(async_client, admin_headers, "Alfa", company_a)
            center_b = await _create_center(async_client, admin_headers, "Beta", company_b)

            # Seed company B's assignment directly on the document.
            await test_db.Workers.update_one(
                {"_id": ObjectId(worker_id)},
                {"$set": {"work_center_assignments": {company_b: center_b}}},
            )

            resp = await async_client.put(
                f"/api/workers/{worker_id}/work-center",
                json={"company_id": company_a, "work_center_id": center_a},
                headers=admin_headers,
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["work_center_assignments"] == {company_a: center_a, company_b: center_b}
            assert data["work_center_names"] == {company_a: "Alfa", company_b: "Beta"}

            stored = await test_db.Workers.find_one({"_id": ObjectId(worker_id)})
            assert stored["work_center_assignments"] == {company_a: center_a, company_b: center_b}
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
    async def test_assign_survives_stale_read_of_other_company_assignment(
        self, async_client, admin_headers, test_db
    ):
        """Regression for the read-modify-write clobber.

        Simulates a concurrent request that wrote company B's assignment after
        this request had read the worker document: the first DB read returns a
        stale worker without B, while the real document already has B. A
        whole-map ``$set`` would wipe B; the targeted ``$set`` must not.
        """
        from api.database import db as app_db
        import api.routers.workers as workers_module

        company_a = None
        company_b = None
        worker_id = None
        try:
            company_a = await _create_company(async_client, admin_headers, "WC Stale A")
            company_b = await _create_company(async_client, admin_headers, "WC Stale B")
            worker_id = await _create_worker(
                async_client, admin_headers, "wc.stale@test.com", "20202020Q", [company_a, company_b]
            )
            center_a = await _create_center(async_client, admin_headers, "Alfa", company_a)
            center_b = await _create_center(async_client, admin_headers, "Beta", company_b)

            # Concurrent write: company B is assigned after this request's read.
            await test_db.Workers.update_one(
                {"_id": ObjectId(worker_id)},
                {"$set": {"work_center_assignments": {company_b: center_b}}},
            )
            stale_worker = await test_db.Workers.find_one({"_id": ObjectId(worker_id)})
            stale_worker["work_center_assignments"] = {}

            real_find_one = app_db.Workers.find_one
            calls = {"n": 0}

            async def find_one_stale_first(*args, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    return stale_worker
                return await real_find_one(*args, **kwargs)

            # Motor collections ignore instance attribute assignment, so patch
            # the router's module-level `db` with a thin proxy that only
            # intercepts Workers.find_one (auth/companies/centers stay real).
            class _WorkersProxy:
                def __init__(self, real_collection, find_one):
                    self._real = real_collection
                    self._find_one = find_one

                def __getattr__(self, name):
                    if name == "find_one":
                        return self._find_one
                    return getattr(self._real, name)

            class _DbProxy:
                def __init__(self, real_db, workers_find_one):
                    self._real = real_db
                    self._workers_find_one = workers_find_one

                def __getattr__(self, name):
                    if name == "Workers":
                        return _WorkersProxy(self._real.Workers, self._workers_find_one)
                    return getattr(self._real, name)

            with patch.object(workers_module, "db", _DbProxy(app_db, find_one_stale_first)):
                resp = await async_client.put(
                    f"/api/workers/{worker_id}/work-center",
                    json={"company_id": company_a, "work_center_id": center_a},
                    headers=admin_headers,
                )
            assert resp.status_code == 200, resp.text
            assert resp.json()["work_center_assignments"] == {company_a: center_a, company_b: center_b}

            stored = await test_db.Workers.find_one({"_id": ObjectId(worker_id)})
            assert stored["work_center_assignments"] == {company_a: center_a, company_b: center_b}
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
    async def test_clear_removes_only_target_company_assignment(
        self, async_client, admin_headers, test_db
    ):
        """Clearing company A leaves company B's assignment untouched."""
        company_a = None
        company_b = None
        worker_id = None
        try:
            company_a = await _create_company(async_client, admin_headers, "WC ClearOnly A")
            company_b = await _create_company(async_client, admin_headers, "WC ClearOnly B")
            worker_id = await _create_worker(
                async_client, admin_headers, "wc.clearonly@test.com", "20202020R", [company_a, company_b]
            )
            center_a = await _create_center(async_client, admin_headers, "Alfa", company_a)
            center_b = await _create_center(async_client, admin_headers, "Beta", company_b)

            await test_db.Workers.update_one(
                {"_id": ObjectId(worker_id)},
                {"$set": {"work_center_assignments": {company_a: center_a, company_b: center_b}}},
            )

            resp = await async_client.put(
                f"/api/workers/{worker_id}/work-center",
                json={"company_id": company_a, "work_center_id": None},
                headers=admin_headers,
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["work_center_assignments"] == {company_b: center_b}
            assert resp.json()["work_center_names"] == {company_b: "Beta"}

            stored = await test_db.Workers.find_one({"_id": ObjectId(worker_id)})
            assert stored["work_center_assignments"] == {company_b: center_b}
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
    async def test_removing_company_from_worker_clears_assignment(
        self, async_client, admin_headers, test_db
    ):
        company_a = None
        company_b = None
        worker_id = None
        try:
            company_a = await _create_company(async_client, admin_headers, "WC Orphan A")
            company_b = await _create_company(async_client, admin_headers, "WC Orphan B")
            worker_id = await _create_worker(
                async_client, admin_headers, "wc.orphan@test.com", "20202020G", [company_a, company_b]
            )
            center_a = await _create_center(async_client, admin_headers, "Alfa", company_a)
            center_b = await _create_center(async_client, admin_headers, "Beta", company_b)
            await async_client.put(
                f"/api/workers/{worker_id}/work-center",
                json={"company_id": company_a, "work_center_id": center_a},
                headers=admin_headers,
            )
            await async_client.put(
                f"/api/workers/{worker_id}/work-center",
                json={"company_id": company_b, "work_center_id": center_b},
                headers=admin_headers,
            )

            # Remove company_b from the worker: its assignment must vanish too
            resp = await async_client.put(
                f"/api/workers/{worker_id}",
                json={"company_ids": [company_a]},
                headers=admin_headers,
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["work_center_assignments"] == {company_a: center_a}

            stored = await test_db.Workers.find_one({"_id": ObjectId(worker_id)})
            assert stored["work_center_assignments"] == {company_a: center_a}
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


class TestWorkCenterFilterOnWorkers:

    @pytest.mark.asyncio
    async def test_work_center_id_filter(self, async_client, admin_headers, test_db):
        company_id = None
        worker_a = None
        worker_b = None
        try:
            company_id = await _create_company(async_client, admin_headers, "WC Filter Company")
            worker_a = await _create_worker(
                async_client, admin_headers, "wc.filter.a@test.com", "20202020H", [company_id]
            )
            worker_b = await _create_worker(
                async_client, admin_headers, "wc.filter.b@test.com", "20202020I", [company_id]
            )
            center_id = await _create_center(async_client, admin_headers, "Central", company_id)

            await async_client.put(
                f"/api/workers/{worker_a}/work-center",
                json={"company_id": company_id, "work_center_id": center_id},
                headers=admin_headers,
            )

            resp = await async_client.get(
                f"/api/workers/?work_center_id={center_id}", headers=admin_headers
            )
            assert resp.status_code == 200, resp.text
            ids = [w["id"] for w in resp.json()]
            assert worker_a in ids
            assert worker_b not in ids

            # The listing also populates work_center_names
            worker_a_data = next(w for w in resp.json() if w["id"] == worker_a)
            assert worker_a_data["work_center_names"] == {company_id: "Central"}
        finally:
            if worker_a:
                await test_db.Workers.delete_one({"_id": ObjectId(worker_a)})
            if worker_b:
                await test_db.Workers.delete_one({"_id": ObjectId(worker_b)})
            if company_id:
                await test_db.WorkCenters.delete_many({"company_id": company_id})
                await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})


class TestWorkCenterSnapshot:

    @pytest.mark.asyncio
    async def test_clock_in_with_assignment_stores_snapshot(
        self, async_client, admin_headers, test_db
    ):
        company_id = None
        worker_id = None
        try:
            company_id = await _create_company(async_client, admin_headers, "WC Snapshot Company")
            worker_id = await _create_worker(
                async_client, admin_headers, "wc.snapshot@test.com", "20202020J", [company_id]
            )
            center_id = await _create_center(async_client, admin_headers, "Central", company_id)
            await async_client.put(
                f"/api/workers/{worker_id}/work-center",
                json={"company_id": company_id, "work_center_id": center_id},
                headers=admin_headers,
            )

            resp = await async_client.post(
                "/api/time-records/",
                json={"email": "wc.snapshot@test.com", "password": "WorkerPass123!", "company_id": company_id, "action": "entry"},
                headers=admin_headers,
            )
            assert resp.status_code == 201, resp.text
            data = resp.json()
            assert data["work_center_id"] == center_id
            assert data["work_center_name"] == "Central"

            stored = await test_db.TimeRecords.find_one({"_id": ObjectId(data["id"])})
            assert stored["work_center_id"] == center_id
            assert stored["work_center_name"] == "Central"
        finally:
            if worker_id:
                await test_db.WorkerShiftStates.delete_many({"worker_id": worker_id})
                await test_db.TimeRecords.delete_many({"worker_id": worker_id})
                await test_db.Workers.delete_one({"_id": ObjectId(worker_id)})
            if company_id:
                await test_db.WorkCenters.delete_many({"company_id": company_id})
                await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_clock_in_without_assignment_stores_null_snapshot(
        self, async_client, admin_headers, test_db
    ):
        company_id = None
        worker_id = None
        try:
            company_id = await _create_company(async_client, admin_headers, "WC NoSnapshot Company")
            worker_id = await _create_worker(
                async_client, admin_headers, "wc.nosnapshot@test.com", "20202020K", [company_id]
            )

            resp = await async_client.post(
                "/api/time-records/",
                json={"email": "wc.nosnapshot@test.com", "password": "WorkerPass123!", "company_id": company_id, "action": "entry"},
                headers=admin_headers,
            )
            assert resp.status_code == 201, resp.text
            data = resp.json()
            assert data["work_center_id"] is None
            assert data["work_center_name"] is None

            stored = await test_db.TimeRecords.find_one({"_id": ObjectId(data["id"])})
            assert stored.get("work_center_id") is None
            assert stored.get("work_center_name") is None
        finally:
            if worker_id:
                await test_db.WorkerShiftStates.delete_many({"worker_id": worker_id})
                await test_db.TimeRecords.delete_many({"worker_id": worker_id})
                await test_db.Workers.delete_one({"_id": ObjectId(worker_id)})
            if company_id:
                await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_snapshot_not_rewritten_when_center_changes(
        self, async_client, admin_headers, test_db
    ):
        company_id = None
        worker_id = None
        try:
            company_id = await _create_company(async_client, admin_headers, "WC Change Company")
            worker_id = await _create_worker(
                async_client, admin_headers, "wc.change@test.com", "20202020L", [company_id]
            )
            center_a = await _create_center(async_client, admin_headers, "Alfa", company_id)
            center_b = await _create_center(async_client, admin_headers, "Beta", company_id)
            await async_client.put(
                f"/api/workers/{worker_id}/work-center",
                json={"company_id": company_id, "work_center_id": center_a},
                headers=admin_headers,
            )

            resp = await async_client.post(
                "/api/time-records/",
                json={"email": "wc.change@test.com", "password": "WorkerPass123!", "company_id": company_id, "action": "entry"},
                headers=admin_headers,
            )
            assert resp.status_code == 201, resp.text
            first_record_id = resp.json()["id"]

            # Reassign to center B and clock out
            await async_client.put(
                f"/api/workers/{worker_id}/work-center",
                json={"company_id": company_id, "work_center_id": center_b},
                headers=admin_headers,
            )
            resp = await async_client.post(
                "/api/time-records/",
                json={"email": "wc.change@test.com", "password": "WorkerPass123!", "company_id": company_id, "action": "exit"},
                headers=admin_headers,
            )
            assert resp.status_code == 201, resp.text
            second_record_id = resp.json()["id"]

            first = await test_db.TimeRecords.find_one({"_id": ObjectId(first_record_id)})
            second = await test_db.TimeRecords.find_one({"_id": ObjectId(second_record_id)})
            assert first["work_center_id"] == center_a
            assert first["work_center_name"] == "Alfa"
            assert second["work_center_id"] == center_b
            assert second["work_center_name"] == "Beta"
        finally:
            if worker_id:
                await test_db.WorkerShiftStates.delete_many({"worker_id": worker_id})
                await test_db.TimeRecords.delete_many({"worker_id": worker_id})
                await test_db.Workers.delete_one({"_id": ObjectId(worker_id)})
            if company_id:
                await test_db.WorkCenters.delete_many({"company_id": company_id})
                await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_delete_center_unassigns_worker_and_preserves_records(
        self, async_client, admin_headers, test_db
    ):
        company_id = None
        worker_id = None
        try:
            company_id = await _create_company(async_client, admin_headers, "WC Delete Snapshot")
            worker_id = await _create_worker(
                async_client, admin_headers, "wc.delsnap@test.com", "20202020M", [company_id]
            )
            center_id = await _create_center(async_client, admin_headers, "Central", company_id)
            await async_client.put(
                f"/api/workers/{worker_id}/work-center",
                json={"company_id": company_id, "work_center_id": center_id},
                headers=admin_headers,
            )

            resp = await async_client.post(
                "/api/time-records/",
                json={"email": "wc.delsnap@test.com", "password": "WorkerPass123!", "company_id": company_id, "action": "entry"},
                headers=admin_headers,
            )
            assert resp.status_code == 201, resp.text
            record_id = resp.json()["id"]

            resp = await async_client.delete(f"/api/work-centers/{center_id}", headers=admin_headers)
            assert resp.status_code == 204, resp.text

            # Worker is unassigned
            stored_worker = await test_db.Workers.find_one({"_id": ObjectId(worker_id)})
            assert stored_worker.get("work_center_assignments", {}) == {}

            # Time record keeps its snapshot untouched
            stored_record = await test_db.TimeRecords.find_one({"_id": ObjectId(record_id)})
            assert stored_record["work_center_id"] == center_id
            assert stored_record["work_center_name"] == "Central"
        finally:
            if worker_id:
                await test_db.WorkerShiftStates.delete_many({"worker_id": worker_id})
                await test_db.TimeRecords.delete_many({"worker_id": worker_id})
                await test_db.Workers.delete_one({"_id": ObjectId(worker_id)})
            if company_id:
                await test_db.WorkCenters.delete_many({"company_id": company_id})
                await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_clock_in_snapshot_preserved_when_center_soft_deleted(
        self, async_client, admin_headers, test_db
    ):
        company_id = None
        worker_id = None
        try:
            company_id = await _create_company(async_client, admin_headers, "WC Snapshot Deleted")
            worker_id = await _create_worker(
                async_client, admin_headers, "wc.snapdel@test.com", "20202020O", [company_id]
            )
            center_id = await _create_center(async_client, admin_headers, "Central", company_id)
            # Assign directly on the worker doc to bypass the auto-unassign
            # that DELETE /work-centers would trigger.
            await test_db.Workers.update_one(
                {"_id": ObjectId(worker_id)},
                {"$set": {"work_center_assignments": {company_id: center_id}}},
            )
            # Soft-delete the center directly in the DB (same bypass).
            await test_db.WorkCenters.update_one(
                {"_id": ObjectId(center_id)},
                {"$set": {"deleted_at": datetime.now(dt_timezone.utc), "deleted_by": "admin_test"}},
            )

            resp = await async_client.post(
                "/api/time-records/",
                json={"email": "wc.snapdel@test.com", "password": "WorkerPass123!", "company_id": company_id, "action": "entry"},
                headers=admin_headers,
            )
            assert resp.status_code == 201, resp.text
            data = resp.json()
            assert data["work_center_id"] == center_id
            assert data["work_center_name"] == "Central"

            stored = await test_db.TimeRecords.find_one({"_id": ObjectId(data["id"])})
            assert stored["work_center_id"] == center_id
            assert stored["work_center_name"] == "Central"
        finally:
            if worker_id:
                await test_db.WorkerShiftStates.delete_many({"worker_id": worker_id})
                await test_db.TimeRecords.delete_many({"worker_id": worker_id})
                await test_db.Workers.delete_one({"_id": ObjectId(worker_id)})
            if company_id:
                await test_db.WorkCenters.delete_many({"company_id": company_id})
                await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})


class TestWorkCenterPermissions:

    @pytest.mark.asyncio
    async def test_tracker_denied_on_all_work_center_endpoints(
        self, async_client, admin_headers, test_db
    ):
        company_id = None
        tracker_headers = None
        try:
            company_id = await _create_company(async_client, admin_headers, "WC Perm Company")
            center_id = await _create_center(async_client, admin_headers, "Central", company_id)
            tracker_headers = await _create_role_headers(
                async_client, test_db, "tracker", TRACKER_PASSWORD
            )

            resp = await async_client.get("/api/work-centers/", headers=tracker_headers)
            assert resp.status_code == 403, resp.text
            resp = await async_client.get(f"/api/work-centers/{center_id}", headers=tracker_headers)
            assert resp.status_code == 403, resp.text
            resp = await async_client.post(
                "/api/work-centers/",
                json={"name": "X", "company_id": company_id},
                headers=tracker_headers,
            )
            assert resp.status_code == 403, resp.text
            resp = await async_client.put(
                f"/api/work-centers/{center_id}", json={"name": "Y"}, headers=tracker_headers
            )
            assert resp.status_code == 403, resp.text
            resp = await async_client.delete(f"/api/work-centers/{center_id}", headers=tracker_headers)
            assert resp.status_code == 403, resp.text
        finally:
            if company_id:
                await test_db.WorkCenters.delete_many({"company_id": company_id})
                await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
            await test_db.APIUsers.delete_one({"email": "tracker@test.com"})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_inspector_can_view_but_not_modify(
        self, async_client, admin_headers, test_db
    ):
        company_id = None
        inspector_headers = None
        try:
            company_id = await _create_company(async_client, admin_headers, "WC Insp Company")
            center_id = await _create_center(async_client, admin_headers, "Central", company_id)
            inspector_headers = await _create_role_headers(
                async_client, test_db, "inspector", INSPECTOR_PASSWORD
            )

            resp = await async_client.get("/api/work-centers/", headers=inspector_headers)
            assert resp.status_code == 200, resp.text
            resp = await async_client.get(f"/api/work-centers/{center_id}", headers=inspector_headers)
            assert resp.status_code == 200, resp.text

            resp = await async_client.post(
                "/api/work-centers/",
                json={"name": "X", "company_id": company_id},
                headers=inspector_headers,
            )
            assert resp.status_code == 403, resp.text
            resp = await async_client.put(
                f"/api/work-centers/{center_id}", json={"name": "Y"}, headers=inspector_headers
            )
            assert resp.status_code == 403, resp.text
            resp = await async_client.delete(f"/api/work-centers/{center_id}", headers=inspector_headers)
            assert resp.status_code == 403, resp.text
        finally:
            if company_id:
                await test_db.WorkCenters.delete_many({"company_id": company_id})
                await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
            await test_db.APIUsers.delete_one({"email": "inspector@test.com"})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_assignment_endpoint_requires_update_workers(
        self, async_client, admin_headers, test_db
    ):
        company_id = None
        worker_id = None
        tracker_headers = None
        try:
            company_id = await _create_company(async_client, admin_headers, "WC Assign Perm")
            worker_id = await _create_worker(
                async_client, admin_headers, "wc.assignperm@test.com", "20202020N", [company_id]
            )
            center_id = await _create_center(async_client, admin_headers, "Central", company_id)
            tracker_headers = await _create_role_headers(
                async_client, test_db, "tracker", TRACKER_PASSWORD
            )

            resp = await async_client.put(
                f"/api/workers/{worker_id}/work-center",
                json={"company_id": company_id, "work_center_id": center_id},
                headers=tracker_headers,
            )
            assert resp.status_code == 403, resp.text

            stored = await test_db.Workers.find_one({"_id": ObjectId(worker_id)})
            assert stored.get("work_center_assignments", {}) == {}
        finally:
            if worker_id:
                await test_db.Workers.delete_one({"_id": ObjectId(worker_id)})
            if company_id:
                await test_db.WorkCenters.delete_many({"company_id": company_id})
                await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
            await test_db.APIUsers.delete_one({"email": "tracker@test.com"})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})

    @pytest.mark.asyncio
    async def test_no_token_returns_401(self, async_client, admin_headers, test_db):
        company_id = None
        try:
            company_id = await _create_company(async_client, admin_headers, "WC Auth Company")
            resp = await async_client.get("/api/work-centers/")
            assert resp.status_code == 401, resp.text
        finally:
            if company_id:
                await test_db.Companies.delete_one({"_id": ObjectId(company_id)})
            await test_db.APIUsers.delete_one({"email": "admin@test.com"})