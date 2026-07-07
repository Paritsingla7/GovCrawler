"""
Tkinter control panel for GovCrawler: state machine, live activity polling,
safe shutdown, and the sv-ttk UI.
"""

import httpx
import logging
import os
import subprocess
import sv_ttk
import sys
import threading
import time
import tkinter as tk
import uvicorn
import webbrowser
from enum import Enum, auto
from tkinter import messagebox, ttk

from portal.paths import BROWSER_PATH, ICON_PATH
from .notifications import notify
from .tray import TrayController

log = logging.getLogger(__name__)


def browsers_installed() -> bool:
    """Heuristic check: has Playwright already installed a chromium build under
    BROWSER_PATH? Checks for the chromium-<rev> folder rather than a specific
    executable name, since that differs per OS (chrome.exe / chrome / Chromium.app)."""
    return BROWSER_PATH.exists() and any(BROWSER_PATH.glob("chromium-*"))


class AppState(Enum):
    IDLE = auto()
    STARTING = auto()
    RUNNING = auto()
    CHECKING = auto()  # briefly asking the server "is anything active?"
    CANCELLING = auto()  # cancel-all issued, waiting for it to take effect
    DRAINING = auto()  # waiting for active jobs/campaigns to actually stop
    STOPPING = auto()  # uvicorn graceful shutdown in progress


STATE_LABELS = {
    AppState.IDLE: ("Idle", "#808080"),
    AppState.STARTING: ("Starting…", "#4fa8d8"),
    AppState.RUNNING: ("Running", "#4caf50"),
    AppState.CHECKING: ("Checking active jobs…", "#4fa8d8"),
    AppState.CANCELLING: ("Cancelling active work…", "#e0a030"),
    AppState.DRAINING: ("Stopping active work…", "#e0a030"),
    AppState.STOPPING: ("Stopping server…", "#e0a030"),
}

DRAIN_TIMEOUT_SECONDS = 180
POLL_INTERVAL_MS = 1500


class CrawlerLauncher:
    def __init__(self, root, config: dict, entry_script: str):
        self.root = root
        self.config = config
        self.entry_script = entry_script  # path re-invoked as a subprocess for INSTALL_BROWSERS

        self.root.title("GovCrawler Control Panel")
        self.root.geometry("440x480")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.on_window_close)

        sv_ttk.set_theme("dark")
        try:
            if ICON_PATH.exists():
                self.root.iconbitmap(default=str(ICON_PATH))
        except Exception as e:
            log.warning(f"Could not set window icon: {e}")

        self.state = AppState.IDLE
        self._browsers_ok = browsers_installed()
        self.server_thread: threading.Thread | None = None
        self.uvicorn_server: uvicorn.Server | None = None
        self.http: httpx.Client | None = None
        self.tray: TrayController | None = None
        self._full_quit_requested = False
        self._drain_deadline: float | None = None
        self._prev_jobs: set[int] = set()
        self._prev_campaigns: set[int] = set()

        self._build_ui()
        self._render_state()

    # --- UI construction ------------------------------------------------

    def _build_ui(self):
        pad = {"padx": 16, "pady": 8}

        header = ttk.Frame(self.root)
        header.pack(fill="x", padx=16, pady=(16, 4))
        ttk.Label(header, text="GovCrawler", font=("Segoe UI", 16, "bold")).pack(side="left")
        status_frame = ttk.Frame(header)
        status_frame.pack(side="right")
        self.status_dot = ttk.Label(status_frame, text="●", foreground="#808080", font=("Segoe UI", 12))
        self.status_dot.pack(side="left", padx=(0, 4))
        self.status_text = ttk.Label(status_frame, text="Idle")
        self.status_text.pack(side="left")

        pw_frame = ttk.LabelFrame(self.root, text="Playwright Browsers")
        pw_frame.pack(fill="x", **pad)
        self.pw_status_lbl = ttk.Label(pw_frame, text="")
        self.pw_status_lbl.pack(anchor="w", padx=10, pady=(8, 4))
        self.btn_download = ttk.Button(pw_frame, text="Download Browsers", command=self.trigger_download)
        self.btn_download.pack(anchor="w", padx=10, pady=(0, 10))

        server_frame = ttk.LabelFrame(self.root, text="Server")
        server_frame.pack(fill="x", **pad)
        btn_row = ttk.Frame(server_frame)
        btn_row.pack(fill="x", padx=10, pady=10)
        self.btn_toggle = ttk.Button(btn_row, text="Start Server", command=self.on_toggle_server,
                                     style="Accent.TButton")
        self.btn_toggle.pack(side="left")
        self.btn_browser = ttk.Button(btn_row, text="Open Web Interface", command=self.open_browser,
                                      state=tk.DISABLED)
        self.btn_browser.pack(side="left", padx=(8, 0))

        activity_frame = ttk.LabelFrame(self.root, text="Activity")
        activity_frame.pack(fill="x", **pad)
        self.activity_lbl = ttk.Label(activity_frame, text="Server not running")
        self.activity_lbl.pack(anchor="w", padx=10, pady=8)
        self.status_detail_lbl = ttk.Label(activity_frame, text="", wraplength=380, foreground="#e0a030")
        self.status_detail_lbl.pack(anchor="w", padx=10, pady=(0, 8))

        ttk.Label(
            self.root,
            text="Closing this window minimizes to the tray. Use Stop Server to fully quit.",
            foreground="#808080", wraplength=400, justify="left",
        ).pack(fill="x", padx=16, pady=(8, 16))

    def _render_state(self):
        text, color = STATE_LABELS[self.state]
        self.status_text.config(text=text)
        self.status_dot.config(foreground=color)

        if self._browsers_ok:
            self.pw_status_lbl.config(text="Browsers installed", foreground="#4caf50")
            self.btn_download.config(text="Re-download")
        else:
            self.pw_status_lbl.config(text="Required before starting the server", foreground="#e0a030")
            self.btn_download.config(text="Download Browsers (~600MB)")
        self.btn_download.config(state=tk.NORMAL if self.state == AppState.IDLE else tk.DISABLED)

        can_toggle = self.state in (AppState.IDLE, AppState.RUNNING) and \
                     (self.state != AppState.IDLE or self._browsers_ok)
        self.btn_toggle.config(
            text="Start Server" if self.state == AppState.IDLE else "Stop Server",
            state=tk.NORMAL if can_toggle else tk.DISABLED,
        )

        browsable_states = (AppState.RUNNING, AppState.CHECKING, AppState.CANCELLING, AppState.DRAINING)
        self.btn_browser.config(state=tk.NORMAL if self.state in browsable_states else tk.DISABLED)

        if self.state == AppState.IDLE:
            self.activity_lbl.config(text="Server not running")
            self.status_detail_lbl.config(text="")
        elif self.state == AppState.STARTING:
            self.activity_lbl.config(text="Starting…")
            self.status_detail_lbl.config(text="")

    # --- Notifications ----------------------------------------------------

    def _toast(self, title: str, msg: str):
        notify(title, msg, ICON_PATH)

    # --- HTTP helper --------------------------------------------------------

    def _base_url(self) -> str:
        host = self.config["api"]["host"]
        display_host = "127.0.0.1" if host == "0.0.0.0" else host
        return f"http://{display_host}:{self.config['api']['port']}"

    def _api_async(self, method: str, path: str, on_done, **kwargs):
        def task():
            try:
                resp = self.http.request(method, path, timeout=5, **kwargs)
                resp.raise_for_status()
                data = resp.json()
                self.root.after(0, on_done, data, None)
            except Exception as e:
                self.root.after(0, on_done, None, e)

        threading.Thread(target=task, daemon=True).start()

    # --- ACTION: Download Browsers ------------------------------------------

    def trigger_download(self):
        self.btn_download.config(state=tk.DISABLED)
        self.status_detail_lbl.config(text="Downloading Playwright browsers (~600MB)… please wait.",
                                      foreground="#4fa8d8")
        threading.Thread(target=self._download_browsers_task, daemon=True).start()

    def _download_browsers_task(self):
        try:
            if getattr(sys, 'frozen', False):
                cmd = [sys.executable, "INSTALL_BROWSERS"]
            else:
                cmd = [sys.executable, self.entry_script, "INSTALL_BROWSERS"]

            BROWSER_PATH.mkdir(parents=True, exist_ok=True)

            env = os.environ.copy()
            env["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSER_PATH)

            subprocess.run(cmd, env=env, check=True)
            self.root.after(0, self._on_download_done, True, None)
        except subprocess.CalledProcessError as e:
            self.root.after(0, self._on_download_done, False, f"Download failed. Check your firewall.\n{e}")
        except Exception as e:
            self.root.after(0, self._on_download_done, False, f"Unexpected error:\n{e}")

    def _on_download_done(self, ok: bool, error: str | None):
        self._browsers_ok = browsers_installed()
        self._render_state()
        if ok:
            self.status_detail_lbl.config(text="Browsers downloaded successfully.", foreground="#4caf50")
            self._toast("GovCrawler", "Playwright browsers downloaded.")
        else:
            self.status_detail_lbl.config(text="")
            messagebox.showerror("Download failed", error)
            self._toast("GovCrawler", "Browser download failed.")

    # --- ACTION: Start Server ------------------------------------------------

    def on_toggle_server(self):
        if self.state == AppState.IDLE:
            self.trigger_start_server()
        elif self.state == AppState.RUNNING:
            self._request_quit(full_quit=False)

    def trigger_start_server(self):
        if not self._browsers_ok:
            messagebox.showwarning("Playwright required",
                                   "Download the Playwright browsers before starting the server.")
            return

        self.state = AppState.STARTING
        self._render_state()

        self.http = httpx.Client(base_url=self._base_url())
        self._prev_jobs, self._prev_campaigns = set(), set()

        if self.tray is None:
            self._setup_tray()

        self.server_thread = threading.Thread(target=self._run_server_task, daemon=True)
        self.server_thread.start()
        self._wait_for_server_ready(attempts=15)

    def _run_server_task(self):
        from portal.db import Database
        from portal.api.server import create_app

        try:
            db = Database(self.config)
            app = create_app(self.config, db)

            u_config = uvicorn.Config(
                app=app,
                host=self.config["api"]["host"],
                port=self.config["api"]["port"],
                log_level="info",
            )
            self.uvicorn_server = uvicorn.Server(u_config)
            self.uvicorn_server.run()
        except Exception as e:
            log.error(f"Server crashed: {e}", exc_info=True)
            self.root.after(0, self._on_server_crashed, str(e))

    def _on_server_crashed(self, error: str):
        self.uvicorn_server = None
        self.http = None
        self.state = AppState.IDLE
        self._render_state()
        messagebox.showerror("Server error", f"The server stopped unexpectedly:\n{error}")
        self._toast("GovCrawler", "Server crashed unexpectedly.")

    def _wait_for_server_ready(self, attempts: int):
        def task():
            try:
                self.http.get("/api/categories", timeout=2)
                self.root.after(0, self._on_server_ready)
            except Exception:
                if attempts > 1:
                    self.root.after(300, lambda: self._wait_for_server_ready(attempts - 1))
                else:
                    self.root.after(0, self._on_server_ready)

        threading.Thread(target=task, daemon=True).start()

    def _on_server_ready(self):
        if self.state != AppState.STARTING:
            return
        self.state = AppState.RUNNING
        self._render_state()
        self._toast("GovCrawler", "Server started.")
        self._schedule_poll()

    # --- Live activity polling ----------------------------------------------

    def _schedule_poll(self):
        if self.state != AppState.RUNNING:
            return
        self._api_async("GET", "/api/system/activity", self._on_activity)

    def _on_activity(self, data, err):
        if err is not None:
            log.debug(f"activity poll failed: {err}")
        else:
            self._update_activity_ui(data)
            self._check_for_completions(data)
        if self.state == AppState.RUNNING:
            self.root.after(POLL_INTERVAL_MS, self._schedule_poll)

    def _update_activity_ui(self, data: dict):
        n = data["total_active"]
        if n == 0:
            self.activity_lbl.config(text="No active jobs")
            return
        parts = []
        if data["crawl_jobs"]:
            parts.append(f"{len(data['crawl_jobs'])} crawl job(s)")
        if data["campaigns"]:
            parts.append(f"{len(data['campaigns'])} campaign(s)")
        self.activity_lbl.config(text=f"Active: {', '.join(parts)}")

    def _check_for_completions(self, data: dict):
        cur_jobs = {j["id"] for j in data["crawl_jobs"]}
        cur_campaigns = {c["id"] for c in data["campaigns"]}

        for job_id in self._prev_jobs - cur_jobs:
            self._api_async("GET", f"/api/jobs/{job_id}",
                            lambda d, e, jid=job_id: self._notify_job_done(jid, d, e))
        for cid in self._prev_campaigns - cur_campaigns:
            self._api_async("GET", f"/api/campaigns/{cid}",
                            lambda d, e, cid=cid: self._notify_campaign_done("Campaign", cid, d, e))

        self._prev_jobs, self._prev_campaigns = cur_jobs, cur_campaigns

    def _notify_job_done(self, job_id: int, data, err):
        if err is not None or not data:
            return
        self._toast("GovCrawler", f"Crawl job #{job_id} {data['status']} — {data['leads_found']} leads found.")

    def _notify_campaign_done(self, label: str, campaign_id: int, data, err):
        if err is not None or not data:
            return
        self._toast("GovCrawler", f"{label} '{data['name']}' is now {data['status']}.")

    # --- ACTION: Safe shutdown ------------------------------------------------

    def _request_quit(self, full_quit: bool):
        if self.state == AppState.IDLE:
            self._hard_exit()
            return
        if self.state != AppState.RUNNING:
            return

        self._full_quit_requested = full_quit
        self.state = AppState.CHECKING
        self._render_state()
        self._api_async("GET", "/api/system/activity", self._on_activity_for_shutdown)

    def _on_activity_for_shutdown(self, data, err):
        if err is not None:
            log.warning(f"Could not check activity before shutdown: {err}")
            self._begin_graceful_shutdown()
            return

        if data["total_active"] == 0:
            self._begin_graceful_shutdown()
            return

        self.state = AppState.RUNNING
        self._render_state()

        labels = [j["label"] for j in data["crawl_jobs"]]
        labels += [f"Campaign: {c['name']}" for c in data["campaigns"]]
        preview = "\n".join(f"• {label}" for label in labels[:6])
        if len(labels) > 6:
            preview += f"\n… and {len(labels) - 6} more"

        proceed = messagebox.askyesno(
            "Active work in progress",
            f"{data['total_active']} job(s)/campaign(s) are currently running:\n\n{preview}\n\n"
            "Stop them and shut down the server?",
        )
        if proceed:
            self._begin_cancel_and_drain()
        else:
            self._schedule_poll()

    def _begin_cancel_and_drain(self):
        self.state = AppState.CANCELLING
        self._render_state()
        self._api_async("POST", "/api/system/cancel-all", self._on_cancel_all_issued)

    def _on_cancel_all_issued(self, data, err):
        if err is not None:
            messagebox.showerror("Error", f"Failed to cancel active work:\n{err}")
            self.state = AppState.RUNNING
            self._render_state()
            self._schedule_poll()
            return

        self._drain_deadline = time.monotonic() + DRAIN_TIMEOUT_SECONDS
        self.state = AppState.DRAINING
        self._render_state()
        self.status_detail_lbl.config(text="Stopping active job(s)… this can take up to ~90s for email campaigns.")
        self._poll_drain()

    def _poll_drain(self):
        self._api_async("GET", "/api/system/activity", self._on_drain_activity)

    def _on_drain_activity(self, data, err):
        if self.state != AppState.DRAINING:
            return

        if err is None and data["total_active"] == 0:
            self._begin_graceful_shutdown()
            return

        remaining = data["total_active"] if data else "an unknown number of"
        self.status_detail_lbl.config(text=f"Stopping {remaining} active job(s)… this can take up to ~90s.")

        if time.monotonic() >= self._drain_deadline:
            proceed = messagebox.askyesno(
                "Still stopping",
                "Some jobs haven't stopped yet (a running email campaign can take up to 90s "
                "between sends to notice it was cancelled).\n\nForce-stop the server anyway?",
            )
            if proceed:
                self._begin_graceful_shutdown()
                return
            self._drain_deadline = time.monotonic() + DRAIN_TIMEOUT_SECONDS

        self.root.after(POLL_INTERVAL_MS, self._poll_drain)

    def _begin_graceful_shutdown(self):
        self.state = AppState.STOPPING
        self._render_state()
        if self.uvicorn_server:
            self.uvicorn_server.should_exit = True
            self.root.after(200, self._check_shutdown_complete)
        else:
            self._on_server_stopped()

    def _check_shutdown_complete(self):
        if self.server_thread and self.server_thread.is_alive():
            self.root.after(200, self._check_shutdown_complete)
        else:
            self._on_server_stopped()

    def _on_server_stopped(self):
        self.uvicorn_server = None
        self.http = None
        self._toast("GovCrawler", "Server stopped.")
        if self._full_quit_requested:
            self._hard_exit()
        else:
            self.state = AppState.IDLE
            self._render_state()

    def _hard_exit(self):
        if self.tray is not None:
            self.tray.stop()
        self.root.destroy()
        sys.exit(0)

    # --- ACTION: Open Browser ------------------------------------------------

    def open_browser(self):
        uri = self._base_url()

        if sys.platform.startswith("linux"):
            # PyInstaller overrides LD_LIBRARY_PATH with bundled libs; child processes
            # like xdg-open inherit it and /bin/sh crashes with a readline symbol error.
            # Restore the original value the bootloader saved before spawning.
            env = os.environ.copy()
            orig = env.get("LD_LIBRARY_PATH_ORIG")
            if orig is not None:
                env["LD_LIBRARY_PATH"] = orig
            else:
                env.pop("LD_LIBRARY_PATH", None)
            subprocess.Popen(["xdg-open", uri], env=env)
        else:
            webbrowser.open(uri)

    # --- Tray icon -------------------------------------------------------

    def _setup_tray(self):
        self.tray = TrayController(
            icon_path=ICON_PATH,
            on_show=lambda: self.root.after(0, self._restore_window),
            on_open_browser=lambda: self.root.after(0, self.open_browser),
            on_quit=lambda: self.root.after(0, self._request_quit, True),
            is_running=lambda: self.state == AppState.RUNNING,
        )
        self.tray.start()

    def _restore_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    # --- ACTION: Window Close Intercept ---------------------------------------

    def on_window_close(self):
        if self.state == AppState.IDLE:
            self._hard_exit()
        else:
            self.root.withdraw()
