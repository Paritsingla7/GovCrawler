import datetime

from sqlalchemy import func, literal, or_
from sqlalchemy.exc import IntegrityError

from ..enums import CampaignStatus, EmailStatus
from ..tables.outreach import (
    Blacklist, Campaign, CampaignEmail, EmailTemplate, SMTPCredential,
    TestCampaign, TestCampaignEmail,
)


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

    def create_campaign(self, name: str, template_id: int,
                        status: CampaignStatus = None) -> int:
        if status is None:
            status = CampaignStatus.RUNNING
        with self._Session() as s:
            c = Campaign(name=name, template_id=template_id, status=status)
            s.add(c)
            s.commit()
            return c.id

    def get_campaign(self, campaign_id: int) -> dict | None:
        with self._Session() as s:
            c = s.query(Campaign).filter_by(id=campaign_id).first()
            if not c:
                return None
            return {"id": c.id, "name": c.name, "template_id": c.template_id,
                    "status": c.status.value,
                    "created_at": c.created_at.isoformat() if c.created_at else None}

    def list_campaigns(self, page: int = 1, limit: int = 20, include_test: bool = True) -> tuple[list[dict], int]:
        with self._Session() as s:
            q1 = s.query(
                Campaign.id.label('id'),
                Campaign.name.label('name'),
                Campaign.template_id.label('template_id'),
                Campaign.status.label('status'),
                Campaign.created_at.label('created_at'),
                literal(False).label('is_test')
            )
            q2 = s.query(
                TestCampaign.id.label('id'),
                TestCampaign.name.label('name'),
                TestCampaign.template_id.label('template_id'),
                TestCampaign.status.label('status'),
                TestCampaign.created_at.label('created_at'),
                literal(True).label('is_test')
            )

            if include_test:
                subq = q1.union_all(q2).subquery()
            else:
                subq = q1.subquery()

            total = s.query(subq).count()
            offset = (page - 1) * limit
            rows = s.query(subq).order_by(subq.c.created_at.desc()).offset(offset).limit(limit).all()

            return (
                [{"id": r.id, "name": r.name, "template_id": r.template_id,
                  "status": r.status.value if hasattr(r.status, 'value') else r.status, "is_test": r.is_test,
                  "created_at": r.created_at.isoformat() if r.created_at and hasattr(r.created_at, 'isoformat') else (
                      r.created_at if isinstance(r.created_at, str) else None)}
                 for r in rows],
                total,
            )

    def update_campaign_status(self, campaign_id: int,
                               new_status: CampaignStatus) -> bool:
        with self._Session() as s:
            updated = s.query(Campaign).filter_by(id=campaign_id).update(
                {"status": new_status}
            )
            s.commit()
            return updated > 0

    # ── CampaignEmail ─────────────────────────────────────────────────────────

    def get_campaign_recipient_emails(self, campaign_id: int) -> set:
        with self._Session() as s:
            rows = s.query(CampaignEmail.recipient_email).filter_by(campaign_id=campaign_id).all()
            return {r.recipient_email for r in rows}

    def bulk_create_campaign_emails(self, campaign_id: int,
                                    emails: list[dict]) -> int:
        """Bulk insert rendered draft emails. Each dict must have:
        lead_id, recipient_email, subject, body. Optional: is_selected, missing_fields."""
        with self._Session() as s:
            objects = [
                CampaignEmail(
                    campaign_id=campaign_id,
                    lead_id=e["lead_id"],
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
        """Aggregate counts by status for a campaign's emails."""
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
        """Toggle selection on a DRAFT email."""
        with self._Session() as s:
            updated = s.query(CampaignEmail).filter_by(
                id=email_id, status=EmailStatus.DRAFT
            ).update({"is_selected": is_selected})
            s.commit()
            return updated > 0

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

    def mark_email_sent(self, email_id: int) -> None:
        """Mark as SENT with current timestamp."""
        with self._Session() as s:
            s.query(CampaignEmail).filter_by(id=email_id).update({
                "status": EmailStatus.SENT,
                "sent_at": datetime.datetime.utcnow()
            })
            s.commit()

    def mark_email_failed(self, email_id: int, error_message: str) -> None:
        """Mark as FAILED with the error reason."""
        with self._Session() as s:
            s.query(CampaignEmail).filter_by(id=email_id).update({
                "status": EmailStatus.FAILED,
                "error_message": error_message
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

    # ── TestCampaign ──────────────────────────────────────────────────────────

    def create_test_campaign(self, name: str, template_id: int, test_credential_id: int | None = None,
                             status: CampaignStatus = None) -> int:
        if status is None:
            status = CampaignStatus.RUNNING
        with self._Session() as s:
            c = TestCampaign(name=name, template_id=template_id, test_credential_id=test_credential_id, status=status)
            s.add(c)
            s.commit()
            return c.id

    def create_test_campaign_email(self, test_campaign_id: int, recipient_email: str, subject: str, body: str) -> int:
        with self._Session() as s:
            e = TestCampaignEmail(test_campaign_id=test_campaign_id, recipient_email=recipient_email, subject=subject,
                                  body=body)
            s.add(e)
            s.commit()
            return e.id

    def get_test_campaign(self, campaign_id: int) -> dict | None:
        with self._Session() as s:
            c = s.query(TestCampaign).filter_by(id=campaign_id).first()
            if not c: return None
            return {"id": c.id, "name": c.name, "template_id": c.template_id,
                    "test_credential_id": c.test_credential_id, "status": c.status.value, "is_test": True,
                    "created_at": c.created_at.isoformat() if c.created_at else None}

    def update_test_campaign_status(self, campaign_id: int, new_status: CampaignStatus) -> bool:
        with self._Session() as s:
            updated = s.query(TestCampaign).filter_by(id=campaign_id).update({"status": new_status})
            s.commit()
            return updated > 0

    def get_test_campaign_stats(self, campaign_id: int) -> dict:
        with self._Session() as s:
            rows = s.query(TestCampaignEmail.status, func.count(TestCampaignEmail.id)).filter_by(
                test_campaign_id=campaign_id).group_by(TestCampaignEmail.status).all()
            stats = {"draft": 0, "queued": 0, "sent": 0, "failed": 0, "total": 0}
            for status_val, count in rows:
                stats[status_val.value.lower()] = count
                stats["total"] += count
            return stats

    def get_test_campaign_emails(self, campaign_id: int, status: str = None, page: int = 1, limit: int = 50) -> tuple[
        list[dict], int]:
        with self._Session() as s:
            q = s.query(TestCampaignEmail).filter_by(test_campaign_id=campaign_id)
            if status: q = q.filter(TestCampaignEmail.status == EmailStatus(status))
            total = q.count()
            offset = (page - 1) * limit
            rows = q.order_by(TestCampaignEmail.id).offset(offset).limit(limit).all()
            return ([{"id": e.id, "campaign_id": e.test_campaign_id, "lead_id": None,
                      "recipient_email": e.recipient_email, "subject": e.subject, "body": e.body,
                      "status": e.status.value, "is_selected": e.is_selected,
                      "missing_fields": e.missing_fields,
                      "error_message": e.error_message,
                      "sent_at": e.sent_at.isoformat() if e.sent_at else None} for e in rows], total)

    def queue_test_campaign_emails(self, campaign_id: int) -> int:
        with self._Session() as s:
            updated = s.query(TestCampaignEmail).filter_by(test_campaign_id=campaign_id,
                                                           status=EmailStatus.DRAFT).update(
                {"status": EmailStatus.QUEUED})
            s.commit()
            return updated

    def get_next_queued_test_email(self, campaign_id: int) -> dict | None:
        with self._Session() as s:
            e = s.query(TestCampaignEmail).filter_by(test_campaign_id=campaign_id, status=EmailStatus.QUEUED).order_by(
                TestCampaignEmail.id).first()
            if not e: return None
            return {"id": e.id, "campaign_id": e.test_campaign_id, "recipient_email": e.recipient_email,
                    "subject": e.subject, "body": e.body}

    def update_test_email(self, email_id: int, new_subject: str, new_body: str) -> bool:
        with self._Session() as s:
            updated = s.query(TestCampaignEmail).filter_by(id=email_id, status=EmailStatus.DRAFT).update({
                "subject": new_subject,
                "body": new_body
            })
            s.commit()
            return updated > 0

    def set_test_email_selection(self, email_id: int, is_selected: bool) -> bool:
        """Toggle selection on a DRAFT test email."""
        with self._Session() as s:
            updated = s.query(TestCampaignEmail).filter_by(
                id=email_id, status=EmailStatus.DRAFT
            ).update({"is_selected": is_selected})
            s.commit()
            return updated > 0

    def delete_test_campaign_email(self, email_id: int) -> bool:
        """Remove a DRAFT test email from a test campaign."""
        with self._Session() as s:
            deleted = s.query(TestCampaignEmail).filter_by(
                id=email_id, status=EmailStatus.DRAFT
            ).delete()
            s.commit()
            return deleted > 0

    def mark_test_email_sent(self, email_id: int) -> None:
        with self._Session() as s:
            s.query(TestCampaignEmail).filter_by(id=email_id).update(
                {"status": EmailStatus.SENT, "sent_at": datetime.datetime.utcnow()})
            s.commit()

    def mark_test_email_failed(self, email_id: int, error_message: str) -> None:
        with self._Session() as s:
            s.query(TestCampaignEmail).filter_by(id=email_id).update(
                {"status": EmailStatus.FAILED, "error_message": error_message})
            s.commit()

    def cancel_remaining_queued_test(self, campaign_id: int) -> int:
        with self._Session() as s:
            updated = s.query(TestCampaignEmail).filter_by(test_campaign_id=campaign_id,
                                                           status=EmailStatus.QUEUED).update(
                {"status": EmailStatus.FAILED, "error_message": "Campaign cancelled"})
            s.commit()
            return updated

    # ── SMTP Credential operations ────────────────────────────────────────────

    def create_credential(self, host: str, port: int, username: str, password: str) -> int:
        with self._Session() as s:
            c = SMTPCredential(host=host, port=port, username=username, password=password)
            s.add(c)
            s.commit()
            return c.id

    def get_credential(self, credential_id: int) -> dict | None:
        with self._Session() as s:
            c = s.query(SMTPCredential).filter_by(id=credential_id).first()
            if not c:
                return None
            return {"id": c.id, "host": c.host, "port": c.port,
                    "username": c.username, "password": c.password,
                    "is_active": c.is_active,
                    "cooldown_until": c.cooldown_until.isoformat() if c.cooldown_until else None}

    def list_credentials(self) -> list[dict]:
        with self._Session() as s:
            rows = s.query(SMTPCredential).order_by(SMTPCredential.id).all()
            return [{"id": c.id, "host": c.host, "port": c.port,
                     "username": c.username, "is_active": c.is_active,
                     "cooldown_until": c.cooldown_until.isoformat() if c.cooldown_until else None}
                    for c in rows]

    def update_credential(self, credential_id: int, **kwargs) -> bool:
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
                     "username": c.username, "password": c.password} for c in rows]

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
