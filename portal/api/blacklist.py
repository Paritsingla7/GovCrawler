"""
Blacklist CRUD endpoints for manual email/domain blocking.

Registers routes:
  GET    /api/blacklist          → paginated blacklist
  POST   /api/blacklist          → manually block an email (domain auto-extracted)
  DELETE /api/blacklist/{id}     → unblock an entry
"""

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from ..db import Database


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class BlacklistAdd(BaseModel):
    email: str
    reason: str | None = None


# ── Route registration ────────────────────────────────────────────────────────

def register_blacklist_routes(app: FastAPI, db: Database):
    @app.get("/api/blacklist")
    async def list_blacklist(
            page: int = Query(1, ge=1),
            limit: int = Query(50, ge=1, le=200),
    ):
        entries, total = db.list_blacklist(page=page, limit=limit)
        return {
            "entries": entries,
            "total": total,
            "page": page,
            "pages": max(1, (total + limit - 1) // limit),
        }

    @app.post("/api/blacklist", status_code=201)
    async def add_to_blacklist(req: BlacklistAdd):
        email = req.email.strip().lower()
        if "@" not in email:
            raise HTTPException(status_code=400, detail="Invalid email address")

        domain = email.split("@")[1]
        added = db.add_to_blacklist(email=email, domain=domain, reason=req.reason)
        if not added:
            raise HTTPException(status_code=409, detail="Email already blacklisted")
        return {"message": f"Blacklisted {email}", "domain": domain}

    @app.delete("/api/blacklist/{blacklist_id}")
    async def remove_from_blacklist(blacklist_id: int):
        if not db.remove_from_blacklist(blacklist_id):
            raise HTTPException(status_code=404, detail="Blacklist entry not found")
        return {"message": "Entry removed from blacklist"}
