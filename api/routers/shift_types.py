"""
Shift types CRUD (per company, opt-in module).

All endpoints are gated by ``shift_planning_gating`` and require the admin
permissions ``view_shift_plans`` / ``manage_shift_plans``. Editing or deleting
a type never touches planned shifts: cells keep their copied values (D2).
"""
import logging
from datetime import datetime
from typing import List

from bson.objectid import ObjectId
from fastapi import APIRouter, Depends, Query, status

from ..auth.permissions import PermissionChecker
from ..database import db, convert_id
from ..models.shift_planning import ShiftTypeCreate, ShiftTypeResponse, ShiftTypeUpdate
from ..services.shift_planning_gating import ensure_shift_planning_module_enabled
from ..services.shift_planning_time import validate_shift_timing
from ..utils.errors import raise_api_error

logger = logging.getLogger(__name__)
router = APIRouter()


def _to_response(doc: dict) -> ShiftTypeResponse:
    doc = convert_id(doc)
    for key in ("created_at", "updated_at"):
        if hasattr(doc.get(key), "isoformat"):
            doc[key] = doc[key].isoformat()
    return ShiftTypeResponse(**doc)


@router.post("", response_model=ShiftTypeResponse, status_code=status.HTTP_201_CREATED)
async def create_shift_type(
    shift_type: ShiftTypeCreate,
    _user=Depends(PermissionChecker("manage_shift_plans")),
):
    await ensure_shift_planning_module_enabled(shift_type.company_id)
    validate_shift_timing(shift_type.start_time, shift_type.end_time, shift_type.break_minutes)

    doc = shift_type.model_dump()
    doc["created_at"] = datetime.utcnow()
    doc["updated_at"] = None
    doc["deleted_at"] = None
    result = await db.ShiftTypes.insert_one(doc)
    created = await db.ShiftTypes.find_one({"_id": result.inserted_id})
    return _to_response(created)


@router.get("", response_model=List[ShiftTypeResponse])
async def list_shift_types(
    company_id: str = Query(...),
    _user=Depends(PermissionChecker("view_shift_plans")),
):
    await ensure_shift_planning_module_enabled(company_id)
    docs = (
        await db.ShiftTypes.find({"company_id": company_id, "deleted_at": None})
        .sort("name", 1)
        .to_list(length=None)
    )
    return [_to_response(d) for d in docs]


async def _get_active_type(type_id: str, company_id: str) -> dict:
    try:
        oid = ObjectId(type_id)
    except Exception:
        oid = None
    doc = await db.ShiftTypes.find_one({"_id": oid, "deleted_at": None}) if oid else None
    if doc is None:
        raise_api_error(
            status_code=status.HTTP_404_NOT_FOUND,
            error_code="shift_plan.shift_type_not_found",
            message="Tipo de turno no encontrado",
        )
    if company_id and str(doc.get("company_id")) != company_id:
        raise_api_error(
            status_code=status.HTTP_404_NOT_FOUND,
            error_code="shift_plan.shift_type_not_found",
            message="Tipo de turno no encontrado",
        )
    return doc


@router.put("/{type_id}", response_model=ShiftTypeResponse)
async def update_shift_type(
    type_id: str,
    updates: ShiftTypeUpdate,
    company_id: str = Query(...),
    _user=Depends(PermissionChecker("manage_shift_plans")),
):
    await ensure_shift_planning_module_enabled(company_id)
    doc = await _get_active_type(type_id, company_id)

    changes = updates.model_dump(exclude_unset=True)
    start = changes.get("start_time", doc["start_time"])
    end = changes.get("end_time", doc["end_time"])
    break_minutes = changes.get("break_minutes", doc.get("break_minutes", 0))
    validate_shift_timing(start, end, int(break_minutes))

    changes["updated_at"] = datetime.utcnow()
    await db.ShiftTypes.update_one({"_id": doc["_id"]}, {"$set": changes})
    updated = await db.ShiftTypes.find_one({"_id": doc["_id"]})
    return _to_response(updated)


@router.delete("/{type_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_shift_type(
    type_id: str,
    company_id: str = Query(...),
    _user=Depends(PermissionChecker("manage_shift_plans")),
):
    """Logical delete. Planned shifts built from this type are kept (D2)."""
    await ensure_shift_planning_module_enabled(company_id)
    doc = await _get_active_type(type_id, company_id)
    await db.ShiftTypes.update_one(
        {"_id": doc["_id"]}, {"$set": {"deleted_at": datetime.utcnow()}}
    )
