"""Read-only SQLite access to the existing medicines database."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "database" / "medicines.db"
# Separate file so rebuilding medicines.db (scripts/build_database.py) keeps it.
KENDRA_DB_PATH = ROOT / "data" / "database" / "kendras.db"


def _read_only_uri(path: Path) -> str:
    resolved = path.resolve().as_posix()
    return f"file:{resolved}?mode=ro"


def get_connection(path: Path = DB_PATH) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(f"Database not found: {path}")
    conn = sqlite3.connect(_read_only_uri(path), uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA query_only = ON")
    return conn


@contextmanager
def db_session(path: Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = get_connection(path)
    try:
        yield conn
    finally:
        conn.close()
