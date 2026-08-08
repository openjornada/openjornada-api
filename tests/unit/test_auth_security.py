"""
Unit tests for the security-critical dependency migration
(cryptography, python-jose -> PyJWT, passlib -> argon2-cffi).

All tests are pure unit tests -- no MongoDB or HTTP server required.
"""
from datetime import timedelta

import bcrypt
import jwt
import pytest
from fastapi import HTTPException

from api.auth.auth_handler import (
    ALGORITHM,
    SECRET_KEY,
    create_access_token,
    get_current_user,
    get_password_hash,
    verify_password,
)
from api.utils.encryption import CredentialEncryption


class TestPasswordHashing:
    """verify_password/get_password_hash must keep legacy bcrypt hashes
    working while hashing new passwords with argon2."""

    def test_legacy_bcrypt_hash_still_verifies(self):
        # Simulates a hash stored in production before the argon2 migration
        # (passlib's bcrypt scheme also produces standard $2b$ hashes).
        legacy_hash = bcrypt.hashpw(b"CorrectHorse123", bcrypt.gensalt()).decode()
        assert verify_password("CorrectHorse123", legacy_hash) is True

    def test_legacy_bcrypt_hash_rejects_wrong_password(self):
        legacy_hash = bcrypt.hashpw(b"CorrectHorse123", bcrypt.gensalt()).decode()
        assert verify_password("WrongPassword", legacy_hash) is False

    def test_new_password_hashes_with_argon2(self):
        hashed = get_password_hash("SomeNewPassword1")
        assert hashed.startswith("$argon2")

    def test_new_argon2_hash_verifies(self):
        hashed = get_password_hash("SomeNewPassword1")
        assert verify_password("SomeNewPassword1", hashed) is True

    def test_new_argon2_hash_rejects_wrong_password(self):
        hashed = get_password_hash("SomeNewPassword1")
        assert verify_password("NotThePassword", hashed) is False


class TestJWT:
    """PyJWT-based encode/decode and error mapping."""

    def test_create_and_decode_token(self):
        token = create_access_token({"sub": "alice"})
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        assert payload["sub"] == "alice"

    def test_expired_token_raises_expired_signature_error(self):
        token = create_access_token({"sub": "alice"}, expires_delta=timedelta(seconds=-1))
        with pytest.raises(jwt.ExpiredSignatureError):
            jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])

    def test_invalid_token_raises_invalid_token_error(self):
        with pytest.raises(jwt.InvalidTokenError):
            jwt.decode("not-a-valid-token", SECRET_KEY, algorithms=[ALGORITHM])

    def test_wrong_secret_raises_pyjwt_error(self):
        token = create_access_token({"sub": "alice"})
        with pytest.raises(jwt.PyJWTError):
            jwt.decode(token, "wrong-secret", algorithms=[ALGORITHM])

    def test_interop_with_legacy_python_jose_token(self):
        """A token issued by the previous python-jose implementation (same
        SECRET_KEY + HS256) must still validate under PyJWT, proving
        existing production JWTs remain valid after this migration."""
        pytest.importorskip("jose")
        from jose import jwt as jose_jwt

        legacy_token = jose_jwt.encode({"sub": "alice"}, SECRET_KEY, algorithm=ALGORITHM)
        payload = jwt.decode(legacy_token, SECRET_KEY, algorithms=[ALGORITHM])
        assert payload["sub"] == "alice"

    async def test_get_current_user_rejects_invalid_token(self):
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(token="not-a-valid-token")
        assert exc_info.value.status_code == 401

    async def test_get_current_user_rejects_expired_token(self):
        token = create_access_token({"sub": "alice"}, expires_delta=timedelta(seconds=-1))
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(token=token)
        assert exc_info.value.status_code == 401


class TestFernetEncryption:
    """cryptography.fernet round-trip after the cryptography version bump."""

    def test_round_trip(self):
        enc = CredentialEncryption()
        ciphertext = enc.encrypt("super-secret-value")
        assert ciphertext != "super-secret-value"
        assert enc.decrypt(ciphertext) == "super-secret-value"

    def test_empty_string_round_trip(self):
        enc = CredentialEncryption()
        assert enc.encrypt("") == ""
        assert enc.decrypt("") == ""
