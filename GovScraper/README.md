# GovScraper

GovScraper is a standalone web scraper designed to extract a comprehensive directory of Indian government domains (`.gov.in`, `.nic.in`) directly from the `india.gov.in` Web Directory API.

It operates without needing a browser, Playwright, or dealing with CAPTCHAs, as it interacts directly with the internal Next.js API routes that proxy the backend GraphQL search.

## Features

- **Direct API Interaction**: Scrapes data directly via POST JSON API requests.
- **Zero Bot Detection**: Uses the same origin API routes, mimicking normal application behavior to avoid detection.
- **Data Extraction**: Extracts root domains (`.gov.in`, `.nic.in`).
- **Fallback Mechanism**: Includes an HTML/RSC (React Server Components) stream parsing fallback if the main API returns no domains.
- **Automatic Pagination**: Safely paginates through all categories and entries up to the total counts provided by the API.

## Requirements

- Python 3.x
- `httpx` library

You can install the required dependencies using pip:

```bash
pip install httpx
```

## Usage

GovScraper is now integrated directly into GovCrawler as a module. It uses `runner.py` to programmatically retrieve domains and pass them to the crawler's seeder.

To fetch domains within another Python script:

```python
from GovScraper.runner import run_all_domains

domains = run_all_domains()
print(domains)
```

## Outputs

The module returns a set of unique root domains (`.gov.in`, `.nic.in`) extracted from all categories.

These outputs are perfect for use as seed lists for broader crawling or enumeration tasks.
