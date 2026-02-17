from fastapi import APIRouter, HTTPException, status, Depends

from ..models.settings import (
    SettingsResponse, SettingsUpdate, BackupConfigResponse,
    BackupConfigInput, BackupConfigStored, BackupSchedule,
    S3ConfigStored, SFTPConfigStored, LocalConfig
)
from ..models.auth import APIUser
from ..database import db, convert_id
from ..auth.permissions import PermissionChecker
from ..utils.encryption import credential_encryption
from ..services.scheduler_service import scheduler_service

router = APIRouter()


def _build_backup_config_response(backup_config: dict | None) -> BackupConfigResponse | None:
    """Build backup config response from stored config (hides credentials)."""
    if not backup_config:
        return None

    response = BackupConfigResponse(
        enabled=backup_config.get("enabled", False),
        schedule=BackupSchedule(**backup_config["schedule"]) if backup_config.get("schedule") else None,
        retention_days=backup_config.get("retention_days", 730),
        storage_type=backup_config.get("storage_type", "local")
    )

    # S3 info
    s3_config = backup_config.get("s3_config")
    if s3_config:
        response.s3_configured = True
        response.s3_endpoint = s3_config.get("endpoint_url")
        response.s3_bucket = s3_config.get("bucket_name")

    # SFTP info
    sftp_config = backup_config.get("sftp_config")
    if sftp_config:
        response.sftp_configured = True
        response.sftp_host = sftp_config.get("host")
        response.sftp_path = sftp_config.get("remote_path")

    # Local info
    local_config = backup_config.get("local_config")
    if local_config:
        response.local_configured = True
        response.local_path = local_config.get("path")

    return response


def _build_settings_response(settings: dict) -> SettingsResponse:
    """Build settings response from database document."""
    backup_config_response = _build_backup_config_response(settings.get("backup_config"))

    return SettingsResponse(
        id=str(settings["_id"]),
        contact_email=settings["contact_email"],
        backup_config=backup_config_response
    )


@router.get("/settings/", response_model=SettingsResponse)
async def get_settings(current_user: APIUser = Depends(PermissionChecker("view_settings"))):
    """
    Get application settings. Creates default settings if they don't exist.
    Admin only.
    """
    # Find settings document
    settings = await db.Settings.find_one()

    # If no settings exist, create default
    if not settings:
        default_settings = {
            "contact_email": "support@openjornada.local"
        }
        result = await db.Settings.insert_one(default_settings)
        settings = await db.Settings.find_one({"_id": result.inserted_id})

    return _build_settings_response(settings)


@router.patch("/settings/", response_model=SettingsResponse)
async def update_settings(
    settings_update: SettingsUpdate,
    current_user: APIUser = Depends(PermissionChecker("update_settings"))
):
    """
    Update application settings (partial update).
    Admin only.
    """
    # Get current settings (create if not exists)
    settings = await db.Settings.find_one()

    if not settings:
        # Create default settings first
        default_settings = {
            "contact_email": "support@openjornada.local"
        }
        result = await db.Settings.insert_one(default_settings)
        settings = await db.Settings.find_one({"_id": result.inserted_id})

    # Prepare update data
    update_data = {}

    # Handle contact_email
    if settings_update.contact_email is not None:
        update_data["contact_email"] = settings_update.contact_email

    # Handle backup_config
    if settings_update.backup_config is not None:
        backup_input = settings_update.backup_config
        backup_stored = _process_backup_config(backup_input, settings.get("backup_config"))
        update_data["backup_config"] = backup_stored

    if not update_data:
        # No fields to update
        return _build_settings_response(settings)

    # Update settings
    await db.Settings.update_one(
        {"_id": settings["_id"]},
        {"$set": update_data}
    )

    # Reload scheduler if backup config changed
    if "backup_config" in update_data:
        await scheduler_service.reload_schedule()

    # Return updated settings
    updated_settings = await db.Settings.find_one({"_id": settings["_id"]})
    return _build_settings_response(updated_settings)


def _process_backup_config(backup_input: BackupConfigInput, existing_config: dict | None) -> dict:
    """
    Process backup config input and encrypt credentials.
    Preserves existing encrypted credentials if not provided in input.
    """
    stored = {
        "enabled": backup_input.enabled,
        "retention_days": backup_input.retention_days,
        "storage_type": backup_input.storage_type
    }

    # Schedule
    if backup_input.schedule:
        stored["schedule"] = backup_input.schedule.model_dump()

    # S3 config
    if backup_input.s3_config:
        s3_input = backup_input.s3_config
        stored["s3_config"] = {
            "endpoint_url": s3_input.endpoint_url,
            "bucket_name": s3_input.bucket_name,
            "access_key_id_encrypted": credential_encryption.encrypt(s3_input.access_key_id),
            "secret_access_key_encrypted": credential_encryption.encrypt(s3_input.secret_access_key),
            "region": s3_input.region
        }
    elif existing_config and existing_config.get("s3_config"):
        # Preserve existing S3 config
        stored["s3_config"] = existing_config["s3_config"]

    # SFTP config
    if backup_input.sftp_config:
        sftp_input = backup_input.sftp_config
        stored["sftp_config"] = {
            "host": sftp_input.host,
            "port": sftp_input.port,
            "username": sftp_input.username,
            "password_encrypted": credential_encryption.encrypt(sftp_input.password),
            "remote_path": sftp_input.remote_path
        }
    elif existing_config and existing_config.get("sftp_config"):
        # Preserve existing SFTP config
        stored["sftp_config"] = existing_config["sftp_config"]

    # Local config
    if backup_input.local_config:
        stored["local_config"] = backup_input.local_config.model_dump()
    elif existing_config and existing_config.get("local_config"):
        # Preserve existing local config
        stored["local_config"] = existing_config["local_config"]
    else:
        # Default local config
        stored["local_config"] = {"path": "/app/backups"}

    return stored
