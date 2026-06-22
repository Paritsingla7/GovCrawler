import asyncio
import httpx
import logging
from playwright.async_api import Playwright
from urllib.parse import quote_plus

log = logging.getLogger(__name__)

async def get_seed_urls(p: Playwright, config: dict, keyword: str) -> list[str]:
    """
    Orchestrates a multi-tiered seed generation process for maximum reliability.
    """
    seed_urls = set()
    log.info("--- Starting Seed Generation ---")

    # Tier 1: Official Developer APIs (Google & Bing)
    if config.get('google_cse_api', {}).get('enabled'):
        log.info("Attempting Google Custom Search API...")
        seed_urls.update(await _get_seeds_from_google_api(config, keyword))
    
    if config.get('bing_search_api', {}).get('enabled'):
        log.info("Attempting Bing Web Search API...")
        seed_urls.update(await _get_seeds_from_bing_api(config, keyword))

    # Tier 2: Free DuckDuckGo API
    if len(seed_urls) < 10 and config.get('duckduckgo_api', {}).get('enabled'):
        log.info("Official API results are low. Attempting DuckDuckGo API...")
        seed_urls.update(await _get_seeds_from_duckduckgo_api(config, keyword))

    # Tier 3: Browser-based Scraping
    if len(seed_urls) < 10:
        log.info("API results are still low. Falling back to browser-based scraping.")
        seed_urls.update(await _get_seeds_from_browsers(p, config, keyword))

    # Tier 4: Hardcoded Fallbacks
    if len(seed_urls) < 5:
        log.warning("All seeders returned low results. Injecting hardcoded fallback URLs.")
        seed_urls.update(config.get('fallback_urls', []))

    log.info(f"Seed generation finished. Total unique seeds: {len(seed_urls)}")
    return list(seed_urls)[:50] # Limit total seeds to a reasonable number

async def _get_seeds_from_google_api(config: dict, keyword: str) -> set:
    """Fetches seed URLs from the Google Custom Search JSON API."""
    api_config = config['google_cse_api']
    domain_query = " OR ".join([f"site:{domain}" for domain in config['target_domains']])
    search_query = f'{domain_query} "{keyword}" contact email'
    
    params = {
        "key": api_config['api_key'],
        "cx": api_config['cse_id'],
        "q": search_query,
        "num": 10 # Max results per page
    }
    
    found_urls = set()
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get("https://www.googleapis.com/customsearch/v1", params=params)
            response.raise_for_status()
            data = response.json()
            
            for item in data.get("items", []):
                if "link" in item:
                    found_urls.add(item["link"])
        log.info(f"Google API found {len(found_urls)} seeds.")
    except Exception as e:
        log.error(f"Google Custom Search API failed: {e}. Check your API key and CSE ID.")
    return found_urls

async def _get_seeds_from_bing_api(config: dict, keyword: str) -> set:
    """Fetches seed URLs from the Bing Web Search API."""
    api_config = config['bing_search_api']
    domain_query = " OR ".join([f"site:{domain}" for domain in config['target_domains']])
    search_query = f'{domain_query} "{keyword}" contact email'

    headers = {"Ocp-Apim-Subscription-Key": api_config['api_key']}
    params = {"q": search_query, "count": 20}
    
    found_urls = set()
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get("https://api.bing.microsoft.com/v7.0/search", headers=headers, params=params)
            response.raise_for_status()
            data = response.json()
            
            for item in data.get("webPages", {}).get("value", []):
                if "url" in item:
                    found_urls.add(item["url"])
        log.info(f"Bing API found {len(found_urls)} seeds.")
    except Exception as e:
        log.error(f"Bing Web Search API failed: {e}. Check your API key.")
    return found_urls

async def _get_seeds_from_duckduckgo_api(config: dict, keyword: str) -> set:
    """Fetches seed URLs from the DuckDuckGo Search API."""
    api_config = config['duckduckgo_api']
    domain_query = " OR ".join([f"site:{domain}" for domain in config['target_domains']])
    search_query = f'{domain_query} "{keyword}" contact email'
    
    found_urls = set()
    try:
        async with httpx.AsyncClient(headers={"User-Agent": config['user_agent']}) as client:
            response = await client.get(
                "https://api.duckduckgo.com/",
                params={"q": search_query, "format": "json", "l": api_config['region']},
                timeout=api_config['timeout']
            )
            response.raise_for_status()
            data = response.json()

        results = data.get("RelatedTopics", []) + data.get("Results", [])
        for item in results:
            url = item.get("FirstURL") or item.get("URL")
            if url:
                found_urls.add(url.strip())
        log.info(f"DuckDuckGo API found {len(found_urls)} seeds.")
    except Exception as e:
        log.warning(f"DuckDuckGo API failed: {e}")
    return found_urls

async def _get_seeds_from_browsers(p: Playwright, config: dict, keyword: str) -> set:
    """Scrapes search engines using a disposable browser instance as a last resort."""
    domain_query = " OR ".join([f"site:{domain}" for domain in config['target_domains']])
    search_query = f'{domain_query} "{keyword}" contact email'
    found_urls = set()
    
    browser = None
    try:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=config['user_agent'])
        page = await context.new_page()

        for engine in config.get('browser_seeders', []):
            log.info(f"Scraping {engine['name']}...")
            try:
                query = quote_plus(search_query)
                await page.goto(engine["url"].format(query=query), wait_until="domcontentloaded", timeout=config['defaults']['page_timeout'] * 1000)
                
                raw_urls = await page.eval_on_selector_all(engine["selector"], "elements => elements.map(e => e.href || e.textContent)")
                
                for url in raw_urls:
                    if "google.com/url?q=" in url:
                        url = url.split("google.com/url?q=")[1].split("&")[0]
                    
                    formatted_url = url.strip()
                    if not formatted_url.startswith("http"):
                        formatted_url = f"https://{formatted_url}"
                    found_urls.add(formatted_url)

                if len(found_urls) > 15:
                    break
            except Exception as e:
                log.warning(f"Scraping {engine['name']} failed: {e}")
    finally:
        if browser:
            await browser.close()
            
    return found_urls
