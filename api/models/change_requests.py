from pydantic import BaseModel, EmailStr, Field, AwareDatetime
from typing import Optional, List
from datetime import datetime, date
from enum import Enum


class ChangeRequestStatus(str, Enum):
    """Change request status enum"""
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class ChangeRequestCreate(BaseModel):
    """Schema for creating a change request (worker authentication required)"""
    email: EmailStr
    password: str
    date: date  # Fecha del registro a modificar (YYYY-MM-DD)
    company_id: str
    time_record_id: str  # ID del registro específico a modificar
    new_timestamp: AwareDatetime  # Nueva fecha y hora solicitada (UTC)
    reason: str = Field(..., min_length=10, max_length=1000)  # Motivo del cambio


class ChangeRequestUpdate(BaseModel):
    """Schema for updating a change request (admin approval/rejection)"""
    status: ChangeRequestStatus
    admin_internal_notes: Optional[str] = None
    admin_public_comment: Optional[str] = None


class ChangeRequestInDB(BaseModel):
    """Full change request model as stored in MongoDB"""
    worker_id: str
    worker_email: str
    worker_name: str
    worker_id_number: str

    # Registro original
    date: date  # Fecha del registro (para filtros y queries)
    time_record_id: str
    original_timestamp: AwareDatetime  # Fecha/hora original del registro (UTC)
    original_created_at: AwareDatetime  # Timestamp real del registro original
    original_type: str  # "entry" o "exit"
    company_id: str
    company_name: str

    # Cambios solicitados
    new_timestamp: AwareDatetime  # Nueva fecha/hora solicitada (UTC)
    reason: str

    # Estado y gestión
    status: ChangeRequestStatus = ChangeRequestStatus.PENDING
    created_at: AwareDatetime
    updated_at: AwareDatetime

    # Aprobación/Rechazo
    reviewed_by_admin_id: Optional[str] = None
    reviewed_by_admin_email: Optional[str] = None
    reviewed_at: Optional[AwareDatetime] = None
    admin_internal_notes: Optional[str] = None  # Solo admin
    admin_public_comment: Optional[str] = None  # Se envía en email


class ChangeRequestResponse(BaseModel):
    """Change request response model (converts _id to id)"""
    id: str
    worker_id: str
    worker_email: str
    worker_name: str
    worker_id_number: str

    # Registro original
    date: date
    time_record_id: str
    original_timestamp: AwareDatetime
    original_created_at: AwareDatetime
    original_type: str
    company_id: str
    company_name: str

    # Cambios solicitados
    new_timestamp: AwareDatetime
    reason: str

    # Estado y gestión
    status: str
    created_at: AwareDatetime
    updated_at: AwareDatetime

    # Aprobación/Rechazo
    reviewed_by_admin_id: Optional[str] = None
    reviewed_by_admin_email: Optional[str] = None
    reviewed_at: Optional[AwareDatetime] = None
    admin_public_comment: Optional[str] = None  # No incluir admin_internal_notes en response

    # Validaciones (solo en GET /change-requests/{id})
    validation_errors: Optional[List[str]] = None
