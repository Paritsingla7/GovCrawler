"""
Domain metadata and browsing endpoints.

Registers routes:
  GET  /api/categories      → [{code, title, count}]
  GET  /api/states          → state list (filtered by category if provided)
  GET  /api/org-types       → org type list (filtered by category+state)
  GET  /api/domains         → paginated domain list
  GET  /api/domains/ids     → all matching domain IDs (for select-all)
"""

from fastapi import APIRouter, Depends, Query

from ..db import Database
from .deps import get_db

router = APIRouter(tags=["domains"])


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
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=200),
        db: Database = Depends(get_db),
):
    domains, total = db.get_domains(
        category=category or None,
        state=state or None,
        org_type=org_type or None,
        search=search or None,
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
