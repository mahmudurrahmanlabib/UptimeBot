"""SQLite storage for tracked sites.

Uptime Kuma itself has no concept of "this is a client site that should expire",
who added it, or which Slack channel owns it. We keep that metadata here and
treat this table as the source of truth for site *type* and *expiry*.
"""
import sqlite3
import time
from contextlib import contextmanager

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor_id    INTEGER NOT NULL,           -- the Uptime Kuma monitor id
    name          TEXT    NOT NULL,
    url           TEXT    NOT NULL,
    site_type     TEXT    NOT NULL,           -- prod | staging | demo | client
    added_by      TEXT,                       -- Slack user id
    added_by_name TEXT,
    channel_id    TEXT,                        -- where notifications should go
    is_dm         INTEGER DEFAULT 0,
    created_at    REAL    NOT NULL,
    expires_at    REAL,                        -- NULL = permanent
    last_status   TEXT    DEFAULT 'pending',
    warned_expiry INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sites_monitor ON sites(monitor_id);
"""


@contextmanager
def _conn():
    con = sqlite3.connect(config.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA busy_timeout=30000;")
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    with _conn() as con:
        con.executescript(SCHEMA)


def add_site(monitor_id, name, url, site_type, added_by, added_by_name,
             channel_id, is_dm, expires_at):
    now = time.time()
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO sites
               (monitor_id, name, url, site_type, added_by, added_by_name,
                channel_id, is_dm, created_at, expires_at, last_status, warned_expiry)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,0)""",
            (monitor_id, name, url, site_type, added_by, added_by_name,
             channel_id, 1 if is_dm else 0, now, expires_at, "pending"),
        )
        return cur.lastrowid


def list_sites(site_type=None):
    q = "SELECT * FROM sites"
    params = ()
    if site_type:
        q += " WHERE site_type = ?"
        params = (site_type,)
    q += " ORDER BY site_type, name COLLATE NOCASE"
    with _conn() as con:
        return [dict(r) for r in con.execute(q, params).fetchall()]


def all_sites():
    return list_sites()


def get_by_rowid(rowid):
    with _conn() as con:
        r = con.execute("SELECT * FROM sites WHERE id = ?", (rowid,)).fetchone()
        return dict(r) if r else None


def find_sites(identifier):
    """Match by exact url, exact name (case-insensitive), or substring of either."""
    ident = (identifier or "").strip()
    like = f"%{ident}%"
    with _conn() as con:
        rows = con.execute(
            """SELECT * FROM sites
               WHERE url = ? COLLATE NOCASE
                  OR name = ? COLLATE NOCASE
                  OR url LIKE ? COLLATE NOCASE
                  OR name LIKE ? COLLATE NOCASE
               ORDER BY
                  CASE WHEN url = ? COLLATE NOCASE OR name = ? COLLATE NOCASE
                       THEN 0 ELSE 1 END,
                  name COLLATE NOCASE""",
            (ident, ident, like, like, ident, ident),
        ).fetchall()
    return [dict(r) for r in rows]


def remove_site(rowid):
    with _conn() as con:
        con.execute("DELETE FROM sites WHERE id = ?", (rowid,))


def set_status(rowid, status):
    with _conn() as con:
        con.execute("UPDATE sites SET last_status = ? WHERE id = ?", (status, rowid))


def set_expiry(rowid, expires_at):
    with _conn() as con:
        con.execute(
            "UPDATE sites SET expires_at = ?, warned_expiry = 0 WHERE id = ?",
            (expires_at, rowid),
        )


def reclassify(rowid, new_type, expires_at):
    with _conn() as con:
        con.execute(
            "UPDATE sites SET site_type = ?, expires_at = ?, warned_expiry = 0 WHERE id = ?",
            (new_type, expires_at, rowid),
        )


def mark_warned(rowid):
    with _conn() as con:
        con.execute("UPDATE sites SET warned_expiry = 1 WHERE id = ?", (rowid,))


def get_expiring(now, horizon_seconds):
    """Client sites that will expire within the horizon and haven't been warned."""
    with _conn() as con:
        rows = con.execute(
            """SELECT * FROM sites
               WHERE expires_at IS NOT NULL
                 AND warned_expiry = 0
                 AND expires_at > ?
                 AND expires_at <= ?""",
            (now, now + horizon_seconds),
        ).fetchall()
    return [dict(r) for r in rows]


def get_expired(now):
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM sites WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (now,),
        ).fetchall()
    return [dict(r) for r in rows]
