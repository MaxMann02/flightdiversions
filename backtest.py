"""Backtests detector.py's heuristics against real, documented diversion
incidents (see backtest_cases.py).

We don't have raw historical ADS-B recordings for these flights, so each
case is a synthetic track built from public incident reports (position,
altitude, timing) — the goal is to test the DETECTION LOGIC against
realistic geometry and timing, not to byte-for-byte replay a historical
feed. Every case cites its source; keep the assumptions that fill gaps
between reported facts (headings, intermediate positions, squawk) called
out in each case's `notes`.

Run: `python backtest.py`
"""
import math
from dataclasses import dataclass, field

from airports import (AirportDB, ROUTE_REVALIDATION_WINDOW_S, bearing_deg,
                      route_corroborated_by_progress, route_plausible)
from config import CONFIG
from detector import detect_emergency, detect_signal_lost_near_airport, evaluate
from state import TrackStore


@dataclass
class Sample:
    t: float  # seconds since case start
    lat: float
    lon: float
    alt_baro: float | None  # None == on the ground
    track: float | None
    squawk: str = "1200"
    emergency: str = "none"


@dataclass
class Case:
    name: str
    source: str
    hex_id: str
    callsign: str
    origin_icao: str
    dest_icao: str
    samples: list  # list[Sample], t ascending
    expected_type: str
    # Seconds-since-case-start of the real crew/ATC decision this detector
    # should have anticipated or confirmed (see each case's notes for what
    # exactly this marks).
    real_decision_t: float
    real_decision_label: str
    notes: str = ""
    milestones: list = field(default_factory=list)  # [(label, t), ...] for the printed report
    # If set, the track goes missing after the last sample instead of
    # landing cleanly — simulates the ADS-B-coverage-drop scenario
    # detect_signal_lost_near_airport exists for (ac.py's tier1_loop only
    # calls it once a track has been absent for signal_lost_missing_cycles
    # consecutive cycles; this harness had no equivalent until this field
    # was added, so that detector had never actually been exercised here).
    goes_missing: bool = False


def _ac_from_sample(s: Sample, hex_id: str, callsign: str) -> dict:
    return {
        "hex": hex_id, "callsign": callsign, "lat": s.lat, "lon": s.lon,
        "alt_baro": s.alt_baro, "on_ground": s.alt_baro is None, "track": s.track,
        "squawk": s.squawk, "emergency": s.emergency,
    }


def run_case(case: Case, airport_db: AirportDB, cfg: dict | None = None,
             incident_mgr=None, aircraft_class: str = "AIRLINER") -> list:
    """Replays a case's samples through the real detector functions in the
    same order/state-mutation sequence main.py's tier0/tier1 loops use.
    Returns a list of (t, Event) for every event the detectors raised —
    NOT de-duped by cooldown, since we want to see every raw detector hit,
    not just what would have cleared the alert cooldown.

    incident_mgr: optional incidents.IncidentManager — when given, every
    cycle's fresh events (plus reassessment when there are none) are also
    fed through IncidentManager.step(), exactly mirroring how main.py's
    tier1_loop drives it per aircraft per cycle. Lets a real, sourced Case
    exercise the incident-lifecycle engine's actual scoring/state machine,
    not just detector.py's geometry — see check_incident_engine_real_case_
    escalation (BACKTEST_LOG.md ronde 16)."""
    cfg = cfg or CONFIG
    store = TrackStore()
    origin = airport_db.get(case.origin_icao)
    dest = airport_db.get(case.dest_icao)
    fired = []

    for s in case.samples:
        ac = _ac_from_sample(s, case.hex_id, case.callsign)
        track = store.update(ac, s.t)
        if track.route is None and not track.route_checked and origin and dest:
            # Modelled as resolved from the start (ground truth, not a real
            # adsbdb lookup) — this harness tests the geometry/altitude
            # detectors, not tier2's lookup latency. But still gated on
            # route_checked and still run through the STRICT plausibility
            # check (check_progress defaults to True), mirroring main.py's
            # resolution-time call site — needed so a track whose route was
            # cleared by the ongoing recheck below and is being re-resolved
            # behaves the same way main.py's re-lookup-against-_route_cache
            # does: a still-implausible position at (re-)resolution time
            # permanently latches route_checked=True with route=None.
            candidate = {
                "origin_icao": case.origin_icao, "origin_lat": origin["lat"], "origin_lon": origin["lon"],
                "destination_icao": case.dest_icao, "destination_lat": dest["lat"], "destination_lon": dest["lon"],
            }
            if ac.get("lat") is not None and not route_plausible(
                candidate["origin_lat"], candidate["origin_lon"],
                candidate["destination_lat"], candidate["destination_lon"],
                ac["lat"], ac["lon"],
            ):
                track.route = None
                track.route_resolved_ts = None
            else:
                track.route = candidate
                track.route_resolved_ts = s.t
            track.route_checked = True

        # Mirrors main.py's tier1_loop: re-check plausibility every cycle
        # while airborne. Strict (check_progress=True) only within
        # ROUTE_REVALIDATION_WINDOW_S of resolution, relaxed (cross-track-
        # only) after that — see route_plausible's check_progress docstring.
        # Needed so a case like ATL->MCO->FLL below (a genuine diversion
        # that overflies its destination in a near-straight line, well
        # outside that window) actually exercises whether track.route
        # survives to the eventual landing, not just whether the geometry
        # detectors fire along the way. Resets route_checked on failure too
        # (mirrors main.py) so the block above gets a chance to re-resolve
        # (and, per its own strict check, re-reject) on the next cycle.
        if track.route and ac.get("lat") is not None and not ac["on_ground"]:
            within_revalidation_window = (
                track.route_resolved_ts is not None
                and s.t - track.route_resolved_ts < ROUTE_REVALIDATION_WINDOW_S
            )
            if not route_plausible(
                track.route["origin_lat"], track.route["origin_lon"],
                track.route["destination_lat"], track.route["destination_lon"],
                ac["lat"], ac["lon"], check_progress=within_revalidation_window,
            ):
                track.route = None
                track.route_checked = False
                track.route_corroborated = False

        # Mirrors main.py's tier1_loop: positive route corroboration — we
        # watched this aircraft fly a substantial part of the filed route
        # toward the filed DESTINATION. Needed here so a real, sourced Case
        # exercises incidents.py's BEVESTIGD gate under the same route-trust
        # conditions production would have, instead of silently never being
        # corroborated (which would make every case look artificially
        # unconfirmable). See CHECKPOINT.md bevinding 2 en 9.
        if track.route and not track.route_corroborated and ac.get("lat") is not None:
            if route_corroborated_by_progress(
                track.route["origin_lat"], track.route["origin_lon"],
                track.route["destination_lat"], track.route["destination_lon"],
                ac["lat"], ac["lon"],
            ):
                track.route_corroborated = True

        # Mirrors main.py's tier1_loop: just_took_off is computed from the
        # PRIOR cycle's ground state, before it's updated below — needed so
        # detect_landed_wrong_airport's early-return check has a real
        # last_takeoff_ts to compare against for cases that start on the
        # ground (see _early_return_* cases in backtest_cases.py).
        just_took_off = (not ac["on_ground"]) and track.was_on_ground is True

        cycle_events = []
        ev = detect_emergency(track, ac)
        if ev:
            cycle_events.append(ev)
        cycle_events.extend(evaluate(track, ac, airport_db, cfg, db_conn=None))
        for ev in cycle_events:
            fired.append((s.t, ev))

        if just_took_off:
            # Mirrors main.py's tier1_loop record_takeoff call. Sets BOTH
            # last_takeoff_ts (for detect_landed_wrong_airport's early-return
            # window) and pending_origin — the latter is what the observed-
            # departure branch of the route-corroboration check above reads,
            # so without it an early-return case like TO3510 could never be
            # corroborated here even though production would corroborate it
            # immediately. See CHECKPOINT.md bevinding 2.
            departure_ap, _ = airport_db.nearest(s.lat, s.lon, max_km=15.0)
            store.record_takeoff(track, departure_ap, s.t)

        # Mirrors main.py's tier1_loop: was_on_ground only updates AFTER
        # evaluate() runs, so detect_landed_wrong_airport sees the PRIOR
        # cycle's ground state when it checks for a fresh landing.
        track.was_on_ground = ac["on_ground"]

        if incident_mgr is not None:
            incident_mgr.step(case.hex_id, case.callsign, aircraft_class, cycle_events, ac, s.t, hazards=None)

    if case.goes_missing and case.samples:
        # Mirrors main.py's tier1_loop: consecutive tier1 cycles absent
        # from the snapshot increment missing_cycles; once it hits the
        # configured threshold, check whether the track disappeared low
        # and close to a non-destination airport.
        last_t = case.samples[-1].t
        track.missing_cycles = cfg["signal_lost_missing_cycles"]
        ev = detect_signal_lost_near_airport(track, airport_db, cfg)
        if ev:
            missing_t = last_t + cfg["signal_lost_missing_cycles"] * cfg["tier1_interval_seconds"]
            fired.append((missing_t, ev))
            # Mirrors main.py's tier1_loop, which puts this event into
            # events_by_hex like any other and therefore feeds it to
            # IncidentManager.step(). This harness used to append it to
            # `fired` only, so signal_lost_near_airport was the one detector
            # whose evidence never reached the incident engine at all in a
            # backtest — invisible while the engine was only exercised by
            # hand-built Events, but wrong as soon as a real case's incident
            # outcome is asserted (check_real_case_confidence_outcomes).
            # ac=None mirrors production: the aircraft is, by definition of
            # this case, absent from the snapshot.
            ev.route_corroborated = bool(track.route_corroborated)
            if incident_mgr is not None:
                incident_mgr.step(case.hex_id, case.callsign, aircraft_class, [ev], None, missing_t)

    return fired


def fmt_t(seconds: float) -> str:
    sign = "-" if seconds < 0 else "+"
    seconds = abs(seconds)
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{sign}{h}h{m:02d}m" if h else f"{sign}{m}m{s:02d}s"


def report_case(case: Case, airport_db: AirportDB, cfg: dict | None = None) -> dict:
    fired = run_case(case, airport_db, cfg)
    matches = [(t, ev) for t, ev in fired if ev.event_type == case.expected_type]
    first_match = min(matches, key=lambda x: x[0]) if matches else None

    print(f"\n=== {case.name} ===")
    print(f"source: {case.source}")
    if case.notes:
        print(f"notes:  {case.notes}")
    for label, t in case.milestones:
        print(f"  [real] {fmt_t(t):>8}  {label}")
    if first_match:
        t, ev = first_match
        lead = case.real_decision_t - t
        verdict = "EARLIER than" if lead > 0 else "LATER than"
        print(f"  [dtct] {fmt_t(t):>8}  {ev.event_type}/{ev.confidence} fired — {ev.message}")
        print(f"         -> {fmt_t(abs(lead))} {verdict} real '{case.real_decision_label}' "
              f"({fmt_t(case.real_decision_t)})")
    else:
        print(f"  [dtct] NEVER fired expected type '{case.expected_type}' "
              f"(real '{case.real_decision_label}' at {fmt_t(case.real_decision_t)})")

    other_types = sorted({ev.event_type for _, ev in fired if ev.event_type != case.expected_type})
    if other_types:
        print(f"  (also fired: {', '.join(other_types)})")

    return {
        "case": case.name,
        "detected": first_match is not None,
        "detected_at": first_match[0] if first_match else None,
        "lead_seconds": (case.real_decision_t - first_match[0]) if first_match else None,
        "all_events": fired,
    }


# Real production data-quality issues, not diversion incidents — caught
# live 2026-08-06 shortly after first deployment. Both callsigns resolved
# to a filed adsbdb route that passed the OLD 1.5x route_plausible
# threshold despite being clearly wrong for the aircraft actually observed
# (AAL974: filed SBGL->JFK, but a domestic 737 near Dallas — a widebody-only
# international route mismatch; SWA2820: filed KMCO->KMHT, but mid a
# Chicago-Savannah rotation). Not Case objects like the incidents above:
# the point here isn't "does a detector fire", it's "does route_plausible
# correctly reject this at resolution so nothing downstream ever sees a
# route for it at all" — a Case's expected_type/real_decision_t framing
# doesn't fit a should-never-fire assertion. See BACKTEST_LOG.md round 7.
BAD_ROUTE_REGRESSIONS = [
    {
        "name": "AAL974 filed SBGL->JFK, actually a domestic 737 near Dallas",
        "origin": (-22.809999, -43.250557),   # SBGL
        "dest": (40.639801, -73.7789),         # KJFK
        "actual_pos": (32.815464, -97.167234),  # live position, 2026-08-06
    },
    {
        "name": "SWA2820 filed KMCO->KMHT, actually mid Chicago-Savannah rotation",
        "origin": (28.4294, -81.3090),    # KMCO
        "dest": (42.9326, -71.4357),       # KMHT
        "actual_pos": (37.941191, -85.617888),  # live position, 2026-08-06
    },
]


def check_bad_route_regressions() -> bool:
    print("\n=== route_plausible bad-data regression checks ===")
    all_ok = True
    for case in BAD_ROUTE_REGRESSIONS:
        rejected = not route_plausible(*case["origin"], *case["dest"], *case["actual_pos"])
        print(f"  {case['name']}: {'OK (correctly rejected)' if rejected else 'FAIL (would still be trusted!)'}")
        all_ok = all_ok and rejected
    return all_ok


def check_course_deviation_holding_suppression() -> bool:
    """DAL2564 (live, 2026-08-06): flagged course_deviation while actually
    circling in a holding pattern near an airport at 24000ft with no
    resolved route (adsbdb had no data, so detect_holding_pattern's own
    route gate meant it never even ran — see its docstring).

    Not a Case: the point is a should-mostly-NOT-fire assertion, and
    honestly, not an absolute one — the very first turn of a freshly-
    observed hold can still fire once (a repeating pattern can't be told
    apart from a real turn before it's actually repeated at least once).
    What the fix guarantees is that it does NOT keep re-firing on every
    subsequent turn of the same ongoing hold: each turn is its own
    detector hit, and course_deviation itself has no memory of "already
    alerted on this hold" (that's a detector-level concern this fix
    addresses directly — the old alert_cooldown_seconds-gated dispatch,
    since removed in BACKTEST_LOG.md ronde 11, would have masked the
    SYMPTOM at the notification layer for holds with turns closer together
    than the cooldown window, but a hold with turns spaced further apart
    than that window would still have kept clearing it and firing again,
    turn after turn, for as long as the hold lasted). See
    BACKTEST_LOG.md round 7."""
    import math
    from detector import detect_course_deviation
    from state import AircraftTrack, TrackPoint

    print("\n=== course_deviation / holding-pattern suppression check ===")
    cfg = CONFIG

    # Realistic racetrack sampling at 60s: a straight leg (stable heading)
    # for a couple samples, then a ~180deg reversal turn, repeating — the
    # "stable, sudden change, held for one more sample" shape that used to
    # look identical to a genuine diversion turn every single lap.
    headings = [10, 10, 10, 10, 190, 190, 10, 10, 190, 190, 10, 10, 190, 190]
    track = AircraftTrack(hex="dal2564_regression")
    fired_at = []
    for i, hdg in enumerate(headings):
        lat = 28.5 + 0.03 * math.sin(math.radians(hdg))
        lon = -81.3 + 0.03 * math.cos(math.radians(hdg))
        track.history.append(TrackPoint(ts=i * 60.0, lat=lat, lon=lon, alt_baro=24000, track=hdg, on_ground=False))
        ev = detect_course_deviation(track, {"on_ground": False, "squawk": "1000"}, cfg)
        if ev:
            fired_at.append(i)
    no_repeats = len(fired_at) <= 1
    print(f"  DAL2564-style sustained hold, route unknown, 24000ft: fired at {fired_at or 'never'} "
          f"— {'OK (no repeated false positives)' if no_repeats else 'FAIL (still repeats)'}")

    # Sanity check: a genuine one-off sudden turn (not part of a repeating
    # pattern, far net displacement) must still fire — proves the
    # suppression isn't silencing course_deviation altogether.
    track2 = AircraftTrack(hex="real_diversion_regression")
    fired2 = False
    for i, hdg in enumerate([90, 90, 90, 210, 210]):
        track2.history.append(TrackPoint(ts=i * 60.0, lat=40.0 + i * 0.5, lon=-70.0 + i * 0.3,
                                          alt_baro=35000, track=hdg, on_ground=False))
        if detect_course_deviation(track2, {"on_ground": False, "squawk": "1000"}, cfg):
            fired2 = True
    print(f"  genuine one-off turn (not a hold): {'OK (still fires)' if fired2 else 'FAIL (suppression too broad!)'}")

    return no_repeats and fired2


def check_emergency_status_regressions() -> bool:
    """Live production data (2026-08-06, see MASTERPLAN.md sectie 1c):
    AUA96J squawked 1000 with the ADS-B emergency-status field set to
    'reserved' — a DO-260B-unassigned value that can never represent a
    real pilot action — and still reached BEVESTIGD/CONFIRMED on the
    dashboard. Fixed in providers._normalize_aircraft (normalizes
    'reserved' to 'none' at the source, so every downstream consumer sees
    it consistently). Not a Case: this tests the normalization function
    directly, same pattern as check_bad_route_regressions testing
    route_plausible directly rather than going through a full Case."""
    from providers import _normalize_aircraft

    print("\n=== emergency-status normalization regression check ===")
    normalized = _normalize_aircraft({"hex": "test", "emergency": "reserved", "squawk": "1000"})
    ok = normalized["emergency"] == "none"
    print(f"  emergency='reserved' normalized to 'none': {'OK' if ok else 'FAIL (still reserved!)'}")
    return ok


def check_corridor_deviation_bow_suppression() -> bool:
    """Live production data (2026-08-06, see MASTERPLAN.md sectie 1b/8):
    corridor_deviation fired on ordinary wind/airway-bowed routing with no
    other corroborating signal (AAL168 KPDX->KCLT 503nm off, ASH4002
    KIAH->KOKC 85nm off, NOZ1865 LIBD->ENGM 152nm off). The bow-tolerance
    check — previously scoped only to routes crossing the Russia/Siberia
    zone — now applies to every route: a bowed route whose CURRENT heading
    still points broadly toward the filed destination is treated as
    routine, non-diverting. Not a Case: a should-mostly-NOT-fire assertion
    (bowed but still inbound) contrasted with a should-still-fire one
    (bowed AND heading no longer toward the destination), same pattern as
    check_course_deviation_holding_suppression."""
    from detector import detect_route_corridor_deviation
    from state import AircraftTrack, TrackPoint

    print("\n=== corridor_deviation bow-tolerance suppression check ===")
    cfg = CONFIG
    origin = (30.0, -100.0)
    dest = (30.0, -60.0)
    # 8deg of latitude displacement from a same-latitude origin/dest pair —
    # verified (not guessed) to clear the firing threshold: the great
    # circle between two same-latitude points bows poleward, so at 4deg
    # north the cross-track distance (~146nm) was still UNDER this route's
    # ~207nm threshold; 8deg (~386nm) clears it with real margin.
    lat, lon = 38.0, -80.0
    bearing_to_dest = bearing_deg(lat, lon, dest[0], dest[1])

    def make_track(bearing_offset_deg: float) -> AircraftTrack:
        track = AircraftTrack(hex="bow_regression")
        track.route = {
            "origin_icao": "ORIG", "origin_lat": origin[0], "origin_lon": origin[1],
            "destination_icao": "DEST", "destination_lat": dest[0], "destination_lon": dest[1],
        }
        heading = (bearing_to_dest + bearing_offset_deg) % 360
        for i in range(cfg["corridor_deviation_min_samples"] + 1):
            track.history.append(TrackPoint(ts=i * 60.0, lat=lat, lon=lon + i * 0.01, alt_baro=38000, track=heading, on_ground=False))
        return track

    ac = {"on_ground": False, "squawk": "1200"}

    still_inbound = make_track(20.0)  # heading only 20deg off bearing-to-dest
    fired_inbound = None
    for _ in range(cfg["corridor_deviation_min_samples"] + 1):
        fired_inbound = detect_route_corridor_deviation(still_inbound, ac, cfg, airport_db=None)
    suppressed_ok = fired_inbound is None
    print(f"  bowed route, heading still ~toward destination: {'OK (suppressed)' if suppressed_ok else 'FAIL (still fired)'}")

    diverted = make_track(120.0)  # heading well off bearing-to-dest
    fired_diverted = None
    for _ in range(cfg["corridor_deviation_min_samples"] + 1):
        fired_diverted = detect_route_corridor_deviation(diverted, ac, cfg, airport_db=None)
    still_fires_ok = fired_diverted is not None
    print(f"  bowed route, heading no longer toward destination: {'OK (still fires)' if still_fires_ok else 'FAIL (suppression too broad!)'}")

    return suppressed_ok and still_fires_ok


def check_signal_lost_origin_suppression(airport_db: AirportDB) -> bool:
    """Live production data (2026-08-07, see BACKTEST_LOG.md ronde 17):
    signal_lost_near_airport fired for ~21% of its live hits (6/29 sampled)
    on aircraft last seen near their OWN filed ORIGIN, not some unrelated
    airport — a freshly-departed aircraft still climbing out below
    signal_lost_max_altitude_ft, whose ADS-B coverage briefly drops (common
    near smaller/regional fields during initial climb). The detector
    already excluded 'signal lost near the actual destination' as routine
    but had no equivalent exclusion for origin. Not a Case: a
    should-mostly-NOT-fire assertion (lost near origin) contrasted with a
    should-still-fire one (lost near a genuinely unrelated third airport),
    same pattern as check_corridor_deviation_bow_suppression."""
    from detector import detect_signal_lost_near_airport
    from state import AircraftTrack, TrackPoint

    print("\n=== signal_lost_near_airport origin-suppression check ===")
    cfg = CONFIG
    origin = airport_db.get("KIAH")
    dest = airport_db.get("KPHX")
    third = airport_db.get("KDFW")  # real airport, nowhere near IAH or PHX's own coordinates

    def make_track(pos: dict, alt_ft: float) -> AircraftTrack:
        track = AircraftTrack(hex="signal_lost_regression")
        track.route = {
            "origin_icao": "KIAH", "origin_lat": origin["lat"], "origin_lon": origin["lon"],
            "destination_icao": "KPHX", "destination_lat": dest["lat"], "destination_lon": dest["lon"],
        }
        track.history.append(TrackPoint(ts=0.0, lat=pos["lat"], lon=pos["lon"], alt_baro=alt_ft, track=90.0, on_ground=False))
        return track

    near_origin = make_track(origin, 4000.0)
    suppressed_ok = detect_signal_lost_near_airport(near_origin, airport_db, cfg) is None
    print(f"  last seen near own filed origin (freshly departed): {'OK (suppressed)' if suppressed_ok else 'FAIL (still fired)'}")

    near_third = make_track(third, 4000.0)
    fired = detect_signal_lost_near_airport(near_third, airport_db, cfg)
    still_fires_ok = fired is not None and fired.event_type == "signal_lost_near_airport"
    print(f"  last seen near an unrelated third airport: {'OK (still fires)' if still_fires_ok else 'FAIL (suppression too broad!)'}")

    return suppressed_ok and still_fires_ok


def check_holding_pattern_origin_gating(airport_db: AirportDB) -> bool:
    """Live data (2026-08-07, BACKTEST_LOG.md ronde 19): ~24% (5/21
    sampled) of live holding_pattern hits were sustained circling near the
    aircraft's OWN filed origin — the immediate, ungated 'non-destination'
    scoring tier fired on every one, despite the same class of explanation
    (adsbdb route-mismatch/regional-multi-leg-reuse) already fixed for
    signal_lost_near_airport's equivalent case in round 17. Unlike that
    fix (unconditional suppression, since signal_lost has no sustained-
    evidence mechanism), holding_pattern already tracks a streak — so this
    reuses the SAME long-streak patience already applied to destination
    holds instead of suppressing outright, so a genuine early-return-then-
    hold still has a chance to surface. Not a Case: a should-not-fire-
    immediately/should-eventually-fire assertion (gated), contrasted with
    an unrelated third airport (should still fire immediately, unaffected)
    — same pattern as check_signal_lost_origin_suppression above."""
    import math

    from detector import detect_holding_pattern
    from state import AircraftTrack, TrackPoint

    print("\n=== holding_pattern origin-streak gating check ===")
    cfg = CONFIG
    origin = airport_db.get("KIAH")
    dest = airport_db.get("KPHX")
    third = airport_db.get("KDFW")
    ac = {"on_ground": False}

    def make_track() -> AircraftTrack:
        track = AircraftTrack(hex="holding_origin_regression")
        track.route = {
            "origin_icao": "KIAH", "origin_lat": origin["lat"], "origin_lon": origin["lon"],
            "destination_icao": "KPHX", "destination_lat": dest["lat"], "destination_lon": dest["lon"],
        }
        return track

    def circle_sample(track: AircraftTrack, i: int, center: dict, lap_samples: int = 4, radius_nm: float = 4.0):
        deg_per_sample = 360.0 / lap_samples
        ang = math.radians((i * deg_per_sample) % 360)
        dlat = (radius_nm / 60.0) * math.cos(ang)
        dlon = (radius_nm / 60.0) * math.sin(ang) / math.cos(math.radians(center["lat"]))
        trk = (i * deg_per_sample + 90) % 360
        track.history.append(TrackPoint(ts=i * 60.0, lat=center["lat"] + dlat, lon=center["lon"] + dlon,
                                         alt_baro=8000, track=trk, on_ground=False))

    track1 = make_track()
    fired_early = None
    for i in range(cfg["holding_pattern_samples"] + 1):
        circle_sample(track1, i, origin)
        fired_early = detect_holding_pattern(track1, ac, airport_db, cfg)
    gated_ok = fired_early is None
    print(f"  circling near own origin, short streak: {'OK (gated)' if gated_ok else 'FAIL (fired immediately)'}")

    fired_late = None
    for i in range(cfg["holding_pattern_samples"] + 1, cfg["holding_pattern_samples"] + cfg["holding_pattern_destination_min_streak"] + 2):
        circle_sample(track1, i, origin)
        fired_late = detect_holding_pattern(track1, ac, airport_db, cfg)
    eventually_fires_ok = fired_late is not None and fired_late.at_destination is False
    print(f"  circling near own origin, sustained past the long streak: "
          f"{'OK (fires, stronger tier)' if eventually_fires_ok else f'FAIL (fired_late={fired_late})'}")

    track2 = make_track()
    fired_third = None
    for i in range(cfg["holding_pattern_samples"] + 1):
        circle_sample(track2, i, third)
        fired_third = detect_holding_pattern(track2, ac, airport_db, cfg)
    unrelated_fires_ok = fired_third is not None
    print(f"  circling near an unrelated third airport, short streak: "
          f"{'OK (still fires immediately)' if unrelated_fires_ok else 'FAIL (suppression too broad!)'}")

    return gated_ok and eventually_fires_ok and unrelated_fires_ok


def check_holding_pattern_same_metro(airport_db: AirportDB) -> bool:
    """Live data (2026-08-07, BACKTEST_LOG.md ronde 21): ~33% (7/21
    sampled) of live holding_pattern hits were near a DIFFERENT airport
    serving the SAME city as the filed destination (e.g. Dallas Love Field
    KDAL vs. the filed KDFW, 10nm apart) rather than an unrelated location —
    adsbdb schedule data naming one member of a metro pair while the
    aircraft is actually headed to the other. Fixed via a generic distance
    check (airports.airports_same_metro, 35nm), extending BOTH the
    destination and origin exclusions (not a hardcoded per-city list).
    Deliberately NOT applied to signal_lost_near_airport in the same round
    — that broke the real UA2078 case (Luke AFB is a genuine diversion
    target only 15nm from filed KPHX), see that detector's own comment.
    holding_pattern is safe from that failure mode because even a same-
    metro match still requires the same long streak as an exact match, so
    a genuine emergency hold near a metro-sister airport is delayed, not
    silently lost, the way a single-snapshot detector's suppression would
    be. Not a Case: a should-be-gated-like-destination assertion, same
    pattern as check_holding_pattern_origin_gating above — uses real,
    verified airport coordinates (KDFW/KDAL, confirmed 10nm apart) rather
    than synthetic ones."""
    import math

    from detector import detect_holding_pattern
    from state import AircraftTrack, TrackPoint

    print("\n=== holding_pattern same-metro-area gating check ===")
    cfg = CONFIG
    filed_dest = airport_db.get("KDFW")
    sister = airport_db.get("KDAL")  # real sister airport, ~10nm from KDFW, NOT an exact ICAO match
    origin = airport_db.get("KIAH")
    ac = {"on_ground": False}

    def make_track() -> AircraftTrack:
        track = AircraftTrack(hex="holding_metro_regression")
        track.route = {
            "origin_icao": "KIAH", "origin_lat": origin["lat"], "origin_lon": origin["lon"],
            "destination_icao": "KDFW", "destination_lat": filed_dest["lat"], "destination_lon": filed_dest["lon"],
        }
        return track

    def circle_sample(track: AircraftTrack, i: int, center: dict, lap_samples: int = 4, radius_nm: float = 4.0):
        deg_per_sample = 360.0 / lap_samples
        ang = math.radians((i * deg_per_sample) % 360)
        dlat = (radius_nm / 60.0) * math.cos(ang)
        dlon = (radius_nm / 60.0) * math.sin(ang) / math.cos(math.radians(center["lat"]))
        trk = (i * deg_per_sample + 90) % 360
        track.history.append(TrackPoint(ts=i * 60.0, lat=center["lat"] + dlat, lon=center["lon"] + dlon,
                                         alt_baro=8000, track=trk, on_ground=False))

    track = make_track()
    fired_early = None
    for i in range(cfg["holding_pattern_samples"] + 1):
        circle_sample(track, i, sister)
        fired_early = detect_holding_pattern(track, ac, airport_db, cfg)
    gated_ok = fired_early is None
    print(f"  circling near destination's metro-sister airport, short streak: {'OK (gated)' if gated_ok else 'FAIL (fired immediately)'}")

    fired_late = None
    for i in range(cfg["holding_pattern_samples"] + 1, cfg["holding_pattern_samples"] + cfg["holding_pattern_destination_min_streak"] + 2):
        circle_sample(track, i, sister)
        fired_late = detect_holding_pattern(track, ac, airport_db, cfg)
    eventually_fires_ok = fired_late is not None and fired_late.at_destination is True
    print(f"  circling near metro-sister airport, sustained past the long streak: "
          f"{'OK (fires, at_destination tier)' if eventually_fires_ok else f'FAIL (fired_late={fired_late})'}")

    return gated_ok and eventually_fires_ok


def check_classification_regressions() -> bool:
    """classify.py's decision order (MASTERPLAN.md sectie 4), sanity-checked
    against real live dbFlags/category values seen 2026-08-06 via
    api.adsb.lol/v2/mil (an H60 and an AS65 helicopter and a C-17 transport,
    all dbFlags=1, categories A7/A7/A5 respectively — confirms dbFlags
    correctly wins over category) plus synthetic cases for the branches that
    snapshot didn't happen to cover (a military feed only returns military
    aircraft, so it can't exercise the non-military branches)."""
    import classify

    print("\n=== aircraft classification regression check ===")
    cases = [
        ("military dbFlags wins over rotorcraft category (live: AS65, cat A7, dbFlags=1)",
         {"db_flags": 1, "category": "A7", "callsign": "", "type": "AS65"}, classify.MILITARY),
        ("military dbFlags wins over heavy category (live: C-17, cat A5, dbFlags=1)",
         {"db_flags": 1, "category": "A5", "callsign": "TIGER11", "type": "C17"}, classify.MILITARY),
        ("civilian rotorcraft, no military flag",
         {"db_flags": 0, "category": "A7", "callsign": "", "type": "EC35"}, classify.HELICOPTER),
        ("UAV/drone category",
         {"db_flags": 0, "category": "B6", "callsign": "", "type": ""}, classify.LIGHT_OTHER),
        ("airline callsign + airliner-scale category -> AIRLINER",
         {"db_flags": 0, "category": "A3", "callsign": "AAL168", "type": "B738"}, classify.AIRLINER),
        ("light category, non-airline callsign -> GA_PRIVE",
         {"db_flags": 0, "category": "A1", "callsign": "N12345", "type": "C172"}, classify.GA_PRIVATE),
        ("large-category bizjet type, no airline callsign -> ZAKENJET via type fallback",
         {"db_flags": 0, "category": "A3", "callsign": "", "type": "GLF6"}, classify.BUSINESS_JET),
        ("nothing recognizable -> ONBEKEND (not suppressed)",
         {"db_flags": 0, "category": "", "callsign": "", "type": ""}, classify.UNKNOWN),
    ]
    all_ok = True
    for label, ac, expected in cases:
        got = classify.classify(ac)
        ok = got == expected
        all_ok = all_ok and ok
        print(f"  {label}: {'OK' if ok else f'FAIL (got {got}, expected {expected})'}")
    return all_ok


def check_route_widening_regressions() -> bool:
    """Pure-logic checks for the phase-1 route-widening additions
    (MASTERPLAN.md sectie 5) that don't need a live network call: hexdb.io
    route-string parsing, and the adsbdb negative-retry timing logic. Live
    network shape (hexdb.io's route/aircraft endpoints, adsbdb's response
    shape) was smoke-tested manually against the real services while
    writing this — see MASTERPLAN.md sectie 2.2 for the confirmed examples
    — not re-verified here, same convention as providers.route_lookup_
    pending_retry (BACKTEST_LOG.md ronde 5: 'verified with a standalone
    async repro ... not backtested via backtest.py, since this is a
    live-network provider concern')."""
    import time as _time

    import providers

    print("\n=== route-widening (hexdb.io + negative-retry) regression check ===")
    all_ok = True

    # hexdb.io route string "EDLW-LROP" -> ("EDLW", "LROP") parsing,
    # exercised via the same partition logic lookup_route_hexdb uses.
    route_str = "EDLW-LROP"
    origin, _, dest = route_str.partition("-")
    parsed_ok = (origin, dest) == ("EDLW", "LROP")
    all_ok = all_ok and parsed_ok
    print(f"  hexdb.io route-string parsing ('EDLW-LROP' -> EDLW/LROP): {'OK' if parsed_ok else 'FAIL'}")

    cfg = dict(CONFIG)
    cfg["route_lookup_negative_retry_hours"] = 1 / 3600  # 1 second, for a fast test
    callsign = "ROUTE_WIDENING_REGRESSION_TEST"  # already uppercase — route_lookup_negative_retry_due normalizes internally, so the test's direct cache writes must match
    providers._route_cache.pop(callsign, None)
    providers._route_cache_negative_at.pop(callsign, None)

    not_yet_negative = not providers.route_lookup_negative_retry_due(callsign, cfg)
    all_ok = all_ok and not_yet_negative
    print(f"  never-looked-up callsign is not treated as a due negative: {'OK' if not_yet_negative else 'FAIL'}")

    providers._route_cache[callsign] = None
    providers._route_cache_negative_at[callsign] = _time.monotonic()
    fresh_negative_not_due = not providers.route_lookup_negative_retry_due(callsign, cfg)
    all_ok = all_ok and fresh_negative_not_due
    print(f"  fresh negative result not yet due for retry: {'OK' if fresh_negative_not_due else 'FAIL'}")

    providers._route_cache_negative_at[callsign] = _time.monotonic() - 5  # older than the 1s test window
    aged_negative_due = providers.route_lookup_negative_retry_due(callsign, cfg)
    all_ok = all_ok and aged_negative_due
    print(f"  aged-out negative result is due for retry: {'OK' if aged_negative_due else 'FAIL'}")

    providers._route_cache[callsign] = {"origin_icao": "TEST"}
    real_route_never_due = not providers.route_lookup_negative_retry_due(callsign, cfg)
    all_ok = all_ok and real_route_never_due
    print(f"  a real cached route is never treated as a due negative: {'OK' if real_route_never_due else 'FAIL'}")

    providers._route_cache.pop(callsign, None)
    providers._route_cache_negative_at.pop(callsign, None)
    return all_ok


def check_wrong_airport_route_crosscheck() -> bool:
    """Live data (2026-08-07, BACKTEST_LOG.md ronde 18): 24/25 sampled live
    wrong_airport/BEVESTIGD hits had zero corroborating evidence from any
    other detector, and several (e.g. DLH8NK: adsbdb filed EDDF->LEMG,
    actually landed EDDM — an extremely common, routine Lufthansa shuttle
    hop) look like adsbdb schedule-data errors, not real diversions. Fixed
    in main.py's enrich_events: a second, independent route source
    (hexdb.io) is queried specifically for wrong_airport events, downgrading
    confidence to WAARSCHIJNLIJK on disagreement rather than trusting
    adsbdb's filed destination unconditionally. Not testable via
    backtest.py's Case harness (that's detector.py geometry; this is
    main.py's async enrichment layer, a live-network provider concern) —
    standalone async repro with a mocked providers.lookup_route_hexdb
    instead, same convention as round 5's route_lookup_pending_retry
    verification (BACKTEST_LOG.md: 'not backtested via backtest.py, since
    this is a live-network provider concern the synthetic geometry harness
    doesn't model')."""
    import asyncio

    import main as main_module
    import providers
    from detector import Event

    print("\n=== wrong_airport route-source cross-check (mocked hexdb.io) ===")
    cfg = dict(CONFIG)
    # Isolate: only exercise the new hexdb.io check, not the other
    # (already-tested) enrich_events branches.
    cfg["cross_provider_consensus_enabled"] = False
    cfg["weather_enrichment_enabled"] = False
    cfg["fr24_confirm_enabled"] = False
    cfg["route_secondary_source_enabled"] = True

    def mock_hexdb(result):
        async def _mock(session, callsign):
            return result
        return _mock

    original = providers.lookup_route_hexdb
    all_ok = True

    async def run():
        nonlocal all_ok
        # CHECKPOINT.md bevinding 14: dit IS het DLH8NK-geval. adsbdb zei
        # EDDF->LEMG, het toestel landde op EDDM, en hexdb.io noemt EDDM als
        # bestemming. Eén van de twee bronnen noemt dus precies waar het
        # toestel feitelijk naartoe ging -> geen uitwijking, alleen verouderde
        # scheduledata bij de eerste bron.
        providers.lookup_route_hexdb = mock_hexdb(("EDDF", "EDDM"))
        ev1 = Event(hex="t1", callsign="DLH8NK", event_type="wrong_airport", confidence="BEVESTIGD",
                    message="test", origin_icao="EDDF", dest_icao="LEMG", observed_icao="EDDM")
        await main_module.enrich_events(None, cfg, [ev1])
        ok1 = ev1.suppressed
        all_ok = all_ok and ok1
        print(f"  hexdb.io noemt precies de luchthaven waar het toestel geland is -> onderdrukt, "
              f"geen diversie: {'OK' if ok1 else f'FAIL (suppressed={ev1.suppressed})'}")

        # Maar noemt GEEN van beide bronnen de waargenomen luchthaven, dan
        # blijft een uitwijking de aannemelijke verklaring — wel met verlaagd
        # gewicht, want de referentiedata is dan nog steeds betwist.
        providers.lookup_route_hexdb = mock_hexdb(("EDDF", "EDDK"))
        ev1b = Event(hex="t1b", callsign="DLH9XX", event_type="wrong_airport", confidence="BEVESTIGD",
                     message="test", origin_icao="EDDF", dest_icao="LEMG", observed_icao="EDDM")
        await main_module.enrich_events(None, cfg, [ev1b])
        ok1b = (not ev1b.suppressed and ev1b.route_source_disputed
                and ev1b.confidence == "WAARSCHIJNLIJK" and "hexdb.io" in ev1b.message)
        all_ok = all_ok and ok1b
        print(f"  geen van beide bronnen noemt de waargenomen luchthaven -> blijft een uitwijking, "
              f"gedegradeerd: {'OK' if ok1b else f'FAIL (suppressed={ev1b.suppressed}, disputed={ev1b.route_source_disputed})'}")

        # Zelfde regel voor signal_lost_near_airport. Dit is de vorm die ronde
        # 24 live 6 van de 8 keer aantrof (RYR49MG: gefiled LROP->EGGD,
        # signaal verloren bij LIRA, hexdb.io's route EYVI->LIRA).
        providers.lookup_route_hexdb = mock_hexdb(("EYVI", "LIRA"))
        ev1c = Event(hex="t1c", callsign="RYR49MG", event_type="signal_lost_near_airport",
                     confidence="MOGELIJK", message="test", origin_icao="LROP", dest_icao="EGGD",
                     observed_icao="LIRA")
        await main_module.enrich_events(None, cfg, [ev1c])
        ok1c = ev1c.suppressed
        all_ok = all_ok and ok1c
        print(f"  signal_lost: hexdb.io noemt precies de luchthaven waar het signaal verloren ging "
              f"-> onderdrukt: {'OK' if ok1c else f'FAIL (suppressed={ev1c.suppressed})'}")

        providers.lookup_route_hexdb = mock_hexdb(("LROP", "LIRF"))
        ev1d = Event(hex="t1d", callsign="RYR50MG", event_type="signal_lost_near_airport",
                     confidence="MOGELIJK", message="test", origin_icao="LROP", dest_icao="EGGD",
                     observed_icao="LIRA")
        await main_module.enrich_events(None, cfg, [ev1d])
        ok1d = not ev1d.suppressed and ev1d.route_source_disputed
        all_ok = all_ok and ok1d
        print(f"  signal_lost: derde luchthaven bij de tweede bron -> niet onderdrukt, alleen "
              f"gedegradeerd: {'OK' if ok1d else f'FAIL (suppressed={ev1d.suppressed})'}")

        # Regressie op de in ronde 21 teruggedraaide zelfde-metropool-regel:
        # UA2078's echte diversie landde ~15nm van zijn gefilede bestemming.
        # hexdb noemt daar KPHX, gelijk aan adsbdb, dus de onderdrukkingstak
        # wordt niet eens bereikt en de route wordt juist gecorroboreerd.
        providers.lookup_route_hexdb = mock_hexdb(("KIAH", "KPHX"))
        ev1e = Event(hex="t1e", callsign="UAL2078", event_type="signal_lost_near_airport",
                     confidence="MOGELIJK", message="test", origin_icao="KIAH", dest_icao="KPHX",
                     observed_icao="KLUF")
        await main_module.enrich_events(None, cfg, [ev1e])
        ok1e = not ev1e.suppressed and ev1e.route_corroborated
        all_ok = all_ok and ok1e
        print(f"  UA2078 (echte diversie 15nm van de bestemming): beide bronnen noemen KPHX, niet "
              f"KLUF -> niet onderdrukt en route gecorroboreerd: {'OK' if ok1e else f'FAIL (suppressed={ev1e.suppressed}, corroborated={ev1e.route_corroborated})'}")

        providers.lookup_route_hexdb = mock_hexdb(("KIAH", "KPHX"))
        ev2 = Event(hex="t2", callsign="UAL2078", event_type="wrong_airport", confidence="BEVESTIGD",
                    message="test", origin_icao="KIAH", dest_icao="KPHX")
        await main_module.enrich_events(None, cfg, [ev2])
        ok2 = ev2.confidence == "BEVESTIGD"
        all_ok = all_ok and ok2
        print(f"  hexdb.io agrees with adsbdb's filed destination -> stays BEVESTIGD: {'OK' if ok2 else f'FAIL (confidence={ev2.confidence})'}")

        providers.lookup_route_hexdb = mock_hexdb(None)
        ev3 = Event(hex="t3", callsign="TVF3510", event_type="wrong_airport", confidence="BEVESTIGD",
                    message="test", origin_icao="LFPO", dest_icao="LGMK")
        await main_module.enrich_events(None, cfg, [ev3])
        ok3 = ev3.confidence == "BEVESTIGD"
        all_ok = all_ok and ok3
        print(f"  hexdb.io has no data -> unconfirmed, not penalized: {'OK' if ok3 else f'FAIL (confidence={ev3.confidence})'}")

    try:
        asyncio.run(run())
    finally:
        providers.lookup_route_hexdb = original

    return all_ok


def check_wrong_airport_evidence_weight(airport_db: AirportDB) -> bool:
    """CHECKPOINT.md bevinding 1. enrich_events had two vetoes on
    wrong_airport (cross-provider ground-state disagreement, hexdb.io route
    disagreement) that BOTH only set ev.confidence — while
    incidents.score_for_event read ev.confidence nowhere outside its
    `emergency` branch. Both vetoes were therefore completely inert: a
    disputed landing scored the same flat 90.0 (>= the BEVESTIGD threshold
    of 85) as an undisputed one, and dispatched the same 🚨 Telegram. This
    checks the evidence WEIGHT, which is what actually drives the incident
    state — check_wrong_airport_route_crosscheck above only ever checked the
    (cosmetic) confidence label."""
    import classify
    import db as db_module
    import incidents as incidents_module
    from detector import Event

    print("\n=== wrong_airport evidence weight (disputed / unconfirmed) ===")
    all_ok = True

    trusted = Event(hex="wa1", callsign="TST1", event_type="wrong_airport", confidence="BEVESTIGD",
                    message="test", origin_icao="EHAM", dest_icao="EGLL")
    disputed = Event(hex="wa2", callsign="TST2", event_type="wrong_airport", confidence="WAARSCHIJNLIJK",
                     message="test", origin_icao="EHAM", dest_icao="EGLL", route_source_disputed=True)
    unconfirmed = Event(hex="wa3", callsign="TST3", event_type="wrong_airport", confidence="WAARSCHIJNLIJK",
                        message="test", origin_icao="EHAM", dest_icao="EGLL")

    for label, ev, want_delta, want_source in (
        ("onbetwist", trusted, 90.0, "wrong_airport"),
        ("routebron betwist", disputed, 30.0, "wrong_airport_disputed"),
        ("grondstatus onbevestigd", unconfirmed, 25.0, "wrong_airport_unconfirmed"),
    ):
        delta, source, _ = incidents_module.score_for_event(ev, False)
        ok = delta == want_delta and source == want_source
        all_ok = all_ok and ok
        print(f"  wrong_airport, {label}: {delta:.0f} punten via '{source}' "
              f"(verwacht {want_delta:.0f}/'{want_source}'): {'OK' if ok else 'FAIL'}")

    # End-to-end through the incident engine: a lone disputed/unconfirmed
    # wrong_airport must no longer reach BEVESTIGD. (Before this fix both
    # landed on 90 -> CONFIRMED.)
    cfg = dict(CONFIG)
    for label, ev, hex_id in (("betwist", disputed, "wa2"), ("onbevestigd", unconfirmed, "wa3")):
        mgr = incidents_module.IncidentManager(db_module.connect(":memory:"), cfg, airport_db)
        mgr.step(hex_id, "TST", classify.AIRLINER, [ev], {"squawk": "1200"}, 100.0)
        state = mgr._open[hex_id]["state"]
        ok = state != incidents_module.CONFIRMED
        all_ok = all_ok and ok
        print(f"  lone {label} wrong_airport bereikt geen BEVESTIGD (state={state}): {'OK' if ok else 'FAIL'}")

    # A wrong_airport incident must still close as GESLOTEN_GELAND (a
    # confirmed diversion) even when the evidence landed under one of the
    # new source names — regression for _check_landed's source test.
    mgr = incidents_module.IncidentManager(db_module.connect(":memory:"), cfg, airport_db)
    mgr.step("wa4", "TST4", classify.AIRLINER, [disputed], {"squawk": "1200"}, 200.0)
    other_ap = airport_db.get("EDDF")
    t = mgr.step("wa4", "TST4", classify.AIRLINER, [],
                 {"squawk": "1200", "on_ground": True, "lat": other_ap["lat"], "lon": other_ap["lon"]}, 260.0)
    closed_ok = "wa4" not in mgr._open
    all_ok = all_ok and closed_ok
    print(f"  betwiste wrong_airport sluit nog steeds af als GESLOTEN_GELAND: {'OK' if closed_ok else f'FAIL (transitions={t})'}")

    return all_ok


def check_premature_descent_signal_lost_route_crosscheck(airport_db: AirportDB) -> bool:
    """Live data (2026-08-07, BACKTEST_LOG.md ronde 24): pulled a fresh
    /api/events snapshot and hexdb.io-queried 8 of the sampled
    signal_lost_near_airport callsigns directly. 6/8 hexdb.io lookups named
    a DIFFERENT destination than adsbdb's filed one, and in every one of
    those 6, hexdb.io's destination was EXACTLY the "unexpected" airport the
    event fired on (e.g. RYR49MG: adsbdb filed LROP->EGGD, signal lost near
    LIRA; hexdb.io's own route is EYVI->LIRA — hexdb's destination IS the
    airport this event flagged as "not the destination"). Same
    adsbdb-route-mismatch root cause rounds 16-18 already found for
    premature_descent (round 16, noise) and wrong_airport (round 18, fixed).

    Extends round 18's wrong_airport hexdb.io cross-check
    (check_wrong_airport_route_crosscheck above) to these two detectors —
    but NOT identically: both already fire at the lowest confidence tier
    (MOGELIJK) by design, unlike wrong_airport (BEVESTIGD), so there is no
    lower confidence label to downgrade INTO, and incidents.py's
    score_for_event doesn't consult ev.confidence for these two event types
    at all (confirmed by reading it). main.py's enrich_events therefore
    leaves ev.confidence UNCHANGED for these two (asserted explicitly below
    — this is the key structural difference from wrong_airport, not an
    oversight) and instead sets the new Event.route_source_disputed flag,
    which score_for_event uses to give a disputed hit reduced evidence
    weight (~1/3, the same damping ratio already established for
    REPEATABLE_EVENT_TYPES's repeat hits) instead of a hard suppression —
    see BACKTEST_LOG.md ronde 21's reverted same-metro-exclusion attempt for
    signal_lost_near_airport for why an unconditional exclusion is unsafe
    for that specific detector (single last-known-position snapshot, no
    safety net; UA2078's real diversion landed only ~15nm from its filed
    destination).

    Not testable via backtest.py's Case harness for the same reason as
    check_wrong_airport_route_crosscheck: this is main.py's async
    enrichment layer (live-network provider concern), not detector.py
    geometry — standalone async repro with a mocked
    providers.lookup_route_hexdb instead.

    Extended 2026-08-07 with a real live case: KLM81J, filed EHAM->LEBL,
    reported "vroegtijdige daling: nog 261nm te gaan naar LEBL op 15800ft"
    — but hexdb.io's own route for KLM81J is EHAM->LFBD (Bordeaux), and the
    aircraft's actual position was a completely normal approach to LFBD,
    not a diversion at all. Unlike the ronde-24 cases above (which only
    downgrade evidence weight on disagreement), premature_descent can be
    verified further: main.py's enrich_events now reuses the exact
    top-of-descent physics detect_premature_descent itself uses against
    hexdb's ALTERNATE destination, and hard-suppresses (Event.suppressed)
    when that geometry actually fits — not just "hexdb names something
    else". Live counter-evidence for why this isn't extended to
    signal_lost_near_airport, or made an unconditional distrust of adsbdb:
    hexdb.io's own route for UAL1601 (a genuine, unrelated live emergency
    the same day) was 8 YEARS stale (2018 data, KMCO->KEWR) while adsbdb
    had the correct, current route — the opposite pattern. Geometry
    verification, not source preference, is what makes this safe."""
    import asyncio

    import main as main_module
    import providers
    from detector import Event
    from incidents import score_for_event

    print("\n=== premature_descent/signal_lost_near_airport route-source cross-check (mocked hexdb.io) ===")
    cfg = dict(CONFIG)
    # Isolate: only exercise the new hexdb.io check, not the other
    # (already-tested) enrich_events branches.
    cfg["cross_provider_consensus_enabled"] = False
    cfg["weather_enrichment_enabled"] = False
    cfg["fr24_confirm_enabled"] = False
    cfg["route_secondary_source_enabled"] = True

    def mock_hexdb(result):
        async def _mock(session, callsign):
            return result
        return _mock

    original = providers.lookup_route_hexdb
    all_ok = True

    async def run():
        nonlocal all_ok

        # signal_lost_near_airport: hexdb.io disagrees (mirrors the live
        # RYR49MG finding above) -> NOT suppressed/silenced, confidence
        # UNCHANGED (no lower tier to fall to), but flagged disputed and
        # message annotated.
        providers.lookup_route_hexdb = mock_hexdb(("EYVI", "LIRA"))
        ev1 = Event(hex="t1", callsign="RYR49MG", event_type="signal_lost_near_airport", confidence="MOGELIJK",
                    message="test", origin_icao="LROP", dest_icao="EGGD")
        await main_module.enrich_events(None, cfg, [ev1])
        ok1 = ev1.confidence == "MOGELIJK" and ev1.route_source_disputed and "hexdb.io" in ev1.message
        all_ok = all_ok and ok1
        print(f"  signal_lost: hexdb.io disagrees -> disputed flag set, confidence stays MOGELIJK: {'OK' if ok1 else f'FAIL (confidence={ev1.confidence}, disputed={ev1.route_source_disputed})'}")
        delta1, source1, _ = score_for_event(ev1, False)
        ok1b = delta1 == 13.0 and source1 == "signal_lost_disputed"
        all_ok = all_ok and ok1b
        print(f"  signal_lost: disputed hit scores reduced weight (13.0 vs normal 40.0): {'OK' if ok1b else f'FAIL (delta={delta1}, source={source1})'}")

        # signal_lost_near_airport: hexdb.io agrees -> unaffected, full weight.
        providers.lookup_route_hexdb = mock_hexdb(("KIAH", "KPHX"))
        ev2 = Event(hex="t2", callsign="UAL2078", event_type="signal_lost_near_airport", confidence="MOGELIJK",
                    message="test", origin_icao="KIAH", dest_icao="KPHX")
        await main_module.enrich_events(None, cfg, [ev2])
        ok2 = ev2.confidence == "MOGELIJK" and not ev2.route_source_disputed
        all_ok = all_ok and ok2
        print(f"  signal_lost: hexdb.io agrees -> undisputed, unaffected: {'OK' if ok2 else f'FAIL (disputed={ev2.route_source_disputed})'}")
        delta2, source2, _ = score_for_event(ev2, False)
        ok2b = delta2 == 40.0 and source2 == "signal_lost"
        all_ok = all_ok and ok2b
        print(f"  signal_lost: undisputed hit scores full weight (40.0): {'OK' if ok2b else f'FAIL (delta={delta2}, source={source2})'}")

        # signal_lost_near_airport: hexdb.io has no data -> unconfirmed, not penalized.
        providers.lookup_route_hexdb = mock_hexdb(None)
        ev3 = Event(hex="t3", callsign="AAL710", event_type="signal_lost_near_airport", confidence="MOGELIJK",
                    message="test", origin_icao="KALB", dest_icao="KCLT")
        await main_module.enrich_events(None, cfg, [ev3])
        ok3 = ev3.confidence == "MOGELIJK" and not ev3.route_source_disputed
        all_ok = all_ok and ok3
        print(f"  signal_lost: hexdb.io has no data -> not disputed, not penalized: {'OK' if ok3 else f'FAIL (disputed={ev3.route_source_disputed})'}")

        # premature_descent: hexdb.io disagrees -> disputed, confidence
        # unchanged, first-hit AND repeat-hit both damped further.
        providers.lookup_route_hexdb = mock_hexdb(("EHAM", "ENGM"))
        ev4 = Event(hex="t4", callsign="SAS80M", event_type="premature_descent", confidence="MOGELIJK",
                    message="test", origin_icao="ENGM", dest_icao="EHAM")
        await main_module.enrich_events(None, cfg, [ev4])
        ok4 = ev4.confidence == "MOGELIJK" and ev4.route_source_disputed and "hexdb.io" in ev4.message
        all_ok = all_ok and ok4
        print(f"  premature_descent: hexdb.io disagrees -> disputed flag set, confidence stays MOGELIJK: {'OK' if ok4 else f'FAIL (confidence={ev4.confidence}, disputed={ev4.route_source_disputed})'}")
        delta4_first, source4, _ = score_for_event(ev4, False)
        delta4_repeat, _, _ = score_for_event(ev4, True)
        ok4b = delta4_first == 8.0 and delta4_repeat == 3.0 and source4 == "premature_descent_disputed" and delta4_repeat < delta4_first
        all_ok = all_ok and ok4b
        print(f"  premature_descent: disputed first-hit 8.0, repeat further damped to 3.0: {'OK' if ok4b else f'FAIL (first={delta4_first}, repeat={delta4_repeat}, source={source4})'}")

        # premature_descent: hexdb.io agrees -> unaffected, normal weights.
        providers.lookup_route_hexdb = mock_hexdb(("OMDB", "KSFO"))
        ev5 = Event(hex="t5", callsign="UAE225", event_type="premature_descent", confidence="MOGELIJK",
                    message="test", origin_icao="OMDB", dest_icao="KSFO")
        await main_module.enrich_events(None, cfg, [ev5])
        ok5 = ev5.confidence == "MOGELIJK" and not ev5.route_source_disputed
        all_ok = all_ok and ok5
        print(f"  premature_descent: hexdb.io agrees -> undisputed, unaffected: {'OK' if ok5 else f'FAIL (disputed={ev5.route_source_disputed})'}")
        delta5_first, source5, _ = score_for_event(ev5, False)
        ok5b = delta5_first == 25.0 and source5 == "premature_descent"
        all_ok = all_ok and ok5b
        print(f"  premature_descent: undisputed first-hit scores normal weight (25.0): {'OK' if ok5b else f'FAIL (delta={delta5_first}, source={source5})'}")

        # premature_descent: hexdb.io has no data -> unconfirmed, not penalized.
        providers.lookup_route_hexdb = mock_hexdb(None)
        ev6 = Event(hex="t6", callsign="EDV5329", event_type="premature_descent", confidence="MOGELIJK",
                    message="test", origin_icao="KSTL", dest_icao="KLGA")
        await main_module.enrich_events(None, cfg, [ev6])
        ok6 = ev6.confidence == "MOGELIJK" and not ev6.route_source_disputed
        all_ok = all_ok and ok6
        print(f"  premature_descent: hexdb.io has no data -> not disputed, not penalized: {'OK' if ok6 else f'FAIL (disputed={ev6.route_source_disputed})'}")

        # premature_descent: hexdb.io disagrees AND the observed position is
        # a geometrically normal approach to hexdb's alternate destination
        # (real KLM81J shape: filed EHAM->LEBL, hexdb says EHAM->LFBD,
        # aircraft actually right on top of LFBD) -> hard-suppressed as a
        # false positive, not just disputed. Position pinned exactly at
        # LFBD's own coordinates so the distance-to-alt-dest is ~0nm,
        # trivially inside any positive top-of-descent window, without
        # needing to hand-compute haversine distances for this test.
        providers.lookup_route_hexdb = mock_hexdb(("EHAM", "LFBD"))
        lfbd = airport_db.get("LFBD")
        ev7 = Event(hex="t7", callsign="KLM81J", event_type="premature_descent", confidence="MOGELIJK",
                    message="test", origin_icao="EHAM", dest_icao="LEBL",
                    lat=lfbd["lat"], lon=lfbd["lon"], alt=15800.0)
        await main_module.enrich_events(None, cfg, [ev7], airport_db)
        ok7 = ev7.suppressed and not ev7.route_source_disputed
        all_ok = all_ok and ok7
        print(f"  premature_descent: hexdb.io disagrees AND geometry confirms its destination (live KLM81J) -> suppressed: {'OK' if ok7 else f'FAIL (suppressed={ev7.suppressed}, disputed={ev7.route_source_disputed})'}")

        # premature_descent: hexdb.io disagrees, but the aircraft's position
        # ALSO doesn't fit hexdb's alternate destination (still right at
        # EHAM, hexdb's LFBD is ~480nm away — well beyond the ~315nm
        # top-of-descent window for 35,000ft) -> falls back to the ronde-24
        # disputed-weight path, NOT suppressed. Proves the geometry check
        # actually verifies the alternate destination rather than
        # suppressing on disagreement alone.
        providers.lookup_route_hexdb = mock_hexdb(("EHAM", "LFBD"))
        eham = airport_db.get("EHAM")
        ev8 = Event(hex="t8", callsign="KLM82J", event_type="premature_descent", confidence="MOGELIJK",
                    message="test", origin_icao="EHAM", dest_icao="LEBL",
                    lat=eham["lat"], lon=eham["lon"], alt=35000.0)
        await main_module.enrich_events(None, cfg, [ev8], airport_db)
        ok8 = ev8.route_source_disputed and not ev8.suppressed
        all_ok = all_ok and ok8
        print(f"  premature_descent: hexdb.io disagrees but geometry does NOT fit its destination -> stays disputed, not suppressed: {'OK' if ok8 else f'FAIL (suppressed={ev8.suppressed}, disputed={ev8.route_source_disputed})'}")

    try:
        asyncio.run(run())
    finally:
        providers.lookup_route_hexdb = original

    return all_ok


def check_saturation_caps(airport_db: AirportDB) -> bool:
    """CHECKPOINT.md bevinding 3 en 8. The repeat dampening added in round 16
    was a fixed fraction (~1/3 of the first-hit weight), so the series still
    diverged — the only brake was incident_score_max (150), well ABOVE the
    BEVESTIGD threshold (85). A single signal that simply kept firing
    therefore reached the top level on its own: holding_non_destination in 6
    tier1 cycles (~6 minutes), premature_descent in 9, emergency_status in 3,
    and even emergency_status_low_trust — the conspicuity-squawk decode
    artifact the project itself documented as unreliable — in 11.

    The underlying reasoning that does hold: repetition eliminates NOISE
    hypotheses (a single bad fix, a decode glitch, one ATC vector). It does
    not eliminate SYSTEMATIC benign ones (stale route data, a weather
    reroute, an ATC delay, normal arrival sequencing) — those actively
    predict that the signal persists. So evidence weight from one source has
    to converge, not diverge, and converge to something under the level that
    is supposed to mean 'no reasonable alternative remains'."""
    import classify
    import db as db_module
    import incidents as incidents_module
    from detector import Event

    print("\n=== verzadigingsplafonds per bewijsbron ===")
    all_ok = True
    cfg = dict(CONFIG)
    confirmed = cfg["incident_score_confirmed_threshold"]
    likely = cfg["incident_score_likely_threshold"]
    possible = cfg["incident_score_possible_threshold"]

    def sustain(hex_id, ev_factory, cycles=30):
        """Feed the same detector Event `cycles` times in a row (one per
        tier1 cycle) and return (score, state) — the 'signal simply keeps
        happening' scenario."""
        mgr = incidents_module.IncidentManager(db_module.connect(":memory:"), cfg, airport_db)
        t = 1000.0
        for i in range(cycles):
            mgr.step(hex_id, "TSTC", classify.AIRLINER, [ev_factory(i)], {"squawk": "1200"}, t)
            t += 60.0
        inc = mgr._open[hex_id]
        return inc["score"], inc["state"]

    def hold_nd(i):
        return Event(hex="cap1", callsign="TSTC", event_type="holding_pattern", confidence="MOGELIJK",
                     message=f"m{i}", origin_icao="EHAM", dest_icao="EGLL", at_destination=False,
                     route_corroborated=True)

    def descent(i):
        return Event(hex="cap2", callsign="TSTC", event_type="premature_descent", confidence="MOGELIJK",
                     message=f"m{i}", origin_icao="EHAM", dest_icao="EGLL", route_corroborated=True)

    def low_trust_emergency(i):
        # What enrich_events produces for a conspicuity-squawk emergency
        # status: confidence capped to WAARSCHIJNLIJK, squawk 1000.
        return Event(hex="cap3", callsign="TSTC", event_type="emergency", confidence="WAARSCHIJNLIJK",
                     message=f"m{i}", squawk="1000")

    def status_emergency(i):
        return Event(hex="cap4", callsign="TSTC", event_type="emergency", confidence="BEVESTIGD",
                     message=f"m{i}", squawk="2451")

    def real_squawk(i):
        return Event(hex="cap5", callsign="TSTC", event_type="emergency", confidence="BEVESTIGD",
                     message=f"m{i}", squawk="7700")

    def nordo_squawk(i):
        # CHECKPOINT.md bevinding 15: 7600 blijft zichtbaar (>= MOGELIJK) maar
        # mag ook volgehouden nooit op eigen kracht notificeren.
        return Event(hex="cap6", callsign="TSTC", event_type="emergency", confidence="BEVESTIGD",
                     message=f"m{i}", squawk="7600")

    for label, hex_id, factory, want_cap, want_state in (
        ("holding_non_destination", "cap1", hold_nd, 70.0, incidents_module.LIKELY),
        ("premature_descent", "cap2", descent, 60.0, incidents_module.LIKELY),
        ("emergency_status_low_trust", "cap3", low_trust_emergency, 24.0, incidents_module.WATCHING),
        # WAS 55.0/LIKELY — exact de WAARSCHIJNLIJK-drempel, dus zo gekozen dat
        # het losse ADS-B-statusveld in zijn eentje een Telegram stuurde op
        # cross-provider-corroboratie waarvan main.py's eigen commentaar al
        # vaststelt dat zij niet onafhankelijk is. Zie CHECKPOINT.md bevinding 15.
        ("emergency_status", "cap4", status_emergency, 24.0, incidents_module.WATCHING),
        ("nordo_squawk", "cap6", nordo_squawk, 33.0, incidents_module.POSSIBLE),
    ):
        score, state = sustain(hex_id, factory)
        ok = abs(score - want_cap) < 0.01 and state == want_state and score < confirmed
        all_ok = all_ok and ok
        print(f"  30x achtereen {label}: score {score:.0f} (plafond {want_cap:.0f}), state {state} "
              f"— blijft onder BEVESTIGD ({confirmed}): {'OK' if ok else 'FAIL'}")

    # The low-trust tier is deliberately capped just UNDER the dashboard
    # visibility threshold: a documented decode artifact should not surface
    # on its own at all, however long it persists.
    score3, _ = sustain("cap3", low_trust_emergency)
    ok = score3 < possible
    all_ok = all_ok and ok
    print(f"  het gedocumenteerde conspicuity-decodeartefact komt zelfs na 30 cycli niet op het "
          f"dashboard (score {score3:.0f} < MOGELIJK-drempel {possible}): {'OK' if ok else 'FAIL'}")

    # Regression guard in the other direction: the caps must not blunt a real
    # declared emergency.
    score5, state5 = sustain("cap5", real_squawk, cycles=3)
    ok = state5 == incidents_module.CONFIRMED
    all_ok = all_ok and ok
    print(f"  een echte noodsquawk raakt dit niet: nog steeds direct BEVESTIGD "
          f"(score {score5:.0f}, state {state5}): {'OK' if ok else 'FAIL'}")

    # Every PROVISIONAL cap must sit under the CONFIRMED threshold — a
    # structural invariant, so a future cap can't be added above it by
    # accident. Exempt are exactly the sources _confirmed_bar_met can confirm
    # on by themselves: the route-independent ones (emergency squawk,
    # self-observed early return) and wrong_airport (physically observed
    # landing, on a corroborated route). Derived from those sets rather than
    # hardcoded, so the invariant and the gate can never drift apart.
    may_confirm_alone = incidents_module._ROUTE_INDEPENDENT_SOURCES | {"wrong_airport"}
    over = {s: c for s, c in incidents_module._SOURCE_SCORE_CAP.items()
            if c >= confirmed and s not in may_confirm_alone}
    ok = not over
    all_ok = all_ok and ok
    print(f"  alle provisionele plafonds liggen onder de BEVESTIGD-drempel "
          f"(vrijgesteld: {sorted(may_confirm_alone)}): {'OK' if ok else f'FAIL ({over})'}")

    # The per-source contribution decays along with the score, so a source
    # that saturated and then decayed can contribute again while the
    # underlying signal is still going — the cap bounds the score
    # attributable to a source at any MOMENT, not the total it ever earned.
    mgr = incidents_module.IncidentManager(db_module.connect(":memory:"), cfg, airport_db)
    t = 1000.0
    for i in range(30):
        mgr.step("cap6", "TSTC", classify.AIRLINER, [
            Event(hex="cap6", callsign="TSTC", event_type="holding_pattern", confidence="MOGELIJK",
                  message=f"m{i}", origin_icao="EHAM", dest_icao="EGLL", at_destination=False)], {"squawk": "1200"}, t)
        t += 60.0
    saturated = mgr._open["cap6"]["score"]
    for _ in range(4):  # aircraft briefly missing from the snapshot -> decay
        t += 60.0
        mgr.step("cap6", "TSTC", classify.AIRLINER, [], {"squawk": "1200"}, t)
    decayed = mgr._open["cap6"]["score"]
    t += 60.0
    mgr.step("cap6", "TSTC", classify.AIRLINER, [
        Event(hex="cap6", callsign="TSTC", event_type="holding_pattern", confidence="MOGELIJK",
              message="back", origin_icao="EHAM", dest_icao="EGLL", at_destination=False)], {"squawk": "1200"}, t)
    resumed = mgr._open["cap6"]["score"]
    ok = decayed < saturated and resumed > decayed and resumed <= saturated + 0.01
    all_ok = all_ok and ok
    print(f"  een verzadigde bron kan na verval weer bijdragen, tot hetzelfde plafond "
          f"(verzadigd {saturated:.0f} -> vervallen {decayed:.0f} -> hervat {resumed:.0f}): {'OK' if ok else 'FAIL'}")

    return all_ok


def check_route_corroboration(airport_db: AirportDB) -> bool:
    """CHECKPOINT.md bevinding 2. Second-source checks only ever downgraded on
    ACTIVE disagreement, so 'hexdb.io has no data' counted exactly the same as
    'hexdb.io confirms it'. For the lower levels that's right (don't lose a
    real diversion because a free source is thin). For BEVESTIGD it is exactly
    backwards: 'the filed destination is wrong' is not a theoretical worry
    here but the MEASURED dominant failure mode (24/25 uncorroborated live
    wrong_airport hits, ronde 18; 6/8 hexdb lookups naming a different
    destination, ronde 24). Treating unverified as verified there makes the
    top label rest on the single assumption most likely to be false.

    route_corroborated_by_progress is the positive counterpart of
    route_plausible: not 'not demonstrably wrong' but 'demonstrably flown'."""
    import classify
    import db as db_module
    import incidents as incidents_module
    from airports import route_corroborated_by_progress
    from detector import Event

    print("\n=== route-corroboratie (bevestigd vs. slechts onweersproken) ===")
    all_ok = True

    # Real geometry, EK225's own case: DXB->SFO (7030nm).
    #
    # Deze assertie is bij bevinding 16 OMGEDRAAID, en dat verdient uitleg —
    # het is geen test die is bijgesteld om te laten slagen.
    #
    # Zij testte één los punt: de positie boven Nyagan (62.1N, 65.4E) waar
    # EK225 daadwerkelijk terugkeerde, en eiste dat DAT punt de route
    # corroboreert. Nagerekend ligt dat punt echter 353nm van de directe
    # OMDB->KSFO-lijn: 5.0% van de routelengte, en meer dan de 250nm die
    # corridor_deviation zelf als "afwijking" telt. Eén waarneming 353nm naast
    # de gefilede lijn op 32% voortgang is nu juist NIET onderscheidend — zij is
    # even goed verenigbaar met "dit toestel vliegt ergens anders heen", wat de
    # gemeten hoofdfoutbron van dit systeem is (bevinding 16: 21 van 28
    # doorgerekende combinaties).
    #
    # Wat EK225's route wél corroboreert is de rest van zijn spoor: de cruise
    # over Siberië ligt op 2.1nm van de gefilede grootcirkel. Dat is de
    # waarneming die een verkeerd gematchte schedule niet kan produceren, en
    # daarop corroboreert de case eind-tot-eind nog steeds — zie
    # check_real_case_confidence_outcomes, waar EK225 onveranderd BEVESTIGD
    # haalt. Beide punten staan hieronder, zodat het verschil expliciet bewaakt
    # blijft in plaats van impliciet te verdwijnen.
    dxb, sfo = airport_db.get("OMDB"), airport_db.get("KSFO")
    # Een ECHT sample uit EK225's eigen spoor (backtest_cases.py), niet een
    # verzonnen coördinaat: 2.05nm van de gefilede lijn op 32% voortgang.
    # 39 van de 661 samples van deze case halen deze toets — ruim genoeg om de
    # route eenmalig als gecorroboreerd vast te leggen.
    on_route = route_corroborated_by_progress(dxb["lat"], dxb["lon"], sfo["lat"], sfo["lon"], 62.9851, 52.7412)
    all_ok = all_ok and on_route
    print(f"  EK225 op de gefilede OMDB->KSFO-corridor (2.1nm ernaast, 32% voortgang): "
          f"gecorroboreerd: {'OK' if on_route else 'FAIL'}")

    nyagan = route_corroborated_by_progress(dxb["lat"], dxb["lon"], sfo["lat"], sfo["lon"], 62.1, 65.4)
    all_ok = all_ok and not nyagan
    print(f"  ...maar het losse keerpunt boven Nyagan (353nm = 5.0% naast de lijn) corroboreert "
          f"in zijn eentje NIET: {'OK' if not nyagan else 'FAIL'}")

    # Same function, the schedule-mismatch shape that drives the live false
    # positives (DLH8NK: adsbdb filed EDDF->LEMG, aircraft actually near
    # EDDM). The distance to the filed destination never goes down, so this
    # can never corroborate — which is the whole point.
    eddf, lemg, eddm = airport_db.get("EDDF"), airport_db.get("LEMG"), airport_db.get("EDDM")
    ok = not route_corroborated_by_progress(eddf["lat"], eddf["lon"], lemg["lat"], lemg["lon"],
                                            eddm["lat"], eddm["lon"])
    all_ok = all_ok and ok
    print(f"  DLH8NK-vorm (gefiled EDDF->LEMG, werkelijk bij EDDM): NIET gecorroboreerd: {'OK' if ok else 'FAIL'}")

    # End-to-end: the same wrong_airport evidence, with and without route
    # corroboration. Before this finding both reached BEVESTIGD.
    cfg = dict(CONFIG)
    for label, corroborated, want in (("zonder corroboratie", False, incidents_module.LIKELY),
                                      ("met corroboratie", True, incidents_module.CONFIRMED)):
        mgr = incidents_module.IncidentManager(db_module.connect(":memory:"), cfg, airport_db)
        ev = Event(hex="rc1", callsign="TSTR", event_type="wrong_airport", confidence="BEVESTIGD",
                   message="test", origin_icao="EHAM", dest_icao="EGLL", route_corroborated=corroborated)
        mgr.step("rc1", "TSTR", classify.AIRLINER, [ev], {"squawk": "1200"}, 100.0)
        inc = mgr._open["rc1"]
        ok = inc["state"] == want
        all_ok = all_ok and ok
        print(f"  wrong_airport {label}: score {inc['score']:.0f}, state {inc['state']} "
              f"(verwacht {want}): {'OK' if ok else 'FAIL'}")

    # The corroboration is persisted as a zero-delta evidence row, so it
    # survives a restart and shows up in the incident's own timeline rather
    # than living only in memory.
    mgr = incidents_module.IncidentManager(db_module.connect(":memory:"), cfg, airport_db)
    ev = Event(hex="rc2", callsign="TSTR", event_type="corridor_deviation", confidence="MOGELIJK",
               message="test", origin_icao="EHAM", dest_icao="EGLL", route_corroborated=True)
    mgr.step("rc2", "TSTR", classify.AIRLINER, [ev], {"squawk": "1200"}, 100.0)
    rows = db_module.get_incident_evidence(mgr.db, mgr._open["rc2"]["id"])
    marker = [r for r in rows if r["source"] == "route_corroborated"]
    ok = len(marker) == 1 and marker[0]["delta"] == 0.0
    all_ok = all_ok and ok
    print(f"  route-corroboratie staat als delta-0 evidence-rij in de tijdlijn (niet alleen in geheugen): "
          f"{'OK' if ok else f'FAIL ({marker})'}")
    # CHECKPOINT.md bevinding 9: watching an aircraft DEPART the filed origin
    # confirms the wrong half of the route. It rules out the wrong-LEG error
    # (callsign flies A->B and later B->A while adsbdb only knows A->B), but
    # not the wrong-DESTINATION error — and wrong_airport is entirely about
    # the destination. The live case that motivated this whole round is
    # exactly that shape: DLH8NK filed EDDF->LEMG, actually flew EDDF->EDDM.
    # The origin matches, so an origin-based corroboration would have waved
    # it straight through to BEVESTIGD again.
    mgr = incidents_module.IncidentManager(db_module.connect(":memory:"), cfg, airport_db)
    ev = Event(hex="rc3", callsign="DLH8NK", event_type="wrong_airport", confidence="BEVESTIGD",
               message="geland op EDDM i.p.v. LEMG", origin_icao="EDDF", dest_icao="LEMG")
    mgr.step("rc3", "DLH8NK", classify.AIRLINER, [ev], {"squawk": "1200"}, 100.0)
    state = mgr._open["rc3"]["state"]
    ok = state != incidents_module.CONFIRMED
    all_ok = all_ok and ok
    print(f"  DLH8NK-scenario: kloppende origin maakt de BESTEMMING niet gecorroboreerd "
          f"(state={state}): {'OK' if ok else 'FAIL'}")

    # The early-return observation, by contrast, genuinely does not depend on
    # the filed destination: we watched this aircraft take off from airport X
    # and land back at that same airport X within the early-return window.
    # That is abnormal whatever the schedule said, so it confirms on its own.
    mgr = incidents_module.IncidentManager(db_module.connect(":memory:"), cfg, airport_db)
    ev = Event(hex="rc4", callsign="TVF3510", event_type="wrong_airport", confidence="BEVESTIGD",
               message="teruggekeerd naar LFPO", origin_icao="LFPO", dest_icao="LGMK",
               early_return=True)
    mgr.step("rc4", "TVF3510", classify.AIRLINER, [ev], {"squawk": "1200"}, 100.0)
    state = mgr._open["rc4"]["state"]
    ok = state == incidents_module.CONFIRMED
    all_ok = all_ok and ok
    print(f"  vroege terugkeer naar het zelf waargenomen vertrekveld bevestigt zonder "
          f"route-corroboratie (state={state}): {'OK' if ok else 'FAIL'}")

    # ...and only once, however often it is re-asserted.
    for i in range(3):
        mgr.step("rc2", "TSTR", classify.AIRLINER, [
            Event(hex="rc2", callsign="TSTR", event_type="corridor_deviation", confidence="MOGELIJK",
                  message=f"again{i}", origin_icao="EHAM", dest_icao="EGLL", route_corroborated=True)],
            {"squawk": "1200"}, 200.0 + i * 60)
    rows = db_module.get_incident_evidence(mgr.db, mgr._open["rc2"]["id"])
    ok = sum(1 for r in rows if r["source"] == "route_corroborated") == 1
    all_ok = all_ok and ok
    print(f"  ...en precies één keer, hoe vaak hij ook opnieuw wordt vastgesteld: {'OK' if ok else 'FAIL'}")

    return all_ok


def check_confirmed_bar_structure(airport_db: AirportDB) -> bool:
    """CHECKPOINT.md bevinding 4 (en 7). The old gate tested only whether the
    SET of source names happened to fall entirely inside {course_deviation,
    corridor_deviation}. But the problem was never those two names — it is
    that every detector except detect_emergency measures against
    track.route["destination_*"], so ONE wrong filed destination makes several
    of them fire at once and the sum reads it as independent corroboration.

    Adding evidence is only justified when the pieces are conditionally
    independent. Here a single alternative hypothesis ('the filed route is
    wrong') predicts all of them simultaneously, so more different detectors
    do not make it less likely — they are all predictions OF it."""
    import classify
    import db as db_module
    import incidents as incidents_module
    from detector import Event

    print("\n=== structuur van de BEVESTIGD-poort ===")
    all_ok = True
    cfg = dict(CONFIG)

    def build(hex_id, specs, corroborated):
        mgr = incidents_module.IncidentManager(db_module.connect(":memory:"), cfg, airport_db)
        t = 1000.0
        for et, kwargs in specs:
            mgr.step(hex_id, "TSTS", classify.AIRLINER, [
                Event(hex=hex_id, callsign="TSTS", event_type=et, confidence=kwargs.pop("confidence", "MOGELIJK"),
                      message="m", origin_icao="EHAM", dest_icao="EGLL",
                      route_corroborated=corroborated, **kwargs)], {"squawk": kwargs.get("squawk") or "1200"}, t)
            t += 60.0
        return mgr._open.get(hex_id) or {"state": "GESLOTEN", "score": 0.0}

    # The literal scenario from the finding: one stale schedule, three
    # detectors. Each is a prediction of the same single error.
    stale_schedule = [("corridor_deviation", {}), ("premature_descent", {}),
                      ("holding_pattern", {"at_destination": False})]
    inc = build("cb1", [(et, dict(kw)) for et, kw in stale_schedule], corroborated=False)
    ok = inc["state"] != incidents_module.CONFIRMED
    all_ok = all_ok and ok
    print(f"  drie route-afhankelijke detectoren, route NIET gecorroboreerd (het verouderde-schedule-"
          f"scenario): score {inc['score']:.0f}, state {inc['state']} — geen BEVESTIGD: {'OK' if ok else 'FAIL'}")

    # Same evidence, route corroborated: now the one hypothesis that
    # explained all three is off the table, and three genuinely different
    # dimensions remain.
    inc = build("cb2", [(et, dict(kw)) for et, kw in stale_schedule], corroborated=True)
    ok = inc["state"] == incidents_module.CONFIRMED
    all_ok = all_ok and ok
    print(f"  hetzelfde bewijs mét gecorroboreerde route: score {inc['score']:.0f}, state {inc['state']} "
          f"— wél BEVESTIGD (de poort staat niet gewoon dicht): {'OK' if ok else 'FAIL'}")

    # course_deviation + corridor_deviation are two measurements of the same
    # lateral deviation — one dimension, not two sources' worth of
    # corroboration. This replaces the old _DEVIATION_ONLY_SOURCES assertion
    # with the reason behind it.
    inc = build("cb3", [("course_deviation", {}), ("corridor_deviation", {}),
                        ("course_deviation", {}), ("corridor_deviation", {}),
                        ("course_deviation", {}), ("corridor_deviation", {})], corroborated=True)
    ok = inc["state"] != incidents_module.CONFIRMED
    all_ok = all_ok and ok
    print(f"  course_deviation + corridor_deviation zijn dezelfde dimensie (lateraal), niet twee "
          f"onafhankelijke bronnen: score {inc['score']:.0f}, state {inc['state']}: {'OK' if ok else 'FAIL'}")

    # A real emergency squawk needs nothing else at all — not even a route.
    mgr = incidents_module.IncidentManager(db_module.connect(":memory:"), cfg, airport_db)
    mgr.step("cb4", "TSTS", classify.AIRLINER, [
        Event(hex="cb4", callsign="TSTS", event_type="emergency", confidence="BEVESTIGD",
              message="squawk 7700", squawk="7700")], {"squawk": "7700"}, 1000.0)
    ok = mgr._open["cb4"]["state"] == incidents_module.CONFIRMED
    all_ok = all_ok and ok
    print(f"  noodsquawk alleen, zonder route en zonder corroboratie -> BEVESTIGD: {'OK' if ok else 'FAIL'}")

    # ...but the ADS-B emergency STATUS field must not open the gate on its
    # own, even combined with a deviation and a corroborated route: that
    # field is documented-unreliable, and DIM_DECLARED deliberately does not
    # count toward the 'two different kinds of abnormality' test.
    inc = build("cb5", [("emergency", {"confidence": "BEVESTIGD", "squawk": "2451"})] * 3
                       + [("corridor_deviation", {})] * 3, corroborated=True)
    ok = inc["state"] != incidents_module.CONFIRMED
    all_ok = all_ok and ok
    print(f"  emergency-STATUSveld + laterale afwijking + gecorroboreerde route -> nog steeds geen "
          f"BEVESTIGD (score {inc['score']:.0f}, state {inc['state']}): {'OK' if ok else 'FAIL'}")

    return all_ok


def check_incident_engine_regressions(airport_db: AirportDB) -> bool:
    """Standalone smoke test for incidents.py (MASTERPLAN.md sectie 3),
    wired into main.py's tier0_loop/tier1_loop as of BACKTEST_LOG.md ronde
    10. Uses a throwaway in-memory sqlite db (db.connect(':memory:')) so it
    never touches the real database. Exercises: first-hit vs repeat-hit
    scoring, threshold crossings (BEWAKING invisible -> MOGELIJK ->
    BEVESTIGD), notification gating (no duplicate escalation notices),
    deviation-recovery, landing-based resolution, and score decay ->
    auto-close as a false alarm."""
    import classify
    import db as db_module
    import incidents as incidents_module
    from detector import Event

    print("\n=== incident engine regression check ===")
    all_ok = True

    conn = db_module.connect(":memory:")
    cfg = dict(CONFIG)
    mgr = incidents_module.IncidentManager(conn, cfg, airport_db)

    # 1. First-hit corridor_deviation (weight 30) opens straight at
    # MOGELIJK (>= threshold 25) without a notification — a detector Event
    # has already survived its own internal filtering, so its first hit
    # should be visible immediately, not buried in BEWAKING; MOGELIJK never
    # notifies (MASTERPLAN.md sectie 3.6).
    ev1 = Event(hex="inc_test_1", callsign="TST100", event_type="corridor_deviation",
                confidence="MOGELIJK", message="test", origin_icao="EHAM", dest_icao="EGLL")
    t1 = mgr.step("inc_test_1", "TST100", classify.AIRLINER, [ev1], {"squawk": "1200"}, 1000.0)
    state1 = mgr._open["inc_test_1"]["state"]
    ok = state1 == incidents_module.POSSIBLE and not t1
    all_ok = all_ok and ok
    print(f"  first corridor_deviation hit opens at MOGELIJK, no notification: {'OK' if ok else f'FAIL (state={state1}, transitions={t1})'}")

    # 2. A repeat corridor_deviation hit adds a smaller top-up (+10, not
    # +30) — expected score ~40, still under the LIKELY threshold (55).
    ev2 = Event(hex="inc_test_1", callsign="TST100", event_type="corridor_deviation",
                confidence="MOGELIJK", message="test2", origin_icao="EHAM", dest_icao="EGLL")
    mgr.step("inc_test_1", "TST100", classify.AIRLINER, [ev2], {"squawk": "1200"}, 1060.0)
    score2 = mgr._open["inc_test_1"]["score"]
    ok = 35.0 <= score2 <= 45.0
    all_ok = all_ok and ok
    print(f"  repeat corridor_deviation hit adds a smaller top-up (score={score2:.0f}, expected ~40): {'OK' if ok else 'FAIL'}")

    # 3. An emergency squawk escalates straight to BEVESTIGD with exactly
    # one notification.
    ev3 = Event(hex="inc_test_1", callsign="TST100", event_type="emergency",
                confidence="BEVESTIGD", message="squawk 7700", squawk="7700")
    t3 = mgr.step("inc_test_1", "TST100", classify.AIRLINER, [ev3], {"squawk": "7700"}, 1120.0)
    state3 = mgr._open["inc_test_1"]["state"]
    ok = (state3 == incidents_module.CONFIRMED and len(t3) == 1
          and t3[0]["kind"] == "escalation" and t3[0]["new_state"] == incidents_module.CONFIRMED)
    all_ok = all_ok and ok
    print(f"  emergency squawk escalates straight to BEVESTIGD with one notification: {'OK' if ok else f'FAIL (state={state3}, transitions={t3})'}")

    # 4. Re-stepping with no new events and an unchanged state doesn't
    # re-notify (notified_state gate).
    t4 = mgr.step("inc_test_1", "TST100", classify.AIRLINER, [], {"squawk": "7700"}, 1180.0)
    ok = len(t4) == 0
    all_ok = all_ok and ok
    print(f"  no duplicate notification on unchanged state: {'OK' if ok else f'FAIL (transitions={t4})'}")

    # 5. Deviation recovery: a fresh incident with ONLY a course_deviation
    # (no emergency/wrong_airport reinforcing it) loses score when heading
    # points back at the destination — the mechanism behind "if it turns
    # out to be a false alert, it may disappear from the list again".
    dest_ap = airport_db.get("EGLL")
    ev5 = Event(hex="inc_test_2", callsign="TST200", event_type="course_deviation",
                confidence="MOGELIJK", message="test", origin_icao="EHAM", dest_icao="EGLL")
    mgr.step("inc_test_2", "TST200", classify.AIRLINER, [ev5], {"squawk": "1200"}, 2000.0)
    score_before = mgr._open["inc_test_2"]["score"]
    # Position due south of EGLL, heading due north -> bearing to EGLL ~0deg, matches heading.
    recovery_ac = {"squawk": "1200", "lat": dest_ap["lat"] - 2.0, "lon": dest_ap["lon"], "track": 0.0}
    mgr.step("inc_test_2", "TST200", classify.AIRLINER, [], recovery_ac, 2060.0)
    still_open = "inc_test_2" in mgr._open
    score_after = mgr._open["inc_test_2"]["score"] if still_open else 0.0
    ok = (not still_open) or score_after < score_before
    all_ok = all_ok and ok
    print(f"  deviation-only incident recovers when heading points back at destination "
          f"(before={score_before:.0f}, after={score_after:.0f}): {'OK' if ok else 'FAIL'}")

    # 6. Landing at the expected destination closes the incident as
    # GESLOTEN_NORMAAL.
    ev6 = Event(hex="inc_test_3", callsign="TST300", event_type="premature_descent",
                confidence="MOGELIJK", message="test", origin_icao="EHAM", dest_icao="EGLL")
    mgr.step("inc_test_3", "TST300", classify.AIRLINER, [ev6], {"squawk": "1200"}, 3000.0)
    landed_ac = {"squawk": "1200", "on_ground": True, "lat": dest_ap["lat"], "lon": dest_ap["lon"]}
    mgr.step("inc_test_3", "TST300", classify.AIRLINER, [], landed_ac, 3060.0)
    ok = "inc_test_3" not in mgr._open
    all_ok = all_ok and ok
    print(f"  landing at the expected destination closes the incident: {'OK' if ok else 'FAIL (still open)'}")

    # 7. Idle decay -> auto-close as a false alarm past the idle floor
    # (tiny floor/decay values so the test runs in a handful of iterations).
    fast_cfg = dict(cfg)
    fast_cfg["incident_score_decay_floor_minutes"] = 1
    fast_cfg["incident_score_decay_factor_per_cycle"] = 0.5
    mgr2 = incidents_module.IncidentManager(db_module.connect(":memory:"), fast_cfg, airport_db)
    ev7 = Event(hex="inc_test_4", callsign="TST400", event_type="holding_pattern",
                confidence="MOGELIJK", message="test", origin_icao="EHAM", dest_icao="EGLL", at_destination=False)
    mgr2.step("inc_test_4", "TST400", classify.GA_PRIVATE, [ev7], {"squawk": "1200"}, 4000.0)
    now_t = 4000.0
    closed = False
    for _ in range(10):
        now_t += 300.0
        mgr2.step("inc_test_4", "TST400", classify.GA_PRIVATE, [], {"squawk": "1200"}, now_t)
        if "inc_test_4" not in mgr2._open:
            closed = True
            break
    all_ok = all_ok and closed
    print(f"  idle incident decays and auto-closes as a false alarm: {'OK' if closed else 'FAIL (never closed)'}")

    # 8. wrong_airport evidence + landing somewhere other than the filed
    # destination closes as GESLOTEN_GELAND (a confirmed diversion), not
    # left open to eventually mislabel itself GESLOTEN_VALS_ALARM once it
    # goes quiet. Found via self-review (BACKTEST_LOG.md ronde 15):
    # CLOSED_LANDED was a defined state no code path ever actually reached.
    other_ap = airport_db.get("EDDF")
    ev8 = Event(hex="inc_test_5", callsign="TST500", event_type="wrong_airport",
                confidence="BEVESTIGD", message="test", origin_icao="EHAM", dest_icao="EGLL")
    mgr.step("inc_test_5", "TST500", classify.AIRLINER, [ev8], {"squawk": "1200"}, 5000.0)
    landed_wrong_ac = {"squawk": "1200", "on_ground": True, "lat": other_ap["lat"], "lon": other_ap["lon"]}
    t8 = mgr.step("inc_test_5", "TST500", classify.AIRLINER, [], landed_wrong_ac, 5060.0)
    ok = ("inc_test_5" not in mgr._open and len(t8) == 1
          and t8[0]["new_state"] == incidents_module.CLOSED_LANDED)
    all_ok = all_ok and ok
    print(f"  wrong_airport + landing elsewhere closes as GESLOTEN_GELAND, not left to decay: {'OK' if ok else f'FAIL (transitions={t8})'}")

    # 9. An incident that reached BEVESTIGD (and was notified) closes as
    # GESLOTEN_TIMEOUT if it later goes idle, not GESLOTEN_VALS_ALARM —
    # the latter would misrepresent a real, once-confirmed incident as
    # having turned out to be nothing. Same root cause / same round as #8.
    fast_cfg2 = dict(cfg)
    fast_cfg2["incident_score_decay_floor_minutes"] = 1
    fast_cfg2["incident_score_decay_factor_per_cycle"] = 0.5
    mgr3 = incidents_module.IncidentManager(db_module.connect(":memory:"), fast_cfg2, airport_db)
    ev9 = Event(hex="inc_test_6", callsign="TST600", event_type="emergency",
                confidence="BEVESTIGD", message="test", squawk="7700")
    mgr3.step("inc_test_6", "TST600", classify.AIRLINER, [ev9], {"squawk": "7700"}, 6000.0)
    now_t2, closed_state = 6000.0, None
    for _ in range(15):
        now_t2 += 300.0
        # squawk back to 1200 for reassessment purposes: simulates the
        # emergency clearing, which is what actually allows decay to run
        # at all (an active emergency squawk is exempt from decay).
        trans = mgr3.step("inc_test_6", "TST600", classify.AIRLINER, [], {"squawk": "1200"}, now_t2)
        if "inc_test_6" not in mgr3._open:
            closed_state = trans[0]["new_state"] if trans else None
            break
    ok = closed_state == incidents_module.CLOSED_TIMEOUT
    all_ok = all_ok and ok
    print(f"  previously-BEVESTIGD incident going idle closes as GESLOTEN_TIMEOUT, not a false alarm: {'OK' if ok else f'FAIL (closed_state={closed_state})'}")

    # 10. Deviation-recovery must still work a SECOND time on the same
    # incident (regression for the permanent-disable bug fixed this round
    # in _check_deviation_recovered — see its docstring).
    ev10a = Event(hex="inc_test_7", callsign="TST700", event_type="course_deviation",
                  confidence="MOGELIJK", message="test", origin_icao="EHAM", dest_icao="EGLL")
    mgr.step("inc_test_7", "TST700", classify.AIRLINER, [ev10a], {"squawk": "1200"}, 7000.0)
    recovery_ac2 = {"squawk": "1200", "lat": dest_ap["lat"] - 2.0, "lon": dest_ap["lon"], "track": 0.0}
    mgr.step("inc_test_7", "TST700", classify.AIRLINER, [], recovery_ac2, 7060.0)  # first recovery
    ev10b = Event(hex="inc_test_7", callsign="TST700", event_type="course_deviation",
                  confidence="MOGELIJK", message="test2", origin_icao="EHAM", dest_icao="EGLL")
    mgr.step("inc_test_7", "TST700", classify.AIRLINER, [ev10b], {"squawk": "1200"}, 7120.0)  # diverges again
    score_before_2nd_recovery = mgr._open["inc_test_7"]["score"]
    mgr.step("inc_test_7", "TST700", classify.AIRLINER, [], recovery_ac2, 7180.0)  # second recovery attempt
    score_after_2nd_recovery = mgr._open["inc_test_7"]["score"] if "inc_test_7" in mgr._open else 0.0
    ok = score_after_2nd_recovery < score_before_2nd_recovery
    all_ok = all_ok and ok
    print(f"  deviation-recovery still works a second time on the same incident "
          f"(before={score_before_2nd_recovery:.0f}, after={score_after_2nd_recovery:.0f}): {'OK' if ok else 'FAIL (permanently disabled after first recovery)'}")

    # 11. A sustained course_deviation, repeated many times with ZERO
    # corroboration from any other kind of evidence, must NOT reach
    # BEVESTIGD purely by accumulating repeat top-ups — even though its
    # score alone comfortably crosses the threshold. Real motivation: a
    # long, entirely routine reroute around contested/restricted airspace
    # or weather can keep a heading pointed away from the filed destination
    # for a long time at stable cruise without ever looking like anything
    # but a deviation — see incidents.py's
    # _DIMENSION_FOR_SOURCE/_SOFT_DIMENSIONS and the rewritten
    # _confirmed_bar_met.
    # Route corroborated throughout: this test is about what REPETITION and
    # CORROBORATION do, not about route trust (that's
    # check_confirmed_bar_structure). Without it every assertion below would
    # be capped by the route gate instead, hiding what's being tested.
    now_t3 = 8000.0
    for i in range(9):
        ev11 = Event(hex="inc_test_8", callsign="TST800", event_type="course_deviation",
                     confidence="MOGELIJK", message=f"test{i}", origin_icao="EHAM", dest_icao="EGLL",
                     route_corroborated=True)
        mgr.step("inc_test_8", "TST800", classify.AIRLINER, [ev11], {"squawk": "1200"}, now_t3)
        now_t3 += 60.0
    score11 = mgr._open["inc_test_8"]["score"]
    state11 = mgr._open["inc_test_8"]["state"]
    # CHECKPOINT.md bevinding 3/7: this used to assert score >= 85 with the
    # state held down to WAARSCHIJNLIJK by the source-name gate — i.e. the
    # score was allowed to run away and only the LABEL was corrected. That
    # left the runaway score sitting there as a loaded spring: a single
    # token hit from any other source lifted the gate and the state jumped
    # straight to BEVESTIGD off a score that was ~85% one damped source
    # (assertion 12 below). The score itself now saturates at
    # _SOURCE_SCORE_CAP["course_deviation"] = 55, so there is no spring left
    # to release.
    cap11 = incidents_module._SOURCE_SCORE_CAP["course_deviation"]
    ok = (abs(score11 - cap11) < 0.01 and state11 == incidents_module.LIKELY
          and score11 < cfg["incident_score_confirmed_threshold"])
    all_ok = all_ok and ok
    print(f"  sustained course_deviation ALONE verzadigt op zijn plafond en blijft WAARSCHIJNLIJK "
          f"(score={score11:.0f}, plafond={cap11:.0f}, BEVESTIGD-drempel={cfg['incident_score_confirmed_threshold']}, "
          f"state={state11}): {'OK' if ok else 'FAIL'}")

    # 12. One token hit from a second source must NOT be enough to unlock
    # BEVESTIGD (CHECKPOINT.md bevinding 7). Adding a single
    # premature_descent gives a genuinely different dimension (vertical vs
    # lateral) but only 25 points: 55 + 25 = 80, still under the threshold.
    # The qualitative requirement and the quantitative one now have to be
    # met independently, instead of the qualitative one acting as a switch
    # on an already-inflated score.
    ev12 = Event(hex="inc_test_8", callsign="TST800", event_type="premature_descent",
                 confidence="MOGELIJK", message="also descending now", origin_icao="EHAM", dest_icao="EGLL",
                 route_corroborated=True)
    mgr.step("inc_test_8", "TST800", classify.AIRLINER, [ev12], {"squawk": "1200"}, now_t3)
    now_t3 += 60.0
    score12 = mgr._open["inc_test_8"]["score"]
    state12 = mgr._open["inc_test_8"]["state"]
    ok = state12 == incidents_module.LIKELY and score12 < cfg["incident_score_confirmed_threshold"]
    all_ok = all_ok and ok
    print(f"  één losse hit uit een tweede bron ontgrendelt BEVESTIGD niet "
          f"(score={score12:.0f}, state={state12}): {'OK' if ok else 'FAIL'}")

    # 13. Once that second dimension carries real weight — a second descent
    # hit, so the aircraft is demonstrably off its (corroborated) route AND
    # demonstrably descending far from it — the bar is genuinely met and
    # BEVESTIGD follows. Proves the gate is about the structure of the
    # evidence, not a blanket ceiling.
    ev13 = Event(hex="inc_test_8", callsign="TST800", event_type="premature_descent",
                 confidence="MOGELIJK", message="still descending", origin_icao="EHAM", dest_icao="EGLL",
                 route_corroborated=True)
    mgr.step("inc_test_8", "TST800", classify.AIRLINER, [ev13], {"squawk": "1200"}, now_t3)
    score13 = mgr._open["inc_test_8"]["score"]
    state13 = mgr._open["inc_test_8"]["state"]
    ok = state13 == incidents_module.CONFIRMED
    all_ok = all_ok and ok
    print(f"  twee wezenlijk verschillende bewijsdimensies met echt gewicht, route gecorroboreerd "
          f"-> BEVESTIGD (score={score13:.0f}, state={state13}): {'OK' if ok else 'FAIL'}")

    return all_ok


def check_incident_engine_real_case_escalation(airport_db: AirportDB) -> bool:
    """Runs a real, sourced Case (AI850) through BOTH detector.py's geometry
    AND incidents.py's scoring/state-machine together, via run_case's
    incident_mgr hook — every other incident-engine check
    (check_incident_engine_regressions) only ever feeds it hand-built
    synthetic Events, never a real case's actual detector output over time.
    AI850 is a good vehicle specifically because THREE different real
    detector event_types fire for the same aircraft in sequence
    (holding_pattern for ~90min, then emergency/squawk 7700, then
    wrong_airport at landing) — exactly the 'multiple detectors fire in
    succession for the same aircraft and the score/state must actually
    escalate' scenario, not just detector.py's geometry in isolation.

    Also doubles as the first REAL-case regression coverage for two bugs
    fixed via self-review in BACKTEST_LOG.md ronde 15 (GESLOTEN_TIMEOUT/
    GESLOTEN_GELAND reachability) — round 15's own tests were synthetic.

    Found while building this: holding_pattern (and premature_descent) had
    no repeat-hit dampening the way course_deviation/corridor_deviation
    already did — AI850's ~90min sustained hold would otherwise add +25
    EVERY cycle, hitting BEVESTIGD (and dispatching a Telegram
    notification) within 3 minutes of first qualifying, for what can be a
    completely routine extended ATC hold. Fixed in incidents.py this round
    (score_for_event's holding_pattern/premature_descent branches now take
    is_repeat_type like the deviation types do) — and found a second, more
    fundamental bug while wiring the fix in: apply_events compared
    ev.event_type against evidence *source* strings, which only happens to
    be equal for course_deviation/corridor_deviation/premature_descent/
    wrong_airport. holding_pattern's source ('holding_destination'/
    'holding_non_destination') and emergency's (one of three squawk/
    confidence-dependent strings) never matched ev.event_type, so is_repeat
    silently evaluated to False regardless — meaning the fresh dampening
    code above wouldn't actually have engaged without this second fix
    either. See incidents.py's apply_events docstring/comment for the fix."""
    import classify
    import db as db_module
    from backtest_cases import _ai850
    from detector import Event
    from incidents import REPEATABLE_EVENT_TYPES, IncidentManager, score_for_event

    print("\n=== incident engine real-case escalation check (AI850) ===")
    all_ok = True

    # Structural invariant, not tied to AI850 specifically: every event_type
    # declared REPEATABLE must actually score a repeat hit lower than a
    # first hit — guards against a future detector type being added to
    # REPEATABLE_EVENT_TYPES without wiring dampening into score_for_event,
    # the exact bug class this round found for holding_pattern/
    # premature_descent (see REPEATABLE_EVENT_TYPES's docstring).
    dampening_ok = True
    for et in REPEATABLE_EVENT_TYPES:
        probe = Event(hex="probe", callsign="PROBE", event_type=et, confidence="MOGELIJK",
                       message="probe", at_destination=True)
        first_delta, _, _ = score_for_event(probe, False)
        repeat_delta, _, _ = score_for_event(probe, True)
        if not (repeat_delta < first_delta):
            dampening_ok = False
            print(f"  {et}: repeat delta ({repeat_delta}) not smaller than first-hit delta ({first_delta})")
    all_ok = all_ok and dampening_ok
    print(f"  every REPEATABLE_EVENT_TYPES type actually dampens repeat hits: {'OK' if dampening_ok else 'FAIL (see above)'}")

    case = _ai850()
    conn = db_module.connect(":memory:")
    cfg = dict(CONFIG)
    mgr = IncidentManager(conn, cfg, airport_db)
    run_case(case, airport_db, cfg, incident_mgr=mgr, aircraft_class=classify.AIRLINER)

    rows = conn.execute(
        f"SELECT {','.join(db_module._INCIDENT_COLS)} FROM incidents WHERE hex = ? ORDER BY id",
        (case.hex_id,),
    ).fetchall()
    incidents_seen = [db_module._incident_row_to_dict(row) for row in rows]
    ok = len(incidents_seen) == 2
    all_ok = all_ok and ok
    print(f"  two distinct incidents opened for this one aircraft (hold-driven, then emergency-driven): "
          f"{'OK' if ok else f'FAIL (got {len(incidents_seen)})'}")
    if not ok:
        return False

    hold_incident, emergency_incident = incidents_seen

    hold_evidence = db_module.get_incident_evidence(conn, hold_incident["id"])
    hold_sources = [e["source"] for e in hold_evidence]
    # Damped repeat scoring means the FIRST hold-driven incident should take
    # several repeat hits (not one giant jump) to cross each threshold —
    # concretely, more than one 'holding_destination' evidence row before
    # ever reaching BEVESTIGD requires actual escalation.
    ok = hold_sources.count("holding_destination") >= 4
    all_ok = all_ok and ok
    print(f"  sustained hold contributes multiple damped evidence rows, not one jump "
          f"({hold_sources.count('holding_destination')} holding_destination rows): {'OK' if ok else 'FAIL'}")

    # The hold-driven incident escalates to WAARSCHIJNLIJK — and stops there
    # — before eventually going idle (no more holding evidence once the
    # aircraft leaves the hold) and timing out. state itself is
    # GESLOTEN_TIMEOUT by the time we read it back; peak_score is what shows
    # how far it actually escalated.
    #
    # Changed from "escalates all the way to BEVESTIGD (peak_score 150)"
    # (CHECKPOINT.md bevinding 3). The old behaviour came from the repeat
    # dampening being a fixed fraction: 25 + 8 per cycle diverges, so a
    # sustained hold reached the BEVESTIGD threshold ~9 cycles past the
    # 20-cycle streak gate and then ran to the score cap. But an hours-long
    # hold at one's own filed destination has ordinary explanations that do
    # not go away by waiting longer — arrival congestion, weather at the
    # field, ATC flow control. Repetition rules out sensor noise, not a
    # systematic cause, so "it kept holding" cannot be what carries an
    # incident to a level that is supposed to mean no reasonable alternative
    # remains. holding_destination now saturates at 65 (WAARSCHIJNLIJK).
    #
    # Costs no early warning: WAARSCHIJNLIJK is already a notifying state
    # (_maybe_notify), so the Telegram fires at exactly the same moment it
    # did before — see the lead-time assertion further down. BEVESTIGD for
    # AI850 arrives with the MAYDAY squawk, on the second incident, which is
    # when it genuinely became certain.
    ok = (cfg["incident_score_likely_threshold"] <= hold_incident["peak_score"] < cfg["incident_score_confirmed_threshold"]
          and hold_incident["state"] == "GESLOTEN_TIMEOUT"
          and hold_incident["resolution_reason"] and "eerdere escalatie" in hold_incident["resolution_reason"])
    all_ok = all_ok and ok
    print(f"  hold-driven incident escalates to WAARSCHIJNLIJK and verzadigt daar (peak_score={hold_incident['peak_score']:.0f}, "
          f"BEVESTIGD-drempel={cfg['incident_score_confirmed_threshold']}), then times out honestly "
          f"(state={hold_incident['state']}, reason={hold_incident['resolution_reason']!r}): "
          f"{'OK' if ok else 'FAIL'}")

    # A sustained hold at the aircraft's OWN filed destination is a single
    # dimension of evidence (loiter). Even with the route corroborated by our
    # own observation of the flight, that alone must not read as BEVESTIGD —
    # this is the structural half of the same finding, independent of where
    # the score happens to land.
    hold_dims_ok = not mgr._confirmed_bar_met(hold_incident["id"], route_corroborated_now=True)
    all_ok = all_ok and hold_dims_ok
    print(f"  ...en ook structureel niet BEVESTIGD-waardig: één bewijsdimensie (loiter), zelfs met "
          f"gecorroboreerde route: {'OK' if hold_dims_ok else 'FAIL'}")

    # This incident was never notified as a false alarm — the round-15 fix
    # (GESLOTEN_TIMEOUT vs GESLOTEN_VALS_ALARM) exercised against real data
    # for the first time.
    ok = hold_incident["state"] != "GESLOTEN_VALS_ALARM"
    all_ok = all_ok and ok
    print(f"  a real, once-BEVESTIGD incident going idle is NOT mislabeled a false alarm: {'OK' if ok else 'FAIL'}")

    emergency_sources = {e["source"] for e in db_module.get_incident_evidence(conn, emergency_incident["id"])}
    ok = ("emergency_squawk" in emergency_sources and "wrong_airport" in emergency_sources
          and emergency_incident["state"] == "GESLOTEN_GELAND")
    all_ok = all_ok and ok
    print(f"  second incident (real MAYDAY squawk) escalates via emergency_squawk THEN wrong_airport "
          f"evidence and closes as a confirmed diversion (state={emergency_incident['state']}): "
          f"{'OK' if ok else 'FAIL'}")

    # The early-warning property round 1 found for the raw detector
    # (holding_pattern fires 1h26m before the crew's real divert decision)
    # must hold at the INCIDENT level too. Measured against the NOTIFYING
    # threshold (WAARSCHIJNLIJK), not BEVESTIGD: _maybe_notify dispatches on
    # first reaching either, so WAARSCHIJNLIJK is the moment the operator
    # actually hears about it — which is what "early warning" means. Before
    # CHECKPOINT.md bevinding 3 this was asserted against the BEVESTIGD
    # threshold; that measured the same alert one label too confidently, not
    # any earlier.
    # Reconstruct the cumulative score from the evidence deltas to find the ts.
    cum = 0.0
    likely_ts = None
    for row in db_module.get_incident_evidence(conn, hold_incident["id"]):
        cum += row["delta"]
        if cum >= cfg["incident_score_likely_threshold"]:
            likely_ts = row["ts"]
            break
    real_mayday_t = 189 * 60.0
    ok = likely_ts is not None and likely_ts < real_mayday_t
    all_ok = all_ok and ok
    print(f"  incident reaches WAARSCHIJNLIJK (= het niveau dat notificeert) well before the real "
          f"MAYDAY declaration ({fmt_t(real_mayday_t - likely_ts) if likely_ts is not None else 'n/a'} early): "
          f"{'OK' if ok else 'FAIL'}")

    return all_ok


def check_real_case_confidence_outcomes(airport_db: AirportDB) -> bool:
    """The one guard that the tightened BEVESTIGD gate (CHECKPOINT.md
    bevindingen 2/3/4) did not simply make the top level unreachable.

    Every other check in this file verifies that something is now correctly
    NOT confirmed. That is only half the job: a rule that never says
    BEVESTIGD is exactly as useless as one that always does. So this runs
    EVERY real, sourced case in backtest_cases.py through the full incident
    engine and asserts, per case, the confidence level the evidence actually
    warrants — the balance, in one table, so a future tuning round can see
    immediately if it has broken either side.

    Expectations, and why:
      - real diversions that LANDED somewhere unplanned, on a route we
        watched them fly -> BEVESTIGD (physical ground truth + corroborated
        reference data: nothing benign left);
      - real diversions confirmed by a MAYDAY squawk -> BEVESTIGD (a
        deliberate 4-digit pilot action needs no support);
      - a real, genuine deviation still in progress -> not BEVESTIGD; it is
        real, it is reported (WAARSCHIJNLIJK notifies), but it is not yet
        certain, and saying otherwise would be a guess dressed as a fact;
      - a real diversion we only INFERRED from a signal dropping out ->
        also not BEVESTIGD, for the same reason: signal_lost_near_airport
        reasons from the ABSENCE of data, and the ordinary explanation for
        absent data — an ADS-B coverage gap — is exactly what the detector's
        own docstring says is common near the small/military fields it
        watches. A real alternative explanation therefore survives, however
        right the inference turns out to be;
      - a real weather detour that landed normally -> never escalates."""
    import classify
    import db as db_module
    import incidents as incidents_module
    from backtest_cases import CASES

    print("\n=== zekerheidsuitkomst per echte case (eind-tot-eind) ===")
    all_ok = True

    # Per case: does ANY incident for this aircraft deserve to reach
    # BEVESTIGD by the end of the replay? Keyed on case name prefix.
    EXPECT_CONFIRMED = {
        "Air India AI850": True,           # MAYDAY squawk + landing at Gwalior
        "Air France AF9": True,            # emergency squawk on the turnback
        # The two United 2078 variants are the same real diversion observed
        # two different ways, and they land on different levels ON PURPOSE:
        # the first ends in an observed touchdown at Luke AFB (ground truth),
        # the second only in the signal dropping out nearby. Same event,
        # different quality of evidence — so a different confidence.
        "United 2078 Houston-Phoenix-Luke AFB": True,
        "United 2078 Houston-Phoenix, signal lost": False,
        "Emirates EK225": True,            # landed LHR after a corroborated DXB->SFO leg
        "Transavia France TO3510": True,   # early return, departure observed from the filed origin
        "Synthetic ATL-MCO": True,         # overflew MCO, landed FLL
        "Turkish Airlines TK17": True,     # landed Manchester instead of Toronto
        "Delta 2778": False,               # real storm detour that landed normally
    }

    for case in CASES:
        expected = None
        for prefix, want in EXPECT_CONFIRMED.items():
            if case.name.startswith(prefix):
                expected = want
                break
        if expected is None:
            print(f"  {case.name[:60]}: GEEN VERWACHTING GEDEFINIEERD — voeg toe aan EXPECT_CONFIRMED")
            all_ok = False
            continue

        conn = db_module.connect(":memory:")
        mgr = incidents_module.IncidentManager(conn, dict(CONFIG), airport_db)
        run_case(case, airport_db, dict(CONFIG), incident_mgr=mgr, aircraft_class=classify.AIRLINER)
        rows = conn.execute(
            "SELECT id, peak_score, state FROM incidents WHERE hex = ? ORDER BY id", (case.hex_id,)
        ).fetchall()

        # An incident that reached BEVESTIGD either still reads BEVESTIGD or
        # has since closed as a confirmed diversion (GESLOTEN_GELAND) — both
        # mean "the system said certain". notified_state would be the purest
        # signal but is only set when a transition fires, so read the states.
        reached = any(state in (incidents_module.CONFIRMED, incidents_module.CLOSED_LANDED)
                      for _id, _peak, state in rows)
        detail = ", ".join(f"{state}(peak {peak:.0f})" for _id, peak, state in rows) or "geen incident"
        ok = reached == expected
        all_ok = all_ok and ok
        verdict = "BEVESTIGD bereikt" if reached else "niet BEVESTIGD"
        print(f"  {case.name[:58]:58} {verdict:18} (verwacht {'wel' if expected else 'niet':4}) "
              f"[{detail}]: {'OK' if ok else 'FAIL'}")

    return all_ok


def check_incident_engine_real_case_recovery(airport_db: AirportDB) -> bool:
    """Runs DAL2778 (BACKTEST_LOG.md ronde 22) — a real, sourced, LARGE
    weather-avoidance detour that never diverted anywhere — through the
    full incident engine, the opposite validation from check_incident_
    engine_real_case_escalation above: confirms a real, geometrically
    genuine deviation does NOT escalate past MOGELIJK and does eventually
    close, rather than lingering open or (worse) escalating on a single,
    uncorroborated detector hit.

    Also regression-tests a real, documented side effect of this case's
    own headline finding (see _dal2778's docstring): main.py/backtest.py's
    ongoing route_plausible recheck nulls track.route at t=8min, well
    before course_deviation fires at t=34min — so the resulting incident
    never learns a dest_icao, meaning `_check_landed`'s destination-match
    can't recognize the later normal landing at ATL as "the expected
    destination". The incident still correctly never escalates and still
    closes (via idle decay -> GESLOTEN_VALS_ALARM, not the more accurate
    GESLOTEN_NORMAAL a route-aware incident would get) — a real, minor,
    cascading imprecision from the same root cause, asserted here
    explicitly so a future fix to that root cause has a test that will
    correctly start expecting GESLOTEN_NORMAAL instead, rather than this
    gap silently going unnoticed either way."""
    import classify
    import db as db_module
    from backtest_cases import _dal2778
    from incidents import IncidentManager

    print("\n=== incident engine real-case recovery check (DAL2778) ===")
    all_ok = True

    case = _dal2778()
    conn = db_module.connect(":memory:")
    cfg = dict(CONFIG)
    mgr = IncidentManager(conn, cfg, airport_db)
    run_case(case, airport_db, cfg, incident_mgr=mgr, aircraft_class=classify.AIRLINER)

    rows = conn.execute(
        f"SELECT {','.join(db_module._INCIDENT_COLS)} FROM incidents WHERE hex = ? ORDER BY id",
        (case.hex_id,),
    ).fetchall()
    incidents_seen = [db_module._incident_row_to_dict(row) for row in rows]
    ok = len(incidents_seen) == 1
    all_ok = all_ok and ok
    print(f"  exactly one incident opened for this real, non-diverting flight: "
          f"{'OK' if ok else f'FAIL (got {len(incidents_seen)})'}")
    if not ok:
        return False

    inc = incidents_seen[0]
    ok = inc["peak_score"] < cfg["incident_score_likely_threshold"]
    all_ok = all_ok and ok
    print(f"  a real, single, uncorroborated course_deviation hit never escalates past MOGELIJK "
          f"(peak_score={inc['peak_score']:.0f}, LIKELY threshold={cfg['incident_score_likely_threshold']}): "
          f"{'OK' if ok else 'FAIL'}")

    ok = inc["state"] in ("GESLOTEN_VALS_ALARM", "GESLOTEN_NORMAAL")
    all_ok = all_ok and ok
    print(f"  incident closes cleanly rather than lingering open (state={inc['state']}): {'OK' if ok else 'FAIL'}")

    # The documented cascading side effect: dest_icao is None because
    # route_plausible nulled the route before course_deviation ever saw a
    # valid one to attach to the incident.
    ok = inc["dest_icao"] is None and inc["state"] == "GESLOTEN_VALS_ALARM"
    all_ok = all_ok and ok
    print(f"  known cascading gap still present as documented (dest_icao=None, closes GESLOTEN_VALS_ALARM "
          f"not GESLOTEN_NORMAAL — expected to flip once the route_plausible finding is fixed): "
          f"{'OK' if ok else 'FAIL'}")

    return all_ok


def check_airspace_regressions(airport_db: AirportDB) -> bool:
    """Standalone smoke test for airspace.py (MASTERPLAN.md sectie 6.1) and
    incidents.py's peer-consensus check (sectie 6.2), both new in
    BACKTEST_LOG.md ronde 10. No network calls: point-in-polygon and
    explain_position are tested directly against a hand-built square
    polygon; peer-consensus is tested by seeding IncidentManager's
    in-memory state directly rather than going through step()'s full
    detector-Event path."""
    import classify
    import db as db_module
    import incidents as incidents_module
    from airspace import _parse_tfr_feature, _point_in_polygon, explain_position

    print("\n=== airspace (weather + peer-consensus) regression check ===")
    all_ok = True

    # Real TFR GeoJSON feature, live-captured 2026-08-07 from
    # tfr.faa.gov's public GeoServer WFS endpoint (1NM N Ouray, CO — see
    # BACKTEST_LOG.md ronde 13) — not synthetic, an actual API response
    # shape, same convention as check_bad_route_regressions using real
    # coordinates.
    tfr_feature = {
        "type": "Feature", "id": "V_TFR_LOC.6/9802",
        "geometry": {"type": "Polygon", "coordinates": [[
            [-107.79166667, 38.25833333], [-107.74166667, 38.01666667],
            [-107.26666667, 38.03333333], [-107.25833333, 38.225],
            [-107.45, 38.34166667], [-107.65, 38.34166667], [-107.79166667, 38.25833333],
        ]]},
        "properties": {
            "GID": 231054, "CNS_LOCATION_ID": "ZDV", "NOTAM_KEY": "6/9802-1-FDC-F",
            "TITLE": "1NM N OURAY, CO, Wednesday, July 29, 2026 through Tuesday, August 11, 2026 UTC",
            "STATE": "CO", "LEGAL": "HAZARDS",
        },
    }
    parsed = _parse_tfr_feature(tfr_feature)
    inside_ouray = parsed is not None and _point_in_polygon(38.15, -107.5, parsed["polygon"])
    ok = parsed is not None and parsed["id"] == "6/9802-1-FDC-F" and inside_ouray
    all_ok = all_ok and ok
    print(f"  real TFR GeoJSON feature parses and its polygon contains a point near Ouray, CO: {'OK' if ok else 'FAIL'}")

    non_polygon = _parse_tfr_feature({"type": "Feature", "geometry": {"type": "MultiPolygon", "coordinates": []}, "properties": {}})
    ok = non_polygon is None
    all_ok = all_ok and ok
    print(f"  non-Polygon TFR geometry (e.g. MultiPolygon) skipped, not crashed: {'OK' if ok else 'FAIL'}")

    # A simple square polygon: lat 10-11, lon 10-11.
    square = [(10.0, 10.0), (10.0, 11.0), (11.0, 11.0), (11.0, 10.0), (10.0, 10.0)]
    inside_ok = _point_in_polygon(10.5, 10.5, square) is True
    outside_ok = _point_in_polygon(20.0, 20.0, square) is False
    all_ok = all_ok and inside_ok and outside_ok
    print(f"  point-in-polygon (inside/outside a hand-built square): {'OK' if inside_ok and outside_ok else 'FAIL'}")

    hazard = {"hazard": "CONVECTIVE", "id": "TEST1", "alt_lo_ft": 10000, "alt_hi_ft": 40000, "polygon": square}
    alt_match = explain_position(10.5, 10.5, 25000, [hazard]) is not None
    alt_no_match = explain_position(10.5, 10.5, 45000, [hazard]) is None
    pos_no_match = explain_position(20.0, 20.0, 25000, [hazard]) is None
    ok = alt_match and alt_no_match and pos_no_match
    all_ok = all_ok and ok
    print(f"  explain_position altitude + position filtering: {'OK' if ok else 'FAIL'}")

    # Peer-consensus: 3 incidents in the same coarse grid cell should
    # trigger consensus for each other; a 4th far away should not.
    conn = db_module.connect(":memory:")
    cfg = dict(CONFIG)
    mgr = incidents_module.IncidentManager(conn, cfg, airport_db)
    from detector import Event
    for i, (lat, lon) in enumerate([(50.01, 10.01), (50.05, 10.08), (50.09, 10.02)]):
        ev = Event(hex=f"peer_test_{i}", callsign=f"PT{i}", event_type="corridor_deviation",
                    confidence="MOGELIJK", message="test", lat=lat, lon=lon)
        mgr.step(f"peer_test_{i}", f"PT{i}", classify.AIRLINER, [ev], {"squawk": "1200"}, 5000.0)
    ev_far = Event(hex="peer_test_far", callsign="PTFAR", event_type="corridor_deviation",
                    confidence="MOGELIJK", message="test", lat=-30.0, lon=140.0)
    mgr.step("peer_test_far", "PTFAR", classify.AIRLINER, [ev_far], {"squawk": "1200"}, 5000.0)

    clustered_count = mgr._peer_consensus_count(mgr._open["peer_test_0"])
    far_count = mgr._peer_consensus_count(mgr._open["peer_test_far"])
    ok = clustered_count == 2 and far_count == 0
    all_ok = all_ok and ok
    print(f"  peer-consensus grid clustering (clustered={clustered_count}, expected 2; far={far_count}, expected 0): {'OK' if ok else 'FAIL'}")

    # Peer consensus applies to a clustered incident (3 total in-cell >=
    # peer_consensus_min_aircraft default 3). Entry point moved from
    # reassess() to apply_context_checks() — CHECKPOINT.md bevinding 5.
    mgr.apply_context_checks("peer_test_0", 5060.0)
    evidence_sources = {e["source"] for e in db_module.get_incident_evidence(conn, mgr._open["peer_test_0"]["id"])}
    ok = "peer_consensus" in evidence_sources
    all_ok = all_ok and ok
    print(f"  apply_context_checks() applies peer_consensus evidence for a clustered incident: {'OK' if ok else 'FAIL'}")

    # CHECKPOINT.md bevinding 5: the whole point of the entry-point move.
    # An incident that receives FRESH EVIDENCE EVERY CYCLE is the scenario
    # in which an incident actually climbs — and it used to be exactly the
    # scenario in which the weather check never ran, because reassess() only
    # ran in cycles with no events. main.py fetched the hazard polygons
    # every cycle and passed them in, and they were thrown away unread.
    hazard_cfg = dict(CONFIG)
    mgr2 = incidents_module.IncidentManager(db_module.connect(":memory:"), hazard_cfg, airport_db)
    hazards = [{"hazard": "CONVECTIVE", "id": "TEST2", "alt_lo_ft": None, "alt_hi_ft": None, "polygon": square}]
    t = 6000.0
    for i in range(5):
        ev = Event(hex="wx_test", callsign="WXT", event_type="corridor_deviation", confidence="MOGELIJK",
                    message=f"m{i}", lat=10.5, lon=10.5, alt=25000, origin_icao="EHAM", dest_icao="EGLL")
        mgr2.step("wx_test", "WXT", classify.AIRLINER, [ev], {"squawk": "1200"}, t, hazards)
        t += 60.0
    wx_sources = {e["source"] for e in db_module.get_incident_evidence(mgr2.db, mgr2._open["wx_test"]["id"])}
    ok = "weather_explains" in wx_sources
    all_ok = all_ok and ok
    print(f"  incident dat ELKE cyclus vers bewijs krijgt, binnen een actieve hazard-polygoon, "
          f"krijgt tóch de weerverklaring: {'OK' if ok else 'FAIL'}")

    # ...and that explanation persists as a structural block on BEVESTIGD,
    # not just as a one-off score discount that the next few repeat hits
    # erase (CHECKPOINT.md bevinding 6a).
    ok = not mgr2._confirmed_bar_met(mgr2._open["wx_test"]["id"], route_corroborated_now=True)
    all_ok = all_ok and ok
    print(f"  ...en die verklaring blokkeert BEVESTIGD structureel, ook met gecorroboreerde route: "
          f"{'OK' if ok else 'FAIL'}")

    # CHECKPOINT.md bevinding 6b: bad weather does NOT explain landing at
    # another airport — it is the most common cause of a REAL diversion. The
    # unscoped check used to hand such an incident -50, dropping it from
    # BEVESTIGD to MOGELIJK: lowering confidence in precisely the case it
    # should have raised it.
    mgr3 = incidents_module.IncidentManager(db_module.connect(":memory:"), hazard_cfg, airport_db)
    ev_landed = Event(hex="wx_landed", callsign="WXL", event_type="wrong_airport", confidence="BEVESTIGD",
                      message="m", lat=10.5, lon=10.5, alt=0, origin_icao="EHAM", dest_icao="EGLL",
                      route_corroborated=True)
    mgr3.step("wx_landed", "WXL", classify.AIRLINER, [ev_landed], {"squawk": "1200"}, 7000.0, hazards)
    inc3 = mgr3._open["wx_landed"]
    wx_sources3 = {e["source"] for e in db_module.get_incident_evidence(mgr3.db, inc3["id"])}
    ok = "weather_explains" not in wx_sources3 and inc3["state"] == incidents_module.CONFIRMED
    all_ok = all_ok and ok
    print(f"  een waargenomen landing elders binnen dezelfde polygoon wordt NIET wegverklaard door weer "
          f"(score {inc3['score']:.0f}, state {inc3['state']}): {'OK' if ok else 'FAIL'}")

    # Same inversion for peer consensus: an airport closing and several
    # aircraft each diverting to their alternate is several REAL diversions
    # — the most newsworthy thing this system can see — and the unscoped
    # rule suppressed every one of them, since each one's peers are the
    # others.
    mgr4 = incidents_module.IncidentManager(db_module.connect(":memory:"), hazard_cfg, airport_db)
    for i, (lat, lon) in enumerate([(50.01, 10.01), (50.05, 10.08), (50.09, 10.02)]):
        ev = Event(hex=f"mass_{i}", callsign=f"MD{i}", event_type="wrong_airport", confidence="BEVESTIGD",
                    message="m", lat=lat, lon=lon, origin_icao="EHAM", dest_icao="EGLL",
                    route_corroborated=True)
        mgr4.step(f"mass_{i}", f"MD{i}", classify.AIRLINER, [ev], {"squawk": "1200"}, 8000.0)
    for i in range(3):
        mgr4.apply_context_checks(f"mass_{i}", 8060.0)
    suppressed = [i for i in range(3)
                  if "peer_consensus" in {e["source"] for e in
                                          db_module.get_incident_evidence(mgr4.db, mgr4._open[f"mass_{i}"]["id"])}]
    states = [mgr4._open[f"mass_{i}"]["state"] for i in range(3)]
    ok = not suppressed and all(s == incidents_module.CONFIRMED for s in states)
    all_ok = all_ok and ok
    print(f"  massale diversie (3 toestellen die elk elders landen in dezelfde cel) wordt NIET door "
          f"peer-consensus onderdrukt (states={states}): {'OK' if ok else f'FAIL (onderdrukt: {suppressed})'}")

    return all_ok


def check_disputed_evidence_gate(airport_db: AirportDB) -> bool:
    """CHECKPOINT.md bevinding 10 en 13. Bevinding 4 eiste route-corroboratie
    voordat route-afhankelijk bewijs mag bevestigen, maar implementeerde dat als
    "er bestaat ergens een bevestiging" in plaats van "de bronnen zijn het
    eens". Corroboratie en tegenspraak sluiten elkaar niet uit: de corroboratie
    kan uit onze eigen waargenomen voortgang komen terwijl hexdb.io tegelijk
    een andere bestemming noemt. Bij onenigheid tussen twee referentiebronnen is
    de eerlijke toestand onbeslist — en onbeslist is precies de hypothese die
    elk route-afhankelijk signaal tegelijk verklaart.

    Plus: een incident dat de BEVESTIGD-poort bewust nooit haalde mag bij
    afsluiting niet alsnog "bevestigde diversie" heten. resolution_reason is het
    enige dat van een gesloten incident overblijft."""
    import classify
    import db as db_module
    import incidents as incidents_module
    from detector import Event

    print("\n=== betwist bewijs en het afsluitlabel ===")
    all_ok = True
    cfg = dict(CONFIG)

    def build(hex_id, specs, corroborated):
        mgr = incidents_module.IncidentManager(db_module.connect(":memory:"), cfg, airport_db)
        t = 1000.0
        for et, kwargs in specs:
            kwargs = dict(kwargs)
            mgr.step(hex_id, "TSTS", classify.AIRLINER, [
                Event(hex=hex_id, callsign="TSTS", event_type=et,
                      confidence=kwargs.pop("confidence", "MOGELIJK"),
                      message="m", origin_icao="EHAM", dest_icao="EGLL",
                      route_corroborated=corroborated, **kwargs)], {"squawk": "1200"}, t)
            t += 60.0
        return mgr, mgr._open.get(hex_id)

    route_dependent = [("corridor_deviation", {})] * 3 + [("premature_descent", {})] * 3

    # (a) hexdb.io noemt een ANDERE bestemming, terwijl wij het toestel zelf
    #     richting de gefilede bestemming hebben zien vorderen. Twee
    #     referentiebronnen in tegenspraak -> geen BEVESTIGD.
    mgr, inc = build("dg1", route_dependent + [("wrong_airport", {"route_source_disputed": True})],
                     corroborated=True)
    ok = inc["state"] != incidents_module.CONFIRMED
    all_ok = all_ok and ok
    print(f"  betwiste routebron (hexdb.io noemt een andere bestemming) + eigen waargenomen voortgang "
          f"-> geen BEVESTIGD (score {inc['score']:.0f}, state {inc['state']}): {'OK' if ok else 'FAIL'}")

    # (b) Onbevestigde GRONDSTATUS: punt 4's premisse ("we zagen het fysiek aan
    #     de grond") staat dan juist niet vast, dus dit mag geen volwaardig
    #     grondbewijs zijn — wel een eigen soft-dimensie.
    mgr, inc = build("dg2", route_dependent + [("wrong_airport", {"confidence": "WAARSCHIJNLIJK"})],
                     corroborated=True)
    dims = mgr._active_dimensions(inc["id"])
    ok = (incidents_module.DIM_GROUND_TRUTH not in dims
          and incidents_module.DIM_GROUND_TRUTH_PROVISIONAL in dims)
    all_ok = all_ok and ok
    print(f"  onbevestigde grondstatus telt als voorlopig grondbewijs, niet als punt-4-grondbewijs "
          f"(dims={sorted(dims)}): {'OK' if ok else 'FAIL'}")

    # (c) Regressie: de poort mag niet gewoon dicht komen te staan.
    mgr, inc = build("dg3", [("corridor_deviation", {})] * 3
                            + [("wrong_airport", {"confidence": "BEVESTIGD"})], corroborated=True)
    ok = inc["state"] == incidents_module.CONFIRMED
    all_ok = all_ok and ok
    print(f"  onbetwiste landing elders + gecorroboreerde route -> nog steeds BEVESTIGD "
          f"(score {inc['score']:.0f}, state {inc['state']}): {'OK' if ok else 'FAIL'}")

    def land_and_read(hex_id, confidence):
        mgr = incidents_module.IncidentManager(db_module.connect(":memory:"), cfg, airport_db)
        mgr.step(hex_id, "TSTS", classify.AIRLINER, [
            Event(hex=hex_id, callsign="TSTS", event_type="wrong_airport", confidence=confidence,
                  message="m", origin_icao="EHAM", dest_icao="EGLL",
                  lat=51.4775, lon=-0.4614, route_corroborated=False)], {"squawk": "1200"}, 1000.0)
        mgr.step(hex_id, "TSTS", classify.AIRLINER, [],
                 {"squawk": "1200", "on_ground": True, "lat": 51.148, "lon": -0.190}, 1100.0)
        return mgr.db.execute(
            "SELECT state, resolution_reason FROM incidents WHERE hex=?", (hex_id,)).fetchone()

    # (d) Gedegradeerd bewijs: wel GESLOTEN_GELAND (het toestel is hier echt
    #     geland), niet "bevestigde diversie" (de duiding is onzeker).
    row = land_and_read("dg4", "WAARSCHIJNLIJK")
    ok = row[0] == "GESLOTEN_GELAND" and "niet bevestigd" in row[1] and "bevestigde diversie" not in row[1]
    all_ok = all_ok and ok
    print(f"  betwiste landing sluit als GESLOTEN_GELAND maar NIET als \"bevestigde diversie\" "
          f"({row[0]}): {'OK' if ok else f'FAIL — {row[1]!r}'}")

    # (e) Regressie op dezelfde regel.
    row = land_and_read("dg5", "BEVESTIGD")
    ok = row[0] == "GESLOTEN_GELAND" and "bevestigde diversie" in row[1]
    all_ok = all_ok and ok
    print(f"  onbetwiste landing sluit nog steeds als \"bevestigde diversie\": "
          f"{'OK' if ok else f'FAIL — {row[1]!r}'}")

    return all_ok


def check_benign_explanation_scope(airport_db: AirportDB) -> bool:
    """CHECKPOINT.md bevinding 11. Of een benigne verklaring (weer/peer-
    consensus) werd vastgelegd hing af van de VOLGORDE waarin de detectoren
    toevallig vuurden: de oude subset-test werd op één moment geëvalueerd en het
    resultaat was daarna permanent, want de check is eenmalig per incident en de
    bewijsverzameling groeit alleen. Identiek bewijs op identieke positie in
    dezelfde SIGMET-polygoon kwam zo uit op WAARSCHIJNLIJK of op BEVESTIGD.

    Erger nog: dezelfde tweede dimensie die de verklaring uitschakelde,
    ontgrendelde punt 5 van _confirmed_bar_met. Eén extra signaal verwijderde
    dus de ontlastende verklaring én leverde de bevestigende voorwaarde —
    terwijl om een onweersgebied heen vliegen én daarbij dalen de meest
    doodgewone waarneming is die er bestaat."""
    import classify
    import db as db_module
    import incidents as incidents_module
    from detector import Event

    print("\n=== reikwijdte van de benigne verklaring ===")
    all_ok = True
    cfg = dict(CONFIG)
    HAZ = [{"id": "SIGMET-T", "hazard": "CONVECTIVE", "alt_lo_ft": 0, "alt_hi_ft": 45000,
            "polygon": [(50.0, -5.0), (55.0, -5.0), (55.0, 5.0), (50.0, 5.0), (50.0, -5.0)]}]
    LAT, VERT = "corridor_deviation", "premature_descent"

    _NO_AC = object()  # sentinel: ac=None is een betekenisvolle waarde, geen default

    def _step(mgr, hex_id, spec, t, hazards, lat, lon, ac=_NO_AC):
        et, conf = spec if isinstance(spec, tuple) else (spec, "MOGELIJK")
        if ac is _NO_AC:
            ac = {"squawk": "1200", "lat": lat, "lon": lon, "alt": 30000}
        mgr.step(hex_id, "TSTS", classify.AIRLINER, [
            Event(hex=hex_id, callsign="TSTS", event_type=et, confidence=conf,
                  message="m", origin_icao="EHAM", dest_icao="EGLL",
                  lat=lat, lon=lon, alt=30000, route_corroborated=True)], ac, t, hazards)

    def run(hex_id, order, hazards=HAZ, lat=52.0, lon=0.0, mgr=None, t=1000.0):
        mgr = mgr or incidents_module.IncidentManager(db_module.connect(":memory:"), cfg, airport_db)
        for spec in order:
            _step(mgr, hex_id, spec, t, hazards, lat, lon)
            t += 60.0
        inc = mgr._open.get(hex_id)
        return mgr, inc, mgr._evidence_sources_seen(inc["id"]), t

    def run_peer(hex_id, order):
        """Vier buren in dezelfde rastercel, daarna het onderzochte incident."""
        mgr = incidents_module.IncidentManager(db_module.connect(":memory:"), cfg, airport_db)
        for i in range(4):
            mgr.step(f"{hex_id}nb{i}", "NBR", classify.AIRLINER, [
                Event(hex=f"{hex_id}nb{i}", callsign="NBR", event_type=LAT, confidence="MOGELIJK",
                      message="m", origin_icao="EHAM", dest_icao="EGLL",
                      lat=52.0, lon=0.0, alt=30000, route_corroborated=True)],
                {"squawk": "1200", "lat": 52.0, "lon": 0.0}, 1000.0)
        return run(hex_id, order, hazards=None, mgr=mgr)

    # (a) Dezelfde zes Events in beide volgordes: zowel het NIVEAU als de SCORE
    #     moeten gelijk zijn. Vóór de fix: 61/WAARSCHIJNLIJK vs 91/BEVESTIGD.
    _m1, inc1, src1, _t = run("be1", [LAT] * 3 + [VERT] * 3)
    _m2, inc2, src2, _t = run("be2", [VERT] * 3 + [LAT] * 3)
    for label, inc, src in (("lateraal eerst", inc1, src1), ("daling eerst", inc2, src2)):
        ok = "weather_explains" in src and inc["state"] != incidents_module.CONFIRMED
        all_ok = all_ok and ok
        print(f"  {label}: weerverklaring vastgelegd en geen BEVESTIGD "
              f"(score {inc['score']:.0f}, state {inc['state']}): {'OK' if ok else 'FAIL'}")
    ok = abs(inc1["score"] - inc2["score"]) < 0.01 and inc1["state"] == inc2["state"]
    all_ok = all_ok and ok
    print(f"  zelfde bewijs in beide volgordes levert dezelfde score en hetzelfde niveau "
          f"({inc1['score']:.1f} vs {inc2['score']:.1f}): {'OK' if ok else 'FAIL'}")

    # (b) Idem voor peer-consensus.
    _m3, inc3, src3, _t = run_peer("be3", [LAT] * 3 + [VERT] * 3)
    _m4, inc4, src4, _t = run_peer("be4", [VERT] * 3 + [LAT] * 3)
    ok = ("peer_consensus" in src3 and "peer_consensus" in src4
          and abs(inc3["score"] - inc4["score"]) < 0.01
          and inc3["state"] == inc4["state"]
          and inc3["state"] != incidents_module.CONFIRMED)
    all_ok = all_ok and ok
    print(f"  idem voor peer-consensus ({inc3['score']:.1f}/{inc3['state']} vs "
          f"{inc4['score']:.1f}/{inc4['state']}): {'OK' if ok else 'FAIL'}")

    # (c) De realistische volgorde: het toestel wijkt eerst af en vliegt de
    #     polygoon pás daarna in — main.py meet de polygonen immers tegen de
    #     HUIDIGE positie. Vóór de fix: 88/BEVESTIGD zonder weather_explains.
    mgr, _inc, _src, t = run("be5", [VERT, LAT, VERT], lat=40.0, lon=0.0)
    mgr, inc, src, _t = run("be5", [LAT] * 3, mgr=mgr, t=t)
    ok = "weather_explains" in src and inc["state"] != incidents_module.CONFIRMED
    all_ok = all_ok and ok
    print(f"  toestel dat de hazard pas later invliegt krijgt de verklaring alsnog "
          f"(score {inc['score']:.0f}, state {inc['state']}): {'OK' if ok else 'FAIL'}")

    # (d) Aftrek-regressie: bevinding 6 mag niet terugdraaien. Slecht weer is
    #     juist de meest voorkomende oorzaak van een ECHTE diversie, dus een
    #     waargenomen landing elders wordt er niet door wegverklaard.
    _m, inc, src, _t = run("be6", [LAT] * 3 + [("wrong_airport", "BEVESTIGD")])
    ok = inc["state"] == incidents_module.CONFIRMED and "weather_explains" in src
    all_ok = all_ok and ok
    print(f"  een waargenomen landing elders binnen dezelfde polygoon houdt zijn score en blijft "
          f"BEVESTIGD (score {inc['score']:.0f}, state {inc['state']}): {'OK' if ok else 'FAIL'}")

    # (e) Bewuste versoepeling: de oude harde blokkade gooide alles op één hoop.
    #     Weer verklaart eromheen vliegen en wachten — niet laag verdwijnen bij
    #     een andere luchthaven terwijl je daalt.
    #
    #     signal_lost_near_airport wordt hier met ac=None gevoerd, precies zoals
    #     main.py's tier1_loop doet: dat Event ontstaat in de lus over toestellen
    #     die NIET in de snapshot zitten, dus er is per definitie geen live
    #     positie. Een live ac op diezelfde cyclus zou de gevolgtrekking meteen
    #     weerleggen (en dat doet de engine dan ook, terecht — zie bevinding 12
    #     en check_evidence_refutation). Om diezelfde reden komt er ná het
    #     verdwijnen geen nieuw lateraal/verticaal bewijs meer: die detectoren
    #     hebben live posities nodig.
    _m, inc, src, t = run("be7", [LAT, VERT])
    _step(_m, "be7", "signal_lost_near_airport", t, HAZ, 52.0, 0.0, ac=None)
    inc, src = _m._open["be7"], _m._evidence_sources_seen(_m._open["be7"]["id"])
    dims = _m._active_dimensions(inc["id"])
    ok = "weather_explains" in src and inc["state"] == incidents_module.CONFIRMED
    all_ok = all_ok and ok
    print(f"  weer verklaart eromheen vliegen en wachten, niet laag verdwijnen bij een andere "
          f"luchthaven -> lateraal+verticaal+verdwenen haalt wél BEVESTIGD "
          f"(score {inc['score']:.0f}, state {inc['state']}, dims={sorted(dims)}): {'OK' if ok else 'FAIL'}")

    return all_ok


def check_evidence_refutation(airport_db: AirportDB) -> bool:
    """CHECKPOINT.md bevinding 12. Verval zegt "we hebben al een tijd niets
    gehoord"; weerlegging zegt "we hebben nu iets gehoord dat de eerdere
    gevolgtrekking onmogelijk maakt". Alleen het tweede hoort een dimensie uit
    de BEVESTIGD-poort te halen, en tot deze ronde kende dit systeem alleen het
    eerste: _confirmed_bar_met stelde een vraag in de tegenwoordige tijd op een
    bronnenverzameling die alleen groeit.

    signal_lost_near_airport is de enige bewijsbron die uit AFWEZIGHEID van data
    redeneert. Wordt hetzelfde toestel daarna gewoon weer gevolgd, dan is die
    gevolgtrekking niet zwakker geworden maar weerlegd: het zat in een
    dekkingsgat."""
    import classify
    import db as db_module
    import incidents as incidents_module
    from detector import Event

    print("\n=== weerlegging van bewijs ===")
    all_ok = True
    cfg = dict(CONFIG)
    AIRBORNE = {"squawk": "1200", "lat": 52.0, "lon": 0.0, "alt": 35000, "track": 270.0}
    LAT, LOST = "corridor_deviation", "signal_lost_near_airport"

    def ev(hex_id, et):
        return Event(hex=hex_id, callsign="TSTS", event_type=et, confidence="MOGELIJK",
                     message="m", origin_icao="EHAM", dest_icao="EGLL", route_corroborated=True)

    def feed(mgr, hex_id, ets, ac, t):
        for et in ets:
            mgr.step(hex_id, "TSTS", classify.AIRLINER, [ev(hex_id, et)] if et else [], ac, t)
            t += 60.0
        return t

    mgr = incidents_module.IncidentManager(db_module.connect(":memory:"), cfg, airport_db)
    t = feed(mgr, "rf1", [LOST] + [LAT] * 3, None, 1000.0)
    inc = mgr._open["rf1"]
    dims = mgr._active_dimensions(inc["id"])
    ok = (inc["state"] == incidents_module.CONFIRMED
          and dims == {incidents_module.DIM_LATERAL, incidents_module.DIM_VANISHED})
    all_ok = all_ok and ok
    print(f"  verdwenen + laterale afwijking -> BEVESTIGD (twee dimensies) "
          f"(score {inc['score']:.0f}, dims={sorted(dims)}): {'OK' if ok else 'FAIL'}")

    t = feed(mgr, "rf1", [None], AIRBORNE, t)
    dims = mgr._active_dimensions(inc["id"])
    ok = ("signal_lost_refuted" in mgr._evidence_sources_seen(inc["id"])
          and incidents_module.DIM_VANISHED not in dims
          and inc["state"] != incidents_module.CONFIRMED)
    all_ok = all_ok and ok
    print(f"  toestel wordt weer gevolgd -> de afgeleide landing is weerlegd, niet slechts vervallen "
          f"(score {inc['score']:.1f}, state {inc['state']}, dims={sorted(dims)}): {'OK' if ok else 'FAIL'}")

    t = feed(mgr, "rf1", [LAT] * 3, AIRBORNE, t)
    dims = mgr._active_dimensions(inc["id"])
    ok = inc["state"] != incidents_module.CONFIRMED and dims == {incidents_module.DIM_LATERAL}
    all_ok = all_ok and ok
    print(f"  nieuw lateraal bewijs na de weerlegging tilt het incident niet terug naar BEVESTIGD "
          f"(score {inc['score']:.0f}, state {inc['state']}): {'OK' if ok else 'FAIL'}")

    t = feed(mgr, "rf1", [LOST], None, t)
    dims = mgr._active_dimensions(inc["id"])
    ok = incidents_module.DIM_VANISHED in dims and inc["state"] == incidents_module.CONFIRMED
    all_ok = all_ok and ok
    print(f"  opnieuw verdwijnen laat de dimensie weer meetellen — geen enkele weerlegging schakelt "
          f"hem permanent uit (score {inc['score']:.0f}, state {inc['state']}): {'OK' if ok else 'FAIL'}")

    # Regressie: verdwenen blijven is géén weerlegging.
    mgr2 = incidents_module.IncidentManager(db_module.connect(":memory:"), cfg, airport_db)
    feed(mgr2, "rf2", [LOST] + [LAT] * 3, None, 1000.0)
    inc2 = mgr2._open["rf2"]
    ok = (inc2["state"] == incidents_module.CONFIRMED
          and incidents_module.DIM_VANISHED in mgr2._active_dimensions(inc2["id"]))
    all_ok = all_ok and ok
    print(f"  een toestel dat verdwenen BLIJFT houdt zijn bewijs onverkort "
          f"(score {inc2['score']:.0f}, state {inc2['state']}): {'OK' if ok else 'FAIL'}")

    # Regressie: terugkomen aan de grond is de veronderstelde landing zelf.
    mgr3 = incidents_module.IncidentManager(db_module.connect(":memory:"), cfg, airport_db)
    t3 = feed(mgr3, "rf3", [LOST], None, 1000.0)
    feed(mgr3, "rf3", [None], {"squawk": "1200", "on_ground": True, "lat": 52.0, "lon": 0.0}, t3)
    inc3 = mgr3._open.get("rf3")
    ok = inc3 is not None and "signal_lost_refuted" not in mgr3._evidence_sources_seen(inc3["id"])
    all_ok = all_ok and ok
    print(f"  terugkomen AAN DE GROND weerlegt niets — dat is juist de veronderstelde landing: "
          f"{'OK' if ok else 'FAIL'}")

    return all_ok


def check_retention() -> bool:
    """CHECKPOINT.md bevinding 17. Geen enkele tabel had een bovengrens terwijl
    geen enkele consument verder terugkijkt dan 24 uur (server.py's
    FEED_WINDOW_SECONDS/INCIDENT_FEED_WINDOW_SECONDS), en de schrijfkant logde
    een onveranderde toestand elke cyclus opnieuw — live gemeten: N81051 stond
    26x in `events` binnen 226 seconden.

    Daarnaast verzamelde `learned_routes` rijen met origin == destination
    (circuitvluchten/touch-and-go's van GA-toestellen), die per definitie geen
    route kunnen zijn en wél als eigen grondwaarheid worden teruggegeven aan
    detect_landed_wrong_airport om een uitwijking te ONDERDRUKKEN."""
    import db as db_module
    from detector import Event

    print("\n=== retentie / begrensde groei ===")
    all_ok = True
    now = 1_000_000.0
    day = 86400.0
    conn = db_module.connect(":memory:")

    # --- events: buiten het venster weg, binnen het venster blijft ---
    for age_days, hex_id in ((10, "old1"), (1, "new1")):
        db_module.save_event(conn, Event(hex=hex_id, callsign="T", event_type="course_deviation",
                                          confidence="MOGELIJK", message="m"), ts=now - age_days * day)
    db_module.prune(conn, now=now, event_days=7)
    rows = {r[0] for r in conn.execute("SELECT hex FROM events")}
    ok = rows == {"new1"}
    all_ok = all_ok and ok
    print(f"  events ouder dan het venster verwijderd, recente behouden ({rows}): {'OK' if ok else 'FAIL'}")

    # --- open incidenten worden NOOIT verwijderd, hoe oud ook ---
    open_id = db_module.create_incident(conn, "openx", "T", "MOGELIJK", 30.0, now - 400 * day)
    db_module.add_incident_evidence(conn, open_id, now - 400 * day, "corridor_deviation", 30.0, "d", None)
    closed_id = db_module.create_incident(conn, "closedx", "T", "GESLOTEN_VALS_ALARM", 10.0, now - 400 * day)
    db_module.add_incident_evidence(conn, closed_id, now - 400 * day, "corridor_deviation", 10.0, "d", None)
    db_module.update_incident(conn, closed_id, resolved_ts=now - 400 * day)
    db_module.prune(conn, now=now, incident_days=30)
    remaining = {r[0] for r in conn.execute("SELECT hex FROM incidents")}
    ok = remaining == {"openx"}
    all_ok = all_ok and ok
    print(f"  open incident van 400 dagen oud blijft, afgesloten incident weg ({remaining}): "
          f"{'OK' if ok else 'FAIL'}")
    ev_ids = {r[0] for r in conn.execute("SELECT DISTINCT incident_id FROM incident_evidence")}
    ok = ev_ids == {open_id}
    all_ok = all_ok and ok
    print(f"  de evidence van het verwijderde incident ging mee, die van het open incident niet: "
          f"{'OK' if ok else 'FAIL'}")

    # --- learned_routes ---
    db_module.record_route_observation(conn, "SELF1", "KIWA", "KIWA", ts=now)
    ok = conn.execute("SELECT COUNT(*) FROM learned_routes WHERE callsign='SELF1'").fetchone()[0] == 0
    all_ok = all_ok and ok
    print(f"  record_route_observation weigert origin == destination (circuitvlucht): "
          f"{'OK' if ok else 'FAIL'}")

    # Zoals ze er live al in stonden, vóór die weigering bestond.
    conn.execute("INSERT INTO learned_routes (callsign, origin_icao, destination_icao, times_seen, last_seen) "
                 "VALUES ('N7041X','KIWA','KIWA',3,?)", (now,))
    db_module.record_route_observation(conn, "STALE1", "EHAM", "EGLL", ts=now - 400 * day)
    db_module.record_route_observation(conn, "FRESH1", "EHAM", "EGLL", ts=now - day)
    db_module.prune(conn, now=now, learned_route_days=180)
    left = {r[0] for r in conn.execute("SELECT callsign FROM learned_routes")}
    ok = left == {"FRESH1"}
    all_ok = all_ok and ok
    print(f"  bestaande origin==destination-rijen en verouderde routes opgeruimd ({left}): "
          f"{'OK' if ok else 'FAIL'}")

    # --- schrijfkant: dezelfde onveranderde toestand vouwt samen ---
    conn2 = db_module.connect(":memory:")
    ev = Event(hex="spam1", callsign="N81051", event_type="emergency",
               confidence="BEVESTIGD", message="squawk 7600", squawk="7600")
    for i in range(30):  # 30 tier0-cycli van 15s = 450s, binnen EVENT_DEDUPE_SECONDS
        db_module.save_event(conn2, ev, ts=now + i * 15.0)
    n = conn2.execute("SELECT COUNT(*) FROM events WHERE hex='spam1'").fetchone()[0]
    ok = n == 1
    all_ok = all_ok and ok
    print(f"  30 identieke tier0-hits binnen het dedupe-venster -> {n} rij (was 30): "
          f"{'OK' if ok else 'FAIL'}")

    # ...maar een gewijzigde confidence is wél nieuwe informatie.
    ev2 = Event(hex="spam1", callsign="N81051", event_type="emergency",
                confidence="WAARSCHIJNLIJK", message="squawk 7600", squawk="7600")
    db_module.save_event(conn2, ev2, ts=now + 60.0)
    n = conn2.execute("SELECT COUNT(*) FROM events WHERE hex='spam1'").fetchone()[0]
    ok = n == 2
    all_ok = all_ok and ok
    print(f"  een gewijzigde confidence schrijft wél een nieuwe rij ({n}): {'OK' if ok else 'FAIL'}")

    # --- prune/checkpoint op een lege database ---
    try:
        db_module.prune(db_module.connect(":memory:"))
        db_module.checkpoint(conn2)
        ok = True
    except Exception as e:
        ok = False
        print(f"    {e}")
    all_ok = all_ok and ok
    print(f"  prune/checkpoint op een lege database gooit niets: {'OK' if ok else 'FAIL'}")

    return all_ok


def check_route_corroboration_discriminates(airport_db: AirportDB) -> bool:
    """CHECKPOINT.md bevinding 16 — de bevinding achter "bijna alle valse
    positieven komen uit het routesysteem".

    `route_corroborated_by_progress` eiste alleen dat het toestel 30% dichter
    bij de gefilede bestemming was gekomen, met als motivering dat een verkeerd
    gematchte schedule dat "vrijwel nooit" haalt. Nagerekend klopt dat niet:
    vliegt het toestel in werkelijkheid naar A terwijl D gefiled staat, en ligt
    A onder hoek theta van D gezien vanaf het vertrekpunt, dan komt het tot op
    L*sin(theta) van D — dus corroboreerde ELKE hoekfout tot 44 graden.

    Erger nog is de VOLGORDE: route_plausible staat ervoor en filtert de wild
    verkeerde routes al weg, dus deze toets werd uitsluitend toegepast op
    precies de verzameling die hij niet kan beoordelen.

    Dit is bewust een METING en geen puntcontrole: hij rekent het raster van
    routelengtes x hoekfouten door en telt hoeveel combinaties tegelijk (i)
    corridor_deviation laten vuren en (ii) de route gecorroboreerd verklaren —
    een volledig vals-positiefpad. Faalt zodra dat aantal boven nul komt."""
    import math

    from airports import (cross_track_distance_nm, haversine_nm,
                          route_corroborated_by_progress, _intermediate_point)

    print("\n=== route-corroboratie onderscheidt foute routes (rastermeting) ===")
    R_NM = 3440.065

    def dest_point(lat, lon, brg, dist_nm):
        br, d = math.radians(brg), dist_nm / R_NM
        p1, l1 = math.radians(lat), math.radians(lon)
        p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(br))
        l2 = l1 + math.atan2(math.sin(br) * math.sin(d) * math.cos(p1),
                             math.cos(d) - math.sin(p1) * math.sin(p2))
        return math.degrees(p2), math.degrees(l2)

    cfg = CONFIG
    origin = (50.0, 5.0)
    false_positive_paths = []
    fires_total = 0
    for route_len in (400, 900, 2000, 5000):
        threshold = max(cfg["corridor_deviation_min_nm"],
                        min(cfg["corridor_deviation_max_nm"], route_len * cfg["corridor_deviation_pct"]))
        buffer_nm = max(30.0, min(150.0, route_len * 0.05))
        for theta in (5, 10, 15, 20, 30, 40, 45):
            filed = dest_point(origin[0], origin[1], 90.0, route_len)
            actual = dest_point(origin[0], origin[1], 90.0 + theta, route_len)
            fires = corroborated = False
            for i in range(1, 301):
                cur = _intermediate_point(origin[0], origin[1], actual[0], actual[1], i / 300.0)
                xtd = cross_track_distance_nm(origin[0], origin[1], filed[0], filed[1], cur[0], cur[1])
                d_from_origin = haversine_nm(origin[0], origin[1], cur[0], cur[1])
                d_to_dest = haversine_nm(cur[0], cur[1], filed[0], filed[1])
                if d_from_origin >= buffer_nm and d_to_dest >= buffer_nm and xtd >= threshold:
                    fires = True
                if route_corroborated_by_progress(origin[0], origin[1], filed[0], filed[1], cur[0], cur[1]):
                    corroborated = True
            if fires:
                fires_total += 1
            if fires and corroborated:
                false_positive_paths.append((route_len, theta))

    ok = not false_positive_paths
    print(f"  {fires_total}/28 combinaties laten corridor_deviation vuren op een FOUTE route")
    print(f"  daarvan verklaren er {len(false_positive_paths)} de route óók nog gecorroboreerd "
          f"(= volledig vals-positiefpad; was 21 vóór bevinding 16): "
          f"{'OK' if ok else 'FAIL ' + str(false_positive_paths)}")

    # De andere kant op: de toets mag niet zó streng zijn dat niets meer
    # corroboreert. Een toestel dat de gefilede route daadwerkelijk aflegt moet
    # hem wél bevestigen — anders is de poort niet streng maar kapot.
    filed = dest_point(origin[0], origin[1], 90.0, 2000)
    on_route_ok = any(
        route_corroborated_by_progress(origin[0], origin[1], filed[0], filed[1], *cur)
        for cur in (_intermediate_point(origin[0], origin[1], filed[0], filed[1], f / 20.0)
                    for f in range(1, 20))
    )
    print(f"  een toestel dat de gefilede route wél aflegt corroboreert nog steeds: "
          f"{'OK' if on_route_ok else 'FAIL'}")

    return ok and on_route_ok


def check_unverified_route_geometry_cap(airport_db: AirportDB) -> bool:
    """CHECKPOINT.md bevinding 16, tweede helft. Bevinding 4 stelde vast dat
    vrijwel elke detector tegen dezelfde track.route["destination_*"] meet, en
    dat meerdere detectoren die tegelijk afgaan dus één fout is die meerdere
    keren wordt waargenomen. Die conclusie is toen alleen in de BEVESTIGD-poort
    verwerkt; de SCORE telde ze daarna nog gewoon op (corridor_deviation 55 +
    premature_descent 60 = 115).

    Dat is niet alleen een BEVESTIGD-probleem: _maybe_notify vuurt op
    WAARSCHIJNLIJK (55), en corridor_deviation haalt dat plafond in zijn eentje.
    Elke eerdere ronde verdedigde het aanscherpen van de poort met "dat kost
    vrijwel geen meldingen" — maar de klacht van de gebruiker gaat juist over
    meldingen, en aan die kant was nog nooit iets aangescherpt."""
    import classify
    import db as db_module
    import incidents as incidents_module
    from detector import Event

    print("\n=== gedeeld plafond op onbevestigde route-meetkunde ===")
    all_ok = True
    cfg = dict(CONFIG)
    likely = cfg["incident_score_likely_threshold"]

    def replay(hex_id, corroborated, cycles=30):
        mgr = incidents_module.IncidentManager(db_module.connect(":memory:"), cfg, airport_db)
        t = 1000.0
        for i in range(cycles):
            for et in ("corridor_deviation", "premature_descent", "holding_pattern"):
                mgr.step(hex_id, "TSTG", classify.AIRLINER, [Event(
                    hex=hex_id, callsign="TSTG", event_type=et, confidence="MOGELIJK",
                    message=f"m{i}", origin_icao="EDDF", dest_icao="LEMG",
                    at_destination=False, route_corroborated=corroborated)], {"squawk": "1200"}, t)
            t += 60.0
        inc = mgr._open[hex_id]
        return inc["score"], inc["state"]

    # Het EDDF->LEPA/LEMG-scenario uit bevinding 16 punt 2: drie
    # route-meetkundige detectoren die alle drie blijven vuren omdat de gefilede
    # bestemming fout is. Vóór deze fix: 115+ punten -> BEVESTIGD + Telegram.
    score, state = replay("geo1", corroborated=False)
    ok = score <= likely - 1 and state == incidents_module.POSSIBLE
    all_ok = all_ok and ok
    print(f"  3 route-meetkundige detectoren, 30 cycli, ONbevestigde route: score {score:.0f} "
          f"(plafond {likely - 1:.0f}), state {state} — geen notificatie: {'OK' if ok else 'FAIL'}")

    # Regressiebewaking de andere kant op: met een bevestigde route valt het
    # gedeelde plafond weg en gedraagt de score zich als voorheen.
    score, state = replay("geo2", corroborated=True)
    ok = score > likely and state in (incidents_module.LIKELY, incidents_module.CONFIRMED)
    all_ok = all_ok and ok
    print(f"  dezelfde drie detectoren op een BEVESTIGDE route: score {score:.0f}, state {state} "
          f"— plafond valt weg: {'OK' if ok else 'FAIL'}")

    # Grondbewijs valt buiten het plafond: een waargenomen landing elders is
    # geen meetkunde tegen een onbevestigde lijn, en moet ook op een
    # ongecorroboreerde route nog steeds kunnen notificeren.
    mgr = incidents_module.IncidentManager(db_module.connect(":memory:"), cfg, airport_db)
    mgr.step("geo3", "TSTG", classify.AIRLINER, [Event(
        hex="geo3", callsign="TSTG", event_type="wrong_airport", confidence="BEVESTIGD",
        message="geland elders", origin_icao="EDDF", dest_icao="LEMG",
        observed_icao="EDDM", route_corroborated=False)], {"squawk": "1200"}, 1000.0)
    inc = mgr._open.get("geo3")
    state = inc["state"] if inc else "GESLOTEN"
    ok = state in (incidents_module.LIKELY, incidents_module.CONFIRMED, "GESLOTEN")
    all_ok = all_ok and ok
    print(f"  grondbewijs (wrong_airport) op een ongecorroboreerde route valt buiten het plafond: "
          f"state {state}: {'OK' if ok else 'FAIL'}")

    return all_ok


def check_emergency_semantics(airport_db: AirportDB) -> bool:
    """CHECKPOINT.md bevinding 15. Dit systeem is een UITWIJKdetector, maar de
    emergency-tak beantwoordde een andere vraag: "is er iets mis?". Twee gaten:

      1. Het ADS-B emergency-STATUSVELD leverde een Event op voor ELKE
         niet-'none' waarde. Drie van de zeven DO-260B-waarden voorspellen
         operationeel juist het TEGENDEEL van een uitwijking ('lifeguard' is een
         geplande voorrangsstatus van een ambulancevlucht, 'minfuel' en 'nordo'
         betekenen allebei "ik wil ZONDER omweg naar mijn bestemming"), en
         'downed' is onmogelijk voor een toestel dat op dat moment een positie
         op hoogte uitzendt. De gebruiker rapporteerde precies deze waarden als
         de meldingen die nooit echt bleken.
      2. De drie noodsquawks werden als één signaal behandeld: één sample van
         7500 of 7600 gaf 100 punten en dus onmiddellijk BEVESTIGD, terwijl de
         voorgeschreven lost-comms-procedure (ICAO Annex 2 / 14 CFR 91.185)
         juist DOORVLIEGEN naar de gefilede bestemming is, en een kortstondige
         7500 vrijwel altijd doordraaien tijdens het instellen van een andere
         code is."""
    import classify
    import db as db_module
    import incidents as incidents_module
    from detector import Event, detect_emergency
    from state import AircraftTrack

    print("\n=== emergency-semantiek (statusveld + noodsquawks) ===")
    all_ok = True
    cfg = dict(CONFIG)

    # --- 1. het statusveld op BETEKENIS ---
    track = AircraftTrack(hex="abc123", callsign="TSTX")
    for status, want_event in (
        ("lifeguard", False), ("minfuel", False), ("nordo", False), ("downed", False),
        ("none", False), ("general", True), ("unlawful", True),
    ):
        ev = detect_emergency(track, {"squawk": "2451", "emergency": status,
                                       "lat": 50.0, "lon": 5.0, "alt_baro": 35000})
        ok = (ev is not None) == want_event
        all_ok = all_ok and ok
        print(f"  emergency-status '{status}': {'Event' if ev else 'geen Event'} "
              f"(verwacht {'Event' if want_event else 'geen Event'}): {'OK' if ok else 'FAIL'}")

    # --- 2. de drie noodsquawks los van elkaar ---
    def run(hex_id, squawk, cycles):
        mgr = incidents_module.IncidentManager(db_module.connect(":memory:"), cfg, airport_db)
        t = 1000.0
        for i in range(cycles):
            ev = Event(hex=hex_id, callsign="TSTX", event_type="emergency",
                       confidence="BEVESTIGD", message=f"m{i}", squawk=squawk)
            mgr.step(hex_id, "TSTX", classify.AIRLINER, [ev], {"squawk": squawk}, t)
            t += 15.0
        inc = mgr._open[hex_id]
        return inc["score"], inc["state"]

    for squawk, cycles, want_state, why in (
        ("7700", 1, incidents_module.CONFIRMED, "algemene noodsituatie: ongewijzigd direct BEVESTIGD"),
        ("7600", 1, incidents_module.POSSIBLE, "radiostoring: zichtbaar, niet bevestigd"),
        ("7600", 30, incidents_module.POSSIBLE, "radiostoring, volgehouden: nog steeds geen notificatie"),
        ("7500", 1, incidents_module.WATCHING, "kaping, één sample: nog onzichtbaar (doordraai-artefact)"),
        ("7500", 2, incidents_module.CONFIRMED, "kaping, volgehouden: BEVESTIGD"),
    ):
        score, state = run(f"sq{squawk}{cycles}", squawk, cycles)
        ok = state == want_state
        all_ok = all_ok and ok
        print(f"  squawk {squawk} x{cycles}: score {score:.0f}, state {state} "
              f"(verwacht {want_state}) — {why}: {'OK' if ok else 'FAIL'}")

    # --- 3. 7600 blokkeert een ECHTE uitwijking niet ---
    # Een radiostoring die daadwerkelijk elders landt hoort gewoon BEVESTIGD te
    # worden — via DIM_GROUND_TRUTH, niet via de squawk. Regressiebewaking dat
    # het verlagen van 7600 geen echte detectie kost.
    mgr = incidents_module.IncidentManager(db_module.connect(":memory:"), cfg, airport_db)
    t = 1000.0
    mgr.step("mix1", "TSTX", classify.AIRLINER, [Event(
        hex="mix1", callsign="TSTX", event_type="emergency", confidence="BEVESTIGD",
        message="7600", squawk="7600")], {"squawk": "7600"}, t)
    t += 60.0
    mgr.step("mix1", "TSTX", classify.AIRLINER, [Event(
        hex="mix1", callsign="TSTX", event_type="wrong_airport", confidence="BEVESTIGD",
        message="geland op EDDM", origin_icao="EDDF", dest_icao="LEMG",
        observed_icao="EDDM", route_corroborated=True)], None, t)
    inc = mgr._open.get("mix1")
    state = inc["state"] if inc else "GESLOTEN"
    ok = state in (incidents_module.CONFIRMED, "GESLOTEN")
    all_ok = all_ok and ok
    print(f"  7600 + waargenomen landing elders (gecorroboreerde route): state {state} "
          f"— echte uitwijking blijft BEVESTIGD: {'OK' if ok else 'FAIL'}")

    return all_ok


def main():
    from backtest_cases import CASES
    airport_db = AirportDB()
    results = [report_case(c, airport_db) for c in CASES]
    bad_route_ok = check_bad_route_regressions()
    holding_suppression_ok = check_course_deviation_holding_suppression()
    emergency_status_ok = check_emergency_status_regressions()
    emergency_semantics_ok = check_emergency_semantics(airport_db)
    bow_suppression_ok = check_corridor_deviation_bow_suppression()
    signal_lost_origin_ok = check_signal_lost_origin_suppression(airport_db)
    holding_origin_gating_ok = check_holding_pattern_origin_gating(airport_db)
    holding_same_metro_ok = check_holding_pattern_same_metro(airport_db)
    classification_ok = check_classification_regressions()
    route_widening_ok = check_route_widening_regressions()
    wrong_airport_crosscheck_ok = check_wrong_airport_route_crosscheck()
    wrong_airport_weight_ok = check_wrong_airport_evidence_weight(airport_db)
    pd_sl_crosscheck_ok = check_premature_descent_signal_lost_route_crosscheck(airport_db)
    saturation_caps_ok = check_saturation_caps(airport_db)
    route_corroboration_ok = check_route_corroboration(airport_db)
    route_discriminates_ok = check_route_corroboration_discriminates(airport_db)
    geometry_cap_ok = check_unverified_route_geometry_cap(airport_db)
    retention_ok = check_retention()
    confirmed_bar_ok = check_confirmed_bar_structure(airport_db)
    incident_engine_ok = check_incident_engine_regressions(airport_db)
    incident_engine_real_case_ok = check_incident_engine_real_case_escalation(airport_db)
    incident_engine_recovery_ok = check_incident_engine_real_case_recovery(airport_db)
    real_case_outcomes_ok = check_real_case_confidence_outcomes(airport_db)
    airspace_ok = check_airspace_regressions(airport_db)
    disputed_gate_ok = check_disputed_evidence_gate(airport_db)
    benign_scope_ok = check_benign_explanation_scope(airport_db)
    refutation_ok = check_evidence_refutation(airport_db)

    print("\n=== summary ===")
    hits = sum(1 for r in results if r["detected"])
    print(f"{hits}/{len(results)} cases detected the expected event type")
    for r in results:
        if r["detected"]:
            print(f"  {r['case']}: lead {fmt_t(r['lead_seconds'])}")
        else:
            print(f"  {r['case']}: MISSED")
    print(f"bad-route regression checks: {'all OK' if bad_route_ok else 'FAIL — see above'}")
    print(f"course_deviation holding-pattern suppression: {'OK' if holding_suppression_ok else 'FAIL — see above'}")
    print(f"emergency-status normalization: {'OK' if emergency_status_ok else 'FAIL — see above'}")
    print(f"emergency-semantiek (statusveld + noodsquawks): {'OK' if emergency_semantics_ok else 'FAIL — see above'}")
    print(f"corridor_deviation bow-tolerance suppression: {'OK' if bow_suppression_ok else 'FAIL — see above'}")
    print(f"signal_lost_near_airport origin-suppression: {'OK' if signal_lost_origin_ok else 'FAIL — see above'}")
    print(f"holding_pattern origin-streak gating: {'OK' if holding_origin_gating_ok else 'FAIL — see above'}")
    print(f"holding_pattern same-metro-area gating: {'OK' if holding_same_metro_ok else 'FAIL — see above'}")
    print(f"aircraft classification: {'OK' if classification_ok else 'FAIL — see above'}")
    print(f"route widening (hexdb.io + negative-retry): {'OK' if route_widening_ok else 'FAIL — see above'}")
    print(f"wrong_airport route-source cross-check: {'OK' if wrong_airport_crosscheck_ok else 'FAIL — see above'}")
    print(f"wrong_airport evidence weight: {'OK' if wrong_airport_weight_ok else 'FAIL — see above'}")
    print(f"premature_descent/signal_lost route-source cross-check: {'OK' if pd_sl_crosscheck_ok else 'FAIL — see above'}")
    print(f"verzadigingsplafonds per bewijsbron: {'OK' if saturation_caps_ok else 'FAIL — see above'}")
    print(f"route-corroboratie: {'OK' if route_corroboration_ok else 'FAIL — see above'}")
    print(f"route-corroboratie onderscheidt foute routes: {'OK' if route_discriminates_ok else 'FAIL — see above'}")
    print(f"gedeeld plafond op onbevestigde route-meetkunde: {'OK' if geometry_cap_ok else 'FAIL — see above'}")
    print(f"retentie / begrensde groei: {'OK' if retention_ok else 'FAIL — see above'}")
    print(f"structuur van de BEVESTIGD-poort: {'OK' if confirmed_bar_ok else 'FAIL — see above'}")
    print(f"incident engine: {'OK' if incident_engine_ok else 'FAIL — see above'}")
    print(f"incident engine (real-case escalation, AI850): {'OK' if incident_engine_real_case_ok else 'FAIL — see above'}")
    print(f"incident engine (real-case recovery, DAL2778): {'OK' if incident_engine_recovery_ok else 'FAIL — see above'}")
    print(f"zekerheidsuitkomst per echte case: {'OK' if real_case_outcomes_ok else 'FAIL — see above'}")
    print(f"airspace (weather + peer-consensus): {'OK' if airspace_ok else 'FAIL — see above'}")
    print(f"betwist bewijs / afsluitlabel: {'OK' if disputed_gate_ok else 'FAIL — see above'}")
    print(f"reikwijdte benigne verklaring: {'OK' if benign_scope_ok else 'FAIL — see above'}")
    print(f"weerlegging van bewijs: {'OK' if refutation_ok else 'FAIL — see above'}")


if __name__ == "__main__":
    main()
