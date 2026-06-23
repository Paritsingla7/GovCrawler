"""
Configuration constants for GovCrawler.
"""

WEB_DIR_API = "https://www.india.gov.in/directory/web-directory/api"

TARGET_SUFFIXES = (".gov.in", ".nic.in")

# Page size for paginated queries — 100 is the safe maximum we've observed
PAGE_SIZE = 100

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

OUT_JSON = "gov_domains.json"
