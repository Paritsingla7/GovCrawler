import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint

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
    imported_at = Column(DateTime, default=datetime.datetime.utcnow)


class CrawlJob(Base):
    __tablename__ = "crawl_jobs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    category_filter = Column(String)
    title_filter = Column(String)
    domain_ids = Column(Text)  # JSON list[int]
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
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    started_at = Column(DateTime)
    finished_at = Column(DateTime)


class VisitedUrl(Base):
    __tablename__ = "visited_urls"
    id = Column(Integer, primary_key=True, autoincrement=True)
    url = Column(String, nullable=False, index=True)
    job_id = Column(Integer, ForeignKey("crawl_jobs.id"), nullable=False)
    visited_at = Column(DateTime, default=datetime.datetime.utcnow)
    __table_args__ = (
        UniqueConstraint("url", "job_id", name="uq_visited_url_job"),
    )
