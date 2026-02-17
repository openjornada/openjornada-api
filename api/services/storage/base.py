"""
Abstract base class for storage backends.
Implements Strategy pattern for different storage providers.
"""
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional, Tuple


class StorageBackend(ABC):
    """Abstract base class for backup storage backends."""

    @abstractmethod
    async def upload(self, local_path: Path, remote_path: str) -> bool:
        """
        Upload a file to storage.

        Args:
            local_path: Path to local file
            remote_path: Destination path in storage

        Returns:
            True if successful
        """
        pass

    @abstractmethod
    async def download(self, remote_path: str, local_path: Path) -> bool:
        """
        Download a file from storage.

        Args:
            remote_path: Source path in storage
            local_path: Destination local path

        Returns:
            True if successful
        """
        pass

    @abstractmethod
    async def delete(self, remote_path: str) -> bool:
        """
        Delete a file from storage.

        Args:
            remote_path: Path to delete

        Returns:
            True if successful
        """
        pass

    @abstractmethod
    async def exists(self, remote_path: str) -> bool:
        """
        Check if a file exists in storage.

        Args:
            remote_path: Path to check

        Returns:
            True if exists
        """
        pass

    @abstractmethod
    async def test_connection(self) -> Tuple[bool, str]:
        """
        Test connection to storage.

        Returns:
            Tuple of (success, message)
        """
        pass

    @abstractmethod
    async def get_download_url(self, remote_path: str, expires_in: int = 3600) -> Optional[str]:
        """
        Get a download URL for a file (if supported).

        Args:
            remote_path: Path to file
            expires_in: URL expiration in seconds

        Returns:
            Download URL or None if not supported
        """
        pass
