"""
SFTP storage backend using paramiko.
"""
import asyncio
import logging
import os
from pathlib import Path
from typing import Optional, Tuple

import paramiko

from .base import StorageBackend
from ...utils.encryption import credential_encryption

logger = logging.getLogger(__name__)


class SFTPStorage(StorageBackend):
    """SFTP storage backend using paramiko."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password_encrypted: str,
        remote_path: str = "/backups/openjornada/"
    ):
        self.host = host
        self.port = port
        self.username = username
        self.remote_path = remote_path.rstrip('/') + '/'
        self._password = credential_encryption.decrypt(password_encrypted)

    def _get_connection(self) -> Tuple[paramiko.SSHClient, paramiko.SFTPClient]:
        """Create SSH and SFTP connection."""
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            hostname=self.host,
            port=self.port,
            username=self.username,
            password=self._password,
            timeout=30
        )
        sftp = ssh.open_sftp()
        return ssh, sftp

    def _ensure_remote_dir(self, sftp: paramiko.SFTPClient, remote_path: str):
        """Ensure remote directory exists (create if needed)."""
        dirs = remote_path.split('/')
        current_path = ''
        for dir_name in dirs:
            if not dir_name:
                continue
            current_path += '/' + dir_name
            try:
                sftp.stat(current_path)
            except FileNotFoundError:
                sftp.mkdir(current_path)

    def _upload_sync(self, local_path: Path, remote_path: str):
        """Synchronous upload implementation."""
        ssh, sftp = self._get_connection()
        try:
            full_remote_path = self.remote_path + remote_path
            # Ensure parent directory exists
            parent_dir = os.path.dirname(full_remote_path)
            self._ensure_remote_dir(sftp, parent_dir)
            # Upload file
            sftp.put(str(local_path), full_remote_path)
            logger.info(f"Uploaded {local_path} to sftp://{self.host}{full_remote_path}")
        finally:
            sftp.close()
            ssh.close()

    async def upload(self, local_path: Path, remote_path: str) -> bool:
        """Upload file to SFTP server."""
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._upload_sync, local_path, remote_path)
            return True
        except Exception as e:
            logger.error(f"SFTP upload failed: {e}")
            raise

    def _download_sync(self, remote_path: str, local_path: Path):
        """Synchronous download implementation."""
        ssh, sftp = self._get_connection()
        try:
            full_remote_path = self.remote_path + remote_path
            sftp.get(full_remote_path, str(local_path))
            logger.info(f"Downloaded sftp://{self.host}{full_remote_path} to {local_path}")
        finally:
            sftp.close()
            ssh.close()

    async def download(self, remote_path: str, local_path: Path) -> bool:
        """Download file from SFTP server."""
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._download_sync, remote_path, local_path)
            return True
        except Exception as e:
            logger.error(f"SFTP download failed: {e}")
            raise

    def _delete_sync(self, remote_path: str):
        """Synchronous delete implementation."""
        ssh, sftp = self._get_connection()
        try:
            full_remote_path = self.remote_path + remote_path
            sftp.remove(full_remote_path)
            logger.info(f"Deleted sftp://{self.host}{full_remote_path}")
        finally:
            sftp.close()
            ssh.close()

    async def delete(self, remote_path: str) -> bool:
        """Delete file from SFTP server."""
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._delete_sync, remote_path)
            return True
        except Exception as e:
            logger.error(f"SFTP delete failed: {e}")
            raise

    def _exists_sync(self, remote_path: str) -> bool:
        """Synchronous exists check."""
        ssh, sftp = self._get_connection()
        try:
            full_remote_path = self.remote_path + remote_path
            sftp.stat(full_remote_path)
            return True
        except FileNotFoundError:
            return False
        finally:
            sftp.close()
            ssh.close()

    async def exists(self, remote_path: str) -> bool:
        """Check if file exists on SFTP server."""
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._exists_sync, remote_path)
        except Exception:
            return False

    def _test_connection_sync(self) -> Tuple[bool, str]:
        """Synchronous connection test."""
        try:
            ssh, sftp = self._get_connection()
            # Try to list remote directory
            try:
                sftp.listdir(self.remote_path)
            except FileNotFoundError:
                # Directory doesn't exist, try to create it
                self._ensure_remote_dir(sftp, self.remote_path)
            sftp.close()
            ssh.close()
            return True, f"Conexión exitosa a {self.host}:{self.port}"
        except paramiko.AuthenticationException:
            return False, "Error de autenticación. Verifica usuario y contraseña"
        except paramiko.SSHException as e:
            return False, f"Error SSH: {str(e)}"
        except Exception as e:
            return False, f"Error de conexión: {str(e)}"

    async def test_connection(self) -> Tuple[bool, str]:
        """Test SFTP connection."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._test_connection_sync)

    async def get_download_url(self, remote_path: str, expires_in: int = 3600) -> Optional[str]:
        """SFTP doesn't support download URLs."""
        return None


class SFTPStoragePlain(SFTPStorage):
    """SFTP storage with plain (non-encrypted) credentials for testing."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        remote_path: str = "/backups/openjornada/"
    ):
        self.host = host
        self.port = port
        self.username = username
        self.remote_path = remote_path.rstrip('/') + '/'
        self._password = password
