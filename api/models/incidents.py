from pydantic import BaseModel, EmailStr, Field
from typing import Optional
from datetime import datetime
from enum import Enum


class IncidentStatus(str, Enum):
    """Incident status enum"""
    PENDING = "pending"
    IN_REVIEW = "in_review"
    RESOLVED = "resolved"


class IncidentBase(BaseModel):
    """Base incident fields"""
    description: str = Field(..., min_length=1, max_length=2000)


class IncidentCreate(BaseModel):
    """Schema for creating an incident (worker authentication required)"""
    email: EmailStr
    password: str
    description: str = Field(..., min_length=1, max_length=2000)


class IncidentUpdate(BaseModel):
    """Schema for updating an incident (admin only)"""
    status: Optional[IncidentStatus] = None
    admin_notes: Optional[str] = Field(None, max_length=2000)


class IncidentInDB(BaseModel):
    """Full incident model as stored in MongoDB"""
    worker_id: str
    worker_email: str
    worker_name: str
    worker_id_number: str
    description: str
    status: IncidentStatus = IncidentStatus.PENDING
    created_at: datetime
    updated_at: datetime
    resolved_at: Optional[datetime] = None
    admin_notes: Optional[str] = None


class IncidentResponse(BaseModel):
    """Incident response model (converts _id to id)"""
    id: str
    worker_id: str
    worker_email: str
    worker_name: str
    worker_id_number: str
    description: str
    status: str
    created_at: datetime
    updated_at: datetime
    resolved_at: Optional[datetime] = None
    admin_notes: Optional[str] = None
