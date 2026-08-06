import logging
import os
import time

from aiohttp import web

import db as db_module

log = logging.getLogger("server")

_HERE = os.path.dirname(__file__)
_DASHBOARD_PATH = os.path.join(_HERE, "Flight Diversions Dashboard.dc.html")
_SUPPORT_JS_PATH = os.path.join(_HERE, "support.js")

# How far back the event feed and the events24/confirmed stats look.
# Individual events are already de-duped at the source: main.py only ever
# persists one per (hex, event_type) per alert_cooldown_seconds window (see
# enrich_and_dispatch), so this window just bounds how long a past event
# stays visible on the dashboard, not how often it can recur.
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
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # PORT is what most hosting platforms inject automatically; DASHBOARD_PORT
    # is kept as the local-dev override so existing instructions still work.
    port = int(os.environ.get("PORT") or os.environ.get("DASHBOARD_PORT", "8787"))
    log.info("Dashboard op http://localhost:%d", port)
    web.run_app(build_app(), host="0.0.0.0", port=port, print=None)
