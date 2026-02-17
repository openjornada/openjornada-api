from typing import List
from fastapi import HTTPException, status, Depends
from ..models.auth import APIUser
from .auth_handler import get_current_active_user

# Define permissions for each role
ROLE_PERMISSIONS = {
    "admin": [
        "create_users",
        "view_users",
        "create_workers",
        "view_workers",
        "update_workers",
        "delete_workers",
        "create_time_records",
        "view_all_time_records",
        "view_worker_time_records",
        "manage_pause_types",
        "view_pause_types",
        "view_change_requests",
        "manage_change_requests",
        "create_change_requests",
        "create_companies",
        "view_companies",
        "update_companies",
        "delete_companies",
        "view_incidents",
        "manage_incidents",
        "view_settings",
        "update_settings",
        "view_backups",
        "manage_backups",
        "view_reports",
        "export_reports",
        "manage_inspection"
    ],
    "inspector": [
        "view_reports",
        "export_reports",
        "view_companies",
    ],
    "tracker": [
        "create_time_records",
        "create_change_requests",
        "view_pause_types"
    ]
}

def has_permission(user: APIUser, permission: str) -> bool:
    """Check if user has a specific permission based on their role"""
    user_permissions = ROLE_PERMISSIONS.get(user.role, [])
    return permission in user_permissions

class PermissionChecker:
    """Dependency class to check permissions"""
    def __init__(self, required_permission: str):
        self.required_permission = required_permission
    
    async def __call__(self, current_user: APIUser = Depends(get_current_active_user)):
        if not has_permission(current_user, self.required_permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Not enough permissions. Required: {self.required_permission}"
            )
        return current_user

# Convenience functions for common permissions
def require_admin(current_user: APIUser = Depends(get_current_active_user)):
    """Dependency that requires admin role"""
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
    return current_user
