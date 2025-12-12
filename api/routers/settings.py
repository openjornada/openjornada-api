from fastapi import APIRouter, HTTPException, status, Depends

from ..models.settings import SettingsResponse, SettingsUpdate
from ..models.auth import APIUser
from ..database import db, convert_id
from ..auth.permissions import PermissionChecker

router = APIRouter()


@router.get("/settings/", response_model=SettingsResponse)
async def get_settings(current_user: APIUser = Depends(PermissionChecker("view_settings"))):
    """
    Get application settings. Creates default settings if they don't exist.
    Admin only.
    """
    # Find settings document
    settings = await db.Settings.find_one()

    # If no settings exist, create default
    if not settings:
        default_settings = {
            "contact_email": "support@opentracker.local"
        }
        result = await db.Settings.insert_one(default_settings)
        settings = await db.Settings.find_one({"_id": result.inserted_id})

    return SettingsResponse(**convert_id(settings))


@router.patch("/settings/", response_model=SettingsResponse)
async def update_settings(
    settings_update: SettingsUpdate,
    current_user: APIUser = Depends(PermissionChecker("update_settings"))
):
    """
    Update application settings (partial update).
    Admin only.
    """
    # Get current settings (create if not exists)
    settings = await db.Settings.find_one()

    if not settings:
        # Create default settings first
        default_settings = {
            "contact_email": "support@opentracker.local"
        }
        result = await db.Settings.insert_one(default_settings)
        settings = await db.Settings.find_one({"_id": result.inserted_id})

    # Prepare update data (exclude unset fields)
    update_data = settings_update.model_dump(exclude_unset=True)

    if not update_data:
        # No fields to update
        return SettingsResponse(**convert_id(settings))

    # Update settings
    await db.Settings.update_one(
        {"_id": settings["_id"]},
        {"$set": update_data}
    )

    # Return updated settings
    updated_settings = await db.Settings.find_one({"_id": settings["_id"]})
    return SettingsResponse(**convert_id(updated_settings))
