"""Shared slowapi Limiter for the login endpoint.

Kept in its own module (instead of `api/main.py`) so `api/routers/auth.py`
can import the same `Limiter` instance to decorate `POST /api/token`
without a circular import with `api/main.py`.
"""
import os

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

LOGIN_RATE_LIMIT = os.getenv("LOGIN_RATE_LIMIT", "5/minute")


def get_client_ip(request: Request) -> str:
    """Return the client's real IP address.

    The API runs behind a Caddy reverse proxy, so `request.client.host` is
    always Caddy's address. Honor the `X-Forwarded-For`/`X-Real-IP`
    headers Caddy sets instead, falling back to the direct peer address.
    """
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    return get_remote_address(request)


limiter = Limiter(key_func=get_client_ip)
