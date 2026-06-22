import sqlite3
import csv
import datetime
import logging

# Set up logging
log = logging.getLogger(__name__)

class LocalStorage:
    """
    Manages the SQLite database for storing visited URLs and captured leads.
    Also handles exporting data to a CSV file.
    """
    def __init__(self, db_file="crawler_session.db"):
        try:
            self.conn = sqlite3.connect(db_file)
            self.cursor = self.conn.cursor()
            self._create_tables()
            log.info(f"Successfully connected to database: {db_file}")
        except sqlite3.Error as e:
            log.error(f"Database connection failed: {e}")
            raise

    def _create_tables(self):
        """Creates the necessary database tables if they don't already exist."""
        try:
            self.cursor.execute("""
                CREATE TABLE IF NOT EXISTS visited_urls (
                    url TEXT PRIMARY KEY,
                    visited_at TEXT
                )
            """)
            self.cursor.execute("""
                CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT UNIQUE,
                    source_url TEXT,
                    page_title TEXT,
                    context_snippet TEXT,
                    captured_at TEXT
                )
            """)
            self.conn.commit()
        except sqlite3.Error as e:
            log.error(f"Failed to create database tables: {e}")
            raise

    def is_visited(self, url: str) -> bool:
        """Checks if a URL has already been visited."""
        self.cursor.execute("SELECT 1 FROM visited_urls WHERE url = ?", (url,))
        return self.cursor.fetchone() is not None

    def mark_visited(self, url: str):
        """Marks a URL as visited in the database."""
        try:
            self.cursor.execute(
                "INSERT INTO visited_urls (url, visited_at) VALUES (?, ?)",
                (url, datetime.datetime.now().isoformat())
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            # This can happen in rare race conditions with many workers. It's safe to ignore.
            pass
        except sqlite3.Error as e:
            log.warning(f"Failed to mark URL as visited: {url} - {e}")

    def save_lead(self, email: str, source_url: str, page_title: str, context_snippet: str) -> bool:
        """Saves a new lead to the database. Returns True if a new lead was inserted."""
        try:
            self.cursor.execute(
                "INSERT INTO leads (email, source_url, page_title, context_snippet, captured_at) VALUES (?, ?, ?, ?, ?)",
                (email, source_url, page_title, context_snippet, datetime.datetime.now().isoformat())
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            # This is expected if the email is already in the database (UNIQUE constraint).
            return False
        except sqlite3.Error as e:
            log.warning(f"Failed to save lead: {email} - {e}")
            return False

    def get_lead_count(self) -> int:
        """Returns the total number of leads in the database."""
        try:
            self.cursor.execute("SELECT COUNT(id) FROM leads")
            result = self.cursor.fetchone()
            return result[0] if result else 0
        except sqlite3.Error as e:
            log.error(f"Failed to get lead count: {e}")
            return 0

    def export_to_csv(self, filename="leads.csv") -> int:
        """Exports all unique leads to a CSV file and returns the number of exported leads."""
        try:
            self.cursor.execute("SELECT email, source_url, page_title, context_snippet, captured_at FROM leads")
            rows = self.cursor.fetchall()
            with open(filename, 'w', newline='', encoding='utf-8') as csvfile:
                csv_writer = csv.writer(csvfile)
                csv_writer.writerow(["Email", "Source URL", "Page Title", "Context/Surrounding Text", "Scraped At"])
                csv_writer.writerows(rows)
            log.info(f"Exported {len(rows)} leads to {filename}")
            return len(rows)
        except (IOError, csv.Error, sqlite3.Error) as e:
            log.error(f"Failed to export leads to CSV: {e}")
            return 0

    def close(self):
        """Closes the database connection."""
        if self.conn:
            self.conn.close()
            log.info("Database connection closed.")
