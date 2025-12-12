from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime

class CompanyBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)

class CompanyCreate(CompanyBase):
    """Model for creating a new company"""
    pass

class CompanyUpdate(BaseModel):
    """Model for updating a company"""
    name: Optional[str] = Field(None, min_length=1, max_length=200)

class Company(CompanyBase):
    """Company model as stored in MongoDB"""
    created_at: datetime
    updated_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None
    deleted_by: Optional[str] = None

class CompanyResponse(CompanyBase):
    """Model for company API responses"""
    id: str
    created_at: datetime
    updated_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None
    deleted_by: Optional[str] = None
