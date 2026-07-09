"""Campaign generation, listing, and staging endpoints — production and test
campaigns unified via Campaign.kind. See .docs/outreach.md and
.docs/api-reference.md."""

import asyncio
import logging
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, model_validator

from . import deps
from .deps import CurrentUser, client_ip, forbid_unless_owner, get_current_user, get_db, require
from .dispatcher import resolve_credential_pool, run_campaign_dispatch
from ..db import Database, CampaignKind, CampaignStatus, Lead
from ..services.campaign_service import render_draft_emails, render_template_string
from ..services.csv_import import parse_contacts_csv

log = logging.getLogger(__name__)

router = APIRouter(tags=["campaigns"])

_active_campaign_tasks: dict[int, asyncio.Task] = {}


# ── Pydantic schemas ──────────────────────────────────────────────────────────


class DummyDetails(BaseModel):
    name: str | None = None
    designation: str | None = None
    email: str
    department: str | None = None


class CampaignCreate(BaseModel):
    name: str
    template_id: int
    kind: str = CampaignKind.PRODUCTION.value
    lead_ids: list[int] | None = None
    credential_ids: list[int] = []
    dummy_details: list[DummyDetails] | None = None
    test_credential_id: int | None = None

    @model_validator(mode="after")
    def _check_kind_fields(self):
        if self.kind not in (CampaignKind.PRODUCTION.value, CampaignKind.TEST.value):
            raise ValueError(f"kind must be one of: {CampaignKind.PRODUCTION.value}, {CampaignKind.TEST.value}")
        if self.kind == CampaignKind.PRODUCTION.value and not self.lead_ids:
            raise ValueError("lead_ids is required for a production campaign")
        if self.kind == CampaignKind.TEST.value and not self.dummy_details:
            raise ValueError("dummy_details is required for a test campaign")
        return self


class CampaignCredentialsUpdate(BaseModel):
    credential_ids: list[int]


class CampaignStatusUpdate(BaseModel):
    status: str  # One of: RUNNING, PAUSED, CANCELLED, COMPLETED


class CampaignEmailUpdate(BaseModel):
    subject: str
    body: str


class EmailSelectionUpdate(BaseModel):
    is_selected: bool


class BulkEmailSelectionUpdate(BaseModel):
    is_selected: bool


class AddEmailsRequest(BaseModel):
    lead_ids: list[int]


# ── Ownership helpers ─────────────────────────────────────────────────────────


def _get_visible_campaign(db: Database, campaign_id: int, user: CurrentUser) -> dict:
    campaign = db.get_campaign(campaign_id, owner_id=user.id, view_all=user.can("campaigns.view_all"))
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


def _get_owned_campaign(db: Database, campaign_id: int, user: CurrentUser) -> dict:
    """For mutations: any admin may act on any campaign; otherwise the caller
    must be the owner."""
    campaign = db.get_campaign(campaign_id, view_all=True)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    forbid_unless_owner(campaign["owner_id"], user)
    return campaign


# ── Routes ────────────────────────────────────────────────────────────────────


@router.post("/api/campaigns/parse-csv")
async def parse_campaign_csv(file: UploadFile = File(...)):
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


@router.post("/api/campaigns", status_code=201)
async def create_campaign(
    req: CampaignCreate,
    request: Request,
    db: Database = Depends(get_db),
    user: CurrentUser = Depends(require("campaigns.manage")),
):
    """The core draft generation endpoint — production campaigns render from
    leads, test campaigns render from ad-hoc dummy recipient details."""

    template = db.get_template(req.template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    if req.kind == CampaignKind.TEST.value:
        campaign_id = db.create_campaign(
            name=req.name,
            template_id=req.template_id,
            kind=CampaignKind.TEST.value,
            test_credential_id=req.test_credential_id,
            status=CampaignStatus.PAUSED,
            owner_id=user.id,
        )
        for details in req.dummy_details:
            render_vars = {
                "name": details.name or "Official",
                "designation": details.designation or "",
                "department": details.department or "",
            }
            db.create_campaign_email(
                campaign_id=campaign_id,
                recipient_email=details.email,
                subject=render_template_string(template["subject"], **render_vars),
                body=render_template_string(template["raw_body"], **render_vars),
            )
        db.write_audit(
            user.id, "campaign.create", "campaign", campaign_id, detail={"kind": "test"}, ip=client_ip(request)
        )
        return {
            "campaign_id": campaign_id,
            "message": f"Test campaign '{req.name}' created",
        }

    # Production
    leads = db.get_all_leads_for_export(lead_ids=req.lead_ids)
    if not leads:
        raise HTTPException(status_code=404, detail="No matching leads found")

    blacklisted = db.get_blacklisted_emails_set()
    if all(lead["email"] in blacklisted for lead in leads):
        raise HTTPException(
            status_code=422,
            detail=f"All {len(leads)} leads are blacklisted. No emails to stage.",
        )

    campaign_id = db.create_campaign(
        name=req.name,
        template_id=req.template_id,
        kind=CampaignKind.PRODUCTION.value,
        status=CampaignStatus.PAUSED,
        owner_id=user.id,
    )

    with db._Session() as s:
        rows = s.query(Lead.id, Lead.email).filter(Lead.id.in_(req.lead_ids)).all()
        lead_id_by_email = {r.email: r.id for r in rows}

    email_dicts, blacklisted_count, _ = render_draft_emails(leads, template, blacklisted, lead_id_by_email)

    staged_count = db.bulk_create_campaign_emails(campaign_id, email_dicts)

    if req.credential_ids:
        db.set_campaign_credentials(campaign_id, req.credential_ids)

    log.info(f"Campaign {campaign_id} created: {staged_count} drafts staged, {blacklisted_count} blacklisted")
    db.write_audit(
        user.id,
        "campaign.create",
        "campaign",
        campaign_id,
        detail={"kind": "production", "staged_count": staged_count},
        ip=client_ip(request),
    )

    return {
        "campaign_id": campaign_id,
        "total_staged": staged_count,
        "blacklisted_count": blacklisted_count,
        "message": f"Campaign '{req.name}' created with {staged_count} draft emails",
    }


@router.post("/api/campaigns/{campaign_id}/dispatch")
async def dispatch_campaign(
    campaign_id: int,
    request: Request,
    db: Database = Depends(get_db),
    user: CurrentUser = Depends(require("campaigns.dispatch")),
):
    """Start dispatch for a campaign (production or test). In `dispatch.mode:
    embedded` (default — desktop/dev, one process) this spawns the dispatch
    task directly; in `external` (VPS docker-compose) it only flips the
    campaign to RUNNING and a separate `cloud/dispatch_service.py` process
    picks it up on its next poll — see plan.md §19."""
    campaign = _get_owned_campaign(db, campaign_id, user)

    stats = db.get_campaign_stats(campaign_id)
    # draft count is only selected drafts; queued covers leftovers from a paused run
    if stats.get("draft", 0) == 0 and stats.get("queued", 0) == 0:
        raise HTTPException(
            status_code=400, detail="No selected draft or queued emails to dispatch. Select at least one email first."
        )

    if campaign["status"] == CampaignStatus.RUNNING.value:
        raise HTTPException(status_code=409, detail="Campaign is already running")

    assigned_ids = db.get_campaign_credential_ids(campaign_id)
    raw_creds = db.get_credentials_by_ids(assigned_ids) if assigned_ids else db.get_active_credentials()
    usable_creds = resolve_credential_pool(db, assigned_ids)
    if not usable_creds:
        if not raw_creds:
            detail = "No active SMTP credentials available for this campaign. Add or activate one in Settings."
        else:
            detail = (
                "All SMTP credentials assigned to this campaign have hit their daily send limit, "
                "are cooling down, or are disabled. Try again later, or adjust the limit in Settings."
            )
        raise HTTPException(status_code=400, detail=detail)

    db.update_campaign_status(campaign_id, CampaignStatus.RUNNING)
    db.write_audit(user.id, "campaign.dispatch", "campaign", campaign_id, ip=client_ip(request))

    dispatch_mode = deps._config.get("dispatch", {}).get("mode", "embedded")
    if dispatch_mode == "external":
        return {"message": "Dispatch queued (external dispatcher process will pick it up)"}

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
    user: CurrentUser = Depends(get_current_user),
):
    kind_filter = None if include_test else CampaignKind.PRODUCTION.value
    campaigns, total = db.list_campaigns(
        page=page,
        limit=limit,
        kind=kind_filter,
        owner_id=user.id,
        view_all=user.can("campaigns.view_all"),
    )

    for c in campaigns:
        c["stats"] = db.get_campaign_stats(c["id"])

    return {
        "campaigns": campaigns,
        "total": total,
        "page": page,
        "pages": max(1, (total + limit - 1) // limit),
    }


@router.get("/api/campaigns/{campaign_id}")
async def get_campaign(campaign_id: int, db: Database = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    campaign = _get_visible_campaign(db, campaign_id, user)
    campaign["stats"] = db.get_campaign_stats(campaign_id)
    campaign["credential_ids"] = db.get_campaign_credential_ids(campaign_id)
    return campaign


@router.put("/api/campaigns/{campaign_id}/credentials")
async def update_campaign_credentials(
    campaign_id: int,
    req: CampaignCredentialsUpdate,
    request: Request,
    db: Database = Depends(get_db),
    user: CurrentUser = Depends(require("campaigns.manage")),
):
    """Change which SMTP credentials a campaign may dispatch through, any time after
    drafting (PAUSED or RUNNING). The dispatcher re-reads this assignment on every
    send, so a change to a RUNNING campaign takes effect on its next send without
    needing to pause first. Blocked only once the campaign is CANCELLED/COMPLETED."""
    campaign = _get_owned_campaign(db, campaign_id, user)
    if campaign["status"] in (CampaignStatus.CANCELLED.value, CampaignStatus.COMPLETED.value):
        raise HTTPException(
            status_code=400, detail="Cannot change SMTP credentials on a cancelled or completed campaign"
        )

    db.set_campaign_credentials(campaign_id, req.credential_ids)
    db.write_audit(
        user.id,
        "campaign.set_credentials",
        "campaign",
        campaign_id,
        detail={"credential_ids": req.credential_ids},
        ip=client_ip(request),
    )
    return {"message": "Campaign credentials updated"}


@router.patch("/api/campaigns/{campaign_id}")
async def update_campaign_status(
    campaign_id: int,
    req: CampaignStatusUpdate,
    request: Request,
    db: Database = Depends(get_db),
    user: CurrentUser = Depends(require("campaigns.dispatch")),
):
    """Update campaign status. Used by the kill switch (PAUSED/CANCELLED)."""
    _get_owned_campaign(db, campaign_id, user)

    try:
        new_status = CampaignStatus(req.status)
    except ValueError:
        valid = [s.value for s in CampaignStatus]
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status '{req.status}'. Must be one of: {valid}",
        )

    db.update_campaign_status(campaign_id, new_status)
    db.write_audit(
        user.id,
        "campaign.set_status",
        "campaign",
        campaign_id,
        detail={"status": new_status.value},
        ip=client_ip(request),
    )
    return {"message": f"Campaign status updated to {new_status.value}"}


@router.get("/api/campaigns/{campaign_id}/stats")
async def get_campaign_stats(
    campaign_id: int, db: Database = Depends(get_db), user: CurrentUser = Depends(get_current_user)
):
    """Lightweight stats endpoint for 3-second polling from the dashboard."""
    campaign = _get_visible_campaign(db, campaign_id, user)
    stats = db.get_campaign_stats(campaign_id)
    stats["campaign_status"] = campaign["status"]
    stats["pause_reason"] = campaign.get("pause_reason")
    return stats


@router.get("/api/campaigns/{campaign_id}/emails")
async def get_campaign_emails(
    campaign_id: int,
    status: str = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    db: Database = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    _get_visible_campaign(db, campaign_id, user)

    emails, total = db.get_campaign_emails(campaign_id=campaign_id, status=status, page=page, limit=limit)
    return {
        "emails": emails,
        "total": total,
        "page": page,
        "pages": max(1, (total + limit - 1) // limit),
    }


@router.put("/api/campaigns/{campaign_id}/emails/{email_id}")
async def update_campaign_email(
    campaign_id: int,
    email_id: int,
    req: CampaignEmailUpdate,
    request: Request,
    db: Database = Depends(get_db),
    user: CurrentUser = Depends(require("campaigns.manage")),
):
    """Manual body override for a specific staged email."""
    campaign = _get_owned_campaign(db, campaign_id, user)

    if campaign["status"] == CampaignStatus.CANCELLED.value:
        raise HTTPException(status_code=400, detail="Cannot edit emails in a cancelled campaign")

    if not req.body.strip() or not req.subject.strip():
        raise HTTPException(status_code=400, detail="Subject and Body cannot be empty")

    if not db.update_email(email_id, req.subject, req.body):
        raise HTTPException(status_code=404, detail="Email not found or not in DRAFT status")
    db.write_audit(
        user.id,
        "campaign.email_update",
        "campaign_email",
        email_id,
        detail={"campaign_id": campaign_id},
        ip=client_ip(request),
    )
    return {"message": "Email body updated"}


@router.patch("/api/campaigns/{campaign_id}/emails/{email_id}/selection")
async def toggle_email_selection(
    campaign_id: int,
    email_id: int,
    req: EmailSelectionUpdate,
    db: Database = Depends(get_db),
    user: CurrentUser = Depends(require("campaigns.manage")),
):
    """Select or deselect a DRAFT or QUEUED email. Deselecting a QUEUED email pulls
    it back to DRAFT so it's excluded from the next dispatch."""
    campaign = _get_owned_campaign(db, campaign_id, user)
    if campaign["status"] == CampaignStatus.CANCELLED.value:
        raise HTTPException(status_code=400, detail="Cannot modify emails in a cancelled campaign")
    if not db.set_email_selection(email_id, req.is_selected):
        raise HTTPException(status_code=404, detail="Email not found or not in an editable status")
    return {"message": "Selection updated"}


@router.patch("/api/campaigns/{campaign_id}/emails/selection-all")
async def bulk_toggle_email_selection(
    campaign_id: int,
    req: BulkEmailSelectionUpdate,
    db: Database = Depends(get_db),
    user: CurrentUser = Depends(require("campaigns.manage")),
):
    """Select or deselect every DRAFT email in the campaign, regardless of pagination."""
    campaign = _get_owned_campaign(db, campaign_id, user)
    if campaign["status"] == CampaignStatus.CANCELLED.value:
        raise HTTPException(status_code=400, detail="Cannot modify emails in a cancelled campaign")
    updated = db.set_all_email_selection(campaign_id, req.is_selected)
    return {"message": f"Updated selection for {updated} email(s)", "updated": updated}


@router.delete("/api/campaigns/{campaign_id}/emails/{email_id}", status_code=200)
async def delete_campaign_email(
    campaign_id: int,
    email_id: int,
    request: Request,
    db: Database = Depends(get_db),
    user: CurrentUser = Depends(require("campaigns.manage")),
):
    """Permanently remove a DRAFT email from a campaign."""
    campaign = _get_owned_campaign(db, campaign_id, user)
    if campaign["status"] == CampaignStatus.CANCELLED.value:
        raise HTTPException(status_code=400, detail="Cannot modify emails in a cancelled campaign")
    if not db.delete_campaign_email(email_id):
        raise HTTPException(status_code=404, detail="Email not found or not in DRAFT status")
    db.write_audit(
        user.id,
        "campaign.email_delete",
        "campaign_email",
        email_id,
        detail={"campaign_id": campaign_id},
        ip=client_ip(request),
    )
    return {"message": "Email removed from campaign"}


@router.post("/api/campaigns/{campaign_id}/emails", status_code=201)
async def add_emails_to_campaign(
    campaign_id: int,
    req: AddEmailsRequest,
    request: Request,
    db: Database = Depends(get_db),
    user: CurrentUser = Depends(require("campaigns.manage")),
):
    """Add new leads to an existing (production) campaign by re-rendering the campaign template."""
    campaign = _get_owned_campaign(db, campaign_id, user)
    if campaign["kind"] == CampaignKind.TEST.value:
        raise HTTPException(status_code=400, detail="Cannot add leads to a test campaign")
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
        leads,
        template,
        blacklisted,
        lead_id_by_email,
        exclude_emails=existing_in_campaign,
    )

    staged_count = db.bulk_create_campaign_emails(campaign_id, email_dicts)
    db.write_audit(
        user.id, "campaign.add_emails", "campaign", campaign_id, detail={"added": staged_count}, ip=client_ip(request)
    )
    return {
        "added": staged_count,
        "blacklisted_count": blacklisted_count,
        "already_in_campaign": already_count,
        "message": f"Added {staged_count} draft emails to campaign",
    }
