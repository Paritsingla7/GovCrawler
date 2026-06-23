import sqlite3
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class Database:
    def __init__(self, config: dict):
        db_path = config["database"]["path"]
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA cache_size=10000")
        self._create_schema()
        log.info(f"Database ready: {db_path}")

    def _create_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS domains (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                category_code TEXT NOT NULL,
                category_title TEXT,
                title         TEXT,
                main_url      TEXT,
                contact_url   TEXT,
                raw_data      TEXT,
                imported_at   TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_domains_cat  ON domains(category_code);
            CREATE INDEX IF NOT EXISTS idx_domains_title ON domains(title);

            CREATE TABLE IF NOT EXISTS crawl_jobs (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                category_filter  TEXT,
                title_filter     TEXT,
                domain_ids       TEXT,
                status           TEXT DEFAULT 'pending',
                total_domains    INTEGER DEFAULT 0,
                crawled_domains  INTEGER DEFAULT 0,
                leads_found      INTEGER DEFAULT 0,
                error_message    TEXT,
                created_at       TEXT DEFAULT (datetime('now')),
                started_at       TEXT,
                finished_at      TEXT
            );

            CREATE TABLE IF NOT EXISTS leads (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id          INTEGER NOT NULL,
                domain_id       INTEGER,
                email           TEXT,
                phone           TEXT,
                person_name     TEXT,
                designation     TEXT,
                department      TEXT,
                source_url      TEXT,
                context_snippet TEXT,
                captured_at     TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (domain_id) REFERENCES domains(id),
                FOREIGN KEY (job_id)    REFERENCES crawl_jobs(id)
            );
            CREATE INDEX IF NOT EXISTS idx_leads_job    ON leads(job_id);
            CREATE INDEX IF NOT EXISTS idx_leads_email  ON leads(email);

            CREATE TABLE IF NOT EXISTS visited_urls (
                url        TEXT NOT NULL,
                job_id     INTEGER NOT NULL,
                visited_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (url, job_id),
                FOREIGN KEY (job_id) REFERENCES crawl_jobs(id)
            );
        """)
        self.conn.commit()

    # ── Domain CRUD ───────────────────────────────────────────────────────────

    def insert_domain(self, category_code: str, category_title: str, title: str,
                      main_url: str, contact_url: str, raw_data: dict) -> int:
        cur = self.conn.execute(
            """INSERT INTO domains (category_code, category_title, title, main_url, contact_url, raw_data)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (category_code, category_title, title, main_url, contact_url, json.dumps(raw_data))
        )
        self.conn.commit()
        return cur.lastrowid

    def count_domains(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM domains").fetchone()[0]

    def clear_domains(self):
        self.conn.execute("DELETE FROM domains")
        self.conn.commit()

    def get_categories(self) -> list[dict]:
        rows = self.conn.execute("""
            SELECT category_code AS code, category_title AS title, COUNT(*) AS count
            FROM domains
            GROUP BY category_code
            ORDER BY count DESC
        """).fetchall()
        return [dict(r) for r in rows]

    def get_domains(self, category: str = None, search: str = None,
                    page: int = 1, limit: int = 50) -> tuple[list[dict], int]:
        params: list = []
        conditions: list[str] = []
        if category:
            conditions.append("category_code = ?")
            params.append(category)
        if search:
            conditions.append("title LIKE ?")
            params.append(f"%{search}%")
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        total = self.conn.execute(f"SELECT COUNT(*) FROM domains {where}", params).fetchone()[0]
        offset = (page - 1) * limit
        rows = self.conn.execute(
            f"SELECT * FROM domains {where} ORDER BY title LIMIT ? OFFSET ?",
            params + [limit, offset]
        ).fetchall()
        return [dict(r) for r in rows], total

    def get_domains_by_ids(self, ids: list[int]) -> list[dict]:
        placeholders = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT * FROM domains WHERE id IN ({placeholders})", ids
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Crawl job CRUD ────────────────────────────────────────────────────────

    def create_job(self, domain_ids: list[int], category_filter: str = None,
                   title_filter: str = None) -> int:
        cur = self.conn.execute(
            """INSERT INTO crawl_jobs (domain_ids, category_filter, title_filter,
               total_domains, status)
               VALUES (?, ?, ?, ?, 'pending')""",
            (json.dumps(domain_ids), category_filter, title_filter, len(domain_ids))
        )
        self.conn.commit()
        return cur.lastrowid

    def start_job(self, job_id: int):
        self.conn.execute(
            "UPDATE crawl_jobs SET status='running', started_at=datetime('now') WHERE id=?",
            (job_id,)
        )
        self.conn.commit()

    def finish_job(self, job_id: int, status: str = "done", error: str = None):
        self.conn.execute(
            """UPDATE crawl_jobs
               SET status=?, finished_at=datetime('now'), error_message=?
               WHERE id=?""",
            (status, error, job_id)
        )
        self.conn.commit()

    def increment_job_progress(self, job_id: int, new_leads: int = 0, domain_done: bool = False):
        self.conn.execute(
            """UPDATE crawl_jobs
               SET leads_found = leads_found + ?,
                   crawled_domains = crawled_domains + ?
               WHERE id=?""",
            (new_leads, 1 if domain_done else 0, job_id)
        )
        self.conn.commit()

    def get_job(self, job_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM crawl_jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def list_jobs(self, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM crawl_jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Leads ─────────────────────────────────────────────────────────────────

    def save_lead(self, job_id: int, domain_id: int | None, email: str | None,
                  phone: str | None, person_name: str | None, designation: str | None,
                  department: str | None, source_url: str, context_snippet: str) -> bool:
        try:
            self.conn.execute(
                """INSERT INTO leads
                   (job_id, domain_id, email, phone, person_name, designation,
                    department, source_url, context_snippet)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job_id, domain_id, email, phone, person_name, designation,
                 department, source_url, context_snippet)
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
        except sqlite3.Error as e:
            log.warning(f"save_lead failed: {e}")
            return False

    def get_leads(self, job_id: int, page: int = 1, limit: int = 100) -> tuple[list[dict], int]:
        total = self.conn.execute(
            "SELECT COUNT(*) FROM leads WHERE job_id=?", (job_id,)
        ).fetchone()[0]
        offset = (page - 1) * limit
        rows = self.conn.execute(
            """SELECT l.*, d.title AS domain_title, d.category_code
               FROM leads l
               LEFT JOIN domains d ON d.id = l.domain_id
               WHERE l.job_id=?
               ORDER BY l.captured_at DESC
               LIMIT ? OFFSET ?""",
            (job_id, limit, offset)
        ).fetchall()
        return [dict(r) for r in rows], total

    def get_all_leads_for_export(self, job_id: int) -> list[dict]:
        rows = self.conn.execute(
            """SELECT l.*, d.title AS domain_title, d.category_code, d.category_title
               FROM leads l
               LEFT JOIN domains d ON d.id = l.domain_id
               WHERE l.job_id=?
               ORDER BY l.domain_id, l.captured_at""",
            (job_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Visited URLs ──────────────────────────────────────────────────────────

    def is_visited(self, url: str, job_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM visited_urls WHERE url=? AND job_id=?", (url, job_id)
        ).fetchone()
        return row is not None

    def mark_visited(self, url: str, job_id: int):
        try:
            self.conn.execute(
                "INSERT OR IGNORE INTO visited_urls (url, job_id) VALUES (?, ?)",
                (url, job_id)
            )
            self.conn.commit()
        except sqlite3.Error as e:
            log.debug(f"mark_visited: {e}")

    def get_visited_urls(self, job_id: int) -> set[str]:
        rows = self.conn.execute(
            "SELECT url FROM visited_urls WHERE job_id=?", (job_id,)
        ).fetchall()
        return {r[0] for r in rows}

    def close(self):
        self.conn.close()
