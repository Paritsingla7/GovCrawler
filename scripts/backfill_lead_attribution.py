"""
WI-6 (PLAN_attribution_and_parser.md Plan 1): repair snapshot_id on every
existing crawled lead by re-running the WI-2/WI-4 domain-resolution logic
save_lead now applies at save time — fixes leads captured before this fix
shipped, whose snapshot_id may still be wrongly inherited from the crawl's
seed instead of the domain their source_url actually belongs to.

Idempotent — safe to re-run; only touches rows whose resolved attribution
differs from their current snapshot_id. Manual (CSV-imported) leads are
untouched (they were never snapshot-attributed in the first place).

RUNBOOK: run against a COPY of the database first and spot-check a sample of
the reported changes before running against the real one.

Usage:
    python scripts/backfill_lead_attribution.py <database-url>
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cloud.db import Database  # noqa: E402
from cloud.db.domain_resolution import resolve_domain_for_url  # noqa: E402
from cloud.db.tables.crawl import CrawlSnapshot  # noqa: E402
from cloud.db.tables.leads import Lead  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


def main():
    if len(sys.argv) != 2:
        print("Usage: python scripts/backfill_lead_attribution.py <database-url>")
        sys.exit(1)

    db = Database({"database": {"uri": sys.argv[1]}, "auth": {}})
    netloc_map = db._get_netloc_domain_map()
    target_suffixes = db.get_crawl_policy().get("crawler", {}).get("target_suffixes", [".gov.in", ".nic.in"])

    changed = 0
    examined = 0
    with db._Session() as s:
        leads = s.query(Lead).filter(Lead.channel_tag.isnot(None), Lead.channel_tag != "manual").all()
        for lead in leads:
            examined += 1
            resolved = resolve_domain_for_url(lead.source_url, netloc_map, target_suffixes)

            if resolved is None:
                if lead.snapshot_id is not None:
                    lead.snapshot_id = None
                    changed += 1
                continue

            current_source_domain_id = None
            if lead.snapshot_id is not None:
                snap = s.query(CrawlSnapshot.source_domain_id).filter_by(id=lead.snapshot_id).first()
                current_source_domain_id = snap.source_domain_id if snap else None

            if resolved["id"] != current_source_domain_id:
                lead.snapshot_id = db.create_crawl_snapshot(lead.job_id, resolved, is_seed=False)
                changed += 1

        s.commit()

    log.info(f"Examined {examined} crawled lead(s), repaired snapshot_id on {changed}.")
    db.close()


if __name__ == "__main__":
    main()
