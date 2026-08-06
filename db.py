import os
import sqlite3
import time

_DB_PATH = os.path.join(os.path.dirname(__file__), "data", "flightdiversions.sqlite3")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_cooldowns (
    hex TEXT NOT NULL,
    event_type TEXT NOT NULL,
    last_alert_ts REAL NOT NULL,
    PRIMARY KEY (hex, event_type)
);

CREATE TABLE IF NOT EXISTS learned_routes (
    callsign TEXT NOT NULL,
    origin_icao TEXT NOT NULL,
    destination_icao TEXT NOT NULL,
    times_seen INTEGER NOT NULL DEFAULT 1,
    last_seen REAL NOT NULL,
    PRIMARY KEY (callsign, origin_icao, destination_icao)
);

-- Every event we actually dispatched (i.e. cleared the alert cooldown), so
-- the dashboard has something to read. Deliberately NOT every raw detector
-- hit — course_deviation/holding_pattern etc. can re-evaluate every cycle
-- for a track that's still in the same state, and the cooldown gate is
-- already the natural de-dupe point shared with the Telegram alert.
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hex TEXT NOT NULL,
    callsign TEXT,
    event_type TEXT NOT NULL,
    confidence TEXT NOT NULL,
    message TEXT NOT NULL,
    squawk TEXT,
    alt REAL,
    lat REAL,
    lon REAL,
    origin_icao TEXT,
    dest_icao TEXT,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);

-- Single-row table of the monitor's live operational status, so the
-- dashboard can show "N aircraft tracked" / "last sweep Xs ago" without the
-- web server needing to share process memory with main.py.
CREATE TABLE IF NOT EXISTS sweep_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    tracked_count INTEGER,
    last_tier0_ts REAL,
    last_tier1_ts REAL
);
INSERT OR IGNORE INTO sweep_status (id, tracked_count, last_tier0_ts, last_tier1_ts) VALUES (1, NULL, NULL, NULL);
"""


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    # main.py (writer) and server.py (reader) are separate processes sharing
    # this file — WAL lets the dashboard read without blocking on/being
    # blocked by the monitor's writes, and busy_timeout absorbs the rare
    # moment both touch the file at once instead of raising "database is locked".
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def load_cooldowns(conn: sqlite3.Connection) -> dict:
    """Returns {(hex, event_type): last_alert_ts}. Loaded once at startup so
    a restart doesn't immediately re-fire alerts we already sent."""
    cur = conn.execute("SELECT hex, event_type, last_alert_ts FROM alert_cooldowns")
    return {(row[0], row[1]): row[2] for row in cur.fetchall()}


def save_cooldown(conn: sqlite3.Connection, hex_id: str, event_type: str, ts: float | None = None):
    ts = ts or time.time()
    conn.execute(
        "INSERT INTO alert_cooldowns (hex, event_type, last_alert_ts) VALUES (?, ?, ?) "
        "ON CONFLICT(hex, event_type) DO UPDATE SET last_alert_ts = excluded.last_alert_ts",
        (hex_id, event_type, ts),
    )
    conn.commit()


def record_route_observation(conn: sqlite3.Connection, callsign: str, origin_icao: str, destination_icao: str, ts: float | None = None):
    """Called whenever WE directly observed a callsign fly origin->destination
    (saw it depart, then land somewhere). Builds our own ground truth over
    time, independent of adsbdb's static/sometimes-stale mapping."""
    if not callsign or not origin_icao or not destination_icao:
        return
    ts = ts or time.time()
    conn.execute(
        "INSERT INTO learned_routes (callsign, origin_icao, destination_icao, times_seen, last_seen) "
        "VALUES (?, ?, ?, 1, ?) "
        "ON CONFLICT(callsign, origin_icao, destination_icao) DO UPDATE SET "
        "times_seen = times_seen + 1, last_seen = excluded.last_seen",
        (callsign, origin_icao, destination_icao, ts),
    )
    conn.commit()


def get_learned_destinations(conn: sqlite3.Connection, callsign: str, min_times_seen: int = 2) -> set:
    """All destinations we've personally observed this callsign actually fly
    to at least `min_times_seen` times. Used to avoid flagging a regional/
    commuter operator's normal multi-leg rotation as a diversion."""
    cur = conn.execute(
        "SELECT destination_icao FROM learned_routes WHERE callsign = ? AND times_seen >= ?",
        (callsign, min_times_seen),
    )
    return {row[0] for row in cur.fetchall()}


def get_top_learned_routes(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    cur = conn.execute(
        "SELECT callsign, origin_icao, destination_icao, times_seen FROM learned_routes "
        "ORDER BY times_seen DESC LIMIT ?",
        (limit,),
    )
    return [
        {"callsign": r[0], "origin": r[1], "dest": r[2], "times": r[3]}
        for r in cur.fetchall()
    ]


def save_event(conn: sqlite3.Connection, ev, ts: float | None = None):
    """Persists a detector.Event (or anything with the same attributes) so
    the dashboard can display it. Called at the same point the Telegram
    alert is sent, i.e. only once per (hex, event_type) cooldown window."""
    ts = ts or time.time()
    conn.execute(
        "INSERT INTO events (hex, callsign, event_type, confidence, message, squawk, alt, lat, lon, "
        "origin_icao, dest_icao, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ev.hex, ev.callsign, ev.event_type, ev.confidence, ev.message, ev.squawk,
            ev.alt, ev.lat, ev.lon, ev.origin_icao, ev.dest_icao, ts,
        ),
    )
    conn.commit()


def get_recent_events(conn: sqlite3.Connection, since_ts: float, limit: int = 200) -> list[dict]:
    cur = conn.execute(
        "SELECT id, hex, callsign, event_type, confidence, message, squawk, alt, lat, lon, "
        "origin_icao, dest_icao, ts FROM events WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
        (since_ts, limit),
    )
    cols = ["id", "hex", "callsign", "event_type", "confidence", "message", "squawk", "alt", "lat", "lon",
            "origin_icao", "dest_icao", "ts"]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def count_events_since(conn: sqlite3.Connection, since_ts: float, confidence: str | None = None) -> int:
    if confidence:
        cur = conn.execute("SELECT COUNT(*) FROM events WHERE ts >= ? AND confidence = ?", (since_ts, confidence))
    else:
        cur = conn.execute("SELECT COUNT(*) FROM events WHERE ts >= ?", (since_ts,))
    return cur.fetchone()[0]


def save_sweep_status(conn: sqlite3.Connection, tracked_count: int | None = None,
                       tier0_ts: float | None = None, tier1_ts: float | None = None):
    updates, params = [], []
    if tracked_count is not None:
        updates.append("tracked_count = ?")
        params.append(tracked_count)
    if tier0_ts is not None:
        updates.append("last_tier0_ts = ?")
        params.append(tier0_ts)
    if tier1_ts is not None:
        updates.append("last_tier1_ts = ?")
        params.append(tier1_ts)
    if not updates:
        return
    conn.execute(f"UPDATE sweep_status SET {', '.join(updates)} WHERE id = 1", params)
    conn.commit()


def get_sweep_status(conn: sqlite3.Connection) -> dict:
    cur = conn.execute("SELECT tracked_count, last_tier0_ts, last_tier1_ts FROM sweep_status WHERE id = 1")
    row = cur.fetchone()
    return {"tracked_count": row[0], "last_tier0_ts": row[1], "last_tier1_ts": row[2]} if row else {
        "tracked_count": None, "last_tier0_ts": None, "last_tier1_ts": None,
    }
