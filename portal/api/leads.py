"""
Lead browsing, export, and editing endpoints.

Registers routes:
  GET  /api/leads           → paginated leads for a job
  GET  /api/leads/ids       → all matching lead IDs (for select-all)
  GET  /api/leads/categories→ category counts for leads
  GET  /api/leads/states    → distinct states for leads
  POST /api/leads/export    → CSV download
  PUT  /api/leads/{id}      → edit name/designation/department/state
"""

import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..db import Database
from .deps import get_db

router = APIRouter(tags=["leads"])

_ALL_EXPORT_FIELDS = [
    "email", "person_name", "designation", "department",
    "domain_title", "domain_state", "domain_org_type",
    "category_title", "source_url", "source_title", "context_snippet",
    "depth", "captured_at",
]


class ExportLeadsRequest(BaseModel):
    job_id: int | None = None
    category: str | None = None
    state: str | None = None
    search: str | None = None
    complete_only: bool = False
    lead_ids: list[int] | None = None
    fields: list[str] | None = None


class LeadUpdate(BaseModel):
    person_name: str | None = None
    designation: str | None = None
    department: str | None = None
    domain_state: str | None = None


@router.get("/api/leads")
async def get_leads(
        job_id: int = Query(None),
        category: str = Query(None),
        state: str = Query(None),
        search: str = Query(None),
        complete_only: bool = Query(False),
        page: int = Query(1, ge=1),
        limit: int = Query(100, ge=1, le=500),
        db: Database = Depends(get_db),
):
    leads, total = db.get_leads(job_id=job_id, category=category, state=state, search=search,
                                complete_only=complete_only, page=page, limit=limit)
    return {
        "leads": leads,
        "total": total,
        "page": page,
        "pages": max(1, (total + limit - 1) // limit),
    }


@router.get("/api/leads/ids")
async def get_lead_ids(
        job_id: int = Query(None),
        category: str = Query(None),
        state: str = Query(None),
        search: str = Query(None),
        complete_only: bool = Query(False),
        db: Database = Depends(get_db),
):
    ids = db.get_lead_ids(job_id=job_id, category=category, state=state, search=search,
                          complete_only=complete_only)
    return {"ids": ids, "total": len(ids)}


@router.get("/api/leads/categories")
async def get_lead_categories(job_id: int = Query(None), db: Database = Depends(get_db)):
    return db.get_lead_categories(job_id=job_id)


@router.get("/api/leads/states")
async def get_lead_states(job_id: int = Query(None), category: str = Query(None), db: Database = Depends(get_db)):
    return db.get_lead_states(job_id=job_id, category=category)


@router.post("/api/leads/export")
async def export_leads(req: ExportLeadsRequest, db: Database = Depends(get_db)):
    rows = db.get_all_leads_for_export(
        job_id=req.job_id,
        category=req.category,
        state=req.state,
        search=req.search,
        lead_ids=req.lead_ids,
        complete_only=req.complete_only,
    )
    if not rows:
        raise HTTPException(status_code=404, detail="No leads for this job")

    # Keep only the requested fields (email always included), preserving canonical order
    if req.fields:
        allowed = set(req.fields) | {"email"}
        fieldnames = [f for f in _ALL_EXPORT_FIELDS if f in allowed]
    else:
        fieldnames = _ALL_EXPORT_FIELDS

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition":
                     f'attachment; filename="leads_export.csv"'},
    )


@router.put("/api/leads/{lead_id}")
async def update_lead(lead_id: int, req: LeadUpdate, db: Database = Depends(get_db)):
    updates = req.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    ok = db.update_lead(lead_id, updates)
    if not ok:
        raise HTTPException(status_code=404, detail="Lead not found")
    return {"ok": True}
