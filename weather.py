import logging
import time

import aiohttp

log = logging.getLogger("weather")

METAR_URL = "https://aviationweather.gov/api/data/metar"
CACHE_TTL_SECONDS = 900  # METARs are issued roughly hourly; 15min is plenty fresh

_cache: dict[str, tuple[float, dict | None]] = {}


async def get_metar(session: aiohttp.ClientSession, icao: str) -> dict | None:
    """Current METAR for an airport, cached. Returns None if unavailable —
    weather is corroborating context, never a hard requirement."""
    icao = (icao or "").strip().upper()
    if not icao:
        return None

    cached = _cache.get(icao)
    if cached and time.time() - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    result = None
    try:
        async with session.get(METAR_URL, params={"ids": icao, "format": "json"},
                                timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                if data:
                    result = data[0]
    except Exception as e:
        log.warning("METAR fetch failed for %s: %s", icao, e)

    _cache[icao] = (time.time(), result)
    return result


def describe(metar: dict | None) -> str:
    """Short human-readable weather note for an alert message, or '' if
    nothing noteworthy / no data."""
    if not metar:
        return ""
    cat = metar.get("fltCat")  # VFR / MVFR / IFR / LIFR
    if cat in ("IFR", "LIFR"):
        return f" [weer op {metar.get('icaoId')}: {cat}, mogelijk reden — {metar.get('rawOb', '')}]"
    wspd = metar.get("wspd")
    if isinstance(wspd, (int, float)) and wspd >= 30:
        return f" [weer op {metar.get('icaoId')}: harde wind {wspd}kt, mogelijk reden — {metar.get('rawOb', '')}]"
    return ""
