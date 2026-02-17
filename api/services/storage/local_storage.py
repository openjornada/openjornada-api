"""
Local filesystem storage backend.
Stores backups on the local filesystem (bind mount from host).
"""
import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import Optional, Tuple

from .base import StorageBackend

logger = logging.getLogger(__name__)


class LocalStorage(StorageBackend):
    """Local filesystem storage backend."""

    def __init__(self, base_path: str = "/app/backups"):
        self.base_path = Path(base_path)

    def _ensure_dir(self, path: Path):
        """Ensure directory exists."""
        path.parent.mkdir(parents=True, exist_ok=True)

    async def upload(self, local_path: Path, remote_path: str) -> bool:
        """
        Copy file to storage location.
        Note: 'upload' here means copying from temp location to storage location.
        """
        try:
            dest_path = self.base_path / remote_path
            self._ensure_dir(dest_path)

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                shutil.copy2,
                str(local_path),
                str(dest_path)
            )
            logger.info(f"Copied {local_path} to {dest_path}")
            return True
        except Exception as e:
            logger.error(f"Local storage upload failed: {e}")
            raise

    async def download(self, remote_path: str, local_path: Path) -> bool:
        """
        Copy file from storage location to temp.
        """
        try:
            source_path = self.base_path / remote_path
            local_path.parent.mkdir(parents=True, exist_ok=True)

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                shutil.copy2,
                str(source_path),
                str(local_path)
            )
            logger.info(f"Copied {source_path} to {local_path}")
            return True
        except Exception as e:
            logger.error(f"Local storage download failed: {e}")
            raise

    async def delete(self, remote_path: str) -> bool:
        """Delete file from storage location."""
        try:
            file_path = self.base_path / remote_path

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, os.remove, str(file_path))

            # Try to remove empty parent directories
            try:
                parent = file_path.parent
                while parent != self.base_path:
                    if not any(parent.iterdir()):
                        parent.rmdir()
                    parent = parent.parent
            except Exception:
                pass  # Ignore errors when cleaning up directories

            logger.info(f"Deleted {file_path}")
            return True
        except Exception as e:
            logger.error(f"Local storage delete failed: {e}")
            raise

    async def exists(self, remote_path: str) -> bool:
        """Check if file exists in storage."""
        file_path = self.base_path / remote_path
        return file_path.exists()

    async def test_connection(self) -> Tuple[bool, str]:
        """Test local storage access."""
        try:
            # Check if base path exists
            if not self.base_path.exists():
                try:
                    self.base_path.mkdir(parents=True, exist_ok=True)
                except PermissionError:
                    return False, f"Sin permisos para crear {self.base_path}"

            # Check if writable
            test_file = self.base_path / ".write_test"
            try:
                test_file.touch()
                test_file.unlink()
            except PermissionError:
                return False, f"Sin permisos de escritura en {self.base_path}"

            # Get available space
            stat = os.statvfs(str(self.base_path))
            free_gb = (stat.f_bavail * stat.f_frsize) / (1024**3)

            return True, f"Almacenamiento local OK. {free_gb:.1f} GB disponibles en {self.base_path}"

        except Exception as e:
            return False, f"Error: {str(e)}"

    async def get_download_url(self, remote_path: str, expires_in: int = 3600) -> Optional[str]:
        """
        Local storage doesn't support download URLs.
        Files must be downloaded through the API endpoint.
        """
        return None

    def get_full_path(self, remote_path: str) -> Path:
        """Get the full filesystem path for a backup file."""
        return self.base_path / remote_path
