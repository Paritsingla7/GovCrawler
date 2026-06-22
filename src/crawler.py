import asyncio
import logging
from playwright.async_api import BrowserContext
from urllib.parse import urljoin, urlparse

from .storage import LocalStorage
from .parser import parse_page_for_leads

log = logging.getLogger(__name__)

# Skip these extensions — they fail in Playwright and contain no parseable emails
_SKIP_EXTENSIONS = frozenset({
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.zip', '.rar', '.7z', '.tar', '.gz',
    '.jpg', '.jpeg', '.png', '.gif', '.svg', '.ico',
    '.mp4', '.mp3', '.avi', '.mov',
})

# Links whose URL or anchor text contains these are queued from any page.
# All other links are only queued when we're already on a priority page.
_PRIORITY_KEYWORDS = frozenset({
    # Contact / officer pages
    'contact', 'officer', 'directory', 'whos-who', 'who-is-who',
    'staff', 'personnel', 'secretariat', 'about-us', 'division',
    'minister', 'committee', 'administration', 'team',
    # Tender / procurement pages
    'tender', 'procurement', 'e-tender', 'etender', 'eprocurement', 'rfp', 'bid',
})


def _is_skippable_url(url: str) -> bool:
    url_lower = url.lower()
    parsed = urlparse(url_lower)
    if any(parsed.path.endswith(ext) for ext in _SKIP_EXTENSIONS):
        return True
    # Catches download.php?file=report.pdf style URLs
    if any(url_lower.endswith(ext) for ext in _SKIP_EXTENSIONS):
        return True
    return False


class Crawler:
    def __init__(self, config: dict, storage: LocalStorage):
        self.config = config
        self.storage = storage
        self.queue = asyncio.Queue()
        self.visited_urls = set()
        self._domain_semaphores: dict[str, asyncio.Semaphore] = {}
        self._load_visited_from_storage()

    def _load_visited_from_storage(self):
        try:
            self.visited_urls.update(
                row[0] for row in self.storage.cursor.execute("SELECT url FROM visited_urls")
            )
            log.info(f"Loaded {len(self.visited_urls)} previously visited URLs from database.")
        except Exception as e:
            log.error(f"Could not load visited URLs: {e}")

    def _domain_semaphore(self, url: str) -> asyncio.Semaphore:
        netloc = urlparse(url).netloc
        if netloc not in self._domain_semaphores:
            self._domain_semaphores[netloc] = asyncio.Semaphore(1)
        return self._domain_semaphores[netloc]

    def _is_gov_domain(self, url: str) -> bool:
        netloc = urlparse(url).netloc
        target_domains = self.config.get('target_domains', ['.gov.in', '.nic.in'])
        return any(netloc.endswith(d) for d in target_domains)

    def _is_priority_url(self, url: str, anchor_text: str = '') -> bool:
        combined = (url + ' ' + anchor_text).lower()
        return any(kw in combined for kw in _PRIORITY_KEYWORDS)

    async def run(self, browser_context: BrowserContext, seed_urls: list[str]):
        for url in seed_urls:
            if url not in self.visited_urls and not _is_skippable_url(url):
                self.visited_urls.add(url)
                await self.queue.put((url, 0))

        workers = [
            asyncio.create_task(self._worker(browser_context))
            for _ in range(self.config['defaults']['num_workers'])
        ]

        await self.queue.join()

        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    async def _worker(self, context: BrowserContext):
        while True:
            try:
                url, depth = await self.queue.get()
                try:
                    await asyncio.wait_for(
                        self._process_url(url, depth, context),
                        timeout=self.config['defaults']['url_process_timeout'],
                    )
                except asyncio.TimeoutError:
                    log.warning(f"Processing stalled on {url}. Moving on.")
                except Exception as e:
                    log.error(f"Critical error processing {url}: {e}")
                finally:
                    self.queue.task_done()
            except asyncio.CancelledError:
                break

    async def _process_url(self, url: str, depth: int, context: BrowserContext):
        max_depth    = self.config['defaults']['max_depth']
        page_timeout = self.config['defaults']['page_timeout']
        request_delay = self.config['defaults'].get('request_delay', 1.5)

        if max_depth > 0 and depth >= max_depth:
            return

        self.storage.mark_visited(url)
        log.info(f"Crawling (Depth {depth}): {url}")

        sem  = self._domain_semaphore(url)
        page = await context.new_page()
        try:
            async with sem:
                try:
                    await page.goto(
                        url,
                        timeout=page_timeout * 1000,
                        wait_until="domcontentloaded",
                    )
                except Exception as nav_err:
                    # One retry on timeout — slow gov sites often succeed on second attempt
                    if "Timeout" in type(nav_err).__name__:
                        log.info(f"Timeout on {url}, retrying in 5s...")
                        await asyncio.sleep(5)
                        await page.goto(
                            url,
                            timeout=page_timeout * 1000,
                            wait_until="domcontentloaded",
                        )
                    else:
                        raise
                await asyncio.sleep(request_delay)

            html_content = await page.content()
            page_title   = await page.title()

            leads = parse_page_for_leads(html_content)
            new_leads = 0
            for lead in leads:
                if self.storage.save_lead(
                    email=lead['email'],
                    source_url=url,
                    page_title=page_title,
                    context_snippet=lead['context_snippet'],
                ):
                    new_leads += 1
            if new_leads > 0:
                log.info(f"Found {new_leads} new lead(s) at: {url}")

            if max_depth == 0 or depth < max_depth - 1:
                await self._queue_internal_links(page, url, depth)

        except Exception as e:
            err = str(e)
            if "Download is starting" in err:
                log.debug(f"Skipping download URL: {url}")
            elif "Timeout" in type(e).__name__:
                log.warning(f"Timeout navigating to {url} (after retry)")
            else:
                log.warning(f"Failed to process {url}: {type(e).__name__}: {err[:120]}")
        finally:
            await page.close()

    async def _queue_internal_links(self, page, base_url: str, current_depth: int):
        """
        Queues links for crawling with two key improvements over original:
          - Cross-domain: follows links to ANY .gov.in/.nic.in domain, not just same-netloc
          - Smart filtering: from non-priority pages, only queues contact/tender-relevant links
        """
        links_data = await page.eval_on_selector_all(
            "a[href]",
            "elements => elements.map(e => ({"
            "  href: e.href,"
            "  text: (e.textContent || '').toLowerCase().trim().substring(0, 100)"
            "}))"
        )

        links_added = 0
        # Taper link budget by depth: seed pages get full budget, deeper pages get less.
        # depth 0 → max_links_per_page (80), depth 1 → ~30, depth 2+ → 8
        base = self.config['defaults']['max_links_per_page']
        if current_depth == 0:
            max_links = base
        elif current_depth == 1:
            max_links = max(8, base // 3)
        else:
            max_links = 15
        # If we're already on a contact/officer/tender page, queue all gov links freely
        is_priority_page = self._is_priority_url(base_url)

        for item in links_data:
            try:
                href = item.get('href', '')
                text = item.get('text', '')
                absolute = urljoin(base_url, href)

                if _is_skippable_url(absolute):
                    continue
                # Cross-domain: allow any .gov.in / .nic.in link, not just same-netloc
                if not self._is_gov_domain(absolute):
                    continue
                if absolute in self.visited_urls:
                    continue
                # From generic pages: only follow contact/tender-looking links
                if not is_priority_page and not self._is_priority_url(absolute, text):
                    continue

                self.visited_urls.add(absolute)
                await self.queue.put((absolute, current_depth + 1))
                links_added += 1

                if max_links > 0 and links_added >= max_links:
                    break
            except Exception:
                continue

        if links_added > 0:
            log.debug(f"Queued {links_added} links from {base_url}")
