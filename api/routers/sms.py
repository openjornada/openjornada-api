"""
SMS router: endpoints for SMS configuration, logs, credits, and dashboard.
"""

import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..auth.permissions import PermissionChecker
from ..database import db, convert_id
from ..models.auth import APIUser
from ..models.sms import (
    DEFAULT_SMS_TEMPLATE,
    AVAILABLE_TAGS,
    SmsCompanyConfig,
    SmsCompanyConfigUpdate,
    SmsCreditsResponse,
    SmsDashboardResponse,
    SmsDashboardCompanyStats,
    SmsHistoryResponse,
    SmsLogListResponse,
    SmsLogResponse,
    SmsMessage,
    SmsSendRequest,
    SmsSendResponse,
    SmsStats,
    SmsTemplateResponse,
    SmsTemplateUpdate,
    SmsWorkerConfig,
    SmsWorkerConfigUpdate,
)

router = APIRouter()

_PHONE_PATTERN = re.compile(r"^\+?[1-9]\d{6,14}$")


def _validate_phone_number(phone: str) -> str:
    """Validate and clean phone number format."""
    cleaned = phone.strip().replace(" ", "").replace("-", "")
    if not _PHONE_PATTERN.match(cleaned):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Formato de teléfono inválido: {phone}"
        )
    return cleaned


# ============================================================================
# Helper: resolve first active company (for routes without company_id)
# ============================================================================

async def _get_first_active_company() -> dict:
    """Return the first non-deleted company, raising 404 if none found."""
    company = await db.Companies.find_one({"deleted_at": None}, sort=[("created_at", 1)])
    if not company:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No se encontro ninguna empresa activa"
        )
    return company


def _doc_to_sms_message(doc: dict) -> SmsMessage:
    """Convert a raw SmsLogs document to the frontend SmsMessage shape."""
    data = convert_id(doc)
    return SmsMessage(
        id=data["id"],
        worker_id=data.get("worker_id", ""),
        worker_name=data.get("worker_name"),
        worker_id_number=data.get("worker_id_number"),
        phone_number=data.get("phone_number", ""),
        message=data.get("message"),
        status=data.get("status", ""),
        sent_at=data.get("created_at"),
        delivered_at=data.get("delivered_at"),
        error_message=data.get("error_message"),
    )


# ============================================================================
# SMS Template endpoints
# ============================================================================

async def _build_template_response() -> SmsTemplateResponse:
    """Build template response reading from DB or using default."""
    settings = await db.Settings.find_one()
    template = DEFAULT_SMS_TEMPLATE
    if settings and "sms_reminder_template" in settings:
        template = settings["sms_reminder_template"]
    return SmsTemplateResponse(
        template=template,
        default_template=DEFAULT_SMS_TEMPLATE,
        available_tags=AVAILABLE_TAGS,
    )


@router.get("/sms/template", response_model=SmsTemplateResponse)
async def get_sms_template(
    current_user: APIUser = Depends(PermissionChecker("manage_sms_config"))
):
    """Get the current SMS reminder template."""
    return await _build_template_response()


@router.put("/sms/template", response_model=SmsTemplateResponse)
async def update_sms_template(
    body: SmsTemplateUpdate,
    current_user: APIUser = Depends(PermissionChecker("manage_sms_config"))
):
    """Update the SMS reminder template."""
    settings = await db.Settings.find_one()
    if settings:
        await db.Settings.update_one(
            {"_id": settings["_id"]},
            {"$set": {"sms_reminder_template": body.template}},
        )
    else:
        await db.Settings.insert_one({"sms_reminder_template": body.template})
    return await _build_template_response()


@router.delete("/sms/template", response_model=SmsTemplateResponse)
async def reset_sms_template(
    current_user: APIUser = Depends(PermissionChecker("manage_sms_config"))
):
    """Reset the SMS reminder template to default."""
    settings = await db.Settings.find_one()
    if settings:
        await db.Settings.update_one(
            {"_id": settings["_id"]},
            {"$unset": {"sms_reminder_template": ""}},
        )
    return await _build_template_response()


# ============================================================================
# Frontend-friendly: GET /sms/config  (uses first active company)
# ============================================================================

@router.get("/sms/config", response_model=SmsCompanyConfig)
async def get_sms_config(
    current_user: APIUser = Depends(PermissionChecker("manage_sms_config"))
):
    """Get SMS configuration for the active company."""
    company = await _get_first_active_company()
    sms_config = company.get("sms_config", {})
    return SmsCompanyConfig(**sms_config) if sms_config else SmsCompanyConfig()


# ============================================================================
# Frontend-friendly: PATCH /sms/config  (uses first active company)
# ============================================================================

@router.patch("/sms/config", response_model=SmsCompanyConfig)
async def patch_sms_config(
    config_update: SmsCompanyConfigUpdate,
    current_user: APIUser = Depends(PermissionChecker("manage_sms_config"))
):
    """Partially update SMS configuration for the active company."""
    company = await _get_first_active_company()

    existing = company.get("sms_config", {})
    defaults = SmsCompanyConfig().model_dump()
    merged = {**defaults, **existing}

    update_data = config_update.model_dump(exclude_unset=True)
    merged.update(update_data)

    await db.Companies.update_one(
        {"_id": company["_id"]},
        {"$set": {"sms_config": merged, "updated_at": datetime.now(timezone.utc)}}
    )

    return SmsCompanyConfig(**merged)


# ============================================================================
# Per-company SMS Config (kept for multi-company admin)
# ============================================================================

@router.get("/companies/{company_id}/sms-config", response_model=SmsCompanyConfig)
async def get_company_sms_config(
    company_id: str,
    current_user: APIUser = Depends(PermissionChecker("manage_sms_config"))
):
    """Get SMS configuration for a specific company."""
    try:
        oid = ObjectId(company_id)
    except InvalidId:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Formato de ID inválido")
    company = await db.Companies.find_one({"_id": oid, "deleted_at": None})

    if not company:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Empresa no encontrada"
        )

    sms_config = company.get("sms_config", {})
    return SmsCompanyConfig(**sms_config) if sms_config else SmsCompanyConfig()


@router.patch("/companies/{company_id}/sms-config", response_model=SmsCompanyConfig)
async def patch_company_sms_config(
    company_id: str,
    config_update: SmsCompanyConfigUpdate,
    current_user: APIUser = Depends(PermissionChecker("manage_sms_config"))
):
    """Partially update SMS configuration for a specific company."""
    try:
        oid = ObjectId(company_id)
    except InvalidId:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Formato de ID inválido")
    company = await db.Companies.find_one({"_id": oid, "deleted_at": None})

    if not company:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Empresa no encontrada"
        )

    existing = company.get("sms_config", {})
    defaults = SmsCompanyConfig().model_dump()
    merged = {**defaults, **existing}

    update_data = config_update.model_dump(exclude_unset=True)
    merged.update(update_data)

    await db.Companies.update_one(
        {"_id": oid},
        {"$set": {"sms_config": merged, "updated_at": datetime.now(timezone.utc)}}
    )

    return SmsCompanyConfig(**merged)


# ============================================================================
# Worker SMS Config
# ============================================================================

@router.get("/workers/{worker_id}/sms-config", response_model=SmsWorkerConfig)
async def get_worker_sms_config(
    worker_id: str,
    current_user: APIUser = Depends(PermissionChecker("manage_sms_config"))
):
    """Get SMS configuration for a worker."""
    try:
        oid = ObjectId(worker_id)
    except InvalidId:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Formato de ID inválido")
    worker = await db.Workers.find_one({"_id": oid, "deleted_at": None})

    if not worker:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Trabajador no encontrado"
        )

    sms_config = worker.get("sms_config", {})
    config = SmsWorkerConfig(**sms_config) if sms_config else SmsWorkerConfig()
    config.worker_id = worker_id
    return config


@router.post("/workers/{worker_id}/sms/send", response_model=SmsSendResponse)
async def send_worker_sms(
    worker_id: str,
    body: SmsSendRequest,
    current_user: APIUser = Depends(PermissionChecker("manage_sms_config"))
):
    """Send a custom SMS to a worker."""
    from ..services.sms_service import sms_service

    try:
        oid = ObjectId(worker_id)
    except InvalidId:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Formato de ID inválido")
    worker = await db.Workers.find_one({"_id": oid, "deleted_at": None})

    if not worker:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Trabajador no encontrado"
        )

    phone_number = worker.get("phone_number", "")
    if not phone_number:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El trabajador no tiene número de teléfono"
        )

    phone_number = _validate_phone_number(phone_number)

    company_ids = worker.get("company_ids", [])
    company_id = company_ids[0] if company_ids else ""

    worker_name = f"{worker.get('first_name', '')} {worker.get('last_name', '')}".strip()
    worker_id_number = worker.get("id_number", "")

    success, error_message = await sms_service.send_custom_sms(
        worker_id=worker_id,
        company_id=company_id,
        phone_number=phone_number,
        message=body.message,
        worker_name=worker_name,
        worker_id_number=worker_id_number,
    )

    return SmsSendResponse(success=success, error_message=error_message)


@router.patch("/workers/{worker_id}/sms-config", response_model=SmsWorkerConfig)
async def patch_worker_sms_config(
    worker_id: str,
    config_update: SmsWorkerConfigUpdate,
    current_user: APIUser = Depends(PermissionChecker("manage_sms_config"))
):
    """Partially update SMS configuration for a worker."""
    try:
        oid = ObjectId(worker_id)
    except InvalidId:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Formato de ID inválido")
    worker = await db.Workers.find_one({"_id": oid, "deleted_at": None})

    if not worker:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Trabajador no encontrado"
        )

    existing = worker.get("sms_config", {})
    defaults = SmsWorkerConfig().model_dump(exclude={"worker_id"})
    merged = {**defaults, **existing}

    update_data = config_update.model_dump(exclude_unset=True)
    merged.update(update_data)

    await db.Workers.update_one(
        {"_id": oid},
        {"$set": {"sms_config": merged, "updated_at": datetime.now(timezone.utc)}}
    )

    result = SmsWorkerConfig(**merged)
    result.worker_id = worker_id
    return result


# ============================================================================
# SMS History (frontend route)
# ============================================================================

@router.get("/sms/history", response_model=SmsHistoryResponse)
async def list_sms_history(
    worker_id: Optional[str] = Query(None),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    log_status: Optional[str] = Query(None, alias="status"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    current_user: APIUser = Depends(PermissionChecker("view_sms_logs"))
):
    """List SMS messages with skip/limit pagination (frontend-friendly)."""
    query: dict = {}

    if worker_id:
        query["worker_id"] = worker_id
    if log_status:
        query["status"] = log_status
    if start_date or end_date:
        date_filter: dict = {}
        if start_date:
            date_filter["$gte"] = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc)
        if end_date:
            date_filter["$lte"] = datetime.combine(end_date, datetime.max.time(), tzinfo=timezone.utc)
        query["created_at"] = date_filter

    total = await db.SmsLogs.count_documents(query)

    messages: list[SmsMessage] = []
    async for doc in db.SmsLogs.find(query).sort("created_at", -1).skip(skip).limit(limit):
        messages.append(_doc_to_sms_message(doc))

    return SmsHistoryResponse(messages=messages, total=total, skip=skip, limit=limit)


@router.delete("/sms/history")
async def clear_sms_history(
    confirm: bool = Query(False, description="Must be true to confirm deletion"),
    current_user: APIUser = Depends(PermissionChecker("manage_sms_config"))
):
    """Delete all SMS log entries. Requires confirm=true."""
    if not confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Set confirm=true to confirm deletion of all SMS history"
        )
    result = await db.SmsLogs.delete_many({})
    return {"deleted": result.deleted_count}


@router.get("/sms/messages/{message_id}", response_model=SmsMessage)
async def get_sms_message(
    message_id: str,
    current_user: APIUser = Depends(PermissionChecker("view_sms_logs"))
):
    """Get a single SMS message by ID (frontend-friendly)."""
    try:
        oid = ObjectId(message_id)
    except InvalidId:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Formato de ID inválido")
    doc = await db.SmsLogs.find_one({"_id": oid})

    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Mensaje SMS no encontrado"
        )

    return _doc_to_sms_message(doc)


# ============================================================================
# SMS Logs (legacy / admin routes — kept for backwards compatibility)
# ============================================================================

@router.get("/sms/logs", response_model=SmsLogListResponse)
async def list_sms_logs(
    company_id: Optional[str] = Query(None),
    worker_id: Optional[str] = Query(None),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    log_status: Optional[str] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    current_user: APIUser = Depends(PermissionChecker("view_sms_logs"))
):
    """List SMS log entries with optional filters and page-based pagination."""
    query: dict = {}

    if company_id:
        query["company_id"] = company_id
    if worker_id:
        query["worker_id"] = worker_id
    if log_status:
        query["status"] = log_status
    if start_date or end_date:
        date_filter: dict = {}
        if start_date:
            date_filter["$gte"] = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc)
        if end_date:
            date_filter["$lte"] = datetime.combine(end_date, datetime.max.time(), tzinfo=timezone.utc)
        query["created_at"] = date_filter

    total = await db.SmsLogs.count_documents(query)
    skip = (page - 1) * page_size

    items: list[SmsLogResponse] = []
    async for doc in db.SmsLogs.find(query).sort("created_at", -1).skip(skip).limit(page_size):
        doc_data = convert_id(doc)
        items.append(SmsLogResponse(**doc_data))

    return SmsLogListResponse(items=items, total=total, page=page, page_size=page_size)


@router.get("/sms/logs/{log_id}", response_model=SmsLogResponse)
async def get_sms_log(
    log_id: str,
    current_user: APIUser = Depends(PermissionChecker("view_sms_logs"))
):
    """Get a single SMS log entry by ID (admin/legacy)."""
    try:
        oid = ObjectId(log_id)
    except InvalidId:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Formato de ID inválido")
    doc = await db.SmsLogs.find_one({"_id": oid})

    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Registro SMS no encontrado"
        )

    return SmsLogResponse(**convert_id(doc))


# ============================================================================
# SMS Credits — frontend route (no company_id)
# ============================================================================

@router.get("/sms/credits", response_model=SmsCreditsResponse)
async def get_sms_credits(
    current_user: APIUser = Depends(PermissionChecker("view_sms_dashboard"))
):
    """Get SMS credit/balance info for the active company (frontend-friendly)."""
    from ..services.sms_service import sms_service

    unlimited = sms_service.is_unlimited_balance()
    return SmsCreditsResponse(
        balance=0.0,
        currency="EUR",
        unlimited=unlimited,
        provider_enabled=sms_service.is_enabled(),
    )


# ============================================================================
# SMS Stats — frontend route
# ============================================================================

@router.get("/sms/stats", response_model=SmsStats)
async def get_sms_stats(
    current_user: APIUser = Depends(PermissionChecker("view_sms_dashboard"))
):
    """Get simplified SMS statistics for the frontend dashboard."""
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    sent_today = await db.SmsLogs.count_documents({
        "status": {"$in": ["sent", "delivered"]},
        "created_at": {"$gte": today_start}
    })
    failed_today = await db.SmsLogs.count_documents({
        "status": "failed",
        "created_at": {"$gte": today_start}
    })
    sent_this_month = await db.SmsLogs.count_documents({
        "status": {"$in": ["sent", "delivered"]},
        "created_at": {"$gte": month_start}
    })
    # "pending" = queued/in-flight; we don't track that state yet, so return 0
    pending = 0

    return SmsStats(
        sent_today=sent_today,
        failed_today=failed_today,
        pending=pending,
        sent_this_month=sent_this_month,
    )


# ============================================================================
# SMS Dashboard (full aggregate — kept for admin)
# ============================================================================

@router.get("/sms/dashboard", response_model=SmsDashboardResponse)
async def get_sms_dashboard(
    current_user: APIUser = Depends(PermissionChecker("view_sms_dashboard"))
):
    """Get aggregate SMS statistics for the admin dashboard."""
    from ..services.sms_service import sms_service

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Week start (Monday) — safe subtraction avoids day-of-month underflow
    week_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = week_start - timedelta(days=week_start.weekday())

    # Month start
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    pipeline = [
        {"$match": {"created_at": {"$gte": month_start}}},
        {"$facet": {
            "by_company": [
                {"$group": {
                    "_id": "$company_id",
                    "sent_today": {"$sum": {"$cond": [
                        {"$and": [
                            {"$gte": ["$created_at", today_start]},
                            {"$in": ["$status", ["sent", "delivered"]]}
                        ]}, 1, 0
                    ]}},
                    "sent_this_week": {"$sum": {"$cond": [
                        {"$and": [
                            {"$gte": ["$created_at", week_start]},
                            {"$in": ["$status", ["sent", "delivered"]]}
                        ]}, 1, 0
                    ]}},
                    "sent_this_month": {"$sum": {"$cond": [
                        {"$in": ["$status", ["sent", "delivered"]]},
                        1, 0
                    ]}},
                    "failed_this_month": {"$sum": {"$cond": [
                        {"$eq": ["$status", "failed"]},
                        1, 0
                    ]}}
                }}
            ],
            "totals": [
                {"$group": {
                    "_id": None,
                    "sent_today": {"$sum": {"$cond": [
                        {"$and": [
                            {"$gte": ["$created_at", today_start]},
                            {"$in": ["$status", ["sent", "delivered"]]}
                        ]}, 1, 0
                    ]}},
                    "sent_this_week": {"$sum": {"$cond": [
                        {"$and": [
                            {"$gte": ["$created_at", week_start]},
                            {"$in": ["$status", ["sent", "delivered"]]}
                        ]}, 1, 0
                    ]}},
                    "sent_this_month": {"$sum": {"$cond": [
                        {"$in": ["$status", ["sent", "delivered"]]},
                        1, 0
                    ]}},
                    "failed_this_month": {"$sum": {"$cond": [
                        {"$eq": ["$status", "failed"]},
                        1, 0
                    ]}}
                }}
            ]
        }}
    ]

    agg_result = await db.SmsLogs.aggregate(pipeline).to_list(1)
    facets = agg_result[0] if agg_result else {"by_company": [], "totals": []}

    totals = facets["totals"][0] if facets["totals"] else {
        "sent_today": 0, "sent_this_week": 0, "sent_this_month": 0, "failed_this_month": 0
    }

    # Build company stats — need company names
    company_stats_map = {item["_id"]: item for item in facets["by_company"]}
    companies_cursor = db.Companies.find({"deleted_at": None}, {"_id": 1, "name": 1})
    company_stats: list[SmsDashboardCompanyStats] = []
    async for company in companies_cursor:
        cid = str(company["_id"])
        stats = company_stats_map.get(cid, {})
        company_stats.append(SmsDashboardCompanyStats(
            company_id=cid,
            company_name=company.get("name", ""),
            sent_today=stats.get("sent_today", 0),
            sent_this_week=stats.get("sent_this_week", 0),
            sent_this_month=stats.get("sent_this_month", 0),
            failed_this_month=stats.get("failed_this_month", 0),
        ))

    return SmsDashboardResponse(
        total_sent_today=totals["sent_today"],
        total_sent_this_week=totals["sent_this_week"],
        total_sent_this_month=totals["sent_this_month"],
        total_failed_this_month=totals["failed_this_month"],
        unlimited_balance=sms_service.is_unlimited_balance(),
        companies=company_stats,
        provider_enabled=sms_service.is_enabled(),
        provider_name="labsmobile"
    )
