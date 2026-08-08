import hashlib
import json
import logging
from datetime import datetime, timezone as dt_timezone
from bson import ObjectId
from fastapi import HTTPException, status

from ..database import db

logger = logging.getLogger(__name__)

_HASH_FIELDS = ("worker_id", "company_id", "type", "timestamp", "duration_minutes", "created_at")


def _canonical_datetime(value: datetime) -> str:
    """
    Normalize a datetime to a single canonical UTC representation.

    Two sources of non-determinism must be neutralised so that the hash is
    stable across a MongoDB write->read round-trip:

    - Timezone-awareness: values written in-memory are UTC-aware
      (``datetime.now(timezone.utc)``), while Motor may return naive
      datetimes that are implicitly UTC. Both must serialise identically.
    - Precision: BSON datetimes only have millisecond resolution, so a
      microsecond-precision Python datetime gets truncated once it round-trips
      through MongoDB. Truncating here too keeps pre-insert and post-read
      hashes equal.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt_timezone.utc)
    else:
        value = value.astimezone(dt_timezone.utc)
    value = value.replace(microsecond=(value.microsecond // 1000) * 1000)
    return value.isoformat()


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
            if isinstance(value, datetime):
                value = _canonical_datetime(value)
            elif hasattr(value, "isoformat"):
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
            ``verified`` (bool), ``status`` (``"verified"``, ``"tampered"`` or
            ``"legacy"``).

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

        if not stored_hash:
            # Record predates this capability: no hash was ever stored.
            # Distinct from tampering — there is nothing to compare against.
            record_status = "legacy"
            verified = False
            logger.debug("Integrity check: record %s has no stored hash (legacy)", record_id)
        else:
            verified = stored_hash == computed_hash
            record_status = "verified" if verified else "tampered"
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
            "status": record_status,
        }
