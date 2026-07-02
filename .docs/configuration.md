# Configuration Reference

Config lives in `portal/config.yaml` (live, user-editable) and `portal/default_config.yaml` (shipped defaults, read-only
in the compiled `.exe`).

On first run, `default_config.yaml` is copied to `config.yaml` if it does not exist. Changes to `config.yaml` are picked
up by the Settings page in the UI (`POST /api/config`) or by restarting the server.

> **Crawler settings** (workers, depth, timeouts, keywords) take effect on the **next** job created after saving.
> In-flight jobs continue with their original settings.

---

## Full Default Configuration

```yaml
# ── Database ───────────────────────────────────────────────────────────────────
database:
  uri: sqlite:///portal/data/govcrawler.db
  # PostgreSQL alternative:
  # uri: postgresql://user:password@localhost:5432/govcrawler

# ── API Server ─────────────────────────────────────────────────────────────────
api:
  host: 0.0.0.0    # bind address; GUI opens browser on 127.0.0.1
  port: 8000

# ── GovScraper (domain import from india.gov.in) ───────────────────────────────
scraper:
  category_filter: ''    # e.g. 'ug' — import only this category; empty = all
  org_type_filter: ''    # e.g. 'dept' — filter by org type; empty = all

# ── Crawler Engine ─────────────────────────────────────────────────────────────
crawler:
  workers: 50            # concurrent async worker coroutines
  max_depth: 3           # 0 = seed page only; 3 = seed + 3 levels deep
  recrawl_days: 30       # skip URLs visited in any job within last N days

  # Fetch strategy
  httpx_first: true      # try plain HTTP before launching browser
  playwright_fallback: false  # enable Playwright for JS-heavy sites

  # Timeouts
  httpx_timeout:
    connect: 10          # TCP connect timeout (seconds)
    read: 30             # HTTP read timeout (seconds)
  playwright_timeout: 45 # page.goto() timeout (seconds)
  js_settle_time: 3.0    # extra wait after domcontentloaded for JS (seconds)
  per_url_timeout: 100   # hard watchdog per URL — kills stalled workers (seconds)

  # Politeness
  request_delay: 1.5     # minimum seconds between requests to the same netloc

  # Filtering
  target_suffixes:
    - .gov.in
    - .nic.in
  # Only crawl URLs whose netloc ends in one of these.
  # Empty list = accept all domains (not recommended).

  priority_keywords:
    - contact
    - officer
    - directory
    - whos-who
    - who-is-who
    - staff
    - personnel
    - secretariat
    - about-us
    - division
    - minister
    - committee
    - administration
    - team
    - tender
    - procurement
    - telephone
    - tele-directory
    - phone-directory
    - email
  # URLs containing any of these are assigned priority 0 (crawled first).
  # Empty list = no prioritization (all URLs treated equally).

  skip_extensions:
    - .pdf
    - .doc
    - .docx
    - .xls
    - .xlsx
    - .ppt
    - .pptx
    - .zip
    - .rar
    - .7z
    - .tar
    - .gz
    - .jpg
    - .jpeg
    - .png
    - .gif
    - .svg
    - .ico
    - .mp4
    - .mp3
    - .avi
    - .mov
  # URLs whose path ends in these extensions are never enqueued.

  js_indicators:
    - '<div id="__next"'
    - '<div id="root"'
    - 'Please enable JavaScript'
    - 'You need to enable JavaScript'
    - 'This page requires JavaScript'
  # If any indicator appears in the HTTPX response body, Playwright is used instead.

  user_agent: >-
    Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36
    (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36

  # Max links to follow per page, keyed by depth level.
  # Depth 0 = seed page, depth 1 = first hop, etc.
  max_links_per_page:
    0: 100
    1: 50
    2: 40
    default: 15   # used for depths not explicitly listed

# ── Extraction ─────────────────────────────────────────────────────────────────
extraction:
  email:
    enabled: true
    regex: '[a-zA-Z0-9._%+\-]+@(?:[a-zA-Z0-9\-]+\.)+[a-zA-Z]{2,6}'
    valid_suffixes:
      - .gov.in
      - .nic.in
      - .res.in
      - .ac.in
    context_chars: 200        # snippet length around each email (chars each side)
    obfuscation:
      - ['\s*\[at\]\s*', '@']
      - ['\s*\(at\)\s*', '@']
      - ['\s+at\s+',     '@']
      - ['\s*\[dot\]\s*', '.']
      - ['\s*\(dot\)\s*', '.']
      - ['\s+dot\s+',    '.']
    # Each pair: [regex_pattern, replacement]. Applied before email scanning.

  person:
    enabled: true
    title_prefixes:
      - Shri
      - Smt
      - Dr
      - Mr
      - Mrs
      - Ms
      - Prof
      - Sh
      - Shrimati
      - Km
    # Pattern: <prefix> <Capitalized Words> (1–4 words) near the email

    designation_keywords:
      - Secretary
      - Director
      - Commissioner
      - Collector
      - Superintendent
      - Inspector
      - Officer
      - Manager
      - Chairman
      - President
      - Minister
      - Deputy
      - Additional
      - Principal
      - Chief
      - Joint
      - Under Secretary
      - IAS
      - IPS
      - IFS
      - IRS
    # First matching keyword + next 60 chars used as designation

    proximity_chars: 300
    # Window (chars on each side of the email) searched for name + designation
```

---

## Key Decisions and Trade-offs

### `workers`

Higher values increase throughput but also server load on crawled sites. 50 is a good default for a local machine.
Reduce to 10–20 for slow machines or rate-sensitive targets.

### `max_depth`

Depth 0 = only the seed URL (fastest, but may miss contact pages). Depth 3 = enough to reach most `/contact`, `/about`,
`/staff` pages two hops from the home page. Deeper crawls grow exponentially in URL count.

### `recrawl_days`

Set to 0 to always re-crawl everything (useful for development). A higher value is more conservative but prevents
re-extracting the same leads.

### `httpx_first` + `playwright_fallback`

- `httpx_first: true, playwright_fallback: false` — Fastest; skips all JS sites silently.
- `httpx_first: true, playwright_fallback: true` — Recommended for production; handles JS after plain HTML fails.
- `httpx_first: false` — Not recommended; launches a browser page for every URL.

### `max_links_per_page`

Tighter limits reduce crawl scope and duration. Seed pages (depth 0) are typically the home page with many nav links,
hence a higher limit of 100. By depth 2, you're usually on specific subpages with fewer relevant links.

### `valid_suffixes` (extraction)

Extend this list if you want to capture emails from `.edu.in`, `.ac.in`, `.res.in`, or other government-adjacent domains
that are cross-linked from `.gov.in` pages.

---

## Editing via the Settings UI

The Settings page (`/settings`) renders all the above fields in an editable form. Multiline fields (keyword lists,
extensions) are newline-separated text areas. On save, `POST /api/config` (`portal/api/config.py`) writes the new
`config.yaml` and updates the in-memory config dict shared via `portal/api/deps.py`. The browser reloads the form to
confirm the saved values.

---

## PostgreSQL Setup

1. Install the driver:
   ```bash
   pip install psycopg2-binary
   ```

2. Create the database and user in PostgreSQL.

3. Update `portal/config.yaml`:
   ```yaml
   database:
     uri: postgresql://govcrawler_user:password@localhost:5432/govcrawler
   ```

4. Run Alembic migrations:
   ```bash
   alembic upgrade head
   ```

SQLAlchemy will use PostgreSQL instead of SQLite transparently. The WAL pragmas applied for SQLite are no-ops on
PostgreSQL.
