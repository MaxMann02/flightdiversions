import time
from collections import deque
from dataclasses import dataclass, field

import db as db_module

STALE_AFTER_SECONDS = 2 * 3600
HISTORY_MAXLEN = 20


@dataclass
class TrackPoint:
    ts: float
    lat: float
    lon: float
    alt_baro: float | None
    track: float | None
    on_ground: bool


@dataclass
class AircraftTrack:
    hex: str
    callsign: str = ""
    route: dict | None = None
    route_checked: bool = False
    # When the current route (if any) was assigned. Lets main.py's ongoing
    # recheck apply the strict route_plausible check (check_progress=True)
    # only for a bounded window after resolution — long enough to catch a
    # callsign collision that "looks plausible by chance at first, reveals
    # itself wrong soon after" (the original motivating case for this
    # recheck), short enough that it can't also strip a real diversion
    # that's still developing well past that window. See
    # main.py's ROUTE_REVALIDATION_WINDOW_S and airports.route_plausible's
    # docstring for why a single fixed threshold can't do both jobs at once.
    route_resolved_ts: float | None = None
    history: deque = field(default_factory=lambda: deque(maxlen=HISTORY_MAXLEN))
    last_seen: float = 0.0
    # None = never observed yet (distinct from False = "was airborne last
    # cycle"). Bug found by backtest review: defaulting this to False made
    # every aircraft's FIRST-ever sighting look like "just landed" if it
    # happened to already be on the ground — which, since TrackStore is
    # purely in-memory, means EVERY process restart falsely flags most
    # currently-parked aircraft worldwide as freshly landed once their route
    # resolves.
    was_on_ground: bool | None = None
    pending_deviation: dict | None = None
    corridor_deviation_streak: int = 0
    # Airport nearest to the last observed takeoff, kept so that when this
    # aircraft next lands we can record (callsign, origin, destination) as a
    # real observation into the learned-routes database.
    pending_origin: dict | None = None
    # Timestamp of the last observed on_ground->airborne transition, if we
    # were actually watching when it happened (None if this track was first
    # seen already airborne). Lets detect_landed_wrong_airport tell a genuine
    # fast return-to-origin (minutes after takeoff) apart from a later,
    # different leg reusing the same callsign hours apart — see its
    # docstring/comments for why that distinction matters.
    last_takeoff_ts: float | None = None
    # Consecutive tier1 cycles this aircraft was expected but absent from the
    # snapshot. Used to infer "signal lost near a non-destination airport,
    # possibly landed there unconfirmed" well before the 2-hour stale-prune
    # window would ever notice — found via backtesting the UA2078/Luke AFB
    # case: ADS-B coverage often drops before touchdown near small/military
    # fields, so the normal on_ground-transition landing check never fires.
    missing_cycles: int = 0
    # Consecutive cycles spent holding near the planned destination — see
    # detect_holding_pattern for why this is tracked separately from the
    # immediate non-destination case.
    holding_streak: int = 0

    def add_point(self, ac: dict, ts: float):
        if ac.get("callsign"):
            self.callsign = ac["callsign"]
        if ac.get("lat") is not None and ac.get("lon") is not None:
            self.history.append(TrackPoint(
                ts=ts,
                lat=ac["lat"],
                lon=ac["lon"],
                alt_baro=ac.get("alt_baro"),
                track=ac.get("track"),
                on_ground=ac.get("on_ground", False),
            ))
        self.last_seen = ts


class TrackStore:
    def __init__(self, db_conn=None):
        self.tracks: dict[str, AircraftTrack] = {}
        self.db = db_conn
        self.cooldowns: dict = db_module.load_cooldowns(db_conn) if db_conn else {}

    def get_or_create(self, hex_id: str) -> AircraftTrack:
        t = self.tracks.get(hex_id)
        if t is None:
            t = AircraftTrack(hex=hex_id)
            self.tracks[hex_id] = t
        return t

    def update(self, ac: dict, ts: float | None = None) -> AircraftTrack:
        ts = ts or time.time()
        t = self.get_or_create(ac["hex"])
        t.add_point(ac, ts)
        return t

    def prune(self, now: float | None = None):
        now = now or time.time()
        stale = [h for h, t in self.tracks.items() if now - t.last_seen > STALE_AFTER_SECONDS]
        for h in stale:
            del self.tracks[h]

    def should_alert(self, hex_id: str, event_type: str, cooldown_seconds: int, now: float | None = None) -> bool:
        now = now or time.time()
        last = self.cooldowns.get((hex_id, event_type))
        return last is None or (now - last) >= cooldown_seconds

    def mark_alerted(self, hex_id: str, event_type: str, now: float | None = None):
        now = now or time.time()
        self.cooldowns[(hex_id, event_type)] = now
        if self.db:
            db_module.save_cooldown(self.db, hex_id, event_type, now)

    def record_takeoff(self, track: AircraftTrack, airport: dict | None, ts: float | None = None):
        track.pending_origin = airport
        if ts is not None:
            track.last_takeoff_ts = ts

    def record_landing_observation(self, track: AircraftTrack, landing_airport: dict):
        """If we personally watched this aircraft take off earlier, record the
        (callsign, origin, destination) pair we just observed into the
        learned-routes DB — our own ground truth, built up over time,
        independent of adsbdb's static mapping."""
        if self.db and track.pending_origin and track.callsign:
            db_module.record_route_observation(
                self.db, track.callsign, track.pending_origin["icao"], landing_airport["icao"],
            )
        track.pending_origin = None
