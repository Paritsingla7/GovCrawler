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
    UniqueConstraint, create_engine, event, func, or_, literal, desc, text
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
    depth = Column(Integer, nullable=False, default=0)
    captured_at = Column(DateTime, default=datetime.datetime.utcnow)
    __table_args__ = (
        UniqueConstraint("job_id", "email", name="uq_lead_job_email"),
    )


class VisitedUrl(Base):
    __tablename__ = "visited_urls"
    id = Column(Integer, primary_key=True, autoincrement=True)
    url = Column(String, nullable=False, index=True)
    job_id = Column(Integer, ForeignKey("crawl_jobs.id"), nullable=False)
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
        self._ensure_columns()
        log.info(f"Database ready: {uri}")

    def _ensure_columns(self):
        """Safely add new columns to existing tables without a full migration."""
        from sqlalchemy import inspect as sa_inspect
        inspector = sa_inspect(self.engine)
        tables_to_patch = {
            "campaign_emails": [
                ("is_selected", "BOOLEAN NOT NULL DEFAULT 1"),
                ("missing_fields", "VARCHAR"),
            ],
            "test_campaign_emails": [
                ("is_selected", "BOOLEAN NOT NULL DEFAULT 1"),
                ("missing_fields", "VARCHAR"),
            ],
            "crawl_jobs": [
                ("current_depth", "INTEGER NOT NULL DEFAULT 0"),
                ("active_workers", "INTEGER NOT NULL DEFAULT 0"),
            ],
            "leads": [
                ("entity_kind", "VARCHAR"),
                ("phone", "VARCHAR"),
                ("channel_tag", "VARCHAR"),
                ("confidence_band", "VARCHAR"),
                ("field_provenance", "TEXT"),
            ],
        }
        with self.engine.connect() as conn:
            for table, columns in tables_to_patch.items():
                if table not in inspector.get_table_names():
                    continue
                existing = {c["name"] for c in inspector.get_columns(table)}
                for col_name, col_def in columns:
                    if col_name not in existing:
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}"))
                        log.info(f"Schema: added column {table}.{col_name}")
            conn.commit()

    # ── Domain ────────────────────────────────────────────────────────────────

    def upsert_domain(self, category_code: str, category_title: str,
                      state: str, org_type: str, org_type_title: str,
                      title: str, main_url: str, contact_url: str) -> int:
        with self._Session() as s:
            existing = s.query(Domain).filter_by(main_url=main_url).first()
            if existing:
                existing.category_code = category_code
                existing.category_title = category_title
                existing.state = state
                existing.org_type = org_type
                existing.org_type_title = org_type_title
                existing.title = title
                existing.contact_url = contact_url
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
                "leads_found": CrawlJob.leads_found + new_leads,
                "crawled_domains": CrawlJob.crawled_domains + (1 if domain_done else 0),
            })
            s.commit()

    def update_job_metrics(self, job_id: int, queued_urls: int, visited_urls: int,
                           skipped_urls: int, current_depth: int = 0,
                           active_workers: int = 0):
        with self._Session() as s:
            s.query(CrawlJob).filter_by(id=job_id).update({
                "queued_urls": queued_urls,
                "visited_urls": visited_urls,
                "skipped_urls": skipped_urls,
                "current_depth": current_depth,
                "active_workers": active_workers,
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
            "current_depth": j.current_depth or 0,
            "active_workers": j.active_workers or 0,
            "error_message": j.error_message,
            "category_filter": j.category_filter,
            "title_filter": j.title_filter,
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "started_at": j.started_at.isoformat() if j.started_at else None,
            "finished_at": j.finished_at.isoformat() if j.finished_at else None,
        }

    # ── Leads ─────────────────────────────────────────────────────────────────

    def save_lead(self, job_id: int, domain_id: int | None, email: str | None,
                  person_name: str | None, designation: str | None,
                  department: str | None, source_url: str, source_title: str | None,
                  context_snippet: str, entity_kind: str | None = None,
                  phone: str | None = None, channel_tag: str | None = None,
                  confidence_band: str | None = None,
                  field_provenance: str | None = None, depth: int = 0) -> bool:
        if not email:
            return False
        email = email.lower()
        with self._Session() as s:
            existing = s.query(Lead.id).filter(Lead.email == email).first()
            if existing:
                return False

            domain_state = None
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
                    source_title=source_title,
                    context_snippet=context_snippet,
                    domain_state=domain_state, domain_org_type=domain_org_type,
                    entity_kind=entity_kind, phone=phone, channel_tag=channel_tag,
                    confidence_band=confidence_band, field_provenance=field_provenance,
                    depth=depth,
                ))
                s.commit()
                return True
            except IntegrityError:
                s.rollback()
                return False

    def get_leads(self, job_id: int | None = None, category: str = None,
                  state: str = None, search: str = None, page: int = 1,
                  limit: int = 100, complete_only: bool = False) -> tuple[list[dict], int]:
        with self._Session() as s:
            q = (
                s.query(Lead, Domain.title.label("domain_title"),
                        Domain.category_code)
                .outerjoin(Domain, Lead.domain_id == Domain.id)
            )
            if job_id is not None:
                q = q.filter(Lead.job_id == job_id)
            if category:
                q = q.filter(Domain.category_code == category)
            if state:
                q = q.filter(Domain.state == state)
            if search:
                q = q.filter(
                    or_(Lead.email.ilike(f"%{search}%"),
                        Lead.person_name.ilike(f"%{search}%"),
                        Lead.department.ilike(f"%{search}%"),
                        Lead.designation.ilike(f"%{search}%"))
                )
            if complete_only:
                q = q.filter(
                    Lead.person_name.isnot(None), Lead.person_name != "",
                    Lead.designation.isnot(None), Lead.designation != "",
                    Lead.department.isnot(None), Lead.department != "",
                )

            total_q = s.query(Lead).outerjoin(Domain, Lead.domain_id == Domain.id)
            if job_id is not None:
                total_q = total_q.filter(Lead.job_id == job_id)
            if category:
                total_q = total_q.filter(Domain.category_code == category)
            if state:
                total_q = total_q.filter(Domain.state == state)
            if search:
                total_q = total_q.filter(
                    or_(Lead.email.ilike(f"%{search}%"),
                        Lead.person_name.ilike(f"%{search}%"),
                        Lead.department.ilike(f"%{search}%"),
                        Lead.designation.ilike(f"%{search}%"))
                )
            if complete_only:
                total_q = total_q.filter(
                    Lead.person_name.isnot(None), Lead.person_name != "",
                    Lead.designation.isnot(None), Lead.designation != "",
                    Lead.department.isnot(None), Lead.department != "",
                )
            total = total_q.count()

            offset = (page - 1) * limit
            rows = q.order_by(Lead.captured_at.desc()).offset(offset).limit(limit).all()
            return (
                [{"id": l.id, "email": l.email, "person_name": l.person_name,
                  "designation": l.designation, "department": l.department,
                  "source_url": l.source_url, "source_title": l.source_title,
                  "context_snippet": l.context_snippet,
                  "domain_title": dt, "category_code": cc,
                  "domain_state": l.domain_state, "domain_org_type": l.domain_org_type,
                  "confidence_band": l.confidence_band,
                  "field_provenance": l.field_provenance,
                  "phone": l.phone,
                  "depth": l.depth or 0,
                  "captured_at": l.captured_at.isoformat() if l.captured_at else None}
                 for l, dt, cc in rows],
                total,
            )

    def get_lead_ids(self, job_id: int | None = None, category: str = None,
                     state: str = None, search: str = None,
                     complete_only: bool = False) -> list[int]:
        with self._Session() as s:
            q = s.query(Lead.id).outerjoin(Domain, Lead.domain_id == Domain.id)
            if job_id is not None:
                q = q.filter(Lead.job_id == job_id)
            if category:
                q = q.filter(Domain.category_code == category)
            if state:
                q = q.filter(Domain.state == state)
            if search:
                q = q.filter(
                    or_(Lead.email.ilike(f"%{search}%"),
                        Lead.person_name.ilike(f"%{search}%"),
                        Lead.department.ilike(f"%{search}%"),
                        Lead.designation.ilike(f"%{search}%"))
                )
            if complete_only:
                q = q.filter(
                    Lead.person_name.isnot(None), Lead.person_name != "",
                    Lead.designation.isnot(None), Lead.designation != "",
                    Lead.department.isnot(None), Lead.department != "",
                )
            return [r[0] for r in q.all()]

    def get_all_leads_for_export(self, job_id: int | None = None,
                                 category: str = None, state: str = None,
                                 search: str = None, lead_ids: list[int] = None,
                                 complete_only: bool = False) -> list[dict]:
        with self._Session() as s:
            q = (
                s.query(Lead, Domain.title.label("domain_title"),
                        Domain.category_code, Domain.category_title)
                .outerjoin(Domain, Lead.domain_id == Domain.id)
            )
            if lead_ids:
                q = q.filter(Lead.id.in_(lead_ids))
            else:
                if job_id is not None:
                    q = q.filter(Lead.job_id == job_id)
                if category:
                    q = q.filter(Domain.category_code == category)
                if state:
                    q = q.filter(Domain.state == state)
                if search:
                    q = q.filter(
                        or_(Lead.email.ilike(f"%{search}%"),
                            Lead.person_name.ilike(f"%{search}%"),
                            Lead.department.ilike(f"%{search}%"),
                            Lead.designation.ilike(f"%{search}%"))
                    )
            if complete_only:
                q = q.filter(
                    Lead.person_name.isnot(None), Lead.person_name != "",
                    Lead.designation.isnot(None), Lead.designation != "",
                    Lead.department.isnot(None), Lead.department != "",
                )
            rows = q.order_by(Lead.domain_id, Lead.captured_at).all()
            return [
                {"email": l.email, "person_name": l.person_name or "",
                 "designation": l.designation or "", "department": l.department or "",
                 "domain_title": dt or "", "domain_state": l.domain_state or "",
                 "domain_org_type": l.domain_org_type or "",
                 "category_title": ct or cc or "",
                 "source_url": l.source_url or "",
                 "source_title": l.source_title or "",
                 "context_snippet": l.context_snippet or "",
                 "confidence_band": l.confidence_band or "",
                 "field_provenance": l.field_provenance or "",
                 "phone": l.phone or "",
                 "depth": l.depth or 0,
                 "captured_at": l.captured_at.isoformat() if l.captured_at else ""}
                for l, dt, cc, ct in rows
            ]

    def get_lead_categories(self, job_id: int | None = None) -> list[dict]:
        with self._Session() as s:
            q = (
                s.query(Domain.category_code, Domain.category_title,
                        func.count(Lead.id).label("count"))
                .join(Lead, Lead.domain_id == Domain.id)
            )
            if job_id is not None:
                q = q.filter(Lead.job_id == job_id)
            rows = (
                q.group_by(Domain.category_code, Domain.category_title)
                .order_by(func.count(Lead.id).desc())
                .all()
            )
            return [
                {"code": r.category_code,
                 "title": r.category_title or r.category_code,
                 "count": r.count}
                for r in rows
            ]

    def get_lead_states(self, job_id: int | None = None, category: str = None) -> list[str]:
        with self._Session() as s:
            q = s.query(Domain.state).join(Lead, Lead.domain_id == Domain.id).filter(Domain.state.isnot(None))
            if job_id is not None:
                q = q.filter(Lead.job_id == job_id)
            if category:
                q = q.filter(Domain.category_code == category)
            rows = q.distinct().order_by(Domain.state).all()
            return [r[0] for r in rows if r[0]]

    _LEAD_EDITABLE = frozenset({"person_name", "designation", "department", "domain_state"})

    def update_lead(self, lead_id: int, updates: dict) -> bool:
        safe = {
            k: (v.strip() if isinstance(v, str) and v.strip() else None)
            for k, v in updates.items()
            if k in self._LEAD_EDITABLE
        }
        if not safe:
            return False
        with self._Session() as s:
            updated = s.query(Lead).filter_by(id=lead_id).update(safe)
            s.commit()
            return updated > 0

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
                        status: "CampaignStatus" = None) -> int:
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
                  "created_at": r.created_at.isoformat() if r.created_at and hasattr(r.created_at, 'isoformat') else (r.created_at if isinstance(r.created_at, str) else None)}
                 for r in rows],
                total,
            )

    def update_campaign_status(self, campaign_id: int,
                               new_status: "CampaignStatus") -> bool:
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

    def create_test_campaign(self, name: str, template_id: int, test_credential_id: int | None = None, status: "CampaignStatus" = None) -> int:
        if status is None:
            status = CampaignStatus.RUNNING
        with self._Session() as s:
            c = TestCampaign(name=name, template_id=template_id, test_credential_id=test_credential_id, status=status)
            s.add(c)
            s.commit()
            return c.id

    def create_test_campaign_email(self, test_campaign_id: int, recipient_email: str, subject: str, body: str) -> int:
        with self._Session() as s:
            e = TestCampaignEmail(test_campaign_id=test_campaign_id, recipient_email=recipient_email, subject=subject, body=body)
            s.add(e)
            s.commit()
            return e.id

    def get_test_campaign(self, campaign_id: int) -> dict | None:
        with self._Session() as s:
            c = s.query(TestCampaign).filter_by(id=campaign_id).first()
            if not c: return None
            return {"id": c.id, "name": c.name, "template_id": c.template_id, "test_credential_id": c.test_credential_id, "status": c.status.value, "is_test": True, "created_at": c.created_at.isoformat() if c.created_at else None}

    def update_test_campaign_status(self, campaign_id: int, new_status: "CampaignStatus") -> bool:
        with self._Session() as s:
            updated = s.query(TestCampaign).filter_by(id=campaign_id).update({"status": new_status})
            s.commit()
            return updated > 0

    def get_test_campaign_stats(self, campaign_id: int) -> dict:
        with self._Session() as s:
            rows = s.query(TestCampaignEmail.status, func.count(TestCampaignEmail.id)).filter_by(test_campaign_id=campaign_id).group_by(TestCampaignEmail.status).all()
            stats = {"draft": 0, "queued": 0, "sent": 0, "failed": 0, "total": 0}
            for status_val, count in rows:
                stats[status_val.value.lower()] = count
                stats["total"] += count
            return stats

    def get_test_campaign_emails(self, campaign_id: int, status: str = None, page: int = 1, limit: int = 50) -> tuple[list[dict], int]:
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
            updated = s.query(TestCampaignEmail).filter_by(test_campaign_id=campaign_id, status=EmailStatus.DRAFT).update({"status": EmailStatus.QUEUED})
            s.commit()
            return updated

    def get_next_queued_test_email(self, campaign_id: int) -> dict | None:
        with self._Session() as s:
            e = s.query(TestCampaignEmail).filter_by(test_campaign_id=campaign_id, status=EmailStatus.QUEUED).order_by(TestCampaignEmail.id).first()
            if not e: return None
            return {"id": e.id, "campaign_id": e.test_campaign_id, "recipient_email": e.recipient_email, "subject": e.subject, "body": e.body}

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
            s.query(TestCampaignEmail).filter_by(id=email_id).update({"status": EmailStatus.SENT, "sent_at": datetime.datetime.utcnow()})
            s.commit()

    def mark_test_email_failed(self, email_id: int, error_message: str) -> None:
        with self._Session() as s:
            s.query(TestCampaignEmail).filter_by(id=email_id).update({"status": EmailStatus.FAILED, "error_message": error_message})
            s.commit()

    def cancel_remaining_queued_test(self, campaign_id: int) -> int:
        with self._Session() as s:
            updated = s.query(TestCampaignEmail).filter_by(test_campaign_id=campaign_id, status=EmailStatus.QUEUED).update({"status": EmailStatus.FAILED, "error_message": "Campaign cancelled"})
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

# ---- Outreach & Campaign Management Models ----
import enum
from sqlalchemy import Boolean, Enum as SqlEnum

class CampaignStatus(enum.Enum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"

class EmailStatus(enum.Enum):
    DRAFT = "DRAFT"
    QUEUED = "QUEUED"
    SENT = "SENT"
    FAILED = "FAILED"

class Campaign(Base):
    __tablename__ = "campaigns"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    template_id = Column(Integer, ForeignKey("email_templates.id"), nullable=True)
    status = Column(SqlEnum(CampaignStatus), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

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
    password = Column(String, nullable=False)  # plain‑text password
    is_active = Column(Boolean, default=True, nullable=False)
    cooldown_until = Column(DateTime, nullable=True)

class CampaignEmail(Base):
    __tablename__ = "campaign_emails"
    id = Column(Integer, primary_key=True, autoincrement=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id"), nullable=False)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=False)
    recipient_email = Column(String, nullable=False)
    subject = Column(String, nullable=False)
    body = Column(Text, nullable=False)
    status = Column(SqlEnum(EmailStatus), nullable=False, default=EmailStatus.DRAFT)
    is_selected = Column(Boolean, nullable=False, default=True)
    missing_fields = Column(String, nullable=True)  # comma-separated list of missing template vars
    error_message = Column(String, nullable=True)
    sent_at = Column(DateTime, nullable=True)

class Blacklist(Base):
    __tablename__ = "blacklist"
    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String, nullable=False, unique=True, index=True)
    domain = Column(String, nullable=False, index=True)
    reason = Column(String, nullable=True)

class TestCampaign(Base):
    __tablename__ = "test_campaigns"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    template_id = Column(Integer, ForeignKey("email_templates.id"), nullable=True)
    test_credential_id = Column(Integer, ForeignKey("smtp_credentials.id"), nullable=True)
    status = Column(SqlEnum(CampaignStatus), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class TestCampaignEmail(Base):
    __tablename__ = "test_campaign_emails"
    id = Column(Integer, primary_key=True, autoincrement=True)
    test_campaign_id = Column(Integer, ForeignKey("test_campaigns.id"), nullable=False)
    recipient_email = Column(String, nullable=False)
    subject = Column(String, nullable=False)
    body = Column(Text, nullable=False)
    status = Column(SqlEnum(EmailStatus), nullable=False, default=EmailStatus.DRAFT)
    is_selected = Column(Boolean, nullable=False, default=True)
    missing_fields = Column(String, nullable=True)
    error_message = Column(String, nullable=True)
    sent_at = Column(DateTime, nullable=True)
