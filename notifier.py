import logging

import aiohttp

from detector import Event

log = logging.getLogger("notifier")

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

EMOJI = {"BEVESTIGD": "\U0001F6A8", "WAARSCHIJNLIJK": "⚠️", "MOGELIJK": "\U0001F440"}


def format_message(ev: Event) -> str:
    emoji = EMOJI.get(ev.confidence, "")
    callsign = ev.callsign or ev.hex
    link = f"https://globe.adsbexchange.com/?icao={ev.hex}"
    return (
        f"{emoji} <b>{ev.confidence}</b> — {callsign}\n"
        f"{ev.message}\n"
        f"<a href=\"{link}\">bekijk op de kaart</a>"
    )


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


async def notify(session: aiohttp.ClientSession, cfg: dict, ev: Event):
    text = format_message(ev)
    ok = await send_telegram(session, cfg["telegram_bot_token"], cfg["telegram_chat_id"], text)
    if not ok:
        log.info("ALERT (not delivered to telegram): %s", text.replace("\n", " | "))
