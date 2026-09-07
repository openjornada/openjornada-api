import os
from fastapi import APIRouter, status, Depends, Query, Body
from datetime import datetime, date, timezone as dt_timezone
from typing import List, Optional
from bson.objectid import ObjectId

from ..models.change_requests import (
    ChangeRequestCreate,
    ChangeRequestUpdate,
    ChangeRequestResponse,
    ChangeRequestStatus,
    WorkerChangeRequestsRequest,
    WorkerChangeRequestResponse,
)
from ..models.auth import APIUser
from ..database import db, convert_id
from ..auth.auth_handler import verify_password
from ..auth.permissions import PermissionChecker
from ..services.change_request_validator import ChangeRequestValidator
from ..services.email_service import EmailService
from ..services.integrity_service import IntegrityService
from ..services.time_calculation_service import TimeCalculationService
from ..utils.company_locale import resolve_company_locale
from ..utils.errors import raise_api_error
from ..utils.worker_auth import _authenticate_worker, _verify_worker_company_access

router = APIRouter()
validator = ChangeRequestValidator()


def _dt_to_iso(val) -> Optional[str]:
    """Convert a datetime (naive or aware) or other value to an ISO 8601 string."""
    if val is None:
        return None
    if isinstance(val, datetime):
        if val.tzinfo is None:
            val = val.replace(tzinfo=dt_timezone.utc)
        return val.isoformat()
    return str(val)


def ensure_utc_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """
    Convierte un datetime naive (de MongoDB) a UTC aware.
    MongoDB devuelve datetimes naive que se asumen como UTC.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=dt_timezone.utc)
    return dt


async def _recompute_integrity_hash(record_id: ObjectId) -> None:
    """
    Recompute and persist ``integrity_hash`` for a TimeRecord from its
    current stored fields. Used after an approved change-request mutates a
    hashed field, so a legitimate correction doesn't read as tampering.
    """
    record = await db.TimeRecords.find_one({"_id": record_id})
    if record is None:
        return
    new_hash = IntegrityService.compute_record_hash(record)
    await db.TimeRecords.update_one(
        {"_id": record_id},
        {"$set": {"integrity_hash": new_hash}}
    )



def prepare_change_request_response(cr: dict) -> dict:
    """
    Prepara un documento de MongoDB de change request para ChangeRequestResponse.
    Convierte todos los datetimes a UTC aware.
    """
    data = convert_id(cr)

    # Asegurar que todos los datetimes sean UTC aware
    data["original_timestamp"] = ensure_utc_aware(data.get("original_timestamp"))
    data["new_timestamp"] = ensure_utc_aware(data.get("new_timestamp"))
    data["original_created_at"] = ensure_utc_aware(data.get("original_created_at"))
    data["created_at"] = ensure_utc_aware(data.get("created_at"))
    data["updated_at"] = ensure_utc_aware(data.get("updated_at"))
    data["reviewed_at"] = ensure_utc_aware(data.get("reviewed_at"))

    return data


# Localized display label for the record type in worker notifications.
_RECORD_TYPE_LABELS = {
    "entry": {"es": "Entrada", "en": "Clock-in", "ca": "Entrada"},
    "exit": {"es": "Salida", "en": "Clock-out", "ca": "Sortida"},
}


def _record_type_display(original_type: Optional[str], locale: str) -> str:
    labels = _RECORD_TYPE_LABELS.get(original_type or "", _RECORD_TYPE_LABELS["entry"])
    return labels.get(locale) or labels["es"]


@router.post("/", response_model=ChangeRequestResponse, status_code=status.HTTP_201_CREATED)
async def create_change_request(
    request_data: ChangeRequestCreate,
    current_user: APIUser = Depends(PermissionChecker("create_change_requests"))
):
    """
    Create a new change request. Worker authenticates with email/password.

    Validations:
    1. Authenticate worker with email/password
    2. Verify worker doesn't have another pending request (unique index in MongoDB)
    3. Verify time record exists and belongs to worker
    4. Verify new_datetime is different from original_datetime
    """

    # 1. Authenticate worker
    worker = await db.Workers.find_one({
        "email": request_data.email,
        "deleted_at": None
    })

    if not worker:
        raise_api_error(
            status_code=status.HTTP_404_NOT_FOUND,
            error_code="worker.not_found",
            message="Worker not found or has been deleted",
        )

    # Verify password
    if not verify_password(request_data.password, worker["hashed_password"]):
        raise_api_error(
            status_code=status.HTTP_401_UNAUTHORIZED,
            error_code="worker.invalid_credentials",
            message="Invalid credentials",
        )

    # 2. Get original time record
    try:
        time_record_id = ObjectId(request_data.time_record_id)
    except Exception:
        raise_api_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_code="change_request.invalid_time_record_id",
            message="Invalid time record ID format",
        )

    time_record = await db.TimeRecords.find_one({
        "_id": time_record_id,
        "worker_id": str(worker["_id"]),
        "company_id": request_data.company_id
    })

    if not time_record:
        raise_api_error(
            status_code=status.HTTP_404_NOT_FOUND,
            error_code="change_request.time_record_not_found",
            message="Time record not found or does not belong to this worker",
        )

    # 3. Get original timestamp from record
    record_type = time_record.get("type")
    original_timestamp = ensure_utc_aware(time_record.get("timestamp"))

    if not original_timestamp:
        raise_api_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_code="change_request.time_record_invalid",
            message="Time record has no valid timestamp",
        )

    # 4. Verify new timestamp is different
    if request_data.new_timestamp == original_timestamp:
        raise_api_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_code="change_request.same_timestamp",
            message="New timestamp must be different from original",
        )

    # 5. Create change request document
    current_time = datetime.now(dt_timezone.utc)
    worker_name = f"{worker['first_name']} {worker['last_name']}"

    # Convert date to datetime (start of day)
    date_as_datetime = datetime.combine(request_data.date, datetime.min.time())

    change_request_doc = {
        "worker_id": str(worker["_id"]),
        "worker_email": worker["email"],
        "worker_name": worker_name,
        "worker_id_number": worker.get("id_number", ""),

        "date": date_as_datetime,
        "time_record_id": str(time_record_id),
        "original_timestamp": original_timestamp,
        "original_created_at": ensure_utc_aware(time_record.get("created_at")),
        "original_type": record_type,
        "company_id": request_data.company_id,
        "company_name": time_record.get("company_name", ""),

        "new_timestamp": request_data.new_timestamp,
        "reason": request_data.reason,

        "status": ChangeRequestStatus.PENDING.value,
        "created_at": current_time,
        "updated_at": current_time
    }

    # 7. Insert and handle duplicate key error
    try:
        result = await db.ChangeRequests.insert_one(change_request_doc)
        change_request_doc["_id"] = result.inserted_id
        return ChangeRequestResponse(**convert_id(change_request_doc))
    except Exception as e:
        if "duplicate key error" in str(e).lower():
            raise_api_error(
                status_code=status.HTTP_400_BAD_REQUEST,
                error_code="change_request.pending_exists",
                message="You already have a pending change request. Wait for it to be reviewed before creating a new one.",
            )
        raise


@router.post("/pending/check")
async def check_pending_request(
    email: str = Body(...),
    password: str = Body(...),
    current_user: APIUser = Depends(PermissionChecker("create_change_requests"))
):
    """
    Check if a worker has a pending change request.
    Used by webapp to prevent creating multiple pending requests.

    Returns:
    {
        "has_pending": bool,
        "pending_request_id": str (optional)
    }
    """
    # Authenticate worker
    worker = await db.Workers.find_one({
        "email": email,
        "deleted_at": None
    })

    if not worker:
        raise_api_error(
            status_code=status.HTTP_404_NOT_FOUND,
            error_code="worker.not_found",
            message="Worker not found",
        )

    # Verify password
    if not verify_password(password, worker["hashed_password"]):
        raise_api_error(
            status_code=status.HTTP_401_UNAUTHORIZED,
            error_code="worker.invalid_credentials",
            message="Invalid credentials",
        )

    # Check for pending request
    pending = await db.ChangeRequests.find_one({
        "worker_id": str(worker["_id"]),
        "status": ChangeRequestStatus.PENDING.value
    })

    return {
        "has_pending": pending is not None,
        "pending_request_id": str(pending["_id"]) if pending else None
    }


@router.get("/", response_model=List[ChangeRequestResponse])
async def list_change_requests(
    status_filter: Optional[ChangeRequestStatus] = Query(None, alias="status"),
    worker_id: Optional[str] = Query(None),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    current_user: APIUser = Depends(PermissionChecker("view_change_requests"))
):
    """
    List all change requests with optional filters (admin only).
    Returns change requests sorted by created_at descending (most recent first).
    """
    # Build query
    query = {}

    if status_filter:
        query["status"] = status_filter.value

    if worker_id:
        query["worker_id"] = worker_id

    # Date range filter
    if start_date or end_date:
        date_query = {}

        if start_date:
            start_datetime = datetime.combine(start_date, datetime.min.time())
            date_query["$gte"] = start_datetime

        if end_date:
            end_datetime = datetime.combine(end_date, datetime.max.time())
            date_query["$lte"] = end_datetime

        if date_query:
            query["created_at"] = date_query

    # Fetch change requests
    change_requests = []
    async for cr in db.ChangeRequests.find(query).sort("created_at", -1):
        change_requests.append(ChangeRequestResponse(**prepare_change_request_response(cr)))

    return change_requests


@router.get("/{change_request_id}", response_model=ChangeRequestResponse)
async def get_change_request(
    change_request_id: str,
    current_user: APIUser = Depends(PermissionChecker("view_change_requests"))
):
    """
    Get a single change request by ID (admin only).
    Includes validation_errors computed in real-time.
    """
    # Validate ObjectId format
    try:
        cr_obj_id = ObjectId(change_request_id)
    except Exception:
        raise_api_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_code="change_request.invalid_id",
            message="Invalid change request ID format",
        )

    # Find change request
    cr = await db.ChangeRequests.find_one({"_id": cr_obj_id})

    if not cr:
        raise_api_error(
            status_code=status.HTTP_404_NOT_FOUND,
            error_code="change_request.not_found",
            message="Change request not found",
        )

    # Convert to response
    response = ChangeRequestResponse(**prepare_change_request_response(cr))

    # If pending, compute validation errors in real-time
    if cr.get("status") == ChangeRequestStatus.PENDING.value:
        is_valid, errors = await validator.validate_change(
            db,
            cr.get("time_record_id"),
            cr.get("original_timestamp"),
            cr.get("new_timestamp"),
            cr.get("worker_id"),
            cr.get("company_id")
        )
        response.validation_errors = errors if not is_valid else None

    return response


@router.patch("/{change_request_id}", response_model=ChangeRequestResponse)
async def update_change_request(
    change_request_id: str,
    request_data: ChangeRequestUpdate,
    current_user: APIUser = Depends(PermissionChecker("manage_change_requests"))
):
    """
    Approve or reject a change request.

    CRITICAL: Uses atomic update to prevent race conditions.
    Only updates if status is currently "pending".
    """
    # Validate ObjectId format
    try:
        cr_obj_id = ObjectId(change_request_id)
    except Exception:
        raise_api_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_code="change_request.invalid_id",
            message="Invalid change request ID format",
        )

    # 1. Atomic update - only succeeds if status is currently "pending"
    from pymongo import ReturnDocument

    change_request = await db.ChangeRequests.find_one_and_update(
        {
            "_id": cr_obj_id,
            "status": ChangeRequestStatus.PENDING.value  # CRITICAL: only if pending
        },
        {
            "$set": {
                "reviewed_by_admin_id": str(current_user.id),
                "reviewed_by_admin_email": current_user.email,
                "reviewed_at": datetime.now(dt_timezone.utc),
                "admin_internal_notes": request_data.admin_internal_notes,
                "admin_public_comment": request_data.admin_public_comment,
                "status": request_data.status.value
            }
        },
        return_document=ReturnDocument.BEFORE  # Return before update
    )

    if not change_request:
        raise_api_error(
            status_code=status.HTTP_409_CONFLICT,
            error_code="change_request.not_pending",
            message="Change request not found, already processed, or not pending",
        )

    # 2. Verify original time record exists and wasn't modified
    try:
        time_record_id = ObjectId(change_request.get("time_record_id"))
    except Exception:
        raise_api_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_code="change_request.invalid_time_record_id",
            message="Invalid time record ID in change request",
        )

    time_record = await db.TimeRecords.find_one({"_id": time_record_id})

    if not time_record:
        # EDGE CASE: Record was deleted
        await db.ChangeRequests.update_one(
            {"_id": cr_obj_id},
            {"$set": {
                "status": ChangeRequestStatus.REJECTED.value,
                "admin_public_comment": "Original time record was deleted"
            }}
        )
        raise_api_error(
            status_code=status.HTTP_410_GONE,
            error_code="change_request.original_record_deleted",
            message="Original time record was deleted",
        )

    # 3. Process based on status
    if request_data.status == ChangeRequestStatus.ACCEPTED:
        # Validate one more time in real-time
        is_valid, errors = await validator.validate_change(
            db,
            change_request.get("time_record_id"),
            change_request.get("original_timestamp"),
            change_request.get("new_timestamp"),
            change_request.get("worker_id"),
            change_request.get("company_id")
        )

        if not is_valid:
            # Revert to pending
            await db.ChangeRequests.update_one(
                {"_id": cr_obj_id},
                {"$set": {"status": ChangeRequestStatus.PENDING.value}}
            )
            error_msg = "; ".join(errors)
            raise_api_error(
                status_code=status.HTTP_400_BAD_REQUEST,
                error_code="change_request.validation_failed",
                message=f"Cannot approve: {error_msg}",
            )

        # Update original time record - always use 'timestamp' field now
        await db.TimeRecords.update_one(
            {"_id": time_record_id},
            {
                "$set": {
                    "timestamp": change_request.get("new_timestamp"),
                    "modified_by_admin_id": str(current_user.id),
                    "modified_by_admin_email": current_user.email,
                    "modified_at": datetime.now(dt_timezone.utc),
                    "modification_reason": change_request.get("reason"),
                    "original_timestamp": change_request.get("original_timestamp")
                }
            }
        )

        # Recalculate duration_minutes if there's a pair record
        record_type = time_record.get("type")
        if record_type == "entry":
            # Find corresponding EXIT
            exit_record = await db.TimeRecords.find_one({
                "worker_id": change_request.get("worker_id"),
                "company_id": change_request.get("company_id"),
                "type": "exit",
                "created_at": {"$gt": time_record.get("created_at")}
            }, sort=[("created_at", 1)])

            if exit_record:
                # Recalculate duration
                duration = await TimeCalculationService.calculate_duration_with_pauses(
                    change_request.get("worker_id"),
                    change_request.get("company_id"),
                    change_request.get("new_timestamp"),  # New entry time
                    ensure_utc_aware(exit_record.get("timestamp"))
                )
                await db.TimeRecords.update_one(
                    {"_id": exit_record["_id"]},
                    {"$set": {"duration_minutes": duration}}
                )
                # duration_minutes is a hashed field: recompute the paired exit
                # record's integrity_hash so it doesn't read as tampered.
                await _recompute_integrity_hash(exit_record["_id"])

        elif record_type == "exit":
            # Find corresponding ENTRY
            entry_record = await db.TimeRecords.find_one({
                "worker_id": change_request.get("worker_id"),
                "company_id": change_request.get("company_id"),
                "type": "entry",
                "created_at": {"$lt": time_record.get("created_at")}
            }, sort=[("created_at", -1)])

            if entry_record:
                # Recalculate duration
                duration = await TimeCalculationService.calculate_duration_with_pauses(
                    change_request.get("worker_id"),
                    change_request.get("company_id"),
                    ensure_utc_aware(entry_record.get("timestamp")),
                    change_request.get("new_timestamp")  # New exit time
                )
                await db.TimeRecords.update_one(
                    {"_id": time_record_id},
                    {"$set": {"duration_minutes": duration}}
                )

        # The corrected record's own hashed fields (timestamp and, for exit
        # records, duration_minutes) changed above: recompute its hash so
        # this legitimate, audited correction still verifies as untampered.
        await _recompute_integrity_hash(time_record_id)

        # Send acceptance email
        email_service = EmailService()
        worker = await db.Workers.find_one({"_id": ObjectId(change_request.get("worker_id"))})

        if worker:
            try:
                notification_locale = await resolve_company_locale(change_request.get("company_id"))
                record_type_display = _record_type_display(change_request.get("original_type"), notification_locale)

                await email_service.send_change_request_accepted_email(
                    to_email=change_request.get("worker_email"),
                    worker_name=change_request.get("worker_name"),
                    company_name=change_request.get("company_name"),
                    record_type=record_type_display,
                    original_datetime=change_request.get("original_timestamp"),
                    new_datetime=change_request.get("new_timestamp"),
                    reason=change_request.get("reason"),
                    admin_public_comment=request_data.admin_public_comment or "",
                    contact_email=os.getenv("SMTP_FROM_EMAIL", "support@openjornada.es"),
                    locale=notification_locale
                )
            except Exception as e:
                # Log error but don't fail the request
                print(f"Error sending acceptance email: {e}")

    elif request_data.status == ChangeRequestStatus.REJECTED:
        # Send rejection email (always)
        email_service = EmailService()

        # Get worker to send email
        worker = await db.Workers.find_one({"_id": ObjectId(change_request.get("worker_id"))})

        if worker:
            try:
                notification_locale = await resolve_company_locale(change_request.get("company_id"))
                record_type_display = _record_type_display(change_request.get("original_type"), notification_locale)

                await email_service.send_change_request_rejected_email(
                    to_email=change_request.get("worker_email"),
                    worker_name=change_request.get("worker_name"),
                    company_name=change_request.get("company_name"),
                    record_type=record_type_display,
                    original_datetime=change_request.get("original_timestamp"),
                    new_datetime=change_request.get("new_timestamp"),
                    reason=change_request.get("reason"),
                    admin_public_comment=request_data.admin_public_comment or "",
                    contact_email=os.getenv("SMTP_FROM_EMAIL", "support@openjornada.es"),
                    locale=notification_locale
                )
            except Exception as e:
                # Log error but don't fail the request
                print(f"Error sending rejection email: {e}")

    # 4. Retrieve and return updated change request
    updated_cr = await db.ChangeRequests.find_one({"_id": cr_obj_id})
    return ChangeRequestResponse(**prepare_change_request_response(updated_cr))


# ---------------------------------------------------------------------------
# Worker endpoints (email + password authentication)
# ---------------------------------------------------------------------------


@router.post(
    "/worker/history",
    response_model=List[WorkerChangeRequestResponse],
    summary="Historial de solicitudes de cambio del trabajador",
)
async def get_worker_change_request_history(
    request: WorkerChangeRequestsRequest,
) -> List[WorkerChangeRequestResponse]:
    """
    Permite a un trabajador consultar su historial de solicitudes de cambio.

    La autenticación se realiza con email y contraseña (sin JWT). El trabajador
    sólo puede consultar solicitudes de empresas a las que pertenece.

    El campo ``admin_internal_notes`` nunca se incluye en la respuesta.
    """
    # 1. Authenticate worker
    worker = await _authenticate_worker(request.email, request.password)
    worker_id = str(worker["_id"])

    # 2. If company_id provided, verify worker belongs to it
    if request.company_id is not None:
        _verify_worker_company_access(worker, request.company_id)

    # 3. Build query
    query: dict = {"worker_id": worker_id}
    if request.company_id is not None:
        query["company_id"] = request.company_id
    if request.status_filter is not None:
        query["status"] = request.status_filter

    # 4. Fetch, sort desc by created_at, limit
    results: List[WorkerChangeRequestResponse] = []
    async for cr in db.ChangeRequests.find(query).sort("created_at", -1).limit(request.limit):
        data = convert_id(cr)

        # date field is stored as datetime (start-of-day); format as ISO date string
        date_val = data.get("date")
        if isinstance(date_val, datetime):
            date_str = date_val.date().isoformat()
        else:
            date_str = str(date_val) if date_val is not None else ""

        results.append(WorkerChangeRequestResponse(
            id=data["id"],
            date=date_str,
            time_record_id=data.get("time_record_id", ""),
            original_timestamp=_dt_to_iso(data.get("original_timestamp")) or "",
            original_type=data.get("original_type", ""),
            company_id=data.get("company_id", ""),
            company_name=data.get("company_name", ""),
            new_timestamp=_dt_to_iso(data.get("new_timestamp")) or "",
            reason=data.get("reason", ""),
            status=data.get("status", ""),
            created_at=_dt_to_iso(data.get("created_at")) or "",
            updated_at=_dt_to_iso(data.get("updated_at")) or "",
            reviewed_at=_dt_to_iso(data.get("reviewed_at")),
            admin_public_comment=data.get("admin_public_comment"),
        ))

    return results
