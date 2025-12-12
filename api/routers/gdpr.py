"""
GDPR Router - Endpoints for GDPR compliance (ARCO rights)

Implements:
- Right of access: Export all worker data
- Right to portability: Export in machine-readable format
- Right to erasure: Anonymize worker data (respecting legal retention periods)
"""

from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from bson import ObjectId

from ..auth.permissions import PermissionChecker
from ..database import db


router = APIRouter(prefix="/api/gdpr", tags=["GDPR"])


# Response Models
class WorkerGDPRData(BaseModel):
    id: str
    first_name: str
    last_name: str
    email: str
    phone_number: Optional[str] = None
    id_number: str
    created_at: str
    companies: List[str]


class TimeRecordGDPR(BaseModel):
    id: str
    type: str
    timestamp: str
    company_name: str
    duration_minutes: Optional[float] = None
    pause_type_name: Optional[str] = None


class IncidentGDPR(BaseModel):
    id: str
    description: str
    status: str
    created_at: str
    resolved_at: Optional[str] = None


class ChangeRequestGDPR(BaseModel):
    id: str
    date: str
    original_timestamp: str
    new_timestamp: str
    reason: str
    status: str
    created_at: str


class GDPRExportResponse(BaseModel):
    export_date: str
    worker: WorkerGDPRData
    time_records: List[TimeRecordGDPR]
    incidents: List[IncidentGDPR]
    change_requests: List[ChangeRequestGDPR]


class AnonymizeRequest(BaseModel):
    reason: str


class AnonymizeResponse(BaseModel):
    message: str
    anonymized_at: str
    reason: str


@router.get(
    "/worker/{worker_id}/export",
    response_model=GDPRExportResponse,
    summary="Export worker GDPR data",
    description="Export all personal data for a worker (Right of access and portability)"
)
async def export_worker_data(
    worker_id: str,
    _=Depends(PermissionChecker("manage_workers"))
):
    """
    Export all data related to a worker for GDPR compliance.

    This implements:
    - Right of access (Art. 15 GDPR)
    - Right to data portability (Art. 20 GDPR)
    """
    # Get worker
    worker = await db.Workers.find_one({"_id": ObjectId(worker_id)})
    if not worker:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Trabajador no encontrado"
        )

    # Get company names
    company_names = []
    if worker.get("company_ids"):
        companies = await db.Companies.find({
            "_id": {"$in": [ObjectId(cid) for cid in worker["company_ids"]]}
        }).to_list(length=100)
        company_names = [c["name"] for c in companies]

    # Get time records
    time_records_cursor = db.TimeRecords.find({"worker_id": worker_id})
    time_records = await time_records_cursor.to_list(length=10000)

    time_records_data = []
    for tr in time_records:
        time_records_data.append(TimeRecordGDPR(
            id=str(tr["_id"]),
            type=tr.get("type", "unknown"),
            timestamp=tr.get("timestamp", tr.get("created_at", "")).isoformat() if isinstance(tr.get("timestamp", tr.get("created_at")), datetime) else str(tr.get("timestamp", tr.get("created_at", ""))),
            company_name=tr.get("company_name", ""),
            duration_minutes=tr.get("duration_minutes"),
            pause_type_name=tr.get("pause_type_name")
        ))

    # Get incidents
    incidents_cursor = db.Incidents.find({"worker_id": worker_id})
    incidents = await incidents_cursor.to_list(length=1000)

    incidents_data = []
    for inc in incidents:
        incidents_data.append(IncidentGDPR(
            id=str(inc["_id"]),
            description=inc.get("description", ""),
            status=inc.get("status", ""),
            created_at=inc.get("created_at", "").isoformat() if isinstance(inc.get("created_at"), datetime) else str(inc.get("created_at", "")),
            resolved_at=inc.get("resolved_at", "").isoformat() if isinstance(inc.get("resolved_at"), datetime) else str(inc.get("resolved_at")) if inc.get("resolved_at") else None
        ))

    # Get change requests
    change_requests_cursor = db.ChangeRequests.find({"worker_id": worker_id})
    change_requests = await change_requests_cursor.to_list(length=1000)

    change_requests_data = []
    for cr in change_requests:
        change_requests_data.append(ChangeRequestGDPR(
            id=str(cr["_id"]),
            date=cr.get("date", ""),
            original_timestamp=cr.get("original_datetime", cr.get("original_timestamp", "")).isoformat() if isinstance(cr.get("original_datetime", cr.get("original_timestamp")), datetime) else str(cr.get("original_datetime", cr.get("original_timestamp", ""))),
            new_timestamp=cr.get("new_datetime", cr.get("new_timestamp", "")).isoformat() if isinstance(cr.get("new_datetime", cr.get("new_timestamp")), datetime) else str(cr.get("new_datetime", cr.get("new_timestamp", ""))),
            reason=cr.get("reason", ""),
            status=cr.get("status", ""),
            created_at=cr.get("created_at", "").isoformat() if isinstance(cr.get("created_at"), datetime) else str(cr.get("created_at", ""))
        ))

    # Build response
    worker_data = WorkerGDPRData(
        id=str(worker["_id"]),
        first_name=worker.get("first_name", ""),
        last_name=worker.get("last_name", ""),
        email=worker.get("email", ""),
        phone_number=worker.get("phone_number"),
        id_number=worker.get("id_number", ""),
        created_at=worker.get("created_at", "").isoformat() if isinstance(worker.get("created_at"), datetime) else str(worker.get("created_at", "")),
        companies=company_names
    )

    return GDPRExportResponse(
        export_date=datetime.now(timezone.utc).isoformat(),
        worker=worker_data,
        time_records=time_records_data,
        incidents=incidents_data,
        change_requests=change_requests_data
    )


@router.post(
    "/worker/{worker_id}/anonymize",
    response_model=AnonymizeResponse,
    summary="Anonymize worker data",
    description="Anonymize personal data while preserving time records for legal compliance"
)
async def anonymize_worker_data(
    worker_id: str,
    request: AnonymizeRequest,
    _=Depends(PermissionChecker("manage_workers"))
):
    """
    Anonymize a worker's personal data for GDPR compliance.

    This implements the Right to erasure (Art. 17 GDPR) while respecting
    the legal obligation to retain time records for 4 years (Art. 34.9 ET).

    What gets anonymized:
    - Worker name → "Usuario Anonimizado"
    - Worker email → anonymized-{id}@deleted.local
    - Worker phone → NULL
    - Worker ID number → "XXXXXXXX"

    What is preserved (anonymized):
    - Time records (worker_name anonymized)
    - Incidents (worker references anonymized)
    - Change requests (worker references anonymized)

    The time record data is preserved because employers are legally required
    to keep this information for 4 years per Spanish labor law.
    """
    if not request.reason or len(request.reason.strip()) < 5:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debe proporcionar un motivo para la anonimización"
        )

    # Get worker
    worker = await db.Workers.find_one({"_id": ObjectId(worker_id)})
    if not worker:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Trabajador no encontrado"
        )

    anonymized_name = "Usuario Anonimizado"
    anonymized_email = f"anonymized-{worker_id}@deleted.local"
    anonymized_id_number = "XXXXXXXX"
    anonymize_timestamp = datetime.now(timezone.utc)

    # Anonymize the worker record
    await db.Workers.update_one(
        {"_id": ObjectId(worker_id)},
        {
            "$set": {
                "first_name": "Usuario",
                "last_name": "Anonimizado",
                "email": anonymized_email,
                "phone_number": None,
                "id_number": anonymized_id_number,
                "anonymized_at": anonymize_timestamp,
                "anonymization_reason": request.reason,
                "password_hash": None,  # Remove password
            }
        }
    )

    # Anonymize time records (keep the data, anonymize the reference)
    await db.TimeRecords.update_many(
        {"worker_id": worker_id},
        {
            "$set": {
                "worker_name": anonymized_name,
                "worker_id_number": anonymized_id_number,
                "anonymized": True
            }
        }
    )

    # Anonymize incidents
    await db.Incidents.update_many(
        {"worker_id": worker_id},
        {
            "$set": {
                "worker_name": anonymized_name,
                "worker_email": anonymized_email,
                "worker_id_number": anonymized_id_number,
                "anonymized": True
            }
        }
    )

    # Anonymize change requests
    await db.ChangeRequests.update_many(
        {"worker_id": worker_id},
        {
            "$set": {
                "worker_name": anonymized_name,
                "worker_email": anonymized_email,
                "worker_id_number": anonymized_id_number,
                "anonymized": True
            }
        }
    )

    return AnonymizeResponse(
        message=f"Datos del trabajador anonimizados correctamente. Los registros de jornada se conservan de forma anónima según la obligación legal de 4 años.",
        anonymized_at=anonymize_timestamp.isoformat(),
        reason=request.reason
    )


@router.get(
    "/worker/{worker_id}/data",
    response_model=WorkerGDPRData,
    summary="Get worker personal data",
    description="Get only personal data for a worker (simplified access)"
)
async def get_worker_personal_data(
    worker_id: str,
    _=Depends(PermissionChecker("manage_workers"))
):
    """
    Get personal data for a worker (simplified version of export).
    """
    worker = await db.Workers.find_one({"_id": ObjectId(worker_id)})
    if not worker:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Trabajador no encontrado"
        )

    # Get company names
    company_names = []
    if worker.get("company_ids"):
        companies = await db.Companies.find({
            "_id": {"$in": [ObjectId(cid) for cid in worker["company_ids"]]}
        }).to_list(length=100)
        company_names = [c["name"] for c in companies]

    return WorkerGDPRData(
        id=str(worker["_id"]),
        first_name=worker.get("first_name", ""),
        last_name=worker.get("last_name", ""),
        email=worker.get("email", ""),
        phone_number=worker.get("phone_number"),
        id_number=worker.get("id_number", ""),
        created_at=worker.get("created_at", "").isoformat() if isinstance(worker.get("created_at"), datetime) else str(worker.get("created_at", "")),
        companies=company_names
    )
