from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from datetime import timedelta, datetime
import secrets
import logging
import os

from ..auth.auth_handler import (
    authenticate_user,
    create_access_token,
    get_password_hash,
    ACCESS_TOKEN_EXPIRE_MINUTES,
    get_current_active_user
)
from ..auth.permissions import PermissionChecker
from ..models.auth import Token, APIUserCreate, APIUser, ForgotPasswordRequest, ResetPasswordRequest
from ..database import db, convert_id
from ..services.email_service import email_service

router = APIRouter()
logger = logging.getLogger(__name__)

@router.post("/token", response_model=Token)
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends()):
    # Note: form_data.username can contain either username or email
    user = await authenticate_user(form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.username}, expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer"}

@router.post("/users/", response_model=APIUser, dependencies=[Depends(PermissionChecker("create_users"))])
async def create_user(user: APIUserCreate, current_user: APIUser = Depends(get_current_active_user)):
    # Check if username or email already exists
    db_user = await db.APIUsers.find_one(
        {"$or": [{"username": user.username}, {"email": user.email}]}
    )
    if db_user:
        raise HTTPException(
            status_code=400,
            detail="Username or email already registered"
        )
    
    # Create new user
    hashed_password = get_password_hash(user.password)
    user_data = user.model_dump()
    user_data.pop("password", None)
    user_data["hashed_password"] = hashed_password
    user_data["created_at"] = datetime.utcnow()
    
    result = await db.APIUsers.insert_one(user_data)
    
    created_user = await db.APIUsers.find_one({"_id": result.inserted_id})
    return APIUser(**convert_id(created_user))

@router.get("/users/me", response_model=APIUser)
async def read_users_me(current_user: APIUser = Depends(get_current_active_user)):
    return current_user

@router.get("/users/", response_model=list[APIUser], dependencies=[Depends(PermissionChecker("view_users"))])
async def list_users(current_user: APIUser = Depends(get_current_active_user)):
    users = []
    async for user in db.APIUsers.find():
        users.append(APIUser(**convert_id(user)))
    return users

@router.post("/forgot-password", status_code=status.HTTP_200_OK)
async def forgot_password(request: ForgotPasswordRequest):
    """
    Request password reset email for API users.

    Public endpoint (no authentication required).
    Always returns success message regardless of whether email exists (security best practice).
    Rate limited to 3 attempts per hour per user.
    """
    logger.info(f"[FORGOT-PASSWORD] Request received for email: {request.email}")

    # Generic success message (don't reveal if email exists)
    success_message = {
        "message": "Si el email existe, recibirás instrucciones para restablecer tu contraseña"
    }

    try:
        # Find user by email
        logger.info(f"[FORGOT-PASSWORD] Searching for API user with email: {request.email}")
        user = await db.APIUsers.find_one({"email": request.email})

        # If user doesn't exist, return success message anyway (security)
        if not user:
            logger.info(f"[FORGOT-PASSWORD] API user not found for email: {request.email}")
            return success_message

        logger.info(f"[FORGOT-PASSWORD] API user found: {user.get('username', '')}")

        # Check rate limit: count reset attempts in last hour
        one_hour_ago = datetime.utcnow() - timedelta(hours=1)
        recent_attempts = user.get("reset_attempts", [])

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

        # Update user with reset token and cleaned attempts list
        await db.APIUsers.update_one(
            {"_id": user["_id"]},
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
        admin_url = os.getenv("ADMIN_URL", "http://localhost:3001")
        logger.info(f"[FORGOT-PASSWORD] Settings - Admin URL: {admin_url}, Contact Email: {contact_email}")

        # Get username
        username = user.get("username", "Usuario")
        logger.info(f"[FORGOT-PASSWORD] Username: {username}")

        # Send reset email (don't wait for result, catch errors silently)
        try:
            logger.info(f"[FORGOT-PASSWORD] Calling email service to send reset email to: {request.email}")
            email_result = await email_service.send_admin_password_reset_email(
                to_email=request.email,
                username=username,
                reset_token=reset_token,
                admin_url=admin_url,
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
        logger.error(f"[FORGOT-PASSWORD] Error in forgot_password: {e}")
        return success_message


@router.post("/reset-password", status_code=status.HTTP_200_OK)
async def reset_password(request: ResetPasswordRequest):
    """
    Reset password using token from email for API users.

    Public endpoint (no authentication required).
    Token must be valid and not expired.
    """
    # Find user by reset token
    user = await db.APIUsers.find_one({
        "reset_token": request.token
    })

    # Check if token exists
    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token inválido o expirado"
        )

    # Check if token is expired
    reset_token_expires = user.get("reset_token_expires")
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

    # Update user: set new password, clear reset token
    result = await db.APIUsers.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "hashed_password": new_hashed_password
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
