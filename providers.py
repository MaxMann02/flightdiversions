import asyncio
import logging
import time

import aiohttp

log = logging.getLogger("providers")

ADSBLOL_BASE = "https://api.adsb.lol/v2"
AIRPLANESLIVE_BASE = "https://api.airplanes.live/v2"
ADSBDB_BASE = "https://api.adsbdb.com/v0"

EMERGENCY_SQUAWKS = ("7700", "7600", "7500")

# adsb.lol tolerates a large radius and effectively returns "everything within
# range" rather than strictly enforcing 250nm (verified live: dist=6000 from
# (20,0) returned ~12.5k aircraft, roughly half the globe). Two calls centered
# on opposite sides of the globe give worldwide coverage in two requests.
WORLD_TILES = [(20.0, 0.0), (20.0, 180.0)]
WORLD_RADIUS_NM = 6000


def _normalize_aircraft(raw: dict) -> dict:
    alt_baro = raw.get("alt_baro")
    on_ground = alt_baro == "ground"
    return {
        "hex": raw.get("hex", "").lower(),
        "callsign": (raw.get("flight") or "").strip(),
        "registration": raw.get("r"),
        "type": raw.get("t"),
        "lat": raw.get("lat"),
        "lon": raw.get("lon"),
        "alt_baro": None if on_ground else alt_baro,
        "on_ground": on_ground,
        "track": raw.get("track"),
        "gs": raw.get("gs"),
        "baro_rate": raw.get("baro_rate"),
        "squawk": raw.get("squawk"),
        "emergency": raw.get("emergency"),
    }


async def _get_json(session: aiohttp.ClientSession, url: str, **kwargs):
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20), **kwargs) as resp:
            if resp.status == 429:
                log.warning("rate limited: %s", url)
                return None
            if resp.status != 200:
                return None
            return await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("request failed %s: %s", url, e)
        return None


async def fetch_squawk_sweep(session: aiohttp.ClientSession) -> dict:
    """Tier 0: global sweep for declared-emergency squawks on both providers.
    Returns dict keyed by hex, deduped, providers overlap so we take the union."""
    tasks = []
    for base in (ADSBLOL_BASE, AIRPLANESLIVE_BASE):
        for code in EMERGENCY_SQUAWKS:
            tasks.append(_get_json(session, f"{base}/squawk/{code}"))
    results = await asyncio.gather(*tasks)

    out = {}
    for res in results:
        if not res:
            continue
        for raw in res.get("ac") or []:
            ac = _normalize_aircraft(raw)
            if ac["hex"]:
                out[ac["hex"]] = ac
    return out


async def fetch_world_snapshot(session: aiohttp.ClientSession) -> dict:
    """Tier 1: worldwide state-vector sweep via adsb.lol (two hemisphere calls)."""
    tasks = [
        _get_json(session, f"{ADSBLOL_BASE}/lat/{lat}/lon/{lon}/dist/{WORLD_RADIUS_NM}")
        for lat, lon in WORLD_TILES
    ]
    results = await asyncio.gather(*tasks)

    out = {}
    for res in results:
        if not res:
            continue
        for raw in res.get("ac") or []:
            ac = _normalize_aircraft(raw)
            if ac["hex"]:
                out[ac["hex"]] = ac
    return out


async def fetch_single_aircraft(session: aiohttp.ClientSession, hex_id: str, base: str = AIRPLANESLIVE_BASE) -> dict | None:
    """Single-aircraft lookup on one provider, used for cross-provider
    consensus checks on already-flagged candidates (cheap: only called for
    the handful of events per hour that clear our own detectors)."""
    data = await _get_json(session, f"{base}/hex/{hex_id}")
    if not data:
        return None
    ac_list = data.get("ac") or []
    if not ac_list:
        return None
    return _normalize_aircraft(ac_list[0])


async def cross_provider_agrees(session: aiohttp.ClientSession, hex_id: str, expected_on_ground: bool) -> bool:
    """Soft corroboration: checks airplanes.live's own view of this one
    aircraft (Tier 1's world snapshot is sourced from adsb.lol, so this is
    genuinely an independent second source). Only actively vetoes an alert
    if that provider explicitly disagrees on ground state; missing/
    unreachable data never blocks an alert on its own — a coverage gap in
    the second provider shouldn't suppress a real event the first already
    confirmed."""
    ac = await fetch_single_aircraft(session, hex_id, base=AIRPLANESLIVE_BASE)
    if ac is None:
        return True
    return ac.get("on_ground") == expected_on_ground


_route_cache: dict[str, dict | None] = {}
# Separate from _route_cache: tracks callsigns whose most recent lookup
# attempt failed to even reach adsbdb (timeout, connection error, non-200,
# 429 rate limit) rather than getting a definitive answer. Previously such a
# failure stored None straight into _route_cache forever, indistinguishable
# from adsbdb genuinely having no route for that callsign — one transient API
# hiccup then permanently blinded that callsign (and, since route_checked
# latches True per-track in main.py regardless of which kind of None it got,
# that specific flight's own track too) for the rest of the process's
# lifetime. Now a comm failure only blocks retries for this cooldown window.
_route_cache_failed_at: dict[str, float] = {}
ROUTE_LOOKUP_RETRY_COOLDOWN_S = 300


def route_lookup_pending_retry(callsign: str) -> bool:
    """True if the callsign's last lookup attempt failed transiently and is
    still within its retry cooldown — callers should treat this as 'not yet
    resolved, try again later' rather than a stable, cacheable answer."""
    callsign = (callsign or "").strip().upper()
    failed_at = _route_cache_failed_at.get(callsign)
    return failed_at is not None and time.monotonic() - failed_at < ROUTE_LOOKUP_RETRY_COOLDOWN_S


async def lookup_route(session: aiohttp.ClientSession, callsign: str) -> dict | None:
    """Tier 2: adsbdb callsign -> expected origin/destination. Cached (routes
    for a given scheduled callsign essentially never change)."""
    callsign = (callsign or "").strip().upper()
    if not callsign:
        return None
    if callsign in _route_cache:
        return _route_cache[callsign]
    if route_lookup_pending_retry(callsign):
        return None

    data = await _get_json(session, f"{ADSBDB_BASE}/callsign/{callsign}")
    if data is None:
        # Couldn't reach adsbdb at all — not the same as "no route exists
        # for this callsign". Don't cache it; just note the failure time so
        # we back off for the cooldown instead of hammering adsbdb every cycle.
        _route_cache_failed_at[callsign] = time.monotonic()
        return None

    route = None
    if isinstance(data.get("response"), dict):
        fr = data["response"].get("flightroute")
        if fr and fr.get("origin") and fr.get("destination"):
            route = {
                "origin_icao": fr["origin"]["icao_code"],
                "origin_lat": fr["origin"]["latitude"],
                "origin_lon": fr["origin"]["longitude"],
                "destination_icao": fr["destination"]["icao_code"],
                "destination_lat": fr["destination"]["latitude"],
                "destination_lon": fr["destination"]["longitude"],
                "airline": (fr.get("airline") or {}).get("name"),
            }
    _route_cache[callsign] = route
    _route_cache_failed_at.pop(callsign, None)
    return route
