import csv
import math
import os
import urllib.request

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
_CSV_PATH = os.path.join(_DATA_DIR, "airports.csv")
_CSV_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"

# Only these types are plausible diversion/landing targets for airline traffic.
_RELEVANT_TYPES = {"large_airport", "medium_airport", "small_airport"}


def ensure_airports_csv() -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    if not os.path.exists(_CSV_PATH):
        urllib.request.urlretrieve(_CSV_URL, _CSV_PATH)


class AirportDB:
    def __init__(self):
        ensure_airports_csv()
        self.by_icao = {}
        self._all = []
        self._large = []
        with open(_CSV_PATH, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                icao = row.get("icao_code") or row.get("gps_code") or ""
                icao = icao.strip()
                row_type = row.get("type")
                if not icao or row_type not in _RELEVANT_TYPES:
                    continue
                try:
                    lat = float(row["latitude_deg"])
                    lon = float(row["longitude_deg"])
                except (ValueError, KeyError):
                    continue
                entry = {
                    "icao": icao,
                    "iata": (row.get("iata_code") or "").strip(),
                    "name": row.get("name", ""),
                    "municipality": row.get("municipality", ""),
                    "lat": lat,
                    "lon": lon,
                }
                self.by_icao[icao] = entry
                self._all.append(entry)
                if row_type == "large_airport":
                    self._large.append(entry)

    def get(self, icao: str):
        return self.by_icao.get((icao or "").strip().upper())

    def nearest(self, lat: float, lon: float, max_km: float = 15.0):
        """Nearest airport within max_km, or None. Used to decide 'landed at X'."""
        best = None
        best_dist = max_km
        for a in self._all:
            d = haversine_km(lat, lon, a["lat"], a["lon"])
            if d < best_dist:
                best = a
                best_dist = d
        return best, best_dist if best else None

    def nearest_large(self, lat: float, lon: float, max_km: float = 50.0):
        """Nearest large_airport within max_km. Used for holding-pattern location context."""
        best = None
        best_dist = max_km
        for a in self._large:
            d = haversine_km(lat, lon, a["lat"], a["lon"])
            if d < best_dist:
                best = a
                best_dist = d
        return best, best_dist if best else None

    def candidates_ahead(self, lat: float, lon: float, heading: float, max_nm: float = 300.0, cone_deg: float = 40.0, limit: int = 3):
        """Large airports roughly in the direction of `heading` from (lat, lon),
        ranked by distance. Used to guess a probable diversion target from
        geometry alone once we've already detected a deviation."""
        hits = []
        for a in self._large:
            dist = haversine_nm(lat, lon, a["lat"], a["lon"])
            if dist > max_nm or dist < 1:
                continue
            brng = bearing_deg(lat, lon, a["lat"], a["lon"])
            if angle_diff_deg(heading, brng) > cone_deg:
                continue
            hits.append((dist, a))
        hits.sort(key=lambda x: x[0])
        return [a for _, a in hits[:limit]]


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def haversine_nm(lat1, lon1, lat2, lon2) -> float:
    return haversine_km(lat1, lon1, lat2, lon2) / 1.852


def bearing_deg(lat1, lon1, lat2, lon2) -> float:
    """Initial great-circle bearing from point 1 to point 2, in degrees 0-360."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    x = math.sin(dlambda) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlambda)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def angle_diff_deg(a: float, b: float) -> float:
    """Smallest absolute difference between two headings, 0-180."""
    d = abs(a - b) % 360
    return d if d <= 180 else 360 - d


# Generic "close enough to be the same metro area" radius — deliberately a
# plain distance threshold, not a hardcoded per-city airport list (cheap,
# needs no maintenance as new multi-airport pairs come up worldwide).
# Calibrated against live data (2026-08-07, BACKTEST_LOG.md ronde 21):
# combined ~1100 unique live events across this session's pulls, checking
# holding_pattern/signal_lost_near_airport/wrong_airport hits whose
# "nearest" airport was neither the exact filed origin nor destination.
# Every genuine sister-airport pair found (JFK-LGA 9nm, Milan Malpensa-
# Linate 26nm, Oakland-San Jose 26nm, LAX-area 31nm, London Stansted-Luton
# 22nm) fell within 31nm; the next-closest non-metro case sat at 46nm with
# a large gap in between — 35nm sits with margin in that gap, same
# "start reasoned, verify against real data" approach as ROUTE_PLAUSIBLE_
# PROGRESS_MULTIPLIER (round 7) and corridor_deviation_bow_heading_deg.
AIRPORT_SAME_METRO_RADIUS_NM = 35.0


def airports_same_metro(lat1: float, lon1: float, lat2: float, lon2: float) -> bool:
    """True if two airport coordinates are close enough to plausibly serve
    the same city/metro area (e.g. JFK vs LGA, Milan Malpensa vs Linate),
    even though their ICAO codes differ. Used to stop route-dependent
    detectors from treating 'the aircraft is actually near its filed
    origin/destination's SISTER airport' as if it were an unrelated
    location — a real, recurring pattern behind holding_pattern/
    signal_lost_near_airport/wrong_airport false positives (adsbdb schedule
    data naming one airport of a metro pair while the aircraft is actually
    headed to/from the other)."""
    return haversine_nm(lat1, lon1, lat2, lon2) <= AIRPORT_SAME_METRO_RADIUS_NM


EARTH_RADIUS_NM = 3440.065


# How much slack (as a multiple of the direct route length) a resolved
# route's along-track progress is allowed before route_plausible's strict
# (check_progress=True) mode rejects it. Live-production evidence, not just
# backtest: two real adsbdb mismatches caught in production (AAL974
# resolved to a filed SBGL->JFK route while actually a domestic 737 near
# Dallas — a widebody-only route mismatch adsbdb likely never updated after
# the flight number was reassigned; SWA2820 resolved to KMCO->KMHT while
# actually flying a Chicago-Savannah rotation) both sat at 1.33-1.38x — the
# OLD 1.5x threshold let both through. Every legitimate case in
# backtest_cases.py (including EK225's 5-hour, ~2500nm real diversion)
# stays under 1.09x even at its most extreme point. 1.2x sits with
# comfortable margin on both sides of that gap.
ROUTE_PLAUSIBLE_PROGRESS_MULTIPLIER = 1.2

# How long after a route is (re-)resolved callers should still apply the
# strict, check_progress=True test (rather than the relaxed, cross-track-
# only one) — see route_plausible's docstring for why neither check alone
# can do this job forever. Used by both main.py's ongoing recheck and
# backtest.py's mirror of it.
ROUTE_REVALIDATION_WINDOW_S = 20 * 60


def route_plausible(origin_lat, origin_lon, dest_lat, dest_lon, cur_lat, cur_lon, check_progress: bool = True) -> bool:
    """Sanity check for adsbdb route data before trusting it. Live-tested:
    reused/generic callsigns (regional carriers, charter/on-demand operators
    like NetJets) sometimes get adsbdb's route for a DIFFERENT flight
    entirely, which then produces nonsensical numbers — e.g. a supposed
    Portland->San Francisco flight (450nm route) showing 748nm cross-track
    deviation, which is geometrically impossible for a real flight actually
    on that route. When the numbers don't add up, the route itself is the
    thing that's wrong, not the aircraft's behavior — better to fall back to
    "route unknown" than to alarm on bad reference data.

    check_progress (default True, used when a route is FIRST resolved, and
    for a bounded revalidation window afterward — see main.py's
    ROUTE_REVALIDATION_WINDOW_S) also requires along-route progress to add
    up (dist_from_origin + dist_to_dest <= ROUTE_PLAUSIBLE_PROGRESS_MULTIPLIER
    x route length) — reasonable early on, since a genuinely-matched route
    shouldn't show a huge detour that soon.

    check_progress=False (used for main.py's ONGOING recheck of a route
    that's already survived its revalidation window) drops that
    requirement and relies on cross-track distance alone. Found via
    backtest reproduction: the strict progress check also flags a GENUINE
    diversion that simply continues in a straight line past its filed
    destination toward a further alternate — e.g. a missed approach
    continuing on to another airport instead of holding (see
    backtest_cases.py's ATL->MCO->FLL case). Cross-track distance stays ~0
    for that pattern (it's not a lateral deviation at all), yet the
    progress ratio clears the threshold once the overrun passes a modest
    fraction of the route's own length — and grows UNboundedly the further
    a real diversion continues, so no fixed progress threshold can ever be
    both loose enough to tolerate a real overflight-diversion and tight
    enough to reject bad route data at the same ratio (production evidence
    above: AAL974/SWA2820's bad-data ratio of 1.33-1.38x is comfortably
    BELOW what ATL->MCO->FLL's legitimate overflight case reaches, 1.88x).
    That's why this is check_progress=False rather than just a looser
    multiplier: no single number can do that job forever, so the ongoing
    check gives up on progress entirely and leans on cross-track distance,
    which a genuine diversion (still moving toward some real destination)
    doesn't blow past the way a wrong route match does."""
    route_len = haversine_nm(origin_lat, origin_lon, dest_lat, dest_lon)
    if route_len < 1:
        return False
    xtd = cross_track_distance_nm(origin_lat, origin_lon, dest_lat, dest_lon, cur_lat, cur_lon)
    if xtd > route_len:
        return False
    if not check_progress:
        return True
    dist_from_origin = haversine_nm(origin_lat, origin_lon, cur_lat, cur_lon)
    dist_to_dest = haversine_nm(cur_lat, cur_lon, dest_lat, dest_lon)
    return dist_from_origin + dist_to_dest <= route_len * ROUTE_PLAUSIBLE_PROGRESS_MULTIPLIER


# Hoeveel van de gefilede route we het toestel ZELF moeten hebben zien
# afleggen voordat we de route als door eigen waarneming gecorroboreerd
# beschouwen. 0.30 = het toestel is minstens 30% dichter bij de gefilede
# bestemming gekomen dan het vertrekpunt lag, zonder dat de afgelegde weg meer
# dan ROUTE_PLAUSIBLE_PROGRESS_MULTIPLIER x de directe afstand werd.
#
# Ruim gekozen: een verkeerd gematchte schedule (de gemeten hoofdfoutbron van
# dit systeem — zie CHECKPOINT.md bevinding 2) wijst naar een bestemming waar
# het toestel juist NIET naartoe vliegt, dus daar daalt de resterende afstand
# helemaal niet en is 30% al onbereikbaar. Hoger zetten zou alleen echte
# vroege diversies onnodig ongecorroboreerd laten; die worden juist door de
# tweede corroboratieweg (zelf waargenomen vertrek vanaf het gefilede
# vertrekpunt) opgevangen.
ROUTE_CORROBORATION_MIN_PROGRESS = 0.30

# Hoe dicht bij de gefilede grootcirkel het toestel op dat moment moet zitten
# voordat "dichterbij gekomen" ook echt "de gefilede route gevlogen" betekent.
#
# Dit is de voorwaarde die ontbrak, en zonder haar leverde de hele toets geen
# informatie op. Het commentaar hierboven nam aan dat een verkeerd gematchte
# schedule 30% voortgang "vrijwel nooit" haalt. Dat klopt niet: vliegt het
# toestel in werkelijkheid naar A terwijl D gefiled staat, en ligt A onder een
# hoek theta van D gezien vanaf het vertrekpunt, dan komt het tot op
# L*sin(theta) van D. De voortgangseis alleen corroboreert dus ELKE hoekfout tot
# 44 graden — en omdat route_plausible er al vóór staat en de wild verkeerde
# routes wegfiltert, is dat precies de verzameling die overblijft. Doorgerekend:
# 21 van 28 combinaties waren een volledig vals-positiefpad (route fout,
# corridor_deviation vuurt, route toch "gecorroboreerd"). Zie CHECKPOINT.md
# bevinding 16.
#
# 2% van de routelengte met een ondergrens van 25nm. De ondergrens houdt korte
# routes werkbaar (2% van 288nm is 6nm, krapper dan gewone ATC-vectoring); de 2%
# houdt lange routes streng. Gekozen vanaf de KANT VAN DE FOUT en niet vanaf de
# echte cases: backtest_cases.py interpoleert grootcirkels, dus die halen per
# constructie ~0% en bewijzen niets over echte windgeoptimaliseerde routes.
ROUTE_CORROBORATION_MAX_XTD_PCT = 0.02
ROUTE_CORROBORATION_MAX_XTD_FLOOR_NM = 25.0


def route_corroborated_by_progress(origin_lat, origin_lon, dest_lat, dest_lon,
                                   cur_lat, cur_lon) -> bool:
    """True als we dit toestel de gefilede route daadwerkelijk een substantieel
    stuk hebben zien afleggen — onze eigen waarneming als tweede mening over de
    enkele, crowdsourced schedulebron waar de route vandaan komt.

    Bewust het POSITIEVE spiegelbeeld van route_plausible: die filtert alleen
    ONmogelijke routes weg ("niet aantoonbaar fout"), wat iets heel anders is
    dan bevestiging ("aantoonbaar gevlogen"). Dat verschil is precies waar
    incidents.py's BEVESTIGD-poort op steunt: zolang de gefilede bestemming
    alleen onweersproken is en niet bevestigd, blijft "de route klopt niet"
    een redelijke alternatieve verklaring voor élk route-afhankelijk signaal
    tegelijk."""
    route_len = haversine_nm(origin_lat, origin_lon, dest_lat, dest_lon)
    if route_len < 1:
        return False
    if not route_plausible(origin_lat, origin_lon, dest_lat, dest_lon,
                           cur_lat, cur_lon, check_progress=True):
        return False
    dist_to_dest = haversine_nm(cur_lat, cur_lon, dest_lat, dest_lon)
    if dist_to_dest > route_len * (1.0 - ROUTE_CORROBORATION_MIN_PROGRESS):
        return False
    # ... en het toestel moet op dat moment ook daadwerkelijk OP de gefilede
    # corridor zitten. Zonder deze regel is "dichter bij D gekomen" verenigbaar
    # met een vlucht naar een heel ander veld dat toevallig dezelfde kant op
    # ligt — zie ROUTE_CORROBORATION_MAX_XTD_PCT.
    max_xtd = max(ROUTE_CORROBORATION_MAX_XTD_FLOOR_NM,
                  route_len * ROUTE_CORROBORATION_MAX_XTD_PCT)
    return cross_track_distance_nm(origin_lat, origin_lon, dest_lat, dest_lon,
                                   cur_lat, cur_lon) <= max_xtd


def _intermediate_point(lat1, lon1, lat2, lon2, fraction):
    """Point along the great circle from (lat1,lon1) to (lat2,lon2), at the
    given fraction of angular distance (0=start, 1=end)."""
    p1, l1, p2, l2 = (math.radians(x) for x in (lat1, lon1, lat2, lon2))
    d = 2 * math.asin(math.sqrt(math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin((l2 - l1) / 2) ** 2))
    if d == 0:
        return (lat1, lon1)
    a = math.sin((1 - fraction) * d) / math.sin(d)
    b = math.sin(fraction * d) / math.sin(d)
    x = a * math.cos(p1) * math.cos(l1) + b * math.cos(p2) * math.cos(l2)
    y = a * math.cos(p1) * math.sin(l1) + b * math.cos(p2) * math.sin(l2)
    z = a * math.sin(p1) + b * math.sin(p2)
    return (math.degrees(math.atan2(z, math.sqrt(x * x + y * y))), math.degrees(math.atan2(y, x)))


def cross_track_distance_nm(start_lat, start_lon, end_lat, end_lon, point_lat, point_lon) -> float:
    """Perpendicular distance (nm) of `point` from the great-circle path
    start->end. Standard aviation cross-track-distance formula (see e.g. Ed
    Williams' Aviation Formulary). Unsigned: just the magnitude of the
    sideways offset from the direct route, regardless of which side."""
    d13 = haversine_nm(start_lat, start_lon, point_lat, point_lon) / EARTH_RADIUS_NM
    brng13 = math.radians(bearing_deg(start_lat, start_lon, point_lat, point_lon))
    brng12 = math.radians(bearing_deg(start_lat, start_lon, end_lat, end_lon))
    dxt = math.asin(math.sin(d13) * math.sin(brng13 - brng12)) * EARTH_RADIUS_NM
    return abs(dxt)
