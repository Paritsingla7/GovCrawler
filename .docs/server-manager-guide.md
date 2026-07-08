# Server Manager's Guide

A practical, step-by-step runbook for whoever operates the GovCrawler **cloud server** and rolls the
**agent app** out to operators. If you want the technical "why," the other docs in `.docs/` cover that —
this one is the "what do I actually type" guide.

**The two pieces, in one paragraph:** GovCrawler is split into a **cloud server** (one VPS, shared by
everyone — the database, the admin dashboard, user accounts) and an **agent app** (a small desktop program
each operator installs on their own machine, which does the actual crawling and talks back to the cloud
server). You set up the cloud server once. You then hand the agent app + your server's URL to each
operator, and they run it on their own machine — you never need to touch their computer.

---

## Part 1 — Deploying the cloud server

### 1A. Production (a real VPS)

**Prerequisites**
- A VPS (any provider) running Linux, with [Docker](https://docs.docker.com/engine/install/) and the
  Docker Compose plugin installed.
- A domain name (e.g. `crawler.yourcompany.com`) with its DNS **A record** pointed at the VPS's IP address.
  TLS (HTTPS) is issued automatically once this is in place — no certificate to buy or upload.
- Port 80 and 443 open on the VPS's firewall (for the automatic TLS + the app itself); port 22 for your own
  SSH access.

**Step by step**

1. SSH into the VPS and get the code:
   ```bash
   git clone <your-repo-url> GovCrawler
   cd GovCrawler/deploy
   ```
2. Create your secrets file from the template:
   ```bash
   cp .env.example .env
   ```
3. Edit `.env` and fill in every value. Generate the two random secrets with:
   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(48))"                              # JWT_SECRET
   python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" # CREDENTIAL_ENC_KEY
   ```
   Set `DOMAIN` to your real domain name (no `https://` prefix, just the hostname). Pick your own
   `POSTGRES_PASSWORD` and `GOVCRAWLER_APP_PASSWORD` — any strong random strings. **Do not** commit this
   file or share it outside your ops team — treat it like a password vault entry.
4. Bring the stack up:
   ```bash
   docker compose up --build -d
   ```
   This starts five containers: the database, a one-time migration step, the API server, the email
   dispatcher, and a TLS-terminating proxy. The first build takes a few minutes.
5. Create your first admin account:
   ```bash
   docker compose exec api python -m portal create-admin you@yourcompany.com
   ```
   It will prompt you to type (and confirm) a password. Keep it somewhere safe — this is the account you'll
   use to log into the admin dashboard.
6. Verify it's alive:
   ```bash
   curl https://your-domain.example.com/healthz     # should print {"status":"ok"}
   ```
   Then open `https://your-domain.example.com/admin/dashboard` in a browser and log in with the admin
   account from step 5.

**You're done.** The cloud server is now live at `https://your-domain.example.com` — this is the URL
you'll give to every operator when they set up their agent app (Part 4).

**Ongoing operations, in brief** (each has its own detailed runbook in `deploy/`):
- **Logs:** `docker compose logs -f api` (or `dispatcher`, `db`, `proxy`).
- **Restart after a config change:** `docker compose up -d` (rebuilds only what changed).
- **Update to a new version:** `git pull`, then `docker compose up --build -d` again (migrations run
  automatically via the `migrate` service).
- **Backups:** a nightly database backup is documented in [`deploy/BACKUP.md`](../deploy/BACKUP.md) — set
  it up via cron on the VPS; **do this before you have real client data on the server.**
- **Point-in-time recovery** (tighter than nightly backups): [`deploy/PITR.md`](../deploy/PITR.md).
- **Hardening checklist + secret rotation:** [`deploy/SECURITY.md`](../deploy/SECURITY.md) — run
  `deploy/harden-vps.sh` once as root shortly after provisioning the VPS.
- **Full technical reference:** [`deployment.md`](deployment.md).

### 1B. Development / test environment (no VPS, no domain)

Use this to try things out, test an update before rolling it to production, or develop against a local
server. Two options, from lightest to most production-like:

**Option 1 — bare Python, fastest to start, no Docker:**
```bash
git clone <your-repo-url> && cd GovCrawler
python -m venv venv && source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements/cloud.txt
python -m portal serve
```
This starts the cloud server on `http://127.0.0.1:8001` using a local SQLite database file (no Postgres
needed) — good enough for testing the admin dashboard, permissions, or a config change. Create an admin
with `python -m portal create-admin you@example.com` in a second terminal.

If you're also running an agent (`python run.py`) on this **same machine** to test the full flow, note the
cloud and the agent's local BFF both default to port 8001 — they'll collide. Edit the cloud's
`portal/config.yaml` (created on first run from `default_config.yaml`) and change `api.port` to something
else, e.g. `8000`, before starting it.

**Option 2 — the same Docker stack, just pointed at `localhost`:** follow 1A's steps exactly, but set
`DOMAIN=localhost` in `.env` and skip pointing DNS anywhere. This is the closest thing to a production
rehearsal (same containers, same Postgres) — use it before rolling out a real upgrade. One catch: Caddy
can't get a real Let's Encrypt certificate for `localhost` (or a bare IP) — it isn't a public, resolvable
name — so it automatically falls back to serving HTTPS with its own **internal, untrusted** CA instead of
plain HTTP. Any client that validates certificates normally (an agent, a browser without that CA imported)
will fail with `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`. Don't try to work around
this by trusting Caddy's internal CA — it's extra setup for no benefit locally. Instead, reach the `api`
container directly over plain HTTP, bypassing Caddy entirely: `deploy/docker-compose.yml`'s `api` service
already publishes `127.0.0.1:18001` (container port 8001, remapped — the agent's own local BFF binds 8001
on this same machine, so the host side has to differ) for exactly this.

Either way, agents can point at this test server the same way they'd point at production (Part 4) — use
`http://127.0.0.1:8001` (or whatever port you chose above) as the Cloud Server URL for Option 1, and
`http://127.0.0.1:18001` for Option 2. Anyone testing this way must be on the same machine as the test
server, since the port is loopback-only and isn't exposed to the network or the internet.

---

## Part 2 — Using the admin dashboard

Log in at `https://your-domain.example.com/` with an admin account, then open **🛡️ Admin** in the top nav
(or go straight to `/admin/dashboard`). Admin is only ever reachable on the cloud server directly — it does
not appear inside any operator's agent app.

### Creating users and admins
Under **Users & Permissions**, click **+ New User**. Fill in their email, set a temporary password (they can
be given a new one later — see below), assign a **Role**, and check **Is admin** only for people who should
have full access to everything (admins bypass all permission checks).

Built-in roles:

| Role | Can do |
|------|--------|
| **Admin** | Everything (user/role management, settings, all data) |
| **Operator** | Run crawls, manage leads/campaigns/templates/credentials/blacklist — the day-to-day role for most people |
| **Viewer** | Browse domains and leads only, no editing |

### Fine-tuning one person's access
Every user's row has a **Permissions** button — this opens a grid of every individual permission
(`crawl.run`, `leads.export`, `settings.manage`, etc.), each showing whether it's *Inherited from role*,
explicitly *Granted*, or explicitly *Denied*. Use this when someone needs one extra capability without
promoting their whole role (e.g. an Operator who should also be able to view the audit log), or needs one
capability *removed* without demoting them. Changes apply immediately, the next time that person's session
refreshes (within about 15 minutes at the outside).

### Resetting a password
Click **Reset PW** on a user's row and set a new one — there's no self-service "forgot password" flow yet,
so this is the way to help someone who's locked out.

### The audit log
The **Audit Log** panel records who did what, when: logins, permission changes, every crawl job
created/cancelled/resumed, every lead/campaign/template/credential/blacklist edit. Filter by user, by an
action prefix (e.g. type `campaign.` to see only campaign-related events), or by date range. Use this to
answer "who changed this setting" or "who exported this data" questions.

### Settings (crawl policy)
The **Settings** page (reachable from the main nav, not just admin — gated by the `settings.manage`
permission) controls crawl depth, rate limiting, which domain suffixes are crawled, extraction rules, and
lead-scoring weights. These apply to **every** crawl on **every** agent — there's one shared policy, not a
per-machine one. See [`configuration.md`](configuration.md) if you need the meaning of a specific field.

### Seeding the domain catalog
Before anyone can crawl, the catalog of government domains needs to be populated once. From the admin
dashboard's dashboard page (or via CLI on the server:
`docker compose exec api python -m portal import-json <path>` for a pre-generated file, or
`... import` to pull fresh from the live `india.gov.in` API) — see [`README.md`](../README.md) for the
one-time `GovScraper` step that generates that file.

### Everything else
Admins also have full access to the same Leads/Campaigns/Settings pages every operator uses (via their own
agent app) — an admin who wants to browse leads directly on the server can do so from
`https://your-domain.example.com/leads` with their own login.

---

## Part 3 — Packaging the agent app for your operators

Operators never touch the server. What they need from you is **one file** (the agent app) and **one piece
of information** (your cloud server's URL).

### Getting the agent app (pick one)

**Option A — use a pre-built release (recommended).** Every tagged version automatically builds signed
packages for Windows, macOS, and Linux (`.github/workflows/release.yaml`). Go to the repository's **Releases**
page, download `GovCrawler-windows-vX.Y.Z.zip` (or `-macos-`/`-linux-`) for each operator's OS, and send them
that zip file (email, shared drive, internal download page — however you distribute files to staff).

**Option B — build it yourself**, if you've made local changes or Releases aren't set up for this repo:
```bash
pip install -r requirements/agent.txt
pip install pyinstaller
pyinstaller GovCrawler.spec --clean
```
The finished app appears in `dist/GovCrawler/` — zip that whole folder and send it the same way as Option A.
Building must be done **on the same OS** you're targeting (build on Windows for a Windows package, etc.).

### What to tell each operator
Just two things:
1. Where to unzip and run the app (`GovCrawler.exe` on Windows, `GovCrawler` on macOS/Linux — no
   installer, no admin rights needed).
2. Your cloud server's URL, e.g. `https://your-domain.example.com` — they'll be asked for this exactly once,
   the first time they run it.

You do **not** need to create their user accounts for them ahead of time unless you want to — you can also
just tell them their login email and a temporary password from Part 2.

---

## Part 4 — Setting up an agent (what to tell your operators)

Give your operators this section directly — it's everything they need.

1. **Unzip** the file you were given, anywhere convenient (Desktop, Documents — no installation needed).
2. **Run `GovCrawler.exe`** (or the equivalent for your OS). The **GovCrawler Control Panel** window opens.
3. **First run only:** a **"Cloud Server URL"** prompt appears. Enter the exact URL your server manager gave
   you (e.g. `https://crawler.yourcompany.com`) and press OK. You won't be asked again on this machine.
4. Click **Download Browsers (~600MB)** — a one-time download needed for the crawler itself. Wait for it to
   finish (a toast notification confirms it).
5. Click **Start Server**. A **Sign in** dialog appears — enter the email and password your server manager
   gave you. (Your password is checked directly against the cloud server, the same account works if you
   later use the admin dashboard directly, if you have access to it.)
6. Once signed in, click **Open Web Interface** — your browser opens the GovCrawler dashboard. This is
   where you pick domains, start crawls, browse leads, and build campaigns, exactly like the admin
   dashboard except scoped to what your role allows.
7. **To stop:** click **Stop Server** (or just close the window — it minimizes to the system tray; use
   **Stop Server** or the tray icon's quit option to fully shut it down). A running crawl is asked to wrap
   up cleanly first (up to 3 minutes) rather than being killed outright.

**Running a crawl:** pick categories/states/org types (or paste in custom URLs) on the dashboard, click
Start — progress shows live. Leads land in the shared pool as they're found, visible to everyone with
`leads.view`, not just you. Closing your laptop pauses (not loses) a running crawl — reopen the app and hit
**Resume** on that job; it'll pick up exactly where it left off. **Only the machine that started a crawl can
resume it** — if you start a job and someone else tries to resume it from their own machine, they'll be
told it belongs to a different agent.

**Multiple operators at once:** every operator's agent app is completely independent — there's no limit
tied to the software itself (only to your VPS's capacity). Everyone shares the same leads pool and domain
catalog, but nobody can see or affect another operator's currently-running crawl.

### Developer/test setup for an agent
If you're testing against a dev cloud server (Part 1B) instead of production, run the agent **from source**
instead of the packaged exe:
```bash
pip install -r requirements/agent.txt
playwright install chromium
python run.py
```
Everything else is identical — you'll still get the Cloud Server URL prompt (enter your dev server's
address, e.g. `http://127.0.0.1:8001`), sign-in dialog, etc.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Agent's first-run prompt won't accept my URL | Make sure it includes `https://` (or `http://` for a local dev server) and has no trailing slash |
| "Not logged in" errors right after signing in | Your server clock and the agent machine's clock may be too far apart, or your account may be disabled — check with your admin |
| Sign-in works but every page shows an error | Your agent can't reach the cloud server over the network — check the URL, check the VPS is up (`/healthz`), check any local firewall/VPN |
| "This job was started by a different agent" | Expected — a crawl can only be resumed from the exact machine that started it. Start a fresh job instead |
| Admin dashboard link inside the agent app opens a blank/error page | It opens your cloud server's admin page in your normal browser, which requires **its own separate login** — sign in there directly |
| Playwright/browser download fails | Check disk space (~600MB needed) and that the machine has internet access; retry via **Download Browsers** |
| `docker compose up` fails on the VPS | Run `docker compose logs` to see which service failed; most often a missing/blank value in `.env` |

For anything not covered here: [`architecture.md`](architecture.md) for how the system fits together,
[`resilience.md`](resilience.md) for what happens during outages/crashes, and
[`api-reference.md`](api-reference.md) for the full endpoint/permission reference.
