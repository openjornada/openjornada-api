"""
Tests de integración para POST /api/workers/bulk-import (importación masiva).

Cubren: dry-run mixto, import parcial real, duplicados en BD e intra-lote,
resolución de empresas por nombre (inexistente / ambigua / varias por fila),
lote vacío, email de bienvenida on/off/dry-run, autorización y límite de lote.

Se ejecutan contra la BD de tests real (ver tests/conftest.py). Cada test usa
emails/id_numbers/nombres de empresa únicos y limpia lo que inserta.
"""
import uuid
from datetime import datetime, timezone
from typing import Dict, List

import pytest
from httpx import AsyncClient

from api.models.workers import MAX_BULK_IMPORT_ROWS

BULK_URL = "/api/workers/bulk-import"


def _uid() -> str:
    return uuid.uuid4().hex[:10]


def _row(company_names: List[str], **overrides) -> dict:
    row = {
        "first_name": "Ana",
        "last_name": "García",
        "email": f"ana.{_uid()}@example.com",
        "phone_number": "+34600112233",
        "id_number": f"Z{_uid()}",
        "company_names": company_names,
        "default_timezone": "Europe/Madrid",
    }
    row.update(overrides)
    return row


async def _insert_company(test_db, name: str) -> str:
    result = await test_db.Companies.insert_one({
        "name": name,
        "created_at": datetime.now(timezone.utc),
        "deleted_at": None,
    })
    return str(result.inserted_id)


@pytest.fixture
async def cleanup(test_db):
    """Registra emails de workers y nombres de empresa creados y los borra."""
    created: Dict[str, list] = {"emails": [], "company_names": []}
    yield created
    if created["emails"]:
        await test_db.Workers.delete_many({"email": {"$in": created["emails"]}})
    if created["company_names"]:
        await test_db.Companies.delete_many({"name": {"$in": created["company_names"]}})


@pytest.fixture
def welcome_email_spy(monkeypatch) -> List[str]:
    """Sustituye email_service.send_welcome_email y registra los emails destino."""
    sent: List[str] = []

    async def fake_send(to_email, worker_name, reset_token, webapp_url, contact_email, locale="es"):
        sent.append(to_email)
        return True

    monkeypatch.setattr(
        "api.routers.workers.email_service.send_welcome_email", fake_send
    )
    return sent


class TestWorkerBulkImport:

    @pytest.mark.asyncio
    async def test_dry_run_mixed_batch_no_writes(
        self, async_client: AsyncClient, admin_headers, test_db, cleanup, welcome_email_spy
    ):
        company = f"DryCo {_uid()}"
        cleanup["company_names"].append(company)
        await _insert_company(test_db, company)

        valid = _row([company])
        invalid_email = _row([company], email="no-es-un-email")
        missing_company = _row([f"Inexistente {_uid()}"])
        cleanup["emails"].append(valid["email"])

        response = await async_client.post(
            BULK_URL,
            json={
                "rows": [valid, invalid_email, missing_company],
                "dry_run": True,
                "send_welcome_email": True,
            },
            headers=admin_headers,
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == 3
        assert body["created"] == 1
        assert body["skipped"] == 0
        assert body["errors"] == 2
        assert body["results"][0]["status"] == "created"
        assert body["results"][0]["row_index"] == 0
        assert body["results"][1]["status"] == "error"
        assert "Email inválido" in body["results"][1]["detail"]
        assert body["results"][1]["email"] is None
        assert body["results"][2]["status"] == "error"
        assert "Empresa no encontrada" in body["results"][2]["detail"]

        assert await test_db.Workers.find_one({"email": valid["email"]}) is None
        assert welcome_email_spy == []

    @pytest.mark.asyncio
    async def test_real_partial_import(
        self, async_client: AsyncClient, admin_headers, test_db, cleanup
    ):
        company = f"RealCo {_uid()}"
        cleanup["company_names"].append(company)
        company_id = await _insert_company(test_db, company)

        valid = _row([company])
        missing_fields = _row([company], first_name="   ")
        missing_company = _row([f"Inexistente {_uid()}"])
        cleanup["emails"] += [valid["email"], missing_fields["email"]]

        response = await async_client.post(
            BULK_URL,
            json={"rows": [valid, missing_fields, missing_company], "dry_run": False},
            headers=admin_headers,
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == 3
        assert body["created"] == 1
        assert body["skipped"] == 0
        assert body["errors"] == 2
        assert body["results"][1]["status"] == "error"
        assert "first_name" in body["results"][1]["detail"]
        assert body["results"][1]["email"] == missing_fields["email"]

        worker = await test_db.Workers.find_one({"email": valid["email"]})
        assert worker is not None
        assert worker["company_ids"] == [company_id]
        assert worker["created_by"] == "admin_test"
        assert worker["deleted_at"] is None
        assert worker["hashed_password"].startswith("$argon2")
        assert await test_db.Workers.find_one({"email": missing_fields["email"]}) is None

    @pytest.mark.asyncio
    async def test_invalid_email_domain_is_row_error(
        self, async_client: AsyncClient, admin_headers, test_db, cleanup
    ):
        company = f"BadDomainCo {_uid()}"
        cleanup["company_names"].append(company)
        await _insert_company(test_db, company)

        row = _row([company], email="ana@x..y.com")

        response = await async_client.post(
            BULK_URL, json={"rows": [row], "dry_run": False}, headers=admin_headers
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["created"] == 0
        assert body["errors"] == 1
        assert body["results"][0]["status"] == "error"
        assert "Email inválido" in body["results"][0]["detail"]
        assert body["results"][0]["email"] is None
        assert await test_db.Workers.find_one({"email": row["email"]}) is None

    @pytest.mark.asyncio
    async def test_invalid_timezone_is_row_error(
        self, async_client: AsyncClient, admin_headers, test_db, cleanup
    ):
        company = f"BadTzCo {_uid()}"
        cleanup["company_names"].append(company)
        await _insert_company(test_db, company)

        invalid_tz = _row([company], default_timezone="Mars/Phobos")
        valid = _row([company])
        cleanup["emails"] += [invalid_tz["email"], valid["email"]]

        response = await async_client.post(
            BULK_URL,
            json={"rows": [invalid_tz, valid], "dry_run": False},
            headers=admin_headers,
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["created"] == 1
        assert body["errors"] == 1
        assert body["results"][0]["status"] == "error"
        assert body["results"][0]["detail"] == "Zona horaria inválida: Mars/Phobos"
        assert body["results"][1]["status"] == "created"
        assert await test_db.Workers.find_one({"email": invalid_tz["email"]}) is None
        assert await test_db.Workers.find_one({"email": valid["email"]}) is not None

    @pytest.mark.asyncio
    async def test_phone_number_optional(
        self, async_client: AsyncClient, admin_headers, test_db, cleanup
    ):
        company = f"PhoneCo {_uid()}"
        cleanup["company_names"].append(company)
        await _insert_company(test_db, company)

        empty_phone = _row([company], phone_number="")
        missing_phone = _row([company])
        del missing_phone["phone_number"]
        cleanup["emails"] += [empty_phone["email"], missing_phone["email"]]

        response = await async_client.post(
            BULK_URL,
            json={"rows": [empty_phone, missing_phone], "dry_run": False},
            headers=admin_headers,
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["created"] == 2
        assert body["results"][0]["status"] == "created"
        assert body["results"][1]["status"] == "created"

        worker = await test_db.Workers.find_one({"email": missing_phone["email"]})
        assert worker["phone_number"] == ""

    @pytest.mark.asyncio
    async def test_duplicate_in_db_skipped(
        self, async_client: AsyncClient, admin_headers, test_db, cleanup
    ):
        company = f"DupCo {_uid()}"
        cleanup["company_names"].append(company)
        await _insert_company(test_db, company)

        existing = {
            "first_name": "Ya",
            "last_name": "Estaba",
            "email": f"existing.{_uid()}@example.com",
            "id_number": f"Y{_uid()}",
            "phone_number": "+34600000000",
            "hashed_password": "$argon2$placeholder",
            "company_ids": [],
            "created_at": datetime.now(timezone.utc),
            "deleted_at": None,
        }
        insert = await test_db.Workers.insert_one(existing)
        cleanup["emails"].append(existing["email"])

        row_dup_email = _row([company], email=existing["email"])
        row_dup_idn = _row([company], id_number=existing["id_number"])
        cleanup["emails"] += [row_dup_email["email"], row_dup_idn["email"]]

        response = await async_client.post(
            BULK_URL,
            json={"rows": [row_dup_email, row_dup_idn], "dry_run": False},
            headers=admin_headers,
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["created"] == 0
        assert body["skipped"] == 2
        assert body["results"][0] == {
            "row_index": 0,
            "status": "skipped_duplicate",
            "detail": "Email ya registrado",
            "email": existing["email"],
        }
        assert body["results"][1]["detail"] == "DNI ya registrado"

        stored = await test_db.Workers.find_one({"_id": insert.inserted_id})
        assert stored["first_name"] == existing["first_name"]
        assert "updated_at" not in stored
        assert await test_db.Workers.find_one({"email": row_dup_idn["email"]}) is None

    @pytest.mark.asyncio
    async def test_duplicate_email_case_insensitive(
        self, async_client: AsyncClient, admin_headers, test_db, cleanup
    ):
        company = f"CaseCo {_uid()}"
        cleanup["company_names"].append(company)
        await _insert_company(test_db, company)

        existing_email = f"case.{_uid()}@example.com"
        existing = {
            "first_name": "Ya",
            "last_name": "Estaba",
            "email": existing_email,
            "id_number": f"C{_uid()}",
            "phone_number": "+34600000000",
            "hashed_password": "$argon2$placeholder",
            "company_ids": [],
            "created_at": datetime.now(timezone.utc),
            "deleted_at": None,
        }
        await test_db.Workers.insert_one(existing)
        cleanup["emails"].append(existing_email)

        row = _row([company], email=existing_email.upper())

        response = await async_client.post(
            BULK_URL, json={"rows": [row], "dry_run": False}, headers=admin_headers
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["skipped"] == 1
        assert body["results"][0]["status"] == "skipped_duplicate"
        assert body["results"][0]["detail"] == "Email ya registrado"

    @pytest.mark.asyncio
    async def test_intra_lot_duplicate(
        self, async_client: AsyncClient, admin_headers, test_db, cleanup
    ):
        company = f"LotCo {_uid()}"
        cleanup["company_names"].append(company)
        await _insert_company(test_db, company)

        first = _row([company])
        second = _row([company], email=first["email"], id_number=f"Q{_uid()}")
        cleanup["emails"].append(first["email"])

        # En seco también debe detectar el duplicado intra-lote
        dry = await async_client.post(
            BULK_URL, json={"rows": [first, second], "dry_run": True}, headers=admin_headers
        )
        assert dry.status_code == 200
        dry_body = dry.json()
        assert dry_body["created"] == 1
        assert dry_body["skipped"] == 1
        assert dry_body["results"][1]["status"] == "skipped_duplicate"
        assert "lote" in dry_body["results"][1]["detail"]

        response = await async_client.post(
            BULK_URL, json={"rows": [first, second], "dry_run": False}, headers=admin_headers
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["results"][0]["status"] == "created"
        assert body["results"][1]["status"] == "skipped_duplicate"
        assert body["created"] == 1
        assert body["skipped"] == 1

        count = await test_db.Workers.count_documents({"email": first["email"]})
        assert count == 1

    @pytest.mark.asyncio
    async def test_company_not_found_does_not_create_it(
        self, async_client: AsyncClient, admin_headers, test_db
    ):
        ghost = f"GhostCo {_uid()}"
        response = await async_client.post(
            BULK_URL,
            json={"rows": [_row([ghost])], "dry_run": False},
            headers=admin_headers,
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["errors"] == 1
        assert body["results"][0]["status"] == "error"
        assert f"Empresa no encontrada: {ghost}" == body["results"][0]["detail"]
        assert await test_db.Companies.find_one({"name": ghost}) is None

    @pytest.mark.asyncio
    async def test_empty_company_names_is_row_error(
        self, async_client: AsyncClient, admin_headers, test_db, cleanup
    ):
        company = f"ReqCo {_uid()}"
        cleanup["company_names"].append(company)
        await _insert_company(test_db, company)

        valid = _row([company])
        no_company = _row([])
        cleanup["emails"] += [valid["email"], no_company["email"]]

        response = await async_client.post(
            BULK_URL,
            json={"rows": [valid, no_company], "dry_run": False},
            headers=admin_headers,
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["created"] == 1
        assert body["errors"] == 1
        assert body["results"][1]["status"] == "error"
        assert body["results"][1]["detail"] == "Debe indicar al menos una empresa"

        assert await test_db.Workers.find_one({"email": valid["email"]}) is not None
        assert await test_db.Workers.find_one({"email": no_company["email"]}) is None

    @pytest.mark.asyncio
    async def test_ambiguous_company_name(
        self, async_client: AsyncClient, admin_headers, test_db, cleanup
    ):
        base = f"AmbiCo {_uid()}"
        cleanup["company_names"] += [base, base.lower()]
        await _insert_company(test_db, base)
        await _insert_company(test_db, base.lower())

        response = await async_client.post(
            BULK_URL,
            json={"rows": [_row([base.upper()])], "dry_run": False},
            headers=admin_headers,
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["errors"] == 1
        assert body["results"][0]["detail"] == f"Empresa ambigua: {base.upper()}"

    @pytest.mark.asyncio
    async def test_multiple_companies_per_row(
        self, async_client: AsyncClient, admin_headers, test_db, cleanup
    ):
        name_a = f"Multi A {_uid()}"
        name_b = f"multi b {_uid()}"
        cleanup["company_names"] += [name_a, name_b]
        id_a = await _insert_company(test_db, name_a)
        id_b = await _insert_company(test_db, name_b)

        row = _row([name_a.upper(), f"  {name_b}  "])
        cleanup["emails"].append(row["email"])

        response = await async_client.post(
            BULK_URL, json={"rows": [row], "dry_run": False}, headers=admin_headers
        )

        assert response.status_code == 200, response.text
        assert response.json()["created"] == 1

        worker = await test_db.Workers.find_one({"email": row["email"]})
        assert worker is not None
        assert set(worker["company_ids"]) == {id_a, id_b}

    @pytest.mark.asyncio
    async def test_empty_lot(self, async_client: AsyncClient, admin_headers):
        response = await async_client.post(
            BULK_URL, json={"rows": [], "dry_run": False}, headers=admin_headers
        )

        assert response.status_code == 200, response.text
        assert response.json() == {
            "total": 0, "created": 0, "skipped": 0, "errors": 0, "results": []
        }

    @pytest.mark.asyncio
    async def test_welcome_email_only_for_created(
        self, async_client: AsyncClient, admin_headers, test_db, cleanup, welcome_email_spy
    ):
        company = f"MailCo {_uid()}"
        cleanup["company_names"].append(company)
        await _insert_company(test_db, company)

        created_row = _row([company])
        invalid_row = _row([company], email="not-an-email")
        error_row = _row([f"Ausente {_uid()}"])
        cleanup["emails"].append(created_row["email"])

        response = await async_client.post(
            BULK_URL,
            json={
                "rows": [created_row, invalid_row, error_row],
                "dry_run": False,
                "send_welcome_email": True,
            },
            headers=admin_headers,
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["created"] == 1
        assert body["errors"] == 2
        assert welcome_email_spy == [created_row["email"]]

        worker = await test_db.Workers.find_one({"email": created_row["email"]})
        assert worker.get("reset_token")
        assert worker.get("reset_token_expires")

    @pytest.mark.asyncio
    async def test_welcome_email_disabled_by_default(
        self, async_client: AsyncClient, admin_headers, test_db, cleanup, welcome_email_spy
    ):
        company = f"NoMailCo {_uid()}"
        cleanup["company_names"].append(company)
        await _insert_company(test_db, company)

        row = _row([company])
        cleanup["emails"].append(row["email"])

        response = await async_client.post(
            BULK_URL, json={"rows": [row], "dry_run": False}, headers=admin_headers
        )

        assert response.status_code == 200, response.text
        assert response.json()["created"] == 1
        assert welcome_email_spy == []

    @pytest.mark.asyncio
    async def test_requires_create_workers_permission(
        self, async_client: AsyncClient, test_db, cleanup
    ):
        from api.auth.auth_handler import get_password_hash

        company = f"PermCo {_uid()}"
        cleanup["company_names"].append(company)
        await _insert_company(test_db, company)
        row = _row([company])
        cleanup["emails"].append(row["email"])
        payload = {"rows": [row], "dry_run": False}

        # Sin token -> 401
        response = await async_client.post(BULK_URL, json=payload)
        assert response.status_code == 401

        # Rol tracker (sin create_workers) -> 403
        tracker_email = f"tracker.{_uid()}@test.com"
        tracker_password = "Tracker123!"
        await test_db.APIUsers.insert_one({
            "username": "tracker_test",
            "email": tracker_email,
            "hashed_password": get_password_hash(tracker_password),
            "role": "tracker",
            "is_active": True,
            "created_at": datetime.now(timezone.utc),
        })
        login = await async_client.post(
            "/api/token",
            data={"username": tracker_email, "password": tracker_password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert login.status_code == 200, login.text
        tracker_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        response = await async_client.post(BULK_URL, json=payload, headers=tracker_headers)
        assert response.status_code == 403
        assert await test_db.Workers.find_one({"email": row["email"]}) is None

        await test_db.APIUsers.delete_one({"email": tracker_email})

    @pytest.mark.asyncio
    async def test_batch_over_max_rows_rejected(
        self, async_client: AsyncClient, admin_headers
    ):
        rows = [_row([f"Ignorada {_uid()}"]) for _ in range(MAX_BULK_IMPORT_ROWS + 1)]
        response = await async_client.post(
            BULK_URL, json={"rows": rows, "dry_run": True}, headers=admin_headers
        )
        assert response.status_code == 422
        assert "max_length" in response.text or "at most" in response.text
