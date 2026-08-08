"""Single source of truth for the app's SECRET_KEY.

`api.auth.auth_handler` (JWT signing) and `api.utils.encryption` (Fernet
key derivation) both import `SECRET_KEY` from here so they always share
the exact same value. Reading it happens at import time, so a missing or
empty `SECRET_KEY` aborts startup instead of failing on the first request.
"""
import os

from dotenv import load_dotenv

load_dotenv()


def get_secret_key() -> str:
    """Return the SECRET_KEY from the environment.

    Raises:
        RuntimeError: if SECRET_KEY is unset or empty.
    """
    secret_key = os.getenv("SECRET_KEY")
    if not secret_key:
        raise RuntimeError(
            "SECRET_KEY environment variable is required and must not be "
            "empty. Set it before starting the API (e.g. in .env)."
        )
    return secret_key


SECRET_KEY = get_secret_key()
