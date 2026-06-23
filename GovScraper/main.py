"""
Entry point for GovCrawler.
This wrapper script ensures backward compatibility for users
who are accustomed to running `python main.py`.
"""

import logging
from cli import interactive_scrape

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

import json

if __name__ == "__main__":
    results = interactive_scrape()
    if results:
        print("\n=== Results ===")
        print(json.dumps(results, indent=2, ensure_ascii=False))
