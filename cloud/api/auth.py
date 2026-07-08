"""Authentication endpoints (login/refresh/logout/me + loopback bootstrap).
See .docs/authentication.md."""
import datetime
import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from .deps import CurrentUser, decode_token_with_rotation, get_config, get_current_user, get_db, require_loopback
from ..db import Database, User
from ..security.hashing import verify_password
from ..security.jwt import create_access_token, generate_refresh_token, hash_refresh_token
from shared.schemas.auth import LoginRequest, RefreshRequest, TokenResponse, UserOut

log = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _set_auth_cookies(response: Response, access_token: str, refresh_token: str, config: dict):
    auth_cfg = config["auth"]
    secure = auth_cfg.get("cookie_secure", False)
    response.set_cookie("access", access_token, httponly=True, secure=secure, samesite="strict",
                        max_age=auth_cfg["access_ttl_minutes"] * 60)
    response.set_cookie("refresh", refresh_token, httponly=True, secure=secure, samesite="strict",
                        max_age=auth_cfg["refresh_ttl_days"] * 86400)
    # Double-submit CSRF token — deliberately NOT httponly, base.js's apiFetch
    # reads it and echoes it back as X-CSRF-Token on mutating requests (see
    # deps.verify_csrf). Same TTL as the access cookie it accompanies.
    response.set_cookie("csrf", secrets.token_urlsafe(32), httponly=False, secure=secure, samesite="strict",
                        max_age=auth_cfg["access_ttl_minutes"] * 60)


def _issue_tokens(db: Database, user: dict, config: dict, request: Request) -> tuple[str, str]:
    auth_cfg = config["auth"]
    access_token = create_access_token(
        user["id"], user["token_version"], auth_cfg["jwt_secret"], auth_cfg["access_ttl_minutes"],
    )
    refresh_token = generate_refresh_token()
    expires_at = datetime.datetime.utcnow() + datetime.timedelta(days=auth_cfg["refresh_ttl_days"])
    db.create_session(
        user_id=user["id"], refresh_token_hash=hash_refresh_token(refresh_token),
        expires_at=expires_at, user_agent=request.headers.get("User-Agent"), ip=_client_ip(request),
    )
    return access_token, refresh_token


def _user_out(db: Database, user: dict) -> UserOut:
    role_name = db.get_role_name(user["role_id"])
    permissions = db.resolve_effective_permissions(user["id"])
    return UserOut(
        id=user["id"], email=user["email"], full_name=user.get("full_name"),
        is_admin=user["is_admin"], role=role_name, permissions=sorted(permissions),
    )


@router.post("/auth/login", response_model=TokenResponse)
async def login(req: LoginRequest, response: Response, request: Request,
                db: Database = Depends(get_db), config: dict = Depends(get_config)):
    user = db.get_user_by_email(req.email)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if user["locked_until"] and user["locked_until"] > datetime.datetime.utcnow():
        raise HTTPException(status_code=423, detail="Account temporarily locked. Try again later.")
    if not user["is_active"]:
        raise HTTPException(status_code=403, detail="Account is disabled")

    with db._Session() as s:
        row = s.query(User.password_hash).filter_by(id=user["id"]).first()
        password_hash = row[0] if row else ""

    if not verify_password(password_hash, req.password):
        db.record_login_failure(user["id"], config["auth"]["lockout_threshold"], config["auth"]["lockout_minutes"])
        db.write_audit(user["id"], "user.login_failed", "user", user["id"], ip=_client_ip(request))
        raise HTTPException(status_code=401, detail="Invalid email or password")

    db.record_login_success(user["id"])
    access_token, refresh_token = _issue_tokens(db, user, config, request)
    _set_auth_cookies(response, access_token, refresh_token, config)
    db.write_audit(user["id"], "user.login", "user", user["id"], ip=_client_ip(request))

    return TokenResponse(access_token=access_token, refresh_token=refresh_token, user=_user_out(db, user))


@router.post("/auth/refresh", response_model=TokenResponse)
async def refresh(req: RefreshRequest, request: Request, response: Response,
                  db: Database = Depends(get_db), config: dict = Depends(get_config)):
    presented = req.refresh_token or request.cookies.get("refresh")
    if not presented:
        raise HTTPException(status_code=401, detail="No refresh token provided")

    token_hash = hash_refresh_token(presented)
    session = db.get_session_by_hash(token_hash)
    if session and session["revoked_at"]:
        # A rotated-away token was presented again: possible theft/replay — kill the family.
        db.revoke_session_family(session["user_id"])
        db.write_audit(session["user_id"], "user.session_reuse_detected", "session", session["id"])
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
    if not session or session["expires_at"] < datetime.datetime.utcnow():
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    user = db.get_user_by_id(session["user_id"])
    if not user or not user["is_active"]:
        raise HTTPException(status_code=401, detail="User not found or inactive")

    auth_cfg = config["auth"]
    new_refresh_token = generate_refresh_token()
    new_expires_at = datetime.datetime.utcnow() + datetime.timedelta(days=auth_cfg["refresh_ttl_days"])
    db.rotate_session(session["id"], hash_refresh_token(new_refresh_token), new_expires_at)

    access_token = create_access_token(
        user["id"], user["token_version"], auth_cfg["jwt_secret"], auth_cfg["access_ttl_minutes"],
    )
    _set_auth_cookies(response, access_token, new_refresh_token, config)

    return TokenResponse(access_token=access_token, refresh_token=new_refresh_token, user=_user_out(db, user))


@router.post("/auth/logout")
async def logout(request: Request, response: Response, db: Database = Depends(get_db)):
    presented = request.cookies.get("refresh")
    if presented:
        session = db.get_session_by_hash(hash_refresh_token(presented))
        if session:
            db.revoke_session(session["id"])
            db.write_audit(session["user_id"], "user.logout", "session", session["id"])
    response.delete_cookie("access")
    response.delete_cookie("refresh")
    response.delete_cookie("csrf")
    return {"message": "Logged out"}


@router.get("/auth/me", response_model=UserOut)
async def me(user: CurrentUser = Depends(get_current_user), db: Database = Depends(get_db)):
    db_user = db.get_user_by_id(user.id)
    return _user_out(db, db_user)


@router.get("/auth/bootstrap", dependencies=[Depends(require_loopback)])
async def bootstrap(token: str, db: Database = Depends(get_db), config: dict = Depends(get_config)):
    """The launcher already holds a valid access token from its own login —
    this lets the browser tab it opens inherit that session via a cookie
    instead of prompting the operator to log in a second time. Loopback-only
    (same trust boundary as /api/system/*) and re-validates the token itself
    rather than trusting the query string blindly."""
    payload = decode_token_with_rotation(token, config)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Invalid token type")
    user = db.get_user_by_id(int(payload["sub"]))
    if not user or not user["is_active"] or payload.get("tv") != user["token_version"]:
        raise HTTPException(status_code=401, detail="Token has been revoked")

    response = RedirectResponse(url="/")
    secure = config["auth"].get("cookie_secure", False)
    response.set_cookie("access", token, httponly=True, secure=secure, samesite="strict",
                        max_age=config["auth"]["access_ttl_minutes"] * 60)
    response.set_cookie("csrf", secrets.token_urlsafe(32), httponly=False, secure=secure, samesite="strict",
                        max_age=config["auth"]["access_ttl_minutes"] * 60)
    return response
