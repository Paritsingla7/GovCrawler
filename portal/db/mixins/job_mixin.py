import datetime
import json

from ..tables.crawl import CrawlJob, CrawlJobDomain, JobCustomUrl


class JobMixin:
    def create_job(self, domain_ids: list[int] = None, custom_urls: list[str] = None,
                   category_filter: str = None, title_filter: str = None,
                   owner_id: int | None = None) -> int:
        source_type = "custom_urls" if custom_urls else "domains"
        seed_count = len(custom_urls) if custom_urls else len(domain_ids or [])
        with self._Session() as s:
            job = CrawlJob(
                domain_ids=json.dumps(domain_ids or []),
                source_type=source_type,
                category_filter=category_filter,
                title_filter=title_filter,
                total_domains=seed_count,
                seed_domains=seed_count,
                status="pending",
                owner_id=owner_id,
            )
            s.add(job)
            s.commit()
            if domain_ids:
                s.add_all([CrawlJobDomain(job_id=job.id, domain_id=d) for d in domain_ids])
                s.commit()
            return job.id

    def get_job_domain_ids(self, job_id: int) -> list[int]:
        with self._Session() as s:
            return [r[0] for r in s.query(CrawlJobDomain.domain_id).filter_by(job_id=job_id).all()]

    def add_job_custom_urls(self, job_id: int, urls: list[str]) -> None:
        with self._Session() as s:
            s.add_all([JobCustomUrl(job_id=job_id, url=url) for url in urls])
            s.commit()

    def get_job_custom_urls(self, job_id: int) -> list[dict]:
        with self._Session() as s:
            rows = (
                s.query(JobCustomUrl)
                .filter_by(job_id=job_id)
                .order_by(JobCustomUrl.id)
                .all()
            )
            return [
                {"id": r.id, "title": r.url, "main_url": r.url,
                 "contact_url": None, "category_code": "custom", "state": None,
                 "org_type": None}
                for r in rows
            ]

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

    def get_or_create_manual_upload_job(self) -> int:
        """Shared synthetic job that all CSV-uploaded manual leads attach to."""
        with self._Session() as s:
            job = s.query(CrawlJob).filter_by(status="manual_upload").first()
            if job:
                return job.id
            job = CrawlJob(status="manual_upload", total_domains=0, seed_domains=0)
            s.add(job)
            s.commit()
            return job.id

    def get_job(self, job_id: int, owner_id: int | None = None, view_all: bool = False) -> dict | None:
        with self._Session() as s:
            q = s.query(CrawlJob).filter_by(id=job_id)
            if not view_all:
                q = q.filter(CrawlJob.owner_id == owner_id)
            j = q.first()
            return self._job_dict(j) if j else None

    def list_jobs(self, limit: int = 20, owner_id: int | None = None,
                  view_all: bool = False) -> list[dict]:
        with self._Session() as s:
            q = s.query(CrawlJob)
            if not view_all:
                q = q.filter(CrawlJob.owner_id == owner_id)
            rows = q.order_by(CrawlJob.created_at.desc()).limit(limit).all()
            return [self._job_dict(j) for j in rows]

    @staticmethod
    def _job_dict(j: CrawlJob) -> dict:
        return {
            "id": j.id, "status": j.status,
            "source_type": j.source_type,
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
            "owner_id": j.owner_id,
            "category_filter": j.category_filter,
            "title_filter": j.title_filter,
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "started_at": j.started_at.isoformat() if j.started_at else None,
            "finished_at": j.finished_at.isoformat() if j.finished_at else None,
        }
