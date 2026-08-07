import os
import sqlite3
import time

_DB_PATH = os.path.join(os.path.dirname(__file__), "data", "flightdiversions.sqlite3")

_SCHEMA = """
-- Legacy: was the old per-(hex, event_type) Telegram-dispatch cooldown,
-- superseded by incidents.py's state-transition-based notification gating
-- (MASTERPLAN.md sectie 3, BACKTEST_LOG.md ronde 10). Kept (not dropped)
-- so an existing deployed database doesn't need a migration for a table
-- nothing writes to anymore; no code reads or writes it either.
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

-- Incident lifecycle (MASTERPLAN.md sectie 3/9): one open "case" per
-- aircraft-flight-instance, continuously re-scored across cycles by
-- incidents.py, instead of the `events` table's one-row-per-dispatch model
-- above (which stays as-is, an append-only raw log — incidents.py reads
-- detector Events same as before, but now feeds them into a persistent,
-- re-evaluated record instead of dispatching each one directly).
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hex TEXT NOT NULL,
    callsign TEXT,
    state TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    peak_score REAL NOT NULL DEFAULT 0,
    opened_ts REAL NOT NULL,
    last_evidence_ts REAL NOT NULL,
    resolved_ts REAL,
    resolution_reason TEXT,
    origin_icao TEXT,
    dest_icao TEXT,
    last_lat REAL,
    last_lon REAL,
    last_alt REAL,
    last_squawk TEXT,
    aircraft_class TEXT,
    notified_state TEXT
);
CREATE INDEX IF NOT EXISTS idx_incidents_state ON incidents (state);
CREATE INDEX IF NOT EXISTS idx_incidents_hex ON incidents (hex);

CREATE TABLE IF NOT EXISTS incident_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL REFERENCES incidents(id),
    ts REAL NOT NULL,
    source TEXT NOT NULL,
    delta REAL NOT NULL,
    description TEXT NOT NULL,
    detector_confidence TEXT
);
CREATE INDEX IF NOT EXISTS idx_evidence_incident ON incident_evidence (incident_id);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """db_path defaults to the real on-disk database; pass ':memory:' (or
    any other sqlite3-accepted path) for tests so they don't touch the
    real file — see backtest.py's incident-engine regression checks."""
    path = db_path if db_path is not None else _DB_PATH
    if path != ":memory:":
        os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    # main.py (writer) and server.py (reader) are separate processes sharing
    # this file — WAL lets the dashboard read without blocking on/being
    # blocked by the monitor's writes, and busy_timeout absorbs the rare
    # moment both touch the file at once instead of raising "database is locked".
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


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


_INCIDENT_COLS = [
    "id", "hex", "callsign", "state", "score", "peak_score", "opened_ts",
    "last_evidence_ts", "resolved_ts", "resolution_reason", "origin_icao",
    "dest_icao", "last_lat", "last_lon", "last_alt", "last_squawk",
    "aircraft_class", "notified_state",
]


def _incident_row_to_dict(row) -> dict:
    return dict(zip(_INCIDENT_COLS, row))


def get_open_incident(conn: sqlite3.Connection, hex_id: str, open_states: tuple) -> dict | None:
    """Most recent still-open incident for this hex, if any. open_states is
    the tuple of non-terminal state strings (incidents.OPEN_STATES) — kept
    as a parameter rather than imported here so db.py doesn't need to know
    incidents.py's vocabulary, matching this file's existing style of
    staying a thin, generic persistence layer."""
    placeholders = ",".join("?" * len(open_states))
    cur = conn.execute(
        f"SELECT {','.join(_INCIDENT_COLS)} FROM incidents "
        f"WHERE hex = ? AND state IN ({placeholders}) "
        f"ORDER BY opened_ts DESC LIMIT 1",
        (hex_id, *open_states),
    )
    row = cur.fetchone()
    return _incident_row_to_dict(row) if row else None


def create_incident(conn: sqlite3.Connection, hex_id: str, callsign: str, state: str,
                     score: float, ts: float, aircraft_class: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO incidents (hex, callsign, state, score, peak_score, opened_ts, "
        "last_evidence_ts, aircraft_class) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (hex_id, callsign, state, score, score, ts, ts, aircraft_class),
    )
    conn.commit()
    return cur.lastrowid


def update_incident(conn: sqlite3.Connection, incident_id: int, **fields):
    """Generic column update — fields are validated against _INCIDENT_COLS
    so a typo'd kwarg fails loudly instead of silently doing nothing."""
    if not fields:
        return
    unknown = set(fields) - set(_INCIDENT_COLS)
    if unknown:
        raise ValueError(f"unknown incident column(s): {unknown}")
    cols = list(fields.keys())
    conn.execute(
        f"UPDATE incidents SET {', '.join(f'{c} = ?' for c in cols)} WHERE id = ?",
        (*[fields[c] for c in cols], incident_id),
    )
    conn.commit()


def add_incident_evidence(conn: sqlite3.Connection, incident_id: int, ts: float, source: str,
                           delta: float, description: str, detector_confidence: str | None = None):
    conn.execute(
        "INSERT INTO incident_evidence (incident_id, ts, source, delta, description, detector_confidence) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (incident_id, ts, source, delta, description, detector_confidence),
    )
    conn.commit()


def get_incident_evidence(conn: sqlite3.Connection, incident_id: int) -> list[dict]:
    cur = conn.execute(
        "SELECT ts, source, delta, description, detector_confidence FROM incident_evidence "
        "WHERE incident_id = ? ORDER BY ts ASC",
        (incident_id,),
    )
    cols = ["ts", "source", "delta", "description", "detector_confidence"]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_active_incidents(conn: sqlite3.Connection, visible_states: tuple) -> list[dict]:
    placeholders = ",".join("?" * len(visible_states))
    cur = conn.execute(
        f"SELECT {','.join(_INCIDENT_COLS)} FROM incidents "
        f"WHERE state IN ({placeholders}) ORDER BY score DESC",
        visible_states,
    )
    return [_incident_row_to_dict(row) for row in cur.fetchall()]


def get_recent_resolved_incidents(conn: sqlite3.Connection, since_ts: float, limit: int = 20) -> list[dict]:
    cur = conn.execute(
        f"SELECT {','.join(_INCIDENT_COLS)} FROM incidents "
        f"WHERE resolved_ts IS NOT NULL AND resolved_ts >= ? ORDER BY resolved_ts DESC LIMIT ?",
        (since_ts, limit),
    )
    return [_incident_row_to_dict(row) for row in cur.fetchall()]


def count_incidents_by_resolution(conn: sqlite3.Connection, since_ts: float) -> dict:
    """{state: count} for incidents resolved since since_ts — backs the
    dashboard's precision-rate stat (MASTERPLAN.md sectie 10). Grouped by
    `state` (the terminal GESLOTEN_* constant, e.g. incidents.CLOSED_LANDED/
    CLOSED_FALSE_ALARM), NOT `resolution_reason` — that column holds a
    free-form human-readable description (incidents.py's `_resolve(...,
    description=...)`, e.g. "geland op EDDM, niet de verwachte bestemming
    — bevestigde diversie") that's never equal to a state constant. Fixed
    2026-08-07 (self-review pass): the original GROUP BY resolution_reason
    meant server.py's `resolution_counts.get(incidents.CLOSED_LANDED, 0)`
    lookup could never match anything, so resolvedConfirmedDiversion24h/
    resolvedFalseAlarm24h/precisionRate were always 0/0/None regardless of
    how many incidents had actually resolved — untested by backtest.py,
    which has no coverage of this function or the API's stats block."""
    cur = conn.execute(
        "SELECT state, COUNT(*) FROM incidents "
        "WHERE resolved_ts IS NOT NULL AND resolved_ts >= ? GROUP BY state",
        (since_ts,),
    )
    return {state: count for state, count in cur.fetchall()}


def get_sweep_status(conn: sqlite3.Connection) -> dict:
    cur = conn.execute("SELECT tracked_count, last_tier0_ts, last_tier1_ts FROM sweep_status WHERE id = 1")
    row = cur.fetchone()
    return {"tracked_count": row[0], "last_tier0_ts": row[1], "last_tier1_ts": row[2]} if row else {
        "tracked_count": None, "last_tier0_ts": None, "last_tier1_ts": None,
    }
