import asyncio
import logging
import random
import re
import ssl
import time

import aiohttp
import certifi

import db as db_module
import providers
import weather
from airports import AirportDB, route_plausible
from config import CONFIG
from detector import detect_emergency, detect_signal_lost_near_airport, evaluate
from notifier import notify
from providers import fetch_squawk_sweep, fetch_world_snapshot, lookup_route
from state import TrackStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

# Scheduled-airline-style callsigns (e.g. KLM123, BAW819K). adsbdb's route
# database is crowdsourced from these; GA/random idents just waste a lookup.
AIRLINE_CALLSIGN_RE = re.compile(r"^[A-Z]{3}[0-9]{1,4}[A-Z]?$")
ROUTE_LOOKUP_BATCH = 20


async def enrich_and_dispatch(session, store: TrackStore, cfg: dict, events: list, db_conn=None):
    for ev in events:
        if not store.should_alert(ev.hex, ev.event_type, cfg["alert_cooldown_seconds"]):
            continue

        if cfg["cross_provider_consensus_enabled"] and ev.event_type == "wrong_airport":
            agrees = await providers.cross_provider_agrees(session, ev.hex, expected_on_ground=True)
            if not agrees:
                ev.confidence = "WAARSCHIJNLIJK"
                ev.message += " (grondstatus niet bevestigd door tweede bron)"
                log.warning("cross-provider disagreement on %s, downgraded", ev.callsign or ev.hex)

        if cfg["weather_enrichment_enabled"] and ev.weather_icao:
            metar = await weather.get_metar(session, ev.weather_icao)
            ev.message += weather.describe(metar)

        if cfg["fr24_confirm_enabled"] and ev.confidence == "MOGELIJK" and ev.lat is not None:
            import fr24_confirm
            confirmation = await fr24_confirm.confirm_diversion(ev.lat, ev.lon, ev.callsign)
            if confirmation:
                ev.confidence = "WAARSCHIJNLIJK"
                ev.message += f" [{confirmation}]"

        log.info("EVENT [%s/%s] %s: %s", ev.confidence, ev.event_type, ev.callsign or ev.hex, ev.message)
        if db_conn is not None:
            db_module.save_event(db_conn, ev)
        await notify(session, cfg, ev)
        store.mark_alerted(ev.hex, ev.event_type)


async def tier0_loop(session, store: TrackStore, cfg: dict, db_conn=None):
    """Fast global sweep for declared emergencies (squawk 7700/7600/7500, or
    the ADS-B 'emergency' status field). Cheap: ~6 tiny requests per cycle."""
    while True:
        try:
            snapshot = await fetch_squawk_sweep(session)
            now = time.time()
            events = []
            for ac in snapshot.values():
                track = store.update(ac, now)
                ev = detect_emergency(track, ac)
                if ev:
                    events.append(ev)
            await enrich_and_dispatch(session, store, cfg, events, db_conn)
            if db_conn is not None:
                db_module.save_sweep_status(db_conn, tier0_ts=now)
        except Exception:
            log.exception("tier0 loop error")
        await asyncio.sleep(cfg["tier0_interval_seconds"])


async def tier1_loop(session, store: TrackStore, airport_db: AirportDB, cfg: dict, db_conn):
    """Slower worldwide state-vector sweep: tracks trajectories, resolves
    expected routes for scheduled-airline callsigns, and runs the full
    diversion heuristics."""
    while True:
        try:
            snapshot = await fetch_world_snapshot(session)
            now = time.time()
            previously_tracked = set(store.tracks.keys())

            pending_lookup = []
            for ac in snapshot.values():
                track = store.get_or_create(ac["hex"])
                if (not track.route_checked and ac["callsign"]
                        and AIRLINE_CALLSIGN_RE.match(ac["callsign"])):
                    pending_lookup.append(ac["callsign"])

            # Random sample rather than "first ROUTE_LOOKUP_BATCH in snapshot
            # order": fetch_world_snapshot builds its dict from two fixed-
            # order API responses, so if that ordering is at all stable
            # across cycles, a sustained backlog (worldwide traffic can
            # easily exceed 20 new callsigns per 60s cycle) would let the
            # same early aircraft monopolize every cycle's lookup slots
            # indefinitely — starving whichever aircraft always land later
            # in that order, rather than just delaying them. Sampling means
            # every pending callsign eventually gets a turn regardless of
            # how stable the snapshot's ordering actually is.
            to_lookup = (random.sample(pending_lookup, ROUTE_LOOKUP_BATCH)
                         if len(pending_lookup) > ROUTE_LOOKUP_BATCH else pending_lookup)

            if to_lookup:
                routes = await asyncio.gather(*(lookup_route(session, cs) for cs in to_lookup))
                for cs, base_route in zip(to_lookup, routes):
                    for ac in snapshot.values():
                        if ac["callsign"] == cs:
                            t = store.get_or_create(ac["hex"])
                            # Fresh copy per aircraft — bug found by backtest review:
                            # reusing the loop variable as scratch space meant one
                            # implausible match for an earlier aircraft with the
                            # same callsign string permanently nulled the route for
                            # every OTHER aircraft sharing that callsign this cycle,
                            # even ones whose position was perfectly plausible.
                            route = base_route
                            if route and ac.get("lat") is not None:
                                plausible = route_plausible(
                                    route["origin_lat"], route["origin_lon"],
                                    route["destination_lat"], route["destination_lon"],
                                    ac["lat"], ac["lon"],
                                )
                                if not plausible:
                                    log.info("adsbdb route voor %s lijkt niet te kloppen met huidige positie, genegeerd", cs)
                                    route = None
                            t.route = route
                            # A route-less result only counts as "checked" (i.e.
                            # never retried again) once adsbdb gave a definitive
                            # answer. If the lookup merely failed to reach adsbdb,
                            # providers.route_lookup_pending_retry keeps this
                            # False so this same track gets retried after the
                            # cooldown instead of staying routeless for its
                            # entire remaining tracked lifetime.
                            t.route_checked = route is not None or not providers.route_lookup_pending_retry(cs)

            events = []
            for ac in snapshot.values():
                # track.was_on_ground still holds the previous cycle's state here,
                # which is what detect_landed_wrong_airport needs to catch the edge.
                track = store.update(ac, now)
                just_landed = ac.get("on_ground") and track.was_on_ground is False
                just_took_off = (not ac.get("on_ground")) and track.was_on_ground is True

                # Re-check plausibility every cycle, not just at resolution
                # time. Found via soak-test backtesting (UAL840: supposedly
                # Melbourne->LAX, landed in Anchorage): route_plausible only
                # ran once, using the position AT resolution — a callsign
                # collision (two aircraft/operators broadcasting the same
                # string) can look plausible by chance at that one moment and
                # only reveal itself as wrong later in the flight. This is a
                # cheap, local geometry check — no extra API calls.
                if track.route and ac.get("lat") is not None and not ac.get("on_ground"):
                    # check_progress=False: this is the ONGOING recheck of an
                    # already-trusted route, not the initial resolution — see
                    # route_plausible's docstring for why the along-track
                    # progress requirement is dropped here specifically (it
                    # also flagged genuine diversions that fly straight past
                    # their destination, not just bad callsign data).
                    if not route_plausible(
                        track.route["origin_lat"], track.route["origin_lon"],
                        track.route["destination_lat"], track.route["destination_lon"],
                        ac["lat"], ac["lon"], check_progress=False,
                    ):
                        log.info("route voor %s niet meer plausibel gezien huidige positie, losgelaten", track.callsign)
                        track.route = None
                        track.route_checked = False

                events.extend(evaluate(track, ac, airport_db, cfg, db_conn))

                # Build our own learned-route ground truth from directly
                # observed takeoff->landing pairs, independent of adsbdb.
                if just_took_off and ac.get("lat") is not None:
                    airport, _ = airport_db.nearest(ac["lat"], ac["lon"], max_km=15.0)
                    store.record_takeoff(track, airport, now)
                elif just_landed and ac.get("lat") is not None:
                    airport, _ = airport_db.nearest(ac["lat"], ac["lon"], max_km=15.0)
                    if airport:
                        store.record_landing_observation(track, airport)

                track.was_on_ground = ac.get("on_ground", False)
                track.missing_cycles = 0

            # Aircraft present last cycle but absent this one: track how long
            # they've been missing, and once that crosses a threshold, check
            # whether they disappeared near a non-destination airport (see
            # detect_signal_lost_near_airport).
            for hex_id in previously_tracked - set(snapshot.keys()):
                t = store.tracks.get(hex_id)
                if not t:
                    continue
                t.missing_cycles += 1
                if t.missing_cycles == cfg["signal_lost_missing_cycles"]:
                    ev = detect_signal_lost_near_airport(t, airport_db, cfg)
                    if ev:
                        events.append(ev)

            await enrich_and_dispatch(session, store, cfg, events, db_conn)
            db_module.save_sweep_status(db_conn, tracked_count=len(snapshot), tier1_ts=now)
            store.prune(now)
            log.info("tier1 cycle: %d aircraft tracked, %d events", len(snapshot), len(events))
        except Exception:
            log.exception("tier1 loop error")
        await asyncio.sleep(cfg["tier1_interval_seconds"])


async def main():
    cfg = CONFIG
    if not cfg["telegram_bot_token"]:
        log.warning("Geen telegram_bot_token in config.json — meldingen worden alleen gelogd, niet verstuurd.")

    log.info("Airport database laden...")
    airport_db = AirportDB()
    log.info("Klaar: %d luchthavens geladen.", len(airport_db.by_icao))

    db_conn = db_module.connect()
    store = TrackStore(db_conn=db_conn)
    log.info("%d alert-cooldowns geladen uit vorige sessie.", len(store.cooldowns))

    # Windows' default OpenSSL cert path (ssl.get_default_verify_paths().openssl_cafile)
    # doesn't exist on this machine, so aiohttp's default context has no usable CA
    # bundle. Point it at certifi's instead.
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            await asyncio.gather(
                tier0_loop(session, store, cfg, db_conn),
                tier1_loop(session, store, airport_db, cfg, db_conn),
            )
    finally:
        db_conn.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Gestopt.")
