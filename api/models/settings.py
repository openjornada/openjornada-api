from pydantic import BaseModel, EmailStr
from typing import Optional


class SettingsBase(BaseModel):
    contact_email: EmailStr


class SettingsUpdate(BaseModel):
    contact_email: Optional[EmailStr] = None


class SettingsInDB(SettingsBase):
    id: str  # MongoDB _id converted to string


class SettingsResponse(SettingsBase):
    id: str
