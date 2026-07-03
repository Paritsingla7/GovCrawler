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


def run_all_domains(config: dict = None) -> dict[str, dict[str, str | None]]:
    """
    Programmatically scrapes categories using optional filters from config,
    and returns a dictionary of organization metadata keyed by the API's
    stable external_id — not by URL, since some organizations have none.
    Format: {
      "<external_id>": {
        "title": "...", "url": "https://example.gov.in" | None,
        "contact_url": "..." | None,
        "category": "Union Government", "state": "Delhi", "org_type": "Statutory Body"
      }
    }
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
                grouped_records = extract_from_entries(entries)

                for state_name, org_groups in grouped_records.items():
                    for o_code, records in org_groups.items():
                        org_title = org_mapping.get(o_code, o_code)
                        for record in records:
                            key = record["external_id"] or f"{record['title']}|{record['url'] or ''}"
                            if key not in domain_metadata:
                                domain_metadata[key] = {
                                    "title": record["title"],
                                    "url": record["url"],
                                    "contact_url": record["contact_url"],
                                    "category": title,
                                    "state": state_name,
                                    "org_type": org_title,
                                }
            except Exception as e:
                log.error(f"[{code}] {title} failed: {e}")

    log.info(f"GovScraper finished. Discovered {len(domain_metadata)} organizations.")
    return domain_metadata


def build_gov_domains_json(config: dict = None) -> dict:
    """
    Runs run_all_domains() and reshapes its flat, external_id-keyed output
    into the nested gov_domains.json structure expected by the portal's
    import_from_json():
      { category_title: { state: { org_type_title: [entry_obj, ...] } } }
    Each entry_obj is {title, url, contact_url, external_id} — url/contact_url
    are None for organizations with no known URL, so they still get imported
    (as "not crawlable") instead of being lost.
    """
    flat = run_all_domains(config)

    gov_domains: dict[str, dict[str, dict[str, list[dict]]]] = {}
    for external_id, record in flat.items():
        state = record["state"] or "National / Unknown"
        by_state = gov_domains.setdefault(record["category"], {})
        by_org = by_state.setdefault(state, {})
        by_org.setdefault(record["org_type"], []).append({
            "title": record["title"],
            "url": record["url"],
            "contact_url": record["contact_url"],
            "external_id": external_id,
        })

    return gov_domains


def main():
    import argparse
    import json

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(
        description="Scrape the india.gov.in Web Directory into a gov_domains.json file."
    )
    parser.add_argument("output", nargs="?", default="gov_domains.json",
                        help="Output path (default: gov_domains.json)")
    parser.add_argument("--category", default="", help="Only scrape this category code (e.g. 'ug')")
    parser.add_argument("--org-type", default="", help="Only scrape this organization_type code")
    args = parser.parse_args()

    config = {"scraper": {"category_filter": args.category, "org_type_filter": args.org_type}}
    gov_domains = build_gov_domains_json(config)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(gov_domains, f, indent=2, ensure_ascii=False)

    total_orgs = sum(
        len(entries)
        for states in gov_domains.values()
        for org_types in states.values()
        for entries in org_types.values()
    )
    log.info(f"Wrote {total_orgs} organizations across {len(gov_domains)} categories to {args.output}")


if __name__ == "__main__":
    main()
