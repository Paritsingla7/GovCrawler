import logging

from sqlalchemy import create_engine, inspect as sa_inspect, text
from sqlalchemy.orm import sessionmaker

from .base import Base
from .mixins.domain_mixin import DomainMixin
from .mixins.job_mixin import JobMixin
from .mixins.lead_mixin import LeadMixin
from .mixins.outreach_mixin import OutreachMixin
from .mixins.visited_mixin import VisitedUrlMixin

log = logging.getLogger(__name__)


class Database(DomainMixin, JobMixin, LeadMixin, VisitedUrlMixin, OutreachMixin):
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
            conn.commit()

    def close(self):
        self.engine.dispose()
        log.info("Database connection closed.")
