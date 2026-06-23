"""
GovCrawler Package
A scraper for the india.gov.in Web Directory API.
"""

from .api import get_categories, get_organization_types, get_entries_for_category
from .extractor import extract_from_entries
from .config import HEADERS, TARGET_SUFFIXES

__all__ = [
    "get_categories",
    "get_organization_types",
    "get_entries_for_category",
    "extract_from_entries",
    "HEADERS",
    "TARGET_SUFFIXES",
]
