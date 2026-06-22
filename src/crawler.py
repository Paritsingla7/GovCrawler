import asyncio
import logging
from playwright.async_api import BrowserContext
from urllib.parse import urljoin, urlparse

from .storage import LocalStorage
from .parser import parse_page_for_leads

log = logging.getLogger(__name__)

class Crawler:
    """
    The main crawler class that encapsulates the entire crawling logic.
    It manages the async workers, the URL queue, and the overall state.
    """
    def __init__(self, config: dict, storage: LocalStorage):
        self.config = config
        self.storage = storage
        self.queue = asyncio.Queue()
        self.visited_urls = set()
        # Load already visited URLs from the database to avoid re-crawling in the same session
        self._load_visited_from_storage()

    def _load_visited_from_storage(self):
        try:
            self.visited_urls.update(
                row[0] for row in self.storage.cursor.execute("SELECT url FROM visited_urls")
            )
            log.info(f"Loaded {len(self.visited_urls)} previously visited URLs from the database.")
        except Exception as e:
            log.error(f"Could not load visited URLs from storage: {e}")

    async def run(self, browser_context: BrowserContext, seed_urls: list[str]):
        """Starts the crawling process with a pool of concurrent workers."""
        for url in seed_urls:
            await self.queue.put((url, 0))

        workers = [
            asyncio.create_task(self._worker(browser_context))
            for _ in range(self.config['defaults']['num_workers'])
        ]

        # Wait for the queue to be processed, then shut down workers
        await self.queue.join()

        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    async def _worker(self, context: BrowserContext):
        """The main worker coroutine that processes URLs from the queue."""
        while True:
            try:
                url, depth = await self.queue.get()
                
                try:
                    await asyncio.wait_for(
                        self._process_url(url, depth, context),
                        timeout=self.config['defaults']['url_process_timeout']
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
        """Contains all logic to process a single URL."""
        max_depth = self.config['defaults']['max_depth']
        if (max_depth > 0 and depth >= max_depth) or url in self.visited_urls:
            return

        self.visited_urls.add(url)
        self.storage.mark_visited(url)
        log.info(f"Crawling (Depth: {depth}): {url}")

        page = await context.new_page()
        try:
            await page.goto(url, timeout=self.config['defaults']['page_timeout'] * 1000, wait_until="domcontentloaded")
            
            html_content = await page.content()
            page_title = await page.title()
            
            leads = parse_page_for_leads(html_content)
            new_leads_found = 0
            for lead in leads:
                if self.storage.save_lead(email=lead['email'], source_url=url, page_title=page_title, context_snippet=lead['context_snippet']):
                    new_leads_found += 1
            if new_leads_found > 0:
                log.info(f"Found {new_leads_found} new lead(s) at: {url}")

            if max_depth == 0 or depth < max_depth:
                await self._queue_internal_links(page, url, depth)
        except Exception as e:
            if "Timeout" in type(e).__name__:
                log.warning(f"Timeout navigating to {url}")
            else:
                log.warning(f"Failed to process {url}: {type(e).__name__}")
        finally:
            await page.close()

    async def _queue_internal_links(self, page, base_url: str, current_depth: int):
        """Finds and queues internal links from a page."""
        links = await page.eval_on_selector_all("a[href]", "elements => elements.map(e => e.href)")
        
        links_added = 0
        max_links = self.config['defaults']['max_links_per_page']

        for link in links:
            try:
                absolute_link = urljoin(base_url, link)
                if urlparse(absolute_link).netloc == urlparse(base_url).netloc and absolute_link not in self.visited_urls:
                    await self.queue.put((absolute_link, current_depth + 1))
                    links_added += 1
                if max_links > 0 and links_added >= max_links:
                    break
            except Exception:
                continue # Ignore malformed URLs
        
        if links_added > 0:
            log.debug(f"Queued {links_added} internal links from {base_url}")
