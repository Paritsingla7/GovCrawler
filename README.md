# GovCrawler

An async web crawler for extracting email leads from Indian government websites (`.gov.in` / `.nic.in`). Built on
Playwright with concurrent workers, per-domain rate limiting, and a post-crawl classification pipeline.

## Features

- **Concurrent async crawling** — configurable worker pool with per-domain rate limiting (one request at a time per
  domain)
- **Dynamic seed generation** — completely automates discovering `.gov.in` domains by fetching the entire india.gov.in web directory programmatically via `GovScraper`
- **Smart link filtering** — from non-contact pages, only follows links matching contact / officer / tender keywords;
  prevents data and stats pages from flooding the queue
- **Depth-tapered link budget** — depth-0 seeds get 80 links, depth-1 gets 26, depth-2+ gets 15; avoids crawl sprawl
  while still following paginated officer directories
- **Cross-domain discovery** — follows `.gov.in` / `.nic.in` links across ministry boundaries, auto-discovering new
  domains without manual seeding
- **Email extraction** — normalises obfuscated addresses (`[at]`, `[dot]`, Unicode variants) before applying regex;
  filters to government suffixes only
- **Centralized database support** — uses SQLAlchemy (supports SQLite, Postgres, MySQL); enforces deduplication and intelligent recrawl intervals based on a `last_hit` threshold

## Project Structure

```text
GovCrawler/
├── main.py          # Entry point — arg parsing, Playwright lifecycle, CSV export
├── config.yaml      # All tunable parameters
├── requirements.txt
├── GovScraper/      # Directory API scraper module
│   └── runner.py    # Fetches all gov domains directly from india.gov.in
└── src/
    ├── crawler.py   # Worker pool, link queuing, smart filtering, depth taper
    ├── seeder.py    # Dynamic seed generation via GovScraper
    ├── parser.py    # Email extraction and normalisation
    └── storage.py   # SQLAlchemy DB wrapper — visited URLs, recrawl tracking, leads, CSV export
```

## Setup

**Requirements:** Python 3.11+

```bash
# 1. Clone and enter the repo
git clone https://github.com/Jaguar000212/GovCrawler.git
cd GovCrawler

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Install Playwright browser (Chromium only)
playwright install chromium
```

## Configuration

All parameters live in `config.yaml`. Key settings:

| Parameter             | Default | Description                                                                  |
|-----------------------|---------|------------------------------------------------------------------------------|
| `max_depth`           | `4`     | Crawl depth. Seeds are depth 0; each link followed increments depth          |
| `max_links_per_page`  | `80`    | Link budget at depth 0; tapered automatically at deeper levels               |
| `num_workers`         | `55`    | Concurrent Playwright workers. Per-domain semaphore keeps rate limiting safe |
| `page_timeout`        | `30`    | Seconds before a page navigation is retried once, then abandoned             |
| `database.uri`            | `sqlite:///central_crawler.db` | SQLAlchemy connection string (e.g. `postgresql://user:pass@host/db`) |
| `crawler.recrawl_days`    | `30`    | Skips URLs if they were already crawled within this many days |
| `scraper.category_filter` | `""`    | Optional filter to only scrape a specific category (e.g. `ug`) |
| `scraper.org_type_filter` | `""`    | Optional filter to only scrape a specific organization code |

## Running

```bash
# Standard run — uses all config.yaml defaults
python3 main.py

# Override specific settings from the command line
python3 main.py --workers 80 --max_depth 4

# Full option list
python3 main.py --help
```

The crawler logs to both stdout and `crawler.log`. Press `Ctrl+C` to stop gracefully — leads collected so far are saved
to `leads.csv`.

## Output

**`leads.csv`** — raw leads, written at the end of every run:

| Column                     | Description                                         |
|----------------------------|-----------------------------------------------------|
| `Email`                    | Extracted email address                             |
| `Source URL`               | Page where it was found                             |
| `Page Title`               | HTML title of the source page                       |
| `Context/Surrounding Text` | ~100 chars around the email for manual verification |
| `Category / Ministry`      | Real-time category assigned from the web directory  |
| `State`                    | State associated with the domain                    |
| `Organization Type`        | Type of organization (e.g. Statutory Body)          |
| `Scraped At`               | ISO timestamp                                       |

**Centralized Database (e.g., `central_crawler.db`)** — managed via SQLAlchemy. Leads accumulate across runs; visited URLs are deduplicated and evaluated against the `recrawl_days` threshold so the same page isn't needlessly crawled.

