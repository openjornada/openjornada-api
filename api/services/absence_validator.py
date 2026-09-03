"""
AbsenceValidator - Validates an absence request before it's created and
again in real time before it's approved (clones the shape of
``change_request_validator.py``, but distinguishes blocking issues from
non-blocking warnings — see design decision D2 and the ``absence-requests``
spec).
"""
from datetime import date, datetime
from typing import List, Optional, Tuple

from bson.objectid import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..models.absences import AbsenceStatus, AbsenceValidationIssue
from .absence_balance_service import get_absence_balance

# Statuses that "occupy" a date range for overlap purposes.
_ACTIVE_STATUSES = [AbsenceStatus.PENDING.value, AbsenceStatus.ACCEPTED.value]


def _to_datetime(d: date) -> datetime:
    """Convert a date to a naive UTC-midnight datetime (MongoDB storage format)."""
    return datetime.combine(d, datetime.min.time())


def _ranges_overlap(start_a: date, end_a: date, start_b: date, end_b: date) -> bool:
    """True if [start_a, end_a] and [start_b, end_b] (inclusive) overlap."""
    return start_a <= end_b and end_a >= start_b


class AbsenceValidator:
    """Validates absence requests: overlap, blackout, advance notice, balance, attachment."""

    async def validate(
        self,
        db: AsyncIOMotorDatabase,
        *,
        worker_id: str,
        company_id: str,
        start_date: date,
        end_date: date,
        days_computed: float,
        absence_type: dict,
        attachment_id: Optional[str],
        policy: dict,
        worker: dict,
        exclude_absence_id: Optional[str] = None,
        created_at: Optional[datetime] = None,
    ) -> Tuple[bool, List[AbsenceValidationIssue]]:
        """
        Validate an absence request.

        Args:
            db: Motor database handle.
            worker_id: Requesting worker's MongoDB _id (string).
            company_id: Company MongoDB _id (string).
            start_date: First day of the absence (inclusive).
            end_date: Last day of the absence (inclusive).
            days_computed: Days this request would consume, per the policy's
                computation mode.
            absence_type: The matching entry from ``policy["absence_types"]``.
            attachment_id: Attachment id supplied by the worker, if any.
            policy: Raw AbsencePolicies document for this company.
            worker: Raw Workers document.
            exclude_absence_id: Absence _id to exclude from overlap/balance
                checks (used when re-validating an existing request).
            created_at: When the request was (or would be) created; defaults
                to now. Used to check the minimum advance notice.

        Returns:
            (is_valid, issues) — ``is_valid`` is False only if at least one
            issue is blocking; non-blocking issues (warnings) are still
            returned so the admin screen can display them.
        """
        issues: List[AbsenceValidationIssue] = []

        exclude_oid = None
        if exclude_absence_id:
            try:
                exclude_oid = ObjectId(exclude_absence_id)
            except Exception:
                exclude_oid = None

        # 1. Overlap with another (non-cancelled/rejected) absence of the same worker — blocks.
        overlap_query: dict = {
            "worker_id": worker_id,
            "company_id": company_id,
            "status": {"$in": _ACTIVE_STATUSES},
            "start_date": {"$lte": _to_datetime(end_date)},
            "end_date": {"$gte": _to_datetime(start_date)},
        }
        if exclude_oid is not None:
            overlap_query["_id"] = {"$ne": exclude_oid}

        own_overlap = await db.Absences.find_one(overlap_query)
        if own_overlap:
            issues.append(AbsenceValidationIssue(
                code="OVERLAP_ABSENCE",
                message="Las fechas se solapan con otra ausencia ya existente del trabajador",
                blocking=True,
            ))

        # 2. Blackout periods — blocks.
        for blackout in policy.get("blackout_periods", []):
            b_start = blackout.get("start_date")
            b_end = blackout.get("end_date")
            if isinstance(b_start, datetime):
                b_start = b_start.date()
            if isinstance(b_end, datetime):
                b_end = b_end.date()
            if b_start and b_end and _ranges_overlap(start_date, end_date, b_start, b_end):
                issues.append(AbsenceValidationIssue(
                    code="BLACKOUT_PERIOD",
                    message=f"Las fechas caen dentro del periodo bloqueado \"{blackout.get('name', '')}\"",
                    blocking=True,
                ))
                break

        # 3. Minimum advance notice — warns, does not block (the admin can
        # approve urgent/exceptional requests).
        min_advance_days = policy.get("min_advance_days", 0)
        if min_advance_days:
            reference_dt = created_at or datetime.now()
            advance_days = (start_date - reference_dt.date()).days
            if advance_days < min_advance_days:
                issues.append(AbsenceValidationIssue(
                    code="MIN_ADVANCE_NOT_MET",
                    message=(
                        f"Se requiere una antelación mínima de {min_advance_days} día(s); "
                        f"la solicitud tiene {max(advance_days, 0)}"
                    ),
                    blocking=False,
                ))

        # 4. Attachment required by the absence type — blocks.
        if absence_type.get("requires_attachment") and not attachment_id:
            issues.append(AbsenceValidationIssue(
                code="ATTACHMENT_REQUIRED",
                message="Este tipo de ausencia requiere adjuntar un justificante",
                blocking=True,
            ))

        # 5. Sufficient balance — blocks (only when the type deducts balance).
        if absence_type.get("deducts_balance"):
            balance = await get_absence_balance(
                db,
                worker_id=worker_id,
                company_id=company_id,
                policy=policy,
                worker=worker,
                as_of=start_date,
                exclude_absence_id=exclude_absence_id,
            )
            if days_computed > balance.available_days:
                issues.append(AbsenceValidationIssue(
                    code="INSUFFICIENT_BALANCE",
                    message=(
                        f"Saldo insuficiente: disponibles {balance.available_days:.1f} día(s), "
                        f"solicitados {days_computed:.1f}"
                    ),
                    blocking=True,
                ))

        # 6. Max overlap per company — warns, does not block.
        max_overlap = policy.get("max_overlap_per_company")
        if max_overlap:
            overlap_pipeline_query: dict = {
                "company_id": company_id,
                "worker_id": {"$ne": worker_id},
                "status": {"$in": _ACTIVE_STATUSES},
                "start_date": {"$lte": _to_datetime(end_date)},
                "end_date": {"$gte": _to_datetime(start_date)},
            }
            other_workers = await db.Absences.distinct("worker_id", overlap_pipeline_query)
            concurrent_count = len(other_workers) + 1  # + this worker
            if concurrent_count > max_overlap:
                issues.append(AbsenceValidationIssue(
                    code="MAX_OVERLAP_EXCEEDED",
                    message=(
                        f"Aprobar esta solicitud dejaría a {concurrent_count} personas fuera a la "
                        f"vez, por encima del máximo configurado ({max_overlap})"
                    ),
                    blocking=False,
                ))

        # 7. Time records on an absence day — warns, does not block.
        time_records_query = {
            "worker_id": worker_id,
            "company_id": company_id,
            "created_at": {
                "$gte": _to_datetime(start_date),
                "$lte": datetime.combine(end_date, datetime.max.time()),
            },
        }
        has_time_records = await db.TimeRecords.find_one(time_records_query)
        if has_time_records:
            issues.append(AbsenceValidationIssue(
                code="TIME_RECORDS_EXIST",
                message="El trabajador tiene fichajes registrados en algún día de la ausencia",
                blocking=False,
            ))

        is_valid = not any(issue.blocking for issue in issues)
        return is_valid, issues
