"""Real, sourced diversion cases for backtest.py.

Each case is built from public incident reports. Exact ADS-B tracks aren't
publicly available for these flights, so positions between reported
waypoints are interpolated along the great circle and headings are derived
from that interpolation — realistic enough to exercise the geometry/
altitude detectors, but NOT a claim of exact historical accuracy. Every
assumption that fills a gap the source doesn't state (squawk codes,
groundspeed, exact hold radius) is called out in the case's `notes`.

Add a new case: research the incident, note ONE clear source, and reuse
`cruise_leg` / `holding_loop` / `ground_sample` to build `samples`. Keep
`real_decision_t` anchored to something the source actually states a time
for (a declared emergency, a decision to divert, a landing) — that's what
`lead_seconds` in the backtest report measures against.
"""
import math

from airports import _intermediate_point, bearing_deg

INTERVAL_S = 60  # matches tier1_interval_seconds default — sample-count thresholds (holding_pattern_samples etc.) assume this cadence.


def cruise_leg(p1, p2, alt, start_t, duration_s, interval_s=INTERVAL_S, squawk="1200", emergency="none"):
    """Samples along the great circle from p1=(lat,lon) to p2=(lat,lon),
    timed so the whole leg takes duration_s (i.e. implies a groundspeed)."""
    from backtest import Sample
    n = max(1, round(duration_s / interval_s))
    out = []
    for i in range(n + 1):
        frac = i / n
        lat, lon = _intermediate_point(p1[0], p1[1], p2[0], p2[1], frac)
        trk = bearing_deg(lat, lon, p2[0], p2[1]) if frac < 1 else bearing_deg(p1[0], p1[1], p2[0], p2[1])
        out.append(Sample(t=start_t + i * interval_s, lat=lat, lon=lon, alt_baro=alt, track=trk,
                           squawk=squawk, emergency=emergency))
    return out


def holding_loop(center, alt, start_t, duration_s, interval_s=INTERVAL_S, radius_nm=4.0,
                  lap_samples=6, squawk="1200", emergency="none"):
    """A tight circular racetrack around `center`, one full lap every
    `lap_samples` samples — well inside holding_pattern's rotation/radius
    thresholds regardless of the real hold's actual shape (real holds are
    a racetrack, not a circle, but the detector only cares about total
    rotation and centroid radius, both of which this matches)."""
    from backtest import Sample
    n = max(1, round(duration_s / interval_s))
    deg_per_sample = 360.0 / lap_samples
    out = []
    for i in range(n + 1):
        ang = math.radians((i * deg_per_sample) % 360)
        dlat = (radius_nm / 60.0) * math.cos(ang)
        dlon = (radius_nm / 60.0) * math.sin(ang) / math.cos(math.radians(center[0]))
        lat, lon = center[0] + dlat, center[1] + dlon
        trk = (i * deg_per_sample + 90) % 360
        out.append(Sample(t=start_t + i * interval_s, lat=lat, lon=lon, alt_baro=alt, track=trk,
                           squawk=squawk, emergency=emergency))
    return out


def descent_leg(p1, p2, alt1, alt2, start_t, duration_s, interval_s=INTERVAL_S, squawk="1200", emergency="none"):
    """Like cruise_leg, but linearly interpolates altitude from alt1 to
    alt2 across the leg — a climb or (usually) a descent."""
    from backtest import Sample
    n = max(1, round(duration_s / interval_s))
    out = []
    for i in range(n + 1):
        frac = i / n
        lat, lon = _intermediate_point(p1[0], p1[1], p2[0], p2[1], frac)
        trk = bearing_deg(lat, lon, p2[0], p2[1]) if frac < 1 else bearing_deg(p1[0], p1[1], p2[0], p2[1])
        alt = alt1 + (alt2 - alt1) * frac
        out.append(Sample(t=start_t + i * interval_s, lat=lat, lon=lon, alt_baro=alt, track=trk,
                           squawk=squawk, emergency=emergency))
    return out


def ground_sample(pos, t, squawk="1200", emergency="none"):
    from backtest import Sample
    return Sample(t=t, lat=pos[0], lon=pos[1], alt_baro=None, track=None, squawk=squawk, emergency=emergency)


def stitch(*legs):
    """Concatenates sample legs, dropping the leading sample of every leg
    after the first — cruise_leg/holding_loop are inclusive of both
    endpoints, so back-to-back legs would otherwise duplicate the boundary
    timestamp. A duplicate-t sample sits at zero simulated time apart from
    its predecessor, which silently masks real heading jumps (a genuine
    course reversal gets diluted across two near-identical angle_diff_deg
    calls instead of registering as one clean sudden turn)."""
    out = list(legs[0])
    for leg in legs[1:]:
        out.extend(leg[1:])
    return out


# ---------------------------------------------------------------------------
# Airports used below (lat, lon) — from data/airports.csv (ourairports).
PUNE = (18.5821, 73.9197)
DELHI = (28.55563, 77.09519)
DELHI_HOLD = (28.95, 77.10)  # "north of Delhi" per source; exact fix not published
JAIPUR = (26.8242, 75.8122)
GWALIOR = (26.2933, 78.2278)

HOUSTON_IAH = (29.9844, -95.3414)
PHOENIX_PHX = (33.4353, -112.0059)
LUKE_AFB = (33.5350, -112.3830)

DUBAI = (25.2498, 55.3710)
SAN_FRANCISCO = (37.6198, -122.3748)
NYAGAN = (62.11, 65.62)  # reported turnback point
LONDON_LHR = (51.4707, -0.4599)

NEW_YORK_JFK = (40.6398, -73.7789)
PARIS_CDG = (49.0097, 2.5479)
GULF_OF_MAINE = (43.5, -68.0)  # approximate — source says "over the Gulf of Maine", no exact fix

PARIS_ORLY = (48.7233, 2.3789)
MYKONOS = (37.4350, 25.3481)

ATLANTA = (33.6367, -84.4281)
ORLANDO = (28.4294, -81.3090)
FORT_LAUDERDALE = (26.0726, -80.1527)

ISTANBUL_IST = (41.2753, 28.7519)   # LTFM
TORONTO_YYZ = (43.6772, -79.6306)   # CYYZ, filed destination — not reached
NORTH_SEA_POINT = (56.0, 3.0)        # source says only "over the North Sea", no exact fix
MANCHESTER = (53.3537, -2.2750)      # EGCC


def _ai850():
    """Air India AI-850, Pune->Delhi, 25 May 2023. A320 VT-ETE held north
    of Delhi, diverted toward Jaipur, aborted the approach, declared MAYDAY
    FUEL, and landed at Gwalior with 464kg of fuel remaining.
    Source: https://avherald.com/h?article=53205b0a&opt=0 (final report timeline)."""
    t0 = 0.0  # 14:52 UTC = descent-into-Delhi start
    HOLD_START = 17 * 60.0     # 15:09 hold established, FL170, 5853kg fuel
    DIVERT_JAIPUR = 126 * 60.0  # 16:18 diverts toward Jaipur, 2990kg fuel
    ABORT_JAIPUR = 166 * 60.0   # ~16:58 missed approach at Jaipur (windshear), ~1600kg fuel
    MAYDAY = 189 * 60.0         # 17:01 "MAYDAY FUEL" declared, diverts to Gwalior
    LANDING = 220 * 60.0        # 17:32 lands Gwalior, 464kg remaining

    # Short cruise-in on the direct Pune->Delhi corridor before the hold.
    leg_in = cruise_leg(PUNE, DELHI_HOLD, 30000, t0, HOLD_START - t0)
    leg_hold = holding_loop(DELHI_HOLD, 17000, HOLD_START, DIVERT_JAIPUR - HOLD_START, lap_samples=4)
    leg_to_jaipur = cruise_leg(DELHI_HOLD, JAIPUR, 15000, DIVERT_JAIPUR, ABORT_JAIPUR - DIVERT_JAIPUR)
    # Missed approach -> MAYDAY FUEL declared -> divert to Gwalior. Squawk
    # 7700 from MAYDAY onward is an assumption (source states the voice
    # call, not the transponder code; ICAO procedure calls for 7700 on a
    # formal MAYDAY, but this isn't independently confirmed here).
    leg_missed = cruise_leg(JAIPUR, JAIPUR, 3300, ABORT_JAIPUR, MAYDAY - ABORT_JAIPUR)
    leg_to_gwl = cruise_leg(JAIPUR, GWALIOR, 4000, MAYDAY, LANDING - MAYDAY, squawk="7700")
    samples = stitch(leg_in, leg_hold, leg_to_jaipur, leg_missed, leg_to_gwl)
    samples.append(ground_sample(GWALIOR, LANDING, squawk="7700"))

    from backtest import Case
    return Case(
        name="Air India AI850 Pune-Delhi-Gwalior (2023-05-25)",
        source="https://avherald.com/h?article=53205b0a&opt=0",
        hex_id="vtete01", callsign="AIC850",
        origin_icao="VAPO", dest_icao="VIDP",
        samples=samples,
        expected_type="holding_pattern",
        real_decision_t=DIVERT_JAIPUR,
        real_decision_label="crew decides to divert (to Jaipur)",
        notes=("Squawk 7700 from the MAYDAY call onward is assumed, not independently "
               "confirmed by the source. Hold center/radius are approximated — source says "
               "'north of Delhi' without an exact fix."),
        milestones=[
            ("hold established, FL170, 5853kg fuel", HOLD_START),
            ("decides to divert toward Jaipur, 2990kg fuel", DIVERT_JAIPUR),
            ("missed approach at Jaipur (windshear), ~1600kg fuel", ABORT_JAIPUR),
            ("MAYDAY FUEL declared, diverts to Gwalior", MAYDAY),
            ("lands Gwalior, 464kg fuel remaining", LANDING),
        ],
    )


def _af9():
    """Air France AF9, New York(JFK)->Paris(CDG), 19 Aug 2025 (departed
    23:30 local Aug 18). B777-300ER (F-GSQL) suffered a right engine
    failure ~40min into the flight, over the Gulf of Maine, declared
    MAYDAY and squawked 7700, turned back and landed at JFK ~2:15am local
    (~2h45m after departure). A clean near-180° reversal back toward the
    ORIGIN — unlike EK225's ~84° reconstructed turn (round 1/3), this one
    is confidently large: bearing-to-CDG vs bearing-to-JFK from the
    reported position differ by ~179°.
    Source: https://airlive.net/emergency/2025/08/19/air-france-af9-is-declaring-an-emergency-and-returning-to-new-york-jfk/"""
    t0 = 0.0
    TURN_T = 40 * 60.0       # ~40min in, over the Gulf of Maine, MAYDAY + squawk 7700
    LANDING = 165 * 60.0     # ~2h45m total flight time, back at JFK (~2:15am - 23:30)

    leg_out = cruise_leg(NEW_YORK_JFK, GULF_OF_MAINE, 35000, t0, TURN_T - t0)
    leg_back = cruise_leg(GULF_OF_MAINE, NEW_YORK_JFK, 35000, TURN_T, LANDING - TURN_T, squawk="7700")
    samples = stitch(leg_out, leg_back)
    samples.append(ground_sample(NEW_YORK_JFK, LANDING, squawk="7700"))

    from backtest import Case
    return Case(
        name="Air France AF9 JFK-CDG turnback to JFK (2025-08-19)",
        source="https://airlive.net/emergency/2025/08/19/air-france-af9-is-declaring-an-emergency-and-returning-to-new-york-jfk/",
        hex_id="fgsql01", callsign="AFR9",
        origin_icao="KJFK", dest_icao="LFPG",
        samples=samples,
        expected_type="course_deviation",
        real_decision_t=TURN_T,
        real_decision_label="MAYDAY declared, turns back over the Gulf of Maine",
        notes=("Gulf of Maine position is approximate (source gives no exact fix). Cruise "
               "altitude at the 40min mark (35000ft, already level) is a plausible assumption "
               "for a widebody this far into a long-haul departure, not sourced. The ~2h45m "
               "total flight time (40min out + long return) implies a slow return leg, "
               "plausibly fuel dumping/holding to land at a safe weight — not modelled here, "
               "the return leg is a direct track at constant altitude/speed."),
        milestones=[
            ("MAYDAY + squawk 7700, turns back over Gulf of Maine", TURN_T),
            ("lands back at JFK", LANDING),
        ],
    )


def _ua2078():
    """United 2078, Houston(IAH)->Phoenix(PHX), 21 Jul 2026. B737-900
    held/vectored near PHX during severe thunderstorms, crew declared
    "minimum fuel", diverted to Luke AFB (~15nm from PHX) instead.
    Source: https://airlive.net/tracking/2026/07/21/united-ua2078-landed-at-luke-air-force-instead-of-phx-after-declaring-a-minimum-fuel-emergency/
    No public source gives exact hold timing/position, so this leg is a
    plausible reconstruction, not a sourced timeline like AI850's."""
    t0 = 0.0
    HOLD_START = 150 * 60.0   # approaching PHX area, weather forces holding — timing approximated
    MIN_FUEL = 195 * 60.0     # crew declares "minimum fuel" — approximated; source gives no timestamp
    LANDING = 205 * 60.0      # diverts and lands Luke AFB

    leg_in = cruise_leg(HOUSTON_IAH, PHOENIX_PHX, 34000, t0, HOLD_START - t0)
    # "Minimum fuel" is a real ICAO/FAA phraseology distinct from a formal
    # emergency declaration and does NOT reliably trigger a 7700 squawk —
    # modelled here as squawk staying at 1200 throughout, to test whether
    # the system can catch this WITHOUT relying on a transponder code change.
    leg_hold = holding_loop(PHOENIX_PHX, 12000, HOLD_START, MIN_FUEL - HOLD_START, radius_nm=6.0, lap_samples=4)
    leg_to_luke = cruise_leg(PHOENIX_PHX, LUKE_AFB, 4000, MIN_FUEL, LANDING - MIN_FUEL)
    samples = stitch(leg_in, leg_hold, leg_to_luke)
    samples.append(ground_sample(LUKE_AFB, LANDING))

    from backtest import Case
    return Case(
        name="United 2078 Houston-Phoenix-Luke AFB (2026-07-21)",
        source="https://airlive.net/tracking/2026/07/21/united-ua2078-landed-at-luke-air-force-instead-of-phx-after-declaring-a-minimum-fuel-emergency/",
        hex_id="ua2078hex", callsign="UAL2078",
        origin_icao="KIAH", dest_icao="KPHX",
        samples=samples,
        expected_type="wrong_airport",
        real_decision_t=MIN_FUEL,
        real_decision_label='crew declares "minimum fuel"',
        notes=("No public source gives exact hold timing or a squawk code — hold duration/"
               "position and the assumption of no 7700 are reconstructed, not sourced. This "
               "case specifically tests detection WITHOUT an emergency squawk, since "
               "'minimum fuel' isn't the same as declaring an emergency."),
        milestones=[
            ("holding/vectoring near PHX in storms (approx.)", HOLD_START),
            ('declares "minimum fuel" (approx. timing)', MIN_FUEL),
            ("lands Luke AFB instead of PHX", LANDING),
        ],
    )


def _ua2078_signal_lost():
    """Variant of the UA2078 case above, testing a different mechanism:
    detect_signal_lost_near_airport's own docstring says it exists BECAUSE
    of this exact flight — "small/military fields often lose ADS-B coverage
    before touchdown" — but the case above models a clean on-ground sample
    at Luke AFB, which only exercises detect_landed_wrong_airport, not the
    coverage-drop detector it was actually written for. No source confirms
    whether UA2078's coverage genuinely dropped before touchdown; this
    variant tests the MECHANISM the code comment describes (last known
    position low and close to a non-destination airport, then nothing),
    using the same well-sourced flight as the vehicle — not a claim that
    this is exactly what happened on 2026-07-21.
    Source: same as above."""
    t0 = 0.0
    HOLD_START = 150 * 60.0
    MIN_FUEL = 195 * 60.0
    LAST_SEEN = 204 * 60.0  # ~1 min from touchdown, low and inbound to Luke AFB, then coverage drops

    leg_in = cruise_leg(HOUSTON_IAH, PHOENIX_PHX, 34000, t0, HOLD_START - t0)
    leg_hold = holding_loop(PHOENIX_PHX, 12000, HOLD_START, MIN_FUEL - HOLD_START, radius_nm=6.0, lap_samples=4)
    leg_to_luke = cruise_leg(PHOENIX_PHX, LUKE_AFB, 4000, MIN_FUEL, LAST_SEEN - MIN_FUEL)
    samples = stitch(leg_in, leg_hold, leg_to_luke)

    from backtest import Case
    return Case(
        name="United 2078 Houston-Phoenix, signal lost nearing Luke AFB (variant)",
        source="https://airlive.net/tracking/2026/07/21/united-ua2078-landed-at-luke-air-force-instead-of-phx-after-declaring-a-minimum-fuel-emergency/",
        hex_id="ua2078hex2", callsign="UAL2078",
        origin_icao="KIAH", dest_icao="KPHX",
        samples=samples,
        expected_type="signal_lost_near_airport",
        real_decision_t=MIN_FUEL,
        real_decision_label='crew declares "minimum fuel"',
        notes=("Same reconstructed hold timing as the base UA2078 case. The 'coverage drop "
               "before touchdown' ending is NOT sourced — it tests the mechanism "
               "detect_signal_lost_near_airport's own docstring cites this flight for, using "
               "a plausible last-known-position rather than a confirmed fact."),
        milestones=[
            ("holding/vectoring near PHX in storms (approx.)", HOLD_START),
            ('declares "minimum fuel" (approx. timing)', MIN_FUEL),
            ("last seen low, inbound to Luke AFB — coverage drops (unsourced variant)", LAST_SEEN),
        ],
        goes_missing=True,
    )


EK225_SOURCE = "https://airlive.net/tracking/2026/07/30/emirates-a380-to-san-francisco-over-northern-russia-performed-a-5-hour-diversion-to-london/"


def _ek225_track():
    """Shared track for both EK225 cases below — see _ek225() for the
    sourced facts. Builds in a realistic descent-into-LHR profile (not
    independently sourced — no source gives an altitude timeline) so the
    same track can also exercise detect_premature_descent, whose own
    docstring cites reviewing 'a real Emirates DXB->SFO diversion to LHR'
    as directly motivating it (that detector doesn't care about turn angle
    at all, only altitude vs. remaining distance to the FILED destination —
    which stays KSFO throughout, thousands of nm away, no matter how close
    the aircraft actually is to LHR)."""
    t0 = 0.0
    TURN_T = 6 * 3600.0            # ~6h into the flight, over Nyagan
    LANDING = TURN_T + 5 * 3600.0  # ~5h flight from the turn to LHR touchdown
    TOD_T = LANDING - 25 * 60.0    # top of descent ~25min out — a plausible widebody profile, not sourced

    leg_out = cruise_leg(DUBAI, NYAGAN, 39000, t0, TURN_T - t0)
    # Medical emergencies are commonly declared PAN PAN, not always 7700 —
    # squawk kept at 1200 to test the geometry-only detectors specifically.
    tod_frac = (TOD_T - TURN_T) / (LANDING - TURN_T)
    tod_pos = _intermediate_point(NYAGAN[0], NYAGAN[1], LONDON_LHR[0], LONDON_LHR[1], tod_frac)
    leg_cruise_back = cruise_leg(NYAGAN, tod_pos, 39000, TURN_T, TOD_T - TURN_T)
    leg_descent = descent_leg(tod_pos, LONDON_LHR, 39000, 1200, TOD_T, (LANDING - 60) - TOD_T)
    samples = stitch(leg_out, leg_cruise_back, leg_descent)
    samples.append(ground_sample(LONDON_LHR, LANDING))
    return samples, TURN_T, TOD_T, LANDING


def _ek225():
    """Emirates EK225, Dubai(DXB)->San Francisco(SFO), 29-30 Jul 2026. A380
    turned back over Nyagan, Russia (~6h into the polar route) after a
    passenger medical emergency, diverting ~2485mi to London Heathrow.
    Source: see EK225_SOURCE."""
    samples, TURN_T, TOD_T, LANDING = _ek225_track()

    from backtest import Case
    return Case(
        name="Emirates EK225 Dubai-SFO-Heathrow turnback (2026-07-29)",
        source=EK225_SOURCE,
        hex_id="a6evh01", callsign="UAE225",
        origin_icao="OMDB", dest_icao="KSFO",
        samples=samples,
        expected_type="corridor_deviation",
        real_decision_t=TURN_T,
        real_decision_label="crew turns back toward Europe over Nyagan",
        notes=("Squawk kept at 1200 (medical emergencies are commonly PAN PAN, not always "
               "7700) — this case specifically tests the geometry detectors' own speed, "
               "independent of any transponder code. Cruise groundspeed between reported "
               "waypoints, and the descent-into-LHR profile, are assumed/plausible "
               "reconstructions, not sourced."),
        milestones=[
            ("turns back over Nyagan, Russia, ~6h into the flight", TURN_T),
            ("top of descent into LHR (unsourced, plausible timing)", TOD_T),
            ("lands London Heathrow, ~5h after the turn", LANDING),
        ],
    )


def _ek225_premature_descent():
    """Same track as _ek225() — see that function and EK225_SOURCE. Tests
    detect_premature_descent specifically: descending toward LHR while the
    FILED route is still OMDB->KSFO, thousands of nm away, is exactly the
    'altitude vs. remaining distance to filed destination' signature that
    detector's docstring says this incident category motivated. The
    descent profile itself (top-of-descent point/rate) is an unsourced but
    plausible reconstruction — no source gives an altitude timeline."""
    samples, TURN_T, TOD_T, LANDING = _ek225_track()

    from backtest import Case
    return Case(
        name="Emirates EK225 premature descent toward LHR (variant)",
        source=EK225_SOURCE,
        hex_id="a6evh02", callsign="UAE225",
        origin_icao="OMDB", dest_icao="KSFO",
        samples=samples,
        expected_type="premature_descent",
        real_decision_t=TOD_T,
        real_decision_label="begins descent toward LHR (unsourced, plausible timing)",
        notes=("Same track as the corridor_deviation EK225 case — this variant exists so "
               "both detectors' timing show up separately in the report. The descent "
               "profile is unsourced/reconstructed, not a documented fact."),
        milestones=[
            ("turns back over Nyagan, Russia, ~6h into the flight", TURN_T),
            ("top of descent into LHR (unsourced, plausible timing)", TOD_T),
            ("lands London Heathrow", LANDING),
        ],
    )


def _to3510():
    """Transavia France TO-3510, Paris Orly(ORY)->Mykonos(JMK), 24 Jul 2026.
    B738 F-GZHJ was climbing out of Orly when the crew reported a smell of
    smoke in the cockpit, stopped the climb at about 4000ft, and returned to
    Orly for a safe landing about 13 minutes after departure.

    A clean, sourced example of the FAST/LOW return-to-origin category
    detect_landed_wrong_airport's own origin-exclusion used to swallow
    completely regardless of cause: never above 4000ft, so
    course_deviation's >=18,000ft cruise floor and
    detect_route_corridor_deviation's 10,000ft floor both exclude it no
    matter the turn angle, and 13 minutes is nowhere near
    holding_pattern's streak requirements either — landing back at the
    filed ORIGIN was the only signal available, which the old blanket
    exclusion (aimed at regional multi-leg callsign reuse) discarded
    outright. See detect_landed_wrong_airport's early-return comment.
    Source: https://avherald.com/h?article=53c715b3&opt=0"""
    TAKEOFF = 0.0
    TURN_T = 180.0        # ~3min in, ~4000ft, crew reports smoke smell, stops climb
    LANDING = 13 * 60.0   # back on the ground at Orly, ~13min after departure

    # Squawk isn't stated by the source — kept at 1200 throughout to test
    # detection without relying on a transponder code change, same
    # convention as the UA2078/EK225 cases.
    turn_point = _intermediate_point(PARIS_ORLY[0], PARIS_ORLY[1], MYKONOS[0], MYKONOS[1], 0.01)
    leg_climb = descent_leg(PARIS_ORLY, turn_point, 1500, 4000, TAKEOFF, TURN_T - TAKEOFF)
    leg_return = descent_leg(turn_point, PARIS_ORLY, 4000, 500, TURN_T, (LANDING - 60) - TURN_T)
    samples = [ground_sample(PARIS_ORLY, TAKEOFF - 60)] + stitch(leg_climb, leg_return)
    samples.append(ground_sample(PARIS_ORLY, LANDING))

    from backtest import Case
    return Case(
        name="Transavia France TO3510 Orly-Mykonos early return to Orly (2026-07-24)",
        source="https://avherald.com/h?article=53c715b3&opt=0",
        hex_id="fgzhj001", callsign="TVF3510",
        origin_icao="LFPO", dest_icao="LGMK",
        samples=samples,
        expected_type="wrong_airport",
        real_decision_t=TURN_T,
        real_decision_label="crew reports smoke smell, stops climb ~4000ft, turns back",
        notes=("Squawk isn't stated by the source — kept at 1200 throughout, same "
               "convention as the UA2078/EK225 cases. The outbound track point (1% of "
               "the great-circle route toward Mykonos) and the return descent profile are "
               "plausible reconstructions, not sourced — the source gives peak altitude "
               "(~4000ft) and total elapsed time (~13min) but no intermediate track."),
        milestones=[
            ("takeoff from Orly", TAKEOFF),
            ("smoke smell reported, climb stopped ~4000ft, turns back", TURN_T),
            ("lands back at Orly, ~13min after departure", LANDING),
        ],
    )


def _atl_mco_fll():
    """NOT a specific sourced incident — a mechanism-testing case, same
    convention as round 2's `_ua2078_signal_lost` variant, built to exercise
    a real orchestration bug found while backtesting: main.py's ONGOING
    route_plausible recheck (added to guard against callsign-collision bad
    data — see that function's docstring) also stripped track.route on a
    GENUINE diversion that simply overflies its destination in a near-
    straight line toward a further alternate, e.g. a real, common pattern
    (weather/traffic at the planned airport, continuing to another instead
    of holding). Real airports/geometry, synthetic scenario: filed
    KATL->KMCO (351nm), continues almost dead straight (bearing changes
    only ~4°, cross-track distance from the direct ATL-MCO line stays under
    7nm) on to KFLL instead of landing — a plausible real-world diversion
    shape, Atlanta-Orlando-Fort Lauderdale all sitting close to one line.

    This overrun trips the OLD strict check (dist_from_origin + dist_to_dest
    > 1.5x route length) well before reaching FLL, at which point
    route_plausible's `check_progress=False` mode (used for this ongoing
    recheck, see its docstring) is what keeps detect_landed_wrong_airport
    able to fire at all when it eventually lands off-route."""
    t0 = 0.0
    MCO_ABEAM = 50 * 60.0     # ~50min in, abeam Orlando at low altitude, continues instead of landing
    LANDING = MCO_ABEAM + 28 * 60.0  # ~28min further on to Fort Lauderdale

    leg1 = descent_leg(ATLANTA, ORLANDO, 25000, 5000, t0, MCO_ABEAM - t0)
    leg2 = descent_leg(ORLANDO, FORT_LAUDERDALE, 5000, 500, MCO_ABEAM, (LANDING - 60) - MCO_ABEAM)
    samples = stitch(leg1, leg2)
    samples.append(ground_sample(FORT_LAUDERDALE, LANDING))

    from backtest import Case
    return Case(
        name="Synthetic ATL-MCO overflight to FLL (route_plausible mechanism test)",
        source="Not a sourced incident — see docstring above for why this case exists.",
        hex_id="synth0001", callsign="TST123",
        origin_icao="KATL", dest_icao="KMCO",
        samples=samples,
        expected_type="wrong_airport",
        real_decision_t=MCO_ABEAM,
        real_decision_label="continues past Orlando instead of landing, proceeds to Fort Lauderdale",
        notes=("Synthetic timing/altitude profile, not sourced — this case exists to test the "
               "route_plausible orchestration fix (see airports.py and BACKTEST_LOG.md round 5), "
               "not to reconstruct a specific real flight. Airports and their relative geometry "
               "are real."),
        milestones=[
            ("abeam Orlando (MCO), continues instead of landing", MCO_ABEAM),
            ("lands Fort Lauderdale (FLL) instead of Orlando", LANDING),
        ],
    )


def _tk17():
    """Turkish Airlines TK17, Istanbul(IST)->Toronto(YYZ), 29 Jul 2026.
    B777-3F2ER (TC-JJR) reported a technical fault ~4h into the transatlantic
    passage, cruising FL320, descended to FL260 over the North Sea while
    coordinating with ATC, diverted to Manchester (EGCC) instead of
    continuing to Toronto, landed runway 23L ~19:30 local (UK).
    Source: https://airlive.net/tracking/2026/07/29/after-holding-pattern-over-the-north-sea-a-turkish-airlines-boeing-777-to-toronto-diverted-to-manchester/

    Deliberately a different shape from the existing wrong_airport cases
    (UA2078/TO3510/ATL-MCO-FLL): Istanbul->Toronto's great circle already
    bows north across Europe/the UK, so IST->North Sea 4h in is plausibly
    close to the FILED route, not a large corridor deviation the way
    AI850/EK225 are — this case mainly checks that detect_landed_wrong_
    airport still cleanly catches a genuine diversion via the landing
    itself even when the in-flight geometry detectors have comparatively
    little to grab onto, rather than adding a second data point for an
    already-well-covered large-deviation shape."""
    t0 = 0.0
    FAULT_T = 240 * 60.0    # ~4h in, "roughly four hours into the transatlantic passage", FL320, technical fault, begins descent toward FL260
    LANDING = 290 * 60.0    # ~4h50m total (16:40 IST local departure -> ~19:30 UK local landing), Manchester rwy 23L

    leg_out = cruise_leg(ISTANBUL_IST, NORTH_SEA_POINT, 32000, t0, FAULT_T - t0)
    leg_divert = descent_leg(NORTH_SEA_POINT, MANCHESTER, 32000, 1500, FAULT_T, (LANDING - 60) - FAULT_T)
    samples = stitch(leg_out, leg_divert)
    samples.append(ground_sample(MANCHESTER, LANDING))

    from backtest import Case
    return Case(
        name="Turkish Airlines TK17 Istanbul-Toronto diverted to Manchester (2026-07-29)",
        source="https://airlive.net/tracking/2026/07/29/after-holding-pattern-over-the-north-sea-a-turkish-airlines-boeing-777-to-toronto-diverted-to-manchester/",
        hex_id="tcjjr001", callsign="THY17",
        origin_icao="LTFM", dest_icao="CYYZ",
        samples=samples,
        expected_type="wrong_airport",
        real_decision_t=FAULT_T,
        real_decision_label="technical fault reported, begins descent/ATC coordination over the North Sea",
        notes=("Exact position ('over the North Sea') and the FL320->FL260 descent's precise "
               "timing/rate are approximated — source gives altitudes and a landing time but no "
               "track. The 4h/4h50m timings are derived from the source's 'roughly four hours' "
               "phrasing and the stated 16:40 Istanbul-local departure / ~19:30 UK-local landing, "
               "not independently confirmed minute-by-minute. Squawk kept at 1200 throughout — "
               "the source describes a technical fault handled via ATC coordination, not a "
               "declared emergency/squawk change."),
        milestones=[
            ("technical fault at FL320, begins descending toward FL260 over the North Sea", FAULT_T),
            ("lands Manchester (EGCC) runway 23L instead of Toronto", LANDING),
        ],
    )


CASES = [_ai850(), _af9(), _ua2078(), _ua2078_signal_lost(), _ek225(), _ek225_premature_descent(), _to3510(),
         _atl_mco_fll(), _tk17()]
