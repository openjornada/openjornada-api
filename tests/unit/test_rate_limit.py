"""Unit tests for get_client_ip (api/utils/rate_limit.py).

Verifies that only the Caddy-controlled X-Real-IP header is trusted for the
login rate limit key, and that a forged X-Forwarded-For header is ignored
(regression test for the client-spoofable X-Forwarded-For[0] bug).
"""
from starlette.requests import Request

from api.utils.rate_limit import get_client_ip


def _make_request(headers: dict[str, str], client_host: str | None = "9.9.9.9") -> Request:
    """Build a minimal Starlette Request with the given headers/peer address."""
    encoded_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in headers.items()
    ]
    scope = {
        "type": "http",
        "headers": encoded_headers,
        "client": (client_host, 12345) if client_host else None,
        "method": "POST",
        "path": "/api/token",
    }
    return Request(scope)


class TestGetClientIp:
    def test_returns_x_real_ip_when_present(self):
        request = _make_request({"X-Real-IP": "1.2.3.4"})
        assert get_client_ip(request) == "1.2.3.4"

    def test_ignores_forged_x_forwarded_for_when_x_real_ip_present(self):
        request = _make_request(
            {"X-Forwarded-For": "6.6.6.6", "X-Real-IP": "1.2.3.4"}
        )
        assert get_client_ip(request) == "1.2.3.4"

    def test_falls_back_to_peer_address_without_x_real_ip(self):
        request = _make_request({}, client_host="9.9.9.9")
        assert get_client_ip(request) == "9.9.9.9"
