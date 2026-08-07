from dataclasses import dataclass

import db as db_module
from airports import angle_diff_deg, bearing_deg, cross_track_distance_nm, haversine_nm
from providers import EMERGENCY_SQUAWKS
from state import AircraftTrack

SQUAWK_LABELS = {
    "7700": "algemeen noodsignaal (emergency)",
    "7600": "radiostoring (NORDO)",
    "7500": "kaping / unlawful interference",
}


@dataclass
class Event:
    hex: str
    callsign: str
    event_type: str  # emergency | wrong_airport | course_deviation | corridor_deviation | holding_pattern
    confidence: str  # "BEVESTIGD" | "WAARSCHIJNLIJK" | "MOGELIJK"
    message: str
    # ICAO to check weather for as corroborating context (the originally
    # planned destination, when known) — filled in by main.py's dispatch.
    weather_icao: str | None = None
    lat: float | None = None
    lon: float | None = None
    squawk: str | None = None
    alt: float | None = None
    origin_icao: str | None = None
    dest_icao: str | None = None
    # Structured flag so callers (incidents.py's scoring) don't need to
    # pattern-match ev.message text. Currently only set by
    # detect_holding_pattern (holding AT the planned destination is weaker
    # evidence than holding somewhere else — see its docstring).
    at_destination: bool = False


def _probable_destination_note(airport_db, lat: float, lon: float, heading: float) -> str:
    candidates = airport_db.candidates_ahead(lat, lon, heading)
    if not candidates:
        return ""
    names = ", ".join(f"{a['icao']} ({a['name']})" for a in candidates)
    return f" — mogelijke bestemming o.b.v. huidige koers: {names}"


def detect_emergency(track: AircraftTrack, ac: dict) -> Event | None:
    squawk = ac.get("squawk")
    emergency = ac.get("emergency")
    # Route may already be resolved from tier1 (shared TrackStore) even
    # though this detector doesn't depend on it — worth attaching if present.
    origin_icao = track.route["origin_icao"] if track.route else None
    dest_icao = track.route["destination_icao"] if track.route else None
    if squawk in EMERGENCY_SQUAWKS:
        reason = SQUAWK_LABELS.get(squawk, squawk)
        return Event(
            hex=track.hex, callsign=track.callsign, event_type="emergency",
            confidence="BEVESTIGD",
            message=f"squawk {squawk} — {reason}",
            lat=ac.get("lat"), lon=ac.get("lon"), squawk=squawk, alt=ac.get("alt_baro"),
            origin_icao=origin_icao, dest_icao=dest_icao,
        )
    if emergency and emergency != "none":
        return Event(
            hex=track.hex, callsign=track.callsign, event_type="emergency",
            confidence="BEVESTIGD",
            message=f"emergency status gemeld: {emergency}",
            lat=ac.get("lat"), lon=ac.get("lon"), squawk=squawk, alt=ac.get("alt_baro"),
            origin_icao=origin_icao, dest_icao=dest_icao,
        )
    return None


def detect_landed_wrong_airport(track: AircraftTrack, ac: dict, airport_db, cfg: dict, db_conn=None) -> Event | None:
    just_landed = ac.get("on_ground") and track.was_on_ground is False
    if not just_landed or not track.route or not track.history:
        return None

    landing_point = track.history[-1]
    nearest, dist_km = airport_db.nearest(landing_point.lat, landing_point.lon)
    if not nearest:
        return None

    dest_icao = track.route["destination_icao"]
    origin = track.route["origin_icao"]
    early_return_note = ""
    if nearest["icao"] == origin:
        # Live-tested: small regional/commuter operators (observed on Alaska
        # bush routes, callsigns like BRGxxx/AERxxx) reuse one callsign across
        # multiple legs per day. adsbdb's static mapping then reflects the
        # outbound leg while the aircraft is actually flying the return leg
        # back to its own departure point — a normal landing, not a diversion.
        #
        # BUT: if we personally watched THIS aircraft take off only minutes
        # ago, "landing back at origin" can't be a later, different leg —
        # there hasn't been time for one. It's a genuine early-return
        # diversion instead, which would otherwise be invisible: a low, early
        # turnback typically clears neither course_deviation's cruise-altitude
        # floor nor holding_pattern's streak requirement, and course_deviation
        # also only fires above a large turn-angle threshold. See EARLY_RETURN
        # config comment.
        time_since_takeoff_s = (
            landing_point.ts - track.last_takeoff_ts if track.last_takeoff_ts is not None else None
        )
        if time_since_takeoff_s is None or time_since_takeoff_s > cfg["early_return_max_minutes"] * 60:
            return None
        early_return_note = f", {int(time_since_takeoff_s / 60)}min na takeoff"
    if nearest["icao"] != dest_icao:
        if db_conn and track.callsign:
            # We may have personally watched this callsign land here before —
            # our own observed ground truth overrides adsbdb's static (and
            # sometimes stale/wrong-leg) mapping for multi-stop operators.
            learned = db_module.get_learned_destinations(db_conn, track.callsign, cfg["learned_route_min_times_seen"])
            if nearest["icao"] in learned:
                return None
        return Event(
            hex=track.hex, callsign=track.callsign, event_type="wrong_airport",
            confidence="BEVESTIGD",
            message=(
                f"geland op {nearest['icao']} ({nearest['name']}) i.p.v. verwachte "
                f"bestemming {dest_icao} (vlucht {origin} -> {dest_icao}){early_return_note}"
            ),
            weather_icao=dest_icao, lat=landing_point.lat, lon=landing_point.lon,
            squawk=ac.get("squawk"), alt=0, origin_icao=origin, dest_icao=dest_icao,
        )
    return None


PENDING_TIMEOUT_S = 240
PENDING_HOLD_TOLERANCE_DEG = 35


def _deviation_context(track: AircraftTrack, p_latest) -> str:
    if not track.route:
        return ""
    dest = track.route["destination_icao"]
    new_bearing_to_dest = bearing_deg(p_latest.lat, p_latest.lon, track.route["destination_lat"], track.route["destination_lon"])
    if angle_diff_deg(p_latest.track, new_bearing_to_dest) < 30:
        return f" (nieuwe koers wijst nu wél richting {dest})"
    return f" (nieuwe koers wijst niet richting oorspronkelijke bestemming {dest})"


def _rotation_and_radius(track: AircraftTrack, n: int) -> tuple[float, float] | None:
    """Total heading rotation (deg) and max centroid radius (nm) across the
    last n history points, or None if there isn't enough history / heading
    data yet. Shared by detect_holding_pattern (its primary signal) and
    detect_course_deviation (as a suppression check — see its use there for
    why a tight, high-rotation loiter needs to be told apart from a genuine
    diversion turn there too, not just in detect_holding_pattern itself)."""
    if len(track.history) < n:
        return None
    window = list(track.history)[-n:]
    if any(p.track is None for p in window):
        return None
    total_rotation = sum(angle_diff_deg(window[i].track, window[i + 1].track) for i in range(len(window) - 1))
    lat_c = sum(p.lat for p in window) / len(window)
    lon_c = sum(p.lon for p in window) / len(window)
    max_radius = max(haversine_nm(lat_c, lon_c, p.lat, p.lon) for p in window)
    return total_rotation, max_radius


def detect_course_deviation(track: AircraftTrack, ac: dict, cfg: dict, airport_db=None) -> Event | None:
    """Flags a SUDDEN, SUSTAINED change from the aircraft's own recent stable
    heading, rather than comparing to a straight-line bearing to the
    destination.

    Two live-tested iterations led here:
    1. Destination-bearing comparison false-positived constantly (KLAX->KSEA,
       KORD->PANC, etc.) because real airway routing legitimately zig-zags
       relative to a great-circle line for most of a flight.
    2. A single-sample "sudden heading change" (even restricted to >18,000ft,
       level flight) still false-positived on aircraft flying racetrack/orbit
       patterns (military tanker/ISR-style callsigns holding at a constant
       altitude, turning ~90 deg every leg) — a single turn looks identical
       whether it's a diversion or one leg of a loiter pattern.

    So this only fires once a new heading has HELD for a second confirmation
    cycle (~1 more poll interval), which a diversion does and an orbit doesn't.

    Note this only catches diversions that involve a large (>=course_deviation_deg)
    turn. See detect_route_corridor_deviation for diversions that don't need one
    (live-validated against a real case: Djerba->Nice was only 6% off the
    direct Djerba->Paris line — too small a turn for this detector to see).
    """
    if ac.get("on_ground") or not track.history:
        track.pending_deviation = None
        return None

    p_latest = track.history[-1]
    if p_latest.track is None or p_latest.alt_baro is None:
        track.pending_deviation = None
        return None

    # Stage 2: a suspected turn is already pending from a previous cycle —
    # confirm it held, or discard it as a vector/orbit-leg artifact.
    if track.pending_deviation:
        pending = track.pending_deviation
        if p_latest.ts - pending["detected_ts"] > PENDING_TIMEOUT_S:
            track.pending_deviation = None
            return None
        held = angle_diff_deg(p_latest.track, pending["new_heading"]) <= PENDING_HOLD_TOLERANCE_DEG
        track.pending_deviation = None
        if not held:
            return None

        # A held turn can also just be one leg of a racetrack/orbit hold —
        # live-tested (DAL2564, "route unknown" so detect_holding_pattern
        # itself never even ran, since it requires track.route): the "held
        # for one more cycle" confirmation above isn't enough alone, since a
        # hold's leg between turns can itself span multiple sample
        # intervals and look just as "held" as a genuine diversion turn.
        # Reuse detect_holding_pattern's own geometric signature (tight
        # radius + high total rotation) as a targeted extra check —
        # deliberately NOT gated on track.route the way
        # detect_holding_pattern itself is: its route gate exists to tell
        # "holding at the destination" apart from "holding elsewhere", a
        # question that doesn't matter here — a tight, high-rotation loiter
        # isn't a diversion turn either way, route known or not.
        rotation_check = _rotation_and_radius(track, cfg["holding_pattern_samples"])
        if rotation_check:
            recent_rotation, recent_radius = rotation_check
            if (recent_rotation >= cfg["holding_pattern_min_rotation_deg"]
                    and recent_radius <= cfg["holding_pattern_max_radius_nm"]):
                return None

        # Bow-tolerance check (MASTERPLAN.md sectie 8), same rationale as
        # detect_route_corridor_deviation: a held turn that still points
        # broadly toward the filed destination is more likely a real
        # airway/STAR turn than a diversion. Only applicable when a route
        # is known — course_deviation is deliberately route-independent
        # otherwise (see this function's docstring), so this can't run
        # universally the way it does in detect_route_corridor_deviation.
        if track.route:
            bearing_to_dest = bearing_deg(p_latest.lat, p_latest.lon, track.route["destination_lat"], track.route["destination_lon"])
            if angle_diff_deg(p_latest.track, bearing_to_dest) <= cfg["corridor_deviation_bow_heading_deg"]:
                return None

        note = _probable_destination_note(airport_db, p_latest.lat, p_latest.lon, p_latest.track) if airport_db else ""
        return Event(
            hex=track.hex, callsign=track.callsign, event_type="course_deviation",
            confidence="MOGELIJK",
            message=(
                f"aanhoudende koerswijziging {pending['pre_heading']:.0f}° -> "
                f"{pending['new_heading']:.0f}° (~{int(p_latest.ts - pending['detected_ts'])}s vastgehouden), "
                f"cruise op {p_latest.alt_baro:.0f}ft{_deviation_context(track, p_latest)}{note}"
            ),
            weather_icao=track.route["destination_icao"] if track.route else None,
            lat=p_latest.lat, lon=p_latest.lon,
            squawk=ac.get("squawk"), alt=p_latest.alt_baro,
            origin_icao=track.route["origin_icao"] if track.route else None,
            dest_icao=track.route["destination_icao"] if track.route else None,
        )

    # Stage 1: look for a fresh sudden turn, only during confirmed level cruise.
    if len(track.history) < 3:
        return None
    p_base, p_mid = track.history[-3], track.history[-2]
    if p_base.track is None or p_mid.track is None or None in (p_base.alt_baro, p_mid.alt_baro):
        return None

    # Live-tested: a 10,000ft floor let through a flood of false positives
    # (34 in one cycle) clustered at 10-15k ft — the SID/climb-out and STAR/
    # approach-vectoring band, not stable cruise. Altitude must also be level
    # across the window, not still climbing or descending.
    if p_latest.alt_baro < 18000:
        return None
    if max(p_base.alt_baro, p_mid.alt_baro, p_latest.alt_baro) - min(p_base.alt_baro, p_mid.alt_baro, p_latest.alt_baro) > 2000:
        return None

    threshold = cfg["course_deviation_deg"]
    if angle_diff_deg(p_base.track, p_mid.track) > threshold:
        return None  # heading was already unstable before this sample; not a clean "sudden" event
    if angle_diff_deg(p_mid.track, p_latest.track) < threshold:
        return None

    track.pending_deviation = {"pre_heading": p_mid.track, "new_heading": p_latest.track, "detected_ts": p_latest.ts}
    return None  # wait for next cycle to confirm it's sustained, not a vector/orbit blip


def detect_route_corridor_deviation(track: AircraftTrack, ac: dict, cfg: dict, airport_db=None) -> Event | None:
    """Flags an aircraft that has drifted, and STAYED drifted, well off the
    great-circle corridor between its filed origin and destination — without
    requiring any sudden turn. Complements detect_course_deviation, which by
    design only catches sharp, sustained heading breaks.

    Threshold scales with route length (long-haul tracks legitimately bow
    tens to hundreds of nm off the direct line for wind-optimized routing —
    e.g. North Atlantic Tracks). Honest limitation, found while validating
    against a real case: a genuine small diversion (Djerba->Nice instead of
    Djerba->Paris) was only ~6% of route length off the direct line — likely
    too small to reliably clear this threshold either. This detector is
    aimed at LARGER off-corridor excursions (continuing past the destination
    toward a further alternate, being vectored far off course, etc), not a
    substitute for the emergency-squawk or post-landing checks.
    """
    if not track.route or ac.get("on_ground") or not track.history:
        track.corridor_deviation_streak = 0
        return None

    p_latest = track.history[-1]
    if p_latest.alt_baro is None or p_latest.alt_baro < 10000:
        track.corridor_deviation_streak = 0
        return None

    route = track.route
    route_len = haversine_nm(route["origin_lat"], route["origin_lon"], route["destination_lat"], route["destination_lon"])
    # Bow-tolerance check (MASTERPLAN.md sectie 8): a route that bows off
    # the great-circle line but whose CURRENT heading still points broadly
    # toward the filed destination is very likely legitimate airway/wind-
    # optimized routing, not a diversion — a genuine diversion's heading
    # stops pointing there at all. Originally this only ran for routes
    # crossing the Russia/Siberia zone (crosses_russian_airspace_zone);
    # live production data (2026-08-06) showed the identical false-positive
    # pattern well outside that zone with no other corroborating signal
    # (AAL168 KPDX->KCLT 503nm off, ASH4002 KIAH->KOKC 85nm off, NOZ1865
    # LIBD->ENGM 152nm off), so this now runs for every route. Unlike the
    # original Russia-only version, a MISSING heading sample does NOT
    # suppress here — that fallback made sense only within the narrow
    # Russia exception; applied globally it would risk silencing a genuine
    # diversion during a brief heading-data gap, so we only suppress on
    # positive evidence the aircraft is still heading toward the
    # destination.
    if p_latest.track is not None:
        bearing_to_dest = bearing_deg(p_latest.lat, p_latest.lon, route["destination_lat"], route["destination_lon"])
        if angle_diff_deg(p_latest.track, bearing_to_dest) <= cfg["corridor_deviation_bow_heading_deg"]:
            track.corridor_deviation_streak = 0
            return None
    buffer_nm = max(30.0, min(150.0, route_len * 0.05))
    dist_from_origin = haversine_nm(route["origin_lat"], route["origin_lon"], p_latest.lat, p_latest.lon)
    dist_to_dest = haversine_nm(p_latest.lat, p_latest.lon, route["destination_lat"], route["destination_lon"])
    if dist_from_origin < buffer_nm or dist_to_dest < buffer_nm:
        track.corridor_deviation_streak = 0
        return None

    xtd = cross_track_distance_nm(route["origin_lat"], route["origin_lon"], route["destination_lat"], route["destination_lon"], p_latest.lat, p_latest.lon)
    # Live-tested against a real case (Emirates DXB->SFO diverting to LHR,
    # ~7030nm route): a pure 10%-of-route-length threshold gave 703nm before
    # firing — technically correct eventually, but ~15-25 min slower than it
    # should be, since a diversion to a "normal" alternate is nowhere near as
    # far off-course as 10% of an ultra-long-haul route. Capped so the
    # longest routes don't get an unreasonably loose pass.
    threshold_nm = max(cfg["corridor_deviation_min_nm"], min(cfg["corridor_deviation_max_nm"], route_len * cfg["corridor_deviation_pct"]))

    if xtd < threshold_nm:
        track.corridor_deviation_streak = 0
        return None

    track.corridor_deviation_streak += 1
    if track.corridor_deviation_streak < cfg["corridor_deviation_min_samples"]:
        return None  # not sustained yet — could be a single noisy fix or normal route bowing

    note = _probable_destination_note(airport_db, p_latest.lat, p_latest.lon, p_latest.track) if (airport_db and p_latest.track is not None) else ""
    return Event(
        hex=track.hex, callsign=track.callsign, event_type="corridor_deviation",
        confidence="MOGELIJK",
        message=(
            f"{xtd:.0f}nm van de directe route {route['origin_icao']}->{route['destination_icao']} "
            f"af, al {track.corridor_deviation_streak} metingen op rij op {p_latest.alt_baro:.0f}ft{note}"
        ),
        weather_icao=route["destination_icao"], lat=p_latest.lat, lon=p_latest.lon,
        squawk=ac.get("squawk"), alt=p_latest.alt_baro,
        origin_icao=route["origin_icao"], dest_icao=route["destination_icao"],
    )


def detect_premature_descent(track: AircraftTrack, ac: dict, cfg: dict) -> Event | None:
    """Flags a sustained descent that starts implausibly far from the planned
    destination — independent of heading entirely. A real, physics-based
    signal: unlike a lateral course change, a pilot can't avoid a descent
    profile looking abnormal if they're actually landing somewhere else.

    Directly requested after reviewing a real Emirates DXB->SFO diversion to
    LHR where the turn angle was ambiguous (possibly <90 deg, i.e. invisible
    to detect_course_deviation) — this detector doesn't care about turn angle
    at all, only altitude vs. remaining distance.

    Uses the standard ~3nm-per-1000ft top-of-descent rule of thumb, with a
    generous multiplier so normal early descents (ATC step-downs, long STARs,
    weather avoidance) don't false-positive.
    """
    n = cfg["premature_descent_samples"]
    if not track.route or ac.get("on_ground") or len(track.history) < n:
        return None

    window = list(track.history)[-n:]
    if any(p.alt_baro is None for p in window):
        return None

    # Require a real, CONTINUOUS descent through the whole window — each step
    # meaningfully lower than the last. Live-tested: checking only "never
    # goes back up" let through a one-time ATC step-down that then leveled
    # off (e.g. FL380 -> FL282 -> flat) — completely normal, not an approach
    # profile, but it technically "never increased" either.
    for i in range(len(window) - 1):
        if window[i + 1].alt_baro > window[i].alt_baro - 200:
            return None
    alt_drop = window[0].alt_baro - window[-1].alt_baro
    if alt_drop < cfg["premature_descent_min_drop_ft"]:
        return None

    latest = window[-1]
    route = track.route
    dist_to_dest = haversine_nm(latest.lat, latest.lon, route["destination_lat"], route["destination_lon"])
    dist_from_origin = haversine_nm(route["origin_lat"], route["origin_lon"], latest.lat, latest.lon)
    if dist_from_origin < 50:
        return None  # still near departure — this is just the initial climb, not a descent

    expected_tod_nm = (latest.alt_baro / 1000.0) * 3.0 * cfg["premature_descent_multiplier"]
    if dist_to_dest < expected_tod_nm:
        return None  # within plausible range for a normal approach to the actual destination

    return Event(
        hex=track.hex, callsign=track.callsign, event_type="premature_descent",
        confidence="MOGELIJK",
        message=(
            f"vroegtijdige daling: nog {dist_to_dest:.0f}nm te gaan naar {route['destination_icao']} "
            f"op {latest.alt_baro:.0f}ft (normale daling begint rond {expected_tod_nm / cfg['premature_descent_multiplier']:.0f}nm)"
        ),
        weather_icao=route["destination_icao"], lat=latest.lat, lon=latest.lon,
        squawk=ac.get("squawk"), alt=latest.alt_baro,
        origin_icao=route["origin_icao"], dest_icao=route["destination_icao"],
    )


def detect_signal_lost_near_airport(track: AircraftTrack, airport_db, cfg: dict) -> Event | None:
    """Infers a possible unconfirmed landing when ADS-B coverage drops out
    near a non-destination airport before we ever observe an on_ground
    transition. Found via backtesting a real case (United 2078, Houston->
    Phoenix, diverted to Luke AFB on a low-fuel emergency): small/military
    fields often lose ADS-B coverage before touchdown, so
    detect_landed_wrong_airport — which depends on catching that transition —
    never fires. Called from main.py for tracks that have been missing from
    several consecutive tier1 snapshots, using their last known position."""
    if not track.route or not track.history:
        return None
    last = track.history[-1]
    if last.alt_baro is None or last.alt_baro > cfg["signal_lost_max_altitude_ft"]:
        return None
    nearest, _dist = airport_db.nearest(last.lat, last.lon, max_km=cfg["signal_lost_search_km"])
    if not nearest:
        return None
    if nearest["icao"] == track.route["destination_icao"]:
        return None  # signal loss near the actual destination is routine — low-altitude coverage gaps happen there too, not just at diversion targets
    if nearest["icao"] == track.route["origin_icao"]:
        # Live data (2026-08-07, BACKTEST_LOG.md ronde 17): ~21% of live
        # signal_lost_near_airport hits (6/29 sampled) were last seen near
        # their OWN filed origin, not some unrelated airport — a freshly-
        # departed aircraft still climbing out below signal_lost_max_
        # altitude_ft, whose ADS-B coverage briefly drops (common near
        # smaller/regional fields during initial climb) before it ever
        # gets far enough to look like anything unusual. Unlike detect_
        # landed_wrong_airport's origin handling (which uses last_takeoff_ts
        # to allow a genuine early-return diversion through, because a
        # landing is unambiguous ground truth), that same time-window trick
        # doesn't help here: "still climbing out on departure" and "already
        # turned back and inbound" both fall in the identical early-post-
        # takeoff window, and this detector only has a last-known AIRBORNE
        # position, not a confirmed touchdown — there's no reliable way to
        # tell those two apart from this signal alone. A genuine early
        # return that actually LANDS is still caught by detect_landed_
        # wrong_airport's own, already-correct early-return logic (round 5);
        # this exclusion only suppresses the weaker, unconfirmed "vanished
        # near home" case, same reasoning as the destination exclusion
        # directly above.
        return None

    return Event(
        hex=track.hex, callsign=track.callsign, event_type="signal_lost_near_airport",
        confidence="MOGELIJK",
        message=(
            f"signaal verloren nabij {nearest['icao']} ({nearest['name']}) op {last.alt_baro:.0f}ft, "
            f"niet de geplande bestemming {track.route['destination_icao']} — mogelijk onbevestigde landing daar"
        ),
        weather_icao=nearest["icao"], lat=last.lat, lon=last.lon,
        alt=last.alt_baro, origin_icao=track.route["origin_icao"], dest_icao=track.route["destination_icao"],
    )


def detect_holding_pattern(track: AircraftTrack, ac: dict, airport_db, cfg: dict) -> Event | None:
    """Flags sustained rotation without net progress (a racetrack/holding
    pattern). Immediate for a non-destination airport; requires a much
    longer sustained streak near the actual destination (see below)."""
    n = cfg["holding_pattern_samples"]
    if ac.get("on_ground") or len(track.history) < n:
        track.holding_streak = 0
        return None
    if not track.route:
        # Live-tested: without a known route we can't exclude "this IS their
        # destination", and this fired 369 times in one run — almost all GA
        # aircraft doing completely normal circuit/pattern training near
        # their home airport, which adsbdb has no route data for at all.
        # Only meaningful for traffic where we know what they're SUPPOSED to
        # be doing.
        track.holding_streak = 0
        return None

    rotation_check = _rotation_and_radius(track, n)
    if rotation_check is None:
        return None
    total_rotation, max_radius = rotation_check
    if total_rotation < cfg["holding_pattern_min_rotation_deg"]:
        track.holding_streak = 0
        return None
    if max_radius > cfg["holding_pattern_max_radius_nm"]:
        track.holding_streak = 0
        return None  # spread too wide — a normal route with some turns, not a tight loiter

    last = track.history[-1]
    nearest, _dist = airport_db.nearest_large(last.lat, last.lon, max_km=90.0)
    if not nearest:
        track.holding_streak = 0
        return None  # holding somewhere with no major airport nearby — not actionable

    if nearest["icao"] == track.route["destination_icao"]:
        # Holding near the actual destination is normal for a while (arrival
        # sequencing). Backtested against a real case (AI850 Pune->Delhi:
        # held near Delhi 1hr+ before a fuel-emergency diversion to Gwalior)
        # that blanket-excluding this hides exactly the precursor pattern —
        # so it's allowed through once sustained far longer than routine
        # sequencing, instead of never.
        track.holding_streak += 1
        if track.holding_streak < cfg["holding_pattern_destination_min_streak"]:
            return None
        route_note = f" (dit IS de geplande bestemming {track.route['destination_icao']}, maar het wachten duurt ongewoon lang)"
        at_destination = True
    else:
        track.holding_streak = 0
        route_note = f" (geplande bestemming was {track.route['destination_icao']})"
        at_destination = False

    return Event(
        hex=track.hex, callsign=track.callsign, event_type="holding_pattern",
        confidence="MOGELIJK",
        message=f"mogelijk wachtpatroon bij {nearest['icao']} ({nearest['name']}){route_note}",
        weather_icao=nearest["icao"], lat=last.lat, lon=last.lon,
        squawk=ac.get("squawk"), alt=last.alt_baro,
        origin_icao=track.route["origin_icao"], dest_icao=track.route["destination_icao"],
        at_destination=at_destination,
    )


def evaluate(track: AircraftTrack, ac: dict, airport_db, cfg: dict, db_conn=None, skip_behavioral: bool = False) -> list[Event]:
    """skip_behavioral=True runs only detect_emergency — used for aircraft
    classified as military/helicopter/GA-private/business-jet (classify.py)
    when classification_suppress_non_airliner is on. A real emergency
    squawk from any aircraft class is still real, urgent evidence, so
    detect_emergency always runs regardless of this flag. See
    MASTERPLAN.md sectie 4."""
    events = []
    ev = detect_emergency(track, ac)
    if ev:
        events.append(ev)
    if skip_behavioral:
        return events
    ev = detect_landed_wrong_airport(track, ac, airport_db, cfg, db_conn)
    if ev:
        events.append(ev)
    ev = detect_course_deviation(track, ac, cfg, airport_db)
    if ev:
        events.append(ev)
    ev = detect_route_corridor_deviation(track, ac, cfg, airport_db)
    if ev:
        events.append(ev)
    ev = detect_premature_descent(track, ac, cfg)
    if ev:
        events.append(ev)
    ev = detect_holding_pattern(track, ac, airport_db, cfg)
    if ev:
        events.append(ev)
    return events
