from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List, Literal
from datetime import datetime

from .i18n import SupportedLocale
from .sms import SmsWorkerConfig

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
    # UI language preference; None = inherit the company notification_language
    language: Optional[SupportedLocale] = None

class WorkerUpdateModel(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[EmailStr] = None
    phone_number: Optional[str] = None
    id_number: Optional[str] = None
    password: Optional[str] = None  # Para actualizar la contraseña
    company_ids: Optional[List[str]] = Field(None, min_length=1, description="Lista de IDs de empresas asociadas")
    sms_enabled: Optional[bool] = None
    language: Optional[SupportedLocale] = None

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
    sms_config: Optional[SmsWorkerConfig] = None
    language: Optional[SupportedLocale] = None
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
    language: Optional[SupportedLocale] = None
    # Password reset fields
    reset_token: Optional[str] = None
    reset_token_expires: Optional[datetime] = None
    reset_attempts: List[datetime] = Field(default_factory=list)


class WorkerCompaniesRequest(BaseModel):
    """Request model for getting worker's companies"""
    email: EmailStr
    password: str


class WorkerMeRequest(BaseModel):
    """Request body for a worker to retrieve their own profile."""

    email: EmailStr
    password: str


class WorkerMeResponse(BaseModel):
    """Worker self-profile response — no sensitive fields.

    ``language`` is the worker's own UI preference (``None`` = inherit) and
    ``notification_language`` the first associated company's notification
    language, so the webapp can resolve its locale with the contract chain
    ``language ?? notification_language ?? navegador ?? es``.
    """

    id: str
    first_name: str
    last_name: str
    email: str
    phone_number: str
    default_timezone: str
    company_ids: List[str]
    company_names: List[str]
    language: Optional[SupportedLocale] = None
    notification_language: str = "es"


class WorkerLanguageUpdate(BaseModel):
    """Request body for a worker to set their own UI language.

    Authenticates with email + password (same pattern as the worker
    change-password endpoint). ``language=None`` clears the preference so the
    worker inherits the company notification language again. ``language`` is a
    plain ``str`` on purpose: unsupported codes are rejected by the endpoint
    with a stable ``error_code`` (422).
    """

    email: EmailStr
    password: str
    language: Optional[str] = None


class WorkerLanguageResponse(BaseModel):
    """Effective language info returned after a worker language update."""

    language: Optional[SupportedLocale] = None
    notification_language: str = "es"
    effective_language: str = "es"


# Máximo de filas por lote en la importación masiva (evita requests enormes
# que bloqueen el worker; dividir CSVs mayores en lotes de <= este tamaño).
MAX_BULK_IMPORT_ROWS = 200


class WorkerImportRow(BaseModel):
    """One row of the bulk worker import (CSV parsed client-side).

    `email` is a plain str on purpose: a malformed email must be reported as
    a per-row error instead of rejecting the whole request with a 422.
    """

    first_name: str
    last_name: str
    email: str
    phone_number: Optional[str] = None
    id_number: str
    company_names: List[str] = Field(default_factory=list, description="Nombres de empresas")
    default_timezone: str = "UTC"


class WorkerBulkImportRequest(BaseModel):
    """Request body for POST /workers/bulk-import.

    `rows` is limited to MAX_BULK_IMPORT_ROWS (200) entries per request.
    """

    rows: List[WorkerImportRow] = Field(..., max_length=MAX_BULK_IMPORT_ROWS)
    dry_run: bool = Field(True, description="Si es true, solo valida sin crear nada")
    send_welcome_email: bool = Field(False, description="Enviar email de bienvenida a los creados")


class WorkerImportRowResult(BaseModel):
    """Per-row outcome of a bulk import (row_index is 0-based within `rows`)."""

    row_index: int
    status: Literal["created", "skipped_duplicate", "error"]
    detail: Optional[str] = None
    email: Optional[str] = None


class WorkerBulkImportResponse(BaseModel):
    """Summary + per-row results of a bulk import."""

    total: int
    created: int
    skipped: int
    errors: int
    results: List[WorkerImportRowResult]
