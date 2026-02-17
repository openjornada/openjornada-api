from pydantic import BaseModel, EmailStr, Field
from typing import Optional, Literal


# ============================================================================
# Backup Configuration Models
# ============================================================================

class BackupSchedule(BaseModel):
    """Schedule configuration for automated backups."""
    frequency: Literal["daily", "weekly", "monthly"] = "daily"
    time: str = "00:00"  # HH:MM in UTC
    day_of_week: int = Field(default=0, ge=0, le=6)  # 0=Monday, 6=Sunday
    day_of_month: int = Field(default=1, ge=1, le=28)


class S3ConfigInput(BaseModel):
    """S3 configuration input (plain credentials)."""
    endpoint_url: str  # e.g., https://s3.us-west-004.backblazeb2.com
    bucket_name: str
    access_key_id: str
    secret_access_key: str
    region: str = "us-west-004"


class S3ConfigStored(BaseModel):
    """S3 configuration as stored in DB (encrypted credentials)."""
    endpoint_url: str
    bucket_name: str
    access_key_id_encrypted: str
    secret_access_key_encrypted: str
    region: str = "us-west-004"


class SFTPConfigInput(BaseModel):
    """SFTP configuration input (plain credentials)."""
    host: str
    port: int = 22
    username: str
    password: str
    remote_path: str = "/backups/openjornada/"


class SFTPConfigStored(BaseModel):
    """SFTP configuration as stored in DB (encrypted credentials)."""
    host: str
    port: int = 22
    username: str
    password_encrypted: str
    remote_path: str = "/backups/openjornada/"


class LocalConfig(BaseModel):
    """Local filesystem configuration."""
    path: str = "/app/backups"


class BackupConfigInput(BaseModel):
    """Backup configuration input model (for API updates)."""
    enabled: bool = False
    schedule: Optional[BackupSchedule] = None
    retention_days: int = Field(default=730, ge=1, le=3650)  # 2 years default, max 10 years
    storage_type: Literal["s3", "sftp", "local"] = "local"
    s3_config: Optional[S3ConfigInput] = None
    sftp_config: Optional[SFTPConfigInput] = None
    local_config: Optional[LocalConfig] = None


class BackupConfigStored(BaseModel):
    """Backup configuration as stored in MongoDB."""
    enabled: bool = False
    schedule: Optional[BackupSchedule] = None
    retention_days: int = 730
    storage_type: Literal["s3", "sftp", "local"] = "local"
    s3_config: Optional[S3ConfigStored] = None
    sftp_config: Optional[SFTPConfigStored] = None
    local_config: Optional[LocalConfig] = None


class BackupConfigResponse(BaseModel):
    """Backup configuration response (hides sensitive data)."""
    enabled: bool = False
    schedule: Optional[BackupSchedule] = None
    retention_days: int = 730
    storage_type: Literal["s3", "sftp", "local"] = "local"
    # S3 info (without credentials)
    s3_configured: bool = False
    s3_endpoint: Optional[str] = None
    s3_bucket: Optional[str] = None
    # SFTP info (without credentials)
    sftp_configured: bool = False
    sftp_host: Optional[str] = None
    sftp_path: Optional[str] = None
    # Local info
    local_configured: bool = False
    local_path: Optional[str] = None


# ============================================================================
# Settings Models
# ============================================================================

class SettingsBase(BaseModel):
    contact_email: EmailStr


class SettingsUpdate(BaseModel):
    contact_email: Optional[EmailStr] = None
    backup_config: Optional[BackupConfigInput] = None


class SettingsInDB(SettingsBase):
    id: str  # MongoDB _id converted to string
    backup_config: Optional[BackupConfigStored] = None


class SettingsResponse(SettingsBase):
    id: str
    backup_config: Optional[BackupConfigResponse] = None
