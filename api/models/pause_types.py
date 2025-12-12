from pydantic import BaseModel, Field, EmailStr
from typing import Optional, List
from datetime import datetime
from bson import ObjectId

class PauseTypeBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="Nombre de la pausa")
    type: str = Field(..., pattern="^(inside_shift|outside_shift)$", description="Tipo de pausa")
    company_ids: List[str] = Field(..., description="IDs de empresas donde aplica")
    description: Optional[str] = Field(None, max_length=500, description="Descripción opcional")

class PauseTypeCreate(PauseTypeBase):
    """Modelo para crear tipo de pausa"""
    pass

class PauseTypeUpdate(BaseModel):
    """Modelo para actualizar tipo de pausa"""
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    # type NO SE PUEDE MODIFICAR si hay registros
    company_ids: Optional[List[str]] = None
    description: Optional[str] = Field(None, max_length=500)

class PauseTypeResponse(BaseModel):
    """Modelo de respuesta con datos enriquecidos"""
    id: str
    name: str
    type: str
    company_ids: List[str]
    company_names: List[str]  # Nombres resueltos
    description: Optional[str]
    created_at: datetime
    created_by: str
    updated_at: Optional[datetime]
    deleted_at: Optional[datetime]
    can_edit_type: bool  # True si no hay registros usando este tipo
    usage_count: int  # Número de registros que usan este tipo

class PauseTypeInDB(PauseTypeBase):
    """Modelo para MongoDB"""
    created_at: datetime
    created_by: str
    updated_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None
    deleted_by: Optional[str] = None

class AvailablePausesRequest(BaseModel):
    """Request para obtener pausas disponibles (workers)"""
    email: EmailStr
    password: str
    company_id: str

class AvailablePauseResponse(BaseModel):
    """Respuesta simplificada para workers"""
    id: str
    name: str
    type: str
    description: Optional[str]
    counts_as_work: bool  # True = inside_shift, False = outside_shift
