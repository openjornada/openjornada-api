from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


class WorkCenterCreate(BaseModel):
    """Model for creating a new work center"""
    name: str = Field(..., min_length=1, max_length=200)
    code: Optional[str] = Field(None, max_length=50)
    address: Optional[str] = Field(None, max_length=300)
    company_id: str

class WorkCenterUpdate(BaseModel):
    """Model for updating a work center (company_id is immutable)"""
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    code: Optional[str] = Field(None, max_length=50)
    address: Optional[str] = Field(None, max_length=300)

class WorkCenterResponse(BaseModel):
    """Model for work center API responses"""
    id: str
    name: str
    code: Optional[str] = None
    address: Optional[str] = None
    company_id: str
    company_name: str
    created_at: datetime
    updated_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None
    deleted_by: Optional[str] = None