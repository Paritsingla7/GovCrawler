"""
SMTP Credential management endpoints.

Registers routes:
  GET    /api/credentials          → list all credentials (passwords masked)
  POST   /api/credentials          → add new credential
  PUT    /api/credentials/{id}     → update credential
  DELETE /api/credentials/{id}     → delete credential
  POST   /api/credentials/{id}/test→ test connection and login
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import aiosmtplib

from ..db import Database
from .deps import get_db

router = APIRouter(tags=["credentials"])


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class CredentialCreate(BaseModel):
    host: str
    port: int
    username: str
    password: str
    daily_send_limit: int | None = None


class CredentialUpdate(BaseModel):
    host: str | None = None
    port: int | None = None
    username: str | None = None
    password: str | None = None
    is_active: bool | None = None
    daily_send_limit: int | None = None


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/api/credentials")
async def list_credentials(db: Database = Depends(get_db)):
    creds = db.list_credentials()
    # Mask passwords for output
    for c in creds:
        c["password"] = "••••••••"
    return creds


@router.post("/api/credentials", status_code=201)
async def create_credential(req: CredentialCreate, db: Database = Depends(get_db)):
    cid = db.create_credential(
        host=req.host,
        port=req.port,
        username=req.username,
        password=req.password,
        daily_send_limit=req.daily_send_limit,
    )
    return {"id": cid, "message": "Credential created"}


@router.put("/api/credentials/{credential_id}")
async def update_credential(credential_id: int, req: CredentialUpdate, db: Database = Depends(get_db)):
    updates = req.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    if not db.update_credential(credential_id, **updates):
        raise HTTPException(status_code=404, detail="Credential not found")
    return {"message": "Credential updated"}


@router.delete("/api/credentials/{credential_id}")
async def delete_credential(credential_id: int, db: Database = Depends(get_db)):
    if not db.delete_credential(credential_id):
        raise HTTPException(status_code=404, detail="Credential not found")
    return {"message": "Credential deleted"}


@router.post("/api/credentials/{credential_id}/test")
async def test_credential(credential_id: int, db: Database = Depends(get_db)):
    """Test SMTP connection and login."""
    cred = db.get_credential(credential_id)
    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")

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
        await smtp.login(cred["username"], cred["password"])
        await smtp.quit()

        # If we get here, connection works! Activate if it was disabled.
        if not cred["is_active"]:
            db.update_credential(credential_id, is_active=True)

        return {"success": True, "message": "Connection successful"}

    except aiosmtplib.SMTPAuthenticationError as e:
        db.update_credential(credential_id, is_active=False)
        return {"success": False, "error": f"Authentication failed: {e.message}"}
    except Exception as e:
        db.update_credential(credential_id, is_active=False)
        return {"success": False, "error": f"Connection failed: {str(e)}"}
