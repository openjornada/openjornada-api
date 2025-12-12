from pydantic import BaseModel, EmailStr, Field
from typing import Optional, ClassVar, List
from datetime import datetime

class WorkerModel(BaseModel):
    first_name: str
    last_name: str
    email: EmailStr
    phone_number: str
    id_number: str  # DNI del trabajador (obligatorio)
    password: str   # Contraseña (se guardará encriptada)
    default_timezone: str = "UTC"
    created_by: Optional[str] = None
    company_ids: List[str] = Field(..., min_length=1, description="Lista de IDs de empresas asociadas (mínimo 1)")
    send_welcome_email: Optional[bool] = Field(False, description="Enviar email de bienvenida")

class WorkerUpdateModel(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[EmailStr] = None
    phone_number: Optional[str] = None
    id_number: Optional[str] = None
    password: Optional[str] = None  # Para actualizar la contraseña
    company_ids: Optional[List[str]] = Field(None, min_length=1, description="Lista de IDs de empresas asociadas")

class WorkerResponse(BaseModel):
    id: str
    first_name: str
    last_name: str
    email: EmailStr
    phone_number: str
    id_number: str
    created_at: Optional[datetime] = None
    created_by: Optional[str] = None
    deleted_at: Optional[datetime] = None
    deleted_by: Optional[str] = None
    company_ids: List[str] = Field(default_factory=list, description="Lista de IDs de empresas asociadas")
    company_names: List[str] = Field(default_factory=list, description="Nombres de las empresas asociadas")
    # No incluimos la contraseña en la respuesta

class ChangePasswordRequest(BaseModel):
    email: EmailStr
    current_password: str
    new_password: str = Field(min_length=6)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=6)


class WorkerInDB(BaseModel):
    """Worker model as stored in MongoDB, including password reset fields"""
    id: str
    first_name: str
    last_name: str
    email: EmailStr
    phone_number: str
    id_number: str
    hashed_password: str
    default_timezone: str = "UTC"
    created_at: Optional[datetime] = None
    created_by: Optional[str] = None
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None
    deleted_at: Optional[datetime] = None
    deleted_by: Optional[str] = None
    company_ids: List[str] = Field(default_factory=list)
    # Password reset fields
    reset_token: Optional[str] = None
    reset_token_expires: Optional[datetime] = None
    reset_attempts: List[datetime] = Field(default_factory=list)


class WorkerCompaniesRequest(BaseModel):
    """Request model for getting worker's companies"""
    email: EmailStr
    password: str
