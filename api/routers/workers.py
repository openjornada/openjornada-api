from fastapi import APIRouter, HTTPException, status, Depends, Body
from typing import List
from datetime import datetime, timedelta
from passlib.context import CryptContext
from bson.objectid import ObjectId
import secrets
import logging

from ..models.workers import (
    WorkerModel,
    WorkerResponse,
    WorkerUpdateModel,
    ChangePasswordRequest,
    ForgotPasswordRequest,
    ResetPasswordRequest,
    WorkerCompaniesRequest
)
from ..models.auth import APIUser
from ..database import db, convert_id
from ..auth.auth_handler import get_current_active_user, get_password_hash, verify_password
from ..auth.permissions import PermissionChecker
from ..services.email_service import email_service

router = APIRouter()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
logger = logging.getLogger(__name__)

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
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"La empresa con ID {company_id} no existe o ha sido eliminada"
                    )
            except Exception as e:
                logger.error(f"Error validating company {company_id}: {e}")
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"ID de empresa inválido: {company_id}"
                )

    # Check if email or id_number already exists
    if await db.Workers.find_one({"$or": [
        {"email": worker.email},
        {"id_number": worker.id_number}
    ]}):
        # Determine which field is duplicated for a better error message
        if await db.Workers.find_one({"email": worker.email}):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="ID number (DNI) already registered"
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
        contact_email = settings.get("contact_email", "support@openjornada.local") if settings else "support@openjornada.local"
        import os
        webapp_url = os.getenv("WEBAPP_URL", "http://localhost:5173")
       
        worker_name = f"{created_worker.get('first_name', '')} {created_worker.get('last_name', '')}".strip() or "Usuario"
        
        try:
            await email_service.send_welcome_email(
                to_email=created_worker["email"],
                worker_name=worker_name,
                reset_token=reset_token,
                webapp_url=webapp_url,
                contact_email=contact_email
            )
        except Exception as e:
            logger.error(f"[CREATE-WORKER] Error sending welcome email: {type(e).__name__}: {e}")
            import traceback
            logger.error(traceback.format_exc())

    # Get company names for response
    company_names = []
    for company_id in created_worker.get("company_ids", []):
        try:
            company = await db.Companies.find_one({"_id": ObjectId(company_id)})
            if company:
                company_names.append(company["name"])
        except:
            pass

    response_data = convert_id(created_worker)
    response_data["company_names"] = company_names

    return WorkerResponse(**response_data)

@router.put("/workers/{worker_id}", response_model=WorkerResponse)
async def update_worker(
    worker_id: str,
    worker_update: WorkerUpdateModel,
    current_user: APIUser = Depends(PermissionChecker("update_workers"))
):
    try:
        worker = await db.Workers.find_one({"_id": ObjectId(worker_id), "deleted_at": None})
    except:
        worker = None

    if not worker:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Worker not found"
        )

    # Prepare update data
    update_data = worker_update.model_dump(exclude_unset=True)

    # If company_ids is being updated, validate
    if "company_ids" in update_data:
        company_ids = update_data["company_ids"]

        # Must have at least 1 company
        if not company_ids or len(company_ids) == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="El trabajador debe estar asociado a al menos una empresa"
            )

        # Validate all companies exist and are not deleted
        for company_id in company_ids:
            try:
                company = await db.Companies.find_one({
                    "_id": ObjectId(company_id),
                    "deleted_at": None
                })
                if not company:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"La empresa con ID {company_id} no existe o ha sido eliminada"
                    )
            except Exception as e:
                logger.error(f"Error validating company {company_id}: {e}")
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"ID de empresa inválido: {company_id}"
                )

    # If email is being updated, check if it's already taken
    if "email" in update_data and update_data["email"] != worker["email"]:
        if await db.Workers.find_one({"email": update_data["email"]}):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )

    # If id_number is being updated, check if it's already taken
    if "id_number" in update_data and update_data["id_number"] != worker["id_number"]:
        if await db.Workers.find_one({"id_number": update_data["id_number"]}):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="ID number (DNI) already registered"
            )

    # Handle password update
    if "password" in update_data:
        hashed_password = get_password_hash(update_data["password"])
        update_data["hashed_password"] = hashed_password
        del update_data["password"]

    # Update last modified
    update_data["updated_at"] = datetime.utcnow()
    update_data["updated_by"] = current_user.username

    # Update the worker
    await db.Workers.update_one(
        {"_id": ObjectId(worker_id)},
        {"$set": update_data}
    )

    updated_worker = await db.Workers.find_one({"_id": ObjectId(worker_id)})

    # Get company names for response
    company_names = []
    for company_id in updated_worker.get("company_ids", []):
        try:
            company = await db.Companies.find_one({"_id": ObjectId(company_id)})
            if company:
                company_names.append(company["name"])
        except:
            pass

    response_data = convert_id(updated_worker)
    response_data["company_names"] = company_names

    return WorkerResponse(**response_data)

@router.get("/workers/", response_model=List[WorkerResponse])
async def get_workers(current_user: APIUser = Depends(PermissionChecker("view_workers"))):
    workers = []
    # Exclude deleted workers
    async for worker in db.Workers.find({"deleted_at": None}):
        # Get company names for each worker
        company_names = []
        for company_id in worker.get("company_ids", []):
            try:
                company = await db.Companies.find_one({"_id": ObjectId(company_id)})
                if company:
                    company_names.append(company["name"])
            except:
                pass

        worker_data = convert_id(worker)
        worker_data["company_names"] = company_names
        workers.append(WorkerResponse(**worker_data))
    return workers

@router.get("/workers/{worker_id}", response_model=WorkerResponse)
async def get_worker(
    worker_id: str,
    current_user: APIUser = Depends(PermissionChecker("view_workers"))
):
    try:
        worker = await db.Workers.find_one({"_id": ObjectId(worker_id), "deleted_at": None})
    except:
        worker = None

    if not worker:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Worker not found"
        )

    # Get company names
    company_names = []
    for company_id in worker.get("company_ids", []):
        try:
            company = await db.Companies.find_one({"_id": ObjectId(company_id)})
            if company:
                company_names.append(company["name"])
        except:
            pass

    worker_data = convert_id(worker)
    worker_data["company_names"] = company_names

    return WorkerResponse(**worker_data)

@router.get("/workers/id_number/{id_number}", response_model=WorkerResponse)
async def get_worker_by_id_number(
    id_number: str,
    current_user: APIUser = Depends(PermissionChecker("view_workers"))
):
    worker = await db.Workers.find_one({"id_number": id_number, "deleted_at": None})
    if not worker:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Worker not found"
        )

    return WorkerResponse(**convert_id(worker))

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
    except:
        worker = None

    if not worker:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Worker not found"
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
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials"
        )

    # Verify current password
    if not verify_password(request.current_password, worker["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials"
        )

    # Validate new password is not empty/whitespace only
    if not request.new_password.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password cannot be empty"
        )

    # Validate new password is different from current password
    if verify_password(request.new_password, worker["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must be different from current password"
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
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Demasiados intentos de restablecimiento. Por favor, espera una hora antes de intentarlo de nuevo."
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
        contact_email = settings.get("contact_email", "support@openjornada.local") if settings else "support@openjornada.local"

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
            email_result = await email_service.send_password_reset_email(
                to_email=request.email,
                worker_name=worker_name,
                reset_token=reset_token,
                webapp_url=webapp_url,
                contact_email=contact_email
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
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token inválido o expirado"
        )

    # Check if token is expired
    reset_token_expires = worker.get("reset_token_expires")
    if not reset_token_expires or reset_token_expires < datetime.utcnow():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token inválido o expirado"
        )

    # Validate new password
    if not request.new_password or not request.new_password.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La contraseña no puede estar vacía"
        )

    if len(request.new_password) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La contraseña debe tener al menos 6 caracteres"
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
async def get_worker_companies(request: WorkerCompaniesRequest):
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
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Credenciales inválidas"
            )

        # Verify password
        if not verify_password(request.password, worker.get("hashed_password", "")):
            logger.info(f"[MY-COMPANIES] Invalid password for: {request.email}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Credenciales inválidas"
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
                        "updated_at": company.get("updated_at")
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
