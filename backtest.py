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


def run_case(case: Case, airport_db: AirportDB, cfg: dict | None = None) -> list:
    """Replays a case's samples through the real detector functions in the
    same order/state-mutation sequence main.py's tier0/tier1 loops use.
    Returns a list of (t, Event) for every event the detectors raised —
    NOT de-duped by cooldown, since we want to see every raw detector hit,
    not just what would have cleared the alert cooldown."""
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

        ev = detect_emergency(track, ac)
        if ev:
            fired.append((s.t, ev))
        for ev in evaluate(track, ac, airport_db, cfg, db_conn=None):
            fired.append((s.t, ev))

        if just_took_off:
            track.last_takeoff_ts = s.t

        # Mirrors main.py's tier1_loop: was_on_ground only updates AFTER
        # evaluate() runs, so detect_landed_wrong_airport sees the PRIOR
        # cycle's ground state when it checks for a fresh landing.
        track.was_on_ground = ac["on_ground"]

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
    detector hit (course_deviation has no memory of "already alerted on
    this hold" the way alert_cooldown_seconds gates repeat DISPATCHES of
    an identical event_type — a hold with turns spaced further apart than
    the cooldown window would previously have kept clearing it and firing
    again, turn after turn, for as long as the hold lasted). See
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


def main():
    from backtest_cases import CASES
    airport_db = AirportDB()
    results = [report_case(c, airport_db) for c in CASES]
    bad_route_ok = check_bad_route_regressions()
    holding_suppression_ok = check_course_deviation_holding_suppression()

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


if __name__ == "__main__":
    main()
