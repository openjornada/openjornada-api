import os
from fastapi import APIRouter, HTTPException, status, Depends, Query, Body
from datetime import datetime, date, timezone as dt_timezone
from typing import List, Optional
from bson.objectid import ObjectId

from ..models.change_requests import (
    ChangeRequestCreate,
    ChangeRequestUpdate,
    ChangeRequestResponse,
    ChangeRequestStatus
)
from ..models.auth import APIUser
from ..database import db, convert_id
from ..auth.auth_handler import verify_password
from ..auth.permissions import PermissionChecker
from ..services.change_request_validator import ChangeRequestValidator
from ..services.email_service import EmailService
from ..services.time_calculation_service import TimeCalculationService

router = APIRouter()
validator = ChangeRequestValidator()


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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Worker not found or has been deleted"
        )

    # Verify password
    if not verify_password(request_data.password, worker["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials"
        )

    # 2. Get original time record
    try:
        time_record_id = ObjectId(request_data.time_record_id)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid time record ID format"
        )

    time_record = await db.TimeRecords.find_one({
        "_id": time_record_id,
        "worker_id": str(worker["_id"]),
        "company_id": request_data.company_id
    })

    if not time_record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Time record not found or does not belong to this worker"
        )

    # 3. Get original timestamp from record
    record_type = time_record.get("type")
    original_timestamp = ensure_utc_aware(time_record.get("timestamp"))

    if not original_timestamp:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Time record has no valid timestamp"
        )

    # 4. Verify new timestamp is different
    if request_data.new_timestamp == original_timestamp:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New timestamp must be different from original"
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
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You already have a pending change request. Wait for it to be reviewed before creating a new one."
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Worker not found"
        )

    # Verify password
    if not verify_password(password, worker["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials"
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
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid change request ID format"
        )

    # Find change request
    cr = await db.ChangeRequests.find_one({"_id": cr_obj_id})

    if not cr:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Change request not found"
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
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid change request ID format"
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
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Change request not found, already processed, or not pending"
        )

    # 2. Verify original time record exists and wasn't modified
    try:
        time_record_id = ObjectId(change_request.get("time_record_id"))
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid time record ID in change request"
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
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Original time record was deleted"
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
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot approve: {error_msg}"
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

        # Send acceptance email
        email_service = EmailService()
        worker = await db.Workers.find_one({"_id": ObjectId(change_request.get("worker_id"))})

        if worker:
            try:
                record_type_display = "Entrada" if change_request.get("original_type") == "entry" else "Salida"

                await email_service.send_change_request_accepted_email(
                    to_email=change_request.get("worker_email"),
                    worker_name=change_request.get("worker_name"),
                    company_name=change_request.get("company_name"),
                    record_type=record_type_display,
                    original_datetime=change_request.get("original_timestamp"),
                    new_datetime=change_request.get("new_timestamp"),
                    reason=change_request.get("reason"),
                    admin_public_comment=request_data.admin_public_comment or "",
                    contact_email=os.getenv("SMTP_FROM_EMAIL", "support@openjornada.local"),
                    locale="es"
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
                record_type_display = "Entrada" if change_request.get("original_type") == "entry" else "Salida"

                await email_service.send_change_request_rejected_email(
                    to_email=change_request.get("worker_email"),
                    worker_name=change_request.get("worker_name"),
                    company_name=change_request.get("company_name"),
                    record_type=record_type_display,
                    original_datetime=change_request.get("original_timestamp"),
                    new_datetime=change_request.get("new_timestamp"),
                    reason=change_request.get("reason"),
                    admin_public_comment=request_data.admin_public_comment or "",
                    contact_email=os.getenv("SMTP_FROM_EMAIL", "support@openjornada.local"),
                    locale="es"
                )
            except Exception as e:
                # Log error but don't fail the request
                print(f"Error sending rejection email: {e}")

    # 4. Retrieve and return updated change request
    updated_cr = await db.ChangeRequests.find_one({"_id": cr_obj_id})
    return ChangeRequestResponse(**prepare_change_request_response(updated_cr))
