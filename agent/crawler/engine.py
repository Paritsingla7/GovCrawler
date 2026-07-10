"""CrawlerEngine — config-driven, httpx-first with Playwright fallback.

Public API: `CrawlerEngine(config, cloud, job_id, browser=None)` then
`await engine.run(seeds, visited_bootstrap=None, frontier=None)`.

Design (priority queue, per-worker browser context, thread pools, frontier
checkpoint/resume, outbox backpressure) is documented in .docs/crawler.md.
"""

import asyncio
import httpx
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urlparse, urlsplit

from shared.urls import strip_www
from . import pagination
from .parser import parse_for_engine
from ..cloud_client import CloudApiClient

log = logging.getLogger(__name__)


@dataclass(order=True)
class _QueueItem:
    priority: int
    counter: int
    url: str = field(compare=False)
    depth: int = field(compare=False)
    domain_id: int | None = field(compare=False, default=None)
    is_seed: bool = field(compare=False, default=False)
    page_hops: int = field(compare=False, default=0)
    # Shared mutable [count] cell across every page in one pagination chain —
    # bounds the chain's TOTAL structural fan-out (Story #9 Task B4 / AC 7).
    # None for any page that isn't itself part of a pagination chain.
    # repr=False: multiple in-flight items reference the SAME mutating list,
    # so printing/logging one mid-chain would show a value that changes out
    # from under it — confusing in debug output, not a correctness concern.
    chain_budget: list[int] | None = field(compare=False, default=None, repr=False)


class CrawlerEngine:
    def __init__(self, config: dict, cloud: CloudApiClient, job_id: int, browser=None):
        self._cfg = config["crawler"]
        self._excfg = config["extraction"]
        # Ships disabled by default; absent in older config.yaml files is safe.
        self._pag = self._cfg.get("pagination", {})
        self._cloud = cloud
        self._job_id = job_id
        self._browser = browser

        self._queue: asyncio.PriorityQueue[_QueueItem] = asyncio.PriorityQueue()
        # Everything queued OR currently in-flight (removed only once a
        # worker fully finishes with it) — the frontier checkpoint snapshot
        # source. Keyed by counter since that's already a stable per-item id.
        self._pending: dict[int, _QueueItem] = {}
        self._visited: set[str] = set()
        self._domain_locks: dict[str, asyncio.Lock] = {}
        self._domain_next: dict[str, float] = {}  # netloc → earliest next-request time
        self._counter = 0
        self._skipped = 0
        self._session_visited_count = 0
        self._max_depth_seen = 0
        self._active_workers = 0
        # Wholesale counters sent on every heartbeat (the coordination API has
        # no per-write increment call anymore — save_lead is outboxed/async).
        self._leads_found = 0
        self._crawled_domains = 0

        self._netloc_to_domain: dict[str, int] = {}

        # Initialised in run() once we have a running loop.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: httpx.AsyncClient | None = None
        self._db_pool: ThreadPoolExecutor | None = None
        self._checkpoint_pool: ThreadPoolExecutor | None = None
        self._parse_pool: ThreadPoolExecutor | None = None
        self._run_task: asyncio.Task | None = None

    # ── Small helpers ───────────────────────────────────────────────────────────

    def _next_counter(self) -> int:
        self._counter += 1
        return self._counter

    @staticmethod
    def _url_key(url: str) -> str:
        """Scheme-agnostic, www-stripped, slash-normalised key for visited-set
        membership. Treats http/https and www/no-www as the same page.
        Query strings are preserved — ?page=2 is a different page than ?page=1."""
        try:
            p = urlsplit(url)
            netloc = p.netloc.lower().removeprefix("www.")
            path = p.path.rstrip("/") or "/"
            key = netloc + path
            if p.query:
                key += "?" + p.query
            return key
        except Exception:
            return url

    async def _enqueue(
        self,
        url: str,
        depth: int,
        domain_id: int | None,
        is_seed: bool = False,
        page_hops: int = 0,
        chain_budget: list[int] | None = None,
    ):
        if self._is_skippable(url) or not self._is_gov_domain(url):
            self._skipped += 1
            return

        key = self._url_key(url)
        if key in self._visited:
            self._skipped += 1
            return

        if not is_seed:
            self._visited.add(key)
        item = _QueueItem(
            priority=self._url_priority(url),
            counter=self._next_counter(),
            url=url,
            depth=depth,
            domain_id=domain_id,
            is_seed=is_seed,
            page_hops=page_hops,
            chain_budget=chain_budget,
        )
        self._pending[item.counter] = item
        await self._queue.put(item)

    def _url_priority(self, url: str) -> int:
        kws = self._cfg.get("priority_keywords", [])
        if not kws:
            return 0  # no keywords configured → everything is equal priority
        return 0 if any(kw in url.lower() for kw in kws) else 1

    def _is_skippable(self, url: str) -> bool:
        exts = self._cfg.get("skip_extensions", [])
        if not exts:
            return False
        # Compare against the PATH only, so "/file.pdf?v=2" is still skipped.
        path = urlsplit(url).path.lower()
        return any(path.endswith(ext) for ext in exts)

    def _is_gov_domain(self, url: str) -> bool:
        suffixes = self._cfg.get("target_suffixes", [])
        if not suffixes:
            return True  # empty → accept all domains
        netloc = urlparse(url).netloc.lower()
        return any(netloc.endswith(s) for s in suffixes)

    def _is_priority_url(self, url: str, anchor_text: str = "") -> bool:
        kws = self._cfg.get("priority_keywords", [])
        if not kws:
            return True  # empty → treat every page/link as priority
        combined = (url + " " + anchor_text).lower()
        return any(kw in combined for kw in kws)

    def _needs_js(self, html: str) -> bool:
        return any(ind in html for ind in self._cfg.get("js_indicators", []))

    def _domain_lock(self, netloc: str) -> asyncio.Lock:
        lock = self._domain_locks.get(netloc)
        if lock is None:
            lock = asyncio.Lock()
            self._domain_locks[netloc] = lock
        return lock

    # ── Run ───────────────────────────────────────────────────────────────────

    async def run(
        self, seeds: list[tuple[str, int | None]], visited_bootstrap: list[str] = None, frontier: dict | None = None
    ):
        """seeds: list of (url, domain_id) tuples. visited_bootstrap: URLs
        agent/api.py already computed as pre-visited for this job (this
        machine's own recent crawl history, minus this job's own seed
        domains — recrawl protection is 100% local, plan.md §19.1 Phase 9
        Part 2) — see agent/api.py:_local_visited_bootstrap.
        frontier: a previous _save_checkpoint() snapshot (from agent/api.py's
        resume route) — when given, the queue is rehydrated from it instead
        of enqueueing `seeds` fresh, so a resumed run continues rather than
        restarts. `visited_bootstrap` is still applied (unioned) even on a
        resume, since it may have grown since the checkpoint was taken."""
        self._loop = asyncio.get_running_loop()
        self._run_task = asyncio.current_task()

        # Build netloc → domain_id map so mid-crawl link discovery can resolve
        # a discovered page's snapshot id back from its netloc.
        for url, did in seeds:
            parsed_url = url if "://" in url else "http://" + url
            netloc = urlparse(parsed_url).netloc.lower()
            root = strip_www(netloc)
            if did is not None:
                self._netloc_to_domain[netloc] = did
                self._netloc_to_domain[root] = did

        for v in visited_bootstrap or []:
            self._visited.add(self._url_key(v))

        if frontier:
            await self._rehydrate_frontier(frontier)

        workers = self._cfg["workers"]
        parse_workers = self._cfg.get("parse_workers") or (os.cpu_count() or 4)

        # Shared, pooled httpx client for all workers.
        hcfg = self._cfg.get("httpx_timeout", {})
        timeout = httpx.Timeout(
            connect=hcfg.get("connect", 10),
            read=hcfg.get("read", 30),
            write=5,
            pool=5,
        )
        limits = httpx.Limits(
            max_connections=max(workers * 2, 10),
            max_keepalive_connections=max(workers, 10),
        )
        self._client = httpx.AsyncClient(
            headers={"User-Agent": self._cfg.get("user_agent", "")},
            follow_redirects=True,
            timeout=timeout,
            limits=limits,
        )

        # Off-loop executors: DB writes serialized on a single thread; parsing
        # spread across cores (GIL-bound, but never blocks the event loop).
        # Checkpoint gets its OWN single thread — it used to share _db_pool with
        # mark_visited/save_leads, so every 5s frontier rewrite (potentially a
        # large JSON blob) queued behind and stalled outbox writes on the same
        # thread, and vice versa.
        self._db_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db")
        self._checkpoint_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="checkpoint")
        self._parse_pool = ThreadPoolExecutor(max_workers=parse_workers, thread_name_prefix="parse")

        if not frontier:
            for url, did in seeds:
                await self._enqueue(url, depth=0, domain_id=did, is_seed=True, page_hops=0)

        # One Playwright context per worker (None if disabled / no browser).
        if self._cfg.get("playwright_fallback") and self._browser:

            async def _new_ctx():
                try:
                    return await self._browser.new_context(user_agent=self._cfg.get("user_agent", ""))
                except Exception:
                    return None

            contexts = list(await asyncio.gather(*[_new_ctx() for _ in range(workers)]))
        else:
            contexts = [None] * workers

        tasks = [asyncio.create_task(self._worker(i, contexts[i])) for i in range(workers)]
        reporter_task = asyncio.create_task(self._reporter())
        checkpoint_task = asyncio.create_task(self._checkpoint_loop())

        try:
            await self._queue.join()
        finally:
            reporter_task.cancel()
            checkpoint_task.cancel()
            for t in tasks:
                t.cancel()
            await asyncio.gather(reporter_task, checkpoint_task, *tasks, return_exceptions=True)

            for ctx in contexts:
                if ctx:
                    try:
                        await ctx.close()
                    except Exception:
                        pass

            if self._client:
                try:
                    await self._client.aclose()
                except Exception:
                    pass

            # Flush any in-flight DB writes before we report final metrics.
            if self._db_pool:
                self._db_pool.shutdown(wait=True)
            if self._checkpoint_pool:
                self._checkpoint_pool.shutdown(wait=True)
            if self._parse_pool:
                self._parse_pool.shutdown(wait=False)

            # Final heartbeat (crawl is over — active_workers=0).
            try:
                await self._cloud.send_heartbeat(self._metrics_snapshot(active_workers=0))
            except Exception:
                pass

    def _metrics_snapshot(self, active_workers: int) -> dict:
        return {
            "queued_urls": self._queue.qsize(),
            "visited_urls": self._session_visited_count,
            "skipped_urls": self._skipped,
            "leads_found": self._leads_found,
            "crawled_domains": self._crawled_domains,
            "current_depth": self._max_depth_seen,
            "active_workers": active_workers,
        }

    async def _reporter(self):
        try:
            while True:
                await asyncio.sleep(2)
                try:
                    cancel_requested = await self._cloud.send_heartbeat(
                        self._metrics_snapshot(active_workers=self._active_workers)
                    )
                except Exception:
                    # A transient network blip must not kill this loop forever —
                    # that would silently starve the cloud reaper of heartbeats
                    # and it'd wrongly flip a still-healthy crawl to `interrupted`
                    # ~150s later (plan.md §10.6). Skip this beat, try again next.
                    log.warning(f"Job {self._job_id}: heartbeat send failed, will retry", exc_info=True)
                    continue
                if cancel_requested and self._run_task:
                    log.info(f"Job {self._job_id}: cancel_requested seen on heartbeat, stopping.")
                    self._run_task.cancel()
                    return
        except asyncio.CancelledError:
            pass

    async def _checkpoint_loop(self):
        """Periodically persists the in-progress frontier (§10.4) — checkpointed
        every 5s, same shape as `_reporter`'s heartbeat loop. A crash between
        checkpoints loses at most 5s of queue progress, never more (already-
        flushed leads/visited are unaffected — this only concerns the queue)."""
        try:
            while True:
                await asyncio.sleep(5)
                try:
                    await self._loop.run_in_executor(self._checkpoint_pool, self._save_checkpoint)
                except Exception:
                    log.warning(f"Job {self._job_id}: checkpoint save failed, will retry", exc_info=True)
        except asyncio.CancelledError:
            pass

    def _save_checkpoint(self):
        chain_keys: dict[int, str] = {}  # id(chain_budget list) -> chain_key
        chains: dict[str, int] = {}  # chain_key -> current budget value
        items = []
        for item in self._pending.values():
            chain_key = None
            if item.chain_budget is not None:
                obj_id = id(item.chain_budget)
                if obj_id not in chain_keys:
                    chain_keys[obj_id] = str(len(chain_keys))
                    chains[chain_keys[obj_id]] = item.chain_budget[0]
                chain_key = chain_keys[obj_id]
            items.append(
                {
                    "priority": item.priority,
                    "counter": item.counter,
                    "url": item.url,
                    "depth": item.depth,
                    "domain_id": item.domain_id,
                    "is_seed": item.is_seed,
                    "page_hops": item.page_hops,
                    "chain_key": chain_key,
                }
            )
        self._cloud.save_frontier(
            {
                "visited": list(self._visited),
                "counter": self._counter,
                "skipped": self._skipped,
                "max_depth_seen": self._max_depth_seen,
                "session_visited_count": self._session_visited_count,
                "chains": chains,
                "items": items,
            }
        )

    async def _rehydrate_frontier(self, frontier: dict):
        """Inverse of `_save_checkpoint` — rebuilds one shared chain_budget
        list per chain_key FIRST, then points every item referencing that key
        at the SAME object, preserving the aliasing `_enqueue_links` depends
        on to bound a chain's total fan-out (see module research: naively
        restoring one independent [value] list per item would silently
        defeat that cap)."""
        self._visited |= set(frontier.get("visited", []))
        self._counter = max(self._counter, frontier.get("counter", 0))
        self._skipped = frontier.get("skipped", 0)
        self._max_depth_seen = frontier.get("max_depth_seen", 0)
        self._session_visited_count = frontier.get("session_visited_count", 0)

        chain_objs: dict[str, list[int]] = {key: [budget] for key, budget in frontier.get("chains", {}).items()}
        for it in frontier.get("items", []):
            chain_budget = chain_objs.get(it["chain_key"]) if it.get("chain_key") is not None else None
            item = _QueueItem(
                priority=it["priority"],
                counter=it["counter"],
                url=it["url"],
                depth=it["depth"],
                domain_id=it.get("domain_id"),
                is_seed=it.get("is_seed", False),
                page_hops=it.get("page_hops", 0),
                chain_budget=chain_budget,
            )
            self._pending[item.counter] = item
            await self._queue.put(item)
        log.info(
            f"Job {self._job_id}: rehydrated {len(frontier.get('items', []))} frontier item(s) "
            f"across {len(chain_objs)} pagination chain(s)"
        )

    # ── Worker loop ───────────────────────────────────────────────────────────

    async def _worker(self, worker_id: int, browser_context):
        while True:
            try:
                item: _QueueItem = await self._queue.get()
            except asyncio.CancelledError:
                break  # cancelled while idle — no item taken, nothing to ack
            self._active_workers += 1
            try:
                await asyncio.wait_for(
                    self._process(item, browser_context),
                    timeout=self._cfg.get("per_url_timeout", 100),
                )
            except asyncio.TimeoutError:
                log.warning(f"[w{worker_id}] stall killed: {item.url}")
            except asyncio.CancelledError:
                self._active_workers -= 1
                self._pending.pop(item.counter, None)
                self._queue.task_done()
                raise  # propagate so the task actually stops
            except Exception as e:
                log.error(f"[w{worker_id}] unhandled error on {item.url}: {e}")
            self._active_workers -= 1
            # Exactly one task_done per dequeued item (the CancelledError branch
            # above already acked-and-raised, so we never double-count). Popped
            # from _pending here too — this item is now fully finished, whether
            # it succeeded, timed out, or errored.
            self._pending.pop(item.counter, None)
            self._queue.task_done()

    # ── URL processing ────────────────────────────────────────────────────────

    async def _process(self, item: _QueueItem, browser_context):
        url = item.url
        depth = item.depth
        # NB: the seed "domain id" threaded through the queue is actually a
        # crawl_snapshots.id (a per-crawl frozen snapshot), not a domains.id.
        domain_id = item.domain_id

        if depth > self._max_depth_seen:
            self._max_depth_seen = depth

        html = await self._fetch(url, browser_context)
        if not html:
            if item.is_seed:
                self._crawled_domains += 1
            return

        # Counted only on a successful fetch — counting every attempt (including
        # fetch failures) inflated visited_urls above the actual pages crawled.
        self._session_visited_count += 1

        # Persist to visited_urls only AFTER a successful fetch: a failed fetch
        # leaves the URL un-marked so it's retried next run instead of being
        # stranded (and, for a pagination page, stranding the rest of its
        # chain) for recrawl_days. Seeds are never marked — they must stay
        # re-crawlable entry points; marking them would pollute the recrawl set
        # (see .docs/crawler.md, .docs/resilience.md).
        if not item.is_seed:
            await self._loop.run_in_executor(self._db_pool, self._cloud.mark_visited, url)

        leads, raw_links = await self._loop.run_in_executor(self._parse_pool, parse_for_engine, html, url, self._excfg)

        if domain_id is None:
            netloc = urlparse(url).netloc
            domain_id = self._netloc_to_domain.get(netloc) or self._netloc_to_domain.get(strip_www(netloc))

        new_leads = await self._loop.run_in_executor(
            self._db_pool, self._save_leads, leads, domain_id, item.is_seed, depth
        )

        if new_leads:
            log.info(f"  +{new_leads} leads at {url}")

        max_depth = self._cfg.get("max_depth", 3)
        if max_depth == 0 or depth < max_depth:
            await self._enqueue_links(
                raw_links,
                url,
                depth,
                domain_id,
                is_seed_page=item.is_seed,
                page_hops=item.page_hops,
                chain_budget=item.chain_budget,
            )

    # ── Outbox-writer-pool callable (run on the single DB thread) ────────────────

    def _save_leads(self, leads, snapshot_id, is_seed, depth: int = 0) -> int:
        """Enqueues each lead into the local outbox — fire-and-forget, so the
        return count is an ATTEMPT count, not a confirmed-novel count (the
        outbox model means we no longer learn synchronously whether the cloud
        found it a duplicate). `leads_found`/`crawled_domains` become local
        running totals sent wholesale on the next heartbeat, since there's no
        per-write increment call anymore."""
        attempted = 0
        for lead in leads:
            if not lead.email:
                continue
            self._cloud.save_lead(
                snapshot_id=snapshot_id,
                email=lead.email,
                person_name=lead.person_name,
                designation=lead.designation,
                department=lead.department,
                source_url=lead.source_url,
                source_title=lead.source_title,
                context_snippet=lead.context_snippet,
                entity_kind=lead.entity_kind,
                phone=lead.phone,
                channel_tag=lead.channel_tag,
                confidence_band=lead.confidence_band,
                field_provenance=lead.field_provenance,
                depth=depth,
            )
            attempted += 1
        self._leads_found += attempted
        if is_seed:
            self._crawled_domains += 1
        return attempted

    # ── Fetching ──────────────────────────────────────────────────────────────

    async def _throttle(self, netloc: str):
        """Per-domain spacing. Holds the domain lock only across the (yielding)
        sleep, so other domains run freely while same-domain requests stay spaced
        by request_delay. Called ONCE per page visit — not per httpx/Playwright
        attempt — so the fallback path never double-sleeps.
        """
        delay = self._cfg.get("request_delay", 1.5)
        lock = self._domain_lock(netloc)
        async with lock:
            now = self._loop.time()
            nxt = self._domain_next.get(netloc, 0.0)
            if nxt > now:
                await asyncio.sleep(nxt - now)
            self._domain_next[netloc] = self._loop.time() + delay

    async def _fetch(self, url: str, browser_context) -> str | None:
        netloc = urlparse(url).netloc
        await self._throttle(netloc)

        html = None
        if self._cfg.get("httpx_first", True):
            html = await self._fetch_httpx(url)
            if html and not self._needs_js(html):
                return html

        if self._cfg.get("playwright_fallback", True) and browser_context:
            pw_html = await self._fetch_playwright(url, browser_context)
            if pw_html:
                html = pw_html

        return html

    async def _fetch_httpx(self, url: str) -> str | None:
        r = None
        for attempt in range(2):  # one retry on a transient network/timeout error
            try:
                r = await self._client.get(url)
                break
            except httpx.TransportError as e:
                if attempt == 0:
                    await asyncio.sleep(1)
                    continue
                log.warning(f"httpx failed {url}: {type(e).__name__} (after retry)")
                return None
            except Exception as e:
                log.warning(f"httpx failed {url}: {type(e).__name__}")
                return None
        if r.status_code == 200:
            final_netloc = urlparse(str(r.url)).netloc
            if self._is_gov_domain(str(r.url)) or not final_netloc:
                return r.text
        return None

    async def _fetch_playwright(self, url: str, ctx) -> str | None:
        page = None
        timeout_ms = self._cfg.get("playwright_timeout", 45) * 1000
        settle_ms = self._cfg.get("js_settle_time", 3.0) * 1000
        try:
            page = await ctx.new_page()
            try:
                await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            except Exception as nav_err:
                if "Timeout" in type(nav_err).__name__:
                    log.debug(f"Playwright timeout (1st try), retrying: {url}")
                    await asyncio.sleep(3)
                    await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
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

    async def _enqueue_links(
        self,
        raw_links: list[tuple[str, str, list[str]]],
        base_url: str,
        depth: int,
        domain_id: int | None,
        is_seed_page: bool = False,
        page_hops: int = 0,
        chain_budget: list[int] | None = None,
    ):
        if self._cloud.is_backpressured:
            # The local outbox is backed up past its threshold (a long cloud
            # outage) — pause NEW discovery from this page entirely.
            # Already-queued/in-flight items keep draining normally; nothing
            # already found is dropped, per plan.md §10.3.
            log.debug(f"outbox backpressured — skipping link discovery from {base_url}")
            self._skipped += len(raw_links)
            return

        depth_limits = self._cfg.get("max_links_per_page", {})
        max_links = depth_limits.get(str(depth), depth_limits.get("default", 5))

        # Seed pages are always treated as priority — the user explicitly chose
        # them, so all their links (up to max_links) should be followed regardless
        # of whether the seed URL itself contains priority keywords.
        is_priority_page = is_seed_page or self._is_priority_url(base_url)
        links_added = 0

        pagination_on = bool(self._pag.get("enabled"))
        max_pagination_pages = pagination.safe_int(self._pag.get("max_pagination_pages", 50), 50)
        max_chain_children = pagination.safe_int(self._pag.get("max_chain_children", 100), 100)
        pagination_target = pagination.elect_pagination_target(self._pag, raw_links, pagination_on)

        for absolute, text, rel in raw_links:
            is_pag = pagination_target is not None and absolute == pagination_target

            # Per-page link cap governs only non-chain pages (chain pages use the
            # shared chain budget below). Gated on `pagination_target is not None`,
            # not the global flag, so a page with no pagination link keeps the
            # original break/_skipped semantics. See .docs/crawler.md.
            if not is_pag and chain_budget is None and max_links > 0 and links_added >= max_links:
                self._skipped += 1
                if pagination_target is not None:
                    continue
                break
            if self._is_skippable(absolute):
                self._skipped += 1
                continue
            if not self._is_gov_domain(absolute):
                self._skipped += 1
                continue
            if self._url_key(absolute) in self._visited:
                self._skipped += 1
                continue

            if is_pag:
                # Pagination bypasses the per-page link cap and priority filter;
                # it spends page_hops, not depth/links_added. See .docs/crawler.md.
                if page_hops >= max_pagination_pages:
                    self._skipped += 1
                    continue
                # Reuse the chain's shared budget cell once mid-chain;
                # this is the first hop into a chain otherwise, so mint one.
                child_budget = chain_budget if chain_budget is not None else [0]
                await self._enqueue(
                    absolute, depth=depth, domain_id=domain_id, page_hops=page_hops + 1, chain_budget=child_budget
                )
                continue

            # On a non-priority page, only follow links that look priority. With
            # no priority_keywords configured, is_priority_page is always True,
            # so this filter is disabled and every link is followed.
            if not is_priority_page and not self._is_priority_url(absolute, text):
                self._skipped += 1
                continue

            if chain_budget is not None:
                # This page is itself a chain page: its structural children
                # draw from the shared per-chain budget, not the per-depth
                # cap, and do NOT carry the budget further — it bounds each
                # chain page's direct fan-out, not the whole subtree beneath it.
                if chain_budget[0] >= max_chain_children:
                    self._skipped += 1
                    continue
                chain_budget[0] += 1
                await self._enqueue(absolute, depth=depth + 1, domain_id=domain_id, page_hops=page_hops)
                continue

            await self._enqueue(absolute, depth=depth + 1, domain_id=domain_id, page_hops=page_hops)
            links_added += 1
