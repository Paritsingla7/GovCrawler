"""
Minimal user administration endpoints (Phase 0 — just enough to provision the
handful of operators; a full admin UI lands in a later phase).

Registers routes:
  GET  /api/admin/users                        → list users
  POST /api/admin/users                         → create a user
  PATCH /api/admin/users/{id}                   → set active/role
  POST /api/admin/users/{id}/reset-password     → admin sets a new password
  GET  /api/admin/roles                         → list roles
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .deps import CurrentUser, get_db, require
from ..db import Database

router = APIRouter(tags=["admin"], dependencies=[Depends(require("users.manage"))])


class UserCreate(BaseModel):
    email: str
    password: str
    full_name: str | None = None
    role: str | None = None
    is_admin: bool = False


class UserPatch(BaseModel):
    is_active: bool | None = None
    role: str | None = None


class PasswordReset(BaseModel):
    password: str


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.get("/api/admin/users")
async def list_users(db: Database = Depends(get_db)):
    users = db.list_users()
    for u in users:
        u["role"] = db.get_role_name(u["role_id"])
    return users


@router.post("/api/admin/users", status_code=201)
async def create_user(req: UserCreate, request: Request, db: Database = Depends(get_db),
                      user: CurrentUser = Depends(require("users.manage"))):
    if db.get_user_by_email(req.email):
        raise HTTPException(status_code=409, detail="A user with this email already exists")
    user_id = db.create_user(
        email=req.email, password=req.password, full_name=req.full_name,
        is_admin=req.is_admin, role_name=req.role, created_by=user.id,
    )
    db.write_audit(user.id, "user.create", "user", user_id, ip=_client_ip(request))
    return {"id": user_id, "message": "User created"}


@router.patch("/api/admin/users/{user_id}")
async def patch_user(user_id: int, req: UserPatch, request: Request, db: Database = Depends(get_db),
                     user: CurrentUser = Depends(require("users.manage"))):
    if req.is_active is not None:
        if not db.set_user_active(user_id, req.is_active):
            raise HTTPException(status_code=404, detail="User not found")
        db.write_audit(user.id, "user.set_active", "user", user_id,
                       detail={"is_active": req.is_active}, ip=_client_ip(request))
    if req.role is not None:
        if not db.set_user_role(user_id, req.role):
            raise HTTPException(status_code=404, detail="User or role not found")
        db.write_audit(user.id, "user.set_role", "user", user_id,
                       detail={"role": req.role}, ip=_client_ip(request))
    return {"message": "User updated"}


@router.post("/api/admin/users/{user_id}/reset-password")
async def reset_password(user_id: int, req: PasswordReset, request: Request, db: Database = Depends(get_db),
                         user: CurrentUser = Depends(require("users.manage"))):
    if not db.set_password(user_id, req.password):
        raise HTTPException(status_code=404, detail="User not found")
    db.write_audit(user.id, "user.reset_password", "user", user_id, ip=_client_ip(request))
    return {"message": "Password reset"}


@router.get("/api/admin/roles")
async def list_roles(db: Database = Depends(get_db)):
    return db.list_roles()
