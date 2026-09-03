"""
AttachmentService - Stores absence justificantes (supporting documents) in
GridFS, on the same tenant MongoDB (see design decision D4). Files are
therefore included in the existing per-tenant Mongo backups without any new
infrastructure.
"""
from datetime import datetime, timezone as dt_timezone
from typing import Optional

from bson.errors import InvalidId
from bson.objectid import ObjectId
from fastapi import HTTPException, status
from motor.motor_asyncio import AsyncIOMotorGridFSBucket

from ..database import db

# Justificantes are typically scans/photos of a document: PDF or common
# image formats. Adjust here if new formats need to be accepted.
ALLOWED_CONTENT_TYPES = {"application/pdf", "image/jpeg", "image/png"}
MAX_ATTACHMENT_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB

_bucket = AsyncIOMotorGridFSBucket(db, bucket_name="absence_attachments")


class AttachmentService:
    """Uploads and reads absence justificantes stored in GridFS."""

    async def upload(
        self,
        *,
        company_id: str,
        worker_id: str,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> str:
        """
        Validate and store a justificante in GridFS.

        Args:
            company_id: Company the absence belongs to.
            worker_id: Worker uploading the file.
            filename: Original filename (for display only).
            content_type: MIME type reported by the client.
            data: Raw file bytes.

        Returns:
            The GridFS file id as a string (``attachment_id``).

        Raises:
            HTTPException 400: If the file type is not allowed or it exceeds
                the maximum size.
        """
        if content_type not in ALLOWED_CONTENT_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Tipo de archivo no permitido. Formatos aceptados: "
                    "PDF, JPEG, PNG"
                ),
            )

        if len(data) > MAX_ATTACHMENT_SIZE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"El archivo supera el tamaño máximo permitido "
                    f"({MAX_ATTACHMENT_SIZE_BYTES // (1024 * 1024)} MB)"
                ),
            )

        if not data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="El archivo está vacío",
            )

        file_id = await _bucket.upload_from_stream(
            filename,
            data,
            metadata={
                "company_id": company_id,
                "worker_id": worker_id,
                "content_type": content_type,
                "uploaded_at": datetime.now(dt_timezone.utc),
            },
        )
        return str(file_id)

    async def get(self, attachment_id: str) -> tuple[bytes, dict]:
        """
        Retrieve a stored justificante by id.

        Args:
            attachment_id: GridFS file id (string).

        Returns:
            Tuple of (file bytes, metadata dict with filename/content_type/company_id/worker_id).

        Raises:
            HTTPException 404: If the attachment doesn't exist.
        """
        try:
            oid = ObjectId(attachment_id)
        except InvalidId:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Justificante no encontrado",
            )

        try:
            stream = await _bucket.open_download_stream(oid)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Justificante no encontrado",
            )

        data = await stream.read()
        metadata = dict(stream.metadata or {})
        metadata["filename"] = stream.filename
        return data, metadata

    async def get_metadata(self, attachment_id: str) -> Optional[dict]:
        """Return only the stored metadata for an attachment, or None if missing."""
        try:
            oid = ObjectId(attachment_id)
        except InvalidId:
            return None

        doc = await db["absence_attachments.files"].find_one({"_id": oid})
        if doc is None:
            return None
        metadata = dict(doc.get("metadata") or {})
        metadata["filename"] = doc.get("filename")
        return metadata


attachment_service = AttachmentService()
