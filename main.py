import asyncio
import logging
import random
import re
import ssl
import time

import aiohttp
import certifi

import airspace
import classify
import db as db_module
import incidents
import providers
import weather
from airports import AirportDB, ROUTE_REVALIDATION_WINDOW_S, route_plausible
from config import CONFIG
from detector import detect_emergency, detect_signal_lost_near_airport, evaluate
from incidents import IncidentManager
from notifier import notify_incident_transition
from providers import fetch_squawk_sweep, fetch_world_snapshot, lookup_route
from state import TrackStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

# Scheduled-airline-style callsigns (e.g. KLM123, BAW819K). adsbdb's route
# database is crowdsourced from these; GA/random idents just waste a lookup.
AIRLINE_CALLSIGN_RE = re.compile(r"^[A-Z]{3}[0-9]{1,4}[A-Z]?$")
ROUTE_LOOKUP_BATCH = 20
# Same free-lookup-budget-per-cycle reasoning as ROUTE_LOOKUP_BATCH, for the
# hexdb.io aircraft-lookup (classification refinement) batch.
CLASS_LOOKUP_BATCH = 20


async def enrich_events(session, cfg: dict, events: list) -> list:
    """Enriches each fresh detector.Event in place (cross-provider
    corroboration, weather context, optional FR24 confirmation). No longer
    dispatches directly to Telegram/DB — MASTERPLAN.md sectie 3 replaced
    the old direct-dispatch-per-event model with the incident engine
    (incidents.py): every event is now EVIDENCE fed into IncidentManager.
    step(), and Telegram fires only on incident state transitions (see
    notify_incident_transition, called from tier0_loop/tier1_loop)."""
    for ev in events:
        if cfg["cross_provider_consensus_enabled"] and ev.event_type == "wrong_airport":
            agrees = await providers.cross_provider_agrees(session, ev.hex, expected_on_ground=True)
            if not agrees:
                ev.confidence = "WAARSCHIJNLIJK"
                ev.message += " (grondstatus niet bevestigd door tweede bron)"
                log.warning("cross-provider disagreement on %s, downgraded", ev.callsign or ev.hex)

        # Live data (2026-08-07, BACKTEST_LOG.md ronde 18): 24/25 sampled
        # live wrong_airport/BEVESTIGD hits had ZERO corroborating evidence
        # from any other detector — a real diversion almost always leaves
        # SOME other trace (emergency, a deviation, a hold); a plane that
        # flew a completely normal profile and simply landed somewhere
        # "unexpected" per adsbdb's filed route is far more likely a stale/
        # wrong SCHEDULE match (e.g. DLH8NK: adsbdb said EDDF->LEMG
        # (Malaga), landed EDDM (Munich) — an extremely common, routine
        # Lufthansa shuttle hop, not a dramatic unreported diversion).
        # Unlike the ground-state check above (which validates the
        # OBSERVATION — is this aircraft really on the ground here), this
        # validates the REFERENCE DATA the whole detector is built on: is
        # the FILED destination itself trustworthy? hexdb.io is a second,
        # independent route source (already used as main.py's tier1_loop
        # resolution-time fallback, MASTERPLAN.md sectie 5) — if it names a
        # DIFFERENT destination for this exact callsign, that's real doubt
        # on the reference adsbdb data, not on the landing observation
        # itself. Deliberately its own flag/block, NOT nested under
        # cross_provider_consensus_enabled above — that flag is about ADS-B
        # PROVIDER consensus (adsb.lol vs airplanes.live), an unrelated
        # concern from ROUTE-SOURCE consensus (adsbdb vs hexdb.io); nesting
        # it there would have silently disabled this check whenever a user
        # turns off provider consensus for an unrelated reason. Only
        # downgrades on an active DISAGREEMENT, same pattern as
        # cross_provider_agrees above — no hexdb.io data at all leaves
        # confidence unchanged (unconfirmed, not penalized).
        if ev.event_type == "wrong_airport" and cfg.get("route_secondary_source_enabled") and ev.callsign:
            hexdb_pair = await providers.lookup_route_hexdb(session, ev.callsign)
            if hexdb_pair is not None and hexdb_pair[1] != ev.dest_icao:
                ev.confidence = "WAARSCHIJNLIJK"
                ev.message += (
                    f" (tweede routebron (hexdb.io) noemt {hexdb_pair[1]} als bestemming i.p.v. "
                    f"{ev.dest_icao} — mogelijk verouderde/foute scheduledata bij de eerste bron)"
                )
                log.warning("wrong_airport voor %s: hexdb.io route disagreement (%s vs adsbdb's %s), gedegradeerd",
                            ev.callsign, hexdb_pair[1], ev.dest_icao)

        # Same second-opinion-on-the-REFERENCE-DATA idea as the wrong_airport
        # check above, extended to the other two detectors that depend on
        # track.route["destination_icao"] the same way (BACKTEST_LOG.md
        # ronde 24). premature_descent and signal_lost_near_airport both only
        # fire because the observed altitude/position looks wrong relative
        # to adsbdb's FILED destination — if that destination is itself
        # stale/wrong, the underlying observation can be completely routine.
        # Live data pulled this round (2026-08-07,
        # http://35.227.51.25:8787/api/events) showed the exact same
        # adsbdb-route-mismatch shape rounds 16-18 already found for
        # wrong_airport, often even more starkly for signal_lost_near_
        # airport: RYR49MG (adsbdb filed LROP->EGGD, signal lost near LIRA)
        # — hexdb.io's own route is EYVI->LIRA, i.e. hexdb's DESTINATION is
        # exactly the airport this event flagged as "not the destination".
        # Same shape for SAS80M (filed ENGM->EHAM, lost near ENGM; hexdb
        # says EHAM->ENGM), MEA307 (filed OLBA->EDDF, lost near OLBA; hexdb
        # says HECA->OLBA), NSZ8FI, EIN860, TAP1863 — 6 of 8 sampled hexdb.io
        # lookups this round independently named a DIFFERENT destination
        # than adsbdb's filed one, and in every one of those 6 that
        # destination was exactly the "unexpected" airport the event fired
        # on. premature_descent's live noise (round 16: 44/200 events, 22%)
        # was already established as plausibly the same root cause there,
        # though round 16 deliberately fixed it via a duration/threshold
        # tune instead (premature_descent_samples/min_drop_ft) — a
        # genuinely DIFFERENT, complementary mechanism (that fix separates a
        # brief step-down from a sustained descent; this one separates a
        # trustworthy filed destination from an untrustworthy one), so both
        # coexist without redundancy.
        #
        # UNLIKE wrong_airport, neither detector ever starts above MOGELIJK
        # (see their docstrings in detector.py — both are deliberately the
        # WEAKEST, most-provisional tier: an inferred/unconfirmed signal,
        # not physical proof like a landing). There is therefore no lower
        # confidence label to downgrade INTO, and incidents.py's
        # score_for_event doesn't even consult ev.confidence for these two
        # event types (confirmed by reading it — only `emergency` does), so
        # relabeling confidence here would be purely cosmetic — worse,
        # setting it to WAARSCHIJNLIJK would read as MORE trusted than the
        # plain MOGELIJK case, the opposite of the intent. Instead: the
        # dedicated Event.route_source_disputed flag (see detector.py),
        # which incidents.py's score_for_event uses to give a disputed hit
        # reduced evidence weight instead of the normal one — a single
        # doubted hit alone then stays below the dashboard-visibility
        # threshold, while genuine corroboration from ANY other detector can
        # still surface it. Deliberately never a hard suppression (no early
        # return / dropped event): BACKTEST_LOG.md ronde 21's reverted
        # same-metro-exclusion attempt for signal_lost_near_airport is the
        # direct cautionary precedent — that detector is a single
        # last-known-position snapshot with no safety net (UA2078's real
        # diversion landed only ~15nm from its filed destination; an
        # unconditional exclusion there would have silently and permanently
        # lost it). A soft weight reduction preserves detection even if
        # hexdb.io's own data happens to also be wrong/stale, at the cost of
        # somewhat less certainty when hexdb.io is right — same
        # "disagreement downgrades, silence doesn't" philosophy as the
        # wrong_airport check above, just landing on a weight instead of a
        # confidence label. Deliberately the SAME route_secondary_source_
        # enabled gate as wrong_airport's check — both are the identical
        # "trust a second route source over the first" concern.
        if (ev.event_type in ("premature_descent", "signal_lost_near_airport")
                and cfg.get("route_secondary_source_enabled") and ev.callsign):
            hexdb_pair = await providers.lookup_route_hexdb(session, ev.callsign)
            if hexdb_pair is not None and hexdb_pair[1] != ev.dest_icao:
                ev.route_source_disputed = True
                ev.message += (
                    f" (tweede routebron (hexdb.io) noemt {hexdb_pair[1]} als bestemming i.p.v. "
                    f"{ev.dest_icao} — mogelijk verouderde/foute scheduledata bij de eerste bron)"
                )
                log.warning("%s voor %s: hexdb.io route disagreement (%s vs adsbdb's %s), evidence-gewicht verlaagd",
                            ev.event_type, ev.callsign, hexdb_pair[1], ev.dest_icao)

        # The 'emergency status' ADS-B subfield (nordo/lifeguard/minfuel/...)
        # is a separate, much less reliable signal than the 7700/7600/7500
        # squawk codes — see providers.cross_provider_confirms_emergency's
        # docstring for the live evidence. Squawk-code emergencies stay
        # trusted as-is; the status-field path needs extra scrutiny.
        if ev.event_type == "emergency" and ev.squawk not in providers.EMERGENCY_SQUAWKS:
            if ev.squawk in providers.CONSPICUITY_SQUAWKS:
                # Live-confirmed decode artifact, not just theorized
                # (MASTERPLAN.md sectie 1c/7): 3 independent cases
                # (AUA96J/AUA12K/CFG6UA, all squawk 1000) showed a bogus
                # emergency-status value with cross-provider AGREEMENT
                # rather than disagreement — adsb.lol and airplanes.live
                # have overlapping feeder coverage and share the same
                # decode convention for this subfield, so agreement here
                # isn't independent confirmation the way it is for ground
                # state. Cap instead of trusting corroboration to clear it.
                ev.confidence = "WAARSCHIJNLIJK"
                ev.message += " (squawk is een conspicuity-code, geen ATC-toegewezen squawk — emergency-status hierbij live onbetrouwbaar gebleken)"
                log.info("emergency-status op conspicuity-squawk %s voor %s, capped op WAARSCHIJNLIJK", ev.squawk, ev.callsign or ev.hex)
            elif cfg["cross_provider_consensus_enabled"]:
                confirmed = await providers.cross_provider_confirms_emergency(session, ev.hex)
                if confirmed is False:
                    ev.confidence = "WAARSCHIJNLIJK"
                    ev.message += " (niet bevestigd door tweede bron — mogelijk onjuiste data)"
                    log.warning("emergency-status niet bevestigd door tweede provider voor %s, gedegradeerd", ev.callsign or ev.hex)

        if cfg["weather_enrichment_enabled"] and ev.weather_icao:
            metar = await weather.get_metar(session, ev.weather_icao)
            ev.message += weather.describe(metar)

        if cfg["fr24_confirm_enabled"] and ev.confidence == "MOGELIJK" and ev.lat is not None:
            import fr24_confirm
            confirmation = await fr24_confirm.confirm_diversion(ev.lat, ev.lon, ev.callsign)
            if confirmation:
                ev.confidence = "WAARSCHIJNLIJK"
                ev.message += f" [{confirmation}]"

        log.info("EVIDENCE [%s/%s] %s: %s", ev.confidence, ev.event_type, ev.callsign or ev.hex, ev.message)
    return events


async def _dispatch_transitions(session, cfg: dict, transitions: list):
    for t in transitions:
        log.info("INCIDENT [%s->%s/%s] %s: score %.0f",
                  t["old_state"], t["new_state"], t["kind"],
                  t["incident"].get("callsign") or t["incident"]["hex"], t["incident"]["score"])
        await notify_incident_transition(session, cfg, t)


async def tier0_loop(session, store: TrackStore, incident_mgr: IncidentManager, cfg: dict, db_conn=None):
    """Fast global sweep for declared emergencies (squawk 7700/7600/7500, or
    the ADS-B 'emergency' status field). Cheap: ~6 tiny requests per cycle."""
    while True:
        try:
            snapshot = await fetch_squawk_sweep(session)
            now = time.time()
            events_by_hex: dict[str, list] = {}
            for ac in snapshot.values():
                track = store.update(ac, now)
                ev = detect_emergency(track, ac)
                if ev:
                    events_by_hex.setdefault(ac["hex"], []).append(ev)

            all_events = [ev for evs in events_by_hex.values() for ev in evs]
            await enrich_events(session, cfg, all_events)
            for ev in all_events:
                if db_conn is not None:
                    db_module.save_event(db_conn, ev)

            transitions = []
            for hex_id, evs in events_by_hex.items():
                track = store.tracks.get(hex_id)
                ac = snapshot.get(hex_id)
                transitions.extend(incident_mgr.step(
                    hex_id, track.callsign if track else "", track.aircraft_class if track else None,
                    evs, ac, now,
                ))
            await _dispatch_transitions(session, cfg, transitions)

            if db_conn is not None:
                db_module.save_sweep_status(db_conn, tier0_ts=now)
        except Exception:
            log.exception("tier0 loop error")
        await asyncio.sleep(cfg["tier0_interval_seconds"])


async def tier1_loop(session, store: TrackStore, airport_db: AirportDB, incident_mgr: IncidentManager, cfg: dict, db_conn):
    """Slower worldwide state-vector sweep: tracks trajectories, resolves
    expected routes for scheduled-airline callsigns, and runs the full
    diversion heuristics."""
    while True:
        try:
            snapshot = await fetch_world_snapshot(session)
            now = time.time()
            previously_tracked = set(store.tracks.keys())

            pending_lookup = []
            pending_class_lookup = []
            for ac in snapshot.values():
                track = store.get_or_create(ac["hex"])
                if (not track.route_checked and ac["callsign"]
                        and AIRLINE_CALLSIGN_RE.match(ac["callsign"])):
                    pending_lookup.append(ac["callsign"])

                # Classify here (cheap, synchronous — dbFlags/category/
                # callsign only) rather than in the main per-aircraft loop
                # below, so any UNKNOWN result can be queued for the async
                # hexdb.io refinement batch-fetched next, mirroring the
                # route-lookup pattern above. Refreshed only after the
                # cache TTL — aircraft type/operator can't change
                # mid-flight. See classify.py / MASTERPLAN.md sectie 4.
                if (track.aircraft_class is None
                        or now - (track.aircraft_class_ts or 0) > cfg["classification_cache_ttl_hours"] * 3600):
                    track.aircraft_class = classify.classify(ac)
                    track.aircraft_class_ts = now
                    track.hexdb_aircraft_checked = False  # fresh classification, allow a fresh refinement attempt

                if (cfg["route_secondary_source_enabled"] and track.aircraft_class == classify.UNKNOWN
                        and not track.hexdb_aircraft_checked):
                    pending_class_lookup.append(ac["hex"])

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
                routes = await asyncio.gather(*(lookup_route(session, cs, cfg) for cs in to_lookup))
                resolved_routes = dict(zip(to_lookup, routes))

                if cfg["route_secondary_source_enabled"]:
                    # hexdb.io fallback (MASTERPLAN.md sectie 5): only for
                    # callsigns adsbdb had nothing for, not queried in
                    # parallel for everyone — a second free, independent
                    # source, but no reason to double the request volume
                    # for callsigns adsbdb already resolved. hexdb.io only
                    # returns an ICAO pair, not coordinates, so resolve
                    # those via the local airport DB.
                    still_missing = [cs for cs, r in resolved_routes.items() if r is None]
                    if still_missing:
                        hexdb_pairs = await asyncio.gather(
                            *(providers.lookup_route_hexdb(session, cs) for cs in still_missing)
                        )
                        for cs, pair in zip(still_missing, hexdb_pairs):
                            if pair is None:
                                continue
                            origin_ap, dest_ap = airport_db.get(pair[0]), airport_db.get(pair[1])
                            if origin_ap and dest_ap:
                                resolved_routes[cs] = {
                                    "origin_icao": origin_ap["icao"], "origin_lat": origin_ap["lat"], "origin_lon": origin_ap["lon"],
                                    "destination_icao": dest_ap["icao"], "destination_lat": dest_ap["lat"], "destination_lon": dest_ap["lon"],
                                    "airline": None,
                                }

                for cs, base_route in resolved_routes.items():
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
                                    log.info("route voor %s lijkt niet te kloppen met huidige positie, genegeerd", cs)
                                    route = None
                            t.route = route
                            t.route_resolved_ts = now if route is not None else None
                            # A route-less result only counts as "checked" (i.e.
                            # not retried again THIS cycle) once adsbdb gave a
                            # definitive answer that also isn't due for its
                            # long-term negative retry. If the lookup merely
                            # failed to reach adsbdb (route_lookup_pending_retry)
                            # or the cached negative has aged past
                            # route_lookup_negative_retry_hours
                            # (route_lookup_negative_retry_due — adsbdb/hexdb.io
                            # are crowdsourced and grow over time, so a
                            # permanent "no route" cache would stay wrong
                            # forever), this stays False so the SAME track
                            # gets retried. Known remaining gap: once
                            # route_checked has latched True during an
                            # earlier "not yet due" window, nothing here
                            # proactively flips it back to False when the
                            # window later opens for a still-tracked (not
                            # re-created) aircraft — only a track that's
                            # re-created (e.g. a later flight, same
                            # callsign) benefits automatically. Acceptable
                            # for now: narrower and less severe than the
                            # "permanent forever" gap this replaces. See
                            # MASTERPLAN.md sectie 5, punt 4.
                            t.route_checked = route is not None or (
                                not providers.route_lookup_pending_retry(cs)
                                and not providers.route_lookup_negative_retry_due(cs, cfg)
                            )

            if pending_class_lookup:
                to_class_lookup = (random.sample(pending_class_lookup, CLASS_LOOKUP_BATCH)
                                    if len(pending_class_lookup) > CLASS_LOOKUP_BATCH else pending_class_lookup)
                hexdb_aircraft_results = await asyncio.gather(
                    *(providers.lookup_aircraft_hexdb(session, h) for h in to_class_lookup)
                )
                for hex_id, hexdb_ac in zip(to_class_lookup, hexdb_aircraft_results):
                    t = store.tracks.get(hex_id)
                    if not t:
                        continue
                    t.aircraft_class = classify.refine_with_hexdb(t.aircraft_class, hexdb_ac)
                    t.hexdb_aircraft_checked = True

            events_by_hex: dict[str, list] = {}
            for ac in snapshot.values():
                # track.was_on_ground still holds the previous cycle's state here,
                # which is what detect_landed_wrong_airport needs to catch the edge.
                track = store.update(ac, now)
                # (classification already happened in the first pass above,
                # so any hexdb.io refinement batch this cycle is reflected
                # here too)

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
                    # Strict (check_progress=True) only within
                    # ROUTE_REVALIDATION_WINDOW_S of resolution — long enough
                    # to catch a callsign collision that looked plausible by
                    # chance at first and reveals itself soon after (the
                    # original motivating case), short enough that it can't
                    # also strip a real, still-developing diversion. Past
                    # that window: cross-track-only, see route_plausible's
                    # docstring for why no fixed progress threshold can do
                    # both jobs at once.
                    within_revalidation_window = (
                        track.route_resolved_ts is not None
                        and now - track.route_resolved_ts < ROUTE_REVALIDATION_WINDOW_S
                    )
                    if not route_plausible(
                        track.route["origin_lat"], track.route["origin_lon"],
                        track.route["destination_lat"], track.route["destination_lon"],
                        ac["lat"], ac["lon"], check_progress=within_revalidation_window,
                    ):
                        log.info("route voor %s niet meer plausibel gezien huidige positie, losgelaten", track.callsign)
                        track.route = None
                        track.route_checked = False

                skip_behavioral = (
                    cfg["classification_suppress_non_airliner"]
                    and track.aircraft_class in classify.SUPPRESSED_CLASSES
                )
                ac_events = evaluate(track, ac, airport_db, cfg, db_conn, skip_behavioral=skip_behavioral)
                if ac_events:
                    events_by_hex.setdefault(track.hex, []).extend(ac_events)

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
                    skip = (cfg["classification_suppress_non_airliner"]
                            and t.aircraft_class in classify.SUPPRESSED_CLASSES)
                    if not skip:
                        ev = detect_signal_lost_near_airport(t, airport_db, cfg)
                        if ev:
                            events_by_hex.setdefault(hex_id, []).append(ev)

            all_events = [ev for evs in events_by_hex.values() for ev in evs]
            await enrich_events(session, cfg, all_events)
            for ev in all_events:
                if db_conn is not None:
                    db_module.save_event(db_conn, ev)

            # Fetched once per cycle (SIGMET/CWA polygons change far more
            # slowly than aircraft positions, cached internally per
            # weather_sigmet_refresh_seconds anyway) and passed synchronously
            # into every step() call this cycle — see incidents.py's
            # reassess() docstring for why the fetch lives here, not there.
            hazards = await airspace.get_active_hazards(session, cfg) if cfg["weather_sigmet_enabled"] else None

            transitions = []
            # Aircraft seen this cycle: step() applies fresh evidence (if
            # any) and, when there was none, runs decay/recovery/weather/
            # peer-consensus/landing checks — MASTERPLAN.md sectie 3.5/6.
            for ac in snapshot.values():
                hex_id = ac["hex"]
                track = store.tracks.get(hex_id)
                if track is None:
                    continue
                transitions.extend(incident_mgr.step(
                    hex_id, track.callsign, track.aircraft_class, events_by_hex.get(hex_id, []), ac, now, hazards,
                ))
            # Open incidents for aircraft NOT in this cycle's snapshot at
            # all (signal lost, or just a gap) still get a reassessment
            # pass with ac=None — step()'s decay logic still applies, its
            # recovery/landing/weather checks just no-op without live
            # position data.
            for hex_id in incident_mgr.open_hexes():
                if hex_id in snapshot:
                    continue
                transitions.extend(incident_mgr.step(hex_id, "", None, events_by_hex.get(hex_id, []), None, now, hazards))
            await _dispatch_transitions(session, cfg, transitions)

            db_module.save_sweep_status(db_conn, tracked_count=len(snapshot), tier1_ts=now)
            store.prune(now)
            log.info("tier1 cycle: %d aircraft tracked, %d events, %d incident transitions",
                      len(snapshot), len(all_events), len(transitions))
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

    incident_mgr = IncidentManager(db_conn, cfg, airport_db)
    log.info("%d open incidenten geladen uit vorige sessie.", len(incident_mgr.open_hexes()))

    # Windows' default OpenSSL cert path (ssl.get_default_verify_paths().openssl_cafile)
    # doesn't exist on this machine, so aiohttp's default context has no usable CA
    # bundle. Point it at certifi's instead.
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            await asyncio.gather(
                tier0_loop(session, store, incident_mgr, cfg, db_conn),
                tier1_loop(session, store, airport_db, incident_mgr, cfg, db_conn),
            )
    finally:
        db_conn.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Gestopt.")
