# GovScraper

GovScraper is a standalone web scraper designed to extract a comprehensive directory of Indian government domains (
`.gov.in`, `.nic.in`) directly from the `india.gov.in` Web Directory API.

It operates without needing a browser, Playwright, or dealing with CAPTCHAs, as it interacts directly with the internal
Next.js API routes that proxy the backend GraphQL search.

## Features

- **Direct API Interaction**: Scrapes data directly via POST JSON API requests.
- **Zero Bot Detection**: Uses the same origin API routes, mimicking normal application behavior to avoid detection.
- **Data Extraction**: Extracts root domains (`.gov.in`, `.nic.in`).
- **Fallback Mechanism**: Includes an HTML/RSC (React Server Components) stream parsing fallback if the main API returns
  no domains.
- **Automatic Pagination**: Safely paginates through all categories and entries up to the total counts provided by the
  API.

## Requirements

- Python 3.x
- `httpx` library

You can install the required dependencies using pip:

```bash
pip install httpx
```

## Usage

GovScraper runs standalone — it has no dependency on the portal app or its database.

### CLI: generate a `gov_domains.json`

Run `runner.py` directly from inside the `GovScraper/` directory to scrape the full directory and write a
`gov_domains.json` file in the exact format the portal's `python -m portal import-json` expects:

```bash
cd GovScraper
python runner.py                       # writes ./gov_domains.json
python runner.py ../gov_domains.json   # custom output path
python runner.py --category ug         # only Union Government
python runner.py --org-type <code>     # only a specific organization type
```

Move (or point `import-json` at) the resulting file, then seed the portal's database with zero further API calls:

```bash
python -m portal import-json gov_domains.json
```

### Library usage

To fetch domains within another Python script (also run with `GovScraper/` as the working directory, since its
modules use bare imports):

```python
from runner import run_all_domains, build_gov_domains_json

# Optional config for filtering
config = {
    "scraper": {
        "category_filter": "ug" # Only fetch Union Government
    }
}

domains_metadata = run_all_domains(config)   # flat dict, keyed by external_id
gov_domains = build_gov_domains_json(config) # nested dict, ready for gov_domains.json
```

## Outputs

The module returns a dictionary keyed by each organization's stable `external_id` (not by URL — many organizations in
the directory have no listed URL at all). Each value carries the full record: `title`, `url` (`None` if the
organization has none), `contact_url`, `category`, `state`, and `org_type`.

```python
{
    "K9LWHHUBGGphvn7wG-2q": {
        "title": "...", "url": "https://example.gov.in", "contact_url": None,
        "category": "Union Government", "state": "Delhi", "org_type": "Statutory Body",
    },
    "_9KpHHUBGGphvn7w1-XY": {
        "title": "Agrinnovate India", "url": None, "contact_url": None,
        "category": "Union Government", "state": None, "org_type": "Statutory Body",
    },
    ...
}
```

Organizations with no URL are kept (not dropped) so nothing is lost — the portal imports them with `main_url=None`
and marks them "not crawlable" until a URL is filled in manually.

These outputs are perfect for seeding the GovCrawler, which saves the metadata for real-time lead classification.
