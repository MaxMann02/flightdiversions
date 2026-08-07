"""Combined entrypoint for hosting platforms that only give you a single
always-on process slot (typically a "web service", not a separate
background worker). Runs the exact same tier0/tier1 scanning loops as
`main.py`, plus the dashboard web server from `server.py`, together in one
asyncio event loop.

Locally, `main.py` and `server.py` still work fine as two separate
processes if you prefer that (e.g. so you can restart the dashboard
without interrupting the scanner) — this file changes nothing about their
standalone behavior, it just wires the same pieces together for platforms
that won't give you two free always-on processes.

Run with:  python serve_all.py
"""
import asyncio
import logging
import os
import ssl

import aiohttp
import certifi
from aiohttp import web

import db as db_module
from airports import AirportDB
from config import CONFIG
from incidents import IncidentManager
from main import tier0_loop, tier1_loop
from server import build_app
from state import TrackStore

log = logging.getLogger("serve_all")


async def _start_scan_loops(app: web.Application):
    cfg = CONFIG
    if not cfg["telegram_bot_token"]:
        log.warning("Geen telegram_bot_token geconfigureerd — meldingen worden alleen gelogd, niet verstuurd.")

    log.info("Airport database laden...")
    airport_db = AirportDB()
    log.info("Klaar: %d luchthavens geladen.", len(airport_db.by_icao))

    # Separate db connection from the dashboard's own (set up in
    # server.py's on_startup) — mirrors the existing two-process setup,
    # where main.py and server.py already each open their own connection
    # to the same sqlite file. WAL mode (db.connect()) is what makes that
    # safe; see db.py's own comment on why.
    db_conn = db_module.connect()
    store = TrackStore(db_conn=db_conn)

    incident_mgr = IncidentManager(db_conn, cfg, airport_db)
    log.info("%d open incidenten geladen uit vorige sessie.", len(incident_mgr.open_hexes()))

    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    session = aiohttp.ClientSession(connector=connector)

    app["scan_session"] = session
    app["scan_db"] = db_conn
    app["scan_tasks"] = [
        asyncio.create_task(tier0_loop(session, store, incident_mgr, cfg, db_conn)),
        asyncio.create_task(tier1_loop(session, store, airport_db, incident_mgr, cfg, db_conn)),
    ]


async def _stop_scan_loops(app: web.Application):
    for task in app.get("scan_tasks", ()):
        task.cancel()
    for task in app.get("scan_tasks", ()):
        try:
            await task
        except asyncio.CancelledError:
            pass
    if "scan_session" in app:
        await app["scan_session"].close()
    if "scan_db" in app:
        app["scan_db"].close()


def build_combined_app() -> web.Application:
    app = build_app()
    app.on_startup.append(_start_scan_loops)
    app.on_cleanup.append(_stop_scan_loops)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    port = int(os.environ.get("PORT") or os.environ.get("DASHBOARD_PORT", "8787"))
    log.info("flightdiversions (scanner + dashboard) op http://0.0.0.0:%d", port)
    web.run_app(build_combined_app(), host="0.0.0.0", port=port, print=None)
