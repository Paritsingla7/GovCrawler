"""SMTP credential management endpoints (CRUD + live connection test). See
.docs/outreach.md and .docs/api-reference.md."""

import aiosmtplib
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, model_validator
from sqlalchemy.exc import IntegrityError

from .deps import CurrentUser, client_ip, get_db, require
from ..db import Database
from ..security.oauth import OAuthTokenError, build_authorize_url, get_valid_access_token

router = APIRouter(tags=["credentials"])

_OAUTH_PROVIDERS = ("microsoft", "google")


# ── Pydantic schemas ──────────────────────────────────────────────────────────


class CredentialCreate(BaseModel):
    host: str
    port: int
    username: str
    provider: str = "basic"
    password: str | None = None
    daily_send_limit: int | None = None

    @model_validator(mode="after")
    def _password_required_for_basic(self):
        if self.provider == "basic" and not self.password:
            raise ValueError("password is required for provider 'basic'")
        return self


class CredentialUpdate(BaseModel):
    host: str | None = None
    port: int | None = None
    username: str | None = None
    provider: str | None = None
    password: str | None = None
    is_active: bool | None = None
    daily_send_limit: int | None = None


class OAuthStartRequest(BaseModel):
    provider: str


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("/api/credentials")
async def list_credentials(db: Database = Depends(get_db)):
    creds = db.list_credentials()
    # Cosmetic placeholder only — the underlying dict never carries a real password.
    for c in creds:
        c["password"] = "••••••••" if c["provider"] == "basic" else None
    return creds


@router.post("/api/credentials", status_code=201)
async def create_credential(
    req: CredentialCreate,
    request: Request,
    db: Database = Depends(get_db),
    user: CurrentUser = Depends(require("credentials.manage")),
):
    cid = db.create_credential(
        host=req.host,
        port=req.port,
        username=req.username,
        password=req.password,
        provider=req.provider,
        daily_send_limit=req.daily_send_limit,
    )
    db.write_audit(
        user.id,
        "credential.create",
        "credential",
        cid,
        detail={"host": req.host, "username": req.username},
        ip=client_ip(request),
    )
    return {"id": cid, "message": "Credential created"}


@router.put("/api/credentials/{credential_id}")
async def update_credential(
    credential_id: int,
    req: CredentialUpdate,
    request: Request,
    db: Database = Depends(get_db),
    user: CurrentUser = Depends(require("credentials.manage")),
):
    updates = req.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    if not db.update_credential(credential_id, **updates):
        raise HTTPException(status_code=404, detail="Credential not found")
    # Never record the password value itself — only which fields changed.
    db.write_audit(
        user.id,
        "credential.update",
        "credential",
        credential_id,
        detail={"fields": list(updates.keys())},
        ip=client_ip(request),
    )
    return {"message": "Credential updated"}


@router.delete("/api/credentials/{credential_id}")
async def delete_credential(
    credential_id: int,
    request: Request,
    db: Database = Depends(get_db),
    user: CurrentUser = Depends(require("credentials.manage")),
):
    try:
        if not db.delete_credential(credential_id):
            raise HTTPException(status_code=404, detail="Credential not found")
    except IntegrityError:
        raise HTTPException(
            status_code=409, detail="Credential is assigned to one or more campaigns and can't be deleted."
        )
    db.write_audit(user.id, "credential.delete", "credential", credential_id, ip=client_ip(request))
    return {"message": "Credential deleted"}


@router.post("/api/credentials/{credential_id}/oauth/start")
async def start_credential_oauth(
    credential_id: int,
    req: OAuthStartRequest,
    request: Request,
    db: Database = Depends(get_db),
    user: CurrentUser = Depends(require("credentials.manage")),
):
    """Begins the Authorization Code + PKCE flow for a Microsoft/Google
    credential. Returns an authorize_url for the frontend to open in a new
    tab — the provider's consent redirect goes straight to the cloud's public
    /oauth/callback/{provider}, never through this API call."""
    if req.provider not in _OAUTH_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {req.provider}")

    cred = db.get_credential(credential_id)
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")

    oauth_config = db.get_oauth_config()
    redirect_base = oauth_config.get("redirect_base_url")
    if not redirect_base:
        raise HTTPException(status_code=400, detail="oauth.redirect_base_url is not configured")
    redirect_uri = f"{redirect_base.rstrip('/')}/oauth/callback/{req.provider}"

    try:
        authorize_url, state, code_verifier = build_authorize_url(req.provider, oauth_config, redirect_uri)
    except OAuthTokenError as e:
        raise HTTPException(status_code=400, detail=str(e))

    db.create_oauth_flow(state, credential_id, req.provider, code_verifier)
    db.write_audit(
        user.id,
        "credential.oauth_start",
        "credential",
        credential_id,
        detail={"provider": req.provider},
        ip=client_ip(request),
    )
    return {"authorize_url": authorize_url}


@router.post("/api/credentials/{credential_id}/test")
async def test_credential(
    credential_id: int,
    request: Request,
    db: Database = Depends(get_db),
    user: CurrentUser = Depends(require("credentials.manage")),
):
    """Test SMTP connection and login."""
    cred = db.get_credential(credential_id)
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")

    if cred["provider"] != "basic" and not cred["refresh_token"]:
        return {"success": False, "error": 'Not connected — use "Connect via Microsoft/Google" first.'}

    use_tls = cred["port"] == 465
    start_tls = cred["port"] == 587

    try:
        smtp = aiosmtplib.SMTP(
            hostname=cred["host"],
            port=cred["port"],
            use_tls=use_tls,
            start_tls=start_tls,
            timeout=10,
        )
        await smtp.connect()
        if cred["provider"] == "basic":
            await smtp.login(cred["username"], cred["password"])
        else:
            access_token = await get_valid_access_token(db, cred, db.get_oauth_config())
            await smtp.auth_xoauth2(cred["username"], access_token)
        await smtp.quit()

        # If we get here, connection works! Activate if it was disabled.
        if not cred["is_active"]:
            db.update_credential(credential_id, is_active=True)

        db.write_audit(
            user.id, "credential.test", "credential", credential_id, detail={"success": True}, ip=client_ip(request)
        )
        return {"success": True, "message": "Connection successful"}

    except (aiosmtplib.SMTPAuthenticationError, OAuthTokenError) as e:
        db.update_credential(credential_id, is_active=False)
        db.write_audit(
            user.id,
            "credential.test",
            "credential",
            credential_id,
            detail={"success": False, "error": "authentication_failed"},
            ip=client_ip(request),
        )
        return {"success": False, "error": f"Authentication failed: {getattr(e, 'message', str(e))}"}
    except Exception as e:
        db.update_credential(credential_id, is_active=False)
        db.write_audit(
            user.id,
            "credential.test",
            "credential",
            credential_id,
            detail={"success": False, "error": "connection_failed"},
            ip=client_ip(request),
        )
        return {"success": False, "error": f"Connection failed: {str(e)}"}
