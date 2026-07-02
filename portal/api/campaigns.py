"""
Campaign generation, listing, and staging endpoints.

Registers routes:
  POST   /api/campaigns                       → generate drafts from leads + template
  GET    /api/campaigns                       → paginated campaign list
  GET    /api/campaigns/{id}                  → campaign detail + stats
  PATCH  /api/campaigns/{id}                  → update campaign status (pause/cancel)
  GET    /api/campaigns/{id}/emails           → paginated staged emails
  PUT    /api/campaigns/{id}/emails/{eid}     → manual body override
  GET    /api/campaigns/{id}/stats            → live stats for polling
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel

from ..db import Database, CampaignStatus, Lead
from ..services.campaign_service import render_draft_emails, render_template_string
from ..services.csv_import import parse_contacts_csv
from .deps import get_db
from .dispatcher import run_campaign_dispatch, run_test_campaign_dispatch

log = logging.getLogger(__name__)

router = APIRouter(tags=["campaigns"])

_active_campaign_tasks: dict[int, asyncio.Task] = {}


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class CampaignCreate(BaseModel):
    name: str
    template_id: int
    lead_ids: list[int]


class CampaignStatusUpdate(BaseModel):
    status: str  # One of: RUNNING, PAUSED, CANCELLED, COMPLETED


class CampaignEmailUpdate(BaseModel):
    subject: str
    body: str


class EmailSelectionUpdate(BaseModel):
    is_selected: bool


class AddEmailsRequest(BaseModel):
    lead_ids: list[int]


class DummyDetails(BaseModel):
    name: str | None = None
    designation: str | None = None
    email: str
    department: str | None = None


class TestCampaignCreate(BaseModel):
    name: str
    template_id: int
    dummy_details: list[DummyDetails]
    test_credential_id: int | None = None


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/api/campaigns", status_code=201)
async def create_campaign(req: CampaignCreate, db: Database = Depends(get_db)):
    """The core draft generation endpoint.
    Loads leads, filters blacklist, renders templates, stages drafts."""

    # 1. Validate template exists
    template = db.get_template(req.template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    # 2. Validate lead_ids
    if not req.lead_ids:
        raise HTTPException(status_code=400, detail="lead_ids is empty")

    # 3. Load leads from DB
    # Re-use the existing get_all_leads_for_export which accepts lead_ids
    leads = db.get_all_leads_for_export(lead_ids=req.lead_ids)
    if not leads:
        raise HTTPException(status_code=404, detail="No matching leads found")

    # 4. Blacklist filter (checked up front so we can 422 before creating the campaign)
    blacklisted = db.get_blacklisted_emails_set()
    if all(l["email"] in blacklisted for l in leads):
        raise HTTPException(
            status_code=422,
            detail=f"All {len(leads)} leads are blacklisted. "
                   f"No emails to stage.",
        )

    # 5. Create campaign record
    campaign_id = db.create_campaign(
        name=req.name,
        template_id=req.template_id,
        status=CampaignStatus.PAUSED,
    )

    # 6. Jinja2 render loop + build email dicts
    # We need lead IDs for the FK — get_all_leads_for_export doesn't return them,
    # so we build a lookup from the original lead_ids query
    with db._Session() as s:
        rows = s.query(Lead.id, Lead.email).filter(
            Lead.id.in_(req.lead_ids)
        ).all()
        lead_id_by_email = {r.email: r.id for r in rows}

    email_dicts, blacklisted_count, _ = render_draft_emails(
        leads, template, blacklisted, lead_id_by_email
    )

    # 7. Bulk insert staged drafts
    staged_count = db.bulk_create_campaign_emails(campaign_id, email_dicts)

    log.info(
        f"Campaign {campaign_id} created: {staged_count} drafts staged, "
        f"{blacklisted_count} blacklisted"
    )

    return {
        "campaign_id": campaign_id,
        "total_staged": staged_count,
        "blacklisted_count": blacklisted_count,
        "message": f"Campaign '{req.name}' created with {staged_count} draft emails",
    }


@router.post("/api/campaigns/{campaign_id}/dispatch")
async def dispatch_campaign(campaign_id: int, db: Database = Depends(get_db)):
    """Start the background dispatch worker for a campaign."""
    # 1. Verify campaign exists and has DRAFT emails
    campaign = db.get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    stats = db.get_campaign_stats(campaign_id)
    # draft count is now only selected drafts; skipped are deselected
    if stats.get("draft", 0) == 0:
        raise HTTPException(status_code=400,
                            detail="No selected DRAFT emails to dispatch. Select at least one email first.")

    # 2. Reject if already running
    if campaign["status"] == CampaignStatus.RUNNING.value and campaign_id in _active_campaign_tasks:
        task = _active_campaign_tasks[campaign_id]
        if not task.done():
            raise HTTPException(status_code=409, detail="Campaign is already running")

    # 3. Verify at least 1 active SMTP credential
    active_creds = db.get_active_credentials()
    if not active_creds:
        raise HTTPException(status_code=400, detail="No active SMTP credentials available")

    # 4. Start background task
    # Make sure campaign status is RUNNING
    if campaign["status"] != CampaignStatus.RUNNING.value:
        db.update_campaign_status(campaign_id, CampaignStatus.RUNNING)

    async def _run_and_clean():
        try:
            await run_campaign_dispatch(campaign_id, db)
        finally:
            _active_campaign_tasks.pop(campaign_id, None)

    task = asyncio.create_task(_run_and_clean())
    _active_campaign_tasks[campaign_id] = task

    return {"message": "Dispatch started"}


@router.get("/api/campaigns")
async def list_campaigns(
        page: int = Query(1, ge=1),
        limit: int = Query(20, ge=1, le=100),
        include_test: bool = Query(False),
        db: Database = Depends(get_db),
):
    campaigns, total = db.list_campaigns(page=page, limit=limit, include_test=include_test)

    # Enrich each campaign with email stats
    for c in campaigns:
        if c.get("is_test"):
            c["stats"] = db.get_test_campaign_stats(c["id"])
        else:
            c["stats"] = db.get_campaign_stats(c["id"])

    return {
        "campaigns": campaigns,
        "total": total,
        "page": page,
        "pages": max(1, (total + limit - 1) // limit),
    }


@router.get("/api/campaigns/{campaign_id}")
async def get_campaign(campaign_id: int, db: Database = Depends(get_db)):
    campaign = db.get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    campaign["stats"] = db.get_campaign_stats(campaign_id)
    return campaign


@router.patch("/api/campaigns/{campaign_id}")
async def update_campaign_status(campaign_id: int, req: CampaignStatusUpdate, db: Database = Depends(get_db)):
    """Update campaign status. Used by the kill switch (PAUSED/CANCELLED)."""
    campaign = db.get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    try:
        new_status = CampaignStatus(req.status)
    except ValueError:
        valid = [s.value for s in CampaignStatus]
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status '{req.status}'. Must be one of: {valid}",
        )

    db.update_campaign_status(campaign_id, new_status)
    return {"message": f"Campaign status updated to {new_status.value}"}


@router.get("/api/campaigns/{campaign_id}/stats")
async def get_campaign_stats(campaign_id: int, db: Database = Depends(get_db)):
    """Lightweight stats endpoint for 3-second polling from the dashboard."""
    campaign = db.get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    stats = db.get_campaign_stats(campaign_id)
    stats["campaign_status"] = campaign["status"]
    return stats


@router.get("/api/campaigns/{campaign_id}/emails")
async def get_campaign_emails(
        campaign_id: int,
        status: str = Query(None),
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=200),
        db: Database = Depends(get_db),
):
    campaign = db.get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    emails, total = db.get_campaign_emails(
        campaign_id=campaign_id, status=status, page=page, limit=limit
    )
    return {
        "emails": emails,
        "total": total,
        "page": page,
        "pages": max(1, (total + limit - 1) // limit),
    }


@router.put("/api/campaigns/{campaign_id}/emails/{email_id}")
async def update_campaign_email(campaign_id: int, email_id: int,
                                req: CampaignEmailUpdate, db: Database = Depends(get_db)):
    """Manual body override for a specific staged email."""
    campaign = db.get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    if campaign["status"] == CampaignStatus.CANCELLED.value:
        raise HTTPException(status_code=400, detail="Cannot edit emails in a cancelled campaign")

    if not req.body.strip() or not req.subject.strip():
        raise HTTPException(status_code=400, detail="Subject and Body cannot be empty")

    if not db.update_email(email_id, req.subject, req.body):
        raise HTTPException(status_code=404, detail="Email not found or not in DRAFT status")
    return {"message": "Email body updated"}


@router.patch("/api/campaigns/{campaign_id}/emails/{email_id}/selection")
async def toggle_email_selection(campaign_id: int, email_id: int,
                                 req: EmailSelectionUpdate, db: Database = Depends(get_db)):
    """Select or deselect a DRAFT email for the next dispatch."""
    campaign = db.get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign["status"] == CampaignStatus.CANCELLED.value:
        raise HTTPException(status_code=400, detail="Cannot modify emails in a cancelled campaign")
    if not db.set_email_selection(email_id, req.is_selected):
        raise HTTPException(status_code=404, detail="Email not found or not in DRAFT status")
    return {"message": "Selection updated"}


@router.delete("/api/campaigns/{campaign_id}/emails/{email_id}", status_code=200)
async def delete_campaign_email(campaign_id: int, email_id: int, db: Database = Depends(get_db)):
    """Permanently remove a DRAFT email from a campaign."""
    campaign = db.get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign["status"] == CampaignStatus.CANCELLED.value:
        raise HTTPException(status_code=400, detail="Cannot modify emails in a cancelled campaign")
    if not db.delete_campaign_email(email_id):
        raise HTTPException(status_code=404, detail="Email not found or not in DRAFT status")
    return {"message": "Email removed from campaign"}


@router.post("/api/campaigns/{campaign_id}/emails", status_code=201)
async def add_emails_to_campaign(campaign_id: int, req: AddEmailsRequest, db: Database = Depends(get_db)):
    """Add new leads to an existing campaign by re-rendering the campaign template."""
    campaign = db.get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign["status"] == CampaignStatus.CANCELLED.value:
        raise HTTPException(status_code=400, detail="Cannot add emails to a cancelled campaign")
    if not req.lead_ids:
        raise HTTPException(status_code=400, detail="lead_ids is empty")

    template = db.get_template(campaign["template_id"])
    if not template:
        raise HTTPException(status_code=404, detail="Campaign template not found")

    leads = db.get_all_leads_for_export(lead_ids=req.lead_ids)
    if not leads:
        raise HTTPException(status_code=404, detail="No matching leads found")

    blacklisted = db.get_blacklisted_emails_set()
    existing_in_campaign = db.get_campaign_recipient_emails(campaign_id)

    with db._Session() as s:
        rows = s.query(Lead.id, Lead.email).filter(Lead.id.in_(req.lead_ids)).all()
        lead_id_by_email = {r.email: r.id for r in rows}

    email_dicts, blacklisted_count, already_count = render_draft_emails(
        leads, template, blacklisted, lead_id_by_email,
        exclude_emails=existing_in_campaign,
    )

    staged_count = db.bulk_create_campaign_emails(campaign_id, email_dicts)
    return {
        "added": staged_count,
        "blacklisted_count": blacklisted_count,
        "already_in_campaign": already_count,
        "message": f"Added {staged_count} draft emails to campaign",
    }


# ── Test Campaign routes ──────────────────────────────────────────────────────

@router.post("/api/test-campaigns/parse-csv")
async def parse_test_campaign_csv(file: UploadFile = File(...)):
    """Parse an uploaded CSV into dummy_details for a test campaign. No DB writes."""
    content = await file.read()
    rows, skipped = parse_contacts_csv(content)
    dummy_details = [
        {
            "name": r.get("name") or "",
            "designation": r.get("designation") or "",
            "email": r["email"],
            "department": r.get("department") or "",
        }
        for r in rows
    ]
    return {"dummy_details": dummy_details, "skipped": skipped}


@router.post("/api/test-campaigns", status_code=201)
async def create_test_campaign(req: TestCampaignCreate, db: Database = Depends(get_db)):
    template = db.get_template(req.template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    # Create test campaign record
    campaign_id = db.create_test_campaign(
        name=req.name,
        template_id=req.template_id,
        test_credential_id=req.test_credential_id,
        status=CampaignStatus.PAUSED
    )

    for details in req.dummy_details:
        render_vars = {
            "name": details.name or "Official",
            "designation": details.designation or "",
            "department": details.department or "",
        }

        rendered_subject = render_template_string(template["subject"], **render_vars)
        rendered_body = render_template_string(template["raw_body"], **render_vars)

        db.create_test_campaign_email(
            test_campaign_id=campaign_id,
            recipient_email=details.email,
            subject=rendered_subject,
            body=rendered_body
        )

    return {
        "campaign_id": campaign_id,
        "message": f"Test campaign '{req.name}' created"
    }


@router.post("/api/test-campaigns/{campaign_id}/dispatch")
async def dispatch_test_campaign(campaign_id: int, db: Database = Depends(get_db)):
    campaign = db.get_test_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Test Campaign not found")

    if campaign["status"] != CampaignStatus.RUNNING.value:
        db.update_test_campaign_status(campaign_id, CampaignStatus.RUNNING)

    async def _run_and_clean():
        try:
            await run_test_campaign_dispatch(campaign_id, db)
        except Exception as e:
            log.error(f"Test campaign dispatch failed: {e}")

    asyncio.create_task(_run_and_clean())
    return {"message": "Test Dispatch started"}


@router.get("/api/test-campaigns/{campaign_id}")
async def get_test_campaign(campaign_id: int, db: Database = Depends(get_db)):
    campaign = db.get_test_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Test Campaign not found")
    campaign["stats"] = db.get_test_campaign_stats(campaign_id)
    return campaign


@router.get("/api/test-campaigns/{campaign_id}/stats")
async def get_test_campaign_stats(campaign_id: int, db: Database = Depends(get_db)):
    campaign = db.get_test_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Test Campaign not found")
    stats = db.get_test_campaign_stats(campaign_id)
    stats["campaign_status"] = campaign["status"]
    return stats


@router.get("/api/test-campaigns/{campaign_id}/emails")
async def get_test_campaign_emails(
        campaign_id: int,
        status: str = Query(None),
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=200),
        db: Database = Depends(get_db),
):
    campaign = db.get_test_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Test Campaign not found")

    emails, total = db.get_test_campaign_emails(
        campaign_id=campaign_id, status=status, page=page, limit=limit
    )
    return {
        "emails": emails,
        "total": total,
        "page": page,
        "pages": max(1, (total + limit - 1) // limit),
    }


@router.put("/api/test-campaigns/{campaign_id}/emails/{email_id}")
async def update_test_campaign_email(campaign_id: int, email_id: int,
                                     req: CampaignEmailUpdate, db: Database = Depends(get_db)):
    campaign = db.get_test_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Test Campaign not found")

    if campaign["status"] == CampaignStatus.CANCELLED.value:
        raise HTTPException(status_code=400, detail="Cannot edit emails in a cancelled campaign")

    if not req.body.strip() or not req.subject.strip():
        raise HTTPException(status_code=400, detail="Subject and Body cannot be empty")

    if not db.update_test_email(email_id, req.subject, req.body):
        raise HTTPException(status_code=404, detail="Email not found or not in DRAFT status")
    return {"message": "Email body updated"}


@router.patch("/api/test-campaigns/{campaign_id}/emails/{email_id}/selection")
async def toggle_test_email_selection(campaign_id: int, email_id: int,
                                      req: EmailSelectionUpdate, db: Database = Depends(get_db)):
    """Select or deselect a DRAFT test email for the next dispatch."""
    campaign = db.get_test_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Test Campaign not found")
    if campaign["status"] == CampaignStatus.CANCELLED.value:
        raise HTTPException(status_code=400, detail="Cannot modify emails in a cancelled campaign")
    if not db.set_test_email_selection(email_id, req.is_selected):
        raise HTTPException(status_code=404, detail="Email not found or not in DRAFT status")
    return {"message": "Selection updated"}


@router.delete("/api/test-campaigns/{campaign_id}/emails/{email_id}", status_code=200)
async def delete_test_campaign_email(campaign_id: int, email_id: int, db: Database = Depends(get_db)):
    """Permanently remove a DRAFT test email from a test campaign."""
    campaign = db.get_test_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Test Campaign not found")
    if campaign["status"] == CampaignStatus.CANCELLED.value:
        raise HTTPException(status_code=400, detail="Cannot modify emails in a cancelled campaign")
    if not db.delete_test_campaign_email(email_id):
        raise HTTPException(status_code=404, detail="Email not found or not in DRAFT status")
    return {"message": "Email removed from campaign"}


@router.patch("/api/test-campaigns/{campaign_id}")
async def update_test_campaign_status(campaign_id: int, req: CampaignStatusUpdate, db: Database = Depends(get_db)):
    campaign = db.get_test_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    try:
        new_status = CampaignStatus(req.status)
    except ValueError:
        valid = [s.value for s in CampaignStatus]
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status '{req.status}'. Must be one of: {valid}",
        )

    db.update_test_campaign_status(campaign_id, new_status)
    return {"message": f"Campaign status updated to {new_status.value}"}
