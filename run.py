import os
import sys
import tkinter as tk

import certifi

from portal.config import load_agent_config

config = load_agent_config()

# ==========================================
# SSL Certificate FIX
# ==========================================

if getattr(sys, "frozen", False):
    # PyInstaller creates a temporary folder and stores path in _MEIPASS
    cert_path = os.path.join(sys._MEIPASS, "certifi", "cacert.pem")
    os.environ["REQUESTS_CA_BUNDLE"] = cert_path
    os.environ["SSL_CERT_FILE"] = cert_path
else:
    # Standard runtime environment
    cert_path = certifi.where()

# ==========================================
# NO-CONSOLE CRASH FIX
# ==========================================
# If Windows destroyed the console, redirect all print statements to the void
# so the application doesn't commit suicide on boot.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

# ==========================================
# SECRET ROUTER: Bypass GUI for Background Tasks
# ==========================================
if len(sys.argv) > 1 and sys.argv[1] == "INSTALL_BROWSERS":
    # We are running as a background worker. Do not load Tkinter.
    from playwright.__main__ import main as pw_main

    # Trick Playwright into thinking we typed this in the terminal
    sys.argv = ["playwright", "install", "chromium", "--force"]

    try:
        pw_main()
        sys.exit(0)
    except SystemExit as e:
        sys.exit(e.code)

if __name__ == "__main__":
    from agent.launcher.app import CrawlerLauncher

    root = tk.Tk()
    app = CrawlerLauncher(root, config, entry_script=__file__)
    root.mainloop()
