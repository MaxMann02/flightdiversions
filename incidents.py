"""Incident-levenscyclus: continue scoring + state machine bovenop
detector.py's Events, in plaats van het oude "vuur eenmalig, vergeet"
model. Zie MASTERPLAN.md sectie 3 voor het volledige ontwerp.

STATUS (2026-08-06, zie BACKTEST_LOG.md ronde 10): dit bestand is af en
zelfstandig getest (zie backtest.py's check_incident_engine_regressions),
maar NOG NIET aangesloten op main.py/notifier.py/server.py — dat is de
eerstvolgende stap voor een nieuwe sessie, niet iets dat dit bestand zelf
nog mist. Zie MASTERPLAN.md sectie 11, fase 3 voor het resterende
werk (wiring in tier0_loop/tier1_loop, notifier.notify_incident_transition,
server.py's /api/incidents, dashboard-HTML).

Kernidee: een Event van detector.py is geen melding meer, het is EVIDENCE
voor een (mogelijk al bestaand) Incident voor dat vliegtuig. Elke
tier1-cyclus wordt elk open incident opnieuw beoordeeld: vervalt de score
(tijdsverval, hersteld gedrag) of wordt hij versterkt (nieuwe evidence),
en bij het overschrijden van een drempel verandert de state — wat weer
bepaalt of/hoe genotificeerd wordt (main.py roept dat via de
transition-dicts die step() teruggeeft).
"""
import db as db_module
from airports import angle_diff_deg, bearing_deg
from airspace import explain_position
from providers import EMERGENCY_SQUAWKS

WATCHING = "BEWAKING"
POSSIBLE = "MOGELIJK"
LIKELY = "WAARSCHIJNLIJK"
CONFIRMED = "BEVESTIGD"
CLOSED_FALSE_ALARM = "GESLOTEN_VALS_ALARM"
CLOSED_LANDED = "GESLOTEN_GELAND"
CLOSED_NORMAL = "GESLOTEN_NORMAAL"
CLOSED_TIMEOUT = "GESLOTEN_TIMEOUT"

OPEN_STATES = (WATCHING, POSSIBLE, LIKELY, CONFIRMED)
VISIBLE_STATES = (POSSIBLE, LIKELY, CONFIRMED)
CLOSED_STATES = (CLOSED_FALSE_ALARM, CLOSED_LANDED, CLOSED_NORMAL, CLOSED_TIMEOUT)

_STATE_RANK = {WATCHING: 0, POSSIBLE: 1, LIKELY: 2, CONFIRMED: 3}

# detect_course_deviation/detect_route_corridor_deviation only ever produce
# an Event once the bow-tolerance check (detector.py, MASTERPLAN.md sectie
# 8) has already confirmed heading no longer points toward the filed
# destination — so at this layer there is no separate "weak, still
# inbound" tier to model; that case is now a hard detector-level
# suppression rather than a soft score weight (a deliberate simplification
# made while implementing this, see BACKTEST_LOG.md ronde 10).
DEVIATION_EVENT_TYPES = ("course_deviation", "corridor_deviation")

# Evidence sources whose underlying detect_* condition can keep re-firing
# every single cycle for as long as the underlying situation persists (an
# ongoing hold, a continuous descent) — as opposed to a one-shot signal
# (wrong_airport/signal_lost fire once, at the moment of landing/loss).
# Found via a real-case (AI850) multi-detector-escalation backtest
# (BACKTEST_LOG.md ronde 16): holding_pattern and premature_descent had NO
# repeat dampening, unlike course_deviation/corridor_deviation (which
# already only give a smaller top-up on repeats, see DEVIATION_EVENT_TYPES
# above) — an inconsistency, not a deliberate design choice. Undamped, an
# ordinary extended ATC hold at a busy destination (already required to
# sustain 20+ minutes past holding_pattern_destination_min_streak before it
# ever fires at all) reached BEVESTIGD, and dispatched a Telegram
# notification, within 3 more minutes of that gate clearing — with zero
# corroborating evidence, purely from the same detector re-confirming the
# same ongoing hold every cycle. Same failure mode for premature_descent
# during any sustained continuous descent (e.g. EJU3722 in MASTERPLAN.md
# sectie 12, a long-but-legitimate TMA approach). Repeat weights below use
# the same ~1/3-of-first-hit ratio already established for
# DEVIATION_EVENT_TYPES.
REPEATABLE_EVENT_TYPES = DEVIATION_EVENT_TYPES + ("holding_pattern", "premature_descent")

# Evidence sources that are themselves negative-only/context, never a sign
# the incident is more serious than a plain deviation — excluded from
# _check_deviation_recovered's "is this incident still just a deviation"
# test so a PRIOR recovery (or a weather/peer-consensus note) doesn't
# permanently disable recovery detection for the rest of the incident's
# life. See that method's docstring / BACKTEST_LOG.md ronde 15.
_NON_REINFORCING_SOURCES = {"deviation_resolved", "weather_explains", "peer_consensus"}


def _state_for_score(score: float, cfg: dict) -> str:
    if score >= cfg["incident_score_confirmed_threshold"]:
        return CONFIRMED
    if score >= cfg["incident_score_likely_threshold"]:
        return LIKELY
    if score >= cfg["incident_score_possible_threshold"]:
        return POSSIBLE
    return WATCHING


def score_for_event(ev, is_repeat_type: bool) -> tuple[float, str, str]:
    """Base evidence weight for a fresh detector.Event. is_repeat_type:
    whether this SAME incident already has an earlier contribution from
    this exact event_type — every type in REPEATABLE_EVENT_TYPES gets a
    smaller top-up on repeats rather than the full first-hit weight each
    time (see that constant's docstring for why). Returns (delta, source,
    description). See MASTERPLAN.md sectie 3.3 for the full rationale
    behind each weight."""
    et = ev.event_type
    if et == "emergency":
        if ev.squawk in EMERGENCY_SQUAWKS:
            return 100.0, "emergency_squawk", f"noodsquawk {ev.squawk}"
        if ev.confidence == "WAARSCHIJNLIJK":
            # Already capped by main.py's enrich_events (conspicuity squawk
            # or cross-provider disagreement) — see providers.
            # CONSPICUITY_SQUAWKS / cross_provider_confirms_emergency.
            return 8.0, "emergency_status_low_trust", "emergency-status (laag vertrouwen — conspicuity-squawk of niet bevestigd)"
        return 35.0, "emergency_status", "emergency-status op discrete squawk"
    if et == "wrong_airport":
        return 90.0, "wrong_airport", "geland op onverwachte luchthaven"
    if et == "signal_lost_near_airport":
        return 40.0, "signal_lost", "signaal verloren nabij niet-bestemming, laag"
    if et == "holding_pattern":
        if ev.at_destination:
            return (8.0 if is_repeat_type else 25.0), "holding_destination", "ongewoon lang wachtpatroon bij bestemming"
        return (12.0 if is_repeat_type else 35.0), "holding_non_destination", "wachtpatroon bij niet-bestemming"
    if et in DEVIATION_EVENT_TYPES:
        return (10.0 if is_repeat_type else 30.0), et, f"{et} (koers wijst niet meer naar bestemming)"
    if et == "premature_descent":
        return (8.0 if is_repeat_type else 25.0), "premature_descent", "vroegtijdige daling"
    return 10.0, et, ev.message  # fallback for any future detector type


class IncidentManager:
    """hex -> open-incident bijhouden, in-memory (mirrors de DB) net als
    TrackStore dat voor cooldowns doet. airport_db is nodig om dest_icao
    (een string op het incident) terug om te zetten naar coördinaten voor
    de "afwijking hersteld"-check en de "geland op verwachte bestemming"-
    check."""

    def __init__(self, db_conn, cfg: dict, airport_db):
        self.db = db_conn
        self.cfg = cfg
        self.airport_db = airport_db
        self._open: dict[str, dict] = {}
        for hex_id in self._distinct_hexes_with_open_incidents():
            inc = db_module.get_open_incident(self.db, hex_id, OPEN_STATES)
            if inc:
                self._open[hex_id] = inc

    def _distinct_hexes_with_open_incidents(self) -> list[str]:
        cur = self.db.execute(
            f"SELECT DISTINCT hex FROM incidents WHERE state IN ({','.join('?' * len(OPEN_STATES))})",
            OPEN_STATES,
        )
        return [row[0] for row in cur.fetchall()]

    def open_hexes(self) -> list[str]:
        return list(self._open.keys())

    def _evidence_sources_seen(self, incident_id: int) -> set:
        """The set of evidence `source` strings already recorded for this
        incident — NOT the same as detector.py event_types (renamed from
        _evidence_types_seen, BACKTEST_LOG.md ronde 16, after that naming
        mismatch caused a real bug, see apply_events below)."""
        return {row["source"] for row in db_module.get_incident_evidence(self.db, incident_id)}

    def _apply_delta(self, hex_id: str, now: float, delta: float, source: str, description: str,
                      detector_confidence: str | None, callsign: str = "", aircraft_class: str | None = None,
                      origin_icao: str | None = None, dest_icao: str | None = None,
                      lat: float | None = None, lon: float | None = None, alt: float | None = None,
                      squawk: str | None = None) -> tuple[dict, str | None, str]:
        """Core mutation: find-or-create the open incident for hex_id, add
        one evidence row, recompute score/state, persist, return
        (incident, old_state_or_None, new_state) — old_state is None for a
        brand-new incident."""
        inc = self._open.get(hex_id)
        old_state = None
        if inc is None:
            initial_score = max(0.0, min(self.cfg.get("incident_score_max", 150), delta))
            state = _state_for_score(initial_score, self.cfg)
            incident_id = db_module.create_incident(self.db, hex_id, callsign, state, initial_score, now, aircraft_class)
            inc = {
                "id": incident_id, "hex": hex_id, "callsign": callsign, "state": state,
                "score": initial_score, "peak_score": initial_score, "opened_ts": now,
                "last_evidence_ts": now, "resolved_ts": None, "resolution_reason": None,
                "origin_icao": None, "dest_icao": None, "last_lat": None, "last_lon": None,
                "last_alt": None, "last_squawk": None, "aircraft_class": aircraft_class,
                "notified_state": None,
            }
            self._open[hex_id] = inc
        else:
            old_state = inc["state"]
            inc["score"] = max(0.0, min(self.cfg.get("incident_score_max", 150), inc["score"] + delta))
            inc["peak_score"] = max(inc["peak_score"], inc["score"])
            inc["last_evidence_ts"] = now
            inc["state"] = _state_for_score(inc["score"], self.cfg)

        inc["callsign"] = callsign or inc.get("callsign")
        if origin_icao:
            inc["origin_icao"] = origin_icao
        if dest_icao:
            inc["dest_icao"] = dest_icao
        if lat is not None:
            inc["last_lat"], inc["last_lon"] = lat, lon
        if alt is not None:
            inc["last_alt"] = alt
        if squawk is not None:
            inc["last_squawk"] = squawk

        db_module.add_incident_evidence(self.db, inc["id"], now, source, delta, description, detector_confidence)
        db_module.update_incident(
            self.db, inc["id"],
            state=inc["state"], score=inc["score"], peak_score=inc["peak_score"],
            last_evidence_ts=inc["last_evidence_ts"], callsign=inc["callsign"],
            origin_icao=inc.get("origin_icao"), dest_icao=inc.get("dest_icao"),
            last_lat=inc.get("last_lat"), last_lon=inc.get("last_lon"),
            last_alt=inc.get("last_alt"), last_squawk=inc.get("last_squawk"),
        )
        return inc, old_state, inc["state"]

    def apply_events(self, hex_id: str, callsign: str, aircraft_class: str | None, events: list, now: float) -> list[dict]:
        """Feed this cycle's fresh detector.Events for one aircraft into its
        incident. Returns a list of transition dicts (see _maybe_notify)."""
        transitions = []
        for ev in events:
            existing_sources = self._evidence_sources_seen(self._open[hex_id]["id"]) if hex_id in self._open else set()
            # is_repeat must compare against the SOURCE string this event
            # would produce, not ev.event_type directly — they only happen
            # to be equal for course_deviation/corridor_deviation/
            # premature_descent/wrong_airport. holding_pattern's source is
            # "holding_destination"/"holding_non_destination" (depends on
            # at_destination) and emergency's is one of three squawk/
            # confidence-dependent strings — comparing ev.event_type against
            # existing_sources for those silently always evaluated to
            # False, so holding_pattern/premature_descent's repeat weight
            # (added this round, BACKTEST_LOG.md ronde 16, to fix unbounded
            # per-cycle re-scoring of an ongoing hold/descent) never
            # actually engaged. score_for_event's source choice doesn't
            # depend on is_repeat_type, so computing it with is_repeat_type=
            # False first to learn the prospective source is safe/pure.
            _, prospective_source, _ = score_for_event(ev, False)
            is_repeat = prospective_source in existing_sources
            delta, source, description = score_for_event(ev, is_repeat)
            inc, old_state, new_state = self._apply_delta(
                hex_id, now, delta, source, description, ev.confidence,
                callsign=callsign, aircraft_class=aircraft_class,
                origin_icao=ev.origin_icao, dest_icao=ev.dest_icao,
                lat=ev.lat, lon=ev.lon, alt=ev.alt, squawk=ev.squawk,
            )
            t = self._maybe_notify(inc, old_state, new_state, now)
            if t:
                transitions.append(t)
        return transitions

    def _maybe_notify(self, inc: dict, old_state: str | None, new_state: str, now: float) -> dict | None:
        """MASTERPLAN.md sectie 3.6: notify only on first reaching
        WAARSCHIJNLIJK/BEVESTIGD, and on a stand-down close FROM either of
        those (a MOGELIJK that quietly expires never notified in the first
        place, so it doesn't need a stand-down message either).
        notified_state tracks the highest state already notified so a
        later re-evaluation doesn't re-notify for the same transition."""
        if old_state == new_state:
            return None
        notified = inc.get("notified_state")
        kind = None
        if new_state in (LIKELY, CONFIRMED) and _STATE_RANK.get(notified, -1) < _STATE_RANK[new_state]:
            kind = "escalation"
        elif new_state in CLOSED_STATES and notified in (LIKELY, CONFIRMED):
            kind = "stand_down" if new_state == CLOSED_FALSE_ALARM else "closed"

        if kind is None:
            return None
        if kind == "escalation":
            inc["notified_state"] = new_state
            db_module.update_incident(self.db, inc["id"], notified_state=new_state)
        return {"incident": dict(inc), "kind": kind, "old_state": old_state, "new_state": new_state, "ts": now}

    def _check_landed(self, hex_id: str, ac: dict | None, now: float) -> dict | None:
        """Landing-based resolution. Two outcomes: landed at the expected
        destination (GESLOTEN_NORMAAL), or landed somewhere else while
        already carrying wrong_airport evidence — detect_landed_wrong_
        airport's own +90 ground-truth Event, the strongest signal this
        system has — which is closed as GESLOTEN_GELAND (a confirmed
        diversion) rather than left open to eventually decay away.

        Found via self-review, not a live incident (BACKTEST_LOG.md ronde
        15): CLOSED_LANDED was defined as a state but no code path ever
        actually reached it. Without this, a genuine confirmed diversion
        (BEVESTIGD via wrong_airport) that then sits quietly on the wrong
        ground for a while — no more emergency squawk, no more evidence —
        would decay like anything else and eventually auto-close as
        GESLOTEN_VALS_ALARM ('false alarm'), silently mislabeling a real,
        already-confirmed diversion as if it had been nothing. A route-less
        incident (dest_icao unknown, so wrong_airport can't even fire)
        landing anywhere just falls through to neither branch and keeps
        being reassessed normally — unchanged from before this fix."""
        if ac is None or not ac.get("on_ground") or ac.get("lat") is None:
            return None
        inc = self._open.get(hex_id)
        if inc is None:
            return None
        nearest, _dist = self.airport_db.nearest(ac["lat"], ac["lon"], max_km=15.0)
        if not nearest:
            return None
        if inc.get("dest_icao") and nearest["icao"] == inc["dest_icao"]:
            return self._resolve(hex_id, now, CLOSED_NORMAL, "geland op verwachte bestemming")
        if "wrong_airport" in self._evidence_sources_seen(inc["id"]):
            return self._resolve(
                hex_id, now, CLOSED_LANDED,
                f"geland op {nearest['icao']}, niet de verwachte bestemming — bevestigde diversie",
            )
        return None

    def _check_deviation_recovered(self, inc: dict, ac: dict | None, now: float):
        """If this incident's evidence contains nothing STRONGER than
        corridor/course-deviation (a prior recovery or weather/peer-
        consensus context doesn't count as "stronger" — see below) and the
        aircraft's CURRENT heading points back toward its filed
        destination, treat the deviation as resolved — the concrete
        mechanism behind "if it turns out to be a false alert, it may
        disappear from the list again" (MASTERPLAN.md sectie 3.5).

        _STRONG_EVIDENCE_SOURCES (not just "anything other than deviation
        types") is deliberate, fixed via self-review (BACKTEST_LOG.md
        ronde 15): the original check used `evidence_types <= set(
        DEVIATION_EVENT_TYPES)`, a strict subset test — the FIRST time this
        function itself adds "deviation_resolved" evidence, that source
        joins evidence_types and permanently fails the subset check on
        every later call, silently disabling recovery detection forever
        for that incident even if it diverges and needs to recover a
        second time. weather_explains/peer_consensus (both already
        negative-only evidence) have the same problem. None of those three
        sources represent genuine reinforcement, so excluding them from
        the "is this incident still just a deviation" check (rather than
        excluding nothing, or excluding everything not in
        DEVIATION_EVENT_TYPES) is what actually reflects the intent."""
        if ac is None or ac.get("track") is None or ac.get("lat") is None:
            return None
        evidence_types = self._evidence_sources_seen(inc["id"])
        if not evidence_types or (evidence_types - _NON_REINFORCING_SOURCES) - set(DEVIATION_EVENT_TYPES):
            return None  # reinforced by something stronger than a deviation — don't auto-recover
        dest_icao = inc.get("dest_icao")
        if not dest_icao:
            return None
        dest_ap = self.airport_db.get(dest_icao)
        if not dest_ap:
            return None
        bearing_to_dest = bearing_deg(ac["lat"], ac["lon"], dest_ap["lat"], dest_ap["lon"])
        if angle_diff_deg(ac["track"], bearing_to_dest) > self.cfg["corridor_deviation_bow_heading_deg"]:
            return None
        return self._apply_delta(
            inc["hex"], now, -30.0, "deviation_resolved",
            "afwijking hersteld — koers wijst weer richting bestemming", None,
        )

    def _check_weather_explains(self, inc: dict, hazards: list | None, now: float):
        """MASTERPLAN.md sectie 6.1: an incident whose position sits inside
        an active SIGMET/CWA hazard polygon at a matching altitude is
        probably routine weather-avoidance, not a real diversion. Applied
        at most once per incident (checking evidence_types_seen) — no
        point re-discounting every cycle once already noted."""
        if not hazards or inc.get("last_lat") is None:
            return None
        if "weather_explains" in self._evidence_sources_seen(inc["id"]):
            return None
        hazard = explain_position(inc["last_lat"], inc["last_lon"], inc.get("last_alt"), hazards)
        if not hazard:
            return None
        return self._apply_delta(
            inc["hex"], now, -50.0, "weather_explains",
            f"actief weer ({hazard['hazard']}, {hazard['id']}) op deze positie — verklaart mogelijk de afwijking", None,
        )

    def _peer_consensus_count(self, inc: dict) -> int:
        """Count of OTHER currently-open incidents whose last known
        position falls in the same coarse grid cell as this one — a cheap,
        external-data-free proxy for 'multiple aircraft are reacting to the
        same thing in this area' (MASTERPLAN.md sectie 6.2)."""
        if inc.get("last_lat") is None:
            return 0
        cell_deg = self.cfg["peer_consensus_radius_deg"]
        my_cell = (int(inc["last_lat"] // cell_deg), int(inc["last_lon"] // cell_deg))
        count = 0
        for other_hex, other in self._open.items():
            if other_hex == inc["hex"] or other.get("last_lat") is None:
                continue
            other_cell = (int(other["last_lat"] // cell_deg), int(other["last_lon"] // cell_deg))
            if other_cell == my_cell:
                count += 1
        return count

    def _check_peer_consensus(self, inc: dict, now: float):
        if "peer_consensus" in self._evidence_sources_seen(inc["id"]):
            return None
        # -1: peer count already excludes this incident itself, so N-1
        # others in the same cell means N total including this one.
        if self._peer_consensus_count(inc) < self.cfg["peer_consensus_min_aircraft"] - 1:
            return None
        return self._apply_delta(
            inc["hex"], now, -55.0, "peer_consensus",
            "meerdere andere toestellen wijken tegelijk op vergelijkbare wijze uit in dit gebied — mogelijk gedeelde oorzaak, geen individuele diversie", None,
        )

    def reassess(self, hex_id: str, ac: dict | None, now: float, hazards: list | None = None) -> dict | None:
        """Decay + recovery-detection + weather/peer-consensus context +
        auto-close for one open incident that did NOT receive fresh
        evidence this cycle (see step()) — MASTERPLAN.md sectie 3.5/6.
        hazards: pre-fetched airspace.get_active_hazards() result for this
        cycle, or None to skip the weather check (kept as a plain
        synchronous list here rather than an async fetch inside this
        method, so incidents.py itself stays network-free — main.py fetches
        once per tier1 cycle and passes it in)."""
        inc = self._open.get(hex_id)
        if inc is None:
            return None

        # Emergency-squawk incidents never auto-decay/close while the
        # squawk is still active — silence after a real emergency is
        # itself meaningful, not evidence of a false alarm.
        if ac is not None and ac.get("squawk") in EMERGENCY_SQUAWKS:
            return None

        recovered = self._check_deviation_recovered(inc, ac, now)
        if recovered:
            inc, old_state, new_state = recovered
            t = self._maybe_notify(inc, old_state, new_state, now)
            if t:
                return t

        if self.cfg.get("weather_sigmet_enabled"):
            explained = self._check_weather_explains(inc, hazards, now)
            if explained:
                inc, old_state, new_state = explained
                t = self._maybe_notify(inc, old_state, new_state, now)
                if t:
                    return t

        if self.cfg.get("peer_consensus_enabled"):
            consensus = self._check_peer_consensus(inc, now)
            if consensus:
                inc, old_state, new_state = consensus
                t = self._maybe_notify(inc, old_state, new_state, now)
                if t:
                    return t

        decay = self.cfg["incident_score_decay_factor_per_cycle"]
        old_score = inc["score"]
        inc["score"] = inc["score"] * decay
        if inc["score"] == old_score:
            return None
        old_state = inc["state"]
        inc["state"] = _state_for_score(inc["score"], self.cfg)
        db_module.update_incident(self.db, inc["id"], score=inc["score"], state=inc["state"])

        minutes_idle = (now - inc["last_evidence_ts"]) / 60.0
        if (inc["state"] in (WATCHING, POSSIBLE)
                and inc["score"] < self.cfg["incident_score_possible_threshold"]
                and minutes_idle >= self.cfg["incident_score_decay_floor_minutes"]):
            if inc.get("notified_state") in (LIKELY, CONFIRMED):
                # This incident was serious enough to notify about at some
                # point — decaying quietly away isn't "it turned out to be
                # nothing", it's "no more evidence arrived after it had
                # already escalated" (e.g. a track going stale/pruned, or
                # the situation genuinely going quiet without a clean
                # landing/recovery signal ever arriving). Labeling this
                # GESLOTEN_VALS_ALARM would misrepresent a real, once-
                # confirmed incident as a false alarm — GESLOTEN_TIMEOUT is
                # the honest label instead. Found via self-review, same
                # root cause class as the wrong_airport/GESLOTEN_GELAND fix
                # above: GESLOTEN_TIMEOUT was a defined state with no code
                # path that ever actually reached it. See BACKTEST_LOG.md
                # ronde 15.
                return self._resolve(hex_id, now, CLOSED_TIMEOUT,
                                      "geen nieuw bewijs meer na eerdere escalatie — status onbekend, niet bevestigd vals alarm")
            return self._resolve(hex_id, now, CLOSED_FALSE_ALARM, "score verviel zonder nieuw bewijs")

        if old_state != inc["state"]:
            return self._maybe_notify(inc, old_state, inc["state"], now)
        return None

    def _resolve(self, hex_id: str, now: float, reason: str, description: str) -> dict | None:
        inc = self._open[hex_id]
        old_state = inc["state"]
        inc["state"] = reason
        inc["resolved_ts"] = now
        inc["resolution_reason"] = description
        db_module.add_incident_evidence(self.db, inc["id"], now, "resolved", 0.0, description, None)
        db_module.update_incident(self.db, inc["id"], state=reason, resolved_ts=now, resolution_reason=description)
        t = self._maybe_notify(inc, old_state, reason, now)
        del self._open[hex_id]
        return t

    def step(self, hex_id: str, callsign: str, aircraft_class: str | None, events: list,
              ac: dict | None, now: float, hazards: list | None = None) -> list[dict]:
        """Single entry point for main.py, once per tracked aircraft per
        tier1 cycle. Applies fresh evidence (if any), then landing-based
        resolution, then — only when there was NO fresh evidence this
        cycle, so a score bump isn't immediately partially undone by the
        same cycle's decay — recovery-detection/weather+peer-consensus
        context/decay/auto-close. Returns a list of transition dicts to
        dispatch as notifications. hazards: this cycle's pre-fetched
        airspace.get_active_hazards() result, or None to skip the weather
        check — see reassess()."""
        transitions = []
        if events:
            transitions.extend(self.apply_events(hex_id, callsign, aircraft_class, events, now))

        if hex_id not in self._open:
            return transitions

        landed_t = self._check_landed(hex_id, ac, now)
        if landed_t:
            transitions.append(landed_t)
            return transitions

        if not events:
            t = self.reassess(hex_id, ac, now, hazards)
            if t:
                transitions.append(t)
        return transitions
