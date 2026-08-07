import json
import os

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

_DEFAULTS = {
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "tier0_interval_seconds": 15,
    "tier1_interval_seconds": 60,

    "course_deviation_deg": 90,

    # Corridor (cross-track) deviation: absolute floor and % of route length,
    # whichever is larger. Must hold for this many consecutive tier1 samples.
    "corridor_deviation_min_nm": 80,
    "corridor_deviation_pct": 0.10,
    "corridor_deviation_max_nm": 250,
    "corridor_deviation_min_samples": 3,
    # Bow-tolerance: corridor_deviation and course_deviation both suppress
    # a detected deviation while the CURRENT heading is still within this
    # many degrees of the bearing to the filed destination — a legitimate
    # routing bow (wind-optimized track, airway routing around restricted/
    # congested airspace, a published non-great-circle route) keeps making
    # broad progress toward the destination throughout; a real diversion's
    # heading stops pointing there at all. Backtested against Emirates
    # EK225 (DXB->SFO, diverted to LHR over Nyagan, 29 Jul 2026): the
    # turn-away heading was ~85-90° off the bearing to SFO, so this needs
    # real margin below 90° to actually catch it — set conservatively above
    # typical bow angles rather than tuned tight to that one case.
    #
    # Originally scoped only to routes crossing the Russia/Siberia
    # high-latitude band (a plain max-latitude cutoff was tried and
    # rejected first — it also excluded Salt Lake City->Amsterdam, which
    # peaks 64N over Canada/Greenland with nothing to do with Russia, from
    # a route with a real, legitimately-flaggable 454nm diversion).
    # Generalized to every route 2026-08-06 after live production data
    # showed the identical false-positive pattern well outside that zone
    # (AAL168 KPDX->KCLT 503nm off, ASH4002 KIAH->KOKC 85nm off, NOZ1865
    # LIBD->ENGM 152nm off — none had any other corroborating signal). See
    # MASTERPLAN.md sectie 1b/8.
    "corridor_deviation_bow_heading_deg": 60,

    # Premature descent: sustained descent of at least this many feet, while
    # still further from the destination than `multiplier`x the standard
    # ~3nm-per-1000ft top-of-descent rule of thumb would predict.
    # samples/min_drop_ft raised from 4/4000 (2026-08-07, BACKTEST_LOG.md
    # ronde 16) after live data showed the old, shorter window firing on
    # ordinary ATC-directed cruise step-downs: 44 different real, unrelated
    # aircraft worldwide in one ~20min window, each a lone non-repeating hit
    # (i.e. it never continued descending afterward — consistent with a
    # brief step-down that levels off, not a committed descent), with
    # dist_to_dest 3x-28x beyond the ALREADY-generous 3x multiplier
    # threshold. A distance-based cap can't fix this: EK225's own real,
    # sourced premature_descent case (backtest_cases.py) fires 4654nm from
    # its filed KSFO — thousands of nm further out than any of the live
    # noise cases — because that's the exact, intended signature of "the
    # filed destination is now irrelevant, this is a diversion", so
    # tightening the distance/multiplier would have broken real detection,
    # not just noise. A committed real descent (EK225 sustains ~1575ft/min
    # for 20+ consecutive minutes) easily clears a longer window; a brief
    # ATC step-down (typically executes over 1-3min then levels off, which
    # already breaks the existing "strict continuous decrease" check once
    # the window outlasts the step) does not. Not independently
    # live-reverified post-change (no redeploy access this session) — a
    # reasoned, backtest-safe improvement per this project's tune-from-live-
    # evidence culture, live effectiveness to be confirmed in a later round.
    "premature_descent_min_drop_ft": 6000,
    "premature_descent_multiplier": 3.0,
    "premature_descent_samples": 8,

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

    # When on: military/helicopter/GA-private/business-jet/other-light
    # aircraft (classify.py, using the free dbFlags/category fields already
    # present in every adsb.lol/airplanes.live response) skip the five
    # behavioral detectors (course_deviation, corridor_deviation,
    # holding_pattern, premature_descent, landed_wrong_airport,
    # signal_lost_near_airport) entirely — no incident is even opened.
    # detect_emergency (squawk-based) stays active for every class
    # regardless of this flag: a real emergency squawk from any aircraft
    # is real, urgent evidence. Set False to restore full coverage for
    # every class (e.g. if that's ever wanted again). See MASTERPLAN.md
    # sectie 4.
    "classification_suppress_non_airliner": True,
    # Aircraft type/operator doesn't change mid-flight, so a track's
    # classification is cached and only recomputed after this many hours.
    "classification_cache_ttl_hours": 24,

    # hexdb.io as a second, free, keyless data source (MASTERPLAN.md sectie
    # 2.2/5): a route fallback when adsbdb.com has nothing for a callsign,
    # and an aircraft/operator lookup to refine an otherwise-ONBEKEND
    # classification. Off switch in case hexdb.io ever becomes unreliable
    # without needing a code change.
    "route_secondary_source_enabled": True,
    # How long a genuine "no route found" adsbdb result (as opposed to a
    # transient comm failure, which already retries much sooner — see
    # ROUTE_LOOKUP_RETRY_COOLDOWN_S in providers.py) stays cached before
    # being retried. adsbdb is crowdsourced and grows over time; a callsign
    # with no data today may have data next week. See MASTERPLAN.md sectie
    # 5, punt 4.
    "route_lookup_negative_retry_hours": 12,

    # Incident-levenscyclus (MASTERPLAN.md sectie 3/9, incidents.py — fase
    # 3, groundwork laid 2026-08-06 but NOT YET wired into main.py/
    # notifier.py/server.py, see BACKTEST_LOG.md ronde 10). Starting
    # values, not measured against real data yet — same "begin
    # beargumenteerd, tune met echte data" discipline as every other
    # threshold in this file.
    "incident_score_possible_threshold": 25,
    "incident_score_likely_threshold": 55,
    "incident_score_confirmed_threshold": 85,
    # Multiplied into an open incident's score every tier1 cycle it does
    # NOT receive fresh evidence (see incidents.py's reassess()).
    "incident_score_decay_factor_per_cycle": 0.85,
    # An incident whose score has decayed under the POSSIBLE threshold and
    # has received no new evidence for this many minutes auto-closes as a
    # false alarm.
    "incident_score_decay_floor_minutes": 30,
    # Score cap — found via a live smoke test (BACKTEST_LOG.md ronde 10): a
    # long-lived incident whose trigger keeps re-firing every cycle (e.g. a
    # real, ongoing emergency squawk) otherwise accumulates score without
    # bound (+100 every 15s tier0 cycle, hundreds within minutes). Not a
    # functional bug (state/notification behavior were both already
    # correct regardless), just an unbounded number in the evidence
    # timeline — capped well above CONFIRMED (85) so there's still
    # headroom for multiple distinct corroborating signals to matter.
    "incident_score_max": 150,

    # Weer/luchtruim-context (MASTERPLAN.md sectie 6, fase 2 — airspace.py).
    # Free, keyless aviationweather.gov SIGMET/CWA polygons: when an open
    # incident's position falls inside an active hazard polygon at a
    # matching altitude, that's treated as a likely routine explanation
    # (severe weather being routed around) rather than a genuine diversion.
    "weather_sigmet_enabled": True,
    "weather_sigmet_refresh_seconds": 600,

    # Peer-consensus (MASTERPLAN.md sectie 6.2): multiple independent
    # aircraft deviating the same way in the same area at the same time is
    # itself evidence of a shared cause (unofficial hazard, ATC reroute,
    # GPS interference — not always covered by an official SIGMET/TFR
    # record), with no external data source needed. Grid-cell-based
    # (peer_consensus_radius_deg is a cell size in degrees, not a true
    # radius — deliberately simple, a real clustering approach can replace
    # this once there's real data to tune against).
    "peer_consensus_enabled": True,
    "peer_consensus_min_aircraft": 3,
    "peer_consensus_radius_deg": 2.0,

    # TFRs (MASTERPLAN.md sectie 2.4/6.3, fase 4). Originally scoped as
    # needing FAA API registration or an unofficial scraper — turned out
    # neither was needed: tfr.faa.gov's own public GeoServer WFS endpoint
    # (airspace.TFR_GEOSERVER_URL) returns current TFR polygons as GeoJSON,
    # no key/registration, live-verified working via a plain HTTP client
    # (see BACKTEST_LOG.md ronde 13). On by default like the SIGMET/CWA
    # sources it's combined with in airspace.get_active_hazards — same
    # off-switch pattern if this source ever needs to be disabled without
    # a code change.
    "notam_tfr_enabled": True,

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
