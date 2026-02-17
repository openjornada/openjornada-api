from pydantic import BaseModel
from typing import Optional, Literal, List
from datetime import datetime


class BackupResponse(BaseModel):
    """Backup record response model."""
    id: str
    filename: str
    storage_path: str  # S3 key, SFTP path, or local path
    storage_type: Literal["s3", "sftp", "local"]
    size_bytes: int
    size_human: str
    created_at: datetime
    completed_at: Optional[datetime] = None
    duration_seconds: Optional[int] = None
    status: Literal["in_progress", "completed", "failed"]
    trigger: Literal["scheduled", "manual", "pre_restore"]
    error_message: Optional[str] = None
    collections_count: Optional[int] = None
    documents_count: Optional[int] = None
    checksum_sha256: Optional[str] = None


class BackupListResponse(BaseModel):
    """Response for listing backups."""
    backups: List[BackupResponse]
    total_count: int
    total_size_bytes: int
    total_size_human: str


class RestoreRequest(BaseModel):
    """Request to restore from a backup."""
    confirm: bool = False  # Must be True to proceed


class RestoreResponse(BaseModel):
    """Response after restore operation."""
    status: Literal["success", "failed"]
    message: str
    pre_restore_backup_id: Optional[str] = None  # Auto-backup created before restore


class TestConnectionRequest(BaseModel):
    """Request to test storage connection."""
    storage_type: Literal["s3", "sftp", "local"]
    # S3 fields
    s3_endpoint_url: Optional[str] = None
    s3_bucket_name: Optional[str] = None
    s3_access_key_id: Optional[str] = None
    s3_secret_access_key: Optional[str] = None
    s3_region: Optional[str] = None
    # SFTP fields
    sftp_host: Optional[str] = None
    sftp_port: Optional[int] = 22
    sftp_username: Optional[str] = None
    sftp_password: Optional[str] = None
    sftp_remote_path: Optional[str] = None
    # Local fields
    local_path: Optional[str] = None


class TestConnectionResponse(BaseModel):
    """Response from testing storage connection."""
    success: bool
    message: str
