"""
Standalone script — india.gov.in directory scraper using undetected-chromedriver.
NOT integrated into the main crawler. Delete this file if the approach doesn't work.

Usage:
    pip install undetected-chromedriver
    python3 uc_directory_scraper.py

Output:
    india_gov_domains.txt  — one domain per line
    india_gov_domains.json — JSON array (drop straight into seeder hardcoded list)

Why this exists:
    india.gov.in uses a captcha-gated XHR API to load ministry links.
    Playwright-stealth doesn't patch the CDP socket, so captcha scores it low
    and the API returns empty data. undetected-chromedriver patches the Chrome
    binary itself, which tends to get a higher captcha score and makes the API
    respond with real data.
"""

import re
import time
import json
import logging
from urllib.parse import urlparse

# pip install undetected-chromedriver
import undetected_chromedriver as uc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# --- Config -----------------------------------------------------------

DIRECTORY_PAGES = [
    "https://www.india.gov.in/directory/web-directory",
    "https://www.india.gov.in/directory/whos-who",
    "https://www.india.gov.in/directory/contact-directory",
]

TARGET_DOMAINS = (".gov.in", ".nic.in")

# Seconds to wait after page load for React + XHR to fully render.
# Increase to 20 if you're seeing 0 results.
XHR_WAIT_SECONDS = 15

# Set True to run headless (less detectable by human eye, more detectable
# by captcha). Start with False — only flip if you need unattended runs.
HEADLESS = False

OUT_TXT  = "india_gov_domains.txt"
OUT_JSON = "india_gov_domains.json"

# ----------------------------------------------------------------------


def extract_gov_domains(html: str) -> set[str]:
    domains: set[str] = set()
    for match in re.findall(r'https?://[\w.-]+\.(?:gov|nic)\.in', html):
        parsed = urlparse(match)
        if any(parsed.netloc.endswith(d) for d in TARGET_DOMAINS):
            domains.add(f"{parsed.scheme}://{parsed.netloc}")
    return domains


def scrape() -> list[str]:
    log.info("Launching undetected-chromedriver...")

    options = uc.ChromeOptions()
    if HEADLESS:
        # --headless=new is Chrome's modern headless mode — slightly less
        # detectable than the old --headless flag but still worse than headed.
        options.add_argument("--headless=new")

    driver = uc.Chrome(options=options)
    all_domains: set[str] = set()

    try:
        for url in DIRECTORY_PAGES:
            log.info(f"Navigating to: {url}")
            driver.get(url)

            log.info(f"Waiting {XHR_WAIT_SECONDS}s for React + XHR to complete...")
            time.sleep(XHR_WAIT_SECONDS)

            html = driver.page_source
            found = extract_gov_domains(html)

            log.info(f"  {len(found)} domains extracted from {url}")
            all_domains.update(found)

            # Polite delay between pages
            time.sleep(3)

    finally:
        driver.quit()

    sorted_domains = sorted(all_domains)

    with open(OUT_TXT, "w") as f:
        f.write("\n".join(sorted_domains) + "\n")

    with open(OUT_JSON, "w") as f:
        json.dump(sorted_domains, f, indent=2)

    log.info(f"Done. {len(sorted_domains)} unique domains saved to {OUT_TXT} and {OUT_JSON}")
    return sorted_domains


if __name__ == "__main__":
    results = scrape()
    print(f"\n=== Results ({len(results)} domains) ===")
    for d in results:
        print(d)
