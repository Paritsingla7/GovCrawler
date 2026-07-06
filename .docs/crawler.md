# Crawler Engine

Source: [`portal/crawler/engine.py`](../portal/crawler/engine.py) and [
`portal/crawler/parser.py`](../portal/crawler/parser.py)

---

## Overview

`CrawlerEngine` is an async, priority-queue-driven web crawler. It is designed to share the Uvicorn event loop without
blocking it: all blocking work (HTML parsing, database writes) is offloaded to `ThreadPoolExecutor` pools. One shared
`httpx.AsyncClient` provides connection pooling across all workers.

```
CrawlerEngine.run(seeds)
  │
  ├── Build visited set (current job + recrawl_days global)
  ├── Enqueue seeds → PriorityQueue
  ├── Create N worker coroutines + 1 reporter coroutine
  │
  │   Worker loop (× N):
  │     ├── Dequeue _QueueItem
  │     ├── _throttle(netloc)    — per-domain politeness sleep
  │     ├── _fetch(url, ctx)
  │     │     ├── _fetch_httpx()  [fast path, ~60-70% of sites]
  │     │     └── _fetch_playwright()  [fallback for JS sites]
  │     ├── run_in_executor(parse_pool, parser.parse_for_engine)
  │     │     ├── BeautifulSoup parse
  │     │     ├── Link harvest (before tag decomposition)
  │     │     └── extract_leads()
  │     ├── run_in_executor(db_pool, _save_leads)
  │     └── _enqueue_links()
  │
  ├── queue.join()  ← blocks until all items task_done'd
  └── Cleanup: close contexts, client, flush pools, final metrics
```

---

## Configuration

All crawler behaviour is controlled by the `crawler:` section of `portal/config.yaml`.

| Key                               | Default                                  | Description                                                                                  |
|-----------------------------------|------------------------------------------|----------------------------------------------------------------------------------------------|
| `workers`                         | 10                                       | Number of concurrent async worker coroutines                                                 |
| `max_depth`                       | 4                                        | Maximum crawl depth from each seed (0 = seed only)                                           |
| `recrawl_days`                    | 30                                       | Skip URLs visited in any job within last N days                                              |
| `httpx_first`                     | true                                     | Try HTTP fetch before launching browser                                                      |
| `playwright_fallback`             | false                                    | Enable Playwright fallback for JS sites                                                      |
| `httpx_timeout.connect`           | 10                                       | TCP connect timeout (seconds)                                                                |
| `httpx_timeout.read`              | 30                                       | HTTP read timeout (seconds)                                                                  |
| `playwright_timeout`              | 45                                       | Playwright page load timeout (seconds)                                                       |
| `js_settle_time`                  | 3.0                                      | Extra wait after `domcontentloaded` for JS to execute                                        |
| `per_url_timeout`                 | 100                                      | Hard per-URL watchdog timeout (seconds)                                                      |
| `request_delay`                   | 1.5                                      | Minimum seconds between requests to the same domain                                          |
| `target_suffixes`                 | `.gov.in`, `.nic.in`                     | Only crawl URLs ending in these suffixes (bypassed entirely for custom-URL jobs — see below) |
| `max_custom_urls`                 | 50                                       | Max ad-hoc URLs a `custom_urls`-seeded job may supply                                        |
| `priority_keywords`               | `contact`, `officer`, `directory`, …     | URLs containing these are crawled first                                                      |
| `skip_extensions`                 | `.pdf`, `.doc`, `.jpg`, …                | URLs with these path extensions are skipped                                                  |
| `js_indicators`                   | `<div id="__next"`, …                    | HTML strings that signal JavaScript rendering is needed                                      |
| `max_links_per_page`              | `{0: 100, 1: 50, 2: 40, default: 20}`    | Max links to follow per depth level (bypassed for pagination links — see below)              |
| `user_agent`                      | Chrome/124 Linux                         | User-Agent header sent with all requests                                                     |
| `pagination.enabled`              | true                                     | Master switch for pagination-aware crawling                                                  |
| `pagination.max_pagination_pages` | 50                                       | Max hops followed down one pagination chain                                                  |
| `pagination.max_chain_children`   | 100                                      | Shared cap on non-pagination children spawned across one whole chain                         |
| `pagination.text_signals`         | `next`, `»`, `›`, `more`, `last`         | Anchor text marking a "next page" link (fallback only)                                       |
| `pagination.param_signals`        | `page`, `pageno`, `start`, `offset`, `p` | Query-param names checked first — deciding signal when present                               |

---

## Priority Queue

`asyncio.PriorityQueue[_QueueItem]`

```python
@dataclass(order=True)
class _QueueItem:
    priority: int        # 0 = high (contact/directory), 1 = low (generic)
    counter: int         # insertion order (tiebreak for FIFO within same priority)
    url: str
    depth: int
    domain_id: int | None
    is_seed: bool
```

Priority is determined by `_url_priority(url)`:

- Returns `0` if any `priority_keywords` appears in the URL.
- Returns `1` otherwise.
- If `priority_keywords` is empty, everything gets `0` (equal priority).

Seed pages are always enqueued with their natural priority but bypass the recrawl-visited check (`is_seed=True`),
ensuring they are always re-processed on re-runs.

---

## URL Key Normalization

The visited set uses a normalized key so `http://www.example.gov.in/contact/` and `https://example.gov.in/contact` are
treated as the same page:

```
url_key = lowercase_netloc_without_www + path_without_trailing_slash [+ ?query]
```

This prevents redundant fetches caused by redirect chains, www/non-www aliases, and HTTP/HTTPS differences.

---

## Recrawl Protection

On `run()` start, two sets of already-visited URLs are loaded:

1. **Job-specific:** `db.get_visited_urls(job_id)` — handles mid-run restarts by skipping URLs already processed in the
   current job.
2. **Global recent:** `db.get_recently_visited_global()` — skips URLs visited in *any* job within `recrawl_days`. URLs
   whose root domain matches any seed domain are excluded from this protection, so re-runs always re-crawl the seed
   domain's full frontier fresh.

---

## Fetching Strategy

### HTTPX (fast path)

```
_fetch_httpx(url)
  → httpx.AsyncClient.get(url)
  → If 200 and response URL still on a .gov.in domain → return HTML
  → Otherwise → return None
```

One shared `httpx.AsyncClient` is created per job with:

- Connection pool sized to `max(workers × 2, 10)`
- Keep-alive connections up to `max(workers, 10)`
- Follow redirects enabled

### Playwright (fallback)

```
_fetch_playwright(url, browser_context)
  → ctx.new_page()
  → page.goto(url, wait_until="domcontentloaded")
  → On Timeout → wait 3 s → retry once
  → page.wait_for_timeout(js_settle_time × 1000)
  → return page.content()
```

Each worker gets its own Playwright browser context (`browser.new_context()`), isolating sessions and preventing
`TargetClosedError` across workers.

### JS Detection

After an HTTPX fetch, `_needs_js(html)` scans the response body for `js_indicators` strings. If found, Playwright is
invoked regardless of HTTP success.

### Throttling

`_throttle(netloc)` enforces per-domain politeness:

- Maintains `_domain_next[netloc]` = earliest allowed next-request timestamp.
- Acquires a per-domain `asyncio.Lock` and sleeps if the domain is in cooldown.
- Called once per URL visit, not once per attempt (so HTTPX + Playwright fallback does not double-sleep).

---

## Link Discovery

After parsing, `_enqueue_links()` filters and enqueues discovered hrefs:

1. Elect at most one pagination link for this page (see below) — it's exempt from the rest of this list.
2. Cap non-pagination links at `max_links_per_page[depth]` (or the page's shared `chain_budget` if this page is
   itself part of a pagination chain — see below).
3. Skip if `_is_skippable(url)` (matches `skip_extensions`).
4. Skip if not `_is_gov_domain(url)` (doesn't end in `target_suffixes`).
5. Skip if already in `_visited`.
6. On a non-priority page and no `priority_keywords` match → skip.
7. Enqueue remaining with `depth + 1`.

Seed pages are always treated as priority pages; all their links (up to the cap) are followed regardless of URL content.

---

## Pagination-Aware Crawling

A page can carry one elected "next page" link that follows a separate budget from ordinary link discovery, so a
long results listing doesn't get truncated by `max_links_per_page` after its first page.

### Detection

`_is_pagination_link(url, anchor_text, rel)` is deliberately conservative, to avoid falling into session-URL traps
some `.gov.in` sites use (a "Next" link whose href carries a non-numeric/base64 session param instead of a real page
number):

1. If the URL has a query param matching `pagination.param_signals` (case-insensitive on the param name), its value
   **must** be a plain base-10 integer, or the link is rejected outright — regardless of anchor text or `rel`. A
   blank value (`?page=`) counts as present-but-non-numeric, so it's rejected too. If more than one signal param is
   present, **any** non-numeric value among them rejects the whole URL (fail closed).
2. Only when the URL carries **none** of the configured `param_signals` does it fall back to: `rel="next"`, or the
   anchor text matching `pagination.text_signals` (or being a plain integer itself, e.g. a numbered pager link).

### Election

At most **one** pagination link is chosen per page (`_elect_pagination_target`) — preferring an explicit `rel="next"`
hit, else the first classifier-accepted link in document order. Without this, a numbered pager bar
("1 2 3 4 5 Next Last") would classify every matching link as its own independent pagination chain, multiplying the
amplification bound by however many links the pager shows. Real "next page" progression is one hop per page.

### Chain mechanics

- The elected pagination link bypasses `max_links_per_page` and the priority-keyword filter entirely (a "Next"
  anchor rarely carries a keyword). It spends `page_hops` — a linear counter capped at
  `pagination.max_pagination_pages` — not `depth`.
- Every page in one chain shares one mutable `chain_budget` counter. `pagination.max_chain_children` bounds the
  **total** non-pagination children spawned across the **entire chain**, not per individual page — otherwise an
  N-page chain would re-apply the full per-page cap N times, defeating the point of bounding it.
- `pagination.enabled: false` doesn't disable the classifier itself — the gate lives at the `_enqueue_links()` call
  site, so a disabled config simply never elects a pagination target and every link falls back to ordinary link
  discovery rules.

**Known limitation:** `mark_visited()` fires before `_fetch()` in the worker loop, so a transient fetch failure
partway down a pagination chain permanently strands the rest of that chain for `recrawl_days` — and since chain
continuation depends on parsing the current page, it also aborts the rest of the *current* crawl's discovery down
that chain, not just future recrawls. This is a known, intentionally-deferred interaction, not a bug to work around.

---

## Job Seeding: Domains vs Custom URLs

`POST /api/jobs` seeds a crawl from **either** known `domain_ids` **or** ad-hoc `custom_urls` — exactly one must be
supplied (see [api-reference.md](api-reference.md#crawl-jobs)). This is recorded on the job as `source_type`
(`"domains"` or `"custom_urls"`, [database-schema.md](database-schema.md)).

### Seed snapshots (decoupling leads from the catalog)

For `domains`-sourced jobs, `create_job` calls `db.create_crawl_snapshot(job_id, domain)` for each seed, freezing the
domain's metadata into the `crawl_snapshots` table, and threads the **snapshot id** (not the catalog `domains.id`)
through the engine as the seed id. That id lands on `leads.snapshot_id`, and every lead display/filter/enumeration
reads domain metadata from the snapshot — so a later destructive `domains` refresh (which reassigns `domains.id`)
never corrupts lead-visible data. `get_job_seeds` and the CLI (`python -m portal crawl`) resolve a job's seeds from
these snapshots too. See [database-schema.md](database-schema.md) (`crawl_snapshots`). Custom-URL seeds carry no
domain, so their leads have a null `snapshot_id`.

For `custom_urls`:

- Each URL is trimmed, auto-prefixed with `http://` if it has no scheme, and deduplicated.
- Capped at `crawler.max_custom_urls` (default 50); invalid or empty input is rejected with `422`.
- **`target_suffixes` is not applied** — a caller who supplies explicit URLs has already chosen them deliberately,
  so the crawl runs against an engine config with `target_suffixes: []` for that job only. Every other crawler
  setting (workers, depth, pagination, etc.) is unchanged.
- The raw URLs are stored in the `job_custom_urls` table rather than resolved against `domains`.

---

## Thread Pool Architecture

| Pool         | Threads     | Work                                                                                |
|--------------|-------------|-------------------------------------------------------------------------------------|
| `db_pool`    | 1           | `save_lead()`, `mark_visited()`, `update_job_metrics()`, `increment_job_progress()` |
| `parse_pool` | `cpu_count` | `parser.parse_for_engine()` (BeautifulSoup tree construction + lead extraction)     |

The single-thread DB pool serializes all writes to avoid SQLite WAL contention. The parse pool parallelizes CPU-bound
HTML parsing across all cores without touching shared state.

---

## Metrics Reporter

`_reporter()` is a long-running coroutine that calls `db.update_job_metrics()` every 2 seconds, pushing live queue size,
visit counts, depth, and worker count to the DB. The UI polls `GET /api/jobs/{id}` on the same interval to render the
live status dashboard.

---

## Error Handling

| Scenario                                      | Behaviour                                                  |
|-----------------------------------------------|------------------------------------------------------------|
| `httpx` network error                         | Log warning, return `None`, Playwright fallback if enabled |
| Playwright timeout (first)                    | Wait 3 s, retry once                                       |
| Playwright timeout (second)                   | Log warning, return `None`                                 |
| `TargetClosedError` / `net::ERR`              | Log debug, return `None`                                   |
| Per-URL watchdog exceeded (`per_url_timeout`) | Log warning, `task_done()` the item, continue              |
| Unhandled exception in worker                 | Log error, `task_done()`, continue                         |
| `CancelledError` in worker                    | Re-raise to actually stop the worker                       |

---

## Parser — Lead Extraction

Source: `portal/crawler/parser.py`

`parse_for_engine(html, url, excfg)` is `CrawlerEngine`'s thread-pool entry point. It builds one `BeautifulSoup`
tree, harvests `<a href>` links (including `rel` tokens, used for pagination — see above) before any tag
decomposition happens, then calls `extract_leads()` on the same tree and returns `(leads, raw_links)`.

Extraction is a **6-stage pipes-and-filters pipeline**, entirely config-driven (no regexes or keyword lists
hardcoded in the pipeline itself):

```
extract_candidates → bind_channels → enrich_fields → normalise_spans → score → flatten_emit
```

### Lead Dataclass

```python
@dataclass
class Lead:
    email: str | None = None
    person_name: str | None = None
    designation: str | None = None
    department: str | None = None
    source_url: str = ""
    source_title: str = ""
    context_snippet: str = ""
    entity_kind: str | None = None
    phone: str | None = None
    channel_tag: str | None = None
    confidence_band: str | None = None
    field_provenance: str | None = None
```

### Stage 1 — `extract_candidates`

Harvests `(address, rung, context_node, raw_span, phone)` candidate dicts from four signal sources, all read from
the full DOM **before** `<script>`/`<style>`/`<noscript>` are stripped:

1. **`mailto:`/`tel:` hrefs** (rung `mailto_tel`) — highest precedence.
2. **Microdata** — `itemprop="email"` / `itemprop="telephone"` (rung `microdata`).
3. **Table/card blocks** (rung `table_block`) — for each `<table>` with ≥ 2 rows, locate columns by header keyword
   (`name/officer/official`, `designation/post/rank`, `department/division/ministry`, `email/e-mail/mail`), then
   scan each data row for emails matching the configured regex.
4. **Proximity text scan** (rung `proximity_text`, bounded by `extraction.max_input_chars`) — obfuscation patterns
   are applied to the page text first (`webmaster at nic dot in` → `webmaster@nic.in`), then the email regex runs
   via `re.finditer`. A separate regex also catches *bracketed* obfuscated forms (`[at]`/`[dot]`) for later
   resolution in Stage 4, since those need per-span (not whole-page) de-obfuscation to avoid false positives.

### Stage 2 — `bind_channels`

Groups candidates by email address into **entities**, keeping only the highest-rung candidate per address (rung
precedence: `mailto_tel` > `microdata` > `table_block` > `proximity_text`). Classifies each entity's
`channel_tag`/`entity_kind` from the local-part and domain:

| local-part in `extraction.role_local_parts`? | domain ends in `valid_suffixes`? | `channel_tag`       | `entity_kind` |
|----------------------------------------------|----------------------------------|---------------------|---------------|
| Yes                                          | —                                | `role`              | `org`         |
| No                                           | No                               | `personal-external` | `person`      |
| No                                           | Yes                              | `office`            | `person`      |

Phone-only candidates (no email) are best-effort attached to the nearest entity sharing the same DOM container, or
to every phone-less entity if no container match is found.

### Stage 3 — `enrich_fields`

Adds `person_name`, `designation`, `department` per entity (gated by `extraction.person.enabled`):

- **`table_block` entities:** read from the mapped table columns, falling back to a proximity regex over the row
  text if a column wasn't found (name = title-prefix + capitalized words; designation = first matching
  `designation_keyword` + up to 60 chars).
- **`proximity_text` entities:** the same name/designation regexes run over a ±`proximity_chars` window around the
  email's position in the page text; `context_snippet` is a separate, smaller ±`context_chars` window.
- **`department`:** prefers a table column value, else falls back to a URL-derived guess (first path segment of the
  netloc). A page-level `itemprop="name"` value that disagrees with the URL-derived department is flagged
  internally and degrades the entity's confidence band in Stage 5.

### Stage 4 — `normalise_spans`

Guarded de-obfuscation: resolves the *bracketed* obfuscated candidates carried over from Stage 1 by applying the
configured `obfuscation` pairs to that span only — never a global text rewrite. A span that doesn't resolve to a
valid email, or resolves to an address already captured by a higher-rung candidate, is dropped.

```yaml
obfuscation:
  - ['\s*\[at\]\s*', '@']
  - ['\s*\(at\)\s*', '@']
  - ['\s*\[dot\]\s*', '.']
  - ['\s*\(dot\)\s*', '.']
  - ['\s*\[hyphen\]\s*', '-']
  - ['\s*\(hyphen\)\s*', '-']
```

### Stage 5 — `score`

Assigns `confidence_band` (`HIGH` if the rung is in `extraction.confidence.high_rungs`, else `LOW`) — degraded to
`LOW` if Stage 3 flagged a department mismatch, even for an otherwise-HIGH rung. Also builds a `field_provenance`
JSON blob recording which rung supplied each populated field (`email`, `person_name`, `designation`, `department`,
`phone`).

### Stage 6 — `flatten_emit`

Emits one flat `Lead` per unique email. **The confidence band never drops a lead** — an email with no name is still
a lead; only entities with no email at all are skipped.

### Email Validation

An email is kept only if it passes both:

1. The configured `regex` (default: `[a-zA-Z0-9._%+\-]+@(?:[a-zA-Z0-9\-]+\.)+[a-zA-Z]{2,6}`)
2. Ends with one of `valid_suffixes` (`.gov.in`, `.nic.in`, `.res.in`, `.ac.in`, `.com`)

### Relationship to Lead Scoring

`confidence_band`, `phone`, `person_name`, and `designation` set here feed directly into
`portal/services/lead_scoring.compute_lead_score()` — see [database-schema.md](database-schema.md#leads) and
[configuration.md](configuration.md) for the scoring weights. `channel_tag` values from this parser
(`office`/`personal-external`/`role`) are distinct from the CSV-import sentinel `channel_tag == "manual"`, which
always forces `lead_score` to 0 regardless of any of the above.

---

## Running a Job via CLI

```bash
python -m portal crawl <job_id>
```

This bypasses the API server and runs the crawl synchronously using `asyncio.run()`. Useful for debugging a specific
job. The crawler reads seeds from the DB job record, launches a headless Playwright browser, runs all workers, and
exits.
