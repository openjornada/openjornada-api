from fastapi import APIRouter, HTTPException, status, Depends, Query
from datetime import datetime, date
from typing import List, Optional
from bson.objectid import ObjectId

from ..models.incidents import (
    IncidentCreate,
    IncidentUpdate,
    IncidentResponse,
    IncidentStatus
)
from ..models.auth import APIUser
from ..database import db, convert_id
from ..auth.auth_handler import verify_password
from ..auth.permissions import PermissionChecker

router = APIRouter()


@router.post("/", response_model=IncidentResponse, status_code=status.HTTP_201_CREATED)
async def create_incident(
    incident_data: IncidentCreate,
    current_user: APIUser = Depends(PermissionChecker("create_time_records"))
):
    """
    Create a new incident. Worker authenticates with email/password.
    Auto-populates worker information and sets status to pending.
    """
    # Validate worker credentials
    worker = await db.Workers.find_one({
        "email": incident_data.email,
        "deleted_at": None
    })

    if not worker:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Worker not found or has been deleted"
        )

    # Verify worker password
    if not verify_password(incident_data.password, worker["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials"
        )

    # Get current UTC time
    current_time = datetime.utcnow()

    # Build worker name
    worker_name = f"{worker['first_name']} {worker['last_name']}"

    # Create incident document
    incident_doc = {
        "worker_id": str(worker["_id"]),
        "worker_email": worker["email"],
        "worker_name": worker_name,
        "worker_id_number": worker["id_number"],
        "description": incident_data.description,
        "status": IncidentStatus.PENDING.value,
        "created_at": current_time,
        "updated_at": current_time,
        "resolved_at": None,
        "admin_notes": None
    }

    # Insert into database
    result = await db.Incidents.insert_one(incident_doc)

    # Retrieve the created incident
    created_incident = await db.Incidents.find_one({"_id": result.inserted_id})

    # Convert and return
    return IncidentResponse(**convert_id(created_incident))


@router.get("/", response_model=List[IncidentResponse])
async def list_incidents(
    status_filter: Optional[IncidentStatus] = Query(None, alias="status", description="Filter by incident status"),
    worker_id: Optional[str] = Query(None, description="Filter by worker ID"),
    start_date: Optional[date] = Query(None, description="Filter incidents created after this date (YYYY-MM-DD)"),
    end_date: Optional[date] = Query(None, description="Filter incidents created before this date (YYYY-MM-DD)"),
    current_user: APIUser = Depends(PermissionChecker("view_incidents"))
):
    """
    List all incidents with optional filters (admin only).
    Returns incidents sorted by created_at descending.
    """
    # Build query
    query = {}

    # Status filter
    if status_filter:
        query["status"] = status_filter.value

    # Worker filter
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

    # Fetch incidents from database
    incidents = []
    async for incident in db.Incidents.find(query).sort("created_at", -1):
        incidents.append(IncidentResponse(**convert_id(incident)))

    return incidents


@router.get("/{incident_id}", response_model=IncidentResponse)
async def get_incident(
    incident_id: str,
    current_user: APIUser = Depends(PermissionChecker("view_incidents"))
):
    """
    Get a single incident by ID (admin only).
    Returns 404 if incident not found.
    """
    # Validate ObjectId format
    try:
        incident_obj_id = ObjectId(incident_id)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid incident ID format"
        )

    # Find incident
    incident = await db.Incidents.find_one({"_id": incident_obj_id})

    if not incident:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Incident not found"
        )

    return IncidentResponse(**convert_id(incident))


@router.patch("/{incident_id}", response_model=IncidentResponse)
async def update_incident(
    incident_id: str,
    update_data: IncidentUpdate,
    current_user: APIUser = Depends(PermissionChecker("manage_incidents"))
):
    """
    Update an incident (admin only).
    Can update status and admin_notes.
    Auto-sets resolved_at when status changes to resolved.
    """
    # Validate ObjectId format
    try:
        incident_obj_id = ObjectId(incident_id)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid incident ID format"
        )

    # Check if incident exists
    existing_incident = await db.Incidents.find_one({"_id": incident_obj_id})

    if not existing_incident:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Incident not found"
        )

    # Build update document
    update_doc = {
        "updated_at": datetime.utcnow()
    }

    # Update status if provided
    if update_data.status is not None:
        update_doc["status"] = update_data.status.value

        # Auto-set resolved_at if status changed to resolved
        if update_data.status == IncidentStatus.RESOLVED:
            if existing_incident.get("status") != IncidentStatus.RESOLVED.value:
                update_doc["resolved_at"] = datetime.utcnow()
        # Clear resolved_at if status changed away from resolved
        elif existing_incident.get("status") == IncidentStatus.RESOLVED.value:
            update_doc["resolved_at"] = None

    # Update admin_notes if provided
    if update_data.admin_notes is not None:
        update_doc["admin_notes"] = update_data.admin_notes

    # Perform update
    await db.Incidents.update_one(
        {"_id": incident_obj_id},
        {"$set": update_doc}
    )

    # Retrieve updated incident
    updated_incident = await db.Incidents.find_one({"_id": incident_obj_id})

    return IncidentResponse(**convert_id(updated_incident))
