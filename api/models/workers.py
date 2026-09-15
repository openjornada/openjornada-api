from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List, Literal, Dict
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
    work_center_assignments: Dict[str, str] = Field(default_factory=dict, description="Asignaciones de centro por empresa (company_id -> work_center_id)")
    work_center_names: Dict[str, str] = Field(default_factory=dict, description="Nombres de centros por empresa (company_id -> nombre del centro)")
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
    work_center_assignments: Dict[str, str] = Field(default_factory=dict)
    language: Optional[SupportedLocale] = None
    # Password reset fields
    reset_token: Optional[str] = None
    reset_token_expires: Optional[datetime] = None
    reset_attempts: List[datetime] = Field(default_factory=list)


class WorkerCompaniesRequest(BaseModel):
    """Request model for getting worker's companies"""
    email: EmailStr
    password: str


class WorkerWorkCenterAssignment(BaseModel):
    """Request body for assigning a worker to a work center in one company.

    ``work_center_id=None`` clears the assignment for that company.
    """
    company_id: str
    work_center_id: Optional[str] = None


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


class WorkerBulkWorkCenterRequest(BaseModel):
    """Request body for POST /workers/bulk-work-center.

    ``action="assign"`` sets ``work_center_assignments[company_id]`` to
    ``work_center_id`` for every listed worker that belongs to ``company_id``;
    ``action="clear"`` wipes the work-center assignment of every listed
    worker for ``company_id`` if provided, or ALL of its work-center
    assignments if ``company_id`` is omitted (``work_center_id`` is always
    ignored for ``clear``).
    """

    worker_ids: List[str] = Field(
        ..., min_length=1, max_length=MAX_BULK_IMPORT_ROWS, description="IDs de trabajadores a procesar"
    )
    action: Literal["assign", "clear"]
    company_id: Optional[str] = None
    work_center_id: Optional[str] = None


class WorkerBulkWorkCenterResponse(BaseModel):
    """Summary of a bulk work-center assignment/clear.

    ``skipped`` counts workers not applicable (for assign: not in the center's
    company, or not found/deleted; for clear: not found/deleted), including
    invalid worker ids.
    """

    total: int
    updated: int
    skipped: int
    detail: Optional[str] = None
