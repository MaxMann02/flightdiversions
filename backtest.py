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

from airports import AirportDB, ROUTE_REVALIDATION_WINDOW_S, bearing_deg, route_plausible
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
            track.last_takeoff_ts = s.t

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
            fired.append((last_t + cfg["signal_lost_missing_cycles"] * cfg["tier1_interval_seconds"], ev))

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

    # peak_score reached BEVESTIGD before the incident eventually went idle
    # (no more holding evidence once the aircraft leaves the hold) and
    # timed out — state itself is GESLOTEN_TIMEOUT by the time we read it
    # back, peak_score is what shows it actually escalated all the way.
    ok = (hold_incident["peak_score"] >= cfg["incident_score_confirmed_threshold"]
          and hold_incident["state"] == "GESLOTEN_TIMEOUT"
          and hold_incident["resolution_reason"] and "eerdere escalatie" in hold_incident["resolution_reason"])
    all_ok = all_ok and ok
    print(f"  hold-driven incident escalates all the way to BEVESTIGD (peak_score={hold_incident['peak_score']:.0f}), "
          f"then times out honestly (state={hold_incident['state']}, reason={hold_incident['resolution_reason']!r}): "
          f"{'OK' if ok else 'FAIL'}")

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
    # should hold at the INCIDENT level too: BEVESTIGD reached well before
    # AI850's real MAYDAY declaration (t=189min).
    # Find the ts of the evidence row whose cumulative score first reached
    # the CONFIRMED threshold — reconstruct cumulative score from deltas.
    cum = 0.0
    confirmed_ts = None
    for row in db_module.get_incident_evidence(conn, hold_incident["id"]):
        cum += row["delta"]
        if cum >= cfg["incident_score_confirmed_threshold"]:
            confirmed_ts = row["ts"]
            break
    real_mayday_t = 189 * 60.0
    ok = confirmed_ts is not None and confirmed_ts < real_mayday_t
    all_ok = all_ok and ok
    lead_min = (real_mayday_t - confirmed_ts) / 60.0 if confirmed_ts is not None else None
    print(f"  incident reaches BEVESTIGD well before the real MAYDAY declaration "
          f"({fmt_t(real_mayday_t - confirmed_ts) if confirmed_ts is not None else 'n/a'} early): "
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

    # Reassess should now apply peer_consensus evidence for a clustered
    # incident (3 total in-cell >= peer_consensus_min_aircraft default 3).
    mgr.reassess("peer_test_0", {"squawk": "1200"}, 5060.0)
    evidence_sources = {e["source"] for e in db_module.get_incident_evidence(conn, mgr._open["peer_test_0"]["id"])}
    ok = "peer_consensus" in evidence_sources
    all_ok = all_ok and ok
    print(f"  reassess() applies peer_consensus evidence for a clustered incident: {'OK' if ok else 'FAIL'}")

    return all_ok


def main():
    from backtest_cases import CASES
    airport_db = AirportDB()
    results = [report_case(c, airport_db) for c in CASES]
    bad_route_ok = check_bad_route_regressions()
    holding_suppression_ok = check_course_deviation_holding_suppression()
    emergency_status_ok = check_emergency_status_regressions()
    bow_suppression_ok = check_corridor_deviation_bow_suppression()
    signal_lost_origin_ok = check_signal_lost_origin_suppression(airport_db)
    classification_ok = check_classification_regressions()
    route_widening_ok = check_route_widening_regressions()
    incident_engine_ok = check_incident_engine_regressions(airport_db)
    incident_engine_real_case_ok = check_incident_engine_real_case_escalation(airport_db)
    airspace_ok = check_airspace_regressions(airport_db)

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
    print(f"corridor_deviation bow-tolerance suppression: {'OK' if bow_suppression_ok else 'FAIL — see above'}")
    print(f"signal_lost_near_airport origin-suppression: {'OK' if signal_lost_origin_ok else 'FAIL — see above'}")
    print(f"aircraft classification: {'OK' if classification_ok else 'FAIL — see above'}")
    print(f"route widening (hexdb.io + negative-retry): {'OK' if route_widening_ok else 'FAIL — see above'}")
    print(f"incident engine: {'OK' if incident_engine_ok else 'FAIL — see above'}")
    print(f"incident engine (real-case escalation, AI850): {'OK' if incident_engine_real_case_ok else 'FAIL — see above'}")
    print(f"airspace (weather + peer-consensus): {'OK' if airspace_ok else 'FAIL — see above'}")


if __name__ == "__main__":
    main()
