"""Weer/luchtruim-context: gratis, key-loze SIGMET/CWA-polygonen van
aviationweather.gov (NOAA/NWS Aviation Weather Center), gebruikt om een
afwijking die samenvalt met actief gevaarlijk weer te herkennen als
waarschijnlijk routine-uitwijken in plaats van een echte diversie. Zie
MASTERPLAN.md sectie 6.1.

Live geverifieerde response-vorm (2026-08-06/07, zie MASTERPLAN.md sectie
2.3 en de code hieronder): airsigmet/isigmet geven coords als {"lat":
<float>, "lon": <float>}; cwa geeft coords als {"lat": "<string>", "lon":
"<string>"} — dat typeverschil wordt hier opgevangen met een simpele
float()-cast op beide.

Puntje-in-polygon is met opzet met de hand geschreven (ray-casting), geen
nieuwe dependency: requirements.txt heeft alleen aiohttp+certifi, en
airports.py doet hetzelfde voor haversine/bearing/cross-track-distance in
plaats van een geo-library te gebruiken.
"""
import logging
import time

import aiohttp

log = logging.getLogger("airspace")

AIRSIGMET_URL = "https://aviationweather.gov/api/data/airsigmet"
ISIGMET_URL = "https://aviationweather.gov/api/data/isigmet"
CWA_URL = "https://aviationweather.gov/api/data/cwa"

# TFRs (Temporary Flight Restrictions), MASTERPLAN.md sectie 2.4/6.3 —
# fase 4. Originally scoped as needing either FAA API registration
# (api.faa.gov, manual approval, not doable from an unattended session) or
# an unofficial community scraper of tfr.faa.gov. Neither turned out to be
# necessary: tfr.faa.gov's own public website (a Nuxt SPA) calls a plain
# GeoServer WFS endpoint that returns current TFR polygons as GeoJSON, no
# key/auth/registration, live-verified 2026-08-07 (see BACKTEST_LOG.md
# ronde 13) — found by loading the real page in a browser and reading its
# network requests, not by guessing URLs. Also confirmed working via a
# plain (non-browser) HTTP client with a generic User-Agent — no Akamai
# bot-block observed on this endpoint specifically, despite the site
# fronting other paths with Akamai (visible in the browser network log as
# /akam/... requests).
TFR_GEOSERVER_URL = "https://tfr.faa.gov/geoserver/TFR/ows"
TFR_GEOSERVER_PARAMS = {
    "service": "WFS", "version": "1.1.0", "request": "GetFeature",
    "typeName": "TFR:V_TFR_LOC", "outputFormat": "application/json",
    "srsname": "EPSG:4326",  # WGS84 lat/lon directly — avoids a manual
    # Web-Mercator (EPSG:3857, the browser UI's default) reprojection.
    "maxFeatures": "500",
}

_cache: dict[str, tuple[float, list]] = {}  # cache_key -> (fetched_at, parsed hazards)


def _parse_hazard(raw: dict) -> dict | None:
    coords = raw.get("coords")
    if not coords or len(coords) < 3:
        return None
    try:
        polygon = [(float(c["lat"]), float(c["lon"])) for c in coords]
    except (TypeError, ValueError, KeyError):
        return None
    # airsigmet/isigmet use altitudeLow1/altitudeHi1; cwa uses base/top.
    # Either being missing/null is treated as "no bound on this side" —
    # conservative in favor of still matching (a missed altitude filter
    # just means a real explanation isn't found, not that a real diversion
    # gets wrongly suppressed).
    alt_lo = raw.get("altitudeLow1", raw.get("base"))
    alt_hi = raw.get("altitudeHi1", raw.get("top"))
    return {
        "hazard": raw.get("hazard") or "?",
        "alt_lo_ft": alt_lo,
        "alt_hi_ft": alt_hi,
        "valid_from": raw.get("validTimeFrom"),
        "valid_to": raw.get("validTimeTo"),
        "polygon": polygon,
        "id": raw.get("seriesId") or raw.get("icaoId") or "?",
    }


async def _fetch_hazards(session: aiohttp.ClientSession, url: str, cache_key: str, ttl_s: float) -> list[dict]:
    cached = _cache.get(cache_key)
    if cached and time.time() - cached[0] < ttl_s:
        return cached[1]
    hazards = []
    try:
        async with session.get(url, params={"format": "json"}, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                if isinstance(data, list):
                    for raw in data:
                        h = _parse_hazard(raw)
                        if h:
                            hazards.append(h)
    except Exception as e:
        log.warning("airspace hazard fetch failed (%s): %s", cache_key, e)
        if cached:
            return cached[1]  # serve stale on a transient failure rather than nothing
    _cache[cache_key] = (time.time(), hazards)
    return hazards


def _parse_tfr_feature(feature: dict) -> dict | None:
    geom = feature.get("geometry") or {}
    if geom.get("type") != "Polygon":
        return None  # MultiPolygon/other rare shapes skipped — matches this project's "start simple" convention (see e.g. peer-consensus grid-bucketing)
    rings = geom.get("coordinates")
    if not rings or not rings[0] or len(rings[0]) < 3:
        return None
    try:
        # GeoJSON coordinate order is [lon, lat] — flip to this module's
        # (lat, lon) convention, matching _parse_hazard's SIGMET/CWA polygons.
        polygon = [(float(pt[1]), float(pt[0])) for pt in rings[0]]
    except (TypeError, ValueError, IndexError):
        return None
    props = feature.get("properties") or {}
    return {
        # TFRs don't carry structured altitude bounds in this feed (only
        # free-text in the NOTAM description, e.g. "SFC-3000FT") — treated
        # as unbounded (matches every SIGMET/CWA hazard with missing
        # altitude data: conservative in favor of still finding a real
        # explanation, see explain_position's docstring).
        "hazard": props.get("LEGAL") or "TFR",
        "alt_lo_ft": None,
        "alt_hi_ft": None,
        # TFR validity windows are free text in TITLE (e.g. "Wednesday,
        # July 29, 2026 through Tuesday, August 11, 2026 UTC") — not
        # structured, and format varies (UTC vs Local, date ranges).
        # Deliberately NOT parsed here (a real project for another round,
        # not a quick regex): valid_from/valid_to stay None, trusting that
        # the GeoServer view (V_TFR_LOC) itself already only returns
        # currently-active TFRs, the same way the public tfr.faa.gov map
        # does — get_active_hazards' own valid_from/valid_to filter is a
        # no-op for these entries either way (None passes).
        "valid_from": None,
        "valid_to": None,
        "polygon": polygon,
        "id": props.get("NOTAM_KEY") or feature.get("id") or "?",
        "title": props.get("TITLE"),
    }


async def _fetch_tfrs(session: aiohttp.ClientSession, ttl_s: float) -> list[dict]:
    cache_key = "tfr"
    cached = _cache.get(cache_key)
    if cached and time.time() - cached[0] < ttl_s:
        return cached[1]
    hazards = []
    try:
        async with session.get(TFR_GEOSERVER_URL, params=TFR_GEOSERVER_PARAMS,
                                timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                for feature in (data.get("features") or []):
                    h = _parse_tfr_feature(feature)
                    if h:
                        hazards.append(h)
    except Exception as e:
        log.warning("TFR fetch failed: %s", e)
        if cached:
            return cached[1]
    _cache[cache_key] = (time.time(), hazards)
    return hazards


async def get_active_hazards(session: aiohttp.ClientSession, cfg: dict) -> list[dict]:
    """All currently-active SIGMET (US + international) and CWA hazard
    polygons, refreshed at most every weather_sigmet_refresh_seconds (these
    don't change nearly as fast as aircraft positions, unlike METAR/aircraft
    data). Meant to be fetched once per tier1 cycle in main.py and passed
    into IncidentManager.step()/apply_context_checks() — kept synchronous
    there so incidents.py doesn't need its own network layer."""
    ttl = cfg["weather_sigmet_refresh_seconds"]
    sigmets = await _fetch_hazards(session, AIRSIGMET_URL, "airsigmet", ttl)
    isigmets = await _fetch_hazards(session, ISIGMET_URL, "isigmet", ttl)
    cwas = await _fetch_hazards(session, CWA_URL, "cwa", ttl)
    tfrs = await _fetch_tfrs(session, ttl) if cfg.get("notam_tfr_enabled") else []
    now = time.time()
    return [
        h for h in (sigmets + isigmets + cwas + tfrs)
        if (h["valid_from"] is None or h["valid_from"] <= now)
        and (h["valid_to"] is None or h["valid_to"] >= now)
    ]


def _point_in_polygon(lat: float, lon: float, polygon: list[tuple[float, float]]) -> bool:
    """Standard ray-casting point-in-polygon test. polygon: [(lat, lon), ...].
    Treats (lon, lat) as a flat (x, y) plane — fine at the scale of a
    single SIGMET/CWA polygon (never more than a few hundred nm across),
    where the equirectangular distortion doesn't matter for a purely
    topological inside/outside test."""
    n = len(polygon)
    inside = False
    x, y = lon, lat
    p1_lat, p1_lon = polygon[0]
    p1x, p1y = p1_lon, p1_lat
    for i in range(1, n + 1):
        p2_lat, p2_lon = polygon[i % n]
        p2x, p2y = p2_lon, p2_lat
        if y > min(p1y, p2y):
            if y <= max(p1y, p2y):
                if x <= max(p1x, p2x):
                    xinters = None
                    if p1y != p2y:
                        xinters = (y - p1y) * (p2x - p1x) / (p2y - p1y) + p1x
                    if p1x == p2x or (xinters is not None and x <= xinters):
                        inside = not inside
        p1x, p1y = p2x, p2y
    return inside


def explain_position(lat: float, lon: float, alt_ft: float | None, hazards: list[dict]) -> dict | None:
    """First active hazard (if any) whose polygon contains (lat, lon) and
    whose altitude band covers alt_ft. Returns the hazard dict (has
    'hazard', 'id', etc. — see _parse_hazard) or None. Missing altitude
    bounds on the hazard are treated as unbounded on that side."""
    for h in hazards:
        if alt_ft is not None:
            if h["alt_lo_ft"] is not None and alt_ft < h["alt_lo_ft"]:
                continue
            if h["alt_hi_ft"] is not None and alt_ft > h["alt_hi_ft"]:
                continue
        if _point_in_polygon(lat, lon, h["polygon"]):
            return h
    return None
