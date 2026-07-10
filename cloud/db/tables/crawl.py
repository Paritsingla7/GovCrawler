import datetime
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint

from ..base import Base


class Domain(Base):
    __tablename__ = "domains"
    id = Column(Integer, primary_key=True, autoincrement=True)
    category_code = Column(String, nullable=False, index=True)
    category_title = Column(String)
    state = Column(String, index=True)
    org_type = Column(String, index=True)
    org_type_title = Column(String)
    title = Column(String, index=True)
    main_url = Column(String)
    contact_url = Column(String)
    external_id = Column(String, index=True)
    imported_at = Column(DateTime, default=datetime.datetime.utcnow)


class CrawlSnapshot(Base):
    """Per-crawl frozen copy of a seed domain's metadata.

    Leads (and a job's seed view) point here instead of at the mutable `domains`
    catalog, so refreshing/rebuilding `domains` never alters lead-visible data —
    the metadata is frozen exactly as it was when the crawl ran.
    """

    __tablename__ = "crawl_snapshots"
    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(Integer, ForeignKey("crawl_jobs.id"), nullable=False, index=True)
    source_domain_id = Column(Integer)  # catalog domains.id at crawl time (soft link)
    external_id = Column(String)
    category_code = Column(String)
    category_title = Column(String)
    state = Column(String)
    org_type = Column(String)
    org_type_title = Column(String)
    title = Column(String)
    main_url = Column(String)
    contact_url = Column(String)
    # True for a snapshot created from a job's user-selected seed domain;
    # False for one minted later when a lead's source_url resolved to a
    # different catalog domain the crawl merely discovered (see
    # Database.save_lead's attribution path). GET /api/jobs/{id}/seeds
    # (get_crawl_snapshots) filters on this so discovered domains don't leak
    # into the "Job Seeds" UI.
    is_seed = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    __table_args__ = (UniqueConstraint("job_id", "source_domain_id", name="uq_snapshot_job_domain"),)


class CrawlJob(Base):
    __tablename__ = "crawl_jobs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    category_filter = Column(String)
    title_filter = Column(String)
    domain_ids = Column(Text)  # JSON list[int]
    source_type = Column(String, nullable=False, default="domains")  # "domains" | "custom_urls"
    status = Column(String, default="pending")
    total_domains = Column(Integer, default=0)
    crawled_domains = Column(Integer, default=0)
    seed_domains = Column(Integer, default=0)
    queued_urls = Column(Integer, default=0)
    visited_urls = Column(Integer, default=0)
    skipped_urls = Column(Integer, default=0)
    leads_found = Column(Integer, default=0)
    current_depth = Column(Integer, default=0)
    active_workers = Column(Integer, default=0)
    error_message = Column(String)
    owner_id = Column(Integer, ForeignKey("users.id"))
    cancel_requested = Column(Boolean, nullable=False, default=False)
    agent_hostname = Column(String)
    last_heartbeat_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    started_at = Column(DateTime)
    finished_at = Column(DateTime)


class CrawlJobDomain(Base):
    """Junction replacing the JSON crawl_jobs.domain_ids array as the read path
    going forward (domain_ids itself stays for one more phase — see plan.md)."""

    __tablename__ = "crawl_job_domains"
    job_id = Column(Integer, ForeignKey("crawl_jobs.id", ondelete="CASCADE"), primary_key=True)
    domain_id = Column(Integer, ForeignKey("domains.id", ondelete="CASCADE"), primary_key=True)


class JobCustomUrl(Base):
    __tablename__ = "job_custom_urls"
    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(Integer, ForeignKey("crawl_jobs.id"), nullable=False, index=True)
    url = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    __table_args__ = (UniqueConstraint("job_id", "url", name="uq_job_custom_url"),)
