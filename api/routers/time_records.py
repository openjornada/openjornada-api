from fastapi import APIRouter, HTTPException, status, Depends, Query
from datetime import datetime, date, timezone as dt_timezone
from typing import List, Optional
from bson.objectid import ObjectId
import logging
import pytz

from ..models.time_records import (
    TimeRecordModel,
    TimeRecordWorkerCredentials,
    CreateTimeRecordCredentials,
    TimeRecordResponse,
    TimeRecordHistoryResponse,
    WorkerCurrentStatusResponse,
    WorkerHistoryQuery
)
from ..models.auth import APIUser
from ..database import db, convert_id
from ..auth.auth_handler import verify_password
from ..auth.permissions import PermissionChecker
from ..auth.subscription_guard import require_active_subscription
from ..services.time_calculation_service import TimeCalculationService
from .shift_state import transition_shift_state, revert_shift_state

router = APIRouter()
logger = logging.getLogger(__name__)

def ensure_utc_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """
    Convierte un datetime naive (de MongoDB) a UTC aware.
    MongoDB devuelve datetimes naive que se asumen como UTC.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        # Datetime naive - asumir UTC
        return dt.replace(tzinfo=dt_timezone.utc)
    return dt

@router.post("/time-records/", response_model=TimeRecordResponse, status_code=status.HTTP_201_CREATED)
async def create_time_record(
    credentials: CreateTimeRecordCredentials,
    current_user: APIUser = Depends(PermissionChecker("create_time_records")),
    _subscription: None = Depends(require_active_subscription)
):
    # 1. Validate company_id is provided
    if not credentials.company_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El ID de empresa es requerido"
        )

    # 2. Validate company exists and is not deleted
    try:
        company = await db.Companies.find_one({
            "_id": ObjectId(credentials.company_id),
            "deleted_at": None
        })
    except Exception as e:
        logger.error(f"Error validating company {credentials.company_id}: {e}")
        company = None

    if not company:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La empresa seleccionada no existe o ha sido eliminada"
        )

    company_name = company["name"]

    # 3. Find worker by email (exclude deleted workers)
    worker = await db.Workers.find_one({"email": credentials.email, "deleted_at": None})
    if not worker:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Worker not found or has been deleted"
        )

    # 4. Verify worker password
    if not verify_password(credentials.password, worker["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials"
        )

    worker_id = str(worker["_id"])

    # Build worker full name
    worker_name = f"{worker.get('first_name', '')} {worker.get('last_name', '')}".strip()
    if not worker_name:
        worker_name = "Unknown Worker"

    # 5. CRITICAL: Verify worker has permission for this company
    worker_company_ids = [str(cid) for cid in worker.get("company_ids", [])]
    if credentials.company_id not in worker_company_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No tienes permisos para registrar tiempo en esta empresa"
        )

    # 6. Get current time in UTC. Captured BEFORE the CAS so the state doc and
    #    the TimeRecord share the same instant.
    current_time_utc = datetime.now(dt_timezone.utc)

    # PASO A — action is validated by Pydantic (Literal field); always a known value here.
    action = credentials.action

    # PASO B — business validations PRE-CAS (no state or TimeRecords touched)
    pause_info = None
    if action == "pause_start":
        if not credentials.pause_type_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Debes seleccionar un tipo de pausa"
            )
        try:
            pause_type = await db.PauseTypes.find_one({
                "_id": ObjectId(credentials.pause_type_id),
                "company_ids": credentials.company_id,
                "deleted_at": None
            })
        except Exception:
            pause_type = None

        if not pause_type:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Tipo de pausa no válido para esta empresa"
            )
        pause_info = {
            "pause_type_id":        credentials.pause_type_id,
            "pause_type_name":      pause_type["name"],
            "pause_counts_as_work": pause_type["type"] == "inside_shift",
        }

    # PASO C — ATOMIC GUARD. Writes entry_time / open_pause; returns pre-CAS image.
    version, prev = await transition_shift_state(
        worker_id, credentials.company_id, action, current_time_utc, pause_info
    )

    try:
        # PASO D — build TimeRecord WITHOUT reading the log (data comes from `prev`)
        if action == "entry":
            new_record = TimeRecordModel(
                worker_id=worker_id,
                worker_name=worker_name,
                timestamp=current_time_utc,
                type="entry",
                recorded_by=current_user.username,
                company_id=credentials.company_id,
                company_name=company_name,
            )

        elif action == "pause_start":
            new_record = TimeRecordModel(
                worker_id=worker_id,
                worker_name=worker_name,
                timestamp=current_time_utc,
                type="pause_start",
                recorded_by=current_user.username,
                company_id=credentials.company_id,
                company_name=company_name,
                pause_type_id=pause_info["pause_type_id"],
                pause_type_name=pause_info["pause_type_name"],
                pause_counts_as_work=pause_info["pause_counts_as_work"],
            )

        elif action == "pause_end":
            op = prev.get("open_pause")
            if not op:
                # State doc was inconsistent — revert and surface 500
                await revert_shift_state(worker_id, credentials.company_id, prev, version)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Estado de pausa inconsistente"
                )
            pause_start_time = ensure_utc_aware(op["pause_start_time"])
            pause_duration_minutes = (current_time_utc - pause_start_time).total_seconds() / 60
            new_record = TimeRecordModel(
                worker_id=worker_id,
                worker_name=worker_name,
                timestamp=current_time_utc,
                type="pause_end",
                recorded_by=current_user.username,
                company_id=credentials.company_id,
                company_name=company_name,
                pause_type_id=op["pause_type_id"],
                pause_type_name=op["pause_type_name"],
                pause_counts_as_work=op["pause_counts_as_work"],
                duration_minutes=pause_duration_minutes,
            )
            logger.info(
                f"Pause ended: worker={worker_name}, pause_type={op.get('pause_type_name')}, "
                f"duration={pause_duration_minutes:.2f} minutes"
            )

        elif action == "exit":
            entry_time_raw = prev.get("entry_time")
            if not entry_time_raw:
                # State doc was inconsistent — revert and surface 500
                await revert_shift_state(worker_id, credentials.company_id, prev, version)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="No se encontró la entrada del turno"
                )
            entry_time = ensure_utc_aware(entry_time_raw)
            # calculate_duration_with_pauses reads only CLOSED pauses for this shift.
            # Those are immutable at this point (state is already logged_out), so
            # this read is safe — no race with concurrent clock-ins.
            duration_minutes = await TimeCalculationService.calculate_duration_with_pauses(
                worker_id=worker_id,
                company_id=credentials.company_id,
                entry_time=entry_time,
                exit_time=current_time_utc,
            )
            new_record = TimeRecordModel(
                worker_id=worker_id,
                worker_name=worker_name,
                timestamp=current_time_utc,
                type="exit",
                recorded_by=current_user.username,
                company_id=credentials.company_id,
                company_name=company_name,
                duration_minutes=duration_minutes,
            )

        # PASO E — insert the TimeRecord (source of truth)
        record_data = new_record.model_dump()
        record_data["created_at"] = current_time_utc
        result = await db.TimeRecords.insert_one(record_data)

    except HTTPException:
        raise
    except Exception as insert_exc:
        # PASO F — compensate with fencing token
        # NOTE: si insert_one commiteó en el servidor pero el cliente vio timeout, el revert deja un TimeRecord huérfano. Riesgo inherente al patrón sin transacciones (MongoDB standalone).
        try:
            await revert_shift_state(worker_id, credentials.company_id, prev, version)
        except Exception as revert_exc:
            logger.error(
                f"CRITICAL: insert falló Y la reversión de estado falló. "
                f"worker={worker_id} company={credentials.company_id} action={action} "
                f"insert_error={insert_exc!r} revert_error={revert_exc!r}"
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error interno al registrar el fichaje. Inténtelo de nuevo."
        )

    # PASO G — response (same structure as existing code)
    created_record = await db.TimeRecords.find_one({"_id": result.inserted_id})
    record_data_response = {**convert_id(created_record)}
    record_data_response["record_type"] = action
    record_data_response["timestamp"] = ensure_utc_aware(created_record.get("timestamp"))
    return TimeRecordResponse(**record_data_response)

@router.get("/time-records/{worker_id}/latest", response_model=TimeRecordResponse)
async def get_latest_time_record(
    worker_id: str, 
    current_user: APIUser = Depends(PermissionChecker("view_worker_time_records"))
):
    # Check if worker exists
    try:
        worker = await db.Workers.find_one({"_id": ObjectId(worker_id)})
    except Exception:
        worker = None
        
    if not worker:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Worker not found"
        )
    
    # Get the latest record for this worker
    latest_record = await db.TimeRecords.find_one(
        {"worker_id": worker_id},
        sort=[("created_at", -1)]
    )
    
    if not latest_record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No time records found for this worker"
        )
    
    # Preparar datos con datetimes convertidos
    record_data = {**convert_id(latest_record)}
    record_data["record_type"] = latest_record["type"]
    record_data["timestamp"] = ensure_utc_aware(latest_record.get("timestamp"))

    return TimeRecordResponse(**record_data)

@router.get("/time-records/", response_model=List[TimeRecordHistoryResponse])
async def get_all_time_records(
    start_date: Optional[date] = Query(None, description="Start date filter (YYYY-MM-DD)"),
    end_date: Optional[date] = Query(None, description="End date filter (YYYY-MM-DD)"),
    company_id: Optional[str] = Query(None, description="Filter by company ID"),
    worker_name: Optional[str] = Query(None, description="Filter by worker name (case-insensitive partial match)"),
    timezone: Optional[str] = Query("UTC", description="Timezone for displaying records"),
    current_user: APIUser = Depends(PermissionChecker("view_all_time_records"))
):
    """Get time records for all workers with optional date filtering, company filtering, worker name filtering and timezone conversion"""
    query = {}

    # Company filtering
    if company_id:
        query["company_id"] = company_id

    # Worker name filtering (case-insensitive partial match)
    if worker_name:
        query["worker_name"] = {"$regex": worker_name, "$options": "i"}

    # Date filtering considering timezone
    if start_date or end_date:
        try:
            tz = pytz.timezone(timezone)
        except Exception:
            tz = pytz.UTC

        date_query = {}

        if start_date:
            # Convert start date to UTC considering timezone
            start_local = tz.localize(datetime.combine(start_date, datetime.min.time()))
            start_utc = start_local.astimezone(pytz.UTC)
            date_query["$gte"] = start_utc

        if end_date:
            # Convert end date to UTC considering timezone
            end_local = tz.localize(datetime.combine(end_date, datetime.max.time()))
            end_utc = end_local.astimezone(pytz.UTC)
            date_query["$lte"] = end_utc

        if date_query:
            query["timestamp"] = date_query

    # Get all time records with applied filters
    time_records = []

    async for record in db.TimeRecords.find(query).sort("created_at", -1):
        # Try to get worker_name from record first (new records have it)
        worker_name = record.get("worker_name")

        # If worker_name doesn't exist (old records), do lookup
        if not worker_name:
            try:
                worker = await db.Workers.find_one({"_id": ObjectId(record["worker_id"])})
            except Exception:
                worker = None

            if worker:
                worker_name = f"{worker['first_name']} {worker['last_name']}"
                worker_id_number = worker.get("id_number", "Missing ID")
            else:
                worker_name = "Unknown Worker"
                worker_id_number = "Unknown ID"
        else:
            # Get id_number from worker (still need lookup for this)
            try:
                worker = await db.Workers.find_one({"_id": ObjectId(record["worker_id"])})
                worker_id_number = worker.get("id_number", "Missing ID") if worker else "Unknown ID"
            except Exception:
                worker_id_number = "Unknown ID"

        # Prepare record data with all required fields
        record_data = convert_id(record)
        record_data["record_type"] = record["type"]
        record_data["worker_name"] = worker_name
        record_data["worker_id_number"] = worker_id_number
        record_data["timestamp"] = ensure_utc_aware(record.get("timestamp"))

        # Create history response
        time_record = TimeRecordHistoryResponse(**record_data)
        time_records.append(time_record)

    return time_records

@router.get("/time-records/worker/{worker_id}", response_model=List[TimeRecordHistoryResponse])
async def get_worker_time_records(
    worker_id: str,
    start_date: Optional[date] = Query(None, description="Start date filter (YYYY-MM-DD)"),
    end_date: Optional[date] = Query(None, description="End date filter (YYYY-MM-DD)"),
    current_user: APIUser = Depends(PermissionChecker("view_worker_time_records"))
):
    """Get time records for a specific worker with optional date filtering"""
    # Check if worker exists
    try:
        worker = await db.Workers.find_one({"_id": ObjectId(worker_id)})
    except Exception:
        worker = None
        
    if not worker:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Worker not found"
        )
    
    # Set the base query to filter by worker
    query = {"worker_id": worker_id}
    
    # Add date filtering if provided
    if start_date or end_date:
        date_query = {}
        if start_date:
            start_datetime = datetime.combine(start_date, datetime.min.time())
            date_query["$gte"] = start_datetime
        
        if end_date:
            end_datetime = datetime.combine(end_date, datetime.max.time())
            date_query["$lte"] = end_datetime
        
        if date_query:
            query["timestamp"] = date_query

    # Get all time records for this worker with applied filters
    time_records = []
    worker_id_number = worker.get("id_number", "Missing ID")

    async for record in db.TimeRecords.find(query).sort("created_at", -1):
        # Use worker_name from record if available (new records), otherwise from worker lookup
        worker_name = record.get("worker_name") or f"{worker['first_name']} {worker['last_name']}"

        # Prepare record data with all required fields
        record_data = convert_id(record)
        record_data["record_type"] = record["type"]
        record_data["worker_name"] = worker_name
        record_data["worker_id_number"] = worker_id_number
        record_data["timestamp"] = ensure_utc_aware(record.get("timestamp"))

        time_record = TimeRecordHistoryResponse(**record_data)
        time_records.append(time_record)

    return time_records


@router.post("/time-records/current-status", response_model=WorkerCurrentStatusResponse)
async def get_current_status(
    credentials: TimeRecordWorkerCredentials,
    _subscription: None = Depends(require_active_subscription)
):
    """
    Obtener estado actual del trabajador en una empresa.

    Devuelve el estado actual del trabajador:
    - logged_out: No tiene jornada activa
    - logged_in: En jornada, sin pausa
    - on_pause: Actualmente en pausa

    Endpoint público - autenticación con email/password del trabajador.
    """
    # 1. Authenticate worker
    worker = await db.Workers.find_one({
        "email": credentials.email,
        "deleted_at": None
    })

    if not worker:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials"
        )

    # Verify password
    if not verify_password(credentials.password, worker["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials"
        )

    # 2. Verify that worker has access to this company
    if credentials.company_id not in worker.get("company_ids", []):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Worker does not have access to this company"
        )

    # 3. Get company details
    try:
        company = await db.Companies.find_one({"_id": ObjectId(credentials.company_id)})
    except Exception:
        company = None

    if not company:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company not found"
        )

    worker_id = str(worker["_id"])
    worker_name = f"{worker['first_name']} {worker['last_name']}"
    company_name = company["name"]

    # 4. Get last record for this worker in this company
    last_record = await db.TimeRecords.find_one(
        {
            "worker_id": worker_id,
            "company_id": credentials.company_id
        },
        sort=[("created_at", -1)]
    )

    # 5. Determine status
    if not last_record or last_record["type"] == "exit":
        # No record or last was exit -> logged_out
        return WorkerCurrentStatusResponse(
            worker_id=worker_id,
            worker_name=worker_name,
            company_id=credentials.company_id,
            company_name=company_name,
            status="logged_out"
        )

    elif last_record["type"] == "entry":
        # Last record was entry -> logged_in
        entry_time = ensure_utc_aware(last_record.get("timestamp"))

        # Calculate time worked so far (considering any closed pauses)
        now = datetime.now(dt_timezone.utc)
        time_worked = await TimeCalculationService.calculate_duration_with_pauses(
            worker_id=worker_id,
            company_id=credentials.company_id,
            entry_time=entry_time,
            exit_time=now
        )

        return WorkerCurrentStatusResponse(
            worker_id=worker_id,
            worker_name=worker_name,
            company_id=credentials.company_id,
            company_name=company_name,
            status="logged_in",
            entry_time=entry_time,
            time_worked_minutes=time_worked
        )

    elif last_record["type"] == "pause_start":
        # Last record was pause_start -> on_pause
        # Find the original entry for this shift
        entry_record = await db.TimeRecords.find_one(
            {
                "worker_id": worker_id,
                "company_id": credentials.company_id,
                "type": "entry",
                "created_at": {"$lt": last_record["created_at"]}
            },
            sort=[("created_at", -1)]
        )

        if not entry_record:
            # This shouldn't happen, but handle gracefully
            logger.error(f"No entry found for worker {worker_id} with open pause")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Data inconsistency: pause without entry"
            )

        entry_time = ensure_utc_aware(entry_record.get("timestamp"))
        pause_started_at = ensure_utc_aware(last_record.get("timestamp"))

        # Calculate time worked before the pause started
        time_worked = await TimeCalculationService.calculate_duration_with_pauses(
            worker_id=worker_id,
            company_id=credentials.company_id,
            entry_time=entry_time,
            exit_time=pause_started_at
        )

        # Calculate pause duration
        now = datetime.now(dt_timezone.utc)
        pause_duration = (now - ensure_utc_aware(pause_started_at)).total_seconds() / 60

        return WorkerCurrentStatusResponse(
            worker_id=worker_id,
            worker_name=worker_name,
            company_id=credentials.company_id,
            company_name=company_name,
            status="on_pause",
            entry_time=entry_time,
            time_worked_minutes=time_worked,
            pause_type_id=last_record.get("pause_type_id"),
            pause_type_name=last_record.get("pause_type_name"),
            pause_counts_as_work=last_record.get("pause_counts_as_work"),
            pause_started_at=pause_started_at,
            pause_duration_minutes=pause_duration
        )

    elif last_record["type"] == "pause_end":
        # Last record was pause_end -> logged_in (resumed work)
        # Find the original entry for this shift
        entry_record = await db.TimeRecords.find_one(
            {
                "worker_id": worker_id,
                "company_id": credentials.company_id,
                "type": "entry",
                "created_at": {"$lt": last_record["created_at"]}
            },
            sort=[("created_at", -1)]
        )

        if not entry_record:
            logger.error(f"No entry found for worker {worker_id} after pause_end")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Data inconsistency: pause_end without entry"
            )

        entry_time = ensure_utc_aware(entry_record.get("timestamp"))

        # Calculate time worked so far (from entry to now, excluding closed pauses)
        now = datetime.now(dt_timezone.utc)
        time_worked = await TimeCalculationService.calculate_duration_with_pauses(
            worker_id=worker_id,
            company_id=credentials.company_id,
            entry_time=entry_time,
            exit_time=now
        )

        return WorkerCurrentStatusResponse(
            worker_id=worker_id,
            worker_name=worker_name,
            company_id=credentials.company_id,
            company_name=company_name,
            status="logged_in",
            entry_time=entry_time,
            time_worked_minutes=time_worked
        )

    # Should never reach here
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Unknown record type"
    )


@router.post("/time-records/worker/history", response_model=List[TimeRecordResponse])
async def get_worker_day_records(
    query: WorkerHistoryQuery,
    _subscription: None = Depends(require_active_subscription)
):
    """
    Get time records for authenticated worker within a date range.

    IMPORTANT: Workers can ONLY access their own records.
    Endpoint is public but requires email/password authentication.

    Used by webapp to display day records for change request selection.

    Request body:
    {
        "email": "worker@example.com",
        "password": "password",
        "company_id": "company_id",
        "start_date": "2025-12-03",
        "end_date": "2025-12-03",
        "timezone": "UTC" (optional)
    }
    """
    # 1. Authenticate worker
    worker = await db.Workers.find_one({
        "email": query.email,
        "deleted_at": None
    })

    if not worker:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials"
        )

    # 2. Verify password
    if not verify_password(query.password, worker["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials"
        )

    # 3. Verify worker has access to this company
    worker_company_ids = [str(cid) for cid in worker.get("company_ids", [])]
    if query.company_id not in worker_company_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Worker does not have access to this company"
        )

    # 4. Build date range query
    worker_id = str(worker["_id"])
    start_datetime = datetime.combine(query.start_date, datetime.min.time())
    end_datetime = datetime.combine(query.end_date, datetime.max.time())

    # 5. Query records ONLY for this worker in this company within date range
    mongo_query = {
        "worker_id": worker_id,
        "company_id": query.company_id,
        "timestamp": {
            "$gte": start_datetime,
            "$lte": end_datetime
        }
    }

    # 6. Fetch and return records
    time_records = []
    async for record in db.TimeRecords.find(mongo_query).sort("created_at", -1):
        record_data = convert_id(record)
        record_data["record_type"] = record.get("type")
        record_data["timestamp"] = ensure_utc_aware(record.get("timestamp"))

        time_record = TimeRecordResponse(**record_data)
        time_records.append(time_record)

    return time_records
