"""
S3-compatible storage backend.
Works with AWS S3, Backblaze B2, DigitalOcean Spaces, MinIO, etc.
"""
import asyncio
import logging
from pathlib import Path
from typing import Optional, Tuple

import boto3
from botocore.exceptions import ClientError

from .base import StorageBackend
from ...utils.encryption import credential_encryption

logger = logging.getLogger(__name__)


class S3Storage(StorageBackend):
    """S3-compatible storage backend using boto3."""

    def __init__(
        self,
        endpoint_url: str,
        bucket_name: str,
        access_key_id_encrypted: str,
        secret_access_key_encrypted: str,
        region: str = "us-west-004"
    ):
        self.endpoint_url = endpoint_url
        self.bucket_name = bucket_name
        self.region = region
        # Decrypt credentials
        self._access_key_id = credential_encryption.decrypt(access_key_id_encrypted)
        self._secret_access_key = credential_encryption.decrypt(secret_access_key_encrypted)
        self._client = None

    def _get_client(self):
        """Get or create S3 client."""
        if self._client is None:
            self._client = boto3.client(
                's3',
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self._access_key_id,
                aws_secret_access_key=self._secret_access_key,
                region_name=self.region
            )
        return self._client

    async def upload(self, local_path: Path, remote_path: str) -> bool:
        """Upload file to S3."""
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: self._get_client().upload_file(
                    str(local_path),
                    self.bucket_name,
                    remote_path,
                    ExtraArgs={'ContentType': 'application/gzip'}
                )
            )
            logger.info(f"Uploaded {local_path} to s3://{self.bucket_name}/{remote_path}")
            return True
        except Exception as e:
            logger.error(f"S3 upload failed: {e}")
            raise

    async def download(self, remote_path: str, local_path: Path) -> bool:
        """Download file from S3."""
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: self._get_client().download_file(
                    self.bucket_name,
                    remote_path,
                    str(local_path)
                )
            )
            logger.info(f"Downloaded s3://{self.bucket_name}/{remote_path} to {local_path}")
            return True
        except Exception as e:
            logger.error(f"S3 download failed: {e}")
            raise

    async def delete(self, remote_path: str) -> bool:
        """Delete file from S3."""
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: self._get_client().delete_object(
                    Bucket=self.bucket_name,
                    Key=remote_path
                )
            )
            logger.info(f"Deleted s3://{self.bucket_name}/{remote_path}")
            return True
        except Exception as e:
            logger.error(f"S3 delete failed: {e}")
            raise

    async def exists(self, remote_path: str) -> bool:
        """Check if file exists in S3."""
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: self._get_client().head_object(
                    Bucket=self.bucket_name,
                    Key=remote_path
                )
            )
            return True
        except ClientError as e:
            if e.response['Error']['Code'] == '404':
                return False
            raise

    async def test_connection(self) -> Tuple[bool, str]:
        """Test S3 connection."""
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: self._get_client().list_objects_v2(
                    Bucket=self.bucket_name,
                    MaxKeys=1
                )
            )
            return True, f"Conexión exitosa a {self.bucket_name}"
        except ClientError as e:
            error_code = e.response['Error']['Code']
            if error_code == 'NoSuchBucket':
                return False, f"El bucket '{self.bucket_name}' no existe"
            elif error_code == 'AccessDenied':
                return False, "Acceso denegado. Verifica las credenciales"
            return False, f"Error S3: {str(e)}"
        except Exception as e:
            return False, f"Error de conexión: {str(e)}"

    async def get_download_url(self, remote_path: str, expires_in: int = 3600) -> Optional[str]:
        """Get pre-signed download URL."""
        try:
            loop = asyncio.get_event_loop()
            url = await loop.run_in_executor(
                None,
                lambda: self._get_client().generate_presigned_url(
                    'get_object',
                    Params={
                        'Bucket': self.bucket_name,
                        'Key': remote_path
                    },
                    ExpiresIn=expires_in
                )
            )
            return url
        except Exception as e:
            logger.error(f"Failed to generate presigned URL: {e}")
            return None


class S3StoragePlain(S3Storage):
    """S3 storage with plain (non-encrypted) credentials for testing."""

    def __init__(
        self,
        endpoint_url: str,
        bucket_name: str,
        access_key_id: str,
        secret_access_key: str,
        region: str = "us-west-004"
    ):
        self.endpoint_url = endpoint_url
        self.bucket_name = bucket_name
        self.region = region
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._client = None
