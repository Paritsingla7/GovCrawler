"""
Data extraction and URL processing for GovCrawler.
"""

import re
import logging
from urllib.parse import urlparse
import httpx

from .config import TARGET_SUFFIXES, HEADERS

log = logging.getLogger(__name__)


def _is_target_domain(url: str) -> bool:
    if not url or not url.startswith("http"):
        return False
    netloc = urlparse(url).netloc
    return any(netloc.endswith(s) for s in TARGET_SUFFIXES)


def _root_domain(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def extract_from_entries(entries: list[dict]) -> dict[str, dict[str, set[str]]]:
    """
    From raw API entry dicts, return a nested dictionary grouping root domains
    by their state name and then organization_type code.
    { state_name: { org_type_code: set(urls) } }
    """
    grouped_domains: dict[str, dict[str, set[str]]] = {}

    for entry in entries:
        url = entry.get("url") or ""
        if _is_target_domain(url):
            state = entry.get("stateName")
            state_name = state if state else "National / Unknown"
            org_code = entry.get("organization_type") or "UNKNOWN"

            if state_name not in grouped_domains:
                grouped_domains[state_name] = {}
            if org_code not in grouped_domains[state_name]:
                grouped_domains[state_name][org_code] = set()

            grouped_domains[state_name][org_code].add(_root_domain(url))

    return grouped_domains
