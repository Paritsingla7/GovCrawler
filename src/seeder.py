import sys
import os
import logging

# Add GovScraper to python path so we can import it
sys.path.append(os.path.join(os.path.dirname(os.path.dirname(__file__)), 'GovScraper'))
from runner import run_all_domains

log = logging.getLogger(__name__)

# Common contact page path patterns to synthesize from discovered root domains
_CONTACT_PATHS = [
    "/contact-us",
    "/contact-us.htm",
    "/contact-us.html",
    "/contact",
    "/en/contact-us",
    "/about/officers",
    "/directory-of-officers",
]

async def get_seed_urls(config: dict) -> list[str]:
    """
    Generates seed URLs dynamically using the GovScraper module.
    No hardcoded URLs or external search APIs are used.
    """
    log.info("--- Starting Seed Generation via GovScraper ---")
    
    # Run the GovScraper directly
    root_domains = run_all_domains()
    
    if not root_domains:
        log.warning("GovScraper did not find any root domains.")
        return []

    target_domains = config.get('target_domains', ['.gov.in', '.nic.in'])
    paths = config.get('contact_path_hints', _CONTACT_PATHS)
    
    seed_urls = set()
    for root in root_domains:
        root = root.rstrip('/')
        
        # Verify domain suffix
        if not any(root.endswith(d) for d in target_domains):
            continue
            
        # Add root domain
        seed_urls.add(root)
        
        # Add synthesized contact paths
        for path in paths:
            seed_urls.add(f"{root}{path}")

    log.info(f"Seed generation complete. Created {len(seed_urls)} seed URLs to crawl.")
    return list(seed_urls)
