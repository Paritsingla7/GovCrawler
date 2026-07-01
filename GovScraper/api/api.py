"""
API interactions for the india.gov.in Web Directory.
"""

import logging
import httpx

from .config import WEB_DIR_API, PAGE_SIZE

log = logging.getLogger(__name__)


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
    return results


def get_organization_types(client: httpx.Client, category_code: str) -> list[dict]:
    """
    Fetch the available organization types for a given category.
    Returns list of dicts with title, count, and organization_type codes.
    """
    r = client.post(
        WEB_DIR_API,
        json={"dataval": {
            "clientvalue": "client",
            "mustvalue": category_code,
            "querytype": "organizationtypewithCategory"
        }},
        timeout=15,
    )
    r.raise_for_status()
    results = (
        r.json()
        .get("resultdata", {})
        .get("data", {})
        .get("getIgodOrganizationByCategory", {})
        .get("results", [])
    )
    return results


def get_entries_for_category(
        client: httpx.Client,
        category_code: str,
        org_type_code: str = None,
) -> list[dict]:
    """
    Paginate through all entries for a given category code (e.g. 'ug', 'sg').
    Optionally filter by organization type.
    Returns list of raw entry dicts from the API.
    """
    all_entries: list[dict] = []
    page = 1

    mustvalue = [{"fieldName": "category", "fieldValue": category_code}]
    if org_type_code:
        mustvalue.append({"fieldName": "organization_type", "fieldValue": org_type_code})

    while True:
        r = client.post(
            WEB_DIR_API,
            json={"dataval": {
                "clientvalue": "client",
                "mustvalue": mustvalue,
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
        total = payload.get("total", 0)

        all_entries.extend(results)
        fetched = len(all_entries)

        log.debug(
            f"[{category_code}] page {page} -> {len(results)} entries ({fetched}/{total})"
        )

        if not results or fetched >= total:
            break
        page += 1

    return all_entries
