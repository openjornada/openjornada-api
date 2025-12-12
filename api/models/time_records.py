from pydantic import BaseModel, AwareDatetime, EmailStr
from typing import Optional, ClassVar
from datetime import datetime, date

class TimeRecordModel(BaseModel):
    worker_id: str
    worker_name: str  # Nombre completo del trabajador (first_name + last_name)
    timestamp: AwareDatetime  # UTC aware datetime - momento del registro
    type: str  # "entry" | "exit" | "pause_start" | "pause_end"
    duration_minutes: Optional[float] = None  # Solo para EXIT y PAUSE_END
    recorded_by: str
    company_id: str  # ID de la empresa
    company_name: str  # Nombre de la empresa

    # Campos para pausas
    pause_type_id: Optional[str] = None
    pause_type_name: Optional[str] = None
    pause_counts_as_work: Optional[bool] = None

    # Campos de auditoría (para cambios realizados por admin)
    modified_by_admin_id: Optional[str] = None
    modified_by_admin_email: Optional[str] = None
    modified_at: Optional[AwareDatetime] = None
    modification_reason: Optional[str] = None
    original_timestamp: Optional[AwareDatetime] = None  # Guarda el timestamp original antes del cambio

class TimeRecordWorkerCredentials(BaseModel):
    email: EmailStr
    password: str
    timezone: Optional[str] = "UTC"  # Zona horaria del cliente (para referencia, no se guarda)
    company_id: str  # ID de la empresa para la cual se registra el tiempo

    # Campos para pausas
    action: Optional[str] = None  # "entry" | "exit" | "pause_start" | "pause_end"
    pause_type_id: Optional[str] = None  # Requerido si action = "pause_start"

class WorkerHistoryQuery(BaseModel):
    email: str  # Worker email
    password: str  # Worker password
    company_id: str  # Company ID
    start_date: date  # Start date (YYYY-MM-DD)
    end_date: date  # End date (YYYY-MM-DD)
    timezone: Optional[str] = "UTC"  # Worker timezone

class TimeRecordResponse(BaseModel):
    id: Optional[str] = None
    worker_id: str
    worker_name: Optional[str] = None  # Optional for backward compatibility
    record_type: str
    timestamp: AwareDatetime  # UTC aware datetime - momento del registro
    duration_minutes: Optional[float] = None
    recorded_by: str
    company_id: Optional[str] = None  # Optional for backward compatibility
    company_name: Optional[str] = None  # Optional for backward compatibility

    # Campos para pausas
    pause_type_id: Optional[str] = None
    pause_type_name: Optional[str] = None
    pause_counts_as_work: Optional[bool] = None

class TimeRecordHistoryResponse(TimeRecordResponse):
    worker_name: str  # Required in history (overrides optional from parent)
    worker_id_number: str  # DNI del trabajador

class WorkerCurrentStatusResponse(BaseModel):
    """Estado actual del trabajador en una empresa"""
    worker_id: str
    worker_name: str
    company_id: str
    company_name: str
    status: str  # "logged_out" | "logged_in" | "on_pause"

    # Si logged_in o on_pause
    entry_time: Optional[AwareDatetime] = None  # UTC aware datetime - hora de entrada
    time_worked_minutes: Optional[float] = None  # Tiempo trabajado hasta ahora

    # Si on_pause
    pause_type_id: Optional[str] = None
    pause_type_name: Optional[str] = None
    pause_counts_as_work: Optional[bool] = None
    pause_started_at: Optional[AwareDatetime] = None  # UTC aware datetime
    pause_duration_minutes: Optional[float] = None
