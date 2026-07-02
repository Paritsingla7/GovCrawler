import datetime
import json

from ..tables.crawl import CrawlJob, JobCustomUrl


class JobMixin:
    def create_job(self, domain_ids: list[int] = None, custom_urls: list[str] = None,
                   category_filter: str = None, title_filter: str = None) -> int:
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
            )
            s.add(job)
            s.commit()
            return job.id

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
            "category_filter": j.category_filter,
            "title_filter": j.title_filter,
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "started_at": j.started_at.isoformat() if j.started_at else None,
            "finished_at": j.finished_at.isoformat() if j.finished_at else None,
        }
