import logging

import aiohttp

log = logging.getLogger("notifier")

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

EMOJI = {"BEVESTIGD": "\U0001F6A8", "WAARSCHIJNLIJK": "⚠️", "MOGELIJK": "\U0001F440"}


def format_incident_transition(t: dict) -> str:
    """t: a transition dict returned by incidents.IncidentManager.step()
    ({"incident", "kind", "old_state", "new_state", "ts"}). Replaces the
    old per-Event format_message — MASTERPLAN.md sectie 3.6: Telegram now
    fires only on incident state transitions (first reaching WAARSCHIJNLIJK/
    BEVESTIGD, or a stand-down close from either), not on every raw
    detector hit."""
    inc = t["incident"]
    kind = t["kind"]
    callsign = inc.get("callsign") or inc["hex"]
    link = f"https://globe.adsbexchange.com/?icao={inc['hex']}"
    if kind == "escalation":
        emoji = EMOJI.get(inc["state"], "")
        age_min = max(0, int((t["ts"] - inc["opened_ts"]) / 60))
        header = f"{emoji} <b>{inc['state']}</b> — {callsign}"
        body = f"score {inc['score']:.0f}, sinds {age_min}min in de gaten gehouden"
    elif kind == "stand_down":
        header = f"✅ <b>Vals alarm afgesloten</b> — {callsign}"
        body = f"was {t['old_state']}, {inc.get('resolution_reason') or 'opgelost'}"
    else:  # "closed" — e.g. landed/timeout after having been LIKELY/BEVESTIGD
        header = f"ℹ️ <b>Afgesloten</b> — {callsign}"
        body = f"was {t['old_state']} -> {t['new_state']}: {inc.get('resolution_reason') or ''}"
    return f"{header}\n{body}\n<a href=\"{link}\">bekijk op de kaart</a>"


async def send_telegram(session: aiohttp.ClientSession, token: str, chat_id: str, text: str) -> bool:
    if not token or not chat_id:
        log.warning("Telegram not configured, skipping send: %s", text)
        return False
    url = TELEGRAM_API.format(token=token)
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                body = await resp.text()
                log.error("Telegram send failed (%s): %s", resp.status, body)
                return False
            return True
    except aiohttp.ClientError as e:
        log.error("Telegram send error: %s", e)
        return False


async def notify_incident_transition(session: aiohttp.ClientSession, cfg: dict, t: dict):
    text = format_incident_transition(t)
    ok = await send_telegram(session, cfg["telegram_bot_token"], cfg["telegram_chat_id"], text)
    if not ok:
        log.info("INCIDENT TRANSITIE (niet afgeleverd naar telegram): %s", text.replace("\n", " | "))
