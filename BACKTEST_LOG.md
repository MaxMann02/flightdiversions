# Backtest log

Running log of backtest rounds against `backtest.py` / `backtest_cases.py`.
Each round: research a real, documented diversion, encode it as a case,
run the suite, reflect on what fired (or didn't) and why, fix what's
clearly fixable, and record findings here so the next round doesn't
re-derive them. Cases and their sources live in `backtest_cases.py` —
this file is the *narrative*, not the data.

## Round 1 — 2026-08-06

Seeded 3 cases from public incident reports (see `backtest_cases.py` for
sources): Air India AI850 (Pune-Delhi-Gwalior, 2023-05-25), United 2078
(Houston-Phoenix-Luke AFB, 2026-07-21), Emirates EK225 (Dubai-SFO-Heathrow
turnback, 2026-07-29).

**First run: 1/3 detected.** Two were masked by a bug in the backtest
harness itself, not the detector: `cruise_leg` legs were stitched with a
duplicate boundary sample (same timestamp at both ends), which silently
diluted real heading jumps across two near-identical `angle_diff_deg`
calls instead of registering as one clean turn. Fixed with `stitch()` in
`backtest_cases.py`, which drops the leading sample of every leg after the
first. Also found `holding_loop`'s default lap rate too slow to clear
`holding_pattern_min_rotation_deg` within the 5-sample window — tightened
to `lap_samples=4` for the two hold-based cases.

**Second run: 2/3 detected** (AI850 holding_pattern, UA2078 wrong_airport).
EK225 still missed corridor_deviation — this one was a real detector gap,
not a harness bug: `crosses_russian_airspace_zone` disables
`detect_route_corridor_deviation` for a route's *entire* flight based only
on the filed origin/destination, with no mechanism to re-enable it once an
actual diversion has flown the aircraft completely away from that zone.
EK225's filed route (DXB->SFO) crosses the zone, so the exclusion silently
blocked detection even hours after the aircraft turned fully toward
Europe. Fixed in `detector.py`: the skip now only holds while the
*current* heading is still within `corridor_deviation_bow_heading_deg`
(60°, new config key) of the bearing to the filed destination — a
legitimate Russia-avoidance bow keeps making broad progress toward the
destination throughout; a real diversion's heading stops pointing there.
Sanity-checked against a synthetic non-diverted JFK->DEL bow (drifts far
off the direct line but keeps heading toward Delhi the whole way): zero
false positives.

**Third run: 3/3 detected.**
- AI850: holding_pattern fires 40min into the hold — **1h26m before** the
  crew's own decision to divert. Validates the existing
  `holding_pattern_destination_min_streak=20` tuning; no change made.
- UA2078: wrong_airport fires at landing (10min after the "minimum fuel"
  call, since it can only confirm post-hoc) — but holding_pattern fires
  **23min before** "minimum fuel" was even declared, entirely without an
  emergency squawk (this case deliberately keeps squawk at 1200 throughout,
  since "minimum fuel" isn't a formal emergency declaration and doesn't
  reliably get one). Confirms holding_pattern is a genuinely
  squawk-independent early signal, not just a backstop.
- EK225: corridor_deviation fires 3min after the turn (fastest possible
  given `corridor_deviation_min_samples=3` at 60s cadence) — was a
  complete miss before the fix above.

**Known gap, not yet fixed:** `detect_course_deviation`'s
`course_deviation_deg=90` threshold also nearly missed EK225 — the
reconstructed turn-away heading was ~84° (see `backtest_cases.py`'s notes
on that case for why this reconstruction's exact angle is an
approximation, not a sourced fact). Lowering the threshold risks false
positives on ordinary large-but-legitimate route turns; corridor_deviation
now covers this case anyway via the fix above, so this is lower priority.
Worth another real case before touching it — one data point with an
approximated angle isn't enough to justify retuning a threshold that's
already been through several rounds of live false-positive elimination
(see the existing comments on `detect_course_deviation`).

**Ideas for the next round:**
- Find 1-2 more real cases NOT already referenced in existing code
  comments (Djerba->Nice and the SLC->Amsterdam 454nm case are mentioned
  in `config.py`/`airports.py` comments but couldn't be independently
  re-sourced during this round — if a source turns up, they're good
  candidates since they already motivated existing tuning).
- A case that exercises `detect_premature_descent` specifically — no case
  covers it yet (see round 2 below for `detect_signal_lost_near_airport`,
  which now does).
- Revisit the `course_deviation_deg` threshold once a second real
  borderline-angle case exists.

## Round 2 — 2026-08-06 (same day, /loop iteration 1)

Went looking for a genuinely new real incident for `detect_premature_descent`
or `detect_signal_lost_near_airport` (both had zero coverage). Web search
for a specific, sourceable case for either came up empty this round — but
re-reading `detector.py`'s own docstring for `detect_signal_lost_near_airport`
surfaced something more useful: it says outright that it was written
*because of* United 2078/Luke AFB ("small/military fields often lose
ADS-B coverage before touchdown"). The UA2078 case already in this
suite (round 1) models a clean on-ground touchdown at Luke AFB, which only
ever exercises `detect_landed_wrong_airport` — the detector its own
motivating case was supposedly for had **never actually been run** by this
harness.

Bigger problem found while trying to fix that: `backtest.py`'s `run_case`
had no mechanism at all for a track going missing — it only ever fed real
samples through `evaluate()`, with no equivalent of `main.py`'s
`tier1_loop` incrementing `missing_cycles` for a track that drops out of
the snapshot. `detect_signal_lost_near_airport` was structurally
unreachable from this harness, independent of any detector-side gap.

**Fix (harness, not detector):** added `Case.goes_missing` — when set,
`run_case` sets `track.missing_cycles = cfg["signal_lost_missing_cycles"]`
after the last sample and calls `detect_signal_lost_near_airport` directly,
mirroring the trigger condition in `main.py`'s `tier1_loop`. Added a
variant case, `_ua2078_signal_lost`, reusing UA2078's sourced hold timing
but ending with the aircraft low and inbound to Luke AFB with no landing
sample — explicitly marked as testing the *mechanism*, not a sourced claim
that UA2078's coverage actually dropped (no source confirms that either
way).

**Result: detector fires correctly, first try, no detector-side fix
needed.** `signal_lost_near_airport` fires ~3min after the last known
position (right at `signal_lost_missing_cycles=3` cycles), correctly
identifies Luke AFB as the non-destination nearest airport. This round's
finding is entirely in the harness (a real, unreachable-detector gap, now
closed) — `detector.py` itself needed no change. Worth recording as a
clean validation, not just chasing changes for their own sake.

4/4 cases now detect their expected type. `detect_premature_descent`
remains the only detector with zero backtest coverage — good target for
the next round if a sourceable case turns up.

## Round 3 — 2026-08-06 (same day, /loop iteration 2)

Went looking for a new real incident to cover `detect_premature_descent`.
Found two candidates via WebSearch (American Airlines AA1049 DFW->JFK
diverted to Philadelphia on low fuel, 2026-07-16; Silk Way West 747-8F
Mexico City->Houston that ended up making an emergency landing at
Dallas/Fort Worth after apparently navigating toward the wrong city,
2026-08-05) — but on closer reading, neither is actually a good fit for
*this* detector. AA1049 diverted only ~30min short of JFK, at roughly the
point/time a normal JFK descent would already be starting — the altitude
would look "normal" relative to JFK almost the whole way, not premature.
Silk Way West is a dramatic wrong-airport/navigation-error case, not a
descent-profile one. Chasing a case to fit the detector rather than
building the detector to fit real evidence would be backwards.

Instead: `detect_premature_descent`'s own docstring already names its
motivating incident — "a real Emirates DXB->SFO diversion to LHR where
the turn angle was ambiguous (possibly <90 deg)" — which is exactly the
EK225 case already in this suite (its reconstructed turn was ~84°, see
round 1). The existing EK225 case just never modeled a descent at all
(constant 39000ft cruise, then a same-timestamp on-ground sample right
after) — an artifact of the same duplicate-boundary pattern flagged in
round 1, just not triggered there since nothing depended on the altitude
profile at that point.

**Change:** added a `descent_leg` helper (linear altitude interpolation
alongside `cruise_leg`'s position interpolation) and refactored EK225's
track-building into a shared `_ek225_track()` with a top-of-descent point
~25min from touchdown, descending from FL390 to 1200ft into LHR. Split the
case in two — `_ek225()` (expected_type=corridor_deviation, unchanged) and
new `_ek225_premature_descent()` (same track, expected_type=
premature_descent) — so both detectors' timing show up separately in the
report rather than only the first-registered type per case.
`premature_descent`'s trigger condition is trivially satisfied here (any
descent at all looks "early" relative to a filed destination thousands of
nm away) — that's not a weak test, it's exactly the real mechanism: the
whole point is that the FILED route doesn't update just because the
aircraft turned.

**Result: fires correctly, 3min after top-of-descent, no detector-side fix
needed.** Third round in a row where a previously-uncovered detector
worked correctly on the first real test — worth treating as signal that
the six detectors are individually solid; remaining risk is more likely in
how they interact/get suppressed (see the still-open course_deviation
threshold question below) than in any one detector's own logic.

**5/5 cases now detect their expected type — every one of the six
`detect_*` functions has now fired correctly in at least one backtest.**
`detect_course_deviation` specifically has still never been the expected
type of a passing case (EK225's course_deviation near-miss from round 1
remains just a documented near-miss, not a pass) — it's the one detector
where "backtested" currently only means "confirmed the threshold is close,
not confirmed the detector fires." Best next target: a case with a
cleaner, more confidently-sourced turn angle than EK225's reconstruction.

## Round 4 — 2026-08-06 (same day, /loop iteration 3, run immediately — no scheduling delay)

Went looking for a case with a cleaner, larger, more confidently-sourced
turn than EK225's ~84° reconstruction, to properly backtest
`detect_course_deviation` for the first time (see round 3's closing note).
First candidate checked — Air France 066 (2017 A380 uncontained engine
failure over Greenland, diverted to Goose Bay) — turned out to be a poor
fit once actually computed: Goose Bay sits close enough to a Greenland-
transit routing that the bearing change from the failure position is only
~45°, nowhere near the 90° threshold. Rejected rather than forced (same
discipline as round 3's AA1049/Silk Way West rejection) — this is
genuinely useful information too: Goose Bay's role as the standard NAT
diversion field means it's often NOT a sharp turn away from a normal
transatlantic routing, so it's a poor detector test even though it's a
dramatic, well-documented incident.

Second candidate — Air France AF9 (2025-08-19, B777-300ER, JFK->CDG,
right engine failure ~40min out over the Gulf of Maine, MAYDAY + squawk
7700, returned to JFK) — a return-to-origin, so the geometry is a near-180°
reversal almost by construction. Computed bearing-to-CDG vs bearing-to-JFK
from the reported position: 179°, about as clean as a real case gets.
Source: https://airlive.net/emergency/2025/08/19/air-france-af9-is-declaring-an-emergency-and-returning-to-new-york-jfk/

**Result: course_deviation fires correctly, 2min after the turn — first
pass ever for this detector, no code change needed.** Also correctly
fires `emergency` (squawk 7700 is explicitly sourced for this one, unlike
EK225/AI850 where it's assumed).

**6/6 cases now detect their expected type. Every one of the six
`detect_*` functions has now been the expected, passing type of at least
one backtest case** — `course_deviation` was the last one still only
"near-missed." Fourth round in a row with no detector.py change needed;
the pattern across all four is now strong enough to say the individual
detector logic is solid — remaining risk is much more likely in
cross-detector interaction, config tuning at the margins, or scenarios
this suite hasn't thought to construct yet, than in any one `detect_*`
function's own logic.

**Where this leaves things:** the original "ideas for next round" list
(round 1) is essentially exhausted for the low-effort items. What's left
is lower-confidence, more speculative work: re-sourcing Djerba->Nice /
SLC->Amsterdam (two more WebSearch attempts across rounds 1-3 found
nothing), or building more cases per detector for robustness rather than
first-time coverage (diminishing returns — six single-case passes is
meaningful signal, but not the same as knowing where the failure modes
are). Good next-session question: is broader coverage (more cases per
detector, esp. ones that stress-test the SUPPRESSION logic — corridor
buffer zones, holding-near-destination exclusion, cross-provider
consensus downgrade — rather than the detection logic) more valuable than
continuing to chase single first-pass cases.

## Round 5 — 2026-08-06 (same day)

Followed up on round 4's closing question — stress-tested the SUPPRESSION
logic (things that make a detector go quiet) rather than chasing another
first-pass case. Two items were already scoped from a prior conversation
(not part of this suite's own findings, but converted into fixes+cases
here); two more were found fresh this round.

**Fix 1: early return-to-origin was 100% blind.**
`detect_landed_wrong_airport` unconditionally excluded "landed back at the
filed ORIGIN" (added to handle regional multi-leg callsign reuse). But a
genuine early-return diversion (technical issue shortly after takeoff,
turning back to depart-from) also lands at the origin — and typically
stays low enough and turns gently enough that neither `course_deviation`
(>=18,000ft floor) nor `corridor_deviation`/`holding_pattern` catch it
either. Fixed: `AircraftTrack.last_takeoff_ts` (set from `just_took_off`
in `main.py`/mirrored in `backtest.py`) lets the exclusion check time-
since-takeoff; only landings at origin within `early_return_max_minutes`
(45, new config key) now fall through to a real event. Added a new,
real, sourced case — Transavia France TO3510 (Orly->Mykonos, smoke smell,
returned to Orly at ~4000ft, ~13min after departure,
https://avherald.com/h?article=53c715b3&opt=0) — squawk kept at 1200 to
test geometry only. **Before the fix this case fired zero events at all**
(confirmed by re-running it against the pre-fix code path); after, it
correctly fires `wrong_airport/BEVESTIGD` at +13min with no other
detector involved.

**Fix 2: `_route_cache` permanently blinded a callsign on one API hiccup.**
`providers.lookup_route` cached `None` forever regardless of *why*
adsbdb returned nothing — a genuine "no data for this callsign" and a
transient timeout/429/connection error were indistinguishable, so one bad
moment blinded that callsign for the rest of the process's lifetime.
Fixed: comm failures now go into a separate `_route_cache_failed_at` dict
with a 5min (`ROUTE_LOOKUP_RETRY_COOLDOWN_S`) cooldown instead of
`_route_cache` itself; a real "adsbdb has no route" answer still caches
permanently as before. Found and fixed a compounding bug while wiring
this up: `main.py` also latched `track.route_checked = True` on ANY
lookup result including a transient failure, independent of the module
cache — meaning even after the cache-level fix, the SPECIFIC track whose
one lookup attempt failed would stay routeless for its *entire* remaining
flight (tracks aren't re-created while continuously seen; only a new
track for a later flight would benefit from the module-level fix). Fixed
by exposing `providers.route_lookup_pending_retry(callsign)` and only
latching `route_checked` when the answer was definitive. Verified with a
standalone async repro (mocked `_get_json`: fails once, cooldown blocks a
network retry, succeeds after cooldown expires, permanently caches the
success) — not backtested via `backtest.py`, since this is a live-network
provider concern the synthetic geometry harness doesn't model.

**Found while wiring fix 1 through `cfg`: `learned_route_min_times_seen`
was dead config.** `detect_landed_wrong_airport` called
`db_module.get_learned_destinations(db_conn, track.callsign)` without
ever passing the config value through — the function's own hardcoded
default (2) was always used, so changing `learned_route_min_times_seen`
in `config.json` had zero effect. Confirmed via a grep cross-check: every
other key in `config.py`'s `_DEFAULTS` is referenced at least once
elsewhere in the codebase; this was the only one at zero. Trivial fix —
`cfg["learned_route_min_times_seen"]` is now threaded through (only
possible now that `detect_landed_wrong_airport` takes `cfg` at all, per
fix 1 above).

**Fix 3 (added after user follow-up: stop asking, just decide and fix):**
`route_plausible`'s ongoing mid-flight recheck (`main.py` tier1_loop,
added to fix a real historical false-positive — UAL840, a callsign
collision where two different aircraft shared one broadcast string, corridor_deviation
computed a nonsensical number off the wrong flight's data) can also trip
on a GENUINE diversion, not just bad data — and when it does, the
consequences are worse than previously realized:

1. The moment `track.route` gets nulled (implausible sum-of-distances
   vs. 1.5x route length), `evaluate()` runs in that SAME cycle with
   `route=None` already — corridor_deviation/premature_descent/
   holding_pattern/landed_wrong_airport lose that cycle's data entirely,
   not just future ones.
2. The VERY NEXT cycle's re-lookup hits the permanent `_route_cache` (the
   route is real and resolved, just now "implausible" — nothing about
   `lookup_route` itself is at fault here), reconfirms implausibility
   against the now-further-diverged position, and **permanently** sets
   `route_checked = True` with `route = None` — nothing in the codebase
   ever resets `route_checked` back to False once this happens. The
   track is routeless for the rest of its tracked lifetime, which for a
   continuously-transmitting aircraft is normally its entire remaining
   flight.
3. Reproduced concretely with a standalone script (not added to
   `backtest.py` — this is main.py orchestration logic, not detector
   logic, and the existing harness doesn't model the recheck at all) on
   a synthetic 400nm route: a diversion that just continues PAST the
   filed destination in a straight line (no lateral turn — e.g. a missed
   approach continuing to a further alternate instead of holding) trips
   `route_plausible` and nulls the route at 90% of the way to the
   *original* destination — before the diversion even properly begins.
   Because the aircraft never leaves the great-circle line (cross-track
   distance ~0 throughout), `corridor_deviation` never fires either — so
   for this specific overflight pattern, EVERY route-dependent detector
   is silent, including `landed_wrong_airport` at the eventual touchdown.
   Whether this actually bites in practice depends on route length: the
   backtested EK225/AF9 cases don't trigger it (their routes are long
   enough, or the diversion stays close enough to origin, that the sum
   stays under 1.5x even at landing) — the risk is concentrated in
   short/medium-haul routes where a diversion continues a meaningful
   fraction of the route's own length past the filed destination.

   **Fix:** `route_plausible` (`airports.py`) now takes a `check_progress`
   parameter. `check_progress=True` (default, unchanged) is the original
   full check — used at initial route resolution in `main.py`, where being
   strict is still correct (a genuinely-matched route shouldn't already
   show a huge detour on its very first data point). `check_progress=False`
   drops the along-track sum-of-distances requirement and relies on
   cross-track distance alone — used for `main.py`'s ONGOING recheck of an
   already-trusted route. Cross-track distance alone still catches the
   original UAL840-style failure mode (an unrelated flight is normally
   nowhere near the filed route's *line*, not just further along it) while
   no longer stripping a route just because a real diversion continued
   past its destination.

   `backtest.py`'s harness got a matching, faithful mirror of BOTH main.py
   call sites (previously it had none at all — a track's route was just
   set once, unconditionally, on every cycle where it happened to be
   `None`, which doesn't reflect `route_checked`'s real gating and would
   have silently "self-healed" any cleared route the very next cycle
   regardless of the fix). Added a new case exercising it —
   `_atl_mco_fll()`, NOT a sourced incident (labelled as a mechanism test,
   same convention as round 2's `_ua2078_signal_lost`) but built from real
   airports/geometry: filed KATL->KMCO (351nm), continues almost dead
   straight (bearing changes only ~4°, cross-track distance under 7nm the
   whole way) on to KFLL instead of landing. Verified both directions
   directly (not just "the case passes"): with the harness's ongoing-check
   forced back to `check_progress=True` (the old, unfixed behavior), the
   case fires **zero events at all**; with the fix, `wrong_airport/
   BEVESTIGD` fires correctly at landing.

**8/8 cases now detect their expected type** (6 from rounds 1-4 + TO3510 +
ATL-MCO-FLL). Config keys are now fully wired — every key in `config.py`'s
`_DEFAULTS` is referenced somewhere outside `config.py`.

## Round 6 — 2026-08-06 (same day)

Two more findings, outside `backtest.py`'s own scope (orchestration/
dashboard, not detector geometry — no backtest case applies), found while
continuing the same proactive review:

**`main.py`'s per-cycle route-lookup batch could starve under load.**
`tier1_loop` picked its `ROUTE_LOOKUP_BATCH=20` callsigns-to-resolve by
iterating `snapshot.values()` and `break`-ing once 20 were collected —
i.e. always the first 20 route-needing aircraft in the snapshot dict's
iteration order. `fetch_world_snapshot` builds that dict from two fixed
API calls each cycle; if that ordering is at all stable across cycles (not
verified against the live API, but nothing rules it out either), a
backlog that regularly exceeds 20 pending lookups — plausible at
worldwide scale — would let the same early aircraft monopolize every
cycle's lookup slots indefinitely, never even reaching whichever aircraft
always land later in that order. Fixed defensively regardless of whether
the live ordering is actually stable: collect all pending candidates, then
`random.sample` down to `ROUTE_LOOKUP_BATCH` — guarantees every pending
callsign eventually gets a turn, with no dependency on external API
ordering behavior either way.

**Dashboard stats silently undercounted on busy days.** `server.py`'s
`/api/events` derived `stats.events24` and `stats.confirmed` from the same
`rows` list it returns for display — which `get_recent_events` caps at
`limit=200`. Once more than 200 events land in the 24h window, the
displayed counts fall below the true numbers, silently, with no error —
exactly when the system is busiest and accurate stats matter most.
`db.py` already has `count_events_since(conn, since_ts, confidence=None)`
built for precisely this, doing a real SQL `COUNT(*)` with no cap — it was
dead code, never called anywhere. Swapped both stats to use it. Verified
with a direct DB test (250 synthetic events, 60 confirmed, in an in-memory
sqlite instance): old logic reported `events24=200` (wrong), new logic
reports `events24=250` (correct); `confirmed` happened to already be
correct in that specific test (all 60 fell within the 200-row cap by
recency) but isn't guaranteed to in general — same fix covers both.

**Biggest known remaining gap, unchanged from the original conversation
that kicked off this session:** non-airline callsigns (cargo, business
jets, charter, military — anything `AIRLINE_CALLSIGN_RE` filters out)
never get a route at all, so 4 of the 6 detectors (`corridor_deviation`,
`premature_descent`, `holding_pattern`, `landed_wrong_airport`) are
structurally unavailable for that whole traffic category — only
`emergency` and `course_deviation` still apply. This needs a genuinely
different, route-independent heuristic (e.g. sustained airborne time +
abrupt reversal + landing somewhere other than the departure point,
without needing a filed route) — a real new detector to design and
backtest from scratch, not a fix to an existing one. Good candidate for
the next session with real budget for it. **Update, round 7: the user
explicitly said this class doesn't matter to them — deprioritized, not
being pursued.**

## Round 7 — 2026-08-06 (same day) — first live production findings

First round driven by REAL false positives observed on the live-deployed
dashboard (Google Cloud VM, Telegram wired up), not backtest/synthetic
review. Four issues reported in one batch; all four had a real, fixable
root cause — none were "tune the threshold" hand-waves.

**1. `route_plausible`'s 1.5x threshold was too loose for real bad adsbdb
data.** AAL974 resolved to a filed SBGL(Rio)->KJFK route while actually a
domestic 737 near Dallas (a widebody-only international route mismatch —
adsbdb's schedule data for that flight number is stale); SWA2820 resolved
to KMCO->KMHT while actually mid a Chicago-Savannah rotation. Queried both
live (adsb.lol) to confirm real positions, computed their actual
sum-of-distances ratios: 1.381x and 1.332x respectively — both UNDER the
old 1.5x threshold, both comfortably ABOVE every legitimate backtest
case's worst point (max 1.087x, EK225's real 5-hour diversion). Tightened
`ROUTE_PLAUSIBLE_PROGRESS_MULTIPLIER` to 1.2x (new named constant in
`airports.py`, replacing the inline `1.5`) — clean margin on both sides of
that gap.

Tightening alone only protects FUTURE resolutions, though — a track that
already has a bad route assigned never gets re-offered to the strict
check (`route_checked` latches, and the ongoing recheck deliberately went
cross-track-only earlier this session specifically so it wouldn't strip a
real diversion). Added `AircraftTrack.route_resolved_ts` and
`ROUTE_REVALIDATION_WINDOW_S` (20min, `airports.py`): the ongoing recheck
uses the strict check only within that window of resolution, then falls
back to cross-track-only — catches a route that "looked plausible by
chance, revealed itself wrong soon after" (the original UAL840 motivation)
without being able to strip a genuine diversion still developing an hour+
later. Verified the 20min window doesn't clip any legitimate backtest
case (all deviate well after 20min, or never exceed 1.2x at all) and that
both AAL974/SWA2820 fail even outside a grace window (their ratios are
static/observed, not developing).

Added `check_bad_route_regressions()` to `backtest.py` (not a `Case` —
these are "should never even get a route" assertions, not "detector
should fire" ones) using the exact real coordinates queried live. Both
now correctly rejected.

**2. `course_deviation` repeatedly false-fired on holding patterns with no
resolved route.** DAL2564: flagged mid-turn in a normal hold near an
airport, 24000ft, `route unknown`. `detect_holding_pattern` itself
requires `track.route` (deliberately — without a route it can't tell
"holding at destination" from "holding elsewhere", and fired 369 times in
one run early in this project without that gate, per its own docstring)
— so it never even ran for this track, leaving `course_deviation`'s own
documented-but-unmitigated orbit vulnerability (see its docstring, "a
single turn looks identical whether it's a diversion or one leg of a
loiter pattern") fully exposed. The existing "held for one more cycle"
stage-2 confirmation isn't enough on its own: a hold's leg between turns
can itself span multiple sample intervals and look exactly as "held" as a
genuine turn.

Fix: extracted `_rotation_and_radius()` (total heading rotation + centroid
radius over a window) out of `detect_holding_pattern` into a shared
helper, and added it as a suppression check in `course_deviation`'s
stage-2 confirmation — deliberately NOT gated on `track.route` the way
`detect_holding_pattern` itself is, since "is this a tight, high-rotation
loiter" doesn't need to know WHERE it's holding, only that it is one.
Refactored `detect_holding_pattern` to use the same helper (behavior
unchanged — AI850's case still fires at the identical +40m00s).

Added `check_course_deviation_holding_suppression()` — a hand-built
racetrack sample sequence (stable leg, ~180° reversal, repeat, matching
real ATC hold timing) confirms course_deviation doesn't keep re-firing
turn after turn for a sustained hold (a genuine, if not 100% absolute,
improvement: the very first turn of a freshly-observed hold can still
fire once, since a repeating pattern can't be recognized as such before
it's repeated — but that's a single alert, not one per turn for the
hold's whole duration), while a genuine one-off turn with real
displacement still correctly fires.

**3. The ADS-B 'emergency status' subfield produced false BEVESTIGD
emergency alerts, all on squawk 1000.** Four live cases (EWG3BZ "nordo",
EWG45B "lifeguard", ITY067 "lifeguard", a fourth) — all squawking 1000
(a normal Mode-S conspicuity code used where ATC doesn't assign a
discrete squawk, common in parts of Europe, NOT an emergency code) yet
all reporting a serious `emergency` field value with nothing else
corroborating it. This is a real field (`raw.get("emergency")`, passed
through unmodified in `providers._normalize_aircraft` — not something the
code invented or misread), distinct from the 7700/7600/7500 squawk codes,
which remain a deliberate, reliable pilot action and stay untouched.
Given the pattern (4/4 on the same unrelated squawk code, no
corroborating symptom), this looks like a real upstream decode/reporting
quirk tied to that code path — not confirmed via any public source, but
the empirical pattern across 4 independent flights is strong enough to
act on regardless of the exact root cause.

Fix: added `providers.cross_provider_confirms_emergency()` (mirrors the
existing `cross_provider_agrees` used for `wrong_airport`) — checks
whether airplanes.live's own view of the aircraft ALSO shows a non-'none'
emergency status. Wired into `main.py`'s `enrich_and_dispatch` for
`event_type == "emergency"` specifically when `ev.squawk` is NOT one of
the 7700/7600/7500 codes — downgrades to WAARSCHIJNLIJK on disagreement,
same pattern as the existing wrong_airport check, leaves squawk-code
emergencies alone entirely. Verified the corroboration function directly
with mocked provider responses: disagreement -> False, agreement -> True,
second-provider-unreachable -> None (unconfirmed, not penalized) — not
addable to the geometry-only backtest harness since it's a live-network
dispatch-time enrichment, not detector logic.

**4. Dashboard showed "SQ 1000" with no explanation**, read by the user as
possibly meaning something was wrong. Changed the label to "Squawk" in
`Flight Diversions Dashboard.dc.html`.

**Result:** 8/8 incident cases + all bad-route/holding-suppression
regression checks pass. First round where every finding came from a real,
live, currently-deployed system rather than research/backtest review —
notably, the codebase's OWN cross-provider-corroboration pattern (built
for `wrong_airport`) turned out to generalize cleanly to a completely
different, unanticipated failure mode (the emergency status field) once
one was actually found in production.
