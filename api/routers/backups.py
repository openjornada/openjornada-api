"""
Backups router - API endpoints for backup management.
"""
from fastapi import APIRouter, HTTPException, status, Depends
from fastapi.responses import FileResponse
from bson import ObjectId
from typing import Optional

from ..models.backups import (
    BackupResponse,
    BackupListResponse,
    RestoreRequest,
    RestoreResponse,
    TestConnectionRequest,
    TestConnectionResponse
)
from ..models.auth import APIUser
from ..database import db, convert_id
from ..auth.permissions import PermissionChecker
from ..services.backup_service import backup_service
from ..services.scheduler_service import scheduler_service

router = APIRouter()


def _backup_to_response(backup: dict) -> BackupResponse:
    """Convert backup document to response model."""
    return BackupResponse(
        id=str(backup["_id"]),
        filename=backup["filename"],
        storage_path=backup["storage_path"],
        storage_type=backup["storage_type"],
        size_bytes=backup["size_bytes"],
        size_human=backup["size_human"],
        created_at=backup["created_at"],
        completed_at=backup.get("completed_at"),
        duration_seconds=backup.get("duration_seconds"),
        status=backup["status"],
        trigger=backup["trigger"],
        error_message=backup.get("error_message"),
        collections_count=backup.get("collections_count"),
        documents_count=backup.get("documents_count"),
        checksum_sha256=backup.get("checksum_sha256")
    )


@router.get("/backups/", response_model=BackupListResponse)
async def list_backups(
    current_user: APIUser = Depends(PermissionChecker("view_backups"))
):
    """
    List all backups with summary statistics.
    Admin only.
    """
    backups = await db.Backups.find().sort("created_at", -1).to_list(None)

    # Calculate totals
    completed_backups = [b for b in backups if b.get("status") == "completed"]
    total_size = sum(b.get("size_bytes", 0) for b in completed_backups)

    return BackupListResponse(
        backups=[_backup_to_response(b) for b in backups],
        total_count=len(backups),
        total_size_bytes=total_size,
        total_size_human=backup_service._format_size(total_size)
    )


@router.post("/backups/trigger", response_model=BackupResponse)
async def trigger_backup(
    current_user: APIUser = Depends(PermissionChecker("manage_backups"))
):
    """
    Trigger a manual backup.
    Admin only.
    """
    try:
        backup = await backup_service.create_backup(trigger="manual")
        return _backup_to_response(backup)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Backup failed: {str(e)}"
        )


@router.get("/backups/{backup_id}", response_model=BackupResponse)
async def get_backup(
    backup_id: str,
    current_user: APIUser = Depends(PermissionChecker("view_backups"))
):
    """
    Get details of a specific backup.
    Admin only.
    """
    try:
        backup = await db.Backups.find_one({"_id": ObjectId(backup_id)})
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid backup ID"
        )

    if not backup:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Backup not found"
        )

    return _backup_to_response(backup)


@router.delete("/backups/{backup_id}")
async def delete_backup(
    backup_id: str,
    current_user: APIUser = Depends(PermissionChecker("manage_backups"))
):
    """
    Delete a backup from storage and database.
    Admin only.
    """
    try:
        await backup_service.delete_backup(backup_id)
        return {"message": "Backup eliminado correctamente"}
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error al eliminar backup: {str(e)}"
        )


@router.post("/backups/{backup_id}/restore", response_model=RestoreResponse)
async def restore_backup(
    backup_id: str,
    request: RestoreRequest,
    current_user: APIUser = Depends(PermissionChecker("manage_backups"))
):
    """
    Restore database from a backup.
    Creates a pre-restore backup first for safety.
    Admin only.
    """
    if not request.confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Confirmación requerida. Establece confirm=true para continuar."
        )

    try:
        result = await backup_service.restore_backup(backup_id)
        return RestoreResponse(**result)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error en la restauración: {str(e)}"
        )


@router.get("/backups/{backup_id}/download-url")
async def get_download_url(
    backup_id: str,
    current_user: APIUser = Depends(PermissionChecker("manage_backups"))
):
    """
    Get a pre-signed URL to download the backup file (S3 only).
    For local storage, use the /download endpoint.
    Admin only.
    """
    try:
        backup = await db.Backups.find_one({"_id": ObjectId(backup_id)})
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid backup ID"
        )

    if not backup:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Backup not found"
        )

    if backup["storage_type"] == "local":
        # For local storage, return API endpoint
        return {
            "download_url": f"/api/backups/{backup_id}/download",
            "expires_in": None,
            "storage_type": "local"
        }

    url = await backup_service.get_download_url(backup_id)
    if not url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se puede generar URL de descarga para este tipo de storage"
        )

    return {"download_url": url, "expires_in": 3600, "storage_type": backup["storage_type"]}


@router.get("/backups/{backup_id}/download")
async def download_backup(
    backup_id: str,
    current_user: APIUser = Depends(PermissionChecker("manage_backups"))
):
    """
    Download backup file directly (local storage only).
    Admin only.
    """
    try:
        backup = await db.Backups.find_one({"_id": ObjectId(backup_id)})
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid backup ID"
        )

    if not backup:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Backup not found"
        )

    if backup["storage_type"] != "local":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Descarga directa solo disponible para storage local. Usa download-url para S3/SFTP."
        )

    file_path = await backup_service.get_local_backup_path(backup_id)
    if not file_path or not file_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Archivo de backup no encontrado"
        )

    return FileResponse(
        path=str(file_path),
        filename=backup["filename"],
        media_type="application/gzip"
    )


@router.post("/backups/test-connection", response_model=TestConnectionResponse)
async def test_connection(
    request: TestConnectionRequest,
    current_user: APIUser = Depends(PermissionChecker("update_settings"))
):
    """
    Test storage connection with provided credentials.
    Admin only.
    """
    s3_config = None
    sftp_config = None
    local_config = None

    if request.storage_type == "s3":
        if not all([request.s3_endpoint_url, request.s3_bucket_name,
                    request.s3_access_key_id, request.s3_secret_access_key]):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Faltan campos requeridos para S3"
            )
        s3_config = {
            "endpoint_url": request.s3_endpoint_url,
            "bucket_name": request.s3_bucket_name,
            "access_key_id": request.s3_access_key_id,
            "secret_access_key": request.s3_secret_access_key,
            "region": request.s3_region or "us-west-004"
        }

    elif request.storage_type == "sftp":
        if not all([request.sftp_host, request.sftp_username, request.sftp_password]):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Faltan campos requeridos para SFTP"
            )
        sftp_config = {
            "host": request.sftp_host,
            "port": request.sftp_port or 22,
            "username": request.sftp_username,
            "password": request.sftp_password,
            "remote_path": request.sftp_remote_path or "/backups/openjornada/"
        }

    elif request.storage_type == "local":
        local_config = {
            "path": request.local_path or "/app/backups"
        }

    success, message = await backup_service.test_connection(
        storage_type=request.storage_type,
        s3_config=s3_config,
        sftp_config=sftp_config,
        local_config=local_config
    )

    return TestConnectionResponse(success=success, message=message)


@router.get("/backups/schedule/status")
async def get_schedule_status(
    current_user: APIUser = Depends(PermissionChecker("view_backups"))
):
    """
    Get backup schedule status.
    Admin only.
    """
    is_scheduled = scheduler_service.is_backup_scheduled()
    next_run = scheduler_service.get_next_run_time()

    return {
        "scheduled": is_scheduled,
        "next_run": next_run.isoformat() if next_run else None
    }
