import datetime
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy import Enum as SqlEnum

from ..base import Base
from ..enums import CampaignKind, CampaignStatus, EmailStatus


class Campaign(Base):
    __tablename__ = "campaigns"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    template_id = Column(Integer, ForeignKey("email_templates.id"), nullable=True)
    kind = Column(String, nullable=False, default=CampaignKind.PRODUCTION.value)
    test_credential_id = Column(Integer, ForeignKey("smtp_credentials.id"), nullable=True)  # kind='test' only
    status = Column(SqlEnum(CampaignStatus), nullable=False)
    owner_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    pause_reason = Column(String, nullable=True)  # set when the dispatcher auto-pauses; cleared on any status change


class EmailTemplate(Base):
    __tablename__ = "email_templates"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    subject = Column(String, nullable=False)
    raw_body = Column(Text, nullable=False)


class SMTPCredential(Base):
    __tablename__ = "smtp_credentials"
    id = Column(Integer, primary_key=True, autoincrement=True)
    host = Column(String, nullable=False)
    port = Column(Integer, nullable=False)
    username = Column(String, nullable=False)
    password_encrypted = Column(LargeBinary, nullable=False)  # Fernet-encrypted, see portal/security/crypto.py
    is_active = Column(Boolean, default=True, nullable=False)
    cooldown_until = Column(DateTime, nullable=True)
    daily_send_limit = Column(Integer, nullable=True)  # None = unlimited


class CampaignCredential(Base):
    """Many-to-many: which SMTP credentials a (production) campaign is allowed
    to send through. Test campaigns use Campaign.test_credential_id instead —
    a single-credential assignment, not a pool — since dummy-recipient test
    runs don't need round-robin."""
    __tablename__ = "campaign_credentials"
    id = Column(Integer, primary_key=True, autoincrement=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id"), nullable=False, index=True)
    credential_id = Column(Integer, ForeignKey("smtp_credentials.id"), nullable=False)
    __table_args__ = (
        UniqueConstraint("campaign_id", "credential_id", name="uq_campaign_credential"),
    )


class CampaignEmail(Base):
    __tablename__ = "campaign_emails"
    id = Column(Integer, primary_key=True, autoincrement=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id"), nullable=False)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=True)  # NULL for test/dummy recipients
    recipient_email = Column(String, nullable=False)
    subject = Column(String, nullable=False)
    body = Column(Text, nullable=False)
    status = Column(SqlEnum(EmailStatus), nullable=False, default=EmailStatus.DRAFT)
    is_selected = Column(Boolean, nullable=False, default=True)
    missing_fields = Column(String, nullable=True)  # comma-separated list of missing template vars
    error_message = Column(String, nullable=True)
    sent_at = Column(DateTime, nullable=True)
    credential_id = Column(Integer, ForeignKey("smtp_credentials.id"), nullable=True)


class Blacklist(Base):
    __tablename__ = "blacklist"
    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String, nullable=False, unique=True, index=True)
    domain = Column(String, nullable=False, index=True)
    reason = Column(String, nullable=True)
