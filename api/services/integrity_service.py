import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone as dt_timezone
import pytz
from bson import ObjectId
from fastapi import HTTPException, status

from ..database import db
from ..utils.timezones import DEFAULT_REPORT_TIMEZONE
from .report_service import ReportService, ensure_utc_aware

logger = logging.getLogger(__name__)

# NOTE: these are the fields covered by compute_record_hash, and therefore by
# every monthly digest built on top of it (compute_month_digest). Changing
# this set changes every record hash and invalidates all derived monthly
# digests, because existing signatures are verified against the algorithm
# version persisted with them. If you modify it you MUST: (1) bump
# MONTHLY_DIGEST_VERSION, (2) add the new value to _SUPPORTED_DIGEST_VERSIONS
# keeping "v1" in it, and (3) make the per-record hashing depend on the
# ``version`` argument of compute_month_digest, so a "v1" signature is still
# recomputed with this field set. Only "v1" exists today, hence no dispatch.
_HASH_FIELDS = ("worker_id", "company_id", "type", "timestamp", "duration_minutes", "created_at")

# Version identifier of the monthly-digest algorithm. Used as the preimage
# prefix in compute_month_digest and persisted as ``content_hash_version`` on
# each signature, so a future algorithm change can coexist with signatures
# made under the old one.
MONTHLY_DIGEST_VERSION = "v1"

# Versions this code can still recompute. A signature carrying anything else
# verifies as "unsupported_version" instead of a misleading "mismatch".
_SUPPORTED_DIGEST_VERSIONS = frozenset({MONTHLY_DIGEST_VERSION})


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
    def compute_month_digest(
        records: list[dict],
        version: str = MONTHLY_DIGEST_VERSION,
    ) -> tuple[str, int]:
        """
        Compute the SHA-256 digest of a signed month's record set.

        Each record is hashed with ``compute_record_hash`` from its *current*
        field values; the ``integrity_hash`` stored on the document is
        deliberately ignored, because anyone able to edit the database could
        recompute it after tampering with a record.

        The leaf digests are sorted (as hex strings) and joined with newlines
        under the ``version`` prefix, so the result does not depend on
        retrieval order and the algorithm can evolve without silently
        invalidating existing signatures.

        Args:
            records: Raw TimeRecord documents covered by the signature.
            version: Digest algorithm version to compute, defaulting to the
                current ``MONTHLY_DIGEST_VERSION``. Verification passes the
                version persisted with the signature being checked.

        Returns:
            Tuple ``(digest, record_count)`` where digest is the lowercase
            hex-encoded SHA-256 of ``version + "\\n" + "\\n".join(sorted(leaves))``.
            The empty set hashes the bare version prefix (``"v1\\n"``); the
            result is a constant, not a unique identifier.

        Raises:
            ValueError: If ``version`` is not in ``_SUPPORTED_DIGEST_VERSIONS``.
        """
        if version not in _SUPPORTED_DIGEST_VERSIONS:
            raise ValueError(f"Unsupported monthly digest version: {version}")

        leaves = sorted(IntegrityService.compute_record_hash(record) for record in records)
        preimage = f"{version}\n" + "\n".join(leaves)
        return hashlib.sha256(preimage.encode("utf-8")).hexdigest(), len(records)

    @staticmethod
    def month_utc_range(year: int, month: int, timezone: str) -> tuple[datetime, datetime]:
        """
        Return the UTC window covering a local calendar month.

        Delegates to ``ReportService._month_utc_range`` so the period bound to
        a signature is delimited exactly like the monthly report the worker
        saw before signing.

        Args:
            year: Calendar year.
            month: Calendar month 1-12.
            timezone: IANA timezone name (e.g. "Europe/Madrid").

        Returns:
            Tuple of two UTC-aware datetimes (start inclusive, end exclusive).
        """
        return ReportService._month_utc_range(year, month, pytz.timezone(timezone))

    @staticmethod
    async def get_month_records(
        worker_id: str,
        company_id: str,
        year: int,
        month: int,
        timezone: str,
    ) -> list[dict]:
        """
        Fetch the time records covered by a monthly signature.

        Uses the same filter shape as
        ``ReportService.get_worker_monthly_summary``: worker + company +
        timestamp inside the UTC window of the local calendar month.

        Args:
            worker_id: MongoDB ``_id`` (string) of the worker.
            company_id: MongoDB ``_id`` (string) of the company.
            year: Calendar year.
            month: Calendar month 1-12.
            timezone: IANA timezone name delimiting the signed month.

        Returns:
            List of raw TimeRecord documents for the period (possibly empty).
        """
        start_utc, end_utc = IntegrityService.month_utc_range(year, month, timezone)
        return await db.TimeRecords.find(
            {
                "worker_id": worker_id,
                "company_id": company_id,
                "timestamp": {"$gte": start_utc, "$lt": end_utc},
            }
        ).sort("timestamp", 1).to_list(10_000)

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

    @staticmethod
    async def verify_monthly_signature(signature_id: str) -> dict:
        """
        Verify a monthly signature against the current time records.

        Loads the ``MonthlySignatures`` document, recomputes the month digest
        with the timezone persisted at signing time, and compares it with the
        digest stored on the signature. On ``mismatch``, the approved
        change-requests applied to the same worker/month after ``signed_at``
        are attached, so an audited correction can be told apart from an
        unexplained alteration.

        Signatures created before this capability carry no ``content_hash``
        and report ``legacy``: no digest is back-filled, because computing one
        today over data that may already have been altered would present
        unverified history as verified. A signature whose
        ``content_hash_version`` this code cannot compute reports
        ``unsupported_version`` and is not recomputed either, and one whose
        stored ``timezone`` cannot be resolved reports ``legacy`` without
        recomputing.

        Args:
            signature_id: String representation of the signature ``_id``.

        Returns:
            Dict with keys: ``signature_id``, ``status`` (``"verified"``,
            ``"mismatch"``, ``"legacy"`` or ``"unsupported_version"``),
            ``content_hash`` (digest stored at signing, ``""`` on legacy),
            ``computed_hash`` (digest recomputed now, ``""`` on legacy and on
            an unsupported version), ``content_hash_version``,
            ``timezone``, ``worker_id``, ``company_id``, ``year``, ``month``,
            ``signed_at``, ``signed_record_count``, ``current_record_count``
            and ``audited_corrections`` (non-empty only on ``mismatch``).

        Raises:
            HTTPException 404: If no signature with the given ID exists.
        """
        try:
            object_id = ObjectId(signature_id)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Monthly signature not found: {signature_id}",
            )

        signature = await db.MonthlySignatures.find_one({"_id": object_id})
        if signature is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Monthly signature not found: {signature_id}",
            )

        signed_hash: str = signature.get("content_hash", "")
        result: dict = {
            "signature_id": signature_id,
            "status": "legacy",
            "content_hash": signed_hash,
            "computed_hash": "",
            "content_hash_version": signature.get("content_hash_version"),
            "timezone": signature.get("timezone"),
            # Direct access: the only insertion path always writes these five,
            # and they are required by MonthlySignatureVerification anyway.
            "worker_id": signature["worker_id"],
            "company_id": signature["company_id"],
            "year": signature["year"],
            "month": signature["month"],
            "signed_at": ensure_utc_aware(signature["signed_at"]),
            "signed_record_count": signature.get("record_count"),
            "current_record_count": None,
            "audited_corrections": [],
        }

        if not signed_hash:
            # Signature predates this capability: no digest was ever bound to
            # it. Distinct from a mismatch — there is nothing to compare
            # against, and the record set is not recomputed.
            logger.debug("Monthly signature %s has no content_hash (legacy)", signature_id)
            return result

        # A signature with a digest but no version field was written by this
        # same code before the field existed, so "v1" is the right assumption.
        digest_version: str = signature.get("content_hash_version") or MONTHLY_DIGEST_VERSION
        if digest_version not in _SUPPORTED_DIGEST_VERSIONS:
            # Nothing is recomputed: reporting "mismatch" here would read as an
            # unexplained alteration when the only problem is that this build
            # does not know the algorithm the digest was made with.
            result["status"] = "unsupported_version"
            logger.warning(
                "Monthly signature %s carries unsupported digest version %s (supported: %s)",
                signature_id,
                digest_version,
                ", ".join(sorted(_SUPPORTED_DIGEST_VERSIONS)),
            )
            return result

        # The stored zone is what delimits the signed window; validate it once
        # and reuse the resolved name. An unresolvable zone means this build
        # cannot reproduce the window, so report "legacy" after a warning
        # instead of raising (500) or recomputing a misleading "mismatch".
        stored_timezone: str = signature.get("timezone") or DEFAULT_REPORT_TIMEZONE
        try:
            pytz.timezone(stored_timezone)
        except pytz.UnknownTimeZoneError:
            result["status"] = "legacy"
            logger.warning(
                "Monthly signature %s carries unresolvable timezone %r; "
                "reporting legacy without recomputing",
                signature_id,
                stored_timezone,
            )
            return result

        records = await IntegrityService.get_month_records(
            worker_id=signature["worker_id"],
            company_id=signature["company_id"],
            year=signature["year"],
            month=signature["month"],
            timezone=stored_timezone,
        )
        computed_hash, current_count = IntegrityService.compute_month_digest(records, digest_version)
        result["computed_hash"] = computed_hash
        result["current_record_count"] = current_count

        if computed_hash == signed_hash:
            result["status"] = "verified"
            logger.debug("Monthly signature check passed for %s", signature_id)
        else:
            result["status"] = "mismatch"
            logger.warning(
                "Monthly signature check FAILED for %s: signed=%s computed=%s",
                signature_id,
                signed_hash,
                computed_hash,
            )
            result["audited_corrections"] = await IntegrityService._find_audited_corrections(
                signature
            )

        return result

    @staticmethod
    async def _find_audited_corrections(signature: dict) -> list[dict]:
        """
        Approved change-requests applied to the signed worker/month after the
        signature was created. Used to explain a digest mismatch with an audit
        trail instead of leaving it as an unexplained alteration.

        A correction belongs to the month when its local ``date`` falls inside
        the month bounds widened by one day per side, or when either corrected
        instant falls inside the (equally widened) UTC window of the signed
        month: a correction applied to an adjacent day can rewrite a record of
        this month.

        Args:
            signature: Raw MonthlySignatures document (must carry worker_id,
                company_id, year, month, signed_at and, optionally, timezone).

        Returns:
            List of dicts with keys ``change_request_id``,
            ``reviewed_by_admin_email``, ``reviewed_at``, ``date`` (ISO date
            string) and ``reason``, sorted by application time ascending.
        """
        # ChangeRequests.date holds a NAIVE local calendar date at midnight
        # (``datetime.combine(date, min.time())`` in the change-requests
        # router), not a UTC instant, so it must be matched against the local
        # month bounds and not against the UTC window of the signed month:
        # under a negative-offset zone the UTC window would drop corrections
        # dated on the first day and pull in those of the next month.
        #
        # Both windows are widened by one day per side, and the corrected
        # instants are matched too, because a correction can change the digest
        # of a month it is not dated in: approving it recomputes the
        # ``duration_minutes`` (a hashed field) of the paired record of a shift
        # crossing midnight on the 1st or the last day, and moving a timestamp
        # can push a punch across the month boundary. The trade-off is
        # deliberate: attaching one correction too many is better than leaving
        # a mismatch with no audit trail, which the README prescribes
        # investigating as tampering.
        year, month = signature["year"], signature["month"]
        next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
        month_start = datetime(year, month, 1) - timedelta(days=1)
        month_end = datetime(next_year, next_month, 1) + timedelta(days=1)

        try:
            start_utc, end_utc = IntegrityService.month_utc_range(
                year, month, signature.get("timezone") or DEFAULT_REPORT_TIMEZONE,
            )
        except pytz.UnknownTimeZoneError:
            # Defensive: a signature whose stored zone this build cannot resolve
            # must not turn a "mismatch" report into a 500. Only reached on
            # mismatch, but the monthly-signature path can still be handed such
            # a document by other callers.
            logger.warning(
                "Unresolvable timezone %r looking up audited corrections for "
                "signature %s; falling back to %s",
                signature.get("timezone"),
                signature.get("_id"),
                DEFAULT_REPORT_TIMEZONE,
            )
            start_utc, end_utc = IntegrityService.month_utc_range(
                year, month, DEFAULT_REPORT_TIMEZONE,
            )
        start_utc -= timedelta(days=1)
        end_utc += timedelta(days=1)

        signed_at = ensure_utc_aware(signature.get("signed_at"))

        corrections: list[dict] = []
        query = {
            "worker_id": signature["worker_id"],
            "company_id": signature["company_id"],
            "status": "accepted",
            "reviewed_at": {"$gt": signed_at},
            "$or": [
                {"date": {"$gte": month_start, "$lt": month_end}},
                {"original_timestamp": {"$gte": start_utc, "$lt": end_utc}},
                {"new_timestamp": {"$gte": start_utc, "$lt": end_utc}},
            ],
        }
        async for cr in db.ChangeRequests.find(query).sort("reviewed_at", 1):
            cr_date = cr.get("date")
            corrections.append({
                "change_request_id": str(cr["_id"]),
                "reviewed_by_admin_email": cr.get("reviewed_by_admin_email", ""),
                "reviewed_at": ensure_utc_aware(cr.get("reviewed_at")),
                "date": cr_date.date().isoformat() if isinstance(cr_date, datetime) else str(cr_date or ""),
                "reason": cr.get("reason", ""),
            })
        return corrections
