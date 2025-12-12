from fastapi import APIRouter, HTTPException, status, Depends, Query
from typing import List
from datetime import datetime
from bson.objectid import ObjectId
import logging

from ..models.companies import CompanyCreate, CompanyUpdate, CompanyResponse
from ..models.auth import APIUser
from ..database import db, convert_id
from ..auth.permissions import PermissionChecker

router = APIRouter()
logger = logging.getLogger(__name__)

@router.post("/companies/", response_model=CompanyResponse, status_code=status.HTTP_201_CREATED)
async def create_company(
    company: CompanyCreate,
    current_user: APIUser = Depends(PermissionChecker("create_companies"))
):
    """
    Create a new company (admin only).

    Validates that company name is unique (including deleted companies).
    """
    # Check if company name already exists (including deleted ones)
    existing_company = await db.Companies.find_one({"name": company.name})

    if existing_company:
        # Check if it's a deleted company
        if existing_company.get("deleted_at") is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Ya existe una empresa con el nombre '{company.name}'"
            )

    # Create company document
    company_data = company.model_dump()
    company_data["created_at"] = datetime.utcnow()
    company_data["updated_at"] = None
    company_data["deleted_at"] = None
    company_data["deleted_by"] = None

    try:
        result = await db.Companies.insert_one(company_data)
        created_company = await db.Companies.find_one({"_id": result.inserted_id})
        return CompanyResponse(**convert_id(created_company))
    except Exception as e:
        logger.error(f"Error creating company: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al crear la empresa"
        )

@router.get("/companies/", response_model=List[CompanyResponse])
async def get_companies(
    include_deleted: bool = Query(False, description="Include deleted companies"),
    current_user: APIUser = Depends(PermissionChecker("view_companies"))
):
    """
    List all companies (admin only).

    By default, only returns active companies (not deleted).
    Can include deleted companies with include_deleted=true.

    Note: Workers should use POST /api/workers/my-companies to get their associated companies.
    """

    # Build query
    query = {} if include_deleted else {"deleted_at": None}

    # Get companies sorted alphabetically by name
    companies = []
    async for company in db.Companies.find(query).sort("name", 1):
        companies.append(CompanyResponse(**convert_id(company)))

    return companies

@router.get("/companies/{company_id}", response_model=CompanyResponse)
async def get_company(
    company_id: str,
    current_user: APIUser = Depends(PermissionChecker("view_companies"))
):
    """
    Get a specific company by ID (admin only).

    Returns 404 if company doesn't exist or is deleted.
    """
    try:
        company = await db.Companies.find_one({
            "_id": ObjectId(company_id),
            "deleted_at": None
        })
    except Exception as e:
        logger.error(f"Error fetching company {company_id}: {e}")
        company = None

    if not company:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Empresa no encontrada"
        )

    return CompanyResponse(**convert_id(company))

@router.patch("/companies/{company_id}", response_model=CompanyResponse)
async def update_company(
    company_id: str,
    company_update: CompanyUpdate,
    current_user: APIUser = Depends(PermissionChecker("update_companies"))
):
    """
    Update a company (admin only).

    Validates that new name is unique if changed.
    """
    # Check if company exists and is not deleted
    try:
        company = await db.Companies.find_one({
            "_id": ObjectId(company_id),
            "deleted_at": None
        })
    except Exception as e:
        logger.error(f"Error fetching company {company_id}: {e}")
        company = None

    if not company:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Empresa no encontrada"
        )

    # Prepare update data
    update_data = company_update.model_dump(exclude_unset=True)

    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se proporcionaron datos para actualizar"
        )

    # If name is being updated, check uniqueness
    if "name" in update_data and update_data["name"] != company["name"]:
        existing = await db.Companies.find_one({"name": update_data["name"]})
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Ya existe una empresa con el nombre '{update_data['name']}'"
            )

    # Update timestamp
    update_data["updated_at"] = datetime.utcnow()

    try:
        await db.Companies.update_one(
            {"_id": ObjectId(company_id)},
            {"$set": update_data}
        )

        updated_company = await db.Companies.find_one({"_id": ObjectId(company_id)})
        return CompanyResponse(**convert_id(updated_company))
    except Exception as e:
        logger.error(f"Error updating company {company_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al actualizar la empresa"
        )

@router.delete("/companies/{company_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_company(
    company_id: str,
    current_user: APIUser = Depends(PermissionChecker("delete_companies"))
):
    """
    Soft delete a company (admin only).

    Validates that company has no associated workers before deletion.
    Sets deleted_at timestamp and deleted_by username.
    """
    # Check if company exists and is not deleted
    try:
        company = await db.Companies.find_one({
            "_id": ObjectId(company_id),
            "deleted_at": None
        })
    except Exception as e:
        logger.error(f"Error fetching company {company_id}: {e}")
        company = None

    if not company:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Empresa no encontrada"
        )

    # CRITICAL: Check if company has associated workers
    workers_count = await db.Workers.count_documents({
        "company_ids": company_id,
        "deleted_at": None
    })

    if workers_count > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se puede eliminar una empresa que tiene trabajadores asociados"
        )


    original_name = company.get("name")
    new_name = f"{original_name}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    # Soft delete: set deleted_at and deleted_by
    try:
        await db.Companies.update_one(
            {"_id": ObjectId(company_id)},
            {"$set": {
                "name": new_name,
                "deleted_at": datetime.utcnow(),
                "deleted_by": current_user.username
            }}
        )
        return None
    except Exception as e:
        logger.error(f"Error deleting company {company_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al eliminar la empresa"
        )
