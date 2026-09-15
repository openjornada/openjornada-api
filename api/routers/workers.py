from fastapi import APIRouter, HTTPException, status, Depends, Query
from pydantic import TypeAdapter, EmailStr, ValidationError
from typing import List, Optional
from datetime import datetime, timedelta
from bson.objectid import ObjectId
from zoneinfo import ZoneInfo
import re
import secrets
import logging
import anyio

from ..models.workers import (
    WorkerModel,
    WorkerResponse,
    WorkerUpdateModel,
    ChangePasswordRequest,
    ForgotPasswordRequest,
    ResetPasswordRequest,
    WorkerCompaniesRequest,
    WorkerMeRequest,
    WorkerMeResponse,
    WorkerLanguageUpdate,
    WorkerLanguageResponse,
    WorkerBulkImportRequest,
    WorkerBulkImportResponse,
    WorkerImportRow,
    WorkerImportRowResult,
    WorkerWorkCenterAssignment,
    WorkerBulkWorkCenterRequest,
    WorkerBulkWorkCenterResponse,
)
from ..models.auth import APIUser
from ..models.i18n import (
    SUPPORTED_LOCALES,
    resolve_notification_locale,
    resolve_worker_ui_locale,
)
from ..database import db, convert_id
from ..auth.auth_handler import get_password_hash, verify_password
from ..auth.permissions import PermissionChecker
from ..auth.subscription_guard import require_active_subscription
from ..services.email_service import email_service
from ..utils.company_locale import resolve_company_locale
from ..utils.errors import raise_api_error
from ..utils.worker_auth import _authenticate_worker

router = APIRouter()
logger = logging.getLogger(__name__)

_email_adapter = TypeAdapter(EmailStr)


async def _send_welcome_email_to_worker(created_worker: dict):
    """Generate a password-reset token and send the welcome email (best-effort).

    Shared by create_worker and the bulk import. An email failure never
    propagates: the worker creation is not reverted.
    """
    reset_token = secrets.token_urlsafe(32)
    reset_token_expires = datetime.utcnow() + timedelta(hours=1)

    await db.Workers.update_one(
        {"_id": created_worker["_id"]},
        {"$set": {
            "reset_token": reset_token,
            "reset_token_expires": reset_token_expires
        }}
    )

    settings = await db.Settings.find_one()
    contact_email = settings.get("contact_email", "support@openjornada.es") if settings else "support@openjornada.es"
    import os
    webapp_url = os.getenv("WEBAPP_URL", "http://localhost:5173")

    worker_name = f"{created_worker.get('first_name', '')} {created_worker.get('last_name', '')}".strip() or "Usuario"

    # Worker-facing emails use the recipient company's notification language.
    company_ids = created_worker.get("company_ids", [])
    locale = await resolve_company_locale(str(company_ids[0]) if company_ids else None)

    try:
        await email_service.send_welcome_email(
            to_email=created_worker["email"],
            worker_name=worker_name,
            reset_token=reset_token,
            webapp_url=webapp_url,
            contact_email=contact_email,
            locale=locale,
        )
    except Exception as e:
        logger.error(f"[CREATE-WORKER] Error sending welcome email: {type(e).__name__}: {e}")
        import traceback
        logger.error(traceback.format_exc())


async def _build_company_name_cache(names: set) -> dict:
    """One exact case-insensitive query per distinct company name.

    Returns {lowercase_name: [company_id_str, ...]} over active companies.
    """
    cache = {}
    for name in names:
        ids = []
        async for company in db.Companies.find(
            {"name": {"$regex": f"^{re.escape(name)}$", "$options": "i"}, "deleted_at": None},
            {"_id": 1},
        ):
            ids.append(str(company["_id"]))
        cache[name.lower()] = ids
    return cache


def _resolve_company_ids(row: WorkerImportRow, company_cache: dict):
    """Resolve row.company_names to company ids via the request cache.

    Returns (company_ids, error_detail). Never creates companies.
    """
    company_ids = []
    for raw_name in row.company_names:
        name = raw_name.strip()
        if not name:
            continue
        matches = company_cache.get(name.lower(), [])
        if not matches:
            return [], f"Empresa no encontrada: {name}"
        if len(matches) > 1:
            return [], f"Empresa ambigua: {name}"
        company_ids.extend(matches)
    if not company_ids:
        return [], "Debe indicar al menos una empresa"
    return company_ids, None


async def _resolve_work_center_names(worker: dict) -> dict:
    """Resolve worker.work_center_assignments to {company_id: center_name}."""
    work_center_names = {}
    for company_id, work_center_id in worker.get("work_center_assignments", {}).items():
        try:
            work_center = await db.WorkCenters.find_one({"_id": ObjectId(work_center_id)})
            if work_center:
                work_center_names[company_id] = work_center["name"]
        except Exception:
            pass
    return work_center_names


async def _resolve_company_names(worker: dict) -> list:
    """Resolve worker.company_ids to an index-aligned list of company names.

    One entry per company_id in the same order: the company name when the
    company exists (even if soft-deleted, so historical associations keep
    their label) and "" when it is missing. Alignment matters because the
    admin UI resolves names via company_names[company_ids.indexOf(id)].
    """
    company_names = []
    for company_id in worker.get("company_ids", []):
        try:
            company = await db.Companies.find_one({"_id": ObjectId(company_id)})
        except Exception:
            company = None
        company_names.append(company["name"] if company else "")
    return company_names


async def _get_worker_response(worker: dict) -> WorkerResponse:
    """Build a fully-populated WorkerResponse for a single worker."""
    worker_data = convert_id(worker)
    worker_data["company_names"] = await _resolve_company_names(worker)
    worker_data["work_center_names"] = await _resolve_work_center_names(worker)
    return WorkerResponse(**worker_data)


@router.post("/workers/", response_model=WorkerResponse, status_code=status.HTTP_201_CREATED)
async def create_worker(
    worker: WorkerModel,
    current_user: APIUser = Depends(PermissionChecker("create_workers"))
):
    send_welcome_email = getattr(worker, "send_welcome_email", False)
    # Validate that all company_ids exist and are not deleted
    if worker.company_ids:
        for company_id in worker.company_ids:
            try:
                company = await db.Companies.find_one({
                    "_id": ObjectId(company_id),
                    "deleted_at": None
                })
                if not company:
                    raise_api_error(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        error_code="worker.company_not_found",
                        message=f"La empresa con ID {company_id} no existe o ha sido eliminada",
                    )
            except Exception as e:
                logger.error(f"Error validating company {company_id}: {e}")
                raise_api_error(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    error_code="worker.invalid_company_id",
                    message=f"ID de empresa inválido: {company_id}",
                )

    # Check if email or id_number already exists
    if await db.Workers.find_one({"$or": [
        {"email": worker.email},
        {"id_number": worker.id_number}
    ]}):
        # Determine which field is duplicated for a better error message
        if await db.Workers.find_one({"email": worker.email}):
            raise_api_error(
                status_code=status.HTTP_400_BAD_REQUEST,
                error_code="worker.email_taken",
                message="Email already registered",
            )
        else:
            raise_api_error(
                status_code=status.HTTP_400_BAD_REQUEST,
                error_code="worker.id_number_taken",
                message="ID number (DNI) already registered",
            )

    # Hash the password
    hashed_password = get_password_hash(worker.password)

    # Add the current user as the creator
    worker_data = worker.model_dump(exclude={"password","send_welcome_email"})
    worker_data["hashed_password"] = hashed_password
    worker_data["created_by"] = current_user.username
    worker_data["created_at"] = datetime.utcnow()
    worker_data["deleted_at"] = None
    worker_data["deleted_by"] = None

    new_worker = await db.Workers.insert_one(worker_data)
    created_worker = await db.Workers.find_one({"_id": new_worker.inserted_id})


    if send_welcome_email:
        await _send_welcome_email_to_worker(created_worker)

    # Get company names for response (index-aligned with company_ids)
    response_data = convert_id(created_worker)
    response_data["company_names"] = await _resolve_company_names(created_worker)

    return WorkerResponse(**response_data)


@router.post("/workers/bulk-import", response_model=WorkerBulkImportResponse, status_code=status.HTTP_200_OK)
async def bulk_import_workers(
    request: WorkerBulkImportRequest,
    current_user: APIUser = Depends(PermissionChecker("create_workers"))
):
    """
    Importación masiva de trabajadores (JSON con filas ya parseadas del CSV).

    Procesamiento secuencial con import parcial: una fila que falla no
    aborta el lote. Con `dry_run=true` se ejecutan todas las validaciones
    (obligatorios, formato de email, resolución de empresas por nombre,
    duplicados en BD e intra-lote) sin insertar nada ni enviar emails.
    Máximo MAX_BULK_IMPORT_ROWS (200) filas por request.
    """
    rows = request.rows

    distinct_names = {
        name.strip()
        for row in rows
        for name in row.company_names
        if name.strip()
    }
    company_cache = await _build_company_name_cache(distinct_names)

    results: List[WorkerImportRowResult] = []
    seen_emails = set()
    seen_id_numbers = set()

    for index, row in enumerate(rows):
        email_raw = (row.email or "").strip()
        try:
            _email_adapter.validate_python(email_raw)
            email = email_raw.lower()
            echo_email: Optional[str] = email
        except ValidationError:
            echo_email = None

        def _row_result(status_value: str, detail: Optional[str] = None) -> WorkerImportRowResult:
            return WorkerImportRowResult(
                row_index=index, status=status_value, detail=detail, email=echo_email
            )

        missing = [
            field for field, value in (
                ("first_name", row.first_name),
                ("last_name", row.last_name),
                ("id_number", row.id_number),
            )
            if not value or not value.strip()
        ]
        if missing:
            results.append(_row_result("error", f"Campo/s obligatorios vacíos: {', '.join(missing)}"))
            continue

        if not echo_email:
            results.append(_row_result("error", f"Email inválido: {email_raw or '(vacío)'}"))
            continue

        tz = (row.default_timezone or "UTC").strip() or "UTC"
        try:
            ZoneInfo(tz)
        except Exception:
            results.append(_row_result("error", f"Zona horaria inválida: {tz}"))
            continue

        id_number = row.id_number.strip()

        company_ids, company_error = _resolve_company_ids(row, company_cache)
        if company_error:
            results.append(_row_result("error", company_error))
            continue

        duplicate = await db.Workers.find_one({"$or": [
            {"email": {"$regex": f"^{re.escape(email)}$", "$options": "i"}},
            {"id_number": id_number}
        ]})
        if duplicate:
            detail = "Email ya registrado" if (duplicate.get("email") or "").lower() == email else "DNI ya registrado"
            results.append(_row_result("skipped_duplicate", detail))
            continue
        if email in seen_emails:
            results.append(_row_result("skipped_duplicate", "Email duplicado en el lote"))
            continue
        if id_number in seen_id_numbers:
            results.append(_row_result("skipped_duplicate", "DNI duplicado en el lote"))
            continue

        if not request.dry_run:
            hashed = await anyio.to_thread.run_sync(get_password_hash, secrets.token_urlsafe(16))
            worker_data = {
                "first_name": row.first_name.strip(),
                "last_name": row.last_name.strip(),
                "email": email,
                "phone_number": (row.phone_number or "").strip(),
                "id_number": id_number,
                "default_timezone": tz,
                "company_ids": company_ids,
                "hashed_password": hashed,
                "created_by": current_user.username,
                "created_at": datetime.utcnow(),
                "deleted_at": None,
                "deleted_by": None,
            }
            inserted = await db.Workers.insert_one(worker_data)
            if request.send_welcome_email:
                worker_data["_id"] = inserted.inserted_id
                await _send_welcome_email_to_worker(worker_data)

        seen_emails.add(email)
        seen_id_numbers.add(id_number)
        results.append(_row_result("created"))

    return WorkerBulkImportResponse(
        total=len(rows),
        created=sum(1 for r in results if r.status == "created"),
        skipped=sum(1 for r in results if r.status == "skipped_duplicate"),
        errors=sum(1 for r in results if r.status == "error"),
        results=results,
    )


@router.put("/workers/{worker_id}", response_model=WorkerResponse)
async def update_worker(
    worker_id: str,
    worker_update: WorkerUpdateModel,
    current_user: APIUser = Depends(PermissionChecker("update_workers"))
):
    try:
        worker = await db.Workers.find_one({"_id": ObjectId(worker_id), "deleted_at": None})
    except Exception:
        worker = None

    if not worker:
        raise_api_error(
            status_code=status.HTTP_404_NOT_FOUND,
            error_code="worker.not_found",
            message="Worker not found",
        )

    # Prepare update data
    update_data = worker_update.model_dump(exclude_unset=True)
    unsets = {}

    # If company_ids is being updated, validate
    if "company_ids" in update_data:
        company_ids = update_data["company_ids"]

        # Must have at least 1 company
        if not company_ids or len(company_ids) == 0:
            raise_api_error(
                status_code=status.HTTP_400_BAD_REQUEST,
                error_code="worker.no_companies",
                message="El trabajador debe estar asociado a al menos una empresa",
            )

        # Validate all companies exist and are not deleted
        for company_id in company_ids:
            try:
                company = await db.Companies.find_one({
                    "_id": ObjectId(company_id),
                    "deleted_at": None
                })
                if not company:
                    raise_api_error(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        error_code="worker.company_not_found",
                        message=f"La empresa con ID {company_id} no existe o ha sido eliminada",
                    )
            except Exception as e:
                logger.error(f"Error validating company {company_id}: {e}")
                raise_api_error(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    error_code="worker.invalid_company_id",
                    message=f"ID de empresa inválido: {company_id}",
                )

        # Remove work center assignments for companies no longer associated.
        # Targeted $unset per removed company (dot notation) instead of
        # rewriting the whole work_center_assignments map: a concurrent
        # assign_worker_work_center for a retained company landing between the
        # read above and this write can no longer be clobbered by a stale
        # snapshot. $unset on a missing key is a harmless no-op.
        current_assignments = dict(worker.get("work_center_assignments", {}))
        removed_companies = set(current_assignments.keys()) - set(company_ids)
        unsets = {f"work_center_assignments.{cid}": "" for cid in removed_companies}

    # If email is being updated, check if it's already taken
    if "email" in update_data and update_data["email"] != worker["email"]:
        if await db.Workers.find_one({"email": update_data["email"]}):
            raise_api_error(
                status_code=status.HTTP_400_BAD_REQUEST,
                error_code="worker.email_taken",
                message="Email already registered",
            )

    # If id_number is being updated, check if it's already taken
    if "id_number" in update_data and update_data["id_number"] != worker["id_number"]:
        if await db.Workers.find_one({"id_number": update_data["id_number"]}):
            raise_api_error(
                status_code=status.HTTP_400_BAD_REQUEST,
                error_code="worker.id_number_taken",
                message="ID number (DNI) already registered",
            )

    # Handle password update
    if "password" in update_data:
        hashed_password = get_password_hash(update_data["password"])
        update_data["hashed_password"] = hashed_password
        del update_data["password"]

    # Handle sms_enabled -> store in sms_config subdocument
    if "sms_enabled" in update_data:
        sms_enabled = update_data.pop("sms_enabled")
        existing_sms_config = worker.get("sms_config", {})
        existing_sms_config["sms_enabled"] = sms_enabled
        update_data["sms_config"] = existing_sms_config

    # Update last modified
    update_data["updated_at"] = datetime.utcnow()
    update_data["updated_by"] = current_user.username

    # Update the worker
    update_doc = {"$set": update_data}
    if unsets:
        update_doc["$unset"] = unsets
    await db.Workers.update_one(
        {"_id": ObjectId(worker_id)},
        update_doc
    )

    updated_worker = await db.Workers.find_one({"_id": ObjectId(worker_id)})

    # Get company names for response (index-aligned with company_ids)
    response_data = convert_id(updated_worker)
    response_data["company_names"] = await _resolve_company_names(updated_worker)
    response_data["work_center_names"] = await _resolve_work_center_names(updated_worker)

    return WorkerResponse(**response_data)

@router.put("/workers/{worker_id}/work-center", response_model=WorkerResponse)
async def assign_worker_work_center(
    worker_id: str,
    assignment: WorkerWorkCenterAssignment,
    current_user: APIUser = Depends(PermissionChecker("update_workers"))
):
    """
    Assign a worker to a work center in one of their companies.

    Validates that the company belongs to the worker and that the work center
    exists, is not deleted and belongs to that company. A null work_center_id
    clears the assignment for that company.
    """
    try:
        worker = await db.Workers.find_one({"_id": ObjectId(worker_id), "deleted_at": None})
    except Exception:
        worker = None

    if not worker:
        raise_api_error(
            status_code=status.HTTP_404_NOT_FOUND,
            error_code="worker.not_found",
            message="Worker not found",
        )

    worker_company_ids = [str(cid) for cid in worker.get("company_ids", [])]
    if assignment.company_id not in worker_company_ids:
        raise_api_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_code="worker.company_not_associated",
            message="La empresa no está asociada al trabajador",
        )

    # Targeted $set/$unset on the single company key (dot notation) instead of
    # rewriting the whole work_center_assignments map: two concurrent PUTs for
    # different companies can no longer clobber each other's assignment.
    # Dot notation is safe here because company_id is a MongoDB ObjectId hex
    # string (no dots or "$").
    assignment_key = f"work_center_assignments.{assignment.company_id}"

    if assignment.work_center_id is None:
        # $unset on a nonexistent key is a harmless no-op.
        update_doc = {
            "$unset": {assignment_key: ""},
            "$set": {
                "updated_at": datetime.utcnow(),
                "updated_by": current_user.username,
            },
        }
    else:
        try:
            work_center = await db.WorkCenters.find_one({
                "_id": ObjectId(assignment.work_center_id),
                "company_id": assignment.company_id,
                "deleted_at": None
            })
        except Exception:
            work_center = None

        if not work_center:
            raise_api_error(
                status_code=status.HTTP_400_BAD_REQUEST,
                error_code="work_center.not_found",
                message="El centro de trabajo no existe, está eliminado o no pertenece a esa empresa",
            )
        update_doc = {
            "$set": {
                assignment_key: assignment.work_center_id,
                "updated_at": datetime.utcnow(),
                "updated_by": current_user.username,
            },
        }

    await db.Workers.update_one({"_id": ObjectId(worker_id)}, update_doc)

    updated_worker = await db.Workers.find_one({"_id": ObjectId(worker_id)})

    # Get company names for response (index-aligned with company_ids)
    response_data = convert_id(updated_worker)
    response_data["company_names"] = await _resolve_company_names(updated_worker)
    response_data["work_center_names"] = await _resolve_work_center_names(updated_worker)

    return WorkerResponse(**response_data)

@router.post("/workers/bulk-work-center", response_model=WorkerBulkWorkCenterResponse)
async def bulk_assign_work_center(
    request: WorkerBulkWorkCenterRequest,
    current_user: APIUser = Depends(PermissionChecker("update_workers"))
):
    """
    Assign or clear work-center assignments for many workers at once.

    ``assign`` validates the work center once and only touches workers that
    belong to its company (the ``company_ids`` filter auto-skips the rest).
    ``clear`` wipes the work-center assignment for ``company_id`` if
    provided, or ALL work-center assignments of every listed worker if
    ``company_id`` is omitted.
    Invalid worker ids and non-applicable workers count toward ``skipped``.
    """
    unique_ids = list(dict.fromkeys(request.worker_ids))
    total = len(unique_ids)

    if request.action == "assign":
        if not request.company_id or not request.work_center_id:
            raise_api_error(
                status_code=status.HTTP_400_BAD_REQUEST,
                error_code="work_center.no_update_data",
                message="Debes indicar empresa y centro para asignar",
            )
        try:
            work_center = await db.WorkCenters.find_one({
                "_id": ObjectId(request.work_center_id),
                "company_id": request.company_id,
                "deleted_at": None,
            })
        except Exception:
            work_center = None

        if not work_center:
            raise_api_error(
                status_code=status.HTTP_400_BAD_REQUEST,
                error_code="work_center.not_found",
                message="El centro de trabajo no existe, está eliminado o no pertenece a esa empresa",
            )

    oids = []
    for worker_id in unique_ids:
        try:
            oids.append(ObjectId(worker_id))
        except Exception:
            continue

    query = {"_id": {"$in": oids}, "deleted_at": None}
    update_doc = {
        "$set": {
            "updated_at": datetime.utcnow(),
            "updated_by": current_user.username,
        }
    }

    if request.action == "assign":
        # The company_ids filter is what auto-skips workers not in the
        # center's company: they simply don't match the update.
        query["company_ids"] = request.company_id
        # Dot notation is safe here because company_id is a MongoDB ObjectId
        # hex string (no dots or "$"), same as the single-worker endpoint.
        update_doc["$set"][f"work_center_assignments.{request.company_id}"] = request.work_center_id
    elif request.company_id is not None:
        try:
            ObjectId(request.company_id)
        except Exception:
            raise_api_error(
                status_code=status.HTTP_400_BAD_REQUEST,
                error_code="worker.invalid_company_id",
                message="ID de empresa inválido",
            )
        query["company_ids"] = request.company_id
        # Dot notation is safe here because company_id is a MongoDB ObjectId
        # hex string (no dots or "$"), same as the single-worker endpoint.
        update_doc["$unset"] = {f"work_center_assignments.{request.company_id}": ""}
    else:
        update_doc["$set"]["work_center_assignments"] = {}

    try:
        result = await db.Workers.update_many(query, update_doc)
    except Exception as e:
        logger.error(f"Error in bulk work-center {request.action}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al actualizar los centros de trabajo",
        )

    updated = result.matched_count
    skipped = total - updated

    detail = None
    if request.action == "assign" and skipped > 0:
        detail = "Trabajadores que no pertenecen a la empresa del centro omitidos"

    return WorkerBulkWorkCenterResponse(
        total=total,
        updated=updated,
        skipped=skipped,
        detail=detail,
    )

@router.get("/workers/", response_model=List[WorkerResponse])
async def get_workers(
    work_center_id: Optional[str] = Query(None, description="Filter by work center ID"),
    current_user: APIUser = Depends(PermissionChecker("view_workers"))
):
    query = {"deleted_at": None}
    if work_center_id:
        query["$expr"] = {
            "$gt": [
                {"$size": {"$ifNull": [
                    {"$filter": {
                        "input": {"$objectToArray": "$work_center_assignments"},
                        "as": "entry",
                        "cond": {"$eq": ["$$entry.v", work_center_id]},
                    }},
                    [],
                ]}},
                0,
            ]
        }

    raw_workers = []
    # Exclude deleted workers
    async for worker in db.Workers.find(query):
        raw_workers.append(worker)

    # Batch-resolve every referenced company and work center in two queries
    # instead of one Companies/WorkCenters lookup per worker/assignment.
    def _valid_oids(ids):
        oids = []
        for raw_id in ids:
            try:
                oids.append(ObjectId(raw_id))
            except Exception:
                continue
        return oids

    company_ids = {
        str(cid)
        for worker in raw_workers
        for cid in worker.get("company_ids", [])
    }
    company_map = {}
    company_oids = _valid_oids(company_ids)
    if company_oids:
        async for company in db.Companies.find({"_id": {"$in": company_oids}}):
            company_map[str(company["_id"])] = company

    work_center_ids = {
        str(wc_id)
        for worker in raw_workers
        for wc_id in worker.get("work_center_assignments", {}).values()
    }
    work_center_map = {}
    work_center_oids = _valid_oids(work_center_ids)
    if work_center_oids:
        async for work_center in db.WorkCenters.find({"_id": {"$in": work_center_oids}}):
            work_center_map[str(work_center["_id"])] = work_center

    workers = []
    for worker in raw_workers:
        worker_company_ids = [str(cid) for cid in worker.get("company_ids", [])]
        worker_data = convert_id(worker)
        # Index-aligned with company_ids ("" for missing companies).
        worker_data["company_names"] = [
            company_map.get(cid, {}).get("name", "") for cid in worker_company_ids
        ]
        worker_data["work_center_names"] = {
            company_id: work_center_map[work_center_id]["name"]
            for company_id, work_center_id in worker.get("work_center_assignments", {}).items()
            if work_center_id in work_center_map
        }
        workers.append(WorkerResponse(**worker_data))
    return workers

@router.get("/workers/{worker_id}", response_model=WorkerResponse)
async def get_worker(
    worker_id: str,
    current_user: APIUser = Depends(PermissionChecker("view_workers"))
):
    try:
        worker = await db.Workers.find_one({"_id": ObjectId(worker_id), "deleted_at": None})
    except Exception:
        worker = None

    if not worker:
        raise_api_error(
            status_code=status.HTTP_404_NOT_FOUND,
            error_code="worker.not_found",
            message="Worker not found",
        )

    return await _get_worker_response(worker)

@router.get("/workers/id_number/{id_number}", response_model=WorkerResponse)
async def get_worker_by_id_number(
    id_number: str,
    current_user: APIUser = Depends(PermissionChecker("view_workers"))
):
    worker = await db.Workers.find_one({"id_number": id_number, "deleted_at": None})
    if not worker:
        raise_api_error(
            status_code=status.HTTP_404_NOT_FOUND,
            error_code="worker.not_found",
            message="Worker not found",
        )

    return await _get_worker_response(worker)

@router.delete("/workers/{worker_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_worker(
    worker_id: str,
    current_user: APIUser = Depends(PermissionChecker("delete_workers"))
):
    """
    Soft delete a worker by setting deleted_at timestamp.
    Worker will no longer appear in listings or be able to create time records.
    """
    try:
        worker = await db.Workers.find_one({"_id": ObjectId(worker_id), "deleted_at": None})
    except Exception:
        worker = None

    if not worker:
        raise_api_error(
            status_code=status.HTTP_404_NOT_FOUND,
            error_code="worker.not_found",
            message="Worker not found",
        )
    original_email = worker.get("email")
    new_email = f"{original_email}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"

    original_id = worker.get("id_number")
    new_id_number = f"{original_id}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"

    # Soft delete: set deleted_at timestamp
    await db.Workers.update_one(
        {"_id": ObjectId(worker_id)},
        {"$set": {
            "email": new_email,
            "id_number": new_id_number,
            "deleted_at": datetime.utcnow(),
            "deleted_by": current_user.username
        }}
    )

    # Remove shift state so the (now deleted) worker cannot hold an open
    # jornada that would block the state machine.
    await db.WorkerShiftStates.delete_many({"worker_id": worker_id})

    return None

@router.patch("/workers/change-password", status_code=status.HTTP_200_OK)
async def change_worker_password(request: ChangePasswordRequest):
    """
    Allow workers to change their own password.

    Workers authenticate with email + current password (no JWT required).
    New password must be different from current password and at least 6 characters.
    """
    # Find worker by email (exclude deleted workers)
    worker = await db.Workers.find_one({"email": request.email, "deleted_at": None})
    if not worker:
        raise_api_error(
            status_code=status.HTTP_401_UNAUTHORIZED,
            error_code="worker.invalid_credentials",
            message="Invalid credentials",
        )

    # Verify current password
    if not verify_password(request.current_password, worker["hashed_password"]):
        raise_api_error(
            status_code=status.HTTP_401_UNAUTHORIZED,
            error_code="worker.invalid_credentials",
            message="Invalid credentials",
        )

    # Validate new password is not empty/whitespace only
    if not request.new_password.strip():
        raise_api_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_code="worker.password_empty",
            message="New password cannot be empty",
        )

    # Validate new password is different from current password
    if verify_password(request.new_password, worker["hashed_password"]):
        raise_api_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_code="worker.password_unchanged",
            message="New password must be different from current password",
        )

    # Hash new password
    new_hashed_password = get_password_hash(request.new_password)

    # Update password in database
    result = await db.Workers.update_one(
        {"_id": worker["_id"]},
        {"$set": {
            "hashed_password": new_hashed_password,
            "updated_at": datetime.utcnow()
        }}
    )

    # Verify update was successful
    if result.modified_count == 0:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update password"
        )

    return {"message": "Password changed successfully"}


@router.patch("/workers/language", response_model=WorkerLanguageResponse, status_code=status.HTTP_200_OK)
async def update_worker_language(request: WorkerLanguageUpdate) -> WorkerLanguageResponse:
    """
    Allow workers to read/update their own UI language preference.

    Same authentication pattern as the change-password endpoint: email +
    current password in the body (no JWT). ``language=null`` clears the
    preference so the worker inherits the company notification language
    again. Unsupported codes are rejected with 422 / ``worker.invalid_locale``.
    """
    worker = await _authenticate_worker(request.email, request.password)

    if request.language is not None and request.language not in SUPPORTED_LOCALES:
        raise_api_error(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            error_code="worker.invalid_locale",
            message=f"Idioma no soportado: {request.language}. Soportados: {', '.join(SUPPORTED_LOCALES)}",
        )

    await db.Workers.update_one(
        {"_id": worker["_id"]},
        {"$set": {"language": request.language, "updated_at": datetime.utcnow()}},
    )

    company_ids = worker.get("company_ids", [])
    notification_locale = await resolve_company_locale(
        str(company_ids[0]) if company_ids else None
    )
    return WorkerLanguageResponse(
        language=request.language,
        notification_language=notification_locale,
        effective_language=resolve_worker_ui_locale(
            {"language": request.language},
            {"notification_language": notification_locale},
        ),
    )


@router.post("/workers/forgot-password", status_code=status.HTTP_200_OK)
async def forgot_password(request: ForgotPasswordRequest):
    """
    Request password reset email.

    Public endpoint (no authentication required).
    Always returns success message regardless of whether email exists (security best practice).
    Rate limited to 3 attempts per hour per worker.
    """
    import logging
    logger = logging.getLogger(__name__)

    logger.info(f"[FORGOT-PASSWORD] Request received for email: {request.email}")

    # Generic success message (don't reveal if email exists)
    success_message = {
        "message": "Si el email existe, recibirás instrucciones para restablecer tu contraseña"
    }

    try:
        # Find worker by email (exclude deleted workers)
        logger.info(f"[FORGOT-PASSWORD] Searching for worker with email: {request.email}")
        worker = await db.Workers.find_one({"email": request.email, "deleted_at": None})

        # If worker doesn't exist, return success message anyway (security)
        if not worker:
            logger.info(f"[FORGOT-PASSWORD] Worker not found for email: {request.email}")
            return success_message

        logger.info(f"[FORGOT-PASSWORD] Worker found: {worker.get('first_name', '')} {worker.get('last_name', '')}")

        # Check rate limit: count reset attempts in last hour
        one_hour_ago = datetime.utcnow() - timedelta(hours=1)
        recent_attempts = worker.get("reset_attempts", [])

        # Filter to keep only attempts from last hour
        recent_attempts = [
            attempt for attempt in recent_attempts
            if isinstance(attempt, datetime) and attempt > one_hour_ago
        ]

        # Check if rate limit exceeded
        if len(recent_attempts) >= 3:
            raise_api_error(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                error_code="worker.rate_limited",
                message="Demasiados intentos de restablecimiento. Por favor, espera una hora antes de intentarlo de nuevo.",
            )

        # Generate secure random token
        reset_token = secrets.token_urlsafe(32)

        # Set expiration (1 hour from now)
        reset_token_expires = datetime.utcnow() + timedelta(hours=1)

        # Add current timestamp to reset_attempts
        recent_attempts.append(datetime.utcnow())

        # Update worker with reset token and cleaned attempts list
        await db.Workers.update_one(
            {"_id": worker["_id"]},
            {
                "$set": {
                    "reset_token": reset_token,
                    "reset_token_expires": reset_token_expires,
                    "reset_attempts": recent_attempts
                }
            }
        )

        # Get settings for contact_email and URLs from environment
        logger.info("[FORGOT-PASSWORD] Fetching settings from database...")
        settings = await db.Settings.find_one()
        contact_email = settings.get("contact_email", "support@openjornada.es") if settings else "support@openjornada.es"

        # Get URLs from environment variables
        import os
        webapp_url = os.getenv("WEBAPP_URL", "http://localhost:5173")
        logger.info(f"[FORGOT-PASSWORD] Settings - WebApp URL: {webapp_url}, Contact Email: {contact_email}")

        # Get worker name
        worker_name = f"{worker.get('first_name', '')} {worker.get('last_name', '')}".strip()
        if not worker_name:
            worker_name = "Usuario"
        logger.info(f"[FORGOT-PASSWORD] Worker name: {worker_name}")

        # Send reset email (don't wait for result, catch errors silently)
        try:
            logger.info(f"[FORGOT-PASSWORD] Calling email service to send reset email to: {request.email}")
            # Worker-facing emails use the recipient company's notification language.
            worker_company_ids = worker.get("company_ids", [])
            locale = await resolve_company_locale(
                str(worker_company_ids[0]) if worker_company_ids else None
            )
            email_result = await email_service.send_password_reset_email(
                to_email=request.email,
                worker_name=worker_name,
                reset_token=reset_token,
                webapp_url=webapp_url,
                contact_email=contact_email,
                locale=locale,
            )
            logger.info(f"[FORGOT-PASSWORD] Email service returned: {email_result}")
        except Exception as e:
            # Log error but don't expose it to user
            logger.error(f"[FORGOT-PASSWORD] Error sending password reset email: {type(e).__name__}: {e}")
            import traceback
            logger.error(f"[FORGOT-PASSWORD] Traceback: {traceback.format_exc()}")

        # Always return success message (security best practice)
        return success_message

    except HTTPException:
        # Re-raise HTTP exceptions (like rate limit)
        raise
    except Exception as e:
        # Log error but return success message (security)
        print(f"Error in forgot_password: {e}")
        return success_message


@router.post("/workers/reset-password", status_code=status.HTTP_200_OK)
async def reset_password(request: ResetPasswordRequest):
    """
    Reset password using token from email.

    Public endpoint (no authentication required).
    Token must be valid and not expired.
    """
    # Find worker by reset token
    worker = await db.Workers.find_one({
        "reset_token": request.token,
        "deleted_at": None
    })

    # Check if token exists
    if not worker:
        raise_api_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_code="worker.invalid_reset_token",
            message="Token inválido o expirado",
        )

    # Check if token is expired
    reset_token_expires = worker.get("reset_token_expires")
    if not reset_token_expires or reset_token_expires < datetime.utcnow():
        raise_api_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_code="worker.expired_reset_token",
            message="Token inválido o expirado",
        )

    # Validate new password
    if not request.new_password or not request.new_password.strip():
        raise_api_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_code="worker.password_empty",
            message="La contraseña no puede estar vacía",
        )

    if len(request.new_password) < 6:
        raise_api_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_code="worker.password_too_short",
            message="La contraseña debe tener al menos 6 caracteres",
        )

    # Hash new password
    new_hashed_password = get_password_hash(request.new_password)

    # Update worker: set new password, clear reset token
    result = await db.Workers.update_one(
        {"_id": worker["_id"]},
        {
            "$set": {
                "hashed_password": new_hashed_password,
                "updated_at": datetime.utcnow()
            },
            "$unset": {
                "reset_token": "",
                "reset_token_expires": ""
            }
        }
    )

    # Verify update was successful
    if result.modified_count == 0:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al restablecer la contraseña"
        )

    return {"message": "Contraseña restablecida correctamente"}


@router.post("/workers/my-companies", status_code=status.HTTP_200_OK)
async def get_worker_companies(
    request: WorkerCompaniesRequest,
    _subscription: None = Depends(require_active_subscription)
):
    """
    Get companies associated with a worker.

    Public endpoint - worker authenticates with email and password.
    Returns only the companies this worker is associated with.
    """
    logger.info(f"[MY-COMPANIES] Request received for email: {request.email}")

    try:
        # Find worker by email (exclude deleted workers)
        worker = await db.Workers.find_one({"email": request.email, "deleted_at": None})

        if not worker:
            logger.info(f"[MY-COMPANIES] Worker not found: {request.email}")
            raise_api_error(
                status_code=status.HTTP_401_UNAUTHORIZED,
                error_code="worker.invalid_credentials",
                message="Credenciales inválidas",
            )

        # Verify password
        if not verify_password(request.password, worker.get("hashed_password", "")):
            logger.info(f"[MY-COMPANIES] Invalid password for: {request.email}")
            raise_api_error(
                status_code=status.HTTP_401_UNAUTHORIZED,
                error_code="worker.invalid_credentials",
                message="Credenciales inválidas",
            )

        logger.info(f"[MY-COMPANIES] Worker authenticated: {request.email}")

        # Get worker's company_ids
        company_ids = worker.get("company_ids", [])

        if not company_ids:
            logger.info(f"[MY-COMPANIES] Worker has no companies: {request.email}")
            return []

        logger.info(f"[MY-COMPANIES] Worker has {len(company_ids)} companies")

        # Get companies (only active ones)
        companies = []
        for company_id_str in company_ids:
            try:
                company = await db.Companies.find_one({
                    "_id": ObjectId(company_id_str),
                    "deleted_at": None
                })

                if company:
                    companies.append({
                        "id": str(company["_id"]),
                        "name": company["name"],
                        "created_at": company.get("created_at"),
                        "updated_at": company.get("updated_at"),
                        "absence_management_enabled": company.get("absence_management_enabled", False),
                        # Raw fields so the webapp can resolve its UI locale:
                        # worker.language ?? notification_language ?? navegador ?? es
                        "notification_language": resolve_notification_locale(company)
                    })
            except Exception as e:
                logger.warning(f"[MY-COMPANIES] Error loading company {company_id_str}: {e}")
                continue

        logger.info(f"[MY-COMPANIES] Returning {len(companies)} active companies")

        # Sort by name
        companies.sort(key=lambda x: x["name"])

        return companies

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[MY-COMPANIES] Error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al obtener las empresas"
        )


@router.post("/workers/me", response_model=WorkerMeResponse, status_code=status.HTTP_200_OK)
async def get_worker_me(request: WorkerMeRequest) -> WorkerMeResponse:
    """
    Devuelve el perfil del propio trabajador.

    La autenticación se realiza con email y contraseña (sin JWT).
    Los campos sensibles (hashed_password, reset_token, etc.) nunca se incluyen.
    """
    logger.info("[WORKER-ME] Request received for email: %s", request.email)

    worker = await _authenticate_worker(request.email, request.password)
    worker_id = str(worker["_id"])
    company_ids = [str(cid) for cid in worker.get("company_ids", [])]

    # Resolve company names + notification languages with a single $in query
    oids = [ObjectId(cid) for cid in company_ids]
    companies_cursor = db.Companies.find(
        {"_id": {"$in": oids}, "deleted_at": None},
        {"_id": 1, "name": 1, "notification_language": 1},
    )
    company_map: dict[str, dict] = {
        str(c["_id"]): c
        async for c in companies_cursor
    }
    company_names = [company_map.get(cid, {}).get("name", "") for cid in company_ids]
    first_company = company_map.get(company_ids[0]) if company_ids else None

    logger.info("[WORKER-ME] Returning profile for worker: %s", worker_id)

    return WorkerMeResponse(
        id=worker_id,
        first_name=worker.get("first_name", ""),
        last_name=worker.get("last_name", ""),
        email=worker["email"],
        phone_number=worker.get("phone_number", ""),
        default_timezone=worker.get("default_timezone", "UTC"),
        company_ids=company_ids,
        company_names=company_names,
        # Raw language fields: the client applies the fallback chain
        # worker.language ?? notification_language ?? navegador(soportado) ?? es
        language=worker.get("language"),
        notification_language=resolve_notification_locale(first_company),
    )
