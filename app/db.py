import os
import sqlite3
import threading

DATA_DIR = os.environ.get("DATA_DIR", "/data")

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
-- A store holds the scraping rules for one or more domains
CREATE TABLE IF NOT EXISTS stores (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL,
    domains           TEXT NOT NULL DEFAULT '[]',
    currency          TEXT,
    service           TEXT NOT NULL DEFAULT 'auto',
    last_method       TEXT,
    cookies           TEXT NOT NULL DEFAULT '',
    strategies        TEXT NOT NULL DEFAULT '{}',
    missing_price_oos INTEGER NOT NULL DEFAULT 0,
    created_at        INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT,
    image                TEXT,
    tags                 TEXT NOT NULL DEFAULT '[]',
    target_price         REAL,
    notify_on_drop       INTEGER NOT NULL DEFAULT 1,
    drop_percent         REAL NOT NULL DEFAULT 0,
    notify_back_in_stock INTEGER NOT NULL DEFAULT 0,
    interval_minutes     INTEGER NOT NULL,
    paused               INTEGER NOT NULL DEFAULT 0,
    last_checked         INTEGER,
    created_at           INTEGER NOT NULL
);
-- One product can be tracked at several URLs (stores, or listings in one store)
CREATE TABLE IF NOT EXISTS urls (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id   INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    store_id     INTEGER NOT NULL REFERENCES stores(id),
    url          TEXT NOT NULL,
    url_key      TEXT NOT NULL UNIQUE,
    price        REAL,
    availability TEXT,
    last_checked INTEGER,
    last_error   TEXT
);
-- A row is added only when a URL's price changes
CREATE TABLE IF NOT EXISTS prices (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    url_id     INTEGER NOT NULL REFERENCES urls(id) ON DELETE CASCADE,
    price      REAL NOT NULL,
    checked_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prices_url ON prices(url_id, checked_at);
CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER REFERENCES products(id) ON DELETE CASCADE,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    read       INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def init() -> None:
    global _conn
    os.makedirs(DATA_DIR, exist_ok=True)
    _conn = sqlite3.connect(os.path.join(DATA_DIR, "tracker.db"), check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA foreign_keys = ON")
    _conn.execute("PRAGMA journal_mode = WAL")
    _conn.executescript(SCHEMA)
    _conn.commit()


def query(sql: str, params: tuple = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in _conn.execute(sql, params).fetchall()]


def execute(sql: str, params: tuple = ()) -> int:
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
        return cur.lastrowid


def get_setting(key: str, default: str = "") -> str:
    rows = query("SELECT value FROM settings WHERE key = ?", (key,))
    return rows[0]["value"] if rows else default


def set_setting(key: str, value: str) -> None:
    execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
