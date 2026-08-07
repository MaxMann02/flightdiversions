import asyncio
import logging
import time

import aiohttp

log = logging.getLogger("providers")

ADSBLOL_BASE = "https://api.adsb.lol/v2"
AIRPLANESLIVE_BASE = "https://api.airplanes.live/v2"
ADSBDB_BASE = "https://api.adsbdb.com/v0"

EMERGENCY_SQUAWKS = ("7700", "7600", "7500")
# Conspicuity/no-discrete-assignment codes (1000 confirmed live 2026-08-06,
# 2000 included by the same VFR-conspicuity convention but not separately
# live-verified — see MASTERPLAN.md sectie 14 for that caveat). An aircraft
# squawking one of these was never individually assigned an emergency code
# by ATC, so a non-'none' `emergency` status field alongside one of these is
# much more likely a decode/reporting artifact than a real declaration —
# see cross_provider_confirms_emergency's docstring and MASTERPLAN.md
# sectie 1c/7 for the live evidence (AUA96J/AUA12K/CFG6UA).
CONSPICUITY_SQUAWKS = ("1000", "2000")

# adsb.lol tolerates a large radius and effectively returns "everything within
# range" rather than strictly enforcing 250nm (verified live: dist=6000 from
# (20,0) returned ~12.5k aircraft, roughly half the globe). Two calls centered
# on opposite sides of the globe give worldwide coverage in two requests.
WORLD_TILES = [(20.0, 0.0), (20.0, 180.0)]
WORLD_RADIUS_NM = 6000


def _normalize_aircraft(raw: dict) -> dict:
    alt_baro = raw.get("alt_baro")
    on_ground = alt_baro == "ground"
    emergency = raw.get("emergency")
    if emergency == "reserved":
        # DO-260B section 2.2.3.2.7.8.1.1 defines 'reserved' as an
        # unassigned value — it can never represent a real pilot-declared
        # status. Live-confirmed as a real false positive (2026-08-06):
        # AUA96J squawked 1000 with emergency='reserved' and reached
        # BEVESTIGD/CONFIRMED on the dashboard because nothing else in the
        # pipeline treated this value as meaningless. Normalize once, here,
        # so every downstream consumer (detect_emergency, cross-provider
        # checks, future incident scoring) sees it exactly like 'none'
        # instead of each having to special-case it. See MASTERPLAN.md
        # sectie 1c/7.
        emergency = "none"
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
        "emergency": emergency,
        # ADS-B emitter category (DO-260B, e.g. A1/A2=light/small GA,
        # A3-A5=airliner-scale, A7=rotorcraft, B*=glider/UAV/ultralight/...)
        # and dbFlags (bit0=military, bit1=interesting, bit2=PIA, bit3=LADD)
        # — both already present in every adsb.lol/airplanes.live response,
        # just never read until now. Used by classify.py. See
        # MASTERPLAN.md sectie 2.1/4.
        "category": raw.get("category"),
        "db_flags": raw.get("dbFlags"),
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


async def cross_provider_confirms_emergency(session: aiohttp.ClientSession, hex_id: str) -> bool | None:
    """Soft corroboration specifically for the ADS-B 'emergency status'
    subfield (nordo/lifeguard/minfuel/downed/unlawful/general — the
    non-'none' values of the `emergency` field), as opposed to the
    7700/7600/7500 squawk codes, which stay fully trusted on their own
    (a deliberate 4-digit pilot action, not something decode noise
    produces). Live-tested: 4 real events in one session (EWG3BZ/EWG45B/
    ITY067/a fourth), all squawking 1000 specifically, all reporting a
    serious emergency status (nordo/lifeguard) that had no other
    corroborating signal and didn't hold up — squawk 1000 is a normal
    Mode-S conspicuity code used where ATC doesn't assign a discrete
    squawk (common in parts of Europe), not an emergency code, so this
    looks like a real decode/reporting quirk tied to that code path
    specifically, not a one-off. Checks whether airplanes.live's own view
    of this aircraft ALSO reports a non-'none' emergency status. Returns
    None if the second provider has no data at all (unreachable/not
    found) — caller should treat that as 'unconfirmed, not disproven',
    same convention as cross_provider_agrees."""
    ac = await fetch_single_aircraft(session, hex_id, base=AIRPLANESLIVE_BASE)
    if ac is None:
        return None
    emergency = ac.get("emergency")
    return bool(emergency) and emergency != "none"


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
# Separate again from both of the above: when adsbdb was reachable and gave
# a DEFINITIVE "no route for this callsign" (not a comm failure), the
# timestamp goes here instead of being cached forever. adsbdb is
# crowdsourced and grows over time, so "no data today" shouldn't mean "no
# data ever" — see route_lookup_negative_retry_due and MASTERPLAN.md
# sectie 5, punt 4.
_route_cache_negative_at: dict[str, float] = {}


def route_lookup_pending_retry(callsign: str) -> bool:
    """True if the callsign's last lookup attempt failed transiently and is
    still within its retry cooldown — callers should treat this as 'not yet
    resolved, try again later' rather than a stable, cacheable answer."""
    callsign = (callsign or "").strip().upper()
    failed_at = _route_cache_failed_at.get(callsign)
    return failed_at is not None and time.monotonic() - failed_at < ROUTE_LOOKUP_RETRY_COOLDOWN_S


def route_lookup_negative_retry_due(callsign: str, cfg: dict) -> bool:
    """True if the callsign's cached result is a genuine (non-transient) 'no
    route found' that is now old enough to retry (route_lookup_negative_
    retry_hours). False if a real route is already cached (nothing to
    retry) or if it was never definitively resolved as negative in the
    first place (e.g. still pending a transient-failure retry, or never
    looked up at all — route_lookup_pending_retry / a fresh lookup handle
    those cases)."""
    callsign = (callsign or "").strip().upper()
    if _route_cache.get(callsign) is not None:
        return False
    ts = _route_cache_negative_at.get(callsign)
    if ts is None:
        return False
    return time.monotonic() - ts >= cfg["route_lookup_negative_retry_hours"] * 3600


async def lookup_route(session: aiohttp.ClientSession, callsign: str, cfg: dict) -> dict | None:
    """Tier 2: adsbdb callsign -> expected origin/destination. A resolved
    route is cached essentially forever (these don't change). A genuine
    negative result is retried after route_lookup_negative_retry_hours
    rather than forever — see route_lookup_negative_retry_due."""
    callsign = (callsign or "").strip().upper()
    if not callsign:
        return None
    if callsign in _route_cache:
        cached = _route_cache[callsign]
        if cached is not None or not route_lookup_negative_retry_due(callsign, cfg):
            return cached
        # else: cached negative result is due for a retry — fall through
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
    if route is None:
        _route_cache_negative_at[callsign] = time.monotonic()
    else:
        _route_cache_negative_at.pop(callsign, None)
    return route


HEXDB_BASE = "https://hexdb.io/api/v1"
_hexdb_route_cache: dict[str, tuple | None] = {}
_hexdb_route_cache_checked_at: dict[str, float] = {}
_hexdb_aircraft_cache: dict[str, dict | None] = {}
_hexdb_aircraft_cache_checked_at: dict[str, float] = {}
# hexdb.io doesn't clearly distinguish "no data for this callsign/hex" from
# a transient error in its response shape (both were only confirmed to
# return a non-200/error body, indistinguishable from a network hiccup
# without a documented contract to rely on) — so unlike adsbdb.com above, no
# long-term negative cache is kept here; every non-success is retried on
# this short cadence regardless of cause. See MASTERPLAN.md sectie 5.
HEXDB_RETRY_COOLDOWN_S = ROUTE_LOOKUP_RETRY_COOLDOWN_S
HEXDB_AIRCRAFT_CACHE_TTL_S = 24 * 3600  # aircraft ownership/type essentially never changes


async def lookup_route_hexdb(session: aiohttp.ClientSession, callsign: str) -> tuple | None:
    """Tier 2b fallback: hexdb.io callsign -> (origin_icao, destination_icao).
    A second, independent, free/keyless route source, used by main.py only
    for callsigns lookup_route (adsbdb.com) had nothing for. hexdb.io
    returns only an ICAO pair, not coordinates — the caller resolves those
    via AirportDB. See MASTERPLAN.md sectie 2.2/5."""
    callsign = (callsign or "").strip().upper()
    if not callsign:
        return None
    checked_at = _hexdb_route_cache_checked_at.get(callsign)
    if checked_at is not None and time.monotonic() - checked_at < HEXDB_RETRY_COOLDOWN_S:
        return _hexdb_route_cache.get(callsign)

    data = await _get_json(session, f"{HEXDB_BASE}/route/icao/{callsign}")
    result = None
    if isinstance(data, dict):
        route_str = data.get("route")
        if isinstance(route_str, str) and "-" in route_str:
            origin, _, dest = route_str.partition("-")
            origin, dest = origin.strip().upper(), dest.strip().upper()
            if origin and dest:
                result = (origin, dest)
    _hexdb_route_cache[callsign] = result
    _hexdb_route_cache_checked_at[callsign] = time.monotonic()
    return result


async def lookup_aircraft_hexdb(session: aiohttp.ClientSession, hex_id: str) -> dict | None:
    """hexdb.io aircraft-by-hex lookup: registration, ICAO type code,
    registered owner/operator (e.g. {"RegisteredOwners":"Ryanair",
    "OperatorFlagCode":"RYR", "ICAOTypeCode":"B738", ...}). Used by
    classify.refine_with_hexdb to recognize a commercial operator even for
    callsigns/categories classify() alone couldn't place confidently. See
    MASTERPLAN.md sectie 2.2/4."""
    hex_id = (hex_id or "").strip().lower()
    if not hex_id:
        return None
    checked_at = _hexdb_aircraft_cache_checked_at.get(hex_id)
    if checked_at is not None and time.monotonic() - checked_at < HEXDB_AIRCRAFT_CACHE_TTL_S:
        return _hexdb_aircraft_cache.get(hex_id)

    data = await _get_json(session, f"{HEXDB_BASE}/aircraft/{hex_id}")
    result = data if isinstance(data, dict) and "error" not in data else None
    _hexdb_aircraft_cache[hex_id] = result
    _hexdb_aircraft_cache_checked_at[hex_id] = time.monotonic()
    return result
