"""
Standalone — india.gov.in Web Directory API scraper.
NO browser, NO Playwright, NO captcha.

How it works:
  The india.gov.in directory pages load data via a POST JSON API at:
    /directory/web-directory/api
    /directory/contact-directory/api

  These are internal Next.js API routes that proxy a GraphQL/search backend.
  Being same-origin API routes, they have no bot detection. A plain httpx
  POST with the correct body structure returns all ministry data as JSON.

  The API was discovered by:
    1. Fetching the directory page's compiled JS chunk from the CDN
       (/_next/static/chunks/app/directory/web-directory/union-government/
        page-327a999fcd132631.js) — static files, zero bot detection
    2. Grepping the minified JS for the fetch/axios call pattern
    3. Decoding the `mustvalue` type from the GraphQL error response

API structure (confirmed working):
  POST /directory/web-directory/api
  Body: {"dataval": {"querytype": "Webdirectorycategorywithcounts"}}
  → Returns categories: sg (State), ug (Union), jud, leg, apx, dist, int ...

  POST /directory/web-directory/api
  Body: {"dataval": {
    "clientvalue": "client",
    "mustvalue": [{"fieldName": "category", "fieldValue": "<cat_code>"}],
    "shouldvalue": [],
    "pageno": <int>,
    "pageSize": <int>,
    "querytype": "WebdirectoryCategorydetalsList"
  }}
  → Returns {total, results: [{url, url_1, title, ...}]}
  url   = ministry main website
  url_1 = ministry contact page (direct seed value)

Output:
    india_gov_domains.txt  — one root domain per line
    india_gov_domains.json — JSON array (drop into seeder hardcoded list)
    india_gov_contact_urls.json — JSON array of url_1 contact pages (best seeds)

Usage:
    pip install httpx
    python3 india_gov_next_scraper.py
"""

import re
import json
import logging
from urllib.parse import urlparse

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────

WEB_DIR_API    = "https://www.india.gov.in/directory/web-directory/api"
CONTACT_DIR_API = "https://www.india.gov.in/directory/contact-directory/api"

TARGET_SUFFIXES = (".gov.in", ".nic.in")

# Page size for paginated queries — 100 is the safe maximum we've observed
PAGE_SIZE = 100

OUT_TXT          = "india_gov_domains.txt"
OUT_JSON         = "india_gov_domains.json"
OUT_CONTACT_JSON = "india_gov_contact_urls.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Referer": "https://www.india.gov.in/directory/web-directory",
    "Origin": "https://www.india.gov.in",
}


# ── Helpers ────────────────────────────────────────────────────────────────────


def _is_target_domain(url: str) -> bool:
    if not url or not url.startswith("http"):
        return False
    netloc = urlparse(url).netloc
    return any(netloc.endswith(s) for s in TARGET_SUFFIXES)


def _root_domain(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


# ── Web Directory API ──────────────────────────────────────────────────────────


def get_categories(client: httpx.Client) -> list[dict]:
    """
    Fetch all directory categories and their entry counts.
    Returns list of {category, count, title} dicts.
    """
    r = client.post(
        WEB_DIR_API,
        json={"dataval": {"querytype": "Webdirectorycategorywithcounts"}},
        timeout=15,
    )
    r.raise_for_status()
    results = (
        r.json()
        .get("resultdata", {})
        .get("data", {})
        .get("getIgodCategoryWithCount", {})
        .get("results", [])
    )
    log.info(f"Found {len(results)} categories:")
    for cat in results:
        log.info(f"  [{cat['category']}] {cat['title']} — {cat['count']} entries")
    return results


def get_entries_for_category(
    client: httpx.Client,
    category_code: str,
    category_title: str,
) -> list[dict]:
    """
    Paginate through all entries for a given category code (e.g. 'ug', 'sg').
    Returns list of raw entry dicts from the API.
    """
    all_entries: list[dict] = []
    page = 1

    while True:
        r = client.post(
            WEB_DIR_API,
            json={"dataval": {
                "clientvalue": "client",
                "mustvalue": [{"fieldName": "category", "fieldValue": category_code}],
                "shouldvalue": [],
                "pageno": page,
                "pageSize": PAGE_SIZE,
                "querytype": "WebdirectoryCategorydetalsList",
            }},
            timeout=20,
        )
        r.raise_for_status()

        payload = (
            r.json()
            .get("resultdata", {})
            .get("data", {})
            .get("getIgodWebDirectoryByFilters", {})
        )
        results = payload.get("results") or []
        total   = payload.get("total", 0)

        all_entries.extend(results)

        fetched = len(all_entries)
        log.info(
            f"  [{category_code}] {category_title}: "
            f"page {page} → {len(results)} entries "
            f"({fetched}/{total} fetched)"
        )

        if not results or fetched >= total:
            break
        page += 1

    return all_entries


# ── Extraction ─────────────────────────────────────────────────────────────────


def extract_from_entries(
    entries: list[dict],
) -> tuple[set[str], set[str]]:
    """
    From raw API entry dicts, return:
      root_domains   — {scheme}://{netloc} for gov.in/nic.in URLs
      contact_urls   — url_1 values that point directly to contact pages
    """
    root_domains: set[str] = set()
    contact_urls: set[str] = set()

    for entry in entries:
        for field in ("url", "url_1"):
            url = entry.get(field) or ""
            if _is_target_domain(url):
                root_domains.add(_root_domain(url))
                if field == "url_1":
                    contact_urls.add(url)

    return root_domains, contact_urls


# ── Fallback: RSC payload ──────────────────────────────────────────────────────


def fallback_rsc_scrape(client: httpx.Client) -> set[str]:
    """
    Last-resort: fetch directory sub-pages and extract any gov.in URLs from
    the HTML / RSC stream. Catches entries the main API might miss.
    """
    log.info("Running RSC/HTML fallback scrape...")
    domains: set[str] = set()
    sub_pages = [
        "/directory/web-directory/union-government",
        "/directory/web-directory/apex-bodies",
        "/directory/web-directory/state-uts",
        "/directory/contact-directory/central",
        "/directory/contact-directory/state-uts",
    ]
    html_headers = {
        k: v for k, v in HEADERS.items()
        if k not in ("Content-Type",)
    }
    html_headers["Accept"] = "text/html,application/xhtml+xml"

    for path in sub_pages:
        for use_rsc in (True, False):
            h = dict(html_headers)
            if use_rsc:
                h["RSC"] = "1"
                h["Next-Url"] = path
                h["Accept"] = "text/x-component"
            try:
                r = client.get(
                    f"https://www.india.gov.in{path}",
                    headers=h, timeout=20,
                )
                for m in re.findall(r'https?://[\w.-]+\.(?:gov|nic)\.in', r.text):
                    parsed = urlparse(m)
                    if any(parsed.netloc.endswith(s) for s in TARGET_SUFFIXES):
                        domains.add(f"{parsed.scheme}://{parsed.netloc}")
            except Exception as e:
                log.debug(f"  {'RSC' if use_rsc else 'HTML'} {path} failed: {e}")

    log.info(f"RSC/HTML fallback found {len(domains)} domains")
    return domains


# ── Main ───────────────────────────────────────────────────────────────────────


def scrape() -> list[str]:
    all_root_domains: set[str] = set()
    all_contact_urls: set[str] = set()

    with httpx.Client(
        headers=HEADERS,
        follow_redirects=True,
        timeout=httpx.Timeout(20.0),
    ) as client:

        # Step 1: get categories
        log.info("=== Step 1: Fetching categories ===")
        try:
            categories = get_categories(client)
        except Exception as e:
            log.error(f"Failed to fetch categories: {e}")
            categories = []

        # Step 2: paginate through each category
        log.info("=== Step 2: Fetching all directory entries ===")
        for cat in categories:
            code  = cat["category"]
            title = cat["title"]
            try:
                entries = get_entries_for_category(client, code, title)
                roots, contacts = extract_from_entries(entries)
                all_root_domains.update(roots)
                all_contact_urls.update(contacts)
                log.info(
                    f"  [{code}] done — "
                    f"{len(roots)} gov domains, {len(contacts)} contact URLs"
                )
            except Exception as e:
                log.error(f"  [{code}] {title} failed: {e}")

        # Step 3: fallback if API yielded nothing
        if not all_root_domains:
            log.warning("Main API returned 0 domains — running fallback scrape")
            all_root_domains.update(fallback_rsc_scrape(client))

    # Write outputs
    sorted_domains      = sorted(all_root_domains)
    sorted_contact_urls = sorted(all_contact_urls)

    with open(OUT_TXT, "w") as f:
        f.write("\n".join(sorted_domains) + "\n")

    with open(OUT_JSON, "w") as f:
        json.dump(sorted_domains, f, indent=2)

    with open(OUT_CONTACT_JSON, "w") as f:
        json.dump(sorted_contact_urls, f, indent=2)

    log.info(
        f"Done. {len(sorted_domains)} unique gov domains → {OUT_TXT} / {OUT_JSON}"
    )
    log.info(
        f"      {len(sorted_contact_urls)} contact URLs → {OUT_CONTACT_JSON}"
    )
    return sorted_domains


if __name__ == "__main__":
    results = scrape()
    print(f"\n=== Results: {len(results)} unique gov.in / nic.in domains ===")
    for d in results[:20]:
        print(f"  {d}")
    if len(results) > 20:
        print(f"  … and {len(results) - 20} more (see {OUT_TXT})")
