import logging
from pathlib import Path
from sqlalchemy import create_engine, inspect as sa_inspect, text
from sqlalchemy.orm import sessionmaker

from portal.paths import LIVE_CONFIG_PATH
from shared.scoring import DEFAULT_WEIGHTS, compute_lead_score
from .base import Base
from .migrations import run_migrations
from .mixins.app_settings_mixin import AppSettingsMixin
from .mixins.auth_mixin import AuthMixin
from .mixins.crawl_snapshot_mixin import CrawlSnapshotMixin
from .mixins.domain_mixin import DomainMixin
from .mixins.job_mixin import JobMixin
from .mixins.lead_mixin import LeadMixin
from .mixins.outreach_mixin import OutreachMixin
from ..security.crypto import ensure_credential_enc_key

log = logging.getLogger(__name__)

# plan.md §3.2 — the crawler-policy subset of config.yaml's `crawler` section
# that lives in app_settings (everything else in `crawler` is machine-local:
# workers, timeouts, js_settle_time).
_CRAWLER_POLICY_KEYS = (
    "target_suffixes",
    "priority_keywords",
    "skip_extensions",
    "pagination",
    "js_indicators",
    "max_links_per_page",
    "max_depth",
    "recrawl_days",
    "request_delay",
    "user_agent",
    "max_custom_urls",
)


class Database(DomainMixin, JobMixin, CrawlSnapshotMixin, LeadMixin, OutreachMixin, AuthMixin, AppSettingsMixin):
    def __init__(self, config: dict, config_path: Path = LIVE_CONFIG_PATH):
        """`config_path` is where a missing `credential_enc_key` gets persisted
        (ensure_credential_enc_key) — defaults to the real live config for every
        actual call site (portal.main, dispatch_service, the migration script),
        but callers building a `Database` from an ad-hoc/test config dict MUST
        override it, or a generated key silently overwrites the real file."""
        uri = config["database"]["uri"]
        self.engine = create_engine(uri, echo=False, pool_pre_ping=True)
        Base.metadata.create_all(self.engine)
        self._Session = sessionmaker(bind=self.engine)
        self._cred_enc_key = ensure_credential_enc_key(config, config_path)
        self._oauth_config = config.get("oauth", {})
        # Must precede _ensure_columns(): its lead_score backfill path reads
        # self._lead_score_weights, which is sourced from app_settings.
        self._seed_app_settings(config)
        self._ensure_columns()
        run_migrations(uri)
        self.seed_rbac()
        log.info(f"Database ready: {uri}")

    @property
    def _lead_score_weights(self) -> dict:
        return self.get_crawl_policy().get("lead_score", {}).get("weights", DEFAULT_WEIGHTS)

    def _seed_app_settings(self, config: dict) -> None:
        """First-run only: back-fill the `crawl_policy` app_settings row from
        the live config.yaml's current values (not the shipped defaults), so
        an upgrade preserves whatever policy this deployment already had.
        No-op once the row exists — plan.md §19.1 Phase 8's expand step."""
        if self.get_app_setting("crawl_policy"):
            return
        crawler_cfg = config.get("crawler", {})
        policy = {
            "extraction": config.get("extraction", {}),
            "lead_score": {"weights": config.get("lead_score", {}).get("weights", DEFAULT_WEIGHTS)},
            "crawler": {k: crawler_cfg[k] for k in _CRAWLER_POLICY_KEYS if k in crawler_cfg},
        }
        self.set_app_setting("crawl_policy", policy, updated_by=None)
        log.info("Schema: seeded app_settings.crawl_policy from config.yaml")

    def _ensure_columns(self):
        """Safely add new columns to existing tables without a full migration."""
        inspector = sa_inspect(self.engine)
        tables_to_patch = {
            "campaign_emails": [
                ("is_selected", "BOOLEAN NOT NULL DEFAULT TRUE"),
                ("missing_fields", "VARCHAR"),
                ("credential_id", "INTEGER"),
            ],
            "smtp_credentials": [
                ("daily_send_limit", "INTEGER"),
            ],
            "campaigns": [
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
        lead_score_newly_added = False
        with self.engine.connect() as conn:
            for table, columns in tables_to_patch.items():
                if table not in inspector.get_table_names():
                    continue
                existing = {c["name"] for c in inspector.get_columns(table)}
                for col_name, col_def in columns:
                    if col_name not in existing:
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}"))
                        log.info(f"Schema: added column {table}.{col_name}")
                        if table == "leads" and col_name == "lead_score":
                            lead_score_newly_added = True
            if "leads" in inspector.get_table_names():
                if lead_score_newly_added:
                    # One-time backfill for a column that just landed with its
                    # server_default (0) on every existing row — the Alembic
                    # 0010 migration relies on this same path (see its
                    # docstring). Not run unconditionally every boot anymore
                    # (plan.md §19.1 Phase 8) — only right after the column
                    # itself is added, same as _backfill_snapshots below.
                    self._recompute_lead_scores(conn)
                leads_columns = {c["name"] for c in inspector.get_columns("leads")}
                if "crawl_snapshots" in inspector.get_table_names() and "domain_id" in leads_columns:
                    self._backfill_snapshots(conn)
            conn.commit()

    def recompute_lead_scores(self) -> None:
        """Public entry point for an on-demand recompute — called from the
        Settings API (background task) when a POST actually changes
        lead_score.weights. Opens its own connection since callers (e.g.
        cloud.api.config) have no engine-connection context of their own."""
        with self.engine.connect() as conn:
            self._recompute_lead_scores(conn)
            conn.commit()

    def _recompute_lead_scores(self, conn):
        """Recompute lead_score for every row from the current weights.

        Only called when weights actually change (via the public
        recompute_lead_scores(), triggered from cloud.api.config's
        POST /api/config) — not on every startup (plan.md §19.1 Phase 8).
        Goes through compute_lead_score() itself (not a parallel SQL
        expression) since the band/manual/phone-slice rules aren't cleanly
        expressible in SQL without duplicating — and risking drift from —
        the one scoring implementation.
        """
        rows = conn.execute(
            text("SELECT id, email, phone, person_name, designation, confidence_band, channel_tag FROM leads")
        ).fetchall()
        for row in rows:
            m = row._mapping
            score = compute_lead_score(
                {
                    "email": m["email"],
                    "phone": m["phone"],
                    "person_name": m["person_name"],
                    "designation": m["designation"],
                },
                confidence_band=m["confidence_band"],
                channel_tag=m["channel_tag"],
                weights=self._lead_score_weights,
            )
            conn.execute(text("UPDATE leads SET lead_score = :score WHERE id = :id"), {"score": score, "id": m["id"]})
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
        conn.execute(
            text(
                "INSERT INTO crawl_snapshots "
                "(job_id, source_domain_id, external_id, category_code, category_title, "
                " state, org_type, org_type_title, title, main_url, contact_url, created_at) "
                "SELECT DISTINCT l.job_id, l.domain_id, d.external_id, d.category_code, d.category_title, "
                " d.state, d.org_type, d.org_type_title, d.title, d.main_url, d.contact_url, CURRENT_TIMESTAMP "
                "FROM leads l JOIN domains d ON l.domain_id = d.id "
                "WHERE l.snapshot_id IS NULL AND l.domain_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM crawl_snapshots s "
                "                WHERE s.job_id = l.job_id AND s.source_domain_id = l.domain_id)"
            )
        )
        result = conn.execute(
            text(
                "UPDATE leads SET snapshot_id = ("
                " SELECT s.id FROM crawl_snapshots s "
                " WHERE s.job_id = leads.job_id AND s.source_domain_id = leads.domain_id) "
                "WHERE snapshot_id IS NULL AND domain_id IS NOT NULL"
            )
        )
        if result.rowcount:
            log.info(f"Schema: backfilled snapshot_id for {result.rowcount} leads")

    def close(self):
        self.engine.dispose()
        log.info("Database connection closed.")
