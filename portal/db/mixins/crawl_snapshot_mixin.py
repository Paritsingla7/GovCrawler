from sqlalchemy.exc import IntegrityError

from ..tables.crawl import CrawlSnapshot


class CrawlSnapshotMixin:
    def create_crawl_snapshot(self, job_id: int, domain: dict) -> int:
        """Get-or-insert a frozen snapshot of a seed domain for one crawl job.

        Keyed on (job_id, source_domain_id). If a snapshot already exists for
        this job+domain it is returned UNCHANGED — never overwritten — so leads
        captured by an earlier pass of the same job stay frozen. Otherwise a new
        row is inserted copying the domain's full metadata. Returns the snapshot
        id, which is what gets threaded through the crawler as the seed id and
        stored on `leads.snapshot_id`.
        """
        source_domain_id = domain.get("id")
        with self._Session() as s:
            existing = (
                s.query(CrawlSnapshot.id)
                .filter_by(job_id=job_id, source_domain_id=source_domain_id)
                .first()
            )
            if existing:
                return existing.id
            snap = CrawlSnapshot(
                job_id=job_id,
                source_domain_id=source_domain_id,
                external_id=domain.get("external_id"),
                category_code=domain.get("category_code"),
                category_title=domain.get("category_title"),
                state=domain.get("state"),
                org_type=domain.get("org_type"),
                org_type_title=domain.get("org_type_title"),
                title=domain.get("title"),
                main_url=domain.get("main_url"),
                contact_url=domain.get("contact_url"),
            )
            try:
                s.add(snap)
                s.commit()
                return snap.id
            except IntegrityError:
                # Lost a race on the (job_id, source_domain_id) unique constraint —
                # someone else just inserted it; use theirs.
                s.rollback()
                existing = (
                    s.query(CrawlSnapshot.id)
                    .filter_by(job_id=job_id, source_domain_id=source_domain_id)
                    .first()
                )
                return existing.id

    def get_crawl_snapshots(self, job_id: int) -> list[dict]:
        """All frozen seed snapshots for a job (via the crawl_snapshots.job_id FK).

        Returns raw snapshot rows — both `id` (snapshot PK, threaded to the
        engine) and `source_domain_id` (the original catalog id) — so callers
        can use whichever they need without re-touching the mutable catalog.
        """
        with self._Session() as s:
            rows = (
                s.query(CrawlSnapshot)
                .filter_by(job_id=job_id)
                .order_by(CrawlSnapshot.id)
                .all()
            )
            return [
                {"id": r.id, "source_domain_id": r.source_domain_id,
                 "external_id": r.external_id, "title": r.title,
                 "main_url": r.main_url, "contact_url": r.contact_url,
                 "category_code": r.category_code, "category_title": r.category_title,
                 "state": r.state, "org_type": r.org_type,
                 "org_type_title": r.org_type_title}
                for r in rows
            ]
