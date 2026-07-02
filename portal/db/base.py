"""
Declarative base and SQLite connection pragmas.

Backends (set database.uri in portal/config.yaml):
  SQLite  (dev):    sqlite:///portal/data/govcrawler.db
  PostgreSQL (server): postgresql://user:pass@host:5432/govcrawler
"""

import sqlite3

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base

Base = declarative_base()


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_conn, _):
    if isinstance(dbapi_conn, sqlite3.Connection):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA cache_size=10000")
        cur.close()
