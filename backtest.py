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

from airports import AirportDB, bearing_deg, route_plausible
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
            else:
                track.route = candidate
            track.route_checked = True

        # Mirrors main.py's tier1_loop: re-check plausibility every cycle
        # while airborne, using the relaxed (cross-track-only) check
        # main.py uses for an already-trusted route — see route_plausible's
        # check_progress docstring. Needed so a case like ATL->MCO->FLL
        # below (a genuine diversion that overflies its destination in a
        # near-straight line) actually exercises whether track.route
        # survives to the eventual landing, not just whether the geometry
        # detectors fire along the way. Resets route_checked on failure too
        # (mirrors main.py) so the block above gets a chance to re-resolve
        # (and, per its own strict check, re-reject) on the next cycle.
        if track.route and ac.get("lat") is not None and not ac["on_ground"]:
            if not route_plausible(
                track.route["origin_lat"], track.route["origin_lon"],
                track.route["destination_lat"], track.route["destination_lon"],
                ac["lat"], ac["lon"], check_progress=False,
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


def main():
    from backtest_cases import CASES
    airport_db = AirportDB()
    results = [report_case(c, airport_db) for c in CASES]

    print("\n=== summary ===")
    hits = sum(1 for r in results if r["detected"])
    print(f"{hits}/{len(results)} cases detected the expected event type")
    for r in results:
        if r["detected"]:
            print(f"  {r['case']}: lead {fmt_t(r['lead_seconds'])}")
        else:
            print(f"  {r['case']}: MISSED")


if __name__ == "__main__":
    main()
