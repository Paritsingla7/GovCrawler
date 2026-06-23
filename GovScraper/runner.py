import logging
import httpx

from api import (
    get_categories,
    get_organization_types,
    get_entries_for_category,
    extract_from_entries,
    HEADERS,
)

log = logging.getLogger(__name__)

def run_all_domains() -> set[str]:
    """
    Programmatically scrapes all categories and returns a flat set
    of all discovered .gov.in and .nic.in root domains.
    """
    all_domains = set()

    with httpx.Client(
        headers=HEADERS,
        follow_redirects=True,
        timeout=httpx.Timeout(20.0),
    ) as client:
        log.info("Fetching categories from GovScraper API...")
        try:
            categories = get_categories(client)
        except Exception as e:
            log.error(f"Failed to fetch categories: {e}")
            return set()

        for cat in categories:
            code = cat["category"]
            title = cat["title"]
            log.info(f"Fetching directory entries for {title}...")
            
            try:
                entries = get_entries_for_category(client, code, None)
                grouped_roots = extract_from_entries(entries)
                
                for state_name, org_groups in grouped_roots.items():
                    for o_code, urls in org_groups.items():
                        all_domains.update(urls)
            except Exception as e:
                log.error(f"[{code}] {title} failed: {e}")

    log.info(f"GovScraper finished. Discovered {len(all_domains)} root domains.")
    return all_domains
