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

def run_all_domains(config: dict = None) -> dict[str, dict[str, str]]:
    """
    Programmatically scrapes categories using optional filters from config,
    and returns a dictionary of domain metadata.
    Format: { "https://example.gov.in": {"category": "Union Government", "state": "Delhi", "org_type": "Statutory Body"} }
    """
    if config is None:
        config = {}
    
    scraper_conf = config.get("scraper", {})
    cat_filter = scraper_conf.get("category_filter", "")
    org_filter = scraper_conf.get("org_type_filter", "")

    domain_metadata = {}

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
            return {}

        for cat in categories:
            code = cat["category"]
            title = cat["title"]

            # Apply category filter
            if cat_filter and code != cat_filter:
                continue
                
            # Fetch Org Types to resolve names
            try:
                org_types = get_organization_types(client, code)
                org_mapping = {ot['organization_type']: ot['title'] for ot in org_types} if org_types else {}
            except Exception as e:
                log.warning(f"Failed to fetch org types for {code}: {e}")
                org_mapping = {}

            log.info(f"Fetching directory entries for {title}...")
            
            try:
                # Apply org_type_filter if present
                entries = get_entries_for_category(client, code, org_filter if org_filter else None)
                grouped_roots = extract_from_entries(entries)
                
                for state_name, org_groups in grouped_roots.items():
                    for o_code, urls in org_groups.items():
                        org_title = org_mapping.get(o_code, o_code)
                        for url in urls:
                            if url not in domain_metadata:
                                domain_metadata[url] = {
                                    "category": title,
                                    "state": state_name,
                                    "org_type": org_title
                                }
            except Exception as e:
                log.error(f"[{code}] {title} failed: {e}")

    log.info(f"GovScraper finished. Discovered {len(domain_metadata)} root domains.")
    return domain_metadata
