from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime

from .i18n import SupportedLocale
from .sms import SmsCompanyConfig


class CompanyBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    # Notification language for content sent to this company's workers
    # (emails / SMS / push). Invalid codes are rejected with a 422.
    notification_language: SupportedLocale = "es"

class CompanyCreate(CompanyBase):
    """Model for creating a new company"""
    pass

class CompanyUpdate(BaseModel):
    """Model for updating a company"""
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    absence_management_enabled: Optional[bool] = None
    notification_language: Optional[SupportedLocale] = None

class Company(CompanyBase):
    """Company model as stored in MongoDB"""
    created_at: datetime
    updated_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None
    deleted_by: Optional[str] = None
    absence_management_enabled: bool = False

class CompanyResponse(CompanyBase):
    """Model for company API responses"""
    id: str
    created_at: datetime
    updated_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None
    deleted_by: Optional[str] = None
    sms_config: Optional[SmsCompanyConfig] = None
    absence_management_enabled: bool = False
