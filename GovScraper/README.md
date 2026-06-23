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

Run the scraper directly:

```bash
python main.py
```

## Outputs

The script will generate a single file in the current directory upon successful execution:

- `gov_domains.json`: A nested JSON object of the root domains grouped by category, state, and organization type.

```json
{
  "State / UT Government": {
    "Gujarat": {
      "Statutory / Autonomous Bodies": [
        "https://example.gov.in"
      ]
    }
  }
}
```

These outputs are perfect for use as seed lists for broader crawling or enumeration tasks.
