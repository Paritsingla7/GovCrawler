"""
Domain metadata and browsing endpoints.

Registers routes:
  GET   /api/categories      → [{code, title, count}]
  GET   /api/states          → state list (filtered by category if provided)
  GET   /api/org-types       → org type list (filtered by category+state)
  GET   /api/domains         → paginated domain list
  GET   /api/domains/ids     → all matching domain IDs (for select-all)
  GET   /api/domains/stats   → total / crawlable / duplicate counts
  PATCH /api/domains/{id}    → set/update a domain's crawlable URL
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from urllib.parse import urlsplit

from .deps import CurrentUser, get_db, require
from ..db import Database

router = APIRouter(tags=["domains"])


class UpdateDomainUrlRequest(BaseModel):
    main_url: str
    contact_url: str | None = None


def _normalize_domain_url(raw: str) -> str:
    candidate = raw.strip()
    if not candidate:
        raise HTTPException(status_code=422, detail="URL cannot be empty")
    if "://" not in candidate:
        candidate = "http://" + candidate
    if not urlsplit(candidate).netloc:
        raise HTTPException(status_code=422, detail=f"Invalid URL: {raw}")
    return candidate


@router.get("/api/categories")
async def get_categories(db: Database = Depends(get_db)):
    return db.get_categories()


@router.get("/api/states")
async def get_states(category: str = Query(None), db: Database = Depends(get_db)):
    return db.get_states(category=category or None)


@router.get("/api/org-types")
async def get_org_types(category: str = Query(None), state: str = Query(None), db: Database = Depends(get_db)):
    return db.get_org_types(category=category or None, state=state or None)


@router.get("/api/domains")
async def get_domains(
        category: str = Query(None),
        state: str = Query(None),
        org_type: str = Query(None),
        search: str = Query(None),
        sort_by: str = Query(None),
        sort_dir: str = Query("desc"),
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=200),
        db: Database = Depends(get_db),
):
    domains, total = db.get_domains(
        category=category or None,
        state=state or None,
        org_type=org_type or None,
        search=search or None,
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        limit=limit,
    )
    return {
        "domains": domains,
        "total": total,
        "page": page,
        "limit": limit,
        "pages": max(1, (total + limit - 1) // limit),
    }


@router.get("/api/domains/ids")
async def get_domain_ids(
        category: str = Query(None),
        state: str = Query(None),
        org_type: str = Query(None),
        search: str = Query(None),
        db: Database = Depends(get_db),
):
    ids = db.get_domain_ids(
        category=category or None,
        state=state or None,
        org_type=org_type or None,
        search=search or None,
    )
    return {"ids": ids, "total": len(ids)}


@router.get("/api/domains/stats")
async def get_domain_stats(
        category: str = Query(None),
        state: str = Query(None),
        org_type: str = Query(None),
        search: str = Query(None),
        db: Database = Depends(get_db),
):
    return db.get_domain_stats(
        category=category or None,
        state=state or None,
        org_type=org_type or None,
        search=search or None,
    )


@router.patch("/api/domains/{domain_id}")
async def update_domain_url(
        domain_id: int,
        req: UpdateDomainUrlRequest,
        db: Database = Depends(get_db),
        user: CurrentUser = Depends(require("domains.import")),
):
    main_url = _normalize_domain_url(req.main_url)
    contact_url = _normalize_domain_url(req.contact_url) if req.contact_url else None

    updated = db.update_domain_url(domain_id, main_url=main_url, contact_url=contact_url)
    if not updated:
        raise HTTPException(status_code=404, detail="Domain not found")
    return updated
