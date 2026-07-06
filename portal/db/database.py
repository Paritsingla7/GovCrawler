import logging
from sqlalchemy import create_engine, inspect as sa_inspect, text
from sqlalchemy.orm import sessionmaker

from .base import Base
from .migrations import run_migrations
from .mixins.crawl_snapshot_mixin import CrawlSnapshotMixin
from .mixins.domain_mixin import DomainMixin
from .mixins.job_mixin import JobMixin
from .mixins.lead_mixin import LeadMixin
from .mixins.outreach_mixin import OutreachMixin
from .mixins.visited_mixin import VisitedUrlMixin
from ..services.lead_scoring import DEFAULT_WEIGHTS, compute_lead_score

log = logging.getLogger(__name__)


class Database(DomainMixin, JobMixin, CrawlSnapshotMixin, LeadMixin, VisitedUrlMixin, OutreachMixin):
    def __init__(self, config: dict):
        uri = config["database"]["uri"]
        self.engine = create_engine(uri, echo=False, pool_pre_ping=True)
        Base.metadata.create_all(self.engine)
        self._Session = sessionmaker(bind=self.engine)
        self._recrawl_days = config.get("crawler", {}).get("recrawl_days", 30)
        self._lead_score_weights = config.get("lead_score", {}).get("weights", DEFAULT_WEIGHTS)
        self._ensure_columns()
        run_migrations(uri)
        log.info(f"Database ready: {uri}")

    def _ensure_columns(self):
        """Safely add new columns to existing tables without a full migration."""
        inspector = sa_inspect(self.engine)
        tables_to_patch = {
            "campaign_emails": [
                ("is_selected", "BOOLEAN NOT NULL DEFAULT 1"),
                ("missing_fields", "VARCHAR"),
                ("credential_id", "INTEGER"),
            ],
            "test_campaign_emails": [
                ("is_selected", "BOOLEAN NOT NULL DEFAULT 1"),
                ("missing_fields", "VARCHAR"),
                ("credential_id", "INTEGER"),
            ],
            "smtp_credentials": [
                ("daily_send_limit", "INTEGER"),
            ],
            "campaigns": [
                ("pause_reason", "VARCHAR"),
            ],
            "test_campaigns": [
                ("pause_reason", "VARCHAR"),
            ],
            "crawl_jobs": [
                ("current_depth", "INTEGER NOT NULL DEFAULT 0"),
                ("active_workers", "INTEGER NOT NULL DEFAULT 0"),
                ("source_type", "VARCHAR NOT NULL DEFAULT 'domains'"),
            ],
            "leads": [
                ("entity_kind", "VARCHAR"),
                ("phone", "VARCHAR"),
                ("channel_tag", "VARCHAR"),
                ("confidence_band", "VARCHAR"),
                ("field_provenance", "TEXT"),
                ("depth", "INTEGER NOT NULL DEFAULT 0"),
                ("lead_score", "INTEGER NOT NULL DEFAULT 0"),
                ("snapshot_id", "INTEGER"),
            ],
            "domains": [
                ("external_id", "VARCHAR"),
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
            if "leads" in inspector.get_table_names():
                self._recompute_lead_scores(conn)
                if "crawl_snapshots" in inspector.get_table_names():
                    self._backfill_snapshots(conn)
            conn.commit()

    def _recompute_lead_scores(self, conn):
        """Recompute lead_score for every row from current weights.

        Runs on every startup (not just when the column is new) so a weight
        change in config takes effect on existing leads without a migration.
        Goes through compute_lead_score() itself (not a parallel SQL
        expression) since the band/manual/phone-slice rules aren't cleanly
        expressible in SQL without duplicating — and risking drift from —
        the one scoring implementation.
        """
        rows = conn.execute(text(
            "SELECT id, email, phone, person_name, designation, "
            "confidence_band, channel_tag FROM leads"
        )).fetchall()
        for row in rows:
            m = row._mapping
            score = compute_lead_score(
                {"email": m["email"], "phone": m["phone"],
                 "person_name": m["person_name"], "designation": m["designation"]},
                confidence_band=m["confidence_band"], channel_tag=m["channel_tag"],
                weights=self._lead_score_weights,
            )
            conn.execute(text("UPDATE leads SET lead_score = :score WHERE id = :id"),
                         {"score": score, "id": m["id"]})
        log.info(f"Schema: recomputed lead_score for {len(rows)} leads")

    def _backfill_snapshots(self, conn):
        """One-time: freeze each domain-backed lead's current catalog metadata
        into a per-(job, domain) crawl_snapshots row, then point the lead at it.

        Runs on every startup but is a cheap no-op once done (the snapshot_id
        IS NULL guards on both statements naturally stop matching rows after
        the first successful pass). The extra NOT EXISTS guard on the INSERT
        protects against a prior partial run having already created some
        snapshot rows before failing on the UPDATE (a real failure mode seen
        here — see 0011_add_crawl_snapshots.py). Leads with no domain_id
        (manual/custom-URL) are left with snapshot_id = NULL, same as before.
        """
        conn.execute(text(
            "INSERT INTO crawl_snapshots "
            "(job_id, source_domain_id, external_id, category_code, category_title, "
            " state, org_type, org_type_title, title, main_url, contact_url, created_at) "
            "SELECT DISTINCT l.job_id, l.domain_id, d.external_id, d.category_code, d.category_title, "
            " d.state, d.org_type, d.org_type_title, d.title, d.main_url, d.contact_url, CURRENT_TIMESTAMP "
            "FROM leads l JOIN domains d ON l.domain_id = d.id "
            "WHERE l.snapshot_id IS NULL AND l.domain_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM crawl_snapshots s "
            "                WHERE s.job_id = l.job_id AND s.source_domain_id = l.domain_id)"
        ))
        result = conn.execute(text(
            "UPDATE leads SET snapshot_id = ("
            " SELECT s.id FROM crawl_snapshots s "
            " WHERE s.job_id = leads.job_id AND s.source_domain_id = leads.domain_id) "
            "WHERE snapshot_id IS NULL AND domain_id IS NOT NULL"
        ))
        if result.rowcount:
            log.info(f"Schema: backfilled snapshot_id for {result.rowcount} leads")

    def close(self):
        self.engine.dispose()
        log.info("Database connection closed.")
