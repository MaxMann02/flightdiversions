"""Optional, OFF BY DEFAULT single-flight confirmation via the unofficial
FlightRadar24 API (JeanExtreme002/FlightRadarAPI on PyPI).

Why off by default: FR24's terms of service permit personal use of their own
site/app, but not scripted/reverse-engineered access to their internal
endpoints — this library works by mimicking those endpoints. Using it risks
your IP getting rate-limited or blocked by FR24; it is not illegal, but it
is against their stated terms. Live-tested for this project: it also has
real reliability problems (empty responses, encoding warnings) that suggest
active anti-bot friction, on top of the ToS question.

Given that, this module is called AT MOST once per already-flagged MOGELIJK
event (a handful of times per hour, not a bulk feed), only if a user
explicitly opts in via config.json's `fr24_confirm_enabled`. It never gates
our own alerts — it can only ADD a confirmation line if FR24 agrees, and
silently does nothing if unavailable, disabled, or in disagreement.
"""

import asyncio
import logging

log = logging.getLogger("fr24_confirm")

_fr = None


def _get_client():
    global _fr
    if _fr is None:
        from FlightRadarAPI import FlightRadar24API
        _fr = FlightRadar24API()
    return _fr


def _blocking_confirm(lat: float, lon: float, callsign: str) -> dict | None:
    fr = _get_client()
    bounds = fr.get_bounds_by_point(lat, lon, 300000)  # 300km radius, in meters
    flights = fr.get_flights(bounds=bounds)
    callsign = (callsign or "").strip().upper()
    for f in flights:
        if (f.callsign or "").strip().upper() == callsign:
            return fr.get_flight_details(f)
    return None


async def confirm_diversion(lat: float, lon: float, callsign: str) -> str | None:
    """Returns a short confirmation string if FR24's own status classifies
    this flight as diverted/redirected, else None (covers: disagreement, FR24
    unreachable, flight not found, or any error — all treated the same way,
    since this is a bonus signal, never a blocking one)."""
    try:
        details = await asyncio.wait_for(asyncio.to_thread(_blocking_confirm, lat, lon, callsign), timeout=25)
    except Exception as e:
        log.warning("FR24 confirmation failed for %s: %s", callsign, e)
        return None

    if not details:
        return None
    status = details.get("status") or {}
    text = status.get("text") or ""
    generic_type = ((status.get("generic") or {}).get("status") or {}).get("type") or ""
    if "divert" in text.lower() or "divert" in generic_type.lower():
        return f"FR24 bevestigt: {text}"
    return None
