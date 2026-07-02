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

| Key                     | Default                               | Description                                             |
|-------------------------|---------------------------------------|---------------------------------------------------------|
| `workers`               | 50                                    | Number of concurrent async worker coroutines            |
| `max_depth`             | 3                                     | Maximum crawl depth from each seed (0 = seed only)      |
| `recrawl_days`          | 30                                    | Skip URLs visited in any job within last N days         |
| `httpx_first`           | true                                  | Try HTTP fetch before launching browser                 |
| `playwright_fallback`   | false                                 | Enable Playwright fallback for JS sites                 |
| `httpx_timeout.connect` | 10                                    | TCP connect timeout (seconds)                           |
| `httpx_timeout.read`    | 30                                    | HTTP read timeout (seconds)                             |
| `playwright_timeout`    | 45                                    | Playwright page load timeout (seconds)                  |
| `js_settle_time`        | 3.0                                   | Extra wait after `domcontentloaded` for JS to execute   |
| `per_url_timeout`       | 100                                   | Hard per-URL watchdog timeout (seconds)                 |
| `request_delay`         | 1.5                                   | Minimum seconds between requests to the same domain     |
| `target_suffixes`       | `.gov.in`, `.nic.in`                  | Only crawl URLs ending in these suffixes                |
| `priority_keywords`     | `contact`, `officer`, `directory`, …  | URLs containing these are crawled first                 |
| `skip_extensions`       | `.pdf`, `.doc`, `.jpg`, …             | URLs with these path extensions are skipped             |
| `js_indicators`         | `<div id="__next"`, …                 | HTML strings that signal JavaScript rendering is needed |
| `max_links_per_page`    | `{0: 100, 1: 50, 2: 40, default: 15}` | Max links to follow per depth level                     |
| `user_agent`            | Chrome/124 Linux                      | User-Agent header sent with all requests                |

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

1. Skip if `_is_skippable(url)` (matches `skip_extensions`).
2. Skip if not `_is_gov_domain(url)` (doesn't end in `target_suffixes`).
3. Skip if already in `_visited`.
4. On a non-priority page and no `priority_keywords` match → skip.
5. Cap at `max_links_per_page[depth]`.
6. Enqueue remaining with `depth + 1`.

Seed pages are always treated as priority pages; all their links (up to the cap) are followed regardless of URL content.

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

`parse_for_engine(html, url, excfg)` is `CrawlerEngine`'s thread-pool entry point (moved here from
`engine.py` — it used to live there only because `run_in_executor` needs a top-level callable). It builds one
`BeautifulSoup` tree, harvests `<a href>` links before any tag decomposition happens, then calls `extract_leads()`
on the same tree and returns `(leads, raw_links)`.

### Lead Dataclass

```python
@dataclass
class Lead:
    email: str | None
    person_name: str | None
    designation: str | None
    department: str | None
    source_url: str
    source_title: str
    context_snippet: str
```

### Two-Pass Extraction

**Pass 1 — Table scanning (high confidence)**

For each `<table>` with ≥ 2 rows:

1. Read header row to locate columns by keyword: `name/officer/official`, `designation/post/rank`,
   `department/division/ministry`, `email/e-mail/mail`.
2. For each data row, find emails matching the configured regex + `valid_suffixes`.
3. Extract name, designation, department from the mapped columns.
4. If column-based name/designation is absent, fall back to proximity regex within the row text.

**Pass 2 — Proximity scan**

1. Strip `<script>`, `<style>`, `<noscript>` tags from the soup (mutates the tree).
2. Normalize obfuscation patterns (`[at]` → `@`, `(dot)` → `.`, etc.).
3. Find all emails matching the regex + valid suffixes using `re.finditer`.
4. For each email, extract a `context_snippet` (±`context_chars` characters).
5. Within a ±`proximity_chars` window, search for:
    - Name: title prefix (`Shri`, `Dr`, `Mr`, etc.) followed by capitalized words.
    - Designation: first matching `designation_keyword` in the window + up to 60 chars.

**Phone extraction is not implemented.** The parser is intentionally limited to emails and personnel. Phone support is
planned for a future release.

### Obfuscation Handling

```yaml
obfuscation:
  - ['\s*\[at\]\s*', '@']
  - ['\s*\(at\)\s*', '@']
  - ['\s+at\s+',     '@']
  - ['\s*\[dot\]\s*', '.']
  - ['\s*\(dot\)\s*', '.']
  - ['\s+dot\s+',    '.']
```

Each pair is `[regex_pattern, replacement]`, applied via `re.sub` before email scanning.

### Email Validation

An email is kept only if it passes both:

1. The configured `regex` (default: `[a-zA-Z0-9._%+\-]+@(?:[a-zA-Z0-9\-]+\.)+[a-zA-Z]{2,6}`)
2. Ends with one of `valid_suffixes` (`.gov.in`, `.nic.in`, `.res.in`, `.ac.in`)

---

## Running a Job via CLI

```bash
python -m portal crawl <job_id>
```

This bypasses the API server and runs the crawl synchronously using `asyncio.run()`. Useful for debugging a specific
job. The crawler reads seeds from the DB job record, launches a headless Playwright browser, runs all workers, and
exits.
