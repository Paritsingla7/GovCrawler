"""
CrawlerEngine — config-driven, httpx-first, Playwright fallback.

Design goals (this revision):
  - Nothing blocking runs on the event loop. The crawl shares the same loop as
    the API/uvicorn server, so every CPU-bound parse and every synchronous DB
    write is offloaded to a thread pool. The loop stays free for network I/O
    and the web UI stays responsive.
  - HTML is parsed exactly ONCE per page. A single BeautifulSoup tree feeds both
    lead extraction and link discovery (see parser.parse_for_engine / parser.extract_leads).
  - One shared httpx.AsyncClient across all workers → connection pooling and
    keep-alive instead of a fresh TCP+TLS handshake per URL.
  - Per-domain politeness spacing without double-sleeping the httpx→Playwright
    fallback and without starving other domains.
  - Re-runs advance the frontier: URLs visited in earlier jobs (within
    recrawl_days) are skipped, so each run picks up the NEXT batch of links
    while the seed page itself is always re-crawled.

Key properties:
  - asyncio.PriorityQueue   → contact pages crawled before generic pages
  - httpx-first             → skip browser for ~60-70% of plain HTML gov sites
  - Per-worker browser ctx  → isolates sessions, eliminates TargetClosedError
  - Dedicated DB writer pool (1 thread) → serialized, off-loop persistence
  - Parse pool              → BeautifulSoup off the event loop
  - recrawl_days            → global visited URL set prevents re-crawling fresh URLs

Public API (unchanged — drop-in replacement):
  - CrawlerEngine(config, db, job_id, browser=None)
  - await engine.run(seeds)   where seeds: list[tuple[str, int | None]]
"""

import asyncio
import httpx
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse, urlsplit

from .parser import parse_for_engine
from ..db import Database

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
    def __init__(self, config: dict, db: Database, job_id: int, browser=None):
        self._cfg = config["crawler"]
        self._excfg = config["extraction"]
        # Ships disabled by default; absent in older config.yaml files is safe.
        self._pag = self._cfg.get("pagination", {})
        self._db = db
        self._job_id = job_id
        self._browser = browser

        self._queue: asyncio.PriorityQueue[_QueueItem] = asyncio.PriorityQueue()
        self._visited: set[str] = set()
        self._domain_locks: dict[str, asyncio.Lock] = {}
        self._domain_next: dict[str, float] = {}  # netloc → earliest next-request time
        self._counter = 0
        self._skipped = 0
        self._session_visited_count = 0
        self._max_depth_seen = 0
        self._active_workers = 0

        self._netloc_to_domain: dict[str, int] = {}

        # Initialised in run() once we have a running loop.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: httpx.AsyncClient | None = None
        self._db_pool: ThreadPoolExecutor | None = None
        self._parse_pool: ThreadPoolExecutor | None = None

    # ── Small helpers ───────────────────────────────────────────────────────────

    def _next_counter(self) -> int:
        self._counter += 1
        return self._counter

    @staticmethod
    def _strip_www(netloc: str) -> str:
        return netloc.removeprefix("www.")

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

    async def _enqueue(self, url: str, depth: int, domain_id: int | None,
                       is_seed: bool = False, page_hops: int = 0,
                       chain_budget: list[int] | None = None):
        if self._is_skippable(url) or not self._is_gov_domain(url):
            self._skipped += 1
            return

        key = self._url_key(url)
        if key in self._visited:
            self._skipped += 1
            return

        if not is_seed:
            self._visited.add(key)
        await self._queue.put(_QueueItem(
            priority=self._url_priority(url),
            counter=self._next_counter(),
            url=url,
            depth=depth,
            domain_id=domain_id,
            is_seed=is_seed,
            page_hops=page_hops,
            chain_budget=chain_budget,
        ))

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

    async def run(self, seeds: list[tuple[str, int | None]]):
        """seeds: list of (url, domain_id) tuples."""
        self._loop = asyncio.get_running_loop()

        # Build netloc → domain_id map AND collect seed root domains for recrawl filtering.
        # We track root domains (e.g. "example.gov.in") rather than exact netlocs so that
        # subdomains discovered during crawling ("sub.example.gov.in") are also excluded
        # from recrawl protection and remain re-crawlable on reruns.
        seed_netlocs: set[str] = set()
        seed_root_domains: set[str] = set()
        for url, did in seeds:
            parsed_url = url if "://" in url else "http://" + url
            netloc = urlparse(parsed_url).netloc.lower()
            root = self._strip_www(netloc)
            seed_netlocs.add(netloc)
            seed_netlocs.add(root)
            seed_root_domains.add(root)
            if did is not None:
                self._netloc_to_domain[netloc] = did
                self._netloc_to_domain[root] = did

        # Resume: URLs already visited in THIS job (handles mid-run restarts).
        for v in self._db.get_visited_urls(self._job_id):
            self._visited.add(self._url_key(v))

        # Recrawl protection: skip non-seed-domain URLs visited within recrawl_days.
        # Any URL whose registered domain matches (or is a subdomain of) a seed domain
        # is excluded — so reruns always re-crawl the full seed-domain frontier fresh.
        for v in self._db.get_recently_visited_global():
            v_netloc = urlparse(v).netloc.lower()
            v_root = self._strip_www(v_netloc)
            is_seed_related = any(
                v_root == r or v_root.endswith("." + r)
                for r in seed_root_domains
            )
            if not is_seed_related:
                self._visited.add(self._url_key(v))

        workers = self._cfg["workers"]
        parse_workers = self._cfg.get("parse_workers") or (os.cpu_count() or 4)

        # Shared, pooled httpx client for all workers.
        hcfg = self._cfg.get("httpx_timeout", {})
        timeout = httpx.Timeout(
            connect=hcfg.get("connect", 10),
            read=hcfg.get("read", 30),
            write=5, pool=5,
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
        self._db_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db")
        self._parse_pool = ThreadPoolExecutor(max_workers=parse_workers,
                                              thread_name_prefix="parse")

        for url, did in seeds:
            await self._enqueue(url, depth=0, domain_id=did, is_seed=True, page_hops=0)

        # One Playwright context per worker (None if disabled / no browser).
        if self._cfg.get("playwright_fallback") and self._browser:
            async def _new_ctx():
                try:
                    return await self._browser.new_context(
                        user_agent=self._cfg.get("user_agent", "")
                    )
                except Exception:
                    return None

            contexts = list(await asyncio.gather(
                *[_new_ctx() for _ in range(workers)]))
        else:
            contexts = [None] * workers

        tasks = [
            asyncio.create_task(self._worker(i, contexts[i]))
            for i in range(workers)
        ]
        reporter_task = asyncio.create_task(self._reporter())

        try:
            await self._queue.join()
        finally:
            reporter_task.cancel()
            for t in tasks:
                t.cancel()
            await asyncio.gather(reporter_task, *tasks, return_exceptions=True)

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
            if self._parse_pool:
                self._parse_pool.shutdown(wait=False)

            # Final metric update (direct sync call — crawl is over).
            try:
                self._db.update_job_metrics(
                    self._job_id, self._queue.qsize(),
                    self._session_visited_count, self._skipped,
                    self._max_depth_seen, 0)
            except Exception:
                pass

    async def _reporter(self):
        try:
            while True:
                await asyncio.sleep(2)
                await self._loop.run_in_executor(
                    self._db_pool, self._db.update_job_metrics,
                    self._job_id, self._queue.qsize(),
                    self._session_visited_count, self._skipped,
                    self._max_depth_seen, self._active_workers)
        except asyncio.CancelledError:
            pass

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
                self._queue.task_done()
                raise  # propagate so the task actually stops
            except Exception as e:
                log.error(f"[w{worker_id}] unhandled error on {item.url}: {e}")
            self._active_workers -= 1
            # Exactly one task_done per dequeued item (the CancelledError branch
            # above already acked-and-raised, so we never double-count).
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
        self._session_visited_count += 1
        # Seeds are NOT written to visited_urls in the DB. They are always
        # re-crawlable entry points (is_seed bypass in _enqueue) and writing them
        # to the DB would pollute the recrawl protection set, causing their child
        # pages to be treated as "recently visited by a seed" on future runs.
        # Child pages ARE marked visited so recrawl protection and dedup work.
        if not item.is_seed:
            await self._loop.run_in_executor(
                self._db_pool, self._db.mark_visited, url, self._job_id)

        html = await self._fetch(url, browser_context)
        if not html:
            if item.is_seed:
                await self._loop.run_in_executor(
                    self._db_pool, self._inc_progress, 0, True)
            return

        leads, raw_links = await self._loop.run_in_executor(
            self._parse_pool, parse_for_engine, html, url, self._excfg)

        if domain_id is None:
            netloc = urlparse(url).netloc
            domain_id = (self._netloc_to_domain.get(netloc) or
                         self._netloc_to_domain.get(self._strip_www(netloc)))

        new_leads = await self._loop.run_in_executor(
            self._db_pool, self._save_leads, leads, domain_id, item.is_seed, depth)

        if new_leads:
            log.info(f"  +{new_leads} leads at {url}")

        max_depth = self._cfg.get("max_depth", 3)
        if max_depth == 0 or depth < max_depth:
            await self._enqueue_links(raw_links, url, depth, domain_id,
                                      is_seed_page=item.is_seed,
                                      page_hops=item.page_hops,
                                      chain_budget=item.chain_budget)

    # ── DB writer-pool callables (run on the single DB thread) ──────────────────

    def _inc_progress(self, new_leads: int, domain_done: bool):
        self._db.increment_job_progress(self._job_id, new_leads=new_leads,
                                        domain_done=domain_done)

    def _save_leads(self, leads, snapshot_id, is_seed, depth: int = 0) -> int:
        new_leads = 0
        for lead in leads:
            saved = self._db.save_lead(
                job_id=self._job_id,
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
            if saved:
                new_leads += 1
        # One progress increment per page; domain_done only for the seed page.
        self._db.increment_job_progress(self._job_id, new_leads=new_leads,
                                        domain_done=is_seed)
        return new_leads

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
        try:
            r = await self._client.get(url)
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

    @staticmethod
    def _is_plain_int(value: str) -> bool:
        """Strict base-10 integer check. `str.isdigit()` alone accepts
        Unicode digit-look-alikes (e.g. superscripts) that aren't valid
        `int()` input — `isascii()` closes that gap without loosening the
        check to a substring/regex match."""
        return bool(value) and value.isascii() and value.isdigit()

    def _is_pagination_link(self, url: str, anchor_text: str, rel: list[str]) -> bool:
        """Conservative pagination classifier (Story #9). Deliberately does
        NOT check `pagination.enabled` — that gate lives at the call site in
        `_enqueue_links` — so this stays a pure, independently-testable rule.

        A paging query param, when present, is the DECIDING signal — numeric
        value means pagination, non-numeric means NOT, full stop, regardless
        of anchor text or rel. This is the entire firewall against session-URL
        traps (e.g. tntenders.gov.in) that dress a non-numeric/base64 session
        param up with a visible "Next" link — the link text lies, the query
        value doesn't. Only when the URL carries NONE of the configured
        paging params do we fall back to rel="next" / anchor-text signals.
        Never loosen the numeric check to a substring or regex match.

        Matching is case-insensitive on param NAMES (`?Page=2` must still
        hit `page`) and blank values count as present-but-non-numeric
        (`keep_blank_values=True` — a bare `?page=` must not silently fall
        through to the rel/text fallback). If more than one configured
        param_signal is present on the same URL, ANY non-numeric value among
        them rejects the whole URL — fail closed rather than picking
        whichever configured name happens to be checked first.
        """
        param_signals = self._pag.get("param_signals", [])
        if isinstance(param_signals, str):
            param_signals = [param_signals]
        if param_signals:
            query = parse_qs(urlparse(url).query, keep_blank_values=True)
            query_lower = {k.lower(): v for k, v in query.items()}
            matched_values = [
                query_lower[p.lower()][0]
                for p in param_signals
                if p.lower() in query_lower
            ]
            if matched_values:
                return all(self._is_plain_int(v) for v in matched_values)

        if "next" in rel:
            return True

        text = (anchor_text or "").strip().lower()
        text_signals = self._pag.get("text_signals", [])
        if isinstance(text_signals, str):
            text_signals = [text_signals]
        return bool(text) and (text in text_signals or self._is_plain_int(text))

    @staticmethod
    def _safe_int(value, default: int) -> int:
        """Defensive coercion for pagination config values — a YAML typo
        (a string where an int belongs) must fall back safely, not crash
        the worker's blanket except-and-swallow handler mid-crawl."""
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _elect_pagination_target(self, raw_links, pagination_on: bool) -> str | None:
        """Pick AT MOST ONE pagination link per page — prefer an explicit
        rel="next" signal, else the first classifier-accepted link in
        document order. Without this, a normal numbered pager bar
        ("1 2 3 4 5 Next Last") would classify EVERY matching link as
        pagination independently, each bypassing the per-page cap and
        minting its own fresh chain_budget — multiplying the intended
        amplification bound by however many links the pager shows (code
        review finding, 2026-07-02). Real "next page" progression is one
        hop per page, matching how `page_hops` is defined as a linear counter.
        """
        if not pagination_on:
            return None
        first_match = None
        for absolute, text, rel in raw_links:
            if not self._is_pagination_link(absolute, text, rel):
                continue
            if "next" in rel:
                return absolute
            if first_match is None:
                first_match = absolute
        return first_match

    async def _enqueue_links(self, raw_links: list[tuple[str, str, list[str]]],
                             base_url: str, depth: int, domain_id: int | None,
                             is_seed_page: bool = False, page_hops: int = 0,
                             chain_budget: list[int] | None = None):
        depth_limits = self._cfg.get("max_links_per_page", {})
        max_links = depth_limits.get(str(depth),
                                     depth_limits.get(depth,
                                                      depth_limits.get("default", 5)))

        # Seed pages are always treated as priority — the user explicitly chose
        # them, so all their links (up to max_links) should be followed regardless
        # of whether the seed URL itself contains priority keywords.
        is_priority_page = is_seed_page or self._is_priority_url(base_url)
        links_added = 0

        pagination_on = bool(self._pag.get("enabled"))
        max_pagination_pages = self._safe_int(self._pag.get("max_pagination_pages", 50), 50)
        max_chain_children = self._safe_int(self._pag.get("max_chain_children", 100), 100)
        pagination_target = self._elect_pagination_target(raw_links, pagination_on)

        for absolute, text, rel in raw_links:
            is_pag = pagination_target is not None and absolute == pagination_target

            # The per-page link cap only governs pages OUTSIDE a pagination
            # chain. A page that is itself part of a chain (chain_budget is
            # not None) has its structural fan-out governed by the shared
            # chain budget below instead (Task B4 / AC 7) — otherwise a
            # 50-page chain would re-apply this cap on every single page.
            #
            # Gated on `pagination_target is not None` (does THIS page have
            # an elected pagination hit), NOT the global `pagination_on`
            # flag — a page with zero pagination links must keep the exact
            # original `break` semantics (including the `_skipped` count)
            # even when the feature is enabled elsewhere (AC 9; code review
            # finding, 2026-07-02).
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
                # Pagination bypasses the per-page link cap and the priority-
                # keyword filter (a "Next" anchor rarely carries a keyword) —
                # it spends page_hops, not depth or links_added (AC 1, 2, 3).
                #
                # KNOWN INTERACTION (present & intentionally NOT fixed here —
                # separate effort, Story #9 Task B5): mark_visited fires
                # BEFORE _fetch in _process(), so a transient failure on any
                # page mid-chain permanently strands the rest of that chain
                # for `recrawl_days` — and because chain continuation depends
                # entirely on parsing the current page, it also aborts the
                # REST OF THE CURRENT crawl's discovery down this chain, not
                # just future recrawls. Longer chains (this fix's whole
                # point) hit that flake more often than today's depth-4
                # chains did.
                if page_hops >= max_pagination_pages:
                    self._skipped += 1
                    continue
                # Reuse the chain's shared budget cell once mid-chain;
                # this is the first hop into a chain otherwise, so mint one.
                child_budget = chain_budget if chain_budget is not None else [0]
                await self._enqueue(absolute, depth=depth, domain_id=domain_id,
                                    page_hops=page_hops + 1,
                                    chain_budget=child_budget)
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
                await self._enqueue(absolute, depth=depth + 1, domain_id=domain_id,
                                    page_hops=page_hops)
                continue

            await self._enqueue(absolute, depth=depth + 1, domain_id=domain_id,
                                page_hops=page_hops)
            links_added += 1
