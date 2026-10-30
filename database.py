"""SQLite storage for tracked sites.

Uptime Kuma itself has no concept of "this is a client site that should expire",
who added it, or which Slack channel owns it. We keep that metadata here and
treat this table as the source of truth for site *type* and *expiry*.
"""
import sqlite3
import time
from contextlib import contextmanager

import config
import utils

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor_id    INTEGER NOT NULL,           -- the Uptime Kuma monitor id
    name          TEXT    NOT NULL,
    url           TEXT    NOT NULL,
    site_type     TEXT    NOT NULL,           -- permanent | prod | staging | demo | client
    added_by      TEXT,                       -- Slack user id
    added_by_name TEXT,
    channel_id    TEXT,                        -- where notifications should go
    is_dm         INTEGER DEFAULT 0,           -- 1 = personal: alerts go to added_by's DM
    created_at    REAL    NOT NULL,
    expires_at    REAL,                        -- NULL = permanent
    last_status   TEXT    DEFAULT 'pending',
    warned_expiry INTEGER DEFAULT 0,
    down_since    REAL,                        -- start of the current outage (NULL = none)
    alert_channel TEXT,                        -- where that outage's DOWN alert was posted
    alert_ts      TEXT                         -- its message ts, to thread the recovery
);
CREATE INDEX IF NOT EXISTS idx_sites_monitor ON sites(monitor_id);
CREATE INDEX IF NOT EXISTS idx_sites_channel ON sites(channel_id);

CREATE TABLE IF NOT EXISTS channel_settings (
    channel_id TEXT PRIMARY KEY,
    mention    TEXT,                           -- who DOWN alerts ping, e.g. <!here>
    updated_by TEXT,
    updated_at REAL
);
"""

# Columns added after the first release; older databases get them on startup.
_ADDED_COLUMNS = {"down_since": "REAL", "alert_channel": "TEXT", "alert_ts": "TEXT"}


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
        have = {r["name"] for r in con.execute("PRAGMA table_info(sites)")}
        for col, col_type in _ADDED_COLUMNS.items():
            if col in have:
                continue
            con.execute(f"ALTER TABLE sites ADD COLUMN {col} {col_type}")
            if col == "down_since":
                # Sites already down were alerted on before the upgrade; mark
                # their outage open so they aren't announced a second time.
                con.execute("UPDATE sites SET down_since = ? WHERE last_status = 'down'",
                            (time.time(),))


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


def list_sites(site_type=None, channel_id=None, added_by=None, is_dm=None):
    """Sites, optionally narrowed by type and/or owner (channel or adder)."""
    clauses, params = [], []
    if site_type:
        clauses.append("site_type = ?")
        params.append(site_type)
    if channel_id is not None:
        clauses.append("channel_id = ?")
        params.append(channel_id)
    if added_by is not None:
        clauses.append("added_by = ?")
        params.append(added_by)
    if is_dm is not None:
        clauses.append("is_dm = ?")
        params.append(1 if is_dm else 0)
    q = "SELECT * FROM sites"
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY site_type, name COLLATE NOCASE"
    with _conn() as con:
        return [dict(r) for r in con.execute(q, params).fetchall()]


def all_sites():
    return list_sites()


def find_duplicate(url, channel_id=None, added_by=None):
    """An existing site with the same owner that points at the same place as
    `url`, comparing canonically (scheme/www/case/trailing slash are ignored).

    Pass `channel_id` to check a channel's sites, or `added_by` to check one
    person's DM sites (stored under their DM or user id, so match on the adder).
    Returns the row dict or None. Used to block duplicate adds.
    """
    target = utils.canonical_url(url)
    if added_by is not None:
        candidates = list_sites(added_by=added_by, is_dm=True)
    else:
        candidates = list_sites(channel_id=channel_id)
    for s in candidates:
        if utils.canonical_url(s["url"]) == target:
            return s
    return None


def get_by_rowid(rowid):
    with _conn() as con:
        r = con.execute("SELECT * FROM sites WHERE id = ?", (rowid,)).fetchone()
        return dict(r) if r else None


def find_sites(identifier, channel_id=None, added_by=None):
    """Match by exact url, exact name (case-insensitive), or substring of either.

    When `channel_id` or `added_by` is given, the search is limited to that
    owner's sites — so a user can only manage sites they can actually see.
    """
    ident = (identifier or "").strip()
    like = f"%{ident}%"
    clauses = ["""(url = ? COLLATE NOCASE
                   OR name = ? COLLATE NOCASE
                   OR url LIKE ? COLLATE NOCASE
                   OR name LIKE ? COLLATE NOCASE)"""]
    params = [ident, ident, like, like]
    if channel_id is not None:
        clauses.append("channel_id = ?")
        params.append(channel_id)
    if added_by is not None:
        clauses.append("added_by = ?")
        params.append(added_by)
    params += [ident, ident]
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM sites WHERE " + " AND ".join(clauses) +
            """ ORDER BY
                  CASE WHEN url = ? COLLATE NOCASE OR name = ? COLLATE NOCASE
                       THEN 0 ELSE 1 END,
                  name COLLATE NOCASE""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def remove_site(rowid):
    with _conn() as con:
        con.execute("DELETE FROM sites WHERE id = ?", (rowid,))


def clear_all():
    """Delete every tracked site (keeps the schema and indexes). Returns the
    number of rows removed. Used by the reset script for a fresh start."""
    with _conn() as con:
        cur = con.execute("DELETE FROM sites")
        return cur.rowcount


def set_status(rowid, status):
    with _conn() as con:
        con.execute("UPDATE sites SET last_status = ? WHERE id = ?", (status, rowid))


def open_outage(rowid, since, alert_channel=None, alert_ts=None):
    """Record that a site went down, and where its DOWN alert was posted."""
    with _conn() as con:
        con.execute(
            "UPDATE sites SET down_since = ?, alert_channel = ?, alert_ts = ? WHERE id = ?",
            (since, alert_channel, alert_ts, rowid),
        )


def close_outage(rowid):
    with _conn() as con:
        con.execute(
            "UPDATE sites SET down_since = NULL, alert_channel = NULL, alert_ts = NULL "
            "WHERE id = ?", (rowid,),
        )


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


# --------------------------------------------------------------------------- #
# Per-channel settings
# --------------------------------------------------------------------------- #
def get_mention(channel_id):
    """Who a channel's DOWN alerts ping (Slack markup), or "" for nobody."""
    if not channel_id:
        return ""
    with _conn() as con:
        r = con.execute("SELECT mention FROM channel_settings WHERE channel_id = ?",
                        (channel_id,)).fetchone()
    return (r["mention"] if r else "") or ""


def set_mention(channel_id, mention, updated_by=None):
    with _conn() as con:
        con.execute(
            """INSERT INTO channel_settings (channel_id, mention, updated_by, updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(channel_id) DO UPDATE SET
                 mention = excluded.mention,
                 updated_by = excluded.updated_by,
                 updated_at = excluded.updated_at""",
            (channel_id, mention or "", updated_by, time.time()),
        )
