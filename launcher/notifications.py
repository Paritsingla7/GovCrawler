"""
Windows toast notifications for the GovCrawler control panel.
"""

import logging
from pathlib import Path

from winotify import Notification

log = logging.getLogger(__name__)


def notify(title: str, msg: str, icon_path: Path) -> None:
    try:
        icon = str(icon_path) if icon_path.exists() else ""
        Notification(app_id="GovCrawler", title=title, msg=msg, icon=icon).show()
    except Exception as e:
        log.warning(f"Notification failed: {e}")
