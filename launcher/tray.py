"""
Tray icon lifecycle for the GovCrawler control panel.

pystray needs its own run loop, kept off the Tkinter thread. Callbacks fire on
that loop's thread, so callers are responsible for marshaling back onto the
Tkinter thread (e.g. via root.after) before touching any widget.
"""

import logging
import threading
from pathlib import Path
from typing import Callable

import pystray
from PIL import Image

log = logging.getLogger(__name__)


def _load_image(icon_path: Path) -> Image.Image:
    try:
        if icon_path.exists():
            return Image.open(str(icon_path))
    except Exception as e:
        log.warning(f"Could not load tray icon from {icon_path}: {e}")
    return Image.new("RGBA", (64, 64), (30, 136, 229, 255))


class TrayController:
    """Owns the pystray icon and the thread it runs on."""

    def __init__(self, icon_path: Path, on_show: Callable[[], None],
                on_open_browser: Callable[[], None], on_quit: Callable[[], None],
                is_running: Callable[[], bool]):
        image = _load_image(icon_path)
        menu = pystray.Menu(
            pystray.MenuItem("Show GovCrawler", lambda icon, item: on_show(), default=True),
            pystray.MenuItem("Open Web Interface", lambda icon, item: on_open_browser(),
                             enabled=lambda item: is_running()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda icon, item: on_quit()),
        )
        self._icon = pystray.Icon("govcrawler", image, "GovCrawler Control Panel", menu)

    def start(self) -> None:
        threading.Thread(target=self._icon.run, daemon=True).start()

    def stop(self) -> None:
        try:
            self._icon.stop()
        except Exception:
            pass
