"""
CrawlerEngine — config-driven, httpx-first, Playwright fallback.

Key properties:
  - asyncio.PriorityQueue   → contact pages crawled before generic pages
  - httpx-first             → skip browser for ~60-70% of plain HTML gov sites
  - Per-worker browser ctx  → isolates sessions, eliminates TargetClosedError
  - Per-domain asyncio.Lock → rate limiting without global bottleneck
  - wait_for(process(), timeout=per_url_timeout) → wraps ENTIRE fetch+parse
  - recrawl_days            → global visited URL set prevents re-crawling fresh URLs
"""

import asyncio
import logging
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx

from ..db.models import Database
from .parser import parse_page

log = logging.getLogger(__name__)


@dataclass(order=True)
class _QueueItem:
    priority: int
    counter:  int
    url:      str       = field(compare=False)
    depth:    int       = field(compare=False)
    domain_id: int | None = field(compare=False, default=None)
    is_seed:  bool      = field(compare=False, default=False)


class CrawlerEngine:
    def __init__(self, config: dict, db: Database, job_id: int, browser=None):
        self._cfg   = config["crawler"]
        self._excfg = config["extraction"]
        self._db    = db
        self._job_id = job_id
        self._browser = browser

        self._queue: asyncio.PriorityQueue[_QueueItem] = asyncio.PriorityQueue()
        self._visited: set[str] = set()
        self._domain_locks: dict[str, asyncio.Lock] = {}
        self._counter = 0

        self._netloc_to_domain: dict[str, int] = {}

    def _next_counter(self) -> int:
        self._counter += 1
        return self._counter

    async def _enqueue(self, url: str, depth: int, domain_id: int | None,
                       is_seed: bool = False):
        if url in self._visited or self._is_skippable(url) or not self._is_gov_domain(url):
            return
        self._visited.add(url)
        priority = self._url_priority(url)
        await self._queue.put(_QueueItem(
            priority=priority,
            counter=self._next_counter(),
            url=url,
            depth=depth,
            domain_id=domain_id,
            is_seed=is_seed,
        ))

    def _url_priority(self, url: str) -> int:
        return 0 if any(kw in url.lower()
                        for kw in self._cfg.get("priority_keywords", [])) else 10

    def _is_skippable(self, url: str) -> bool:
        lower = url.lower()
        return any(lower.endswith(ext) for ext in self._cfg.get("skip_extensions", []))

    def _is_gov_domain(self, url: str) -> bool:
        netloc = urlparse(url).netloc.lower()
        return any(netloc.endswith(s) for s in self._cfg.get("target_suffixes", []))

    def _is_priority_url(self, url: str, anchor_text: str = "") -> bool:
        combined = (url + " " + anchor_text).lower()
        return any(kw in combined for kw in self._cfg.get("priority_keywords", []))

    def _needs_js(self, html: str) -> bool:
        return any(ind in html for ind in self._cfg.get("js_indicators", []))

    def _domain_lock(self, url: str) -> asyncio.Lock:
        netloc = urlparse(url).netloc
        if netloc not in self._domain_locks:
            self._domain_locks[netloc] = asyncio.Lock()
        return self._domain_locks[netloc]

    # ── Run ───────────────────────────────────────────────────────────────────

    async def run(self, seeds: list[tuple[str, int | None]]):
        """seeds: list of (url, domain_id) tuples."""

        # Resume: load URLs already visited in this job
        self._visited.update(self._db.get_visited_urls(self._job_id))
        # Recrawl protection: skip URLs visited in ANY job within recrawl_days
        self._visited.update(self._db.get_recently_visited_global())

        # Build netloc → domain_id map
        for url, did in seeds:
            if did is not None:
                netloc = urlparse(url).netloc
                self._netloc_to_domain[netloc] = did
                self._netloc_to_domain[netloc.lstrip("www.")] = did

        for url, did in seeds:
            await self._enqueue(url, depth=0, domain_id=did, is_seed=True)

        # One Playwright context per worker (None if playwright_fallback=false)
        contexts = []
        for _ in range(self._cfg["workers"]):
            if self._cfg.get("playwright_fallback") and self._browser:
                try:
                    ctx = await self._browser.new_context(
                        user_agent=self._cfg.get("user_agent", "")
                    )
                    contexts.append(ctx)
                except Exception:
                    contexts.append(None)
            else:
                contexts.append(None)

        tasks = [
            asyncio.create_task(self._worker(i, contexts[i]))
            for i in range(self._cfg["workers"])
        ]

        await self._queue.join()

        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        for ctx in contexts:
            if ctx:
                try:
                    await ctx.close()
                except Exception:
                    pass

    # ── Worker loop ───────────────────────────────────────────────────────────

    async def _worker(self, worker_id: int, browser_context):
        while True:
            try:
                item: _QueueItem = await self._queue.get()
            except asyncio.CancelledError:
                break
            try:
                await asyncio.wait_for(
                    self._process(item, browser_context),
                    timeout=self._cfg.get("per_url_timeout", 100),
                )
            except asyncio.TimeoutError:
                log.warning(f"[w{worker_id}] stall killed: {item.url}")
            except asyncio.CancelledError:
                self._queue.task_done()
                break
            except Exception as e:
                log.error(f"[w{worker_id}] unhandled error on {item.url}: {e}")
            finally:
                self._queue.task_done()

    # ── URL processing ────────────────────────────────────────────────────────

    async def _process(self, item: _QueueItem, browser_context):
        url       = item.url
        depth     = item.depth
        domain_id = item.domain_id

        self._db.mark_visited(url, self._job_id)

        html = await self._fetch(url, browser_context)
        if not html:
            if item.is_seed:
                self._db.increment_job_progress(self._job_id, domain_done=True)
            return

        leads     = parse_page(html, url, self._excfg)
        new_leads = 0

        if domain_id is None:
            netloc    = urlparse(url).netloc
            domain_id = (self._netloc_to_domain.get(netloc) or
                         self._netloc_to_domain.get(netloc.lstrip("www.")))

        for lead in leads:
            saved = self._db.save_lead(
                job_id=self._job_id,
                domain_id=domain_id,
                email=lead.email,
                person_name=lead.person_name,
                designation=lead.designation,
                department=lead.department,
                source_url=lead.source_url,
                context_snippet=lead.context_snippet,
            )
            if saved:
                new_leads += 1

        if new_leads:
            log.info(f"  +{new_leads} leads at {url}")

        self._db.increment_job_progress(self._job_id, new_leads=new_leads,
                                        domain_done=item.is_seed)

        max_depth = self._cfg.get("max_depth", 3)
        if max_depth == 0 or depth < max_depth:
            await self._queue_links(html, url, depth, domain_id)

    # ── Fetching ──────────────────────────────────────────────────────────────

    async def _fetch(self, url: str, browser_context) -> str | None:
        lock = self._domain_lock(url)
        html = None

        if self._cfg.get("httpx_first", True):
            async with lock:
                html = await self._fetch_httpx(url)
                await asyncio.sleep(self._cfg.get("request_delay", 1.5))
            if html and not self._needs_js(html):
                return html

        if self._cfg.get("playwright_fallback", True) and browser_context:
            async with lock:
                html = await self._fetch_playwright(url, browser_context)
                await asyncio.sleep(self._cfg.get("request_delay", 1.5))

        return html

    async def _fetch_httpx(self, url: str) -> str | None:
        hcfg    = self._cfg.get("httpx_timeout", {})
        timeout = httpx.Timeout(
            connect=hcfg.get("connect", 10),
            read=hcfg.get("read", 30),
            write=5, pool=5,
        )
        try:
            async with httpx.AsyncClient(
                headers={"User-Agent": self._cfg.get("user_agent", "")},
                follow_redirects=True,
                timeout=timeout,
            ) as client:
                r = await client.get(url)
                if r.status_code == 200:
                    final_netloc = urlparse(str(r.url)).netloc
                    if self._is_gov_domain(str(r.url)) or not final_netloc:
                        return r.text
        except Exception as e:
            log.debug(f"httpx failed {url}: {type(e).__name__}")
        return None

    async def _fetch_playwright(self, url: str, ctx) -> str | None:
        page        = None
        timeout_ms  = self._cfg.get("playwright_timeout", 45) * 1000
        settle_ms   = self._cfg.get("js_settle_time", 3.0) * 1000
        try:
            page = await ctx.new_page()
            try:
                await page.goto(url, timeout=timeout_ms,
                                wait_until="domcontentloaded")
            except Exception as nav_err:
                if "Timeout" in type(nav_err).__name__:
                    log.debug(f"Playwright timeout (1st try), retrying: {url}")
                    await asyncio.sleep(3)
                    await page.goto(url, timeout=timeout_ms,
                                    wait_until="domcontentloaded")
                else:
                    raise
            await page.wait_for_timeout(settle_ms)
            return await page.content()
        except Exception as e:
            err = str(e)
            if "net::ERR" in err or "Download is starting" in err:
                log.debug(f"Playwright nav error {url}: {err[:80]}")
            elif "Timeout" in type(e).__name__:
                log.warning(f"Playwright timeout (after retry): {url}")
            else:
                log.warning(f"Playwright failed {url}: {type(e).__name__}: {err[:80]}")
            return None
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass

    # ── Link discovery ────────────────────────────────────────────────────────

    async def _queue_links(self, html: str, base_url: str, depth: int,
                           domain_id: int | None):
        from bs4 import BeautifulSoup
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:
            return

        depth_limits = self._cfg.get("max_links_per_page", {})
        max_links    = depth_limits.get(str(depth),
                       depth_limits.get(depth,
                       depth_limits.get("default", 5)))

        is_priority_page = self._is_priority_url(base_url)
        links_added = 0

        for a in soup.find_all("a", href=True):
            if max_links > 0 and links_added >= max_links:
                break
            try:
                href     = a["href"].strip()
                text     = (a.get_text() or "").strip().lower()[:100]
                absolute = urljoin(base_url, href)

                if self._is_skippable(absolute):
                    continue
                if not self._is_gov_domain(absolute):
                    continue
                if absolute in self._visited:
                    continue
                if not is_priority_page and not self._is_priority_url(absolute, text):
                    continue

                await self._enqueue(absolute, depth=depth + 1, domain_id=domain_id)
                links_added += 1
            except Exception:
                continue
