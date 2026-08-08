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
    always Caddy's address. Caddy overwrites `X-Real-IP` with the true peer
    address (`{http.request.remote.host}`) on every request, so that header
    cannot be forged by the client and is the only one we trust.

    `X-Forwarded-For` is deliberately NOT used: a client can send its own
    `X-Forwarded-For` and Caddy only appends the real IP after it, so
    `X-Forwarded-For.split(",")[0]` is attacker-controlled and would let a
    client bypass the per-IP login rate limit by rotating fake values.
    """
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    return get_remote_address(request)


limiter = Limiter(key_func=get_client_ip)
