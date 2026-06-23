import argparse
import asyncio
import logging
import sys

import yaml
from playwright.async_api import async_playwright

from src.crawler import Crawler
from src.seeder import get_seed_urls
from src.storage import LocalStorage

# --- Structured Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("crawler.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger(__name__)


def load_config(path="config.yaml") -> dict:
    """Loads the YAML configuration file."""
    try:
        with open(path, 'r') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        log.error("Configuration file not found. Please create a 'config.yaml'.")
        sys.exit(1)
    except yaml.YAMLError as e:
        log.error(f"Error parsing YAML configuration: {e}")
        sys.exit(1)


def merge_args_with_config(args: argparse.Namespace, config: dict) -> dict:
    """Overrides config defaults with any provided command-line arguments."""
    if args.keyword:
        config['defaults']['keyword'] = args.keyword
    if args.max_depth is not None:
        config['defaults']['max_depth'] = args.max_depth
    if args.max_links is not None:
        config['defaults']['max_links_per_page'] = args.max_links
    if args.workers is not None:
        config['defaults']['num_workers'] = args.workers
    if args.timeout is not None:
        config['defaults']['page_timeout'] = args.timeout
    return config


async def main():
    """Main entry point for the web crawler application."""
    config = load_config()

    parser = argparse.ArgumentParser(description="A modular, production-ready web crawler.")
    parser.add_argument("-k", "--keyword", type=str,
                        help=f"Search keyword to target. Overrides default: '{config['defaults']['keyword']}'")
    parser.add_argument("-d", "--max_depth", type=int,
                        help=f"Maximum crawl depth. 0 for infinite. Overrides default: {config['defaults']['max_depth']}")
    parser.add_argument("-l", "--max_links", type=int,
                        help=f"Max internal links per page. 0 for infinite. Overrides default: {config['defaults']['max_links_per_page']}")
    parser.add_argument("-w", "--workers", type=int,
                        help=f"Number of concurrent workers. Overrides default: {config['defaults']['num_workers']}")
    parser.add_argument("-t", "--timeout", type=int,
                        help=f"Page navigation timeout in seconds. Overrides default: {config['defaults']['page_timeout']}")

    args = parser.parse_args()
    config = merge_args_with_config(args, config)

    storage = None
    try:
        db_uri = config.get('database', {}).get('uri', 'sqlite:///crawler_session.db')
        recrawl_days = config.get('crawler', {}).get('recrawl_days', 30)
        storage = LocalStorage(db_uri=db_uri, recrawl_days=recrawl_days)
        crawler = Crawler(config, storage)

        async with async_playwright() as p:
            # --- Seed Generation ---
            seed_urls = await get_seed_urls(config, storage)
            if not seed_urls:
                log.error("Could not generate any seed URLs. Exiting.")
                return

            # --- Crawling ---
            log.info(f"Starting crawl with {len(seed_urls)} seed URLs.")
            log.info(
                f"Config: Depth={config['defaults']['max_depth']}, Links/Page={config['defaults']['max_links_per_page']}, Workers={config['defaults']['num_workers']}")
            log.info("Press Ctrl+C to stop gracefully and save results.")

            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=config['user_agent'])

            # Run the crawler and listen for KeyboardInterrupt
            crawl_task = asyncio.create_task(crawler.run(context, seed_urls))

            try:
                await crawl_task
            except asyncio.CancelledError:
                log.info("Main task cancelled.")

            await context.close()
            await browser.close()

    except KeyboardInterrupt:
        log.info("Keyboard interrupt detected. Shutting down gracefully...")
    except Exception as e:
        log.critical(f"A critical error occurred in the main event loop: {e}", exc_info=True)
    finally:
        if storage:
            log.info("--- Crawl Finished ---")
            exported_count = storage.export_to_csv()
            log.info(f"Exported {exported_count} unique leads to leads.csv.")
            storage.close()


if __name__ == "__main__":
    asyncio.run(main())
