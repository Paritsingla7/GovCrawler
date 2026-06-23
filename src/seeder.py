import asyncio
import httpx
import logging
import re
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from playwright.async_api import Playwright
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Comprehensive hardcoded seeds — specific contact/officer pages where known,
# root domains otherwise. Used as final fallback and to fill coverage gaps.
GOV_IN_SEED_URLS = [
    # Central Ministries — contact pages
    "https://mea.gov.in/contact-us.htm",
    "https://mha.gov.in/en/contact-us",
    "https://mod.gov.in/contact-us",
    "https://finance.gov.in/contact-us",
    "https://mohua.gov.in/contact-us.html",
    "https://commerce.gov.in/contact-us/",
    "https://www.meity.gov.in/contact",
    "https://education.gov.in/contact-us",
    "https://mohfw.gov.in/contact-us",
    "https://agricoop.nic.in/contact-us",
    "https://pib.gov.in/ContactInformation.aspx",
    "https://dopt.gov.in/contact-us",
    "https://labour.gov.in/contact-us",
    "https://ppac.gov.in/contact-us",
    "https://powermin.gov.in/contact-us",
    "https://indianrailways.gov.in/railwayboard/view_section.jsp?lang=0&id=0,1,304,366",
    "https://morth.gov.in/contact-us",
    "https://msme.gov.in/contact-us",
    "https://dst.gov.in/contact-us",
    "https://dot.gov.in/contact-us",
    "https://tribal.gov.in/contact-us",
    "https://minorityaffairs.gov.in/contact-us",
    "https://socialjustice.gov.in/contact-us",
    "https://wcd.gov.in/contact-us",
    "https://niti.gov.in/about-us/whos-who",
    "https://tourism.gov.in/about-us/whos-who",
    "https://ayush.gov.in/contact-us",
    "https://coal.nic.in/about-us/meet-minister",
    "https://mines.gov.in/contact-us",
    "https://steel.gov.in/telephone",
    "https://jalshakti-dowr.gov.in/contact-us",
    "https://shipmin.gov.in/contact-us",
    "https://panchayat.gov.in/contact-us",
    # State Governments — contact pages
    "https://www.maharashtra.gov.in/1125/Contact-Us",
    "https://www.up.gov.in/en/page/contactUs",
    "https://www.karnataka.gov.in/english/Pages/ContactUs.aspx",
    "https://www.tn.gov.in/departments/list",
    "https://www.ap.gov.in/contact-us/",
    "https://www.telangana.gov.in/contacts/district-officials/",
    "https://www.telangana.gov.in/contacts/secretariat/",
    "https://www.goa.gov.in/contact/",
    "https://gujaratindia.gov.in/contact-us.htm",
    "https://rajasthan.gov.in/contact-us",
    "https://www.mp.gov.in/en/web/guest/contact-us",
    "https://wb.gov.in/portal/web/guest/contactus",
    "https://punjab.gov.in/government/whos-who",
    "https://haryana.gov.in/contact-us",
    "https://uk.gov.in/contact-us",
    "https://www.jharkhand.gov.in/contact-us",
    "https://odisha.gov.in/or/about-us/whos-who/department",
    "https://cgstate.gov.in/contact-us",
    "https://assam.gov.in/contact-us",
    "https://www.kerala.gov.in/contact-us",
    "https://state.bihar.gov.in/main/CitizenHome.html",
    # Additional Central Ministries / Departments
    "https://mib.gov.in/contact-us",
    "https://moefcc.nic.in/contact-us",
    "https://dpiit.gov.in/contact-us",
    "https://pharmaceuticals.gov.in/contact-us",
    "https://fssai.gov.in/contact-us",
    "https://dgft.gov.in/CP/",
    "https://niti.gov.in/about-us/team",
    "https://dea.gov.in/contact-us",
    "https://cbic.gov.in/contact-us",
    "https://incometax.gov.in/iec/foportal/help/contact-us",
    "https://cbi.gov.in/contact-us",
    "https://cvc.gov.in/contact.htm",
    # Constitutional / Statutory Bodies
    "https://upsc.gov.in/contact-us",
    "https://ssc.nic.in/portal/contact",
    "https://nhrc.nic.in/contacts",
    "https://ncw.nic.in/contact-us",
    "https://uidai.gov.in/contact-support/contact-uidai/headquarters.html",
    "https://trai.gov.in/contact-us",
    "https://loksabha.nic.in/Contact/contactno.aspx",
    "https://rajyasabha.nic.in/rsnew/ContactUs/contact.aspx",
    # North-East & Hill States
    "https://manipur.gov.in/contact-us/",
    "https://meghalaya.gov.in/contact",
    "https://mizoram.gov.in/contact-us",
    "https://nagaland.gov.in/contact-us/",
    "https://sikkim.gov.in/contact-us",
    "https://tripura.gov.in/contact-us",
    "https://arunachal.gov.in/contact-us",
    "https://himachal.nic.in/index.php?lang=0&dpt_id=1&level=0&lid=1&rid=1",
    # Union Territories
    "https://delhi.gov.in/page/contact-us",
    "https://chandigarh.gov.in/contact_us",
    "https://puducherry.gov.in/contact-us/",
    "https://ladakh.gov.in/contact-us/",
    "https://dnhdd.gov.in/contact-us",
    # Key PSUs and Autonomous Bodies
    "https://sansad.in/ls/contact",
    "https://prasarbharati.gov.in/contact-us/",
    "https://mospi.gov.in/contact-us",
    "https://nhb.org.in/contact.php",
    "https://isro.gov.in/contact-us.html",
    "https://drdo.gov.in/drdo/contact-us",
    # NIC and key infrastructure
    "https://nic.gov.in/contact-us/",
    "https://digitalindia.gov.in/contact",
    "https://www.mygov.in/contact-us/",
    "https://informatics.nic.in/page/contact_us",
    "https://www.india.gov.in/my-government/ministries-departments",
]

_SITEMAP_PATHS = ['/sitemap.xml', '/sitemap_index.xml']

# Keywords that mark a sitemap URL as worth crawling
_SITEMAP_KEYWORDS = frozenset({
    'contact', 'officer', 'directory', 'whos-who', 'staff', 'personnel',
    'secretariat', 'about', 'division', 'minister', 'committee', 'team',
    'tender', 'procurement', 'e-tender', 'etender', 'bid', 'rfp',
})

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


async def get_seed_urls(p: Playwright, config: dict, keyword: str) -> list[str]:
    """
    Three-tier seed generation:
      Tier 1 — Google CSE + Bing API: targeted contact pages (best quality)
      Tier 2 — india.gov.in directory: discovers all ministry/state domains,
               then synthesizes contact page URL candidates for each
      Tier 3 — Hardcoded comprehensive list: guaranteed baseline coverage
    """
    seed_urls = set()
    log.info("--- Starting Seed Generation ---")

    # Tier 1: Official search APIs
    if config.get('google_cse_api', {}).get('enabled'):
        log.info("Attempting Google Custom Search API...")
        seed_urls.update(await _get_seeds_from_google_api(config, keyword))

    if config.get('bing_search_api', {}).get('enabled'):
        log.info("Attempting Bing Web Search API...")
        seed_urls.update(await _get_seeds_from_bing_api(config, keyword))

    # Tier 2: india.gov.in directory + contact URL synthesis + sitemap parsing
    log.info("Scraping india.gov.in ministry directory...")
    root_domains = await _get_root_domains_from_india_gov(p, config)
    if root_domains:
        synthesized = _synthesize_contact_urls(root_domains, config)
        log.info(f"Synthesized {len(synthesized)} contact URL candidates from {len(root_domains)} domains.")
        seed_urls.update(synthesized)

        log.info("Parsing sitemaps for targeted contact/tender URLs...")
        sitemap_urls = await _get_urls_from_sitemaps(root_domains, config)
        seed_urls.update(sitemap_urls)

    # Tier 3: Hardcoded list — always added to ensure key ministries are covered
    seed_urls.update(GOV_IN_SEED_URLS)

    # Filter: only keep .gov.in / .nic.in URLs
    target_domains = config.get('target_domains', ['.gov.in', '.nic.in'])
    filtered = {
        url for url in seed_urls
        if any(urlparse(url).netloc.endswith(d) for d in target_domains)
    }

    log.info(f"Seed generation complete. {len(filtered)} valid gov seeds.")
    return list(filtered)


async def _get_seeds_from_google_api(config: dict, keyword: str) -> set:
    """Google Custom Search JSON API — returns targeted contact pages."""
    api_config = config['google_cse_api']
    # lstrip('.') fixes the bug: site:.gov.in is invalid, site:gov.in is correct
    domain_query = " OR ".join(
        f"site:{d.lstrip('.')}" for d in config['target_domains']
    )
    search_query = f'{domain_query} "{keyword}" contact email'

    params = {
        "key": api_config['api_key'],
        "cx":  api_config['cse_id'],
        "q":   search_query,
        "num": 10,
    }

    found_urls = set()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get("https://www.googleapis.com/customsearch/v1", params=params)
            r.raise_for_status()
            for item in r.json().get("items", []):
                if "link" in item:
                    found_urls.add(item["link"])
        log.info(f"Google CSE found {len(found_urls)} seeds.")
    except Exception as e:
        log.error(f"Google CSE failed: {e}")
    return found_urls


async def _get_seeds_from_bing_api(config: dict, keyword: str) -> set:
    """Bing Web Search API v7 — returns targeted contact pages."""
    api_config = config['bing_search_api']
    # lstrip('.') fixes the bug: site:.gov.in is invalid, site:gov.in is correct
    domain_query = " OR ".join(
        f"site:{d.lstrip('.')}" for d in config['target_domains']
    )
    search_query = f'{domain_query} "{keyword}" contact email'

    headers = {"Ocp-Apim-Subscription-Key": api_config['api_key']}
    params  = {"q": search_query, "count": 20}

    found_urls = set()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                "https://api.bing.microsoft.com/v7.0/search",
                headers=headers, params=params
            )
            r.raise_for_status()
            for item in r.json().get("webPages", {}).get("value", []):
                if "url" in item:
                    found_urls.add(item["url"])
        log.info(f"Bing API found {len(found_urls)} seeds.")
    except Exception as e:
        log.error(f"Bing API failed: {e}")
    return found_urls


async def _get_root_domains_from_india_gov(p: Playwright, config: dict) -> set:
    """
    Two-step domain discovery from india.gov.in:
      Step 1 — httpx direct fetch: fast, no bot detection risk, works if the server
               renders static HTML for non-browser clients (common on NIC-hosted sites).
      Step 2 — Playwright + playwright-stealth: if httpx yields fewer than 50 domains,
               apply anti-fingerprint patches to bypass bot detection so AJAX fires.
    Both steps add to the same set; duplicates collapse automatically.
    """
    directory_pages = [
        "https://www.india.gov.in/directory/web-directory",
        "https://www.india.gov.in/directory/whos-who",
        "https://www.india.gov.in/directory/contact-directory",
    ]
    target_domains = config.get('target_domains', ['.gov.in', '.nic.in'])
    root_domains = set()

    # --- Step 1: plain httpx ---
    log.info("Trying httpx fetch of india.gov.in directory...")
    try:
        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=True,
            headers={"User-Agent": config['user_agent']},
        ) as client:
            for url in directory_pages:
                try:
                    r = await client.get(url)
                    if r.status_code == 200:
                        soup = BeautifulSoup(r.text, 'lxml')
                        for a in soup.find_all('a', href=True):
                            parsed = urlparse(a['href'])
                            if parsed.scheme in ('http', 'https') and any(
                                parsed.netloc.endswith(d) for d in target_domains
                            ):
                                root_domains.add(f"{parsed.scheme}://{parsed.netloc}")
                except Exception as e:
                    log.debug(f"httpx fetch failed for {url}: {e}")
    except Exception as e:
        log.warning(f"httpx client error: {e}")

    log.info(f"httpx found {len(root_domains)} domains from india.gov.in.")

    if len(root_domains) >= 50:
        log.info("httpx yield sufficient — skipping Playwright.")
        return root_domains

    # --- Step 2: Playwright + stealth ---
    log.info(f"httpx yield too low ({len(root_domains)}), trying Playwright + stealth...")
    browser = None
    try:
        from playwright_stealth import Stealth
    except ImportError:
        log.warning("playwright-stealth not installed — skipping stealth step. Run: pip install playwright-stealth")
        log.info(f"Directory scrape found {len(root_domains)} root domains.")
        return root_domains

    try:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=config['user_agent'])
        page    = await context.new_page()
        await Stealth().apply_stealth_async(page)

        # Intercept ALL network responses and extract gov.in domains from JSON/text.
        # This captures XHR API responses directly — bypasses the DOM rendering step
        # entirely, so we get data even when React hasn't finished painting the links.
        intercepted: set[str] = set()

        async def _on_response(response):
            try:
                if response.status != 200:
                    return
                ct = response.headers.get('content-type', '')
                if not ('json' in ct or 'javascript' in ct or ct.startswith('text')):
                    return
                text = await response.text()
                for m in re.findall(r'https?://[\w.-]+\.(?:gov|nic)\.in', text):
                    parsed = urlparse(m)
                    if any(parsed.netloc.endswith(d) for d in target_domains):
                        intercepted.add(f"{parsed.scheme}://{parsed.netloc}")
            except Exception:
                pass

        page.on('response', lambda r: asyncio.create_task(_on_response(r)))

        for dir_url in directory_pages:
            try:
                log.info(f"Stealth scraping: {dir_url}")
                await page.goto(
                    dir_url,
                    wait_until="domcontentloaded",
                    timeout=config['defaults']['page_timeout'] * 1000,
                )
                # Wait for XHR calls to complete — the ministry data arrives via
                # async API calls after page load, not in the initial HTML
                await page.wait_for_timeout(15000)

                before = len(root_domains)
                root_domains.update(intercepted)
                intercepted.clear()

                # Also grab any links that did make it into the DOM
                all_hrefs = await page.eval_on_selector_all(
                    "a[href]", "els => els.map(e => e.href)"
                )
                for href in all_hrefs:
                    parsed = urlparse(href)
                    if any(parsed.netloc.endswith(d) for d in target_domains):
                        root_domains.add(f"{parsed.scheme}://{parsed.netloc}")

                added = len(root_domains) - before
                log.info(f"  Added {added} new domains from {dir_url} (interception + DOM)")

                if len(root_domains) > 120:
                    break

            except Exception as e:
                log.warning(f"Failed stealth scraping {dir_url}: {e}")

    except Exception as e:
        log.error(f"Playwright stealth scraper failed: {e}")
    finally:
        if browser:
            await browser.close()

    log.info(f"Directory scrape found {len(root_domains)} root domains.")
    return root_domains


def _synthesize_contact_urls(root_domains: set, config: dict) -> set:
    """
    Generates contact page URL candidates for each discovered root domain
    by appending common contact path patterns. The crawler handles 404s
    gracefully, so we add all candidates without pre-checking.
    """
    paths = config.get('contact_path_hints', _CONTACT_PATHS)
    candidates = set()
    for root in root_domains:
        root = root.rstrip('/')
        for path in paths:
            candidates.add(f"{root}{path}")
    return candidates


async def _get_urls_from_sitemaps(root_domains: set, config: dict) -> set:
    """
    Fetches /sitemap.xml or /sitemap_index.xml for each discovered root domain.
    Extracts only URLs that contain contact/officer/tender keywords.
    Runs 15 domains concurrently to keep seeder startup time reasonable.
    """
    target_domains = config.get('target_domains', ['.gov.in', '.nic.in'])
    found_urls = set()
    sem = asyncio.Semaphore(15)

    async def check_domain(root: str):
        for path in _SITEMAP_PATHS:
            try:
                async with sem:
                    async with httpx.AsyncClient(
                        timeout=10,
                        follow_redirects=True,
                        headers={"User-Agent": config['user_agent']},
                    ) as client:
                        r = await client.get(f"{root.rstrip('/')}{path}")

                if r.status_code != 200:
                    continue

                # Strip XML namespaces so ElementTree iter() works without ns prefixes
                content = re.sub(r'\s+xmlns(?::\w+)?="[^"]+"', '', r.text)
                root_elem = ET.fromstring(content)

                count = 0
                for loc in root_elem.iter('loc'):
                    if not loc.text:
                        continue
                    url = loc.text.strip()
                    url_lower = url.lower()
                    if any(kw in url_lower for kw in _SITEMAP_KEYWORDS):
                        if any(urlparse(url).netloc.endswith(d) for d in target_domains):
                            found_urls.add(url)
                            count += 1

                if count > 0:
                    log.info(f"Sitemap {root}: {count} targeted URLs")
                break  # found a working sitemap, skip remaining paths

            except Exception:
                continue

    await asyncio.gather(*[check_domain(root) for root in root_domains])
    log.info(f"Sitemap parsing complete. {len(found_urls)} targeted URLs found.")
    return found_urls
