"""
Unit tests for the error_code contract (tasks 5.1, 5.4) and the language
preference endpoints (tasks 2.1-2.4).

Uses standalone FastAPI apps with mocked DB / auth dependencies — no MongoDB
or real login required.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api.auth.auth_handler import get_current_active_user, get_current_user
from api.models.auth import APIUser
from api.utils.errors import api_error, raise_api_error


# ===========================================================================
# 5.1 — the helper itself
# ===========================================================================

class TestErrorHelper:
    def test_api_error_builds_detail_object(self):
        exc = api_error(404, "worker.not_found", "Worker not found")
        assert isinstance(exc, HTTPException)
        assert exc.status_code == 404
        assert exc.detail == {"error_code": "worker.not_found", "message": "Worker not found"}

    def test_api_error_keeps_headers(self):
        exc = api_error(401, "auth.invalid_credentials", "nope", headers={"WWW-Authenticate": "Bearer"})
        assert exc.headers == {"WWW-Authenticate": "Bearer"}

    def test_raise_api_error_raises(self):
        with pytest.raises(HTTPException) as exc_info:
            raise_api_error(400, "sms.invalid_locale", "bad")
        assert exc_info.value.detail["error_code"] == "sms.invalid_locale"


# ===========================================================================
# 5.4 — migrated auth errors (direct function calls)
# ===========================================================================

class TestMigratedAuthHandler:
    async def test_invalid_token_error_code(self):
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(token="not.a.jwt")
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["error_code"] == "auth.invalid_token"

    async def test_inactive_account_error_code(self):
        user = APIUser(username="u", email="u@example.com", is_active=False)
        with pytest.raises(HTTPException) as exc_info:
            from api.auth.auth_handler import get_current_active_user as gcau
            await gcau(current_user=user)
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["error_code"] == "auth.account_disabled"

    async def test_permission_checker_error_code(self):
        from api.auth.permissions import PermissionChecker
        checker = PermissionChecker("delete_workers")
        tracker = APIUser(username="t", email="t@example.com", role="tracker")
        with pytest.raises(HTTPException) as exc_info:
            await checker(current_user=tracker)
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail["error_code"] == "auth.insufficient_permissions"


# ===========================================================================
# 2.1 + 5.2 — PATCH /users/me (admin language preference)
# ===========================================================================

@pytest.fixture()
def auth_client():
    from api.routers import auth as auth_router

    app = FastAPI()
    app.include_router(auth_router.router, prefix="/api")
    app.dependency_overrides[get_current_active_user] = lambda: _admin_user

    fake_db = MagicMock()
    user_oid = ObjectId()
    user_doc = {
        "_id": user_oid, "username": "admin", "email": "admin@example.com",
        "role": "admin", "is_active": True, "hashed_password": "x",
    }

    async def _update_one(query, update):
        user_doc.update(update.get("$set", {}))

    fake_db.APIUsers.update_one = AsyncMock(side_effect=_update_one)
    fake_db.APIUsers.find_one = AsyncMock(side_effect=lambda q=None: dict(user_doc))

    with patch.object(auth_router, "db", fake_db):
        yield TestClient(app), user_doc


_admin_user = APIUser(username="admin", email="admin@example.com", role="admin")


class TestAdminLanguagePreference:
    def test_get_me_includes_language(self, auth_client):
        client, _ = auth_client
        resp = client.get("/api/users/me")
        assert resp.status_code == 200
        assert resp.json()["language"] is None

    def test_patch_sets_language(self, auth_client):
        client, user_doc = auth_client
        resp = client.patch("/api/users/me", json={"language": "en"})
        assert resp.status_code == 200
        assert resp.json()["language"] == "en"
        assert user_doc["language"] == "en"

    def test_patch_clears_language_with_null(self, auth_client):
        client, user_doc = auth_client
        client.patch("/api/users/me", json={"language": "ca"})
        resp = client.patch("/api/users/me", json={"language": None})
        assert resp.status_code == 200
        assert resp.json()["language"] is None

    def test_patch_invalid_locale_422_with_error_code(self, auth_client):
        client, user_doc = auth_client
        resp = client.patch("/api/users/me", json={"language": "fr"})
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["error_code"] == "settings.invalid_locale"
        assert "fr" in detail["message"]
        assert "language" not in user_doc  # not persisted


# ===========================================================================
# 5.2 — login error carries error_code (status unchanged)
# ===========================================================================

@pytest.fixture()
def token_client():
    from api.routers import auth as auth_router
    from api.utils.rate_limit import limiter

    limiter.enabled = False
    app = FastAPI()
    app.include_router(auth_router.router, prefix="/api")
    yield TestClient(app)
    limiter.enabled = True


class TestLoginErrorCode:
    def test_invalid_credentials_shape(self, token_client):
        with patch("api.routers.auth.authenticate_user", AsyncMock(return_value=False)):
            resp = token_client.post(
                "/api/token",
                data={"username": "a@b.com", "password": "wrong"},
            )
        assert resp.status_code == 401
        detail = resp.json()["detail"]
        assert detail["error_code"] == "auth.invalid_credentials"
        assert detail["message"] == "Incorrect email or password"

    async def test_duplicate_user_error_code(self, token_client):
        from api.models.auth import APIUserCreate
        from api.routers import auth as auth_router
        with patch.object(auth_router, "db") as fake_db:
            fake_db.APIUsers.find_one = AsyncMock(return_value={"_id": ObjectId()})
            with pytest.raises(HTTPException) as exc_info:
                await auth_router.create_user(
                    APIUserCreate(username="u", email="u@example.com", password="secret123"),
                    current_user=_admin_user,
                )
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["error_code"] == "auth.duplicate_user"

    def test_reset_password_invalid_token_error_code(self, token_client):
        with patch("api.routers.auth.db") as fake_db:
            fake_db.APIUsers.find_one = AsyncMock(return_value=None)
            resp = token_client.post("/api/reset-password", json={"token": "t", "new_password": "secret123"})
        assert resp.status_code == 400
        assert resp.json()["detail"]["error_code"] == "auth.invalid_reset_token"


# ===========================================================================
# 2.2 + 5.2 — PATCH /workers/language (worker self-service)
# ===========================================================================

def _worker_app_and_db(worker_doc):
    from api.routers import workers as workers_router

    app = FastAPI()
    app.include_router(workers_router.router, prefix="/api")

    fake_db = MagicMock()

    async def _update_one(query, update):
        worker_doc.update(update.get("$set", {}))

    fake_db.Workers.find_one = AsyncMock(side_effect=lambda q=None: dict(worker_doc))
    fake_db.Workers.update_one = AsyncMock(side_effect=_update_one)
    company_oid = ObjectId()
    fake_db.Companies.find_one = AsyncMock(return_value={
        "_id": company_oid, "name": "Acme", "notification_language": "ca",
        "deleted_at": None,
    })
    worker_doc["company_ids"] = [str(company_oid)]
    return app, fake_db, workers_router


class TestWorkerLanguageEndpoint:
    async def _call(self, payload, password_ok=True, worker=None):
        worker_doc = worker if worker is not None else {
            "_id": ObjectId(), "email": "w@example.com", "first_name": "W",
            "last_name": "K", "hashed_password": "irrelevant", "deleted_at": None,
        }
        app, fake_db, workers_router = _worker_app_and_db(worker_doc)
        client = TestClient(app)
        with patch.object(workers_router, "db", fake_db), \
             patch("api.utils.worker_auth.db", fake_db), \
             patch("api.utils.worker_auth.verify_password", return_value=password_ok), \
             patch("api.utils.company_locale.db", fake_db):
            resp = client.patch("/api/workers/language", json=payload)
        return resp, worker_doc

    async def test_set_language_persists_and_reports_effective(self):
        resp, doc = await self._call({"email": "w@example.com", "password": "p", "language": "en"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["language"] == "en"
        assert body["notification_language"] == "ca"       # company value (raw)
        assert body["effective_language"] == "en"          # own preference wins
        assert doc["language"] == "en"

    async def test_clear_language_inherits_company(self):
        resp, _ = await self._call({"email": "w@example.com", "password": "p", "language": "en"})
        assert resp.status_code == 200
        resp2, _ = await self._call({"email": "w@example.com", "password": "p", "language": None})
        assert resp2.status_code == 200
        body = resp2.json()
        assert body["language"] is None
        assert body["effective_language"] == "ca"  # inherited from company

    async def test_invalid_locale_422_error_code(self):
        resp, doc = await self._call({"email": "w@example.com", "password": "p", "language": "de"})
        assert resp.status_code == 422
        assert resp.json()["detail"]["error_code"] == "worker.invalid_locale"
        assert "language" not in doc

    async def test_bad_password_401_error_code(self):
        resp, _ = await self._call(
            {"email": "w@example.com", "password": "bad", "language": "en"},
            password_ok=False,
        )
        assert resp.status_code == 401
        assert resp.json()["detail"]["error_code"] == "worker.invalid_credentials"


# ===========================================================================
# 2.4 — profile payloads expose language fields
# ===========================================================================

class TestWorkerMePayload:
    async def test_me_includes_language_and_company_notification_language(self):
        worker_doc = {
            "_id": ObjectId(), "email": "w@example.com", "first_name": "W", "last_name": "K",
            "phone_number": "+34600000001", "default_timezone": "UTC",
            "hashed_password": "irrelevant", "language": "en", "deleted_at": None,
        }
        app, fake_db, _ = _worker_app_and_db(worker_doc)
        company_oid = fake_db.Companies.find_one.return_value["_id"]
        worker_doc["company_ids"] = [str(company_oid)]

        companies_cursor = MagicMock()

        async def _aiter():
            yield {"_id": company_oid, "name": "Acme", "notification_language": "ca"}

        companies_cursor.__aiter__ = lambda self: _aiter()
        fake_db.Companies.find = MagicMock(return_value=companies_cursor)

        client = TestClient(app)
        with patch("api.routers.workers.db", fake_db), \
             patch("api.utils.worker_auth.db", fake_db), \
             patch("api.utils.worker_auth.verify_password", return_value=True):
            resp = client.post("/api/workers/me", json={"email": "w@example.com", "password": "p"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["language"] == "en"
        assert body["notification_language"] == "ca"
        assert body["company_names"] == ["Acme"]


# ===========================================================================
# 2.3 — Company.notification_language through the companies router
# ===========================================================================

class TestCompanyLanguageEndpoint:
    @pytest.fixture()
    def companies_client(self):
        from api.routers import companies as companies_router

        app = FastAPI()
        app.include_router(companies_router.router, prefix="/api")
        app.dependency_overrides[get_current_active_user] = lambda: _admin_user

        company_oid = ObjectId()
        company_doc = {
            "_id": company_oid, "name": "Acme", "created_at": datetime.now(timezone.utc),
            "updated_at": None, "deleted_at": None, "deleted_by": None,
            "absence_management_enabled": False,
        }
        fake_db = MagicMock()

        async def _update_one(query, update):
            company_doc.update(update.get("$set", {}))

        fake_db.Companies.find_one = AsyncMock(side_effect=lambda q=None: dict(company_doc))
        fake_db.Companies.update_one = AsyncMock(side_effect=_update_one)
        with patch.object(companies_router, "db", fake_db):
            yield TestClient(app), company_doc

    def test_response_exposes_default_es(self, companies_client):
        client, _ = companies_client
        resp = client.get(f"/api/companies/{ObjectId()}")
        # find_one mock returns the doc regardless of id
        assert resp.status_code == 200
        assert resp.json()["notification_language"] == "es"

    def test_patch_updates_notification_language(self, companies_client):
        client, doc = companies_client
        resp = client.patch(f"/api/companies/{doc['_id']}", json={"notification_language": "en"})
        assert resp.status_code == 200
        assert resp.json()["notification_language"] == "en"
        assert doc["notification_language"] == "en"

    def test_patch_rejects_invalid_locale_422(self, companies_client):
        client, doc = companies_client
        resp = client.patch(f"/api/companies/{doc['_id']}", json={"notification_language": "xx"})
        assert resp.status_code == 422
        assert doc.get("notification_language") != "xx"


# ===========================================================================
# 5.4 — control: non-migrated endpoints keep the plain-string detail
# ===========================================================================

class TestUnmigratedEndpointsUnchanged:
    def test_sms_history_clear_requires_plain_detail(self):
        """DELETE /sms/history without confirm is NOT migrated: plain string."""
        from api.routers import sms as sms_router

        app = FastAPI()
        app.include_router(sms_router.router, prefix="/api")
        app.dependency_overrides[get_current_active_user] = lambda: _admin_user
        client = TestClient(app)
        resp = client.delete("/api/sms/history")
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert isinstance(detail, str)
        assert "confirm" in detail

    def test_pydantic_validation_errors_stay_standard(self):
        """Low-level schema validation (e.g. bad role) keeps FastAPI's format."""
        from api.models.auth import APIUserCreate
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            APIUserCreate(username="u", email="u@e.com", password="p", role="superuser")
