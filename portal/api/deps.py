"""
Shared application state and FastAPI dependency providers.

State is set once in server.create_app()/lifespan() at startup. Route
handlers pull it via Depends(...) instead of capturing it through closures —
this lets each route module be imported and tested independently of app
construction.
"""
import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import jwt as pyjwt
from fastapi import Depends, HTTPException, Request

from ..db import Database
from ..security.jwt import decode_token

_db: Database | None = None
_config: dict | None = None
_config_path: Path | None = None
_browser = None
_playwright_instance = None
_active_tasks: dict[int, asyncio.Task] = {}


def get_db() -> Database:
    return _db


def get_config() -> dict:
    return _config


def get_config_path() -> Path:
    return _config_path


def get_browser():
    return _browser


def get_active_tasks() -> dict[int, asyncio.Task]:
    return _active_tasks


# ── Auth ──────────────────────────────────────────────────────────────────────

class RedirectException(Exception):
    """Raised by page-auth dependencies to bounce an unauthenticated browser
    request to /login. Handled by an app-level exception handler
    (see server.create_app), since Depends() can't itself return a redirect."""

    def __init__(self, location: str = "/login"):
        self.location = location


@dataclass
class CurrentUser:
    id: int
    email: str
    is_admin: bool
    permissions: set[str] = field(default_factory=set)

    def can(self, perm: str) -> bool:
        return self.is_admin or perm in self.permissions

    def has_all(self, perms: tuple[str, ...]) -> bool:
        return self.is_admin or all(p in self.permissions for p in perms)


def _extract_token(request: Request) -> str | None:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[len("Bearer "):]
    return request.cookies.get("access")


def get_current_user(request: Request) -> CurrentUser:
    token = _extract_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    secret = _config["auth"]["jwt_secret"]
    try:
        payload = decode_token(token, secret)
    except pyjwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Invalid token type")

    user = _db.get_user_by_id(int(payload["sub"]))
    if not user or not user["is_active"]:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    if payload.get("tv") != user["token_version"]:
        raise HTTPException(status_code=401, detail="Token has been revoked")

    permissions = _db.resolve_effective_permissions(user["id"])
    return CurrentUser(id=user["id"], email=user["email"], is_admin=user["is_admin"], permissions=permissions)


def current_user_or_redirect(request: Request) -> CurrentUser:
    try:
        return get_current_user(request)
    except HTTPException:
        raise RedirectException("/login")


def require(*perms: str):
    def dep(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if not user.has_all(perms):
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user
    return dep


_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def require_loopback(request: Request):
    host = request.client.host if request.client else None
    if host not in _LOOPBACK_HOSTS:
        raise HTTPException(status_code=403, detail="This endpoint is only reachable from localhost")
