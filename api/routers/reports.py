"""
Router for labour inspection reports and monthly digital signatures.

Exposes two families of endpoints:

- Admin/Inspector endpoints (JWT authentication via PermissionChecker):
    GET  /reports/monthly                  Company monthly summary
    GET  /reports/monthly/worker/{id}      Single-worker monthly summary
    GET  /reports/overtime                 Overtime report
    GET  /reports/export/monthly           Export monthly report (CSV/XLSX/PDF)
    GET  /reports/export/overtime          Export overtime report as CSV
    GET  /reports/integrity/{record_id}    Verify record integrity (SHA-256)
    GET  /reports/integrity/monthly-signature/{id}  Verify monthly signature digest

- Worker endpoints (email + password authentication, no JWT required):
    POST /reports/worker/monthly           Worker's own monthly report
    POST /reports/worker/monthly/sign      Sign monthly report
    POST /reports/worker/signatures/status Last 12 months signature status
"""

import hashlib
import io
import csv
import logging
from datetime import datetime, timezone as dt_timezone
from typing import Annotated, Optional

import pytz
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pymongo.errors import DuplicateKeyError

from ..auth.permissions import PermissionChecker
from ..database import db
from ..models.auth import APIUser
from ..models.reports import (
    CompanyMonthlySummary,
    ExportFormat,
    IanaTimezone,
    MonthlySignatureRequest,
    MonthlySignatureResponse,
    MonthlySignatureVerification,
    OvertimeReport,
    RecordIntegrity,
    SignatureStatusResponse,
    WorkerExportRequest,
    WorkerMonthlySummary,
    WorkerReportRequest,
)
from ..services.export_service import ExportService
from ..services.integrity_service import MONTHLY_DIGEST_VERSION, IntegrityService
from ..services.report_service import ReportService
from ..utils.timezones import resolve_worker_timezone
from ..utils.worker_auth import _authenticate_worker, _verify_worker_company_access

router = APIRouter()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CONTENT_TYPES: dict[ExportFormat, str] = {
    ExportFormat.CSV: "text/csv",
    ExportFormat.XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ExportFormat.PDF: "application/pdf",
}

_FILE_EXTENSIONS: dict[ExportFormat, str] = {
    ExportFormat.CSV: "csv",
    ExportFormat.XLSX: "xlsx",
    ExportFormat.PDF: "pdf",
}


# ---------------------------------------------------------------------------
# Admin / Inspector endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/reports/monthly",
    response_model=CompanyMonthlySummary,
    summary="Informe mensual de empresa",
)
async def get_company_monthly_report(
    company_id: str = Query(..., description="ID de la empresa"),
    year: int = Query(..., ge=2020, le=2035, description="Año del informe"),
    month: int = Query(..., ge=1, le=12, description="Mes del informe (1-12)"),
    timezone: Annotated[IanaTimezone, Query(description="Zona horaria IANA para agrupar días")] = "Europe/Madrid",
    current_user: APIUser = Depends(PermissionChecker("view_reports")),
) -> CompanyMonthlySummary:
    """
    Genera el resumen mensual de todos los trabajadores activos de una empresa.

    Requiere permiso ``view_reports`` (admin o inspector).
    Los trabajadores sin registros en el mes solicitado quedan excluidos del resultado.
    """
    logger.info(
        "Company monthly report requested: company=%s year=%d month=%d user=%s",
        company_id, year, month, current_user.username,
    )
    return await ReportService().get_company_monthly_summary(
        company_id=company_id,
        year=year,
        month=month,
        timezone=timezone,
    )


@router.get(
    "/reports/monthly/worker/{worker_id}",
    response_model=WorkerMonthlySummary,
    summary="Informe mensual de un trabajador",
)
async def get_worker_monthly_report(
    worker_id: str,
    company_id: str = Query(..., description="ID de la empresa"),
    year: int = Query(..., ge=2020, le=2035, description="Año del informe"),
    month: int = Query(..., ge=1, le=12, description="Mes del informe (1-12)"),
    timezone: Annotated[IanaTimezone, Query(description="Zona horaria IANA para agrupar días")] = "Europe/Madrid",
    current_user: APIUser = Depends(PermissionChecker("view_reports")),
) -> WorkerMonthlySummary:
    """
    Genera el resumen mensual de un trabajador concreto en una empresa.

    Requiere permiso ``view_reports`` (admin o inspector).
    """
    logger.info(
        "Worker monthly report requested: worker=%s company=%s year=%d month=%d user=%s",
        worker_id, company_id, year, month, current_user.username,
    )
    return await ReportService().get_worker_monthly_summary(
        company_id=company_id,
        worker_id=worker_id,
        year=year,
        month=month,
        timezone=timezone,
    )


@router.get(
    "/reports/overtime",
    response_model=OvertimeReport,
    summary="Informe de horas extra",
)
async def get_overtime_report(
    company_id: str = Query(..., description="ID de la empresa"),
    year: int = Query(..., ge=2020, le=2035, description="Año del informe"),
    month: int = Query(..., ge=1, le=12, description="Mes del informe (1-12)"),
    daily_expected_minutes: float = Query(
        480.0,
        ge=0,
        description="Minutos de jornada diaria esperados (por defecto 480 = 8 h)",
    ),
    timezone: Annotated[IanaTimezone, Query(description="Zona horaria IANA")] = "Europe/Madrid",
    current_user: APIUser = Depends(PermissionChecker("view_reports")),
) -> OvertimeReport:
    """
    Genera un informe de horas extra para todos los trabajadores de la empresa.

    Solo se incluyen trabajadores cuya jornada total supera el umbral diario esperado
    multiplicado por sus días trabajados. Requiere permiso ``view_reports``.
    """
    logger.info(
        "Overtime report requested: company=%s year=%d month=%d user=%s",
        company_id, year, month, current_user.username,
    )
    return await ReportService().get_overtime_report(
        company_id=company_id,
        year=year,
        month=month,
        daily_expected_minutes=daily_expected_minutes,
        timezone=timezone,
    )


@router.get(
    "/reports/export/monthly",
    summary="Exportar informe mensual (CSV / XLSX / PDF)",
)
async def export_monthly_report(
    company_id: str = Query(..., description="ID de la empresa"),
    year: int = Query(..., ge=2020, le=2035, description="Año del informe"),
    month: int = Query(..., ge=1, le=12, description="Mes del informe (1-12)"),
    worker_id: Optional[str] = Query(None, description="ID del trabajador (opcional; si se omite, exporta toda la empresa)"),
    format: ExportFormat = Query(ExportFormat.PDF, description="Formato de exportación: csv, xlsx o pdf"),
    timezone: Annotated[IanaTimezone, Query(description="Zona horaria IANA")] = "Europe/Madrid",
    current_user: APIUser = Depends(PermissionChecker("export_reports")),
) -> StreamingResponse:
    """
    Exporta el informe mensual de jornada en el formato solicitado.

    - Si se proporciona ``worker_id``, el informe cubre únicamente ese trabajador.
    - Si se omite, cubre todos los trabajadores activos de la empresa.

    El cuerpo de la respuesta es el fichero binario; los metadatos se transmiten
    en los cabeceros HTTP:

    - ``X-Report-Hash``: SHA-256 del contenido del fichero (para auditoría).
    - ``X-Report-Generated``: Timestamp ISO 8601 de generación.

    Requiere permiso ``export_reports``.
    """
    report_service = ReportService()
    export_service = ExportService()

    if worker_id:
        summary = await report_service.get_worker_monthly_summary(
            company_id=company_id,
            worker_id=worker_id,
            year=year,
            month=month,
            timezone=timezone,
        )
        subject_label = f"trabajador_{worker_id}"
    else:
        summary = await report_service.get_company_monthly_summary(
            company_id=company_id,
            year=year,
            month=month,
            timezone=timezone,
        )
        subject_label = summary.company_name.replace(" ", "_")

    if format == ExportFormat.CSV:
        buf: io.BytesIO = await export_service.export_monthly_csv(summary, timezone=timezone)
    elif format == ExportFormat.XLSX:
        buf = await export_service.export_monthly_xlsx(summary, timezone=timezone)
    else:
        buf = await export_service.export_monthly_pdf(summary, timezone=timezone)

    raw_bytes = buf.getvalue()
    report_hash = IntegrityService.compute_report_hash(raw_bytes)
    buf.seek(0)

    ext = _FILE_EXTENSIONS[format]
    filename = f"informe_{subject_label}_{year}-{month:02d}.{ext}"
    generated_at = datetime.now(dt_timezone.utc).isoformat()

    logger.info(
        "Monthly export generated: file=%s hash=%s user=%s",
        filename, report_hash, current_user.username,
    )

    return StreamingResponse(
        content=buf,
        media_type=_CONTENT_TYPES[format],
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Report-Hash": report_hash,
            "X-Report-Generated": generated_at,
        },
    )


@router.get(
    "/reports/export/overtime",
    summary="Exportar informe de horas extra (CSV)",
)
async def export_overtime_report(
    company_id: str = Query(..., description="ID de la empresa"),
    year: int = Query(..., ge=2020, le=2035, description="Año del informe"),
    month: int = Query(..., ge=1, le=12, description="Mes del informe (1-12)"),
    daily_expected_minutes: float = Query(
        480.0,
        ge=0,
        description="Minutos de jornada diaria esperados (por defecto 480 = 8 h)",
    ),
    timezone: Annotated[IanaTimezone, Query(description="Zona horaria IANA")] = "Europe/Madrid",
    current_user: APIUser = Depends(PermissionChecker("export_reports")),
) -> StreamingResponse:
    """
    Exporta el informe de horas extra en formato CSV.

    El CSV incluye una fila por trabajador con horas totales trabajadas,
    horas esperadas, horas extra y días con jornada extendida.
    Separador de columnas: punto y coma (``;``) para compatibilidad con Excel
    en configuración regional española.

    Requiere permiso ``export_reports``.
    """
    overtime_report = await ReportService().get_overtime_report(
        company_id=company_id,
        year=year,
        month=month,
        daily_expected_minutes=daily_expected_minutes,
        timezone=timezone,
    )

    # Build CSV manually — ExportService only handles monthly summaries
    text_buf = io.StringIO()
    text_buf.write("\ufeff")  # UTF-8 BOM for Excel compatibility

    writer = csv.writer(text_buf, delimiter=";", lineterminator="\r\n")
    writer.writerow([
        "DNI",
        "Nombre",
        "Horas Trabajadas",
        "Horas Esperadas",
        "Horas Extra",
        "Dias con Horas Extra",
    ])

    for w in overtime_report.workers_with_overtime:
        writer.writerow([
            w.worker_id_number,
            w.worker_name,
            f"{w.total_worked_minutes / 60:.2f}",
            f"{w.expected_minutes / 60:.2f}",
            f"{w.overtime_minutes / 60:.2f}",
            str(w.days_with_overtime),
        ])

    raw_bytes = text_buf.getvalue().encode("utf-8")
    report_hash = IntegrityService.compute_report_hash(raw_bytes)

    company_label = overtime_report.company_name.replace(" ", "_")
    filename = f"horas_extra_{company_label}_{year}-{month:02d}.csv"
    generated_at = datetime.now(dt_timezone.utc).isoformat()

    logger.info(
        "Overtime CSV export generated: file=%s hash=%s user=%s",
        filename, report_hash, current_user.username,
    )

    return StreamingResponse(
        content=io.BytesIO(raw_bytes),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Report-Hash": report_hash,
            "X-Report-Generated": generated_at,
        },
    )


@router.get(
    "/reports/integrity/{record_id}",
    response_model=RecordIntegrity,
    summary="Verificar integridad de un registro",
)
async def verify_record_integrity(
    record_id: str,
    current_user: APIUser = Depends(PermissionChecker("view_reports")),
) -> RecordIntegrity:
    """
    Verifica la integridad de un registro de tiempo calculando su hash SHA-256
    y comparándolo con el hash almacenado en la base de datos en el momento de creación.

    Requiere permiso ``view_reports`` (admin o inspector).
    """
    logger.info(
        "Integrity check requested: record=%s user=%s",
        record_id, current_user.username,
    )
    result = await IntegrityService.verify_record_integrity(record_id)

    # IntegrityService returns "stored_hash"; RecordIntegrity model uses "integrity_hash"
    return RecordIntegrity(
        record_id=result["record_id"],
        integrity_hash=result["stored_hash"],
        computed_hash=result["computed_hash"],
        verified=result["verified"],
        status=result["status"],
    )


@router.get(
    "/reports/integrity/monthly-signature/{signature_id}",
    response_model=MonthlySignatureVerification,
    summary="Verificar la firma mensual contra su contenido",
)
async def verify_monthly_signature(
    signature_id: str,
    current_user: APIUser = Depends(PermissionChecker("view_reports")),
) -> MonthlySignatureVerification:
    """
    Recalcula el digest del mes firmado y lo compara con el almacenado en la
    firma mensual, devolviendo ``verified``, ``mismatch``, ``legacy`` o
    ``unsupported_version`` (digest escrito por una versión del algoritmo que
    este código no sabe recalcular; no se compara nada).

    El periodo se delimita con la zona horaria persistida en la firma, y el
    digest se recalcula sobre los valores actuales de los fichajes (nunca
    sobre sus ``integrity_hash`` almacenados). En caso de ``mismatch`` se
    adjuntan las correcciones aprobadas (change-requests) aplicadas después de
    la firma, para distinguir una modificación auditada de una alteración
    inexplicada. Las firmas anteriores a esta capacidad se reportan como
    ``legacy``: nunca se rellena su digest.

    Requiere permiso ``view_reports`` (admin o inspector).
    """
    logger.info(
        "Monthly signature verification requested: signature=%s user=%s",
        signature_id, current_user.username,
    )
    result = await IntegrityService.verify_monthly_signature(signature_id)
    return MonthlySignatureVerification(**result)


# ---------------------------------------------------------------------------
# Worker endpoints (email + password authentication)
# ---------------------------------------------------------------------------


@router.post(
    "/reports/worker/monthly",
    response_model=WorkerMonthlySummary,
    summary="Informe mensual del propio trabajador",
)
async def get_worker_own_monthly_report(
    request: WorkerReportRequest,
) -> WorkerMonthlySummary:
    """
    Permite a un trabajador consultar su propio resumen mensual de jornada.

    La autenticación se realiza con email y contraseña (sin JWT). El trabajador
    sólo puede consultar datos de empresas a las que pertenece.

    El resumen incluye el desglose diario y el estado de firma del mes. El mes
    se delimita con la zona horaria de la ficha del trabajador (el ``timezone``
    de la petición se ignora), de modo que coincide con el mes que se firma.
    """
    worker = await _authenticate_worker(request.email, request.password)
    _verify_worker_company_access(worker, request.company_id)

    worker_id = str(worker["_id"])
    timezone = resolve_worker_timezone(worker)
    logger.info(
        "Worker self-report requested: worker=%s company=%s year=%d month=%d",
        worker_id, request.company_id, request.year, request.month,
    )

    return await ReportService().get_worker_monthly_summary(
        company_id=request.company_id,
        worker_id=worker_id,
        year=request.year,
        month=request.month,
        timezone=timezone,
    )


@router.post(
    "/reports/worker/monthly/sign",
    response_model=MonthlySignatureResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Firmar el registro mensual",
)
async def sign_monthly_report(
    request: MonthlySignatureRequest,
) -> MonthlySignatureResponse:
    """
    Permite a un trabajador firmar digitalmente su registro mensual de jornada.

    La firma registra el consentimiento del trabajador sobre el contenido del
    mes indicado: se calcula un digest SHA-256 del conjunto de fichajes (con la
    ventana UTC de la zona horaria configurada en la ficha del trabajador, la
    misma que delimita el informe que ha consultado) y se persiste junto a la
    firma, de modo que pueda verificarse después. Si el mes ya fue firmado,
    devuelve un error 409. Solo pueden firmarse meses ya cerrados: el mes en
    curso o uno futuro se rechaza con un error 400.

    La autenticación se realiza con email y contraseña (sin JWT).
    """
    worker = await _authenticate_worker(request.email, request.password)
    _verify_worker_company_access(worker, request.company_id)

    worker_id = str(worker["_id"])
    already_signed_detail = f"El mes {request.month}/{request.year} ya fue firmado anteriormente"

    existing = await db.MonthlySignatures.find_one({
        "worker_id": worker_id,
        "company_id": request.company_id,
        "year": request.year,
        "month": request.month,
    })
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=already_signed_detail,
        )

    signed_at = datetime.now(dt_timezone.utc)

    # Bind the signature to the content it covers: digest of the month's
    # records as they are right now, delimited by the timezone window the
    # worker saw in their monthly report. The zone is resolved server-side from
    # the worker's record, never from the request: otherwise two months signed
    # from devices in different zones would leave a boundary punch covered by
    # no signature at all.
    timezone = resolve_worker_timezone(worker)

    # Only closed months may be signed. A month still receiving punches is not
    # stable enough to bind a digest to: a punch landing between reading the
    # records below and inserting the signature would enter the month but not
    # the digest, leaving a signature born in "mismatch" with no audited
    # correction — indistinguishable from tampering (TOCTOU). The current and
    # future months, evaluated in the worker's resolved zone, are rejected.
    now_local = datetime.now(dt_timezone.utc).astimezone(pytz.timezone(timezone))
    if (request.year, request.month) >= (now_local.year, now_local.month):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"El mes {request.month}/{request.year} todavía no ha terminado: "
                "solo se pueden firmar meses ya cerrados"
            ),
        )

    if request.timezone and request.timezone != timezone:
        logger.debug(
            "Sign request for worker %s carried timezone %s; month delimited with %s",
            worker_id, request.timezone, timezone,
        )

    records = await IntegrityService.get_month_records(
        worker_id=worker_id,
        company_id=request.company_id,
        year=request.year,
        month=request.month,
        timezone=timezone,
    )
    content_hash, record_count = IntegrityService.compute_month_digest(records)

    signature_doc = {
        "worker_id": worker_id,
        "company_id": request.company_id,
        "year": request.year,
        "month": request.month,
        "signed_at": signed_at,
        "content_hash": content_hash,
        "content_hash_version": MONTHLY_DIGEST_VERSION,
        "record_count": record_count,
        "timezone": timezone,
    }

    try:
        result = await db.MonthlySignatures.insert_one(signature_doc)
    except DuplicateKeyError:
        # Lost the race against a concurrent request (double tap on the confirm
        # button): the unique index rejects the second insert, and the answer
        # must be the documented 409, not a 500.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=already_signed_detail,
        )

    logger.info(
        "Monthly report signed: worker=%s company=%s year=%d month=%d records=%d hash=%s",
        worker_id, request.company_id, request.year, request.month, record_count, content_hash,
    )

    return MonthlySignatureResponse(
        id=str(result.inserted_id),
        worker_id=worker_id,
        company_id=request.company_id,
        year=request.year,
        month=request.month,
        status="signed",
        signed_at=signed_at,
        content_hash=content_hash,
    )


@router.post(
    "/reports/worker/signatures/status",
    response_model=SignatureStatusResponse,
    summary="Estado de firmas de los últimos 12 meses",
)
async def get_worker_signature_status(
    request: WorkerReportRequest,
) -> SignatureStatusResponse:
    """
    Devuelve el estado de firma de los últimos 12 meses para un trabajador.

    Solo se incluyen meses en los que el trabajador tiene registros de tiempo.
    Los meses se clasifican en dos grupos:

    - ``signed``: meses con firma registrada (incluye ``signed_at``).
    - ``pending``: meses con registros de tiempo pero sin firma.

    La autenticación se realiza con email y contraseña (sin JWT). Los campos
    ``year`` y ``month`` del cuerpo de la petición son ignorados; se utilizan
    los últimos 12 meses desde el mes actual.
    """
    worker = await _authenticate_worker(request.email, request.password)
    _verify_worker_company_access(worker, request.company_id)

    worker_id = str(worker["_id"])

    now = datetime.now(dt_timezone.utc)
    current_year, current_month = now.year, now.month

    # Build the list of the last 12 calendar months (excluding current month)
    candidate_months: list[tuple[int, int]] = []
    y, m = current_year, current_month
    for _ in range(12):
        m -= 1
        if m == 0:
            m = 12
            y -= 1
        candidate_months.append((y, m))

    oldest_year = candidate_months[-1][0]
    oldest_month = candidate_months[-1][1]

    # Find months where the worker actually has time records
    oldest_date = datetime(oldest_year, oldest_month, 1, tzinfo=dt_timezone.utc)
    records_pipeline = [
        {
            "$match": {
                "worker_id": worker_id,
                "company_id": request.company_id,
                "timestamp": {"$gte": oldest_date},
            },
        },
        {
            "$group": {
                "_id": {
                    "year": {"$year": "$timestamp"},
                    "month": {"$month": "$timestamp"},
                },
            },
        },
    ]
    months_with_records: set[tuple[int, int]] = set()
    async for doc in db.TimeRecords.aggregate(records_pipeline):
        months_with_records.add((doc["_id"]["year"], doc["_id"]["month"]))

    signatures_cursor = db.MonthlySignatures.find({
        "worker_id": worker_id,
        "company_id": request.company_id,
        "year": {"$gte": oldest_year},
    })
    signed_set: dict[tuple[int, int], datetime] = {}
    async for sig in signatures_cursor:
        key = (sig["year"], sig["month"])
        signed_at_raw = sig.get("signed_at")
        if signed_at_raw is not None and signed_at_raw.tzinfo is None:
            signed_at_raw = signed_at_raw.replace(tzinfo=dt_timezone.utc)
        signed_set[key] = signed_at_raw

    pending: list[dict] = []
    signed: list[dict] = []

    for year, month in candidate_months:
        key = (year, month)
        if key in signed_set:
            signed.append({
                "year": year,
                "month": month,
                "status": "signed",
                "signed_at": signed_set[key].isoformat() if signed_set[key] else None,
            })
        elif key in months_with_records:
            pending.append({"year": year, "month": month, "status": "pending"})

    logger.info(
        "Signature status requested: worker=%s company=%s pending=%d signed=%d",
        worker_id, request.company_id, len(pending), len(signed),
    )

    return SignatureStatusResponse(pending=pending, signed=signed)


@router.post(
    "/reports/worker/monthly/export",
    summary="Exportar informe mensual del propio trabajador (CSV / PDF)",
)
async def export_worker_own_monthly_report(
    request: WorkerExportRequest,
) -> StreamingResponse:
    """
    Permite a un trabajador exportar su propio resumen mensual de jornada.

    La autenticación se realiza con email y contraseña (sin JWT). El trabajador
    sólo puede exportar datos de empresas a las que pertenece. El mes se
    delimita con la zona horaria de la ficha del trabajador (el ``timezone`` de
    la petición se ignora), igual que al consultar y al firmar.

    Cabeceros de respuesta:

    - ``X-Report-Hash``: SHA-256 del contenido del fichero (para auditoría).
    - ``X-Report-Generated``: Timestamp ISO 8601 de generación.
    """
    worker = await _authenticate_worker(request.email, request.password)
    _verify_worker_company_access(worker, request.company_id)

    worker_id = str(worker["_id"])
    timezone = resolve_worker_timezone(worker)
    logger.info(
        "Worker self-export requested: worker=%s company=%s year=%d month=%d format=%s",
        worker_id, request.company_id, request.year, request.month, request.format,
    )

    summary = await ReportService().get_worker_monthly_summary(
        company_id=request.company_id,
        worker_id=worker_id,
        year=request.year,
        month=request.month,
        timezone=timezone,
    )

    export_service = ExportService()
    if request.format == "csv":
        buf: io.BytesIO = await export_service.export_monthly_csv(summary, timezone=timezone)
        media_type = "text/csv"
        ext = "csv"
    else:
        buf = await export_service.export_monthly_pdf(summary, timezone=timezone)
        media_type = "application/pdf"
        ext = "pdf"

    raw_bytes = buf.getvalue()
    report_hash = hashlib.sha256(raw_bytes).hexdigest()
    buf.seek(0)

    worker_name = f"{worker.get('first_name', '')}_{worker.get('last_name', '')}".strip("_").replace(" ", "_")
    filename = f"informe_{worker_name}_{request.year}-{request.month:02d}.{ext}"
    generated_at = datetime.now(dt_timezone.utc).isoformat()

    logger.info(
        "Worker self-export generated: file=%s hash=%s worker=%s",
        filename, report_hash, worker_id,
    )

    return StreamingResponse(
        content=buf,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Report-Generated": generated_at,
            "X-Report-Hash": report_hash,
        },
    )
