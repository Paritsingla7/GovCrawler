"""
Data extraction and URL processing for GovCrawler.
"""

import logging
from urllib.parse import urlparse

from .config import TARGET_SUFFIXES

log = logging.getLogger(__name__)


def _is_target_domain(url: str) -> bool:
    if not url or not url.startswith("http"):
        return False
    netloc = urlparse(url).netloc
    return any(netloc.endswith(s) for s in TARGET_SUFFIXES)


def _root_domain(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def extract_from_entries(entries: list[dict]) -> dict[str, dict[str, list[dict]]]:
    """
    From raw API entry dicts, return a nested dictionary grouping full entry
    records by state name and then organization_type code. Entries without a
    valid target-domain URL are kept with url=None rather than dropped, so no
    organization is silently lost — a URL can be filled in later.
    { state_name: { org_type_code: [ {title, url, contact_url, external_id}, ... ] } }
    """
    grouped: dict[str, dict[str, dict[str, dict]]] = {}

    for entry in entries:
        raw_main = entry.get("url") or ""
        raw_contact = entry.get("url_1") or ""
        external_id = entry.get("npi_sanitized_id") or None

        if _is_target_domain(raw_main):
            url = _root_domain(raw_main)
            contact_url = raw_contact if _is_target_domain(raw_contact) else None
        elif _is_target_domain(raw_contact):
            url = _root_domain(raw_contact)
            contact_url = None
        else:
            url = None
            contact_url = None

        state_name = entry.get("stateName") or "National / Unknown"
        org_code = entry.get("organization_type") or "UNKNOWN"
        # external_id is the API's stable per-entry id; fall back to a
        # composite key on the rare entry that lacks one, so it isn't
        # collapsed together with unrelated no-url entries.
        key = external_id or f"{entry.get('title', '')}|{url or ''}"

        by_org = grouped.setdefault(state_name, {}).setdefault(org_code, {})
        by_org[key] = {
            "title": entry.get("title") or "",
            "url": url,
            "contact_url": contact_url,
            "external_id": external_id,
        }

    return {
        state: {org: list(records.values()) for org, records in by_org.items()} for state, by_org in grouped.items()
    }
