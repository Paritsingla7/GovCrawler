# Crawler Engine

The crawler is the agent tier (`agent/crawler/`). `CrawlerEngine` (`engine.py`) schedules and fetches
pages; the parser (`parser.py`) turns HTML into `Lead`s. Both are config-driven — no hardcoded regexes or
keywords. All persistence goes through the durable local outbox via `CloudApiClient`, never straight to a
database (see [resilience.md](resilience.md)).

## Overview

`CrawlerEngine(config, cloud, job_id, browser=None)` runs as an `asyncio.Task` on the shared Uvicorn event
loop. `run(seeds, visited_bootstrap, frontier)`:

1. Build a netloc→domain_id map from the seeds (full netloc and `www`-stripped root).
2. Apply `visited_bootstrap` into the visited set (recrawl protection); if a `frontier` is supplied,
   rehydrate it instead of enqueuing seeds (resume).
3. Build one shared `httpx.AsyncClient` (connection limits scaled to `workers`), spawn `workers` worker
   coroutines, one Playwright **context per worker**, a `_reporter`, and a `_checkpoint_loop`.
4. Wait on `queue.join()`; on teardown cancel the helpers, close contexts + client, shut the thread pools,
   and send a final heartbeat with `active_workers=0`.

## Priority queue

An `asyncio.PriorityQueue` of `_QueueItem` (ordered by `priority`, then a monotonic `counter` tiebreaker).
`_url_priority` returns **0** if any `crawler.priority_keywords` substring appears in the URL, else **1** —
so contact/directory/officer pages are crawled before generic pages. `self._pending` (keyed by `counter`)
holds every item queued or in-flight and is the snapshot source for checkpoints; an item is removed only
when a worker fully finishes it.

## URL keys & recrawl protection

`_url_key` is scheme-agnostic: lowercased, `www.`-stripped netloc + path with trailing slash normalized,
**query string preserved** (so `?page=2` ≠ `?page=1`). Non-seed URLs are added to the visited set at
enqueue time; **seeds are never** (they stay re-crawlable). `visited_bootstrap` (the job's own visited URLs
∪ the global set visited within `recrawl_days`, minus the seeds' own root domains) is loaded before the
crawl so reruns advance the frontier instead of re-crawling fresh pages.

## Fetching — HTTPX first, Playwright fallback

`_fetch` calls `_throttle(netloc)` **once** per page (per-domain politeness: a per-netloc lock + earliest-
next-request map spaced by `request_delay`, default 1.5 s — the fallback path never double-sleeps).

- **HTTPX** (`httpx_first`, default on): returns the body only on HTTP 200 where the final (post-redirect)
  URL is still a gov domain. If the HTML trips `_needs_js` (any `crawler.js_indicators` substring), fall
  through to Playwright.
- **Playwright** (`playwright_fallback`, default on, requires a browser context): `goto(wait_until=
  "domcontentloaded")`, retry once after 3 s on timeout, wait `js_settle_time`, return `page.content()`.
  One context per worker isolates sessions and prevents `TargetClosedError`.

`_is_gov_domain` restricts to `crawler.target_suffixes` (empty list = accept all, which is how custom-URL
jobs bypass the suffix filter). `_is_skippable` drops URLs whose path ends with a `skip_extensions` entry.

## Link discovery & pagination

`_enqueue_links` respects per-depth caps (`crawler.max_links_per_page`, keyed by depth). If the local
outbox is **backpressured** (`CloudApiClient.is_backpressured`, >5000 pending rows), it skips *new*
discovery from the page so the outbox can drain — already-queued items keep going.

Pagination (`crawler.pagination`, ships **disabled**) avoids the "numbered pager spawns N chains" trap:

- `_is_pagination_link` — a `param_signals` query param (e.g. `page`, `offset`) is the deciding signal when
  present: a plain-integer value = pagination, non-numeric = not (fail-closed). Falls back to `rel="next"`,
  then `text_signals` / numeric anchor text.
- `_elect_pagination_target` — picks **at most one** pagination link per page.
- A single shared `chain_budget` cell caps a chain's total structural fan-out (`max_chain_children`,
  default 100); `page_hops` caps chain length (`max_pagination_pages`, default 50). The shared cell is
  preserved across checkpoints so resume can't reset the budget.

> Known interaction (documented in code, not yet fixed): `mark_visited` fires before `_fetch`, so a
> transient mid-chain failure can strand the rest of that chain for `recrawl_days`.

## Thread pools

- `db_pool` — 1 thread, serialized off-loop persistence (leads, visited, checkpoints).
- `parse_pool` — `parse_workers` (default `cpu_count`) threads for BeautifulSoup.

Nothing blocking runs on the event loop — every parse and every write is offloaded, because the crawl shares
Uvicorn's loop.

## Metrics, heartbeat & cancel

`_checkpoint_loop` saves a frontier snapshot every **5 s**. `_reporter` sends a heartbeat every **2 s** via
`CloudApiClient.send_heartbeat(metrics)`; if the response carries `cancel_requested`, it cancels the run
task. Metrics (`queued_urls`, `visited_urls`, `skipped_urls`, `leads_found`, `crawled_domains`,
`current_depth`, `active_workers`) flow **only** through the heartbeat — there is no per-write metric
increment call.

---

## Parser — 6-stage lead extraction

`parse_for_engine(html, url, excfg)` is the `parse_pool` target: it builds **one** `BeautifulSoup(html,
"html.parser")` (pure-Python parser, chosen for thread safety), harvests `raw_links` (absolute URL, lowercased
anchor text, `rel`) **before** the tree is decomposed, then runs `extract_leads`. Returns `(leads, raw_links)`.

The `Lead` dataclass: `email`, `person_name`, `designation`, `department`, `source_url`, `source_title`,
`context_snippet`, `entity_kind`, `phone`, `channel_tag`, `confidence_band`, `field_provenance`.

`extract_leads` is a pipes-and-filters pipeline:

1. **`_extract_candidates`** — harvest `(address, rung, node, raw_span, phone)`. Rung ladder (higher wins):
   `proximity_text` < `table_block` < `microdata` < `mailto_tel`. Sub-scans: `mailto:`/`tel:` hrefs,
   microdata `itemprop`, table cells (header→column mapping, bounded), and obfuscation-normalized proximity
   text (bounded by `max_input_chars`), including bracketed forms. `_truncate_at_known_suffix` repairs glued
   domains using `valid_suffixes`.
2. **`_bind_channels`** — group by unique email, keep the highest rung, merge a stray phone forward. Classify
   `channel_tag`/`entity_kind`: role local-part (`webmaster`, `info`, …) → `role`/`org`; non-gov domain →
   `personal-external`/`person`; else `office`/`person`. Attach phone-only candidates by DOM proximity.
3. **`_enrich_fields`** — add `person_name`, `designation`, `department`, `context_snippet` (table columns or
   a proximity text window; `person.enabled` gates it). `department` defaults to a URL-derived value; a
   page/URL mismatch is flagged.
4. **`_normalise_spans`** — guarded de-obfuscation of bracketed `proximity_text` spans **only** (never a
   global rewrite); re-validate and re-classify.
5. **`_score`** — assign `confidence_band` `HIGH` if the rung is in `high_rungs` else `LOW` (degrade to LOW
   on a department mismatch); build the `field_provenance` JSON (per-field rung). The band is informational —
   it never drops a lead.
6. **`_flatten_emit`** — one flat `Lead` per unique email; skip only email-less entities.

Email validity is governed entirely by `extraction.email.valid_suffixes`. Lead **scoring** (the 0–100
number) is separate and runs cloud-side in `save_lead` via `shared/scoring.py` — see
[configuration.md](configuration.md#lead-scoring) and [database-schema.md](database-schema.md#leads).

---

## Running / debugging a job

- **Normal:** the browser posts to `POST /api/jobs`; the agent BFF builds the engine and starts the task.
- **CLI debug:** `python -m portal crawl <job_id>` re-runs an existing job synchronously, driving the
  coordination API in-process over an ASGI transport (no live server needed) with a headless Chromium and a
  real per-job outbox at `portal/data/outbox_job_<id>.db`.
