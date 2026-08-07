import logging
import os
import time

from aiohttp import web

import db as db_module
import incidents

log = logging.getLogger("server")

_HERE = os.path.dirname(__file__)
_DASHBOARD_PATH = os.path.join(_HERE, "Flight Diversions Dashboard.dc.html")
_SUPPORT_JS_PATH = os.path.join(_HERE, "support.js")

# How far back the event feed and the events24/confirmed stats look.
# Individual events are NOT de-duped at the source: main.py's tier0_loop/
# tier1_loop save every raw detector.Event unconditionally (see their
# db_module.save_event calls) — `events` is a raw, append-only log of every
# detector hit, same convention as backtest.py's run_case (see its
# docstring). De-duplication/escalation now happens entirely in the
# incident engine (incidents.py's notified_state gate), not here — this
# comment previously described an alert_cooldown_seconds-gated
# enrich_and_dispatch mechanism that was removed in BACKTEST_LOG.md ronde
# 10-11 (renamed/split into enrich_events + IncidentManager.step()); found
# stale while reviewing this file (ronde 16). This window just bounds how
# long a past event stays visible on the dashboard.
FEED_WINDOW_SECONDS = 24 * 3600

CONFIDENCE_NL_TO_EN = {
    "BEVESTIGD": "CONFIRMED",
    "WAARSCHIJNLIJK": "LIKELY",
    "MOGELIJK": "POSSIBLE",
}


def _event_to_json(row: dict) -> dict:
    return {
        "id": row["id"],
        "hex": row["hex"],
        "callsign": row["callsign"] or row["hex"],
        "type": row["event_type"],
        "confidence": CONFIDENCE_NL_TO_EN.get(row["confidence"], row["confidence"]),
        "message": row["message"],
        "squawk": row["squawk"],
        "alt": row["alt"],
        "lat": row["lat"],
        "lon": row["lon"],
        "origin": row["origin_icao"],
        "dest": row["dest_icao"],
        "tsMs": row["ts"] * 1000,
    }


async def handle_index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(_DASHBOARD_PATH, headers={"Content-Type": "text/html; charset=utf-8"})


async def handle_support_js(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(_SUPPORT_JS_PATH, headers={"Content-Type": "application/javascript; charset=utf-8"})


async def handle_api_events(request: web.Request) -> web.Response:
    conn = request.app["db"]
    now = time.time()
    since = now - FEED_WINDOW_SECONDS

    rows = db_module.get_recent_events(conn, since, limit=200)
    events = [_event_to_json(r) for r in rows]

    sweep = db_module.get_sweep_status(conn)
    last_sweep_ts = sweep["last_tier0_ts"] or sweep["last_tier1_ts"]

    return web.json_response({
        "events": events,
        "learnedRoutes": db_module.get_top_learned_routes(conn, limit=20),
        "stats": {
            # True counts over the full window, not just the (200-row-capped)
            # `rows` list above — on a busy day with >200 events, deriving
            # these from `rows` would silently undercount exactly when
            # accurate stats matter most.
            "confirmed": db_module.count_events_since(conn, since, confidence="BEVESTIGD"),
            "events24": db_module.count_events_since(conn, since),
            "tracked": sweep["tracked_count"],
            "lastSweepSecondsAgo": (now - last_sweep_ts) if last_sweep_ts else None,
        },
    })


# MASTERPLAN.md sectie 9/10: how far back the resolved-incidents feed
# looks and the precision-rate stat is computed over. Active incidents have
# no time window — they're active until they resolve, however long that
# takes.
INCIDENT_FEED_WINDOW_SECONDS = 24 * 3600


def _incident_to_json(row: dict, evidence: list[dict]) -> dict:
    return {
        "id": row["id"], "hex": row["hex"], "callsign": row["callsign"] or row["hex"],
        "state": row["state"], "score": row["score"], "peakScore": row["peak_score"],
        "openedTsMs": row["opened_ts"] * 1000, "lastEvidenceTsMs": row["last_evidence_ts"] * 1000,
        "resolvedTsMs": (row["resolved_ts"] * 1000) if row["resolved_ts"] else None,
        "resolutionReason": row["resolution_reason"],
        "origin": row["origin_icao"], "dest": row["dest_icao"],
        "lat": row["last_lat"], "lon": row["last_lon"], "alt": row["last_alt"], "squawk": row["last_squawk"],
        "aircraftClass": row["aircraft_class"],
        "evidence": [
            {"tsMs": e["ts"] * 1000, "source": e["source"], "delta": e["delta"], "description": e["description"]}
            for e in evidence
        ],
    }


async def handle_api_incidents(request: web.Request) -> web.Response:
    """MASTERPLAN.md sectie 9/10 — the incident-lifecycle API. Consumed by
    Flight Diversions Dashboard.dc.html's "Active incidents" section (added
    ronde 11) via a 5s poll; also independently testable on its own:
    `curl /api/incidents` while serve_all.py runs. (Stale comment claiming
    the dashboard didn't consume this yet found and fixed during ronde 22's
    self-review — the dashboard integration landed in ronde 11, this
    docstring was never updated after.)"""
    conn = request.app["db"]
    now = time.time()
    since = now - INCIDENT_FEED_WINDOW_SECONDS

    active_rows = db_module.get_active_incidents(conn, incidents.VISIBLE_STATES)
    resolved_rows = db_module.get_recent_resolved_incidents(conn, since, limit=20)
    active = [_incident_to_json(r, db_module.get_incident_evidence(conn, r["id"])) for r in active_rows]
    resolved = [_incident_to_json(r, db_module.get_incident_evidence(conn, r["id"])) for r in resolved_rows]

    resolution_counts = db_module.count_incidents_by_resolution(conn, since)
    confirmed_diversions = resolution_counts.get(incidents.CLOSED_LANDED, 0)
    false_alarms = resolution_counts.get(incidents.CLOSED_FALSE_ALARM, 0)
    total_resolved = confirmed_diversions + false_alarms

    return web.json_response({
        "active": active,
        "resolvedRecent": resolved,
        "stats": {
            "activeConfirmed": sum(1 for r in active_rows if r["state"] == incidents.CONFIRMED),
            "activeLikely": sum(1 for r in active_rows if r["state"] == incidents.LIKELY),
            "activePossible": sum(1 for r in active_rows if r["state"] == incidents.POSSIBLE),
            "resolvedFalseAlarm24h": false_alarms,
            "resolvedConfirmedDiversion24h": confirmed_diversions,
            # None (not 0) when nothing has resolved yet — a 0% or 100%
            # precision rate from a sample size of zero would be misleading.
            "precisionRate": (confirmed_diversions / total_resolved) if total_resolved else None,
        },
    })


async def on_startup(app: web.Application):
    app["db"] = db_module.connect()


async def on_cleanup(app: web.Application):
    app["db"].close()


def build_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/", handle_index)
    app.router.add_get("/support.js", handle_support_js)
    app.router.add_get("/api/events", handle_api_events)
    app.router.add_get("/api/incidents", handle_api_incidents)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # PORT is what most hosting platforms inject automatically; DASHBOARD_PORT
    # is kept as the local-dev override so existing instructions still work.
    port = int(os.environ.get("PORT") or os.environ.get("DASHBOARD_PORT", "8787"))
    log.info("Dashboard op http://localhost:%d", port)
    web.run_app(build_app(), host="0.0.0.0", port=port, print=None)
