from pydantic import BaseModel, EmailStr, Field
from typing import Optional, ClassVar, Literal, List
from datetime import datetime
from bson import ObjectId

# Simple function to convert MongoDB document to model-compatible format
def convert_mongodb_doc(document):
    if document:
        document["id"] = str(document.pop("_id"))
    return document

class APIUserBase(BaseModel):
    username: str
    email: EmailStr
    is_active: bool = True
    role: Literal["admin", "tracker"] = "tracker"  # Default role is tracker

class APIUserCreate(APIUserBase):
    password: str

class APIUser(APIUserBase):
    id: Optional[str] = None
    created_at: Optional[datetime] = None

    model_config: ClassVar[dict] = {
        "populate_by_name": True,
        "arbitrary_types_allowed": True,
        "json_encoders": {ObjectId: str}
    }

class APIUserInDB(APIUser):
    hashed_password: str
    # Password reset fields
    reset_token: Optional[str] = None
    reset_token_expires: Optional[datetime] = None
    reset_attempts: List[datetime] = Field(default_factory=list)

class Token(BaseModel):
    access_token: str
    token_type: str

class TokenData(BaseModel):
    username: Optional[str] = None

class ForgotPasswordRequest(BaseModel):
    email: EmailStr

class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=6)
