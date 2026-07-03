import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint

from ..base import Base


class Lead(Base):
    __tablename__ = "leads"
    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(Integer, ForeignKey("crawl_jobs.id"), nullable=False, index=True)
    domain_id = Column(Integer, ForeignKey("domains.id"))
    email = Column(String, nullable=False, index=True)
    person_name = Column(String)
    designation = Column(String)
    department = Column(String)
    source_url = Column(String)
    source_title = Column(String)
    context_snippet = Column(Text)
    domain_state = Column(String)
    domain_org_type = Column(String)
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
