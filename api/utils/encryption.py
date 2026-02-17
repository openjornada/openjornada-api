"""
Encryption utility for sensitive credentials (S3 keys, SFTP passwords, etc.)
Uses Fernet symmetric encryption derived from SECRET_KEY.
"""
import os
import base64
import hashlib
from cryptography.fernet import Fernet


class CredentialEncryption:
    """Handles encryption/decryption of sensitive credentials."""

    def __init__(self):
        secret_key = os.getenv("SECRET_KEY", "default-secret-key-change-me")
        # Derive a 32-byte key from SECRET_KEY using SHA256
        key_bytes = hashlib.sha256(secret_key.encode()).digest()
        # Fernet requires base64-encoded 32-byte key
        self._fernet = Fernet(base64.urlsafe_b64encode(key_bytes))

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a string and return base64-encoded ciphertext."""
        if not plaintext:
            return ""
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt base64-encoded ciphertext."""
        if not ciphertext:
            return ""
        return self._fernet.decrypt(ciphertext.encode()).decode()


# Singleton instance
credential_encryption = CredentialEncryption()
