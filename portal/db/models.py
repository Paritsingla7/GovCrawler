"""
SQLAlchemy ORM models and Database access wrapper.

Backends (set database.uri in portal/config.yaml):
  SQLite  (dev):    sqlite:///portal/data/govcrawler.db
  PostgreSQL (server): postgresql://user:pass@host:5432/govcrawler
"""

import datetime
import json
import logging
import sqlite3

from sqlalchemy import (
    Column, DateTime, ForeignKey, Integer, String, Text,
    UniqueConstraint, create_engine, event, func, or_,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import declarative_base, sessionmaker

log = logging.getLogger(__name__)
Base = declarative_base()


# ── WAL mode for SQLite (no-op for PostgreSQL) ────────────────────────────────

@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_conn, _):
    if isinstance(dbapi_conn, sqlite3.Connection):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA cache_size=10000")
        cur.close()


# ── ORM models ────────────────────────────────────────────────────────────────

class Domain(Base):
    __tablename__ = "domains"
    id             = Column(Integer, primary_key=True, autoincrement=True)
    category_code  = Column(String, nullable=False, index=True)
    category_title = Column(String)
    state          = Column(String, index=True)
    org_type       = Column(String, index=True)
    org_type_title = Column(String)
    title          = Column(String, index=True)
    main_url       = Column(String)
    contact_url    = Column(String)
    imported_at    = Column(DateTime, default=datetime.datetime.utcnow)


class CrawlJob(Base):
    __tablename__ = "crawl_jobs"
    id              = Column(Integer, primary_key=True, autoincrement=True)
    category_filter = Column(String)
    title_filter    = Column(String)
    domain_ids      = Column(Text)  # JSON list[int]
    status          = Column(String, default="pending")
    total_domains   = Column(Integer, default=0)
    crawled_domains = Column(Integer, default=0)
    seed_domains    = Column(Integer, default=0)
    queued_urls     = Column(Integer, default=0)
    visited_urls    = Column(Integer, default=0)
    skipped_urls    = Column(Integer, default=0)
    leads_found     = Column(Integer, default=0)
    error_message   = Column(String)
    created_at      = Column(DateTime, default=datetime.datetime.utcnow)
    started_at      = Column(DateTime)
    finished_at     = Column(DateTime)


class Lead(Base):
    __tablename__ = "leads"
    id              = Column(Integer, primary_key=True, autoincrement=True)
    job_id          = Column(Integer, ForeignKey("crawl_jobs.id"), nullable=False, index=True)
    domain_id       = Column(Integer, ForeignKey("domains.id"))
    email           = Column(String, nullable=False, index=True)
    person_name     = Column(String)
    designation     = Column(String)
    department      = Column(String)
    source_url      = Column(String)
    context_snippet = Column(Text)
    domain_state    = Column(String)
    domain_org_type = Column(String)
    captured_at     = Column(DateTime, default=datetime.datetime.utcnow)
    __table_args__ = (
        UniqueConstraint("job_id", "email", name="uq_lead_job_email"),
    )


class VisitedUrl(Base):
    __tablename__ = "visited_urls"
    id         = Column(Integer, primary_key=True, autoincrement=True)
    url        = Column(String, nullable=False, index=True)
    job_id     = Column(Integer, ForeignKey("crawl_jobs.id"), nullable=False)
    visited_at = Column(DateTime, default=datetime.datetime.utcnow)
    __table_args__ = (
        UniqueConstraint("url", "job_id", name="uq_visited_url_job"),
    )


# ── Database access wrapper ───────────────────────────────────────────────────

class Database:
    def __init__(self, config: dict):
        uri = config["database"]["uri"]
        self.engine = create_engine(uri, echo=False, pool_pre_ping=True)
        Base.metadata.create_all(self.engine)
        self._Session = sessionmaker(bind=self.engine)
        self._recrawl_days = config.get("crawler", {}).get("recrawl_days", 30)
        log.info(f"Database ready: {uri}")

    # ── Domain ────────────────────────────────────────────────────────────────

    def upsert_domain(self, category_code: str, category_title: str,
                      state: str, org_type: str, org_type_title: str,
                      title: str, main_url: str, contact_url: str) -> int:
        with self._Session() as s:
            existing = s.query(Domain).filter_by(main_url=main_url).first()
            if existing:
                existing.category_code  = category_code
                existing.category_title = category_title
                existing.state          = state
                existing.org_type       = org_type
                existing.org_type_title = org_type_title
                existing.title          = title
                existing.contact_url    = contact_url
                s.commit()
                return existing.id
            d = Domain(
                category_code=category_code, category_title=category_title,
                state=state, org_type=org_type, org_type_title=org_type_title,
                title=title, main_url=main_url, contact_url=contact_url,
            )
            s.add(d)
            s.commit()
            return d.id

    def clear_domains(self):
        with self._Session() as s:
            s.query(Domain).delete()
            s.commit()

    def count_domains(self) -> int:
        with self._Session() as s:
            return s.query(Domain).count()

    def get_categories(self) -> list[dict]:
        with self._Session() as s:
            rows = (
                s.query(Domain.category_code, Domain.category_title,
                        func.count(Domain.id).label("count"))
                .group_by(Domain.category_code, Domain.category_title)
                .order_by(func.count(Domain.id).desc())
                .all()
            )
            return [
                {"code": r.category_code,
                 "title": r.category_title or r.category_code,
                 "count": r.count}
                for r in rows
            ]

    def get_states(self, category: str = None) -> list[str]:
        with self._Session() as s:
            q = s.query(Domain.state).filter(Domain.state.isnot(None))
            if category:
                q = q.filter(Domain.category_code == category)
            rows = q.distinct().order_by(Domain.state).all()
            return [r[0] for r in rows if r[0]]

    def get_org_types(self, category: str = None, state: str = None) -> list[dict]:
        with self._Session() as s:
            q = (
                s.query(Domain.org_type, Domain.org_type_title,
                        func.count(Domain.id).label("count"))
                .filter(Domain.org_type.isnot(None))
            )
            if category:
                q = q.filter(Domain.category_code == category)
            if state:
                q = q.filter(Domain.state == state)
            rows = (
                q.group_by(Domain.org_type, Domain.org_type_title)
                .order_by(func.count(Domain.id).desc())
                .all()
            )
            return [
                {"code": r.org_type,
                 "title": r.org_type_title or r.org_type,
                 "count": r.count}
                for r in rows
            ]

    def get_domains(self, category: str = None, state: str = None,
                    org_type: str = None, search: str = None,
                    page: int = 1, limit: int = 50) -> tuple[list[dict], int]:
        with self._Session() as s:
            q = s.query(Domain)
            if category:
                q = q.filter(Domain.category_code == category)
            if state:
                q = q.filter(Domain.state == state)
            if org_type:
                q = q.filter(Domain.org_type == org_type)
            if search:
                # Search title OR the domain URL so it works even when title is empty
                q = q.filter(
                    or_(Domain.title.ilike(f"%{search}%"),
                        Domain.main_url.ilike(f"%{search}%"))
                )
            total = q.count()
            offset = (page - 1) * limit
            rows = q.order_by(Domain.state, Domain.main_url).offset(offset).limit(limit).all()
            return (
                [{"id": d.id, "category_code": d.category_code,
                  "category_title": d.category_title, "state": d.state,
                  "org_type": d.org_type, "org_type_title": d.org_type_title,
                  "title": d.title, "main_url": d.main_url,
                  "contact_url": d.contact_url}
                 for d in rows],
                total,
            )

    def get_domain_ids(self, category: str = None, state: str = None,
                       org_type: str = None, search: str = None) -> list[int]:
        """Return all matching domain IDs — used by select-all in the UI."""
        with self._Session() as s:
            q = s.query(Domain.id)
            if category:
                q = q.filter(Domain.category_code == category)
            if state:
                q = q.filter(Domain.state == state)
            if org_type:
                q = q.filter(Domain.org_type == org_type)
            if search:
                q = q.filter(
                    or_(Domain.title.ilike(f"%{search}%"),
                        Domain.main_url.ilike(f"%{search}%"))
                )
            return [r[0] for r in q.all()]

    def get_domains_by_ids(self, ids: list[int]) -> list[dict]:
        with self._Session() as s:
            rows = s.query(Domain).filter(Domain.id.in_(ids)).all()
            return [
                {"id": d.id, "title": d.title, "main_url": d.main_url,
                 "contact_url": d.contact_url, "category_code": d.category_code,
                 "state": d.state, "org_type": d.org_type}
                for d in rows
            ]

    # ── CrawlJob ──────────────────────────────────────────────────────────────

    def create_job(self, domain_ids: list[int], category_filter: str = None,
                   title_filter: str = None) -> int:
        with self._Session() as s:
            job = CrawlJob(
                domain_ids=json.dumps(domain_ids),
                category_filter=category_filter,
                title_filter=title_filter,
                total_domains=len(domain_ids),
                seed_domains=len(domain_ids),
                status="pending",
            )
            s.add(job)
            s.commit()
            return job.id

    def start_job(self, job_id: int):
        with self._Session() as s:
            s.query(CrawlJob).filter_by(id=job_id).update({
                "status": "running",
                "started_at": datetime.datetime.utcnow(),
            })
            s.commit()

    def finish_job(self, job_id: int, status: str = "done", error: str = None):
        with self._Session() as s:
            s.query(CrawlJob).filter_by(id=job_id).update({
                "status": status,
                "finished_at": datetime.datetime.utcnow(),
                "error_message": error,
            })
            s.commit()

    def increment_job_progress(self, job_id: int, new_leads: int = 0,
                               domain_done: bool = False):
        with self._Session() as s:
            s.query(CrawlJob).filter_by(id=job_id).update({
                "leads_found":      CrawlJob.leads_found + new_leads,
                "crawled_domains":  CrawlJob.crawled_domains + (1 if domain_done else 0),
            })
            s.commit()

    def update_job_metrics(self, job_id: int, queued_urls: int, visited_urls: int, skipped_urls: int):
        with self._Session() as s:
            s.query(CrawlJob).filter_by(id=job_id).update({
                "queued_urls": queued_urls,
                "visited_urls": visited_urls,
                "skipped_urls": skipped_urls,
            })
            s.commit()

    def get_job(self, job_id: int) -> dict | None:
        with self._Session() as s:
            j = s.query(CrawlJob).filter_by(id=job_id).first()
            return self._job_dict(j) if j else None

    def list_jobs(self, limit: int = 20) -> list[dict]:
        with self._Session() as s:
            rows = (
                s.query(CrawlJob)
                .order_by(CrawlJob.created_at.desc())
                .limit(limit)
                .all()
            )
            return [self._job_dict(j) for j in rows]

    @staticmethod
    def _job_dict(j: CrawlJob) -> dict:
        return {
            "id": j.id, "status": j.status,
            "total_domains": j.total_domains,
            "crawled_domains": j.crawled_domains,
            "seed_domains": j.seed_domains,
            "queued_urls": j.queued_urls,
            "visited_urls": j.visited_urls,
            "skipped_urls": j.skipped_urls,
            "leads_found": j.leads_found,
            "error_message": j.error_message,
            "category_filter": j.category_filter,
            "title_filter": j.title_filter,
            "created_at":  j.created_at.isoformat()  if j.created_at  else None,
            "started_at":  j.started_at.isoformat()  if j.started_at  else None,
            "finished_at": j.finished_at.isoformat() if j.finished_at else None,
        }

    # ── Leads ─────────────────────────────────────────────────────────────────

    def save_lead(self, job_id: int, domain_id: int | None, email: str | None,
                  person_name: str | None, designation: str | None,
                  department: str | None, source_url: str,
                  context_snippet: str) -> bool:
        if not email:
            return False
        email = email.lower()
        with self._Session() as s:
            existing = s.query(Lead.id).filter(Lead.email == email).first()
            if existing:
                return False

            domain_state    = None
            domain_org_type = None
            if domain_id:
                row = s.query(Domain.state, Domain.org_type).filter_by(id=domain_id).first()
                if row:
                    domain_state, domain_org_type = row.state, row.org_type
            try:
                s.add(Lead(
                    job_id=job_id, domain_id=domain_id, email=email,
                    person_name=person_name, designation=designation,
                    department=department, source_url=source_url,
                    context_snippet=context_snippet,
                    domain_state=domain_state, domain_org_type=domain_org_type,
                ))
                s.commit()
                return True
            except IntegrityError:
                s.rollback()
                return False

    def get_leads(self, job_id: int | None = None, page: int = 1,
                  limit: int = 100) -> tuple[list[dict], int]:
        with self._Session() as s:
            q = (
                s.query(Lead, Domain.title.label("domain_title"),
                        Domain.category_code)
                .outerjoin(Domain, Lead.domain_id == Domain.id)
            )
            if job_id is not None:
                q = q.filter(Lead.job_id == job_id)

            total_q = s.query(Lead)
            if job_id is not None:
                total_q = total_q.filter(Lead.job_id == job_id)
            total = total_q.count()

            offset = (page - 1) * limit
            rows = q.order_by(Lead.captured_at.desc()).offset(offset).limit(limit).all()
            return (
                [{"id": l.id, "email": l.email, "person_name": l.person_name,
                  "designation": l.designation, "department": l.department,
                  "source_url": l.source_url, "context_snippet": l.context_snippet,
                  "domain_title": dt, "category_code": cc,
                  "domain_state": l.domain_state, "domain_org_type": l.domain_org_type,
                  "captured_at": l.captured_at.isoformat() if l.captured_at else None}
                 for l, dt, cc in rows],
                total,
            )

    def get_all_leads_for_export(self, job_id: int | None = None) -> list[dict]:
        with self._Session() as s:
            q = (
                s.query(Lead, Domain.title.label("domain_title"),
                        Domain.category_code, Domain.category_title)
                .outerjoin(Domain, Lead.domain_id == Domain.id)
            )
            if job_id is not None:
                q = q.filter(Lead.job_id == job_id)
            rows = q.order_by(Lead.domain_id, Lead.captured_at).all()
            return [
                {"email": l.email, "person_name": l.person_name or "",
                 "designation": l.designation or "", "department": l.department or "",
                 "domain_title": dt or "", "domain_state": l.domain_state or "",
                 "domain_org_type": l.domain_org_type or "",
                 "category_title": ct or cc or "",
                 "source_url": l.source_url or "",
                 "context_snippet": l.context_snippet or "",
                 "captured_at": l.captured_at.isoformat() if l.captured_at else ""}
                for l, dt, cc, ct in rows
            ]

    # ── Visited URLs ──────────────────────────────────────────────────────────

    def mark_visited(self, url: str, job_id: int):
        with self._Session() as s:
            try:
                s.add(VisitedUrl(url=url, job_id=job_id))
                s.commit()
            except IntegrityError:
                s.rollback()

    def get_visited_urls(self, job_id: int) -> set[str]:
        with self._Session() as s:
            rows = s.query(VisitedUrl.url).filter_by(job_id=job_id).all()
            return {r[0] for r in rows}

    def get_recently_visited_global(self) -> set[str]:
        """URLs visited in any job within the last recrawl_days — skip these in new jobs."""
        threshold = datetime.datetime.utcnow() - datetime.timedelta(days=self._recrawl_days)
        with self._Session() as s:
            rows = (
                s.query(VisitedUrl.url)
                .filter(VisitedUrl.visited_at >= threshold)
                .distinct()
                .all()
            )
            return {r[0] for r in rows}

    def clear_visited_urls(self):
        with self._Session() as s:
            s.query(VisitedUrl).delete()
            s.commit()

    def close(self):
        self.engine.dispose()
        log.info("Database connection closed.")
