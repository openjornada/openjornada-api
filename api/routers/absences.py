"""
Router for absence & vacation requests.

Two families of endpoints, mirroring ``routers/change_requests.py``:

- Worker endpoints (email + password authentication, no JWT required):
    POST /absences/                    Create a request
    POST /absences/me                  Own absence history
    POST /absences/me/balance          Own vacation balance
    POST /absences/me/types            Company's absence type catalogue
    POST /absences/me/{id}/cancel      Cancel a pending request
    POST /absences/me/calendar         Team calendar (privacy-limited)
    POST /absences/attachments         Upload a justificante (multipart)

- Admin endpoints (JWT authentication via PermissionChecker):
    GET   /absences/                   List with filters
    GET   /absences/{id}               Detail (+ validation_errors)
    PATCH /absences/{id}               Approve / reject (atomic)
    GET   /absences/calendar           Full team calendar
    GET   /absences/attachments/{id}   Download a justificante

Every endpoint is gated by ``require_absence_module``/``ensure_absence_module_enabled``:
absences can only be operated on for companies with the module active (D9).
"""
import logging
import os
from datetime import date, datetime, timezone as dt_timezone
from typing import List, Optional

from bson.objectid import ObjectId
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from pymongo import ReturnDocument

from ..auth.permissions import PermissionChecker
from ..database import db, convert_id
from ..models.absences import (
    AbsenceBalance,
    AbsenceCalendarEntryAdmin,
    AbsenceCalendarEntryWorker,
    AbsenceRequestCreate,
    AbsenceResponse,
    AbsenceStatus,
    AbsenceType,
    AbsenceUpdate,
    WorkerAbsenceBalanceRequest,
    WorkerAbsenceCalendarRequest,
    WorkerAbsenceCancelRequest,
    WorkerAbsenceResponse,
    WorkerAbsencesRequest,
    WorkerAbsenceTypesRequest,
)
from ..models.auth import APIUser
from ..services.absence_balance_service import get_absence_balance
from ..services.absence_days_service import compute_absence_days
from ..services.absence_gating import ensure_absence_module_enabled, require_absence_module
from ..services.absence_validator import AbsenceValidator
from ..services.attachment_service import attachment_service
from ..services.email_service import EmailService
from ..utils.worker_auth import _authenticate_worker, _verify_worker_company_access
from .absence_policies import _get_or_seed_policy

router = APIRouter()
validator = AbsenceValidator()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_datetime(d: date) -> datetime:
    """Convert a date to a naive UTC-midnight datetime (MongoDB storage format)."""
    return datetime.combine(d, datetime.min.time())


def ensure_utc_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Convert a naive datetime (from MongoDB) to UTC aware."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=dt_timezone.utc)
    return dt


async def _get_policy_or_404(company_id: str) -> dict:
    policy = await db.AbsencePolicies.find_one({"company_id": company_id})
    if policy is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="La empresa no tiene una política de ausencias configurada",
        )
    return policy


def _find_absence_type(policy: dict, code: str) -> Optional[dict]:
    for absence_type in policy.get("absence_types", []):
        if absence_type.get("code") == code:
            return absence_type
    return None


def prepare_absence_response(doc: dict) -> dict:
    """Prepare a MongoDB absence document for AbsenceResponse/WorkerAbsenceResponse.

    Ensures datetime fields typed as AwareDatetime in the response models are
    UTC-aware, since MongoDB always returns naive datetimes.
    """
    data = convert_id(doc)
    data["created_at"] = ensure_utc_aware(data.get("created_at"))
    data["updated_at"] = ensure_utc_aware(data.get("updated_at"))
    data["reviewed_at"] = ensure_utc_aware(data.get("reviewed_at"))
    return data


async def _get_worker_or_404(worker_id: str) -> dict:
    try:
        oid = ObjectId(worker_id)
    except Exception:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Worker not found")
    worker = await db.Workers.find_one({"_id": oid, "deleted_at": None})
    if worker is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Worker not found")
    return worker


# ---------------------------------------------------------------------------
# Worker endpoints (email + password authentication)
# ---------------------------------------------------------------------------


@router.post("/", response_model=WorkerAbsenceResponse, status_code=status.HTTP_201_CREATED)
async def create_absence_request(
    request_data: AbsenceRequestCreate,
    current_user: APIUser = Depends(PermissionChecker("create_absences")),
) -> WorkerAbsenceResponse:
    """
    Create a new absence request. Worker authenticates with email/password.

    The request is created in PENDING status. Blocking validation issues
    (overlap, blackout period, insufficient advance notice, insufficient
    balance, missing required attachment) reject the creation outright.
    """
    worker = await _authenticate_worker(request_data.email, request_data.password)
    _verify_worker_company_access(worker, request_data.company_id)

    company = await ensure_absence_module_enabled(request_data.company_id)

    if request_data.end_date < request_data.start_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La fecha de fin no puede ser anterior a la fecha de inicio",
        )

    policy = await _get_or_seed_policy(request_data.company_id)

    absence_type = _find_absence_type(policy, request_data.absence_type_code)
    if absence_type is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Tipo de ausencia desconocido: {request_data.absence_type_code}",
        )

    if request_data.is_partial and not policy.get("allow_half_day", True):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La política de esta empresa no permite solicitudes de medio día",
        )

    is_hourly = request_data.start_time is not None and request_data.end_time is not None
    if is_hourly and not policy.get("allow_hourly", False):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La política de esta empresa no permite solicitudes por horas",
        )

    days_computed = compute_absence_days(
        request_data.start_date,
        request_data.end_date,
        policy.get("computation", "business_days"),
        request_data.is_partial,
        request_data.day_portion,
        is_hourly=is_hourly,
    )

    worker_id = str(worker["_id"])
    now = datetime.now(dt_timezone.utc)

    is_valid, issues = await validator.validate(
        db,
        worker_id=worker_id,
        company_id=request_data.company_id,
        start_date=request_data.start_date,
        end_date=request_data.end_date,
        days_computed=days_computed,
        absence_type=absence_type,
        attachment_id=request_data.attachment_id,
        policy=policy,
        worker=worker,
        created_at=now,
    )
    if not is_valid:
        blocking_messages = [issue.message for issue in issues if issue.blocking]
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="; ".join(blocking_messages),
        )

    absence_doc = {
        "worker_id": worker_id,
        "worker_email": worker["email"],
        "worker_first_name": worker.get("first_name", ""),
        "worker_last_name": worker.get("last_name", ""),

        "company_id": request_data.company_id,
        "company_name": company.get("name", ""),

        "absence_type_code": absence_type["code"],
        "absence_type_name": absence_type["name"],
        "deducts_balance": absence_type.get("deducts_balance", False),

        "start_date": _to_datetime(request_data.start_date),
        "end_date": _to_datetime(request_data.end_date),
        "is_partial": request_data.is_partial,
        "day_portion": request_data.day_portion,
        "start_time": request_data.start_time.isoformat() if request_data.start_time else None,
        "end_time": request_data.end_time.isoformat() if request_data.end_time else None,

        "worker_comment": request_data.worker_comment,
        "attachment_id": request_data.attachment_id,
        "days_computed": days_computed,

        "status": AbsenceStatus.PENDING.value,
        "created_at": now,
        "created_by": worker_id,
        "updated_at": now,
        "updated_by": worker_id,

        "reviewed_by_admin_id": None,
        "reviewed_by_admin_email": None,
        "reviewed_at": None,
        "admin_internal_notes": None,
        "admin_public_comment": None,
    }

    result = await db.Absences.insert_one(absence_doc)
    absence_doc["_id"] = result.inserted_id
    return WorkerAbsenceResponse(**prepare_absence_response(absence_doc))


@router.post("/me", response_model=List[WorkerAbsenceResponse])
async def get_worker_absence_history(request: WorkerAbsencesRequest) -> List[WorkerAbsenceResponse]:
    """
    Return the authenticated worker's own absence history.

    If ``company_id`` is omitted, results are limited to the companies the
    worker belongs to that have the absence module active.
    """
    worker = await _authenticate_worker(request.email, request.password)
    worker_id = str(worker["_id"])

    query: dict = {"worker_id": worker_id}

    if request.company_id is not None:
        _verify_worker_company_access(worker, request.company_id)
        await ensure_absence_module_enabled(request.company_id)
        query["company_id"] = request.company_id
    else:
        enabled_company_ids = await _enabled_company_ids_for_worker(worker)
        query["company_id"] = {"$in": enabled_company_ids}

    if request.status_filter is not None:
        query["status"] = request.status_filter.value

    results: List[WorkerAbsenceResponse] = []
    async for doc in db.Absences.find(query).sort("created_at", -1).limit(request.limit):
        results.append(WorkerAbsenceResponse(**prepare_absence_response(doc)))
    return results


@router.post("/me/balance", response_model=AbsenceBalance)
async def get_worker_absence_balance(request: WorkerAbsenceBalanceRequest) -> AbsenceBalance:
    """Return the authenticated worker's vacation balance for the given/current reference year."""
    worker = await _authenticate_worker(request.email, request.password)
    _verify_worker_company_access(worker, request.company_id)
    await ensure_absence_module_enabled(request.company_id)

    policy = await _get_or_seed_policy(request.company_id)
    return await get_absence_balance(
        db,
        worker_id=str(worker["_id"]),
        company_id=request.company_id,
        policy=policy,
        worker=worker,
        year=request.year,
    )


@router.post("/me/types", response_model=List[AbsenceType])
async def get_worker_absence_types(request: WorkerAbsenceTypesRequest) -> List[AbsenceType]:
    """
    Return the effective absence type catalogue for the worker's company.

    Lazily seeds the company's default policy (and its default type
    catalogue) if one doesn't exist yet — same seeding logic used by the
    admin policy endpoints — so the webapp always reflects the admin's
    customisation instead of a hardcoded default list.
    """
    worker = await _authenticate_worker(request.email, request.password)
    _verify_worker_company_access(worker, request.company_id)
    await ensure_absence_module_enabled(request.company_id)

    policy = await _get_or_seed_policy(request.company_id)
    return [AbsenceType(**absence_type) for absence_type in policy.get("absence_types", [])]


@router.post("/me/{absence_id}/cancel", response_model=WorkerAbsenceResponse)
async def cancel_absence_request(
    absence_id: str,
    request: WorkerAbsenceCancelRequest,
) -> WorkerAbsenceResponse:
    """Cancel one of the authenticated worker's own PENDING requests."""
    worker = await _authenticate_worker(request.email, request.password)
    worker_id = str(worker["_id"])

    try:
        absence_oid = ObjectId(absence_id)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid absence ID format")

    existing = await db.Absences.find_one({"_id": absence_oid, "worker_id": worker_id})
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Absence request not found")

    await ensure_absence_module_enabled(existing["company_id"])

    updated = await db.Absences.find_one_and_update(
        {"_id": absence_oid, "worker_id": worker_id, "status": AbsenceStatus.PENDING.value},
        {"$set": {
            "status": AbsenceStatus.CANCELLED.value,
            "updated_at": datetime.now(dt_timezone.utc),
            "updated_by": worker_id,
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Solo se pueden cancelar solicitudes pendientes",
        )

    return WorkerAbsenceResponse(**prepare_absence_response(updated))


@router.post("/me/calendar", response_model=List[AbsenceCalendarEntryWorker])
async def get_worker_team_calendar(request: WorkerAbsenceCalendarRequest) -> List[AbsenceCalendarEntryWorker]:
    """
    Return who is (or will be) absent within a date range — privacy-limited:
    name and dates only, no type/reason.
    """
    worker = await _authenticate_worker(request.email, request.password)
    _verify_worker_company_access(worker, request.company_id)
    await ensure_absence_module_enabled(request.company_id)

    query = {
        "company_id": request.company_id,
        "status": AbsenceStatus.ACCEPTED.value,
        "start_date": {"$lte": _to_datetime(request.end_date)},
        "end_date": {"$gte": _to_datetime(request.start_date)},
    }

    entries: List[AbsenceCalendarEntryWorker] = []
    async for doc in db.Absences.find(query):
        entries.append(AbsenceCalendarEntryWorker(
            worker_name=f"{doc.get('worker_first_name', '')} {doc.get('worker_last_name', '')}".strip(),
            start_date=doc["start_date"],
            end_date=doc["end_date"],
        ))
    return entries


@router.post("/attachments", status_code=status.HTTP_201_CREATED)
async def upload_absence_attachment(
    email: str = Form(...),
    password: str = Form(...),
    company_id: str = Form(...),
    file: UploadFile = File(...),
) -> dict:
    """Upload a justificante (supporting document) for an absence request. Returns its ``attachment_id``."""
    worker = await _authenticate_worker(email, password)
    _verify_worker_company_access(worker, company_id)
    await ensure_absence_module_enabled(company_id)

    data = await file.read()
    attachment_id = await attachment_service.upload(
        company_id=company_id,
        worker_id=str(worker["_id"]),
        filename=file.filename or "justificante",
        content_type=file.content_type or "application/octet-stream",
        data=data,
    )
    return {"attachment_id": attachment_id}


async def _enabled_company_ids_for_worker(worker: dict) -> List[str]:
    """Return the worker's company_ids restricted to companies with the module active."""
    company_ids = [str(cid) for cid in worker.get("company_ids", [])]
    if not company_ids:
        return []
    oids = [ObjectId(cid) for cid in company_ids]
    cursor = db.Companies.find(
        {"_id": {"$in": oids}, "deleted_at": None, "absence_management_enabled": True},
        {"_id": 1},
    )
    return [str(doc["_id"]) async for doc in cursor]


# ---------------------------------------------------------------------------
# Admin endpoints (JWT authentication)
# ---------------------------------------------------------------------------


@router.get("/", response_model=List[AbsenceResponse])
async def list_absences(
    company_id: str = Depends(require_absence_module),
    status_filter: Optional[AbsenceStatus] = Query(None, alias="status"),
    worker_id: Optional[str] = Query(None),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    current_user: APIUser = Depends(PermissionChecker("view_absences")),
) -> List[AbsenceResponse]:
    """List absence requests for a company with optional filters (admin only)."""
    query: dict = {"company_id": company_id}
    if status_filter:
        query["status"] = status_filter.value
    if worker_id:
        query["worker_id"] = worker_id
    if start_date:
        query["end_date"] = {"$gte": _to_datetime(start_date)}
    if end_date:
        query["start_date"] = {"$lte": _to_datetime(end_date)}

    results: List[AbsenceResponse] = []
    async for doc in db.Absences.find(query).sort("created_at", -1):
        results.append(AbsenceResponse(**prepare_absence_response(doc)))
    return results


@router.get("/calendar", response_model=List[AbsenceCalendarEntryAdmin])
async def get_admin_team_calendar(
    start_date: date = Query(...),
    end_date: date = Query(...),
    company_id: str = Depends(require_absence_module),
    current_user: APIUser = Depends(PermissionChecker("view_absences")),
) -> List[AbsenceCalendarEntryAdmin]:
    """Full team calendar (type, worker, dates) for a company (admin only)."""
    query = {
        "company_id": company_id,
        "status": AbsenceStatus.ACCEPTED.value,
        "start_date": {"$lte": _to_datetime(end_date)},
        "end_date": {"$gte": _to_datetime(start_date)},
    }

    entries: List[AbsenceCalendarEntryAdmin] = []
    async for doc in db.Absences.find(query):
        entries.append(AbsenceCalendarEntryAdmin(
            absence_id=str(doc["_id"]),
            worker_id=doc["worker_id"],
            worker_name=f"{doc.get('worker_first_name', '')} {doc.get('worker_last_name', '')}".strip(),
            absence_type_code=doc["absence_type_code"],
            absence_type_name=doc["absence_type_name"],
            start_date=doc["start_date"],
            end_date=doc["end_date"],
            status=doc["status"],
        ))
    return entries


@router.get("/attachments/{attachment_id}")
async def download_absence_attachment(
    attachment_id: str,
    current_user: APIUser = Depends(PermissionChecker("view_absences")),
) -> Response:
    """Download a justificante by id (admin only), gated by its absence's company."""
    metadata = await attachment_service.get_metadata(attachment_id)
    if metadata is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Justificante no encontrado")

    await ensure_absence_module_enabled(metadata.get("company_id", ""))

    data, metadata = await attachment_service.get(attachment_id)
    return Response(
        content=data,
        media_type=metadata.get("content_type", "application/octet-stream"),
        headers={"Content-Disposition": f'attachment; filename="{metadata.get("filename", "justificante")}"'},
    )


@router.get("/{absence_id}", response_model=AbsenceResponse)
async def get_absence(
    absence_id: str,
    current_user: APIUser = Depends(PermissionChecker("view_absences")),
) -> AbsenceResponse:
    """Get a single absence request by id (admin only). Includes validation_errors when PENDING."""
    try:
        absence_oid = ObjectId(absence_id)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid absence ID format")

    doc = await db.Absences.find_one({"_id": absence_oid})
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Absence request not found")

    await ensure_absence_module_enabled(doc["company_id"])

    response = AbsenceResponse(**prepare_absence_response(dict(doc)))

    if doc.get("status") == AbsenceStatus.PENDING.value:
        policy = await db.AbsencePolicies.find_one({"company_id": doc["company_id"]})
        worker = await db.Workers.find_one({"_id": ObjectId(doc["worker_id"])})
        if policy is not None and worker is not None:
            absence_type = _find_absence_type(policy, doc["absence_type_code"]) or {
                "requires_attachment": False,
                "deducts_balance": doc.get("deducts_balance", False),
            }
            _, issues = await validator.validate(
                db,
                worker_id=doc["worker_id"],
                company_id=doc["company_id"],
                start_date=doc["start_date"].date() if isinstance(doc["start_date"], datetime) else doc["start_date"],
                end_date=doc["end_date"].date() if isinstance(doc["end_date"], datetime) else doc["end_date"],
                days_computed=doc.get("days_computed", 0.0),
                absence_type=absence_type,
                attachment_id=doc.get("attachment_id"),
                policy=policy,
                worker=worker,
                exclude_absence_id=absence_id,
                created_at=doc.get("created_at"),
            )
            response.validation_errors = issues if issues else None

    return response


@router.patch("/{absence_id}", response_model=AbsenceResponse)
async def update_absence(
    absence_id: str,
    request_data: AbsenceUpdate,
    current_user: APIUser = Depends(PermissionChecker("manage_absences")),
) -> AbsenceResponse:
    """
    Approve or reject a PENDING absence request.

    CRITICAL: uses an atomic update — only the first transition on a PENDING
    request has effect (see design decision D2).
    """
    try:
        absence_oid = ObjectId(absence_id)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid absence ID format")

    current_doc = await db.Absences.find_one({"_id": absence_oid})
    if current_doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Absence request not found")

    await ensure_absence_module_enabled(current_doc["company_id"])

    if request_data.status == AbsenceStatus.ACCEPTED:
        policy = await db.AbsencePolicies.find_one({"company_id": current_doc["company_id"]})
        worker = await db.Workers.find_one({"_id": ObjectId(current_doc["worker_id"])})
        if policy is None or worker is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No se puede aprobar: falta la política o el trabajador ya no existe",
            )
        absence_type = _find_absence_type(policy, current_doc["absence_type_code"]) or {
            "requires_attachment": False,
            "deducts_balance": current_doc.get("deducts_balance", False),
        }
        is_valid, issues = await validator.validate(
            db,
            worker_id=current_doc["worker_id"],
            company_id=current_doc["company_id"],
            start_date=current_doc["start_date"].date() if isinstance(current_doc["start_date"], datetime) else current_doc["start_date"],
            end_date=current_doc["end_date"].date() if isinstance(current_doc["end_date"], datetime) else current_doc["end_date"],
            days_computed=current_doc.get("days_computed", 0.0),
            absence_type=absence_type,
            attachment_id=current_doc.get("attachment_id"),
            policy=policy,
            worker=worker,
            exclude_absence_id=absence_id,
            created_at=current_doc.get("created_at"),
        )
        if not is_valid:
            blocking_messages = [issue.message for issue in issues if issue.blocking]
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"No se puede aprobar: {'; '.join(blocking_messages)}",
            )

    # Atomic transition — only succeeds if status is currently PENDING.
    updated_doc = await db.Absences.find_one_and_update(
        {"_id": absence_oid, "status": AbsenceStatus.PENDING.value},
        {"$set": {
            "reviewed_by_admin_id": str(current_user.id),
            "reviewed_by_admin_email": current_user.email,
            "reviewed_at": datetime.now(dt_timezone.utc),
            "admin_internal_notes": request_data.admin_internal_notes,
            "admin_public_comment": request_data.admin_public_comment,
            "status": request_data.status.value,
            "updated_at": datetime.now(dt_timezone.utc),
            "updated_by": str(current_user.id),
        }},
        return_document=ReturnDocument.AFTER,
    )
    if updated_doc is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Absence request not found, already processed, or not pending",
        )

    email_service = EmailService()
    contact_email = os.getenv("SMTP_FROM_EMAIL", "support@openjornada.es")
    start_date_val = updated_doc["start_date"]
    end_date_val = updated_doc["end_date"]

    try:
        if request_data.status == AbsenceStatus.ACCEPTED:
            await email_service.send_absence_approved_email(
                to_email=updated_doc["worker_email"],
                worker_name=f"{updated_doc.get('worker_first_name', '')} {updated_doc.get('worker_last_name', '')}".strip(),
                company_name=updated_doc.get("company_name", ""),
                absence_type_name=updated_doc.get("absence_type_name", ""),
                start_date=start_date_val,
                end_date=end_date_val,
                days_computed=updated_doc.get("days_computed", 0.0),
                worker_comment=updated_doc.get("worker_comment") or "",
                admin_public_comment=request_data.admin_public_comment or "",
                contact_email=contact_email,
                locale="es",
            )
        elif request_data.status == AbsenceStatus.REJECTED:
            await email_service.send_absence_rejected_email(
                to_email=updated_doc["worker_email"],
                worker_name=f"{updated_doc.get('worker_first_name', '')} {updated_doc.get('worker_last_name', '')}".strip(),
                company_name=updated_doc.get("company_name", ""),
                absence_type_name=updated_doc.get("absence_type_name", ""),
                start_date=start_date_val,
                end_date=end_date_val,
                days_computed=updated_doc.get("days_computed", 0.0),
                worker_comment=updated_doc.get("worker_comment") or "",
                admin_public_comment=request_data.admin_public_comment or "",
                contact_email=contact_email,
                locale="es",
            )
    except Exception as e:
        # Log error but don't fail the request — the state transition already succeeded.
        logger.error(f"Error sending absence decision email: {e}")

    return AbsenceResponse(**prepare_absence_response(updated_doc))
