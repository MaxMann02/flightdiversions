import json
import os

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

_DEFAULTS = {
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "tier0_interval_seconds": 15,
    "tier1_interval_seconds": 60,
    "alert_cooldown_seconds": 1800,

    "course_deviation_deg": 90,

    # Corridor (cross-track) deviation: absolute floor and % of route length,
    # whichever is larger. Must hold for this many consecutive tier1 samples.
    "corridor_deviation_min_nm": 80,
    "corridor_deviation_pct": 0.10,
    "corridor_deviation_max_nm": 250,
    "corridor_deviation_min_samples": 3,
    # Skip corridor-deviation for routes whose direct great-circle path
    # crosses the Russia/Siberia high-latitude band (see
    # crosses_russian_airspace_zone). Backtested: a plain max-latitude cutoff
    # was tried first and rejected — it also excluded Salt Lake City->
    # Amsterdam (peaks 64N over Canada/Greenland, nothing to do with Russia)
    # from a route with a real, legitimately-flaggable 454nm diversion.
    #
    # That skip only holds while the CURRENT heading is still within this
    # many degrees of the bearing to the filed destination — a legitimate
    # Russia-avoidance bow keeps making broad progress toward the
    # destination throughout, a real diversion's heading stops pointing
    # there at all. Backtested against Emirates EK225 (DXB->SFO, diverted
    # to LHR over Nyagan, 29 Jul 2026): the turn-away heading was ~85-90°
    # off the bearing to SFO, so this needs real margin below 90° to
    # actually catch it — set conservatively above typical bow angles
    # rather than tuned tight to that one case.
    "corridor_deviation_bow_heading_deg": 60,

    # Premature descent: sustained descent of at least this many feet, while
    # still further from the destination than `multiplier`x the standard
    # ~3nm-per-1000ft top-of-descent rule of thumb would predict.
    "premature_descent_min_drop_ft": 4000,
    "premature_descent_multiplier": 3.0,
    "premature_descent_samples": 4,

    # Holding pattern: total heading rotation (deg) across this many samples,
    # while staying within max_radius_nm of the window's centroid.
    "holding_pattern_samples": 5,
    "holding_pattern_min_rotation_deg": 270,
    "holding_pattern_max_radius_nm": 15,
    # Holding near the actual planned destination is normal for a while
    # (arrival sequencing) — only flag it once sustained far longer than
    # that. Backtested against a real case (AI850 Pune->Delhi: held near
    # Delhi 1hr+ before a fuel-emergency diversion to Gwalior) that a blanket
    # exclusion made invisible; this fires well before the 1hr mark without
    # tripping on normal 5-15 min sequencing holds.
    "holding_pattern_destination_min_streak": 20,

    # Minimum times we must have personally observed a callsign land at a
    # given airport before trusting it as a known route (see db.py).
    "learned_route_min_times_seen": 2,

    # detect_landed_wrong_airport normally excludes "landed back at the
    # filed ORIGIN" from wrong_airport — small regional/commuter operators
    # reuse one callsign across multiple legs per day, so that's usually
    # just adsbdb reflecting an earlier/different leg, not a diversion. But
    # if we personally watched this exact aircraft take off within this many
    # minutes, there hasn't been time for a different leg — it's a genuine
    # early-return diversion instead (engine issue, medical, etc. shortly
    # after departure), which would otherwise go completely undetected: a
    # low, early turnback typically clears neither course_deviation's
    # >=18,000ft cruise floor nor holding_pattern's destination-airport
    # streak requirement. 45min comfortably covers a real turnback while
    # staying well short of genuine multi-leg reuse (hours apart, not tens
    # of minutes).
    "early_return_max_minutes": 45,

    # Signal-lost-near-airport: how many consecutive missed tier1 cycles
    # before treating a track as "gone" and checking whether it disappeared
    # low and close to a non-destination airport (found via backtesting the
    # UA2078/Luke AFB case — coverage often drops before touchdown near
    # small/military fields).
    "signal_lost_missing_cycles": 3,
    "signal_lost_max_altitude_ft": 10000,
    "signal_lost_search_km": 40,

    "weather_enrichment_enabled": True,
    "cross_provider_consensus_enabled": True,

    # Off by default: see fr24_confirm.py for the ToS/reliability tradeoffs.
    "fr24_confirm_enabled": False,
}


def _coerce_env(raw: str, default):
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw


def load_config() -> dict:
    cfg = dict(_DEFAULTS)
    if os.path.exists(_CONFIG_PATH):
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    # Environment variables override both the defaults and config.json, one
    # per key (TELEGRAM_BOT_TOKEN, TIER0_INTERVAL_SECONDS, ...). config.json
    # is gitignored and shouldn't be committed anywhere — most hosting
    # platforms have no way to hand you a file at deploy time, only env
    # vars set through their dashboard/CLI, so this is how secrets (the
    # Telegram token/chat_id) reach a deployed instance.
    for key, default in _DEFAULTS.items():
        raw = os.environ.get(key.upper())
        if raw is not None:
            cfg[key] = _coerce_env(raw, default)
    return cfg


CONFIG = load_config()
