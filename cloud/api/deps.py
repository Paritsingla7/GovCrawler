"""
Shared application state and FastAPI dependency providers.

State is set once in server.create_app()/lifespan() at startup. Route
handlers pull it via Depends(...) instead of capturing it through closures —
this lets each route module be imported and tested independently of app
construction.
"""

from dataclasses import dataclass, field
from pathlib import Path

import jwt as pyjwt
from fastapi import Depends, HTTPException, Request

from ..db import Database
from ..security.jwt import decode_token

_db: Database | None = None
_config: dict | None = None
_config_path: Path | None = None


def decode_token_with_rotation(token: str, config: dict) -> dict | None:
    """Tries auth.jwt_secret first, then auth.jwt_secret_prev if set — lets a
    JWT_SECRET rotation take effect without invalidating every live access
    token at once (see server._ensure_jwt_secret). Returns None on failure
    against both."""
    auth_cfg = config["auth"]
    try:
        return decode_token(token, auth_cfg["jwt_secret"])
    except pyjwt.PyJWTError:
        prev = auth_cfg.get("jwt_secret_prev")
        if not prev:
            return None
        try:
            return decode_token(token, prev)
        except pyjwt.PyJWTError:
            return None


def get_db() -> Database:
    return _db


def get_config() -> dict:
    return _config


def get_config_path() -> Path:
    return _config_path


# ── Auth ──────────────────────────────────────────────────────────────────────


class RedirectException(Exception):
    """Raised by page-auth dependencies to bounce an unauthenticated browser
    request to /login. Handled by an app-level exception handler
    (see server.create_app), since Depends() can't itself return a redirect."""

    def __init__(self, location: str = "/login"):
        self.location = location


class ForbiddenPageException(Exception):
    """Raised by require_page() when an authenticated browser request lacks
    the needed permission. Rendered as a friendly HTML page (not a raw JSON
    403 the browser can't do anything useful with) by the app-level handler
    in cloud/api/server.py — a redirect to /login would be wrong here since
    the visitor already IS logged in; bouncing them there would just be a
    confusing loop back to the same page."""

    def __init__(self, message: str = "You don't have permission to view this page."):
        self.message = message


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
        return auth_header[len("Bearer ") :]
    return request.cookies.get("access")


def get_current_user(request: Request) -> CurrentUser:
    token = _extract_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    payload = decode_token_with_rotation(token, _config)
    if payload is None:
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
        raise RedirectException("/login?notice=login_required")


def require(*perms: str):
    def dep(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if not user.has_all(perms):
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user

    return dep


def require_page(*perms: str):
    """Page-route variant of require(): unauthenticated -> redirect to
    /login (like current_user_or_redirect), authenticated-but-forbidden ->
    ForbiddenPageException (a friendly HTML page), never a raw JSON
    401/403 a full-page browser navigation can't do anything useful with."""

    def dep(request: Request) -> CurrentUser:
        try:
            user = get_current_user(request)
        except HTTPException:
            raise RedirectException("/login?notice=login_required")
        if not user.has_all(perms):
            raise ForbiddenPageException(
                "You're signed in, but your account doesn't have permission to view the admin portal. "
                "Contact your administrator if you believe this is a mistake."
            )
        return user

    return dep


def forbid_unless_owner(owner_id: int | None, user: CurrentUser, *, allow: str | None = None) -> None:
    """403 unless the caller owns the row (or is admin, or holds `allow`). The
    shared object-level-authorization check for per-owner resources — jobs
    (coordination writes) and campaigns."""
    if owner_id == user.id or user.is_admin:
        return
    if allow and user.can(allow):
        return
    raise HTTPException(status_code=403, detail="Insufficient permissions")


def client_ip(request: Request) -> str | None:
    """Shared by every router that writes an audit_log row."""
    return request.client.host if request.client else None


_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def verify_csrf(request: Request):
    """Double-submit CSRF check for mutating requests. Only enforced when the
    request is cookie-authenticated (no Authorization header) — a Bearer
    token isn't sent automatically by the browser, so it isn't CSRF-able.
    SameSite=Strict on the auth cookies already blocks most CSRF vectors;
    this is defense-in-depth per plan.md §13."""
    if request.method in _SAFE_METHODS:
        return
    if request.headers.get("Authorization"):
        return
    cookie_token = request.cookies.get("csrf")
    header_token = request.headers.get("X-CSRF-Token")
    if not cookie_token or not header_token or cookie_token != header_token:
        raise HTTPException(status_code=403, detail="Missing or invalid CSRF token")
