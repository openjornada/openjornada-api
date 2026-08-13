"""
Router for the per-company absence & vacation policy (admin only).

Exactly one policy per company (unique index on ``company_id``). The first
time a company's policy is requested, it's lazily seeded with sensible
defaults and the default absence type catalogue (see Migration Plan in
``design.md``).
"""
import logging
from datetime import date, datetime, timezone as dt_timezone

from fastapi import APIRouter, Depends
from pymongo.errors import DuplicateKeyError

from ..auth.permissions import PermissionChecker
from ..database import db, convert_id
from ..models.absences import (
    AbsencePolicyCreate,
    AbsencePolicyResponse,
    AbsencePolicyUpdate,
    default_absence_types,
)
from ..models.auth import APIUser
from ..services.absence_gating import require_absence_module

router = APIRouter()
logger = logging.getLogger(__name__)


def _ensure_utc_aware(dt):
    """Convert a naive datetime (from MongoDB) to UTC aware."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=dt_timezone.utc)
    return dt


def _prepare_policy_response(doc: dict) -> dict:
    """Prepare a MongoDB policy document for AbsencePolicyResponse (UTC-aware datetimes)."""
    data = convert_id(doc)
    data["created_at"] = _ensure_utc_aware(data.get("created_at"))
    data["updated_at"] = _ensure_utc_aware(data.get("updated_at"))
    return data


def _serialize_policy_doc(doc: dict) -> dict:
    """
    Convert ``date`` values inside ``blackout_periods`` to ``datetime`` —
    PyMongo can only encode ``datetime``, not the bare ``datetime.date``
    Pydantic produces for date fields.
    """
    blackout_periods = doc.get("blackout_periods")
    if blackout_periods:
        doc["blackout_periods"] = [
            {
                **period,
                "start_date": (
                    datetime.combine(period["start_date"], datetime.min.time())
                    if isinstance(period.get("start_date"), date) else period.get("start_date")
                ),
                "end_date": (
                    datetime.combine(period["end_date"], datetime.min.time())
                    if isinstance(period.get("end_date"), date) else period.get("end_date")
                ),
            }
            for period in blackout_periods
        ]
    return doc


async def _get_or_seed_policy(company_id: str) -> dict:
    """Return the company's policy, lazily creating the default one if missing."""
    policy = await db.AbsencePolicies.find_one({"company_id": company_id})
    if policy is not None:
        return policy

    now = datetime.now(dt_timezone.utc)
    default_policy = AbsencePolicyCreate()
    policy_doc = _serialize_policy_doc({
        "company_id": company_id,
        **default_policy.model_dump(),
        "created_at": now,
        "updated_at": now,
    })
    try:
        result = await db.AbsencePolicies.insert_one(policy_doc)
        policy_doc["_id"] = result.inserted_id
        logger.info("Seeded default absence policy for company=%s", company_id)
        return policy_doc
    except DuplicateKeyError:
        return await db.AbsencePolicies.find_one({"company_id": company_id})


@router.get(
    "/{company_id}",
    response_model=AbsencePolicyResponse,
    summary="Obtener la política de ausencias de una empresa",
)
async def get_absence_policy(
    company_id: str = Depends(require_absence_module),
    current_user: APIUser = Depends(PermissionChecker("manage_absence_policies")),
) -> AbsencePolicyResponse:
    """
    Devuelve la política de ausencias de la empresa, creándola con valores
    por defecto (y el catálogo de tipos por defecto) si aún no existe.
    """
    policy = await _get_or_seed_policy(company_id)
    return AbsencePolicyResponse(**_prepare_policy_response(policy))


@router.put(
    "/{company_id}",
    response_model=AbsencePolicyResponse,
    summary="Crear o actualizar la política de ausencias de una empresa",
)
async def upsert_absence_policy(
    policy_data: AbsencePolicyUpdate,
    company_id: str = Depends(require_absence_module),
    current_user: APIUser = Depends(PermissionChecker("manage_absence_policies")),
) -> AbsencePolicyResponse:
    """
    Crea la política de la empresa si no existía, o actualiza la existente
    (nunca crea una segunda política para la misma empresa).
    """
    existing = await db.AbsencePolicies.find_one({"company_id": company_id})
    now = datetime.now(dt_timezone.utc)

    if existing is None:
        base = AbsencePolicyCreate(**policy_data.model_dump(exclude_unset=True))
        policy_doc = _serialize_policy_doc({
            "company_id": company_id,
            **base.model_dump(),
            "created_at": now,
            "updated_at": now,
        })
        result = await db.AbsencePolicies.insert_one(policy_doc)
        policy_doc["_id"] = result.inserted_id
        return AbsencePolicyResponse(**_prepare_policy_response(policy_doc))

    update_data = policy_data.model_dump(exclude_unset=True)
    if update_data:
        update_data["updated_at"] = now
        # Pydantic sub-models (BlackoutPeriod/AbsenceType) must be plain dicts for Mongo.
        for key in ("blackout_periods", "absence_types"):
            if key in update_data and update_data[key] is not None:
                update_data[key] = [dict(item) for item in update_data[key]]
        update_data = _serialize_policy_doc(update_data)

        await db.AbsencePolicies.update_one(
            {"_id": existing["_id"]},
            {"$set": update_data},
        )

    updated = await db.AbsencePolicies.find_one({"_id": existing["_id"]})
    return AbsencePolicyResponse(**_prepare_policy_response(updated))


@router.get(
    "/{company_id}/types",
    response_model=list,
    summary="Catálogo de tipos de ausencia de la empresa",
)
async def get_absence_types(
    company_id: str = Depends(require_absence_module),
    current_user: APIUser = Depends(PermissionChecker("manage_absence_policies")),
) -> list:
    """Atajo de conveniencia: devuelve solo el catálogo de tipos de ausencia."""
    policy = await _get_or_seed_policy(company_id)
    return policy.get("absence_types", [t.model_dump() for t in default_absence_types()])
