import datetime
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from ..enums import CampaignKind, CampaignStatus, EmailStatus
from ..tables.outreach import (
    Blacklist, Campaign, CampaignCredential, CampaignEmail, EmailTemplate, SMTPCredential,
)
from ...security.crypto import decrypt_password, encrypt_password


class OutreachMixin:
    # ── EmailTemplate ─────────────────────────────────────────────────────────

    def create_template(self, name: str, subject: str, raw_body: str) -> int:
        with self._Session() as s:
            t = EmailTemplate(name=name, subject=subject, raw_body=raw_body)
            s.add(t)
            s.commit()
            return t.id

    def get_template(self, template_id: int) -> dict | None:
        with self._Session() as s:
            t = s.query(EmailTemplate).filter_by(id=template_id).first()
            if not t:
                return None
            return {"id": t.id, "name": t.name, "subject": t.subject,
                    "raw_body": t.raw_body}

    def list_templates(self) -> list[dict]:
        with self._Session() as s:
            rows = s.query(EmailTemplate).order_by(EmailTemplate.id.desc()).all()
            return [{"id": t.id, "name": t.name, "subject": t.subject,
                     "raw_body": t.raw_body} for t in rows]

    def update_template(self, template_id: int, **kwargs) -> bool:
        with self._Session() as s:
            updated = s.query(EmailTemplate).filter_by(id=template_id).update(
                {k: v for k, v in kwargs.items() if v is not None}
            )
            s.commit()
            return updated > 0

    def delete_template(self, template_id: int) -> bool:
        with self._Session() as s:
            deleted = s.query(EmailTemplate).filter_by(id=template_id).delete()
            s.commit()
            return deleted > 0

    # ── Blacklist ─────────────────────────────────────────────────────────────

    def add_to_blacklist(self, email: str, domain: str, reason: str = None) -> bool:
        with self._Session() as s:
            try:
                s.add(Blacklist(email=email.lower(), domain=domain.lower(),
                                reason=reason))
                s.commit()
                return True
            except IntegrityError:
                s.rollback()
                return False

    def remove_from_blacklist(self, blacklist_id: int) -> bool:
        with self._Session() as s:
            deleted = s.query(Blacklist).filter_by(id=blacklist_id).delete()
            s.commit()
            return deleted > 0

    def list_blacklist(self, page: int = 1, limit: int = 50) -> tuple[list[dict], int]:
        with self._Session() as s:
            total = s.query(Blacklist).count()
            offset = (page - 1) * limit
            rows = (s.query(Blacklist)
                    .order_by(Blacklist.id.desc())
                    .offset(offset).limit(limit).all())
            return (
                [{"id": b.id, "email": b.email, "domain": b.domain,
                  "reason": b.reason} for b in rows],
                total,
            )

    def get_blacklisted_emails_set(self) -> set[str]:
        """Load all blacklisted emails into a set for O(1) lookup during draft generation."""
        with self._Session() as s:
            rows = s.query(Blacklist.email).all()
            return {r[0] for r in rows}

    # ── Campaign ──────────────────────────────────────────────────────────────

    @staticmethod
    def _campaign_dict(c: Campaign) -> dict:
        return {"id": c.id, "name": c.name, "template_id": c.template_id,
                "kind": c.kind, "test_credential_id": c.test_credential_id,
                "status": c.status.value, "owner_id": c.owner_id,
                "pause_reason": c.pause_reason,
                "created_at": c.created_at.isoformat() if c.created_at else None}

    def create_campaign(self, name: str, template_id: int,
                        kind: str = CampaignKind.PRODUCTION.value,
                        test_credential_id: int | None = None,
                        status: CampaignStatus = None,
                        owner_id: int | None = None) -> int:
        if status is None:
            status = CampaignStatus.RUNNING
        with self._Session() as s:
            c = Campaign(name=name, template_id=template_id, kind=kind,
                        test_credential_id=test_credential_id, status=status,
                        owner_id=owner_id)
            s.add(c)
            s.commit()
            return c.id

    def get_campaign(self, campaign_id: int, owner_id: int | None = None,
                     view_all: bool = False) -> dict | None:
        with self._Session() as s:
            q = s.query(Campaign).filter_by(id=campaign_id)
            if not view_all:
                q = q.filter(Campaign.owner_id == owner_id)
            c = q.first()
            return self._campaign_dict(c) if c else None

    def list_campaigns(self, page: int = 1, limit: int = 20, kind: str | None = None,
                       owner_id: int | None = None, view_all: bool = False) -> tuple[list[dict], int]:
        with self._Session() as s:
            q = s.query(Campaign)
            if kind:
                q = q.filter(Campaign.kind == kind)
            if not view_all:
                q = q.filter(Campaign.owner_id == owner_id)
            total = q.count()
            offset = (page - 1) * limit
            rows = q.order_by(Campaign.created_at.desc()).offset(offset).limit(limit).all()
            return ([self._campaign_dict(c) for c in rows], total)

    def update_campaign_status(self, campaign_id: int, new_status: CampaignStatus,
                               reason: str | None = None) -> bool:
        """reason is stored as pause_reason — pass it when auto-pausing for a
        specific, user-facing cause (e.g. no usable credentials). Any status
        change without a reason clears it, since a stale reason from a prior
        pause is no longer relevant once the status has moved on."""
        with self._Session() as s:
            updated = s.query(Campaign).filter_by(id=campaign_id).update(
                {"status": new_status, "pause_reason": reason}
            )
            s.commit()
            return updated > 0

    # ── Campaign ↔ SMTPCredential assignment ─────────────────────────────────
    # Production campaigns use the campaign_credentials pool (junction table);
    # test campaigns use the single Campaign.test_credential_id column — no
    # round-robin needed for a one-off dummy-recipient test run.

    def set_campaign_credentials(self, campaign_id: int, credential_ids: list[int]) -> None:
        """Replace the set of credentials a campaign is allowed to send through.
        Empty list means no explicit restriction (dispatcher falls back to all active credentials)."""
        with self._Session() as s:
            c = s.query(Campaign).filter_by(id=campaign_id).first()
            if not c:
                return
            if c.kind == CampaignKind.TEST.value:
                c.test_credential_id = credential_ids[0] if credential_ids else None
                s.commit()
                return
            s.query(CampaignCredential).filter_by(campaign_id=campaign_id).delete()
            s.add_all([
                CampaignCredential(campaign_id=campaign_id, credential_id=cid)
                for cid in credential_ids
            ])
            s.commit()

    def get_campaign_credential_ids(self, campaign_id: int) -> list[int]:
        with self._Session() as s:
            c = s.query(Campaign).filter_by(id=campaign_id).first()
            if not c:
                return []
            if c.kind == CampaignKind.TEST.value:
                return [c.test_credential_id] if c.test_credential_id else []
            rows = s.query(CampaignCredential.credential_id).filter_by(campaign_id=campaign_id).all()
            return [r.credential_id for r in rows]

    def get_credentials_by_ids(self, credential_ids: list[int]) -> list[dict]:
        """Active, non-cooling credentials among the given ids. Used to resolve a
        campaign's explicit credential assignment to a send pool."""
        if not credential_ids:
            return []
        now = datetime.datetime.utcnow()
        with self._Session() as s:
            rows = s.query(SMTPCredential).filter(
                SMTPCredential.id.in_(credential_ids),
                SMTPCredential.is_active == True,
                or_(SMTPCredential.cooldown_until == None, SMTPCredential.cooldown_until < now),
            ).all()
            return [{"id": c.id, "host": c.host, "port": c.port,
                     "username": c.username, "password": decrypt_password(c.password_encrypted, self._cred_enc_key),
                     "daily_send_limit": c.daily_send_limit} for c in rows]

    # ── CampaignEmail ─────────────────────────────────────────────────────────

    def get_campaign_recipient_emails(self, campaign_id: int) -> set:
        with self._Session() as s:
            rows = s.query(CampaignEmail.recipient_email).filter_by(campaign_id=campaign_id).all()
            return {r.recipient_email for r in rows}

    def bulk_create_campaign_emails(self, campaign_id: int,
                                    emails: list[dict]) -> int:
        """Bulk insert rendered draft emails. Each dict must have:
        recipient_email, subject, body. Optional: lead_id (None for test/dummy
        recipients), is_selected, missing_fields."""
        with self._Session() as s:
            objects = [
                CampaignEmail(
                    campaign_id=campaign_id,
                    lead_id=e.get("lead_id"),
                    recipient_email=e["recipient_email"],
                    subject=e["subject"],
                    body=e["body"],
                    status=EmailStatus.DRAFT,
                    is_selected=e.get("is_selected", True),
                    missing_fields=e.get("missing_fields"),
                )
                for e in emails
            ]
            s.add_all(objects)
            s.commit()
            return len(objects)

    def create_campaign_email(self, campaign_id: int, recipient_email: str, subject: str,
                              body: str, lead_id: int | None = None) -> int:
        """Single-row insert — used for test-campaign dummy recipients (created
        one at a time from a rendered dummy_details list)."""
        with self._Session() as s:
            e = CampaignEmail(campaign_id=campaign_id, lead_id=lead_id,
                              recipient_email=recipient_email, subject=subject, body=body)
            s.add(e)
            s.commit()
            return e.id

    def get_campaign_emails(self, campaign_id: int, status: str = None,
                            page: int = 1, limit: int = 50) -> tuple[list[dict], int]:
        with self._Session() as s:
            q = s.query(CampaignEmail).filter_by(campaign_id=campaign_id)
            if status:
                q = q.filter(CampaignEmail.status == EmailStatus(status))
            total = q.count()
            offset = (page - 1) * limit
            rows = q.order_by(CampaignEmail.id).offset(offset).limit(limit).all()
            return (
                [{"id": e.id, "campaign_id": e.campaign_id,
                  "lead_id": e.lead_id, "recipient_email": e.recipient_email,
                  "subject": e.subject, "body": e.body,
                  "status": e.status.value,
                  "is_selected": e.is_selected,
                  "missing_fields": e.missing_fields,
                  "error_message": e.error_message,
                  "sent_at": e.sent_at.isoformat() if e.sent_at else None}
                 for e in rows],
                total,
            )

    def update_email(self, email_id: int, new_subject: str, new_body: str) -> bool:
        """Manual override for a staged email's subject and body text."""
        with self._Session() as s:
            updated = s.query(CampaignEmail).filter_by(id=email_id).update({
                "subject": new_subject,
                "body": new_body
            })
            s.commit()
            return updated > 0

    def get_campaign_stats(self, campaign_id: int) -> dict:
        """Aggregate counts by status for a campaign's emails (production or test)."""
        with self._Session() as s:
            rows = (
                s.query(CampaignEmail.status, func.count(CampaignEmail.id))
                .filter_by(campaign_id=campaign_id)
                .group_by(CampaignEmail.status)
                .all()
            )
            stats = {"draft": 0, "queued": 0, "sent": 0, "failed": 0, "skipped": 0, "total": 0}
            for status_val, count in rows:
                stats[status_val.value.lower()] = count
                stats["total"] += count
            # skipped = deselected DRAFT emails (not counted in dispatch)
            skipped = s.query(CampaignEmail).filter_by(
                campaign_id=campaign_id, status=EmailStatus.DRAFT, is_selected=False
            ).count()
            stats["skipped"] = skipped
            stats["draft"] = stats["draft"] - skipped  # selected drafts only
            return stats

    def set_email_selection(self, email_id: int, is_selected: bool) -> bool:
        """Toggle selection on a DRAFT email. Deselecting a QUEUED email (e.g. one
        left over from a paused run) pulls it back to DRAFT so it's excluded from
        the next dispatch instead of still being sent."""
        with self._Session() as s:
            email = s.query(CampaignEmail).filter_by(id=email_id).first()
            if not email or email.status not in (EmailStatus.DRAFT, EmailStatus.QUEUED):
                return False
            updates = {"is_selected": is_selected}
            if email.status == EmailStatus.QUEUED and not is_selected:
                updates["status"] = EmailStatus.DRAFT
            s.query(CampaignEmail).filter_by(id=email_id).update(updates)
            s.commit()
            return True

    def set_all_email_selection(self, campaign_id: int, is_selected: bool) -> int:
        """Bulk select/deselect every DRAFT email in a campaign, across all pages.
        When deselecting, also pulls any QUEUED leftovers back to DRAFT (see
        set_email_selection)."""
        with self._Session() as s:
            updated = s.query(CampaignEmail).filter_by(
                campaign_id=campaign_id, status=EmailStatus.DRAFT
            ).update({"is_selected": is_selected})
            if not is_selected:
                updated += s.query(CampaignEmail).filter_by(
                    campaign_id=campaign_id, status=EmailStatus.QUEUED
                ).update({"status": EmailStatus.DRAFT, "is_selected": False})
            s.commit()
            return updated

    def delete_campaign_email(self, email_id: int) -> bool:
        """Remove a DRAFT email from a campaign entirely."""
        with self._Session() as s:
            deleted = s.query(CampaignEmail).filter_by(
                id=email_id, status=EmailStatus.DRAFT
            ).delete()
            s.commit()
            return deleted > 0

    # ── Dispatcher operations ─────────────────────────────────────────────────

    def queue_campaign_emails(self, campaign_id: int) -> int:
        """Bulk flip selected DRAFT → QUEUED. Returns count updated."""
        with self._Session() as s:
            updated = s.query(CampaignEmail).filter_by(
                campaign_id=campaign_id, status=EmailStatus.DRAFT, is_selected=True
            ).update({"status": EmailStatus.QUEUED})
            s.commit()
            return updated

    def has_remaining_drafts(self, campaign_id: int) -> bool:
        """True if any DRAFT emails (selected or not) still exist for the campaign."""
        with self._Session() as s:
            return s.query(CampaignEmail).filter_by(
                campaign_id=campaign_id, status=EmailStatus.DRAFT
            ).first() is not None

    def get_next_queued_email(self, campaign_id: int) -> dict | None:
        """Fetch one QUEUED email for processing. Returns None when done."""
        with self._Session() as s:
            e = s.query(CampaignEmail).filter_by(
                campaign_id=campaign_id, status=EmailStatus.QUEUED
            ).order_by(CampaignEmail.id).first()
            if not e:
                return None
            return {"id": e.id, "campaign_id": e.campaign_id,
                    "recipient_email": e.recipient_email, "subject": e.subject,
                    "body": e.body}

    def mark_email_sent(self, email_id: int, credential_id: int | None = None) -> None:
        """Mark as SENT with current timestamp."""
        with self._Session() as s:
            s.query(CampaignEmail).filter_by(id=email_id).update({
                "status": EmailStatus.SENT,
                "sent_at": datetime.datetime.utcnow(),
                "credential_id": credential_id,
            })
            s.commit()

    def mark_email_failed(self, email_id: int, error_message: str, credential_id: int | None = None) -> None:
        """Mark as FAILED with the error reason."""
        with self._Session() as s:
            s.query(CampaignEmail).filter_by(id=email_id).update({
                "status": EmailStatus.FAILED,
                "error_message": error_message,
                "credential_id": credential_id,
            })
            s.commit()

    def cancel_remaining_queued(self, campaign_id: int) -> int:
        """Bulk cancel remaining QUEUED emails. Returns count."""
        with self._Session() as s:
            updated = s.query(CampaignEmail).filter_by(
                campaign_id=campaign_id, status=EmailStatus.QUEUED
            ).update({
                "status": EmailStatus.FAILED,
                "error_message": "Campaign cancelled"
            })
            s.commit()
            return updated

    # ── SMTP Credential operations ────────────────────────────────────────────

    def create_credential(self, host: str, port: int, username: str, password: str,
                          daily_send_limit: int | None = None) -> int:
        with self._Session() as s:
            c = SMTPCredential(host=host, port=port, username=username,
                               password_encrypted=encrypt_password(password, self._cred_enc_key),
                               daily_send_limit=daily_send_limit)
            s.add(c)
            s.commit()
            return c.id

    def get_credential(self, credential_id: int) -> dict | None:
        with self._Session() as s:
            c = s.query(SMTPCredential).filter_by(id=credential_id).first()
            if not c:
                return None
            return {"id": c.id, "host": c.host, "port": c.port,
                    "username": c.username, "password": decrypt_password(c.password_encrypted, self._cred_enc_key),
                    "is_active": c.is_active,
                    "cooldown_until": c.cooldown_until.isoformat() if c.cooldown_until else None,
                    "daily_send_limit": c.daily_send_limit}

    def list_credentials(self) -> list[dict]:
        with self._Session() as s:
            rows = s.query(SMTPCredential).order_by(SMTPCredential.id).all()
            creds = [{"id": c.id, "host": c.host, "port": c.port,
                      "username": c.username, "is_active": c.is_active,
                      "cooldown_until": c.cooldown_until.isoformat() if c.cooldown_until else None,
                      "daily_send_limit": c.daily_send_limit}
                     for c in rows]
        for c in creds:
            c.update(self.get_credential_health(c["id"]))
        return creds

    def update_credential(self, credential_id: int, **kwargs) -> bool:
        if "password" in kwargs and kwargs["password"] is not None:
            kwargs["password_encrypted"] = encrypt_password(kwargs.pop("password"), self._cred_enc_key)
        with self._Session() as s:
            updated = s.query(SMTPCredential).filter_by(id=credential_id).update(
                {k: v for k, v in kwargs.items() if v is not None}
            )
            s.commit()
            return updated > 0

    def delete_credential(self, credential_id: int) -> bool:
        with self._Session() as s:
            deleted = s.query(SMTPCredential).filter_by(id=credential_id).delete()
            s.commit()
            return deleted > 0

    def get_active_credentials(self) -> list[dict]:
        """Load credentials where is_active=True AND cooldown expired."""
        now = datetime.datetime.utcnow()
        with self._Session() as s:
            rows = s.query(SMTPCredential).filter(
                SMTPCredential.is_active == True,
                or_(SMTPCredential.cooldown_until == None, SMTPCredential.cooldown_until < now)
            ).all()
            return [{"id": c.id, "host": c.host, "port": c.port,
                     "username": c.username, "password": decrypt_password(c.password_encrypted, self._cred_enc_key),
                     "daily_send_limit": c.daily_send_limit} for c in rows]

    def disable_credential(self, credential_id: int) -> None:
        """Permanently disable (auth failure)."""
        with self._Session() as s:
            s.query(SMTPCredential).filter_by(id=credential_id).update({"is_active": False})
            s.commit()

    def set_credential_cooldown(self, credential_id: int, until: datetime.datetime) -> None:
        """Temporarily pause (rate limited)."""
        with self._Session() as s:
            s.query(SMTPCredential).filter_by(id=credential_id).update({"cooldown_until": until})
            s.commit()

    def get_credential_sent_count_today(self, credential_id: int) -> int:
        """Count of emails successfully sent via this credential since 00:00 UTC today."""
        today_start = datetime.datetime.combine(datetime.datetime.utcnow().date(), datetime.time.min)
        with self._Session() as s:
            return s.query(CampaignEmail).filter(
                CampaignEmail.credential_id == credential_id,
                CampaignEmail.status == EmailStatus.SENT,
                CampaignEmail.sent_at >= today_start,
            ).count()

    def get_credential_health(self, credential_id: int) -> dict:
        """Send/failure counts for a credential, all-time and today. Used to surface
        per-mailbox health in the UI before it gets rate-limited or blacklisted by a provider."""
        today_start = datetime.datetime.combine(datetime.datetime.utcnow().date(), datetime.time.min)
        with self._Session() as s:
            sent_total = 0
            failed_total = 0
            rows = (
                s.query(CampaignEmail.status, func.count(CampaignEmail.id))
                .filter(CampaignEmail.credential_id == credential_id)
                .group_by(CampaignEmail.status)
                .all()
            )
            for status_val, count in rows:
                if status_val == EmailStatus.SENT:
                    sent_total += count
                elif status_val == EmailStatus.FAILED:
                    failed_total += count
            sent_today = s.query(CampaignEmail).filter(
                CampaignEmail.credential_id == credential_id,
                CampaignEmail.status == EmailStatus.SENT,
                CampaignEmail.sent_at >= today_start,
            ).count()
            return {"sent_total": sent_total, "failed_total": failed_total, "sent_today": sent_today}
