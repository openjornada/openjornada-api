"""
Backup service for MongoDB database backups.
Handles backup creation, restoration, and management.
"""
import asyncio
import subprocess
import tempfile
import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from bson import ObjectId

from ..database import db, MONGO_URL, DB_NAME
from ..utils.encryption import credential_encryption
from .storage import StorageBackend, S3Storage, SFTPStorage, LocalStorage
from .storage.s3_storage import S3StoragePlain
from .storage.sftp_storage import SFTPStoragePlain

logger = logging.getLogger(__name__)


class BackupService:
    """Handles MongoDB backup and restore operations."""

    def __init__(self):
        self._storage_cache = {}

    def _get_storage_backend(self, backup_config: dict) -> StorageBackend:
        """Get appropriate storage backend based on config."""
        storage_type = backup_config.get("storage_type", "local")

        if storage_type == "s3":
            s3_config = backup_config.get("s3_config", {})
            return S3Storage(
                endpoint_url=s3_config["endpoint_url"],
                bucket_name=s3_config["bucket_name"],
                access_key_id_encrypted=s3_config["access_key_id_encrypted"],
                secret_access_key_encrypted=s3_config["secret_access_key_encrypted"],
                region=s3_config.get("region", "us-west-004")
            )
        elif storage_type == "sftp":
            sftp_config = backup_config.get("sftp_config", {})
            return SFTPStorage(
                host=sftp_config["host"],
                port=sftp_config.get("port", 22),
                username=sftp_config["username"],
                password_encrypted=sftp_config["password_encrypted"],
                remote_path=sftp_config.get("remote_path", "/backups/openjornada/")
            )
        else:  # local
            local_config = backup_config.get("local_config", {})
            return LocalStorage(
                base_path=local_config.get("path", "/app/backups")
            )

    async def test_connection(
        self,
        storage_type: str,
        s3_config: Optional[dict] = None,
        sftp_config: Optional[dict] = None,
        local_config: Optional[dict] = None
    ) -> Tuple[bool, str]:
        """Test storage connection with provided (unencrypted) credentials."""
        try:
            if storage_type == "s3" and s3_config:
                storage = S3StoragePlain(
                    endpoint_url=s3_config["endpoint_url"],
                    bucket_name=s3_config["bucket_name"],
                    access_key_id=s3_config["access_key_id"],
                    secret_access_key=s3_config["secret_access_key"],
                    region=s3_config.get("region", "us-west-004")
                )
            elif storage_type == "sftp" and sftp_config:
                storage = SFTPStoragePlain(
                    host=sftp_config["host"],
                    port=sftp_config.get("port", 22),
                    username=sftp_config["username"],
                    password=sftp_config["password"],
                    remote_path=sftp_config.get("remote_path", "/backups/openjornada/")
                )
            elif storage_type == "local":
                path = local_config.get("path", "/app/backups") if local_config else "/app/backups"
                storage = LocalStorage(base_path=path)
            else:
                return False, "Configuración de storage inválida"

            return await storage.test_connection()

        except Exception as e:
            return False, f"Error de conexión: {str(e)}"

    async def create_backup(self, trigger: str = "manual") -> dict:
        """
        Create a MongoDB backup and upload to storage.

        Args:
            trigger: "manual", "scheduled", or "pre_restore"

        Returns:
            Backup document on success
        """
        # Get settings
        settings = await db.Settings.find_one()
        if not settings:
            raise ValueError("Settings not configured")

        backup_config = settings.get("backup_config")
        if not backup_config:
            raise ValueError("Backup not configured")

        storage_type = backup_config.get("storage_type", "local")

        # Create backup record
        now = datetime.now(timezone.utc)
        filename = f"backup_{now.strftime('%Y-%m-%d_%H-%M-%S')}.gz"

        # Storage path based on type
        if storage_type == "local":
            storage_path = f"{now.year}/{now.month:02d}/{filename}"
        else:
            storage_path = f"backups/{now.year}/{now.month:02d}/{filename}"

        backup_doc = {
            "filename": filename,
            "storage_path": storage_path,
            "storage_type": storage_type,
            "size_bytes": 0,
            "size_human": "0 B",
            "created_at": now,
            "completed_at": None,
            "duration_seconds": None,
            "status": "in_progress",
            "trigger": trigger,
            "error_message": None,
            "collections_count": None,
            "documents_count": None,
            "checksum_sha256": None
        }

        result = await db.Backups.insert_one(backup_doc)
        backup_id = result.inserted_id

        try:
            # Run mongodump
            loop = asyncio.get_event_loop()
            backup_path, stats = await loop.run_in_executor(
                None,
                self._run_mongodump,
                filename
            )

            # Calculate file size and checksum
            file_size = backup_path.stat().st_size
            checksum = self._calculate_checksum(backup_path)

            # Upload to storage
            storage = self._get_storage_backend(backup_config)
            await storage.upload(backup_path, storage_path)

            # Clean up local temp file
            backup_path.unlink()
            backup_path.parent.rmdir()

            # Update backup record
            completed_at = datetime.now(timezone.utc)
            await db.Backups.update_one(
                {"_id": backup_id},
                {"$set": {
                    "size_bytes": file_size,
                    "size_human": self._format_size(file_size),
                    "completed_at": completed_at,
                    "duration_seconds": int((completed_at - now).total_seconds()),
                    "status": "completed",
                    "collections_count": stats.get("collections", 0),
                    "documents_count": stats.get("documents", 0),
                    "checksum_sha256": checksum
                }}
            )

            logger.info(f"Backup completed: {filename} ({self._format_size(file_size)})")

        except Exception as e:
            # Update backup record with error
            await db.Backups.update_one(
                {"_id": backup_id},
                {"$set": {
                    "status": "failed",
                    "error_message": str(e),
                    "completed_at": datetime.now(timezone.utc)
                }}
            )
            logger.error(f"Backup failed: {e}")
            raise

        return await db.Backups.find_one({"_id": backup_id})

    def _run_mongodump(self, filename: str) -> Tuple[Path, dict]:
        """Run mongodump command (blocking)."""
        temp_dir = Path(tempfile.mkdtemp())
        output_path = temp_dir / filename

        # Build mongodump command
        cmd = [
            "mongodump",
            f"--uri={MONGO_URL}",
            f"--db={DB_NAME}",
            "--gzip",
            f"--archive={output_path}"
        ]

        logger.info(f"Running mongodump to {output_path}")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            raise RuntimeError(f"mongodump failed: {result.stderr}")

        # Parse output for stats
        stats = self._parse_mongodump_output(result.stderr)

        return output_path, stats

    def _parse_mongodump_output(self, output: str) -> dict:
        """Parse mongodump output for statistics."""
        stats = {"collections": 0, "documents": 0}

        # Count collections
        collection_matches = re.findall(r'done dumping (\S+)', output)
        stats["collections"] = len(collection_matches)

        # Count documents
        doc_matches = re.findall(r'\((\d+) documents?\)', output)
        stats["documents"] = sum(int(m) for m in doc_matches)

        return stats

    async def restore_backup(self, backup_id: str) -> dict:
        """
        Restore database from a backup.
        Creates a pre-restore backup first for safety.

        Args:
            backup_id: ID of backup to restore

        Returns:
            Result dict with status and message
        """
        # Get backup record
        backup = await db.Backups.find_one({"_id": ObjectId(backup_id)})
        if not backup:
            raise ValueError("Backup not found")
        if backup["status"] != "completed":
            raise ValueError("Cannot restore from incomplete backup")

        # Get settings
        settings = await db.Settings.find_one()
        backup_config = settings.get("backup_config", {})

        # Create pre-restore backup for safety
        logger.info("Creating pre-restore backup...")
        pre_restore_backup = await self.create_backup(trigger="pre_restore")

        try:
            # Get storage backend
            storage = self._get_storage_backend(backup_config)

            # Download backup from storage
            temp_dir = Path(tempfile.mkdtemp())
            local_path = temp_dir / backup["filename"]

            await storage.download(backup["storage_path"], local_path)

            # Run mongorestore
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                self._run_mongorestore,
                local_path
            )

            # Clean up
            local_path.unlink()
            temp_dir.rmdir()

            logger.info(f"Restore completed from: {backup['filename']}")

            return {
                "status": "success",
                "message": f"Base de datos restaurada desde: {backup['filename']}",
                "pre_restore_backup_id": str(pre_restore_backup["_id"])
            }

        except Exception as e:
            logger.error(f"Restore failed: {e}")
            return {
                "status": "failed",
                "message": f"Error en la restauración: {str(e)}",
                "pre_restore_backup_id": str(pre_restore_backup["_id"])
            }

    def _run_mongorestore(self, backup_path: Path):
        """Run mongorestore command (blocking)."""
        cmd = [
            "mongorestore",
            f"--uri={MONGO_URL}",
            f"--db={DB_NAME}",
            "--gzip",
            f"--archive={backup_path}",
            "--drop"  # Drop existing collections before restore
        ]

        logger.info(f"Running mongorestore from {backup_path}")
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            raise RuntimeError(f"mongorestore failed: {result.stderr}")

    async def delete_backup(self, backup_id: str) -> bool:
        """Delete a backup from storage and database."""
        # Get backup record
        backup = await db.Backups.find_one({"_id": ObjectId(backup_id)})
        if not backup:
            raise ValueError("Backup not found")

        # Get settings
        settings = await db.Settings.find_one()
        backup_config = settings.get("backup_config", {})

        # Delete from storage
        storage = self._get_storage_backend(backup_config)
        try:
            await storage.delete(backup["storage_path"])
        except Exception as e:
            logger.warning(f"Failed to delete from storage: {e}")

        # Delete from database
        await db.Backups.delete_one({"_id": ObjectId(backup_id)})

        logger.info(f"Deleted backup: {backup['filename']}")
        return True

    async def cleanup_old_backups(self):
        """Delete backups older than retention period."""
        settings = await db.Settings.find_one()
        if not settings:
            return

        backup_config = settings.get("backup_config", {})
        if not backup_config.get("enabled"):
            return

        retention_days = backup_config.get("retention_days", 730)
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=retention_days)

        # Find old backups (exclude pre_restore backups from cleanup)
        old_backups = await db.Backups.find({
            "created_at": {"$lt": cutoff_date},
            "status": "completed",
            "trigger": {"$ne": "pre_restore"}
        }).to_list(None)

        for backup in old_backups:
            try:
                await self.delete_backup(str(backup["_id"]))
                logger.info(f"Cleaned up old backup: {backup['filename']}")
            except Exception as e:
                logger.error(f"Failed to cleanup backup {backup['filename']}: {e}")

    async def get_download_url(self, backup_id: str) -> Optional[str]:
        """Get download URL for a backup (S3 only)."""
        backup = await db.Backups.find_one({"_id": ObjectId(backup_id)})
        if not backup:
            raise ValueError("Backup not found")

        settings = await db.Settings.find_one()
        backup_config = settings.get("backup_config", {})

        storage = self._get_storage_backend(backup_config)
        return await storage.get_download_url(backup["storage_path"])

    async def get_local_backup_path(self, backup_id: str) -> Optional[Path]:
        """Get local path for a backup (local storage only)."""
        backup = await db.Backups.find_one({"_id": ObjectId(backup_id)})
        if not backup:
            raise ValueError("Backup not found")

        if backup["storage_type"] != "local":
            return None

        settings = await db.Settings.find_one()
        backup_config = settings.get("backup_config", {})
        local_config = backup_config.get("local_config", {})

        storage = LocalStorage(base_path=local_config.get("path", "/app/backups"))
        return storage.get_full_path(backup["storage_path"])

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        """Format bytes as human-readable string."""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size_bytes < 1024:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024
        return f"{size_bytes:.1f} TB"

    @staticmethod
    def _calculate_checksum(file_path: Path) -> str:
        """Calculate SHA256 checksum of file."""
        sha256 = hashlib.sha256()
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                sha256.update(chunk)
        return sha256.hexdigest()


# Import here to avoid circular import
from datetime import timedelta

# Singleton
backup_service = BackupService()
