"""Unit tests for the shared SECRET_KEY fail-fast helper (api/utils/secrets.py).

Uses subprocess imports (rather than reloading api.utils.secrets in-process)
because SECRET_KEY is validated once at module import time, and the module
is already cached in sys.modules by the time this file runs as part of the
suite.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from api.utils.secrets import get_secret_key

API_ROOT = Path(__file__).resolve().parents[2]


class TestGetSecretKey:
    def test_raises_when_missing(self, monkeypatch):
        monkeypatch.delenv("SECRET_KEY", raising=False)
        with pytest.raises(RuntimeError):
            get_secret_key()

    def test_raises_when_empty(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "")
        with pytest.raises(RuntimeError):
            get_secret_key()

    def test_returns_value_when_set(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "a-strong-value")
        assert get_secret_key() == "a-strong-value"


class TestStartupFailsWithoutSecretKey:
    """Import must fail at process startup, not on first request."""

    def _run(self, tmp_path, secret_key: str | None) -> subprocess.CompletedProcess:
        # Run from an empty tmp directory (instead of the repo root) so
        # load_dotenv() inside api.utils.secrets can't pick up the repo's
        # own .env file and mask a missing/empty SECRET_KEY.
        env = {k: v for k, v in os.environ.items() if k != "SECRET_KEY"}
        env["PYTHONPATH"] = str(API_ROOT)
        if secret_key is not None:
            env["SECRET_KEY"] = secret_key
        return subprocess.run(
            [sys.executable, "-c", "import api.utils.secrets"],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def test_import_fails_when_secret_key_missing(self, tmp_path):
        result = self._run(tmp_path, secret_key=None)
        assert result.returncode != 0
        assert "SECRET_KEY" in result.stderr

    def test_import_fails_when_secret_key_empty(self, tmp_path):
        result = self._run(tmp_path, secret_key="")
        assert result.returncode != 0
        assert "SECRET_KEY" in result.stderr

    def test_import_succeeds_when_secret_key_set(self, tmp_path):
        result = self._run(tmp_path, secret_key="a-strong-value")
        assert result.returncode == 0, result.stderr


class TestNoHardcodedDefaultRemains:
    def test_no_default_secret_string_in_auth_or_encryption(self):
        import inspect

        from api.auth import auth_handler
        from api.utils import encryption

        for module in (auth_handler, encryption):
            source = inspect.getsource(module)
            assert "default_secret_key" not in source
            assert "default-secret-key-change-me" not in source
