"""
Imports the india.gov.in Web Directory into the portal database.
Reads everything from config — no hardcoded values.
"""

import logging
from urllib.parse import urlparse

import httpx

from ..db.database import Database

log = logging.getLogger(__name__)

# Shared progress dict — polled by the FastAPI /api/import/status endpoint
import_status: dict = {
    "running": False,
    "total_categories": 0,
    "done_categories": 0,
    "total_entries": 0,
    "inserted": 0,
    "error": None,
}


def _is_target(url: str, suffixes: list[str]) -> bool:
    if not url or not url.startswith("http"):
        return False
    netloc = urlparse(url).netloc
    return any(netloc.endswith(s) for s in suffixes)


def _root_domain(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _fetch_categories(client: httpx.Client, cfg: dict) -> list[dict]:
    endpoint = cfg["base_url"] + cfg["web_dir_endpoint"]
    r = client.post(
        endpoint,
        json={"dataval": {"querytype": cfg["querytypes"]["categories"]}},
        timeout=cfg["timeout_seconds"],
    )
    r.raise_for_status()
    return (
        r.json()
        .get("resultdata", {})
        .get("data", {})
        .get("getIgodCategoryWithCount", {})
        .get("results", [])
    )


def _fetch_entries(client: httpx.Client, cfg: dict, category_code: str,
                   page: int) -> tuple[list[dict], int]:
    endpoint = cfg["base_url"] + cfg["web_dir_endpoint"]
    r = client.post(
        endpoint,
        json={"dataval": {
            "clientvalue": "client",
            "mustvalue": [{"fieldName": "category", "fieldValue": category_code}],
            "shouldvalue": [],
            "pageno": page,
            "pageSize": cfg["page_size"],
            "querytype": cfg["querytypes"]["entries"],
        }},
        timeout=cfg["timeout_seconds"],
    )
    r.raise_for_status()
    payload = (
        r.json()
        .get("resultdata", {})
        .get("data", {})
        .get("getIgodWebDirectoryByFilters", {})
    )
    return payload.get("results") or [], payload.get("total", 0)


def import_all(db: Database, config: dict):
    """
    Full blocking import. Runs in a thread via asyncio.to_thread() from the API layer.
    Clears existing domains and re-imports everything fresh.
    """
    global import_status
    cfg = config["scraper"]
    suffixes = cfg["target_suffixes"]

    import_status.update({
        "running": True,
        "total_categories": 0,
        "done_categories": 0,
        "total_entries": 0,
        "inserted": 0,
        "error": None,
    })

    try:
        with httpx.Client(headers=cfg["headers"], follow_redirects=True) as client:
            # Step 1: categories
            log.info("Fetching categories from india.gov.in API...")
            categories = _fetch_categories(client, cfg)
            import_status["total_categories"] = len(categories)
            log.info(f"Found {len(categories)} categories")

            # Clear old data before fresh import
            db.clear_domains()

            # Step 2: paginate each category
            for cat in categories:
                code = cat["category"]
                title = cat.get("title", code)
                api_total = cat.get("count", 0)
                import_status["total_entries"] += api_total

                log.info(f"  [{code}] {title} — {api_total} entries")
                page = 1
                fetched = 0

                while True:
                    results, total = _fetch_entries(client, cfg, code, page)
                    if not results:
                        break

                    for entry in results:
                        main_url = entry.get("url") or ""
                        contact_url = entry.get("url_1") or ""
                        entry_title = entry.get("title") or ""

                        # Only store entries that have at least one gov.in URL
                        if not (_is_target(main_url, suffixes) or _is_target(contact_url, suffixes)):
                            continue

                        # Normalise: prefer the gov domain version
                        if not _is_target(main_url, suffixes) and _is_target(contact_url, suffixes):
                            main_url = _root_domain(contact_url)

                        db.insert_domain(
                            category_code=code,
                            category_title=title,
                            title=entry_title,
                            main_url=main_url,
                            contact_url=contact_url if _is_target(contact_url, suffixes) else None,
                            raw_data=entry,
                        )
                        import_status["inserted"] += 1

                    fetched += len(results)
                    log.info(f"    page {page}: {fetched}/{total} fetched, {import_status['inserted']} inserted total")

                    if fetched >= total or not results:
                        break
                    page += 1

                import_status["done_categories"] += 1

        log.info(f"Import complete: {import_status['inserted']} domains in DB")

    except Exception as e:
        log.error(f"Import failed: {e}", exc_info=True)
        import_status["error"] = str(e)
    finally:
        import_status["running"] = False
