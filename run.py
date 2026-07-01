from pathlib import Path
import tkinter as tk
from tkinter import messagebox
import subprocess
import threading
import webbrowser
import sys
import os
import uvicorn

from portal.main import load_config
config = load_config()

# ==========================================
# SSL Certificate FIX
# ==========================================
import certifi

if getattr(sys, 'frozen', False):
    # PyInstaller creates a temporary folder and stores path in _MEIPASS
    cert_path = os.path.join(sys._MEIPASS, 'certifi', 'cacert.pem')
    os.environ['REQUESTS_CA_BUNDLE'] = cert_path
    os.environ['SSL_CERT_FILE'] = cert_path
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

class CrawlerLauncher:
    def __init__(self, root):
        self.root = root
        self.root.title("GovCrawler Control Panel")
        self.root.geometry("400x400") # Made taller to fit 4 buttons
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        # --- UI Elements ---
        self.status_label = tk.Label(root, text="Status: Ready", fg="black")
        self.status_label.pack(pady=20)
        
        self.btn_download = tk.Button(root, text="1. Download Browsers (First Time)", command=self.trigger_download)
        self.btn_download.pack(pady=10)
        
        self.btn_start = tk.Button(root, text="2. Start Server", command=self.trigger_start_server)
        self.btn_start.pack(pady=10)

        self.btn_browser = tk.Button(root, text="3. Open Web Interface", command=self.open_browser, state=tk.DISABLED)
        self.btn_browser.pack(pady=10)

        # The new Stop Button
        self.btn_stop = tk.Button(root, text="4. Stop Server", command=self.trigger_stop_server, state=tk.DISABLED, fg="red")
        self.btn_stop.pack(pady=10)
        
        self.server_thread = None
        self.uvicorn_server = None

    # --- ACTION: Download Browsers ---
    def trigger_download(self):
        self.btn_download.config(state=tk.DISABLED)
        self.status_label.config(text="Status: Downloading ~600MB... Please wait.", fg="blue")
        threading.Thread(target=self._download_browsers_task, daemon=True).start()

    def _download_browsers_task(self):
        try:
            # 1. Intelligently determine paths and commands based on the environment
            if getattr(sys, 'frozen', False):
                # COMPILED MODE: Running as GovCrawler.exe
                base_dir = Path(sys.executable).parent
                cmd = [sys.executable, "INSTALL_BROWSERS"]
            else:
                # DEV MODE: Running as python launcher.py
                base_dir = Path(__file__).resolve().parent
                cmd = [sys.executable, __file__, "INSTALL_BROWSERS"]

            # 2. Create the browsers folder safely
            browser_dir = base_dir / "playwright_browsers"
            browser_dir.mkdir(parents=True, exist_ok=True)
            
            # 3. Copy the current environment and inject the Playwright path
            env = os.environ.copy()
            env["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_dir)
            
            # 4. Trigger the background process
            subprocess.run(cmd, env=env, check=True)
            
            self.root.after(0, lambda: self.status_label.config(text="Status: Download Complete!", fg="green"))
        except subprocess.CalledProcessError as e:
            self.root.after(0, lambda: self.status_label.config(text="Status: Download Failed. Check Firewall.", fg="red"))
            self.root.after(0, lambda: messagebox.showerror("Error", f"Failed to download binaries.\n{e}"))
        except Exception as e:
            self.root.after(0, lambda: self.status_label.config(text="Status: Unexpected Error.", fg="red"))
            self.root.after(0, lambda: messagebox.showerror("Error", f"An error occurred:\n{e}"))
        finally:
            self.root.after(0, lambda: self.btn_download.config(state=tk.NORMAL))

            
    # --- ACTION: Start Server ---
    def trigger_start_server(self):
        # Update UI States
        self.btn_start.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self.btn_browser.config(state=tk.NORMAL)
        self.status_label.config(text="Status: Server Running.", fg="green")
        
        # Start the server on a background thread so the GUI doesn't freeze
        self.server_thread = threading.Thread(target=self._run_server_task, daemon=True)
        self.server_thread.start()

    def _run_server_task(self):
        # Local imports ensure we don't trigger backend logic until the button is clicked
        from portal.db.models import Database
        from portal.api.server import create_app
        
        db = Database(config)
        app = create_app(config, db)
        
        # 1. Use Uvicorn's Config object instead of uvicorn.run()
        u_config = uvicorn.Config(
            app=app, 
            host=config["api"]["host"], 
            port=config["api"]["port"], 
            log_level="info"
        )
        # 2. Instantiate the server object so we can access it later
        self.uvicorn_server = uvicorn.Server(u_config)
        
        # 3. This runs the server. It completely blocks this thread until stopped.
        self.uvicorn_server.run()
        
        # 4. This code ONLY executes after the server has fully shut down
        self.root.after(0, self._on_server_stopped)

    # --- ACTION: Stop Server ---
    def trigger_stop_server(self):
        if self.uvicorn_server:
            self.status_label.config(text="Status: Stopping Server safely...", fg="orange")
            self.btn_stop.config(state=tk.DISABLED)
            self.btn_browser.config(state=tk.DISABLED)
            
            # This is the exact programmatic equivalent of pressing Ctrl+C
            self.uvicorn_server.should_exit = True

    def _on_server_stopped(self):
        # Reset the UI back to the ready state
        self.status_label.config(text="Status: Server Stopped.", fg="black")
        self.btn_start.config(state=tk.NORMAL)
        self.uvicorn_server = None

    # --- ACTION: Open Browser ---
    def open_browser(self):
        if config["api"]["host"] == "0.0.0.0":
            uri = f"http://127.0.0.1:{config['api']['port']}"
        else:
            uri = f"http://{config['api']['host']}:{config['api']['port']}"

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

    # --- ACTION: Window Close Intercept ---
    def on_closing(self):
        # If the server is actively running, we must wait for it to die
        if self.uvicorn_server and self.server_thread and self.server_thread.is_alive():
            self.status_label.config(text="Status: Shutting down safely... please wait.", fg="orange")
            
            # Disable the whole UI so the user doesn't click anything else
            self.btn_start.config(state=tk.DISABLED)
            self.btn_stop.config(state=tk.DISABLED)
            self.btn_browser.config(state=tk.DISABLED)
            self.btn_download.config(state=tk.DISABLED)
            
            # Send the graceful shutdown signal to Uvicorn
            self.uvicorn_server.should_exit = True
            
            # Start polling to see when the thread actually finishes
            self.root.after(200, self._check_shutdown_complete)
        else:
            # Server isn't running; safe to close immediately
            self.root.destroy()
            sys.exit(0)

    def _check_shutdown_complete(self):
        if self.server_thread and self.server_thread.is_alive():
            # Server is still running its shutdown hooks (closing browsers/DB). Keep waiting.
            self.root.after(200, self._check_shutdown_complete)
        else:
            # Thread is completely dead. Safe to destroy the application.
            self.root.destroy()
            sys.exit(0)

if __name__ == "__main__":
    root = tk.Tk()
    app = CrawlerLauncher(root)
    root.mainloop()