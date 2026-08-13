"""
Configuración global de fixtures para tests de integración.

IMPORTANTE: Estos tests se ejecutan contra una BD real (test database).
Los datos se crean y eliminan en cada sesión de tests.
"""
import asyncio
import os
import pytest
from typing import AsyncGenerator, Dict
from httpx import AsyncClient, ASGITransport
from motor.motor_asyncio import AsyncIOMotorClient

# Configurar variables de entorno ANTES de importar la app
# En Docker, MongoDB está en 'mongodb', no en 'localhost'
MONGO_URL = os.getenv("TEST_MONGO_URL", os.getenv("MONGO_URL", "mongodb://mongodb:27017"))
DB_NAME = os.getenv("TEST_DB_NAME", "time_tracking_test_db")

os.environ["MONGO_URL"] = MONGO_URL
os.environ["DB_NAME"] = DB_NAME
os.environ["SECRET_KEY"] = os.getenv("SECRET_KEY", "test_secret_key_for_testing_only")

# The app binds a single module-global Motor client (api.database.client) the
# moment `api.database` is imported, and that client eagerly captures
# `asyncio.get_event_loop()` at construction time — not lazily on first query.
# We therefore create *our* test event loop and install it as the process
# default *before* importing the app, so the global Motor client binds to it
# from the start. This loop is then reused (not recreated) as the
# pytest-asyncio `event_loop` fixture below: pytest-asyncio's fixture-setup
# hook closes whatever loop was previously the process default whenever it
# detects a *different* object being installed as the new one, which would
# otherwise immediately invalidate the global Motor client before any test
# runs. Reusing the same loop object makes that a no-op.
_session_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_session_loop)

from api.main import app
from api.utils.rate_limit import limiter

# Login rate limiting is disabled by default for the suite: many fixtures
# (e.g. admin_token) call POST /api/token once per test, which would trip
# the real per-minute limit across an entire session. The dedicated
# rate-limit tests re-enable it for the duration of a single test.
limiter.enabled = False


@pytest.fixture(scope="session")
def event_loop():
    """
    Session-scoped event loop, reusing the loop already installed as the
    process default above (see comment there for why it must be the same
    object rather than a freshly created one).
    """
    yield _session_loop
    _session_loop.close()


@pytest.fixture(scope="function")
async def test_db():
    """
    Fixture que proporciona acceso a la BD de test.
    Crea un nuevo cliente MongoDB para cada test para evitar problemas de event loop.
    """
    client = AsyncIOMotorClient(MONGO_URL)
    db = client[DB_NAME]
    yield db
    client.close()


@pytest.fixture(scope="function")
async def async_client() -> AsyncGenerator[AsyncClient, None]:
    """
    Cliente HTTP asíncrono para hacer requests a la API.
    Usa ASGITransport para testing sin levantar servidor.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture(scope="function")
async def admin_token(async_client: AsyncClient, test_db) -> str:
    """
    Obtiene un token JWT de administrador para los tests.
    Crea un usuario admin temporal si no existe.
    """
    from api.auth.auth_handler import get_password_hash
    from datetime import datetime, timezone

    admin_email = "admin@test.com"
    admin_password = "TestAdmin123!"

    # Crear admin user para tests
    admin_user = {
        "username": "admin_test",
        "email": admin_email,
        "hashed_password": get_password_hash(admin_password),
        "role": "admin",
        "is_active": True,
        "created_at": datetime.now(timezone.utc)
    }

    # Eliminar si existe y crear nuevo
    await test_db.APIUsers.delete_one({"email": admin_email})
    await test_db.APIUsers.insert_one(admin_user)

    # Hacer login
    login_data = {
        "username": admin_email,
        "password": admin_password
    }

    response = await async_client.post(
        "/api/token",
        data=login_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )

    assert response.status_code == 200, f"Failed to get admin token: {response.text}"
    return response.json()["access_token"]


@pytest.fixture(scope="function")
def admin_headers(admin_token: str) -> Dict[str, str]:
    """Headers con autorización de admin."""
    return {"Authorization": f"Bearer {admin_token}"}
