"""
Blacklist CRUD endpoints for manual email/domain blocking.

Registers routes:
  GET    /api/blacklist          → paginated blacklist
  POST   /api/blacklist          → manually block an email (domain auto-extracted)
  DELETE /api/blacklist/{id}     → unblock an entry
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .deps import CurrentUser, get_db, require
from ..db import Database

router = APIRouter(tags=["blacklist"])


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class BlacklistAdd(BaseModel):
    email: str
    reason: str | None = None


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/api/blacklist")
async def list_blacklist(
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=200),
        db: Database = Depends(get_db),
):
    entries, total = db.list_blacklist(page=page, limit=limit)
    return {
        "entries": entries,
        "total": total,
        "page": page,
        "pages": max(1, (total + limit - 1) // limit),
    }


@router.post("/api/blacklist", status_code=201)
async def add_to_blacklist(req: BlacklistAdd, db: Database = Depends(get_db),
                           user: CurrentUser = Depends(require("blacklist.manage"))):
    email = req.email.strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email address")

    domain = email.split("@")[1]
    added = db.add_to_blacklist(email=email, domain=domain, reason=req.reason)
    if not added:
        raise HTTPException(status_code=409, detail="Email already blacklisted")
    return {"message": f"Blacklisted {email}", "domain": domain}


@router.delete("/api/blacklist/{blacklist_id}")
async def remove_from_blacklist(blacklist_id: int, db: Database = Depends(get_db),
                                user: CurrentUser = Depends(require("blacklist.manage"))):
    if not db.remove_from_blacklist(blacklist_id):
        raise HTTPException(status_code=404, detail="Blacklist entry not found")
    return {"message": "Entry removed from blacklist"}
