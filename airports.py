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


EARTH_RADIUS_NM = 3440.065


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

    check_progress (default True, used when a route is FIRST resolved in
    main.py) also requires along-route progress to add up (dist_from_origin
    + dist_to_dest <= 1.5x route length) — reasonable at resolution time,
    since a genuinely-matched route shouldn't already show a huge detour on
    its very first data point.

    check_progress=False (used for main.py's ONGOING mid-flight recheck of
    an already-trusted route) drops that requirement and relies on
    cross-track distance alone. Found via backtest reproduction: the strict
    progress check also flags a GENUINE diversion that simply continues in
    a straight line past its filed destination toward a further alternate —
    e.g. a missed approach continuing on to another airport instead of
    holding (see backtest_cases.py's ATL->MCO->FLL case). Cross-track
    distance stays ~0 for that pattern (it's not a lateral deviation at
    all), yet the progress ratio clears 1.5x once the overrun passes just
    25% of the route's own length — trips easily on short/medium routes.
    Using that to strip an already-trusted route mid-flight cost more real
    detections (corridor_deviation/premature_descent/holding_pattern/
    landed_wrong_airport all depend on track.route, and nothing ever resets
    route_checked back to False once main.py's recheck clears it) than the
    bad-callsign-data cases the check was protecting against. Cross-track
    distance alone still catches that original motivating case (a callsign
    collision onto a totally different flight's route, e.g. the documented
    Portland->SF example above) — an unrelated flight is normally nowhere
    near the filed route's line, not just further along it."""
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
    return dist_from_origin + dist_to_dest <= route_len * 1.5


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


def great_circle_max_latitude(lat1, lon1, lat2, lon2) -> float:
    """Highest latitude the direct great-circle path between two points
    reaches."""
    fractions = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
    lats = [lat1, lat2] + [_intermediate_point(lat1, lon1, lat2, lon2, f)[0] for f in fractions]
    return max(lats)


def crosses_russian_airspace_zone(lat1, lon1, lat2, lon2, min_lat=55.0, lon_min=20.0, lon_max=170.0) -> bool:
    """Whether the direct great-circle path passes through the high-latitude
    Russia/Siberia longitude band, where real-world routing has legitimately
    bowed 1500-2000nm+ off the direct line since 2022 (Western carriers can't
    overfly Russia). This replaced a plain 'max latitude' check: live-tested,
    that blunter version also excluded Salt Lake City->Amsterdam (peaks 64N
    but stays entirely over Canada/Greenland, longitude -99 to -16 — nowhere
    near Russia) from a route that had a real, large, legitimate-to-flag
    454nm diversion. Checking latitude AND longitude together targets the
    actual physical cause instead of 'any route that goes far north'."""
    fractions = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
    points = [(lat1, lon1), (lat2, lon2)] + [_intermediate_point(lat1, lon1, lat2, lon2, f) for f in fractions]
    return any(lat >= min_lat and lon_min <= lon <= lon_max for lat, lon in points)


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
