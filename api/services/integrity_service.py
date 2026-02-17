import hashlib
import json
import logging
from bson import ObjectId
from fastapi import HTTPException, status

from ..database import db

logger = logging.getLogger(__name__)

_HASH_FIELDS = ("worker_id", "company_id", "type", "timestamp", "duration_minutes", "created_at")


class IntegrityService:
    """SHA-256 integrity verification for time records and exported reports."""

    @staticmethod
    def compute_record_hash(record: dict) -> str:
        """
        Compute the SHA-256 hash of a time record.

        Only a fixed subset of fields is included so that non-critical metadata
        changes (e.g. internal flags) do not invalidate the hash. The payload is
        serialised as canonical JSON (sorted keys, no extra whitespace).

        Args:
            record: Raw MongoDB document or equivalent dict.

        Returns:
            Lowercase hex-encoded SHA-256 digest.
        """
        payload: dict = {}
        for field in _HASH_FIELDS:
            value = record.get(field)
            # Datetime objects must be serialised to an ISO string so that the
            # JSON encoder can handle them deterministically.
            if hasattr(value, "isoformat"):
                value = value.isoformat()
            payload[field] = value

        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def compute_report_hash(report_data: bytes) -> str:
        """
        Compute the SHA-256 hash of an exported report file (PDF, CSV, XLSX).

        Args:
            report_data: Raw bytes of the exported file.

        Returns:
            Lowercase hex-encoded SHA-256 digest.
        """
        return hashlib.sha256(report_data).hexdigest()

    @staticmethod
    async def verify_record_integrity(record_id: str) -> dict:
        """
        Verify the integrity of a stored time record.

        Fetches the record from the database, recomputes its hash from the
        current field values, and compares it against the ``integrity_hash``
        stored at creation time.

        Args:
            record_id: The string representation of the MongoDB ``_id``.

        Returns:
            Dict with keys: ``record_id``, ``stored_hash``, ``computed_hash``,
            ``verified`` (bool).

        Raises:
            HTTPException 404: If no record with the given ID exists.
        """
        try:
            object_id = ObjectId(record_id)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Time record not found: {record_id}",
            )

        record = await db.TimeRecords.find_one({"_id": object_id})
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Time record not found: {record_id}",
            )

        stored_hash: str = record.get("integrity_hash", "")
        computed_hash: str = IntegrityService.compute_record_hash(record)
        verified: bool = stored_hash == computed_hash

        if not verified:
            logger.warning(
                "Integrity check FAILED for record %s: stored=%s computed=%s",
                record_id,
                stored_hash,
                computed_hash,
            )
        else:
            logger.debug("Integrity check passed for record %s", record_id)

        return {
            "record_id": record_id,
            "stored_hash": stored_hash,
            "computed_hash": computed_hash,
            "verified": verified,
        }
