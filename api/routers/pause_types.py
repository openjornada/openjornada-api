from fastapi import APIRouter, HTTPException, status, Depends, Query
from datetime import datetime
from typing import List, Optional
from bson.objectid import ObjectId
import logging

from ..models.pause_types import (
    PauseTypeCreate,
    PauseTypeUpdate,
    PauseTypeResponse,
    PauseTypeInDB,
    AvailablePausesRequest,
    AvailablePauseResponse
)
from ..models.auth import APIUser
from ..database import db, convert_id
from ..auth.permissions import PermissionChecker
from ..auth.auth_handler import verify_password

router = APIRouter()
logger = logging.getLogger(__name__)

@router.post("/pause-types/", response_model=PauseTypeResponse, status_code=status.HTTP_201_CREATED)
async def create_pause_type(
    pause_type: PauseTypeCreate,
    current_user: APIUser = Depends(PermissionChecker("manage_pause_types"))
):
    """
    Crear nuevo tipo de pausa.
    Solo admins.
    """
    # Validar que las empresas existen
    for company_id in pause_type.company_ids:
        try:
            company = await db.Companies.find_one({
                "_id": ObjectId(company_id),
                "deleted_at": None
            })
            if not company:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"La empresa {company_id} no existe o ha sido eliminada"
                )
        except Exception as e:
            logger.error(f"Error validating company {company_id}: {e}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"ID de empresa inválido: {company_id}"
            )

    # Crear documento
    pause_type_data = PauseTypeInDB(
        **pause_type.dict(),
        created_at=datetime.utcnow(),
        created_by=current_user.username
    )

    result = await db.PauseTypes.insert_one(pause_type_data.dict())
    created = await db.PauseTypes.find_one({"_id": result.inserted_id})

    return await enrich_pause_type_response(created)

@router.get("/pause-types/", response_model=List[PauseTypeResponse])
async def get_pause_types(
    include_deleted: bool = Query(False, description="Incluir tipos eliminados"),
    company_id: Optional[str] = Query(None, description="Filtrar por empresa"),
    current_user: APIUser = Depends(PermissionChecker("view_pause_types"))
):
    """
    Listar tipos de pausas.
    Admins y trackers.
    """
    query = {}

    if not include_deleted:
        query["deleted_at"] = None

    if company_id:
        query["company_ids"] = company_id

    pause_types = await db.PauseTypes.find(query).sort("name", 1).to_list(1000)

    return [await enrich_pause_type_response(pt) for pt in pause_types]

@router.get("/pause-types/{pause_type_id}", response_model=PauseTypeResponse)
async def get_pause_type(
    pause_type_id: str,
    current_user: APIUser = Depends(PermissionChecker("view_pause_types"))
):
    """
    Obtener tipo de pausa por ID.
    """
    try:
        pause_type = await db.PauseTypes.find_one({"_id": ObjectId(pause_type_id)})
    except:
        pause_type = None

    if not pause_type:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tipo de pausa no encontrado"
        )

    return await enrich_pause_type_response(pause_type)

@router.put("/pause-types/{pause_type_id}", response_model=PauseTypeResponse)
async def update_pause_type(
    pause_type_id: str,
    pause_type_update: PauseTypeUpdate,
    current_user: APIUser = Depends(PermissionChecker("manage_pause_types"))
):
    """
    Actualizar tipo de pausa.
    Solo admins.

    IMPORTANTE: No se puede modificar el campo 'type' si hay registros usando este tipo.
    """
    try:
        existing = await db.PauseTypes.find_one({"_id": ObjectId(pause_type_id)})
    except:
        existing = None

    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tipo de pausa no encontrado"
        )

    if existing.get("deleted_at"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No se puede editar un tipo de pausa eliminado"
        )

    # Verificar si hay registros usando este tipo
    usage_count = await db.TimeRecords.count_documents({
        "pause_type_id": pause_type_id
    })

    if usage_count > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No se puede editar el tipo de pausa porque hay {usage_count} registros usándolo. Solo puedes editar el nombre, empresas o descripción."
        )

    # Validar empresas si se están actualizando
    if pause_type_update.company_ids is not None:
        for company_id in pause_type_update.company_ids:
            try:
                company = await db.Companies.find_one({
                    "_id": ObjectId(company_id),
                    "deleted_at": None
                })
                if not company:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"La empresa {company_id} no existe"
                    )
            except Exception as e:
                logger.error(f"Error validating company {company_id}: {e}")
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"ID de empresa inválido: {company_id}"
                )

    # Preparar actualización
    update_data = {
        k: v for k, v in pause_type_update.dict(exclude_unset=True).items()
        if v is not None
    }
    update_data["updated_at"] = datetime.utcnow()

    # Actualizar
    await db.PauseTypes.update_one(
        {"_id": ObjectId(pause_type_id)},
        {"$set": update_data}
    )

    updated = await db.PauseTypes.find_one({"_id": ObjectId(pause_type_id)})
    return await enrich_pause_type_response(updated)

@router.delete("/pause-types/{pause_type_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pause_type(
    pause_type_id: str,
    current_user: APIUser = Depends(PermissionChecker("manage_pause_types"))
):
    """
    Eliminar tipo de pausa (soft delete).
    Solo admins.

    Los registros existentes mantienen el nombre del tipo de pausa.
    """
    try:
        pause_type = await db.PauseTypes.find_one({"_id": ObjectId(pause_type_id)})
    except:
        pause_type = None

    if not pause_type:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tipo de pausa no encontrado"
        )

    if pause_type.get("deleted_at"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Este tipo de pausa ya ha sido eliminado"
        )

    # Soft delete
    await db.PauseTypes.update_one(
        {"_id": ObjectId(pause_type_id)},
        {
            "$set": {
                "deleted_at": datetime.utcnow(),
                "deleted_by": current_user.username
            }
        }
    )

    logger.info(f"Pause type {pause_type_id} deleted by {current_user.username}")

@router.post("/pause-types/available", response_model=List[AvailablePauseResponse])
async def get_available_pause_types(request: AvailablePausesRequest):
    """
    Obtener tipos de pausas disponibles para un trabajador en una empresa específica.
    Endpoint público - autenticación con email/password del worker.

    Este endpoint NO requiere JWT token, solo credenciales del worker.
    """
    # 1. Autenticar worker
    worker = await db.Workers.find_one({
        "email": request.email,
        "deleted_at": None
    })

    if not worker or not verify_password(request.password, worker.get("hashed_password", "")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales inválidas"
        )

    # 2. Verificar que worker tiene acceso a la empresa
    worker_company_ids = [str(cid) for cid in worker.get("company_ids", [])]
    if request.company_id not in worker_company_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes acceso a esta empresa"
        )

    # 3. Obtener tipos de pausas de esta empresa
    pause_types = await db.PauseTypes.find({
        "company_ids": request.company_id,
        "deleted_at": None
    }).sort("name", 1).to_list(100)

    # 4. Formatear respuesta
    return [
        AvailablePauseResponse(
            id=str(pt["_id"]),
            name=pt["name"],
            type=pt["type"],
            description=pt.get("description"),
            counts_as_work=(pt["type"] == "inside_shift")
        )
        for pt in pause_types
    ]

async def enrich_pause_type_response(pause_type: dict) -> PauseTypeResponse:
    """
    Enriquecer respuesta con nombres de empresas y metadata.
    """
    # Resolver nombres de empresas
    company_names = []
    for company_id in pause_type.get("company_ids", []):
        try:
            company = await db.Companies.find_one({"_id": ObjectId(company_id), "deleted_at": None})
            if company:
                company_names.append(company["name"])
        except:
            pass

    # Contar uso
    usage_count = await db.TimeRecords.count_documents({
        "pause_type_id": str(pause_type["_id"])
    })

    return PauseTypeResponse(
        **convert_id(pause_type),
        company_names=company_names,
        can_edit_type=(usage_count == 0),
        usage_count=usage_count
    )
