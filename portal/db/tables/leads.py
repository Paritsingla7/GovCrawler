import datetime
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint

from ..base import Base


class Lead(Base):
    __tablename__ = "leads"
    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(Integer, ForeignKey("crawl_jobs.id"), nullable=False, index=True)
    snapshot_id = Column(Integer, ForeignKey("crawl_snapshots.id"))
    email = Column(String, nullable=False, index=True)
    person_name = Column(String)
    designation = Column(String)
    department = Column(String)
    source_url = Column(String)
    source_title = Column(String)
    context_snippet = Column(Text)
    manual_state = Column(String)  # editable only for manual/CSV leads (no snapshot); crawled leads read from snapshot
    entity_kind = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    channel_tag = Column(String, nullable=True)
    confidence_band = Column(String, nullable=True)
    field_provenance = Column(Text, nullable=True)
    lead_score = Column(Integer, nullable=False, default=0)
    depth = Column(Integer, nullable=False, default=0)
    captured_at = Column(DateTime, default=datetime.datetime.utcnow)
    __table_args__ = (
        UniqueConstraint("job_id", "email", name="uq_lead_job_email"),
    )


class LeadOccurrence(Base):
    """Every capture of a shared lead (many-to-many lead<->job), so per-job
    attribution + a truthful per-job leads_found survive the global-email
    dedup in save_lead()/bulk_upsert_manual_leads()."""
    __tablename__ = "lead_occurrences"
    id = Column(Integer, primary_key=True, autoincrement=True)
    lead_id = Column(Integer, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True)
    job_id = Column(Integer, ForeignKey("crawl_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    captured_by = Column(Integer, ForeignKey("users.id"))
    source_url = Column(String)
    captured_at = Column(DateTime, default=datetime.datetime.utcnow)
    __table_args__ = (
        UniqueConstraint("lead_id", "job_id", name="uq_lead_occurrence_lead_job"),
    )
