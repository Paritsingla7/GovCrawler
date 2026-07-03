"""
Lead browsing, export, and editing endpoints.

Registers routes:
  GET  /api/leads                    → paginated leads for a job
  GET  /api/leads/ids                → all matching lead IDs (for select-all)
  GET  /api/leads/score-weights      → current lead_score point weights
  GET  /api/leads/categories         → category counts for leads
  GET  /api/leads/states             → distinct states for leads
  GET  /api/leads/org-types          → organization-type counts for leads
  POST /api/leads/export             → CSV download
  POST /api/leads/import-csv         → bulk-create/update manual leads from CSV
  GET  /api/leads/import-csv/template→ downloadable CSV template
  PUT  /api/leads/{id}               → edit name/designation/department/state
"""

import csv
import io

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..db import Database
from ..services.csv_import import build_template_csv, parse_contacts_csv
from .deps import get_db

router = APIRouter(tags=["leads"])

_ALL_EXPORT_FIELDS = [
    "email", "person_name", "designation", "department",
    "domain_title", "domain_state", "domain_org_type",
    "category_title", "source_url", "source_title", "context_snippet",
    "lead_score", "depth", "captured_at",
]


class ExportLeadsRequest(BaseModel):
    job_id: int | None = None
    category: str | None = None
    state: str | None = None
    search: str | None = None
    complete_only: bool = False
    min_score: int | None = None
    org_type: str | None = None
    show_manual: bool = True
    require_name: bool = False
    require_designation: bool = False
    require_phone: bool = False
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
        min_score: int = Query(None, ge=0, le=100),
        org_type: str = Query(None),
        show_manual: bool = Query(True),
        require_name: bool = Query(False),
        require_designation: bool = Query(False),
        require_phone: bool = Query(False),
        sort_by: str = Query(None),
        sort_dir: str = Query("desc"),
        page: int = Query(1, ge=1),
        limit: int = Query(100, ge=1, le=500),
        db: Database = Depends(get_db),
):
    leads, total = db.get_leads(job_id=job_id, category=category, state=state, search=search,
                                complete_only=complete_only, min_score=min_score,
                                org_type=org_type, show_manual=show_manual,
                                require_name=require_name, require_designation=require_designation,
                                require_phone=require_phone, sort_by=sort_by, sort_dir=sort_dir,
                                page=page, limit=limit)
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
        min_score: int = Query(None, ge=0, le=100),
        org_type: str = Query(None),
        show_manual: bool = Query(True),
        require_name: bool = Query(False),
        require_designation: bool = Query(False),
        require_phone: bool = Query(False),
        db: Database = Depends(get_db),
):
    ids = db.get_lead_ids(job_id=job_id, category=category, state=state, search=search,
                          complete_only=complete_only, min_score=min_score,
                          org_type=org_type, show_manual=show_manual,
                          require_name=require_name, require_designation=require_designation,
                          require_phone=require_phone)
    return {"ids": ids, "total": len(ids)}


@router.get("/api/leads/score-weights")
async def get_lead_score_weights(db: Database = Depends(get_db)):
    return db.get_lead_score_weights()


@router.get("/api/leads/categories")
async def get_lead_categories(job_id: int = Query(None), db: Database = Depends(get_db)):
    return db.get_lead_categories(job_id=job_id)


@router.get("/api/leads/states")
async def get_lead_states(job_id: int = Query(None), category: str = Query(None), db: Database = Depends(get_db)):
    return db.get_lead_states(job_id=job_id, category=category)


@router.get("/api/leads/org-types")
async def get_lead_org_types(job_id: int = Query(None), db: Database = Depends(get_db)):
    return db.get_lead_org_types(job_id=job_id)


@router.post("/api/leads/export")
async def export_leads(req: ExportLeadsRequest, db: Database = Depends(get_db)):
    rows = db.get_all_leads_for_export(
        job_id=req.job_id,
        category=req.category,
        state=req.state,
        search=req.search,
        lead_ids=req.lead_ids,
        complete_only=req.complete_only,
        min_score=req.min_score,
        org_type=req.org_type,
        show_manual=req.show_manual,
        require_name=req.require_name,
        require_designation=req.require_designation,
        require_phone=req.require_phone,
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


@router.post("/api/leads/import-csv")
async def import_leads_csv(file: UploadFile = File(...), db: Database = Depends(get_db)):
    """Bulk-create or update manual leads from an uploaded CSV file."""
    content = await file.read()
    rows, skipped = parse_contacts_csv(content)
    if not rows and not skipped:
        raise HTTPException(status_code=400, detail="CSV file is empty")

    job_id = db.get_or_create_manual_upload_job()
    imported, updated, db_skipped = db.bulk_upsert_manual_leads(job_id, rows)
    skipped.extend(db_skipped)

    return {"imported": imported, "updated": updated, "skipped": skipped}


@router.get("/api/leads/import-csv/template")
async def download_leads_csv_template():
    return StreamingResponse(
        iter([build_template_csv()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="leads_import_template.csv"'},
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
