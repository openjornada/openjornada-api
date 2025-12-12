from fastapi import APIRouter, HTTPException, status, Depends, Query, Body
from datetime import datetime, date, timezone as dt_timezone
from typing import List, Optional
from bson.objectid import ObjectId
import logging
import pytz

from ..models.time_records import (
    TimeRecordModel,
    TimeRecordWorkerCredentials,
    TimeRecordResponse,
    TimeRecordHistoryResponse,
    WorkerCurrentStatusResponse,
    WorkerHistoryQuery
)
from ..models.auth import APIUser
from ..database import db, convert_id
from ..auth.auth_handler import get_current_active_user, verify_password
from ..auth.permissions import PermissionChecker
from ..services.time_calculation_service import TimeCalculationService

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
    credentials: TimeRecordWorkerCredentials,
    current_user: APIUser = Depends(PermissionChecker("create_time_records"))
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

    # 6. Get last record in this company
    last_record = await db.TimeRecords.find_one(
        {"worker_id": worker_id, "company_id": credentials.company_id},
        sort=[("created_at", -1)]
    )

    # 7. Get current time in UTC
    current_time_utc = datetime.now(dt_timezone.utc)
    timezone = credentials.timezone or "UTC"

    # 8. DETERMINE TYPE OF RECORD BASED ON LAST RECORD
    if not last_record or last_record["type"] == "exit":
        # ========================================
        # CASE 1: ENTRY (first entry of the day or after exit)
        # ========================================
        if credentials.action and credentials.action != "entry":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Debes hacer una entrada antes de registrar pausas o salidas"
            )

        new_record = TimeRecordModel(
            worker_id=worker_id,
            worker_name=worker_name,
            timestamp=current_time_utc,
            type="entry",
            recorded_by=current_user.username,
            company_id=credentials.company_id,
            company_name=company_name
        )

        record_data = new_record.model_dump()
        record_data["created_at"] = current_time_utc

        result = await db.TimeRecords.insert_one(record_data)
        created_record = await db.TimeRecords.find_one({"_id": result.inserted_id})

        record_data_response = {**convert_id(created_record)}
        record_data_response["record_type"] = "entry"
        record_data_response["timestamp"] = ensure_utc_aware(created_record.get("timestamp"))

        return TimeRecordResponse(**record_data_response)

    elif last_record["type"] == "entry":
        # ========================================
        # CASE 2: After ENTRY → can be PAUSE_START or EXIT
        # ========================================

        if credentials.action == "pause_start":
            # PAUSE START
            if not credentials.pause_type_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Debes seleccionar un tipo de pausa"
                )

            # Validate pause type exists and belongs to this company
            try:
                pause_type = await db.PauseTypes.find_one({
                    "_id": ObjectId(credentials.pause_type_id),
                    "company_ids": credentials.company_id,
                    "deleted_at": None
                })
            except:
                pause_type = None

            if not pause_type:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Tipo de pausa no válido para esta empresa"
                )

            new_record = TimeRecordModel(
                worker_id=worker_id,
                worker_name=worker_name,
                timestamp=current_time_utc,
                type="pause_start",
                recorded_by=current_user.username,
                company_id=credentials.company_id,
                company_name=company_name,
                pause_type_id=credentials.pause_type_id,
                pause_type_name=pause_type["name"],
                pause_counts_as_work=(pause_type["type"] == "inside_shift")
            )

            record_data = new_record.model_dump()
            record_data["created_at"] = current_time_utc

            result = await db.TimeRecords.insert_one(record_data)
            created_record = await db.TimeRecords.find_one({"_id": result.inserted_id})

            record_data_response = {**convert_id(created_record)}
            record_data_response["record_type"] = "pause_start"
            record_data_response["timestamp"] = ensure_utc_aware(created_record.get("timestamp"))

            return TimeRecordResponse(**record_data_response)

        else:
            # EXIT (salida) - buscar el ENTRY original
            entry_time = ensure_utc_aware(last_record["timestamp"])

            # Calculate duration considering pauses
            duration_minutes = await TimeCalculationService.calculate_duration_with_pauses(
                worker_id=worker_id,
                company_id=credentials.company_id,
                entry_time=entry_time,
                exit_time=current_time_utc
            )

            new_record = TimeRecordModel(
                worker_id=worker_id,
                worker_name=worker_name,
                timestamp=current_time_utc,
                duration_minutes=duration_minutes,
                type="exit",
                recorded_by=current_user.username,
                company_id=credentials.company_id,
                company_name=company_name
            )

            record_data = new_record.model_dump()
            record_data["created_at"] = current_time_utc

            result = await db.TimeRecords.insert_one(record_data)
            created_record = await db.TimeRecords.find_one({"_id": result.inserted_id})

            record_data_response = {**convert_id(created_record)}
            record_data_response["record_type"] = "exit"
            record_data_response["timestamp"] = ensure_utc_aware(created_record.get("timestamp"))

            return TimeRecordResponse(**record_data_response)

    elif last_record["type"] == "pause_start":
        # ========================================
        # CASE 3: After PAUSE_START → can only be PAUSE_END
        # ========================================

        if credentials.action == "exit":
            # Trying to EXIT with open pause → BLOCK
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Tienes una pausa abierta ({last_record.get('pause_type_name', 'sin nombre')}). Debes finalizarla antes de cerrar la jornada."
            )

        if credentials.action == "pause_start":
            # Trying to nest pauses → BLOCK
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Ya tienes una pausa en curso. Debes finalizarla antes de iniciar otra."
            )

        # PAUSE_END
        # Calculate pause duration
        pause_start_time = ensure_utc_aware(last_record["timestamp"])
        pause_duration_minutes = (current_time_utc - pause_start_time).total_seconds() / 60

        new_record = TimeRecordModel(
            worker_id=worker_id,
            worker_name=worker_name,
            timestamp=current_time_utc,
            type="pause_end",
            recorded_by=current_user.username,
            company_id=credentials.company_id,
            company_name=company_name,
            pause_type_id=last_record.get("pause_type_id"),
            pause_type_name=last_record.get("pause_type_name"),
            pause_counts_as_work=last_record.get("pause_counts_as_work"),
            duration_minutes=pause_duration_minutes
        )

        record_data = new_record.model_dump()
        record_data["created_at"] = current_time_utc

        result = await db.TimeRecords.insert_one(record_data)
        created_record = await db.TimeRecords.find_one({"_id": result.inserted_id})

        logger.info(
            f"Pause ended: worker={worker_name}, pause_type={last_record.get('pause_type_name')}, "
            f"duration={pause_duration_minutes:.2f} minutes"
        )

        record_data_response = {**convert_id(created_record)}
        record_data_response["record_type"] = "pause_end"
        record_data_response["timestamp"] = ensure_utc_aware(created_record.get("timestamp"))

        return TimeRecordResponse(**record_data_response)

    elif last_record["type"] == "pause_end":
        # ========================================
        # CASE 4: After PAUSE_END → can be PAUSE_START or EXIT
        # ========================================

        if credentials.action == "pause_start":
            # New pause (same flow as after entry)
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
            except:
                pause_type = None

            if not pause_type:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Tipo de pausa no válido"
                )

            new_record = TimeRecordModel(
                worker_id=worker_id,
                worker_name=worker_name,
                timestamp=current_time_utc,
                type="pause_start",
                recorded_by=current_user.username,
                company_id=credentials.company_id,
                company_name=company_name,
                pause_type_id=credentials.pause_type_id,
                pause_type_name=pause_type["name"],
                pause_counts_as_work=(pause_type["type"] == "inside_shift")
            )

            record_data = new_record.model_dump()
            record_data["created_at"] = current_time_utc

            result = await db.TimeRecords.insert_one(record_data)
            created_record = await db.TimeRecords.find_one({"_id": result.inserted_id})

            record_data_response = {**convert_id(created_record)}
            record_data_response["record_type"] = "pause_start"
            record_data_response["timestamp"] = ensure_utc_aware(created_record.get("timestamp"))

            return TimeRecordResponse(**record_data_response)

        else:
            # EXIT (find original entry)
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
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="No se encontró registro de entrada"
                )

            entry_time = ensure_utc_aware(entry_record["timestamp"])

            duration_minutes = await TimeCalculationService.calculate_duration_with_pauses(
                worker_id=worker_id,
                company_id=credentials.company_id,
                entry_time=entry_time,
                exit_time=current_time_utc
            )

            new_record = TimeRecordModel(
                worker_id=worker_id,
                worker_name=worker_name,
                timestamp=current_time_utc,
                duration_minutes=duration_minutes,
                type="exit",
                recorded_by=current_user.username,
                company_id=credentials.company_id,
                company_name=company_name
            )

            record_data = new_record.model_dump()
            record_data["created_at"] = current_time_utc

            result = await db.TimeRecords.insert_one(record_data)
            created_record = await db.TimeRecords.find_one({"_id": result.inserted_id})

            record_data_response = {**convert_id(created_record)}
            record_data_response["record_type"] = "exit"
            record_data_response["timestamp"] = ensure_utc_aware(created_record.get("timestamp"))

            return TimeRecordResponse(**record_data_response)

    else:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Estado del registro no reconocido: {last_record['type']}"
        )

@router.get("/time-records/{worker_id}/latest", response_model=TimeRecordResponse)
async def get_latest_time_record(
    worker_id: str, 
    current_user: APIUser = Depends(PermissionChecker("view_worker_time_records"))
):
    # Check if worker exists
    try:
        worker = await db.Workers.find_one({"_id": ObjectId(worker_id)})
    except:
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
        except:
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
            query["created_at"] = date_query

    # Get all time records with applied filters
    time_records = []

    async for record in db.TimeRecords.find(query).sort("created_at", -1):
        # Try to get worker_name from record first (new records have it)
        worker_name = record.get("worker_name")

        # If worker_name doesn't exist (old records), do lookup
        if not worker_name:
            try:
                worker = await db.Workers.find_one({"_id": ObjectId(record["worker_id"])})
            except:
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
            except:
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
    except:
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
            query["created_at"] = date_query
    
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
async def get_current_status(credentials: TimeRecordWorkerCredentials):
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
    except:
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
async def get_worker_day_records(query: WorkerHistoryQuery):
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
        "created_at": {
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
