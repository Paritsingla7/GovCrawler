"""
Imports india.gov.in Web Directory into the portal database.

Two import modes:
  1. JSON (preferred, zero API calls):
       import_from_json(db, "gov_domains.json", config)
       Structure: {category_title: {state: {org_type_title: [url_or_entry_obj, ...]}}}
       (see import_from_json's docstring for the entry object shape used by
       organizations with no known URL)

  2. Live API (for refreshing data only):
       import_all(db, config)
       Uses GovScraper: get_categories → get_organization_types → get_entries_for_category

Both run as blocking calls inside asyncio.to_thread() from the API layer.
Progress is tracked in import_status dict, polled by /api/import/status.
"""

import json
import logging
from pathlib import Path
from urllib.parse import urlparse

import httpx

from GovScraper.api import (
    get_categories,
    get_organization_types,
    get_entries_for_category,
    HEADERS,
    TARGET_SUFFIXES,
)
from ..db import Database

log = logging.getLogger(__name__)

# Maps category titles (as they appear in gov_domains.json) to stable short codes.
_CAT_CODE = {
    "State / UT Government": "sg",
    "Union Government": "ug",
    "Judiciary": "jud",
    "Legislature": "leg",
    "Indian Missions Abroad": "ims",
    "Apex Bodies": "apex",
    "Districts": "dist",
}

import_status: dict = {
    "running": False,
    "source": None,  # "json" or "api"
    "total_categories": 0,
    "done_categories": 0,
    "total_entries": 0,
    "inserted": 0,
    "error": None,
}


def _is_target(url: str) -> bool:
    if not url or not url.startswith("http"):
        return False
    netloc = urlparse(url).netloc
    return any(netloc.endswith(s) for s in TARGET_SUFFIXES)


def _root_url(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _title_from_url(url: str) -> str:
    """Derive a readable organization name from a .gov.in URL hostname."""
    try:
        host = urlparse(url).netloc.lower()
        for suffix in ('.gov.in', '.nic.in', '.res.in', '.ac.in', '.edu.in'):
            if host.endswith(suffix):
                host = host[:-len(suffix)]
                break
        name = host.replace('.', ' ').replace('-', ' ').strip()
        return ' '.join(w.capitalize() for w in name.split()) if name else ''
    except Exception:
        return ''


# ── JSON import (zero API calls) ──────────────────────────────────────────────

def _resolve_json_entry(entry) -> dict:
    """Normalize one gov_domains.json list entry to
    {title, main_url, contact_url, external_id}.

    Accepts either a plain URL string (legacy format) or an object
    {"title", "url", "contact_url", "external_id"} — the latter allows
    organizations with no known URL to still be imported (main_url stays
    None) instead of being silently dropped.
    """
    if isinstance(entry, str):
        raw_main, raw_contact = entry, ""
        title, external_id = "", None
    else:
        raw_main = entry.get("url") or ""
        raw_contact = entry.get("contact_url") or ""
        title = entry.get("title") or ""
        external_id = entry.get("external_id") or None

    if _is_target(raw_main):
        main_url = _root_url(raw_main)
        contact_url = raw_contact if _is_target(raw_contact) else None
    elif _is_target(raw_contact):
        main_url = _root_url(raw_contact)
        contact_url = None
    else:
        main_url = None
        contact_url = None

    return {
        "title": title or (_title_from_url(main_url) if main_url else ""),
        "main_url": main_url,
        "contact_url": contact_url,
        "external_id": external_id,
    }


def import_from_json(db: Database, json_path: str | Path, config: dict):
    """
    One-time import from gov_domains.json. Zero API calls.

    Expected format:
      {
        "State / UT Government": {
          "Haryana": {
            "Departments": [
              "https://example.gov.in",
              {"title": "Org With No URL", "url": null, "contact_url": null,
               "external_id": "some-stable-id"}
            ],
            "Others":      ["https://..."]
          }
        },
        ...
      }

    List entries may be a plain URL string (legacy format, dropped if the
    URL isn't a target gov domain) or an object carrying a title/external_id
    so organizations without a known URL are still imported for later
    manual URL entry, instead of being silently discarded.
    """
    global import_status

    json_path = Path(json_path)
    if not json_path.exists():
        msg = f"gov_domains.json not found at: {json_path.resolve()}"
        log.error(msg)
        import_status["error"] = msg
        return

    import_status.update({
        "running": True,
        "source": "json",
        "total_categories": 0,
        "done_categories": 0,
        "total_entries": 0,
        "inserted": 0,
        "error": None,
    })

    try:
        with open(json_path, encoding="utf-8") as f:
            data: dict = json.load(f)

        import_status["total_categories"] = len(data)
        log.info(f"JSON import: {len(data)} categories from {json_path}")

        db.clear_domains()

        for cat_title, states in data.items():
            cat_code = _CAT_CODE.get(cat_title, cat_title.lower().replace(" ", "_")[:20])
            inserted_this_cat = 0

            for state_name, org_types in states.items():
                state = state_name if state_name != "National / Unknown" else "National"

                for org_type_title, urls in org_types.items():
                    # Use org_type_title as the code too (JSON has no separate code)
                    org_code = org_type_title.lower().replace(" ", "_").replace("/", "_")[:30]

                    import_status["total_entries"] += len(urls)

                    for entry in urls:
                        resolved = _resolve_json_entry(entry)
                        # A legacy plain-string entry carries no info beyond
                        # its URL — skip it if that URL isn't a target gov
                        # domain. Object entries are kept even with no URL
                        # since they still carry a title/external_id.
                        if isinstance(entry, str) and not resolved["main_url"]:
                            continue

                        db.upsert_domain(
                            category_code=cat_code,
                            category_title=cat_title,
                            state=state,
                            org_type=org_code,
                            org_type_title=org_type_title,
                            title=resolved["title"],
                            main_url=resolved["main_url"],
                            contact_url=resolved["contact_url"],
                            external_id=resolved["external_id"],
                        )
                        import_status["inserted"] += 1
                        inserted_this_cat += 1

            log.info(f"  [{cat_code}] {cat_title} — {inserted_this_cat} domains")
            import_status["done_categories"] += 1

        log.info(f"JSON import complete: {import_status['inserted']} domains in DB")

    except Exception as e:
        log.error(f"JSON import failed: {e}", exc_info=True)
        import_status["error"] = str(e)
    finally:
        import_status["running"] = False


# ── Live API import (for refreshing data) ─────────────────────────────────────

def import_all(db: Database, config: dict):
    """
    Live import from india.gov.in API. Makes many API calls — use only
    to refresh data after the initial JSON import is done.
    """
    global import_status

    scraper_cfg = config.get("scraper", {})
    cat_filter = scraper_cfg.get("category_filter", "") or ""
    org_filter = scraper_cfg.get("org_type_filter", "") or ""

    import_status.update({
        "running": True,
        "source": "api",
        "total_categories": 0,
        "done_categories": 0,
        "total_entries": 0,
        "inserted": 0,
        "error": None,
    })

    try:
        with httpx.Client(headers=HEADERS, follow_redirects=True,
                          timeout=httpx.Timeout(20.0)) as client:

            log.info("Fetching categories from india.gov.in API…")
            categories = get_categories(client)
            import_status["total_categories"] = len(categories)
            log.info(f"Found {len(categories)} categories")

            db.clear_domains()

            for cat in categories:
                code = cat["category"]
                title = cat.get("title", code)

                if cat_filter and code != cat_filter:
                    import_status["done_categories"] += 1
                    continue

                # Build org_type code → title mapping for this category
                try:
                    org_types = get_organization_types(client, code)
                    org_map = {ot["organization_type"]: ot["title"]
                               for ot in org_types if ot.get("organization_type")}
                except Exception as e:
                    log.warning(f"[{code}] Could not fetch org types: {e}")
                    org_map = {}

                log.info(f"  [{code}] {title} — fetching entries…")

                try:
                    entries = get_entries_for_category(
                        client, code,
                        org_filter if org_filter else None,
                    )
                except Exception as e:
                    log.error(f"  [{code}] Failed to fetch entries: {e}")
                    import_status["done_categories"] += 1
                    continue

                import_status["total_entries"] += len(entries)
                inserted_this_cat = 0

                for entry in entries:
                    raw_main = entry.get("url") or ""
                    raw_contact = entry.get("url_1") or ""
                    entry_title = entry.get("title") or ""
                    state = entry.get("stateName") or "National / Unknown"
                    org_code = entry.get("organization_type") or "UNKNOWN"
                    org_t_title = org_map.get(org_code, org_code)
                    external_id = entry.get("npi_sanitized_id") or None

                    # Entries with no crawlable URL (main or contact) are kept
                    # with main_url=None rather than dropped — the frontend
                    # marks them "not crawlable" and lets a user add one later.
                    if _is_target(raw_main):
                        main_url = _root_url(raw_main)
                        clean_contact = raw_contact if _is_target(raw_contact) else None
                    elif _is_target(raw_contact):
                        main_url = _root_url(raw_contact)
                        clean_contact = None
                    else:
                        main_url = None
                        clean_contact = None

                    db.upsert_domain(
                        category_code=code,
                        category_title=title,
                        state=state,
                        org_type=org_code,
                        org_type_title=org_t_title,
                        title=entry_title,
                        main_url=main_url,
                        contact_url=clean_contact,
                        external_id=external_id,
                    )
                    import_status["inserted"] += 1
                    inserted_this_cat += 1

                log.info(f"  [{code}] {title} — {inserted_this_cat} domains inserted")
                import_status["done_categories"] += 1

        log.info(f"API import complete: {import_status['inserted']} domains in DB")

    except Exception as e:
        log.error(f"API import failed: {e}", exc_info=True)
        import_status["error"] = str(e)
    finally:
        import_status["running"] = False
