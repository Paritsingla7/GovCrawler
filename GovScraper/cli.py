"""
Interactive Command Line Interface for GovCrawler.
"""

import json
import logging
import httpx

from govcrawler import (
    get_categories,
    get_organization_types,
    get_entries_for_category,
    extract_from_entries,
    HEADERS,
)
from govcrawler.config import OUT_JSON

log = logging.getLogger(__name__)


def interactive_scrape() -> dict:
    # { category_title: { state_name: { org_title: [links] } } }
    results_dict: dict[str, dict[str, dict[str, list[str]]]] = {}

    with httpx.Client(
        headers=HEADERS,
        follow_redirects=True,
        timeout=httpx.Timeout(20.0),
    ) as client:

        # Step 1: get categories
        log.info("=== Step 1: Fetching categories ===")
        try:
            categories = get_categories(client)
        except Exception as e:
            log.error(f"Failed to fetch categories: {e}")
            categories = []

        if not categories:
            print("No categories found. Exiting.")
            return {}

        print("\n=== Available Categories ===")
        print("0. All Categories (Full Scrape)")
        for i, cat in enumerate(categories, start=1):
            print(f"{i}. {cat['title']} ({cat['count']} entries) [{cat['category']}]")
        print("x. Exit")

        choice = input("\nEnter the number of the category to scrape: ").strip().lower()
        if choice == 'x':
            print("Exiting.")
            return {}

        if not choice.isdigit():
            print("Invalid input.")
            return {}
        
        choice_idx = int(choice)
        if choice_idx < 0 or choice_idx > len(categories):
            print("Invalid choice.")
            return {}

        selected_categories = categories if choice_idx == 0 else [categories[choice_idx - 1]]
        
        log.info("=== Step 2: Fetching directory entries ===")
        for cat in selected_categories:
            code  = cat["category"]
            title = cat["title"]
            results_dict[title] = {}
            
            try:
                # Fetch org types directly from the API
                log.info(f"=== Fetching filters for {title} ===")
                org_types = get_organization_types(client, code)
                org_mapping = {ot['organization_type']: ot['title'] for ot in org_types} if org_types else {}
                
                org_type_code = None
                if org_types:
                    print(f"\n=== Organization Types for {title} ===")
                    print("0. All (No filter)")
                    for i, ot in enumerate(org_types, start=1):
                        print(f"{i}. {ot['title']} ({ot['count']} entries) [{ot['organization_type']}]")
                        
                    org_choice = input(f"\nEnter the number to filter {title} (0 for all): ").strip()
                    if org_choice.isdigit():
                        o_idx = int(org_choice)
                        if 1 <= o_idx <= len(org_types):
                            selected_ot = org_types[o_idx - 1]
                            org_type_code = selected_ot["organization_type"]
                            print(f"Filtering by: {selected_ot['title']}")
                
                log.info(f"=== Fetching directory entries for {title} ===")
                entries = get_entries_for_category(client, code, org_type_code)
                grouped_roots = extract_from_entries(entries)
                
                total_cat_domains = 0
                for state_name, org_groups in grouped_roots.items():
                    if state_name not in results_dict[title]:
                        results_dict[title][state_name] = {}
                        
                    for o_code, urls in org_groups.items():
                        o_title = org_mapping.get(o_code, o_code)
                        if o_title not in results_dict[title][state_name]:
                            results_dict[title][state_name][o_title] = []
                        results_dict[title][state_name][o_title].extend(sorted(list(urls)))
                        total_cat_domains += len(urls)
                    
                log.info(
                    f"  [{code}] done — "
                    f"{total_cat_domains} gov domains"
                )
            except Exception as e:
                log.error(f"  [{code}] {title} failed: {e}")

        # Fallback removed or handled differently if needed
        has_domains = any(len(urls) > 0 for states in results_dict.values() for orgs in states.values() for urls in orgs.values())
        if not has_domains:
            log.warning("Main API returned 0 domains")

    # Write JSON output only
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(results_dict, f, indent=2, ensure_ascii=False)

    log.info(f"Done. Saved to {OUT_JSON}")
    return results_dict

