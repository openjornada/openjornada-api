from fastapi import APIRouter, HTTPException, status, Depends, Query
from typing import List, Optional
from datetime import datetime
from bson.objectid import ObjectId
import logging

from ..models.work_centers import WorkCenterCreate, WorkCenterUpdate, WorkCenterResponse
from ..models.auth import APIUser
from ..database import db, convert_id
from ..auth.permissions import PermissionChecker
from ..utils.errors import raise_api_error

router = APIRouter()
logger = logging.getLogger(__name__)


async def _resolve_company_name(company_id: str) -> str:
    """Return the company name for a work center ('' if the company is gone)."""
    try:
        company = await db.Companies.find_one({"_id": ObjectId(company_id)})
    except Exception:
        company = None
    return company["name"] if company else ""


@router.post("/work-centers/", response_model=WorkCenterResponse, status_code=status.HTTP_201_CREATED)
async def create_work_center(
    work_center: WorkCenterCreate,
    current_user: APIUser = Depends(PermissionChecker("create_work_centers"))
):
    """
    Create a new work center (admin only).

    Validates that the company exists and is not deleted.
    """
    try:
        company = await db.Companies.find_one({
            "_id": ObjectId(work_center.company_id),
            "deleted_at": None
        })
    except Exception as e:
        logger.error(f"Error validating company {work_center.company_id}: {e}")
        company = None

    if not company:
        raise_api_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_code="work_center.company_not_found",
            message="La empresa no existe o ha sido eliminada",
        )

    work_center_data = work_center.model_dump()
    work_center_data["created_at"] = datetime.utcnow()
    work_center_data["updated_at"] = None
    work_center_data["deleted_at"] = None
    work_center_data["deleted_by"] = None

    try:
        result = await db.WorkCenters.insert_one(work_center_data)
        created = await db.WorkCenters.find_one({"_id": result.inserted_id})
        response_data = convert_id(created)
        response_data["company_name"] = company["name"]
        return WorkCenterResponse(**response_data)
    except Exception as e:
        logger.error(f"Error creating work center: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al crear el centro de trabajo"
        )


@router.get("/work-centers/", response_model=List[WorkCenterResponse])
async def get_work_centers(
    company_id: Optional[str] = Query(None, description="Filter by company ID"),
    current_user: APIUser = Depends(PermissionChecker("view_work_centers"))
):
    """
    List all work centers (admin/inspector only).

    By default, only returns active centers (not deleted).
    Can filter by company with company_id.
    """
    query = {"deleted_at": None}
    if company_id:
        query["company_id"] = company_id

    work_centers = []
    async for work_center in db.WorkCenters.find(query).sort("name", 1):
        response_data = convert_id(work_center)
        response_data["company_name"] = await _resolve_company_name(work_center["company_id"])
        work_centers.append(WorkCenterResponse(**response_data))

    return work_centers


@router.get("/work-centers/{work_center_id}", response_model=WorkCenterResponse)
async def get_work_center(
    work_center_id: str,
    current_user: APIUser = Depends(PermissionChecker("view_work_centers"))
):
    """
    Get a specific work center by ID (admin/inspector only).

    Returns 404 if work center doesn't exist or is deleted.
    """
    try:
        work_center = await db.WorkCenters.find_one({
            "_id": ObjectId(work_center_id),
            "deleted_at": None
        })
    except Exception as e:
        logger.error(f"Error fetching work center {work_center_id}: {e}")
        work_center = None

    if not work_center:
        raise_api_error(
            status_code=status.HTTP_404_NOT_FOUND,
            error_code="work_center.not_found",
            message="Centro de trabajo no encontrado",
        )

    response_data = convert_id(work_center)
    response_data["company_name"] = await _resolve_company_name(work_center["company_id"])
    return WorkCenterResponse(**response_data)


@router.put("/work-centers/{work_center_id}", response_model=WorkCenterResponse)
async def update_work_center(
    work_center_id: str,
    work_center_update: WorkCenterUpdate,
    current_user: APIUser = Depends(PermissionChecker("update_work_centers"))
):
    """
    Update a work center (admin only).

    Only name, code and address are editable; company_id is immutable.
    """
    try:
        work_center = await db.WorkCenters.find_one({
            "_id": ObjectId(work_center_id),
            "deleted_at": None
        })
    except Exception as e:
        logger.error(f"Error fetching work center {work_center_id}: {e}")
        work_center = None

    if not work_center:
        raise_api_error(
            status_code=status.HTTP_404_NOT_FOUND,
            error_code="work_center.not_found",
            message="Centro de trabajo no encontrado",
        )

    update_data = work_center_update.model_dump(exclude_unset=True)

    if not update_data:
        raise_api_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            error_code="work_center.no_update_data",
            message="No se proporcionaron datos para actualizar",
        )

    update_data["updated_at"] = datetime.utcnow()

    try:
        await db.WorkCenters.update_one(
            {"_id": ObjectId(work_center_id)},
            {"$set": update_data}
        )

        updated = await db.WorkCenters.find_one({"_id": ObjectId(work_center_id)})
        response_data = convert_id(updated)
        response_data["company_name"] = await _resolve_company_name(updated["company_id"])
        return WorkCenterResponse(**response_data)
    except Exception as e:
        logger.error(f"Error updating work center {work_center_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al actualizar el centro de trabajo"
        )


@router.delete("/work-centers/{work_center_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_work_center(
    work_center_id: str,
    current_user: APIUser = Depends(PermissionChecker("delete_work_centers"))
):
    """
    Soft delete a work center (admin only).

    Sets deleted_at/deleted_by and automatically removes the center from
    every worker's work_center_assignments. Time records keep their snapshot.
    """
    try:
        work_center = await db.WorkCenters.find_one({
            "_id": ObjectId(work_center_id),
            "deleted_at": None
        })
    except Exception as e:
        logger.error(f"Error fetching work center {work_center_id}: {e}")
        work_center = None

    if not work_center:
        raise_api_error(
            status_code=status.HTTP_404_NOT_FOUND,
            error_code="work_center.not_found",
            message="Centro de trabajo no encontrado",
        )

    try:
        # Order matters and is intentional (this stack has no transactions):
        # 1) auto-unassign first — if it fails, nothing changed and a retry is safe;
        # 2) soft-delete last — if unassign succeeded but the delete fails, the
        #    center still exists and the retry is idempotent.
        # This keeps the critical invariant "no worker assignment ever points to
        # a deleted center" true at all times.
        #
        # Auto-unassign: remove every work_center_assignments entry whose
        # value equals this center id (single pipeline update over Workers).
        assignment_filter = {
            "$expr": {
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
        }
        await db.Workers.update_many(
            assignment_filter,
            [
                {"$set": {
                    "work_center_assignments": {
                        "$arrayToObject": {
                            "$filter": {
                                "input": {"$objectToArray": "$work_center_assignments"},
                                "as": "entry",
                                "cond": {"$ne": ["$$entry.v", work_center_id]}
                            }
                        }
                    }
                }}
            ]
        )

        await db.WorkCenters.update_one(
            {"_id": ObjectId(work_center_id)},
            {"$set": {
                "deleted_at": datetime.utcnow(),
                "deleted_by": current_user.username
            }}
        )
        return None
    except Exception as e:
        logger.error(f"Error deleting work center {work_center_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al eliminar el centro de trabajo"
        )