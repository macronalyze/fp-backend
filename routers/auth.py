import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr

from auth import create_jwt, get_current_user, verify_google_token
from db import get_db

router = APIRouter(prefix="/auth", tags=["auth"])


def _dev_auth_enabled() -> bool:
    """Dev login is allowed only when DEV_AUTH=1 and we're not in production."""
    if os.environ.get("ENV", "").lower() == "production":
        return False
    return os.environ.get("DEV_AUTH", "").strip() in ("1", "true", "yes")


class GoogleLoginRequest(BaseModel):
    token: str


class DevLoginRequest(BaseModel):
    email: EmailStr
    name: str | None = None
    role: str = "user"  # "user" or "admin"


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict


class UserResponse(BaseModel):
    email: str
    name: str
    picture: str | None = None
    role: str


@router.post("/google", response_model=AuthResponse)
async def google_login(body: GoogleLoginRequest):
    """Verify Google ID token, create/fetch user, return JWT."""
    idinfo = verify_google_token(body.token)

    db = get_db()
    users = db["users"]

    user = users.find_one({"google_id": idinfo["sub"]})
    if not user:
        user = {
            "google_id": idinfo["sub"],
            "email": idinfo["email"],
            "name": idinfo.get("name", ""),
            "picture": idinfo.get("picture", ""),
            "role": "user",
            "created_at": datetime.now(timezone.utc),
        }
        users.insert_one(user)

    token = create_jwt(user)
    return AuthResponse(
        access_token=token,
        user={
            "email": user["email"],
            "name": user["name"],
            "picture": user.get("picture", ""),
            "role": user["role"],
        },
    )


@router.get("/me", response_model=UserResponse)
async def get_me(user: dict = Depends(get_current_user)):
    """Return current authenticated user info."""
    return UserResponse(
        email=user["email"],
        name=user["name"],
        picture=user.get("picture"),
        role=user["role"],
    )


@router.post("/dev-login", response_model=AuthResponse)
async def dev_login(body: DevLoginRequest):
    """
    Dev-only login bypass. Issues a JWT for an arbitrary email without
    going through Google. Enabled only when DEV_AUTH=1 and ENV != production.
    """
    if not _dev_auth_enabled():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Not found"
        )

    if body.role not in ("user", "admin"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="role must be 'user' or 'admin'",
        )

    db = get_db()
    users = db["users"]

    # Use a synthetic google_id so the existing JWT/lookup flow works unchanged.
    dev_google_id = f"dev:{body.email}"
    user = users.find_one({"google_id": dev_google_id})
    if not user:
        user = {
            "google_id": dev_google_id,
            "email": body.email,
            "name": body.name or body.email.split("@")[0],
            "picture": "",
            "role": body.role,
            "created_at": datetime.now(timezone.utc),
            "dev": True,
        }
        users.insert_one(user)
    elif user.get("role") != body.role:
        # Allow role switching across dev sessions.
        users.update_one(
            {"google_id": dev_google_id}, {"$set": {"role": body.role}}
        )
        user["role"] = body.role

    token = create_jwt(user)
    return AuthResponse(
        access_token=token,
        user={
            "email": user["email"],
            "name": user["name"],
            "picture": user.get("picture", ""),
            "role": user["role"],
        },
    )
