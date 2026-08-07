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

## Round 8 — 2026-08-06 (same day) — MASTERPLAN.md phase 0

Follow-up session: pulled `/api/events` from the live-deployed dashboard
directly (not just re-reading round 7's findings) to re-diagnose "the
dashboard is still mostly false positives" against fresh data, wrote
`MASTERPLAN.md` (a full redesign toward a stateful incident/scoring system,
phased for later sessions), then implemented that plan's phase 0 —
the subset that needed no new external data source and no architecture
change, just using data already being fetched plus generalizing an
existing, already-validated heuristic.

**Live re-diagnosis (fresh numbers, not a repeat of round 7):** of the last
~200 dashboard events, 88% were MOGELIJK/POSSIBLE and even the
BEVESTIGD/CONFIRMED bucket wasn't clean — AUA96J reached CONFIRMED on
`emergency == "reserved"`, a DO-260B-unassigned value that can't represent
a real pilot action. `corridor_deviation` fired repeatedly on ordinary
wind/airway-bowed routing with zero corroboration (AAL168 KPDX->KCLT
503nm off the great-circle line, ASH4002 KIAH->KOKC 85nm off, NOZ1865
LIBD->ENGM 152nm off — the same "legitimate bow" pattern the existing
Russia-zone exception already knew about, just not generalized). Several
`course_deviation` events had no resolved route at all despite clearly
airline-style callsigns (AAL1936, AAY2648) — an adsbdb coverage gap, not a
detector problem. Full writeup with all examples: `MASTERPLAN.md` sectie 1.

**Change 1: two free fields were sitting unused in every response.**
`providers._normalize_aircraft` now extracts `category` (ADS-B emitter
category — A7=rotorcraft, A1/A2=light/small GA, A3-A5=airliner-scale,
B1-B7=glider/UAV/ultralight/etc, all per DO-260B) and `db_flags`
(bit0=military) from the raw adsb.lol/airplanes.live payload — both were
already being fetched every cycle, just never read. New `classify.py`
uses these plus the existing `AIRLINE_CALLSIGN_RE` callsign pattern (and,
for business jets without a light ADS-B category, a small hardcoded
ICAO-type list) to sort every tracked aircraft into MILITAIR / HELIKOPTER
/ OVERIG_LICHT / AIRLINER / GA_PRIVE / ZAKENJET / ONBEKEND, cached per
track for `classification_cache_ttl_hours` (new config key, default 24h —
aircraft type/operator can't change mid-flight).

**Change 2: non-airliner classes skip the five behavioral detectors
entirely.** New config key `classification_suppress_non_airliner`
(default on). `detector.evaluate()` gained a `skip_behavioral` parameter;
when the classifier puts a track in MILITAIR/HELIKOPTER/OVERIG_LICHT/
GA_PRIVE/ZAKENJET, `main.py`'s `tier1_loop` skips straight to
`detect_emergency` only — `course_deviation`, `corridor_deviation`,
`holding_pattern`, `premature_descent`, `landed_wrong_airport`, and
`signal_lost_near_airport` never even run, so no incident is opened for
routine GA/military/helicopter/business-jet traffic on an "unknown route"
that was never going to have one. `detect_emergency` (squawk-based) is
deliberately NEVER gated by class — a real emergency squawk from a Cessna
or an F-16 is exactly as real as from an airliner. Not backtested via
`backtest.py`'s Case harness (that's geometry/timing, this is a
classification lookup) — a new `check_classification_regressions()` tests
`classify.classify()` directly instead, sanity-checked against real
`dbFlags`/`category` values pulled live from `api.adsb.lol/v2/mil`
(an H60 and an AS65 helicopter and a C-17, all correctly MILITAIR — dbFlags
wins over the A7/A5 categories that would otherwise say HELIKOPTER/nothing
special) plus synthetic cases for the branches a military-only feed can't
exercise.

**Change 3: `emergency == "reserved"` normalized to `"none"` at the
source.** `providers._normalize_aircraft` now does this unconditionally,
so `detect_emergency` and any future consumer see it consistently instead
of each needing its own special case. `check_emergency_status_regressions()`
added to `backtest.py`.

**Change 4: conspicuity-squawk emergency-status capped, not
cross-provider-corroborated.** `main.py`'s `enrich_and_dispatch`: an
`emergency` event on squawk 1000/2000 (new `providers.CONSPICUITY_SQUAWKS`
— 1000 is live-confirmed per round 7, 2000 follows the same international
VFR-conspicuity convention but isn't separately live-verified, see
`MASTERPLAN.md` sectie 14) now caps at WAARSCHIJNLIJK unconditionally,
instead of asking `cross_provider_confirms_emergency` whether to downgrade
from BEVESTIGD. Reasoning: AUA96J/AUA12K/CFG6UA (round 7's three live
cases) all squawked 1000, and cross-provider agreement did NOT catch two
of them — adsb.lol and airplanes.live have overlapping feeder coverage and
decode this subfield the same way, so agreement here isn't independent
confirmation the way it is for ground state. The corroboration call is
still used, unchanged, for a real discrete-squawk emergency-status event.

**Change 5: generalized the Russia-zone bow-tolerance check to every
route** (`detector.py`, both `detect_route_corridor_deviation` and, newly,
`detect_course_deviation`'s stage-2 confirmation). Previously this only
suppressed a deviation whose current heading still points at the filed
destination for routes crossing the Russia/Siberia zone
(`crosses_russian_airspace_zone`) — see round-1-era comments. Live evidence
this round (AAL168/ASH4002/NOZ1865 above) showed the identical pattern
everywhere, not just that one geography, so the check now runs
unconditionally, keyed only on the existing `corridor_deviation_bow_heading_deg`
config value (unchanged, 60°). One deliberate behavior change from the
original: a MISSING heading sample no longer suppresses (the original
`p_latest.track is None -> suppress` fallback made sense only within the
narrow Russia exception; applied globally it would risk silencing a real
diversion during a brief heading-data gap) — now only suppresses on
positive evidence heading still points at the destination.
`crosses_russian_airspace_zone` has no remaining call site and was removed
from `airports.py`, along with `great_circle_max_latitude` (already
unused before this round, an apparent leftover from an earlier rejected
approach — same file, same cleanup pass).

New `check_corridor_deviation_bow_suppression()` added to `backtest.py`:
a synthetic same-latitude origin/destination pair with a position bowed
well off the great-circle line (cross-track distance verified with a
throwaway script against this specific route's threshold before writing
the case, not guessed — first attempt at 4° of latitude displacement
undershot the threshold entirely and would have passed for the wrong
reason). Confirms a bowed-but-still-inbound heading suppresses and a
bowed-and-no-longer-inbound heading still fires.

**Result:** 8/8 incident cases still detect their expected type (no
detector-level regression from the bow-tolerance change),
`check_classification_regressions` (new), `check_emergency_status_regressions`
(new), `check_corridor_deviation_bow_suppression` (new), and both existing
regression checks (`check_bad_route_regressions`,
`check_course_deviation_holding_suppression`) all pass. Every file compiles
cleanly. Not yet deployed to the live VM (no SSH access from this session —
see `MASTERPLAN.md` sectie 14's open question on whether the VM has even
picked up round 7's fixes yet). Phases 1-4 of `MASTERPLAN.md` (wider route
resolution via hexdb.io, SIGMET/peer-consensus weather context, the full
incident-lifecycle/scoring engine, NOTAM/TFR) remain to build.

## Round 9 — 2026-08-06 (same day) — MASTERPLAN.md phase 1

Continued straight on from round 8, autonomously (user stepped away,
asked for phases 1+ to proceed without stopping to ask). hexdb.io
(`providers.lookup_route_hexdb`/`lookup_aircraft_hexdb`, both smoke-tested
live against the real service earlier this session — see `MASTERPLAN.md`
sectie 2.2) wired in as: (1) a route fallback in `main.py`'s `tier1_loop`,
tried only for callsigns adsbdb.com had nothing for that cycle, ICAO pair
resolved to coordinates via the local `AirportDB`; (2) a
`classify.refine_with_hexdb` second opinion for aircraft `classify()` left
ONBEKEND, using `RegisteredOwners`/`OperatorFlagCode` — batched the same
way as route lookups (`CLASS_LOOKUP_BATCH`, random-sampled if the backlog
exceeds it, same starvation-avoidance reasoning as `ROUTE_LOOKUP_BATCH`
from round 6). Also fixed adsbdb's own negative-route-cache from
permanent to `route_lookup_negative_retry_hours` (new
`providers.route_lookup_negative_retry_due`) — documented one accepted,
narrower residual gap in a `main.py` comment: a track whose `route_checked`
already latched True during an earlier "not yet due" window isn't
proactively re-checked once the window opens, only a freshly-created track
(e.g. a later flight, same callsign) benefits automatically. `/v2/mil` (a
second, authoritative confirmation of the military dbFlags bit already
used since round 8) was scoped for this phase but skipped as low marginal
value for the added complexity — noted in `MASTERPLAN.md` as easy to add
later.

New `check_route_widening_regressions()`: pure-logic checks (hexdb.io
route-string parsing, negative-retry timing across all four states —
never-looked-up / fresh-negative / aged-negative / real-route-cached) that
don't need a live call, mirroring how `providers.route_lookup_pending_retry`
was verified in round 5 (a standalone repro, not added to the geometry-only
harness). One bug caught by the check itself while writing it: the test
first used a lowercase callsign key while
`route_lookup_negative_retry_due` normalizes to uppercase internally,
silently missing the cache on every lookup and reporting false negatives
across the board — fixed by uppercasing the test's own callsign.

**Result:** 8/8 + all 6 regression-check groups (2 pre-existing + 4 new
across rounds 8-9) pass. Phase 2 (SIGMET/AIRMET/CWA weather context,
peer-consensus airspace-avoidance clustering) and phase 3 (the full
incident-lifecycle/scoring engine + dashboard rewrite) still to build;
continuing autonomously into those next.

## Round 10 — 2026-08-07 (continued autonomously overnight, user asleep)

Built `MASTERPLAN.md` phase 3: the incident-lifecycle engine
(`incidents.py`, new) that replaces the old "detector fires -> dispatch
immediately, forget" model with persistent, continuously re-scored
incidents. Fully wired into the live pipeline, not left as a standalone
module — `main.py`'s `enrich_and_dispatch` split into `enrich_events`
(the existing cross-provider/weather/FR24 enrichment, unchanged, minus the
dispatch step) and a new per-aircraft `IncidentManager.step()` call in
both `tier0_loop` and `tier1_loop` (and `serve_all.py`'s startup, which
constructs the shared `IncidentManager`). `notifier.py` gained
`notify_incident_transition` (fires only on first reaching WAARSCHIJNLIJK/
BEVESTIGD, or a stand-down close from either) and lost the old
per-Event `notify()`/`format_message()` — nothing called them anymore.
`server.py` gained `/api/incidents` alongside the unchanged `/api/events`.
Also added a structured `Event.at_destination` flag (`detector.py`, set by
`detect_holding_pattern`) so `incidents.py`'s scoring doesn't need to
pattern-match `ev.message` text to tell a destination-hold apart from a
non-destination one.

**Design note, not in the original `MASTERPLAN.md` sectie 3.3 table:** that
table sketched a weak "+10, heading still ~toward destination" score tier
for corridor/course-deviation, distinct from the strong "+30, heading no
longer toward destination" tier. Once actually building `incidents.py`,
realized this distinction is now moot — round 8's bow-tolerance
generalization (sectie 8) already hard-suppresses the "still heading
toward destination" case at the DETECTOR level (`detect_route_
corridor_deviation`/`detect_course_deviation` simply never return an Event
for it anymore). So every Event that reaches `incidents.py` for these two
types is already the strong tier; the weak tier was implemented and
immediately dead code, so it was never added. Documented as a deliberate
simplification in `MASTERPLAN.md`, not silently dropped.

**Testing, two layers:**
1. `backtest.py`'s new `check_incident_engine_regressions()` — an
   in-memory-sqlite (`db.connect(':memory:')`, new optional param on
   `db.connect`) standalone harness exercising 7 scenarios without any
   network call: first-hit vs. repeat-hit scoring, WAARSCHIJNLIJK/BEVESTIGD
   threshold crossings, notification-gating (no duplicate escalation
   notice on an unchanged state), deviation-recovery (a deviation-only
   incident loses score once heading points back at the filed destination
   — computed via a real `AirportDB.get()` lookup, not a hardcoded
   coordinate), landing-at-expected-destination auto-close, and idle-decay
   auto-close as a false alarm. All 7 pass; 8/8 real incident cases and
   all prior regression-check groups (rounds 1-9) still pass too — no
   regression from threading `Event.at_destination` through or from
   removing the old `alert_cooldown_seconds`-gated dispatch path detector
   logic never depended on.
2. A genuine LIVE smoke test, not just synthetic data — `serve_all.py` run
   locally (this machine, not the deployed VM — no SSH access from this
   session either way) against the real adsb.lol/airplanes.live feeds,
   `TELEGRAM_BOT_TOKEN=""` so nothing could reach the real production
   Telegram chat. Two bounded runs (~2.5min, then a short one for an API
   check). Zero exceptions/tracebacks across several tier0 (15s) and tier1
   (60s) cycles against ~8800-8900 real tracked aircraft per cycle. Caught
   a real, currently-active emergency in the wild: N65440, squawk 7600
   (NORDO) over Colorado — correctly detected, opened straight at
   BEVESTIGD, notified exactly once despite the same emergency
   re-detecting every 15s for the rest of the run (confirms the
   `notified_state` gate works against real repeated detector hits, not
   just the synthetic single-shot test in check_incident_engine_
   regressions), and correctly reloaded from the local sqlite db as still
   open after a full process restart between the two runs. `/api/incidents`
   and `/api/events` both manually curled against the running local
   server and both returned well-formed, expected JSON.

**Found via the live test, not backtest.py (a real gap the synthetic
harness wouldn't surface):** a long-lived BEVESTIGD incident whose
triggering condition keeps re-firing every cycle (exactly what a real,
ongoing NORDO situation does) accumulates score without any cap — the
N65440 test incident reached score ~857 after a few minutes of repeated
+100 emergency-squawk evidence. Not a functional bug (state correctly
stays BEVESTIGD, `notified_state` correctly prevents re-notifying), but
untidy in the evidence timeline/raw score display. Left as a known,
low-priority follow-up (a per-source cap, or skip re-adding identical-
source evidence within some short window) rather than fixed under time
pressure — noted in `MASTERPLAN.md` sectie 3 (fase 3 status) instead of
guessed at.

**Explicitly not done this round, and why:**
- Dashboard HTML redesign (`Flight Diversions Dashboard.dc.html` — the
  incident-cards-with-timeline UI `MASTERPLAN.md` sectie 10 describes).
  The one remaining fase-3 piece that genuinely benefits from a live
  browser to catch visual/interaction bugs, which this session can't do
  safely unsupervised — left for a session that can verify it renders
  correctly rather than shipping an unverified rewrite of the user-facing
  page while they're away.
- Deploying anything to the live VM. No SSH credentials available to this
  session; also a materially riskier action (touches the actually-running
  production service) than local changes, so left for the user or a
  session with deploy access rather than attempted opportunistically.
- Phase 2 (SIGMET/AIRMET/CWA, peer-consensus) and phase 4 (NOTAM/TFR,
  blocked on FAA API registration regardless) — next up, continuing
  autonomously per the user's explicit instruction not to stop and wait.

**Continued same round, still 2026-08-07 — phase 2 built too.** New
`airspace.py`: fetches+caches `aviationweather.gov`'s SIGMET (US + `isigmet`
international) and CWA products (verified live — CWA's `coords` are
STRING lat/lon, unlike SIGMET's numeric ones, a real type gotcha caught by
actually curling the endpoint rather than assuming both matched), hand-
rolled ray-casting point-in-polygon (no new dependency, matches
`airports.py`'s own convention of hand-rolling haversine/bearing/
cross-track-distance instead of pulling in a geo library). Wired into
`incidents.py`'s `reassess()` as a `-50` "weather_explains" evidence
source, applied at most once per incident. `main.py`'s `tier1_loop` fetches
the hazard list once per cycle (SIGMET/CWA don't change nearly as fast as
aircraft positions) and passes it into every `IncidentManager.step()` call
that cycle, synchronously — kept the fetch in `main.py` specifically so
`incidents.py` itself stays free of any network dependency, consistent
with how it was designed and tested in round 9.

Peer-consensus (`incidents.py`, no new file needed) turned out simpler than
`MASTERPLAN.md` sectie 6.2 originally sketched: rather than a separate
per-aircraft lateral-deviation sample tracker, it grid-buckets the
`last_lat`/`last_lon` of currently-OPEN incidents (already tracked
in-memory by `IncidentManager`) and applies `-55` once >= 3 (config:
`peer_consensus_min_aircraft`) land in the same coarse cell
(`peer_consensus_radius_deg`, default 2°). Cheaper, and ties naturally into
the same data structure the rest of the engine already maintains — a
deliberate simplification from the original design, not a shortcut that
drops capability (it still answers the same question: are multiple
independent aircraft reacting to something in the same place at the same
time).

New `check_airspace_regressions()` in `backtest.py`: point-in-polygon
inside/outside on a hand-built square, altitude-band filtering, grid
clustering count, and a full `reassess()` call confirming the
`peer_consensus` evidence actually gets applied — all pass, 8/8 +
everything from rounds 1-9 still pass too.

Re-ran the same bounded live smoke test (local `serve_all.py`,
`TELEGRAM_BOT_TOKEN=""`) with phase 2 wired in: no exceptions across
multiple tier1 cycles, confirming the real SIGMET/CWA fetch integrates
cleanly with the live loop and not just in isolation. N65440's
still-ongoing NORDO emergency (see above) continued to be tracked
correctly across yet another process restart, still notifying exactly
once.

**Found via that same live run, fixed same round:** the BEVESTIGD score-
accumulation issue flagged as a known low-priority follow-up earlier this
round — fixed properly instead of left open, since it was cheap and
already diagnosed. New `incident_score_max` config key (150, comfortable
headroom above the 85 CONFIRMED threshold); `incidents.py`'s `_apply_delta`
clamps to it on every score increase (both the fresh-incident and
existing-incident paths). No behavior change to state/notification logic
(both were already correct) — purely bounds the raw score number shown in
the evidence timeline. All regression checks re-run and still pass after
this change.

## Round 11 — 2026-08-07 — dashboard, deferred code cleanup, /loop set up

User went to sleep mid-round-10, explicitly asked this session to keep
going indefinitely without stopping for input, and to set up `/loop` once
out of self-directed ideas rather than ever just stopping. Two more real
pieces of work landed before reaching for `/loop`:

**Dead-code cleanup found while grepping for dangling references (not
optional — this would have crashed on next start).** Removing the old
per-Event cooldown gate in round 10 left `TrackStore.should_alert`/
`mark_alerted`/`cooldowns` and `db.load_cooldowns`/`save_cooldown` as dead
code (verified via grep: zero remaining call sites) — removed, matching
this project's own standard from round 5 ("every other key in `_DEFAULTS`
is referenced at least once elsewhere; this was the only one at zero").
`alert_cooldown_seconds` dropped from `config.py`/`config.json.example`
too. **Caught a real bug this way**: `main.py` and `serve_all.py` both
still logged `len(store.cooldowns)` at startup — an `AttributeError` on
the very next process start, invisible to `python -m py_compile` (valid
syntax, just a runtime-missing attribute) and not exercised by
`backtest.py` (which never starts `main()`/`serve_all()`). Only caught by
actually re-running the live smoke test after the cleanup — reinforces
why this round kept doing bounded live runs after every change, not just
`backtest.py`. `alert_cooldowns` the SQL table itself was left in place
(not dropped) — no code reads/writes it anymore, but dropping a table is
a real schema migration concern for whatever's on the deployed VM, and
leaving an inert `CREATE TABLE IF NOT EXISTS` costs nothing.

**Dashboard HTML — the one piece round 10 explicitly deferred, done
here instead.** Round 10 held this back reasoning "needs a live browser to
verify, this session doesn't have one" — turned out this session DOES have
Browser tools, so re-evaluated and did it rather than deferring further.
Added a new "Active incidents" section to
`Flight Diversions Dashboard.dc.html`, additive alongside the existing
`/api/events`-sourced feed (not a replacement — lower risk, and the raw
per-detector history stays useful on its own). New `fetchIncidents()` +
5s poll timer (separate from the existing events timer, deliberately not
merged, to avoid touching working code), a Dutch-state-to-existing-palette
mapping (`incidentStateMeta()`, reuses the CONFIRMED/LIKELY/POSSIBLE
colors already defined for the event feed rather than inventing a second
palette), and an expandable per-incident evidence timeline.

Verified live, not just written and assumed correct: ran `serve_all.py`
locally (`TELEGRAM_BOT_TOKEN=""`), opened it in the Browser pane, read the
rendered page text (screenshot unavailable in this headless setup —
`get_page_text`/`read_page` used instead throughout), and exercised the
expand/collapse interaction. That took an extra troubleshooting pass: a
generic `document.querySelectorAll('div')` + text-content filter kept
matching the wrong (outer, non-clickable) nested div, and a
`style*="cursor:pointer"` attribute selector missed because the rendered
DOM has `cursor: pointer;` (a space after the colon) — resolved by reading
the actual rendered `outerHTML` first to get the exact `data-dc-tpl` id,
then targeting that directly. Confirmed: incident data loads, the toggle
correctly expands/collapses, the evidence timeline renders each
`incident_evidence` row, and the round-10 score cap + decay are visibly
working (108 → 92 across roughly a minute of real time on the still-open
N65440 incident). Checked the browser console for anything related to the
new code — none. Did find a **pre-existing, unrelated** console error in
the map section (`gridV`/`gridH`/`markers` SVG `<line>`/`<circle>`
elements — "Expected length" on their `{{ }}`-templated x1/y1/x2/y2/cx/cy
attributes) — not touched by this change, not investigated further
(a full map-rendering bug hunt is its own task, and this round's actual
addition doesn't depend on the map working). Logged for a future round
rather than either silently ignored or fixed blind under time pressure.

`python backtest.py` re-run clean (8/8 + all regression-check groups)
after both the cleanup and the dashboard change.

**Set up `/loop` (dynamic/self-paced mode) as explicitly instructed**, so
this project keeps getting worked on across turns without the user needing
to be present — pointed at `MASTERPLAN.md`/`BACKTEST_LOG.md` for context
continuity, phase 4 (NOTAM/TFR) and further live-data-driven false-positive
hunting as the next concrete directions, with hard rules carried into every
future iteration: never deploy to the live VM (no SSH access, and too
risky to attempt unsupervised regardless), never use real Telegram
credentials in local testing, and always re-run `backtest.py` (plus a
bounded live smoke test for anything touching the live loops) after a
functional change before calling it done.

## Round 12 — 2026-08-07 (`/loop` iteration) — map console errors ruled out

First `/loop` wake-up. Picked up item 2 from the queued list: investigate
the map SVG console errors flagged (but not chased) at the end of round 11.

Reproduced live: started `serve_all.py` locally (`TELEGRAM_BOT_TOKEN=""`),
opened it in the Browser pane, read the console. Same errors as round 11
("Expected length" on `<line>`/`<circle>` x1/y1/x2/y2/cx/cy). Key test:
waited past two 5s poll cycles (12s) and re-read the console — the error
count did NOT grow (still exactly 10 lines, the same batch from initial
load). If this were a real per-render bug, every `setState`-triggered
re-render (every 5s, from `fetchEvents`/`fetchIncidents`) would emit a
fresh batch. It doesn't. Then checked the ACTUAL rendered DOM directly
(`document.querySelectorAll('circle'/'line')`, read their `cx`/`cy`/
`x1`/`y1`/`x2`/`y2` attributes post-hydration): all numeric, all correct
(e.g. a grid line at `x1="166.7"`, matching the `(lon+180)/360*2000`
formula in `renderVals()`).

**Conclusion: not a bug.** `Flight Diversions Dashboard.dc.html` is served
as a static file (`server.py`'s `web.FileResponse`) containing the raw,
un-rendered template markup (literal `{{ g.x }}` etc. as attribute text)
inside `<x-dc>`. The browser's native HTML/SVG parser starts parsing that
raw markup — including validating `x1`/`cx`/etc. as SVG `<length>`
attributes — the instant it streams in, before `support.js` finishes
loading and its `boot()` function replaces `<x-dc>` with the real,
JS-rendered `#dc-root` tree. That one-time parse of literal placeholder
text as an SVG length is what Chrome logs the "Expected length" warning
for. Once `boot()` swaps in the hydrated tree (milliseconds later), every
subsequent render goes through React's virtual DOM with real resolved
numbers — no parser ever sees the placeholder text again, which is
exactly the "10 errors at load, 0 more after" pattern observed. This is a
property of the `{{ }}`-in-raw-SVG-markup pattern this whole `dc-runtime`
template system uses everywhere (not specific to the map, and not
introduced by round 11's incident-panel work) — genuinely nothing to fix
in `Flight Diversions Dashboard.dc.html` itself; the "root cause" is the
framework's static-markup-then-hydrate architecture, `dc-runtime`
(`support.js`) is explicitly marked GENERATED/do-not-edit, and the effect
is cosmetic console noise only, not a visible or functional defect (the
"LIVE MAP · N ACTIVE" label, the confirmed-aircraft sidebar list, and now
the directly-inspected DOM attributes all confirmed correct in both round
11 and this round). Closed out in `MASTERPLAN.md` sectie 10 — no longer an
open item.

`python backtest.py` unaffected (no Python changed this round) — re-run
anyway per this session's own stated discipline, still 8/8 + all
regression-check groups green.

Next: phase 4 (NOTAM/TFR, community-scraper route) or new backtest-case
research, continuing autonomously.

## Round 13 — 2026-08-07 (`/loop` iteration) — phase 4 (NOTAM/TFR), no registration needed after all

`MASTERPLAN.md` sectie 2.4 assumed TFRs would need either FAA API
registration (manual approval, can't be done from an unattended session)
or an unofficial community scraper. Neither turned out necessary — found
by actually loading `https://tfr.faa.gov/tfr3/` in the Browser pane and
reading its real network requests (`read_network_requests`) instead of
guessing endpoint URLs the way the earlier `curl` probes against
`/tfr3/api/exportTFRJson` etc. had (all 404 — the site is a Nuxt SPA,
static-URL-guessing was never going to find its actual API). The real
site calls `https://tfr.faa.gov/geoserver/TFR/ows` (a standard GeoServer
WFS endpoint, `typeName=TFR:V_TFR_LOC`) — public, unauthenticated,
returns current TFR polygons as GeoJSON. Verified via plain `curl` (no
special headers, default user-agent) that this specific endpoint isn't
behind the Akamai bot-protection visible elsewhere on the site (`/akam/...`
requests in the network log) — HTTP 200, real data, both for the
geometry endpoint and `tfrapi/getTfrList` (a companion text/metadata
endpoint, not currently used but noted). Requesting
`srsname=EPSG:4326` gets WGS84 lat/lon directly instead of the UI's
native Web Mercator (EPSG:3857) — confirmed by cross-checking one
feature's coordinates against Ouray, CO's real location.

Implemented in `airspace.py`: `_parse_tfr_feature` (Polygon-only —
MultiPolygon/other rare shapes skipped, not crashed, matching this
project's established "start simple" pattern) + `_fetch_tfrs`, combined
with SIGMET/CWA in `get_active_hazards` behind a new `notam_tfr_enabled`
config flag (on by default). Known, accepted limitation: TFR altitude
bounds and exact validity windows are free text in the NOTAM title
("Wednesday, July 29, 2026 through Tuesday, August 11, 2026 UTC" — format
varies, UTC vs Local, no fixed grammar) rather than structured fields —
not parsed (a real project on its own, not a quick regex); treated as
always-valid/no-altitude-bound instead, trusting that the GeoServer view
itself already only returns currently-active TFRs (same as what the
public tfr.faa.gov map shows).

`check_airspace_regressions` extended with a real, live-captured TFR
GeoJSON feature (1NM N Ouray, CO, captured this round) — parses correctly,
its polygon actually contains a real point near Ouray (verified via a
throwaway computation before writing the test, not assumed), and a
MultiPolygon geometry is skipped without crashing. `python backtest.py`:
8/8 + all regression-check groups still pass. Bounded live smoke test
(`serve_all.py`, `TELEGRAM_BOT_TOKEN=""`, ~75s) with TFR fetching enabled:
no exceptions, tier1 cycles complete normally.

All four phases of `MASTERPLAN.md` are now built (fase 4 turned out not
to need the deferred, registration-gated path after all). Remaining open
items across the whole plan: new backtest-case research (real, sourceable
diversion incidents beyond the current 8), continued live-data false-
positive hunting, and general tuning of the score weights/thresholds in
`incidents.py` against real observed behavior over time (flagged
throughout `MASTERPLAN.md` as a starting point, not a measured constant).
Continuing autonomously into those next.

## Round 14 — 2026-08-07 (`/loop` iteration) — new backtest case, TK17

Researched a new, real, sourceable diversion not already in the suite
(`WebSearch`, evaluated a few 2026 candidates, same discipline as round
4 — rejected weaker fits before picking one). Chose Turkish Airlines TK17
(Istanbul->Toronto, 2026-07-29, technical fault ~4h into the transatlantic
leg, diverted to Manchester) over an also-considered Alaska Airlines
AS181 case (Rome return, squawk 7700) specifically because AS181 would
have been a second data point for a shape already well-covered (squawk
7700 + return-to-origin, see AF9) — TK17 is a genuinely different shape:
Istanbul->Toronto's great circle already bows north across Europe/the UK,
so being over the North Sea 4h in is plausibly close to the FILED route,
not a large corridor deviation the way AI850/EK225 are. Deliberately
chosen to test whether `detect_landed_wrong_airport` still cleanly catches
a genuine diversion via the landing itself when the in-flight geometry
detectors (corridor_deviation, premature_descent) have comparatively
little to grab onto — a different angle than "does a large deviation get
caught," more "does the landing-based backstop hold up on its own."

New `_tk17()` in `backtest_cases.py`, `expected_type="wrong_airport"`.
**Fires correctly, first try** — same pattern as rounds 3/4's "individual
detector logic is solid" observation, now extending to a fourth real
case for `wrong_airport` specifically (UA2078, TO3510, and now TK17, plus
the synthetic ATL-MCO-FLL mechanism test). Source only gives altitudes and
a landing time, no track — position/timing between waypoints approximated
and called out in `notes`, same convention as every other case.

**9/9 cases now detect their expected type.** `python backtest.py`
re-run clean (all regression-check groups too).

## Round 15 — 2026-08-07 (`/loop` iteration) — self-review of this session's own code

Deliberately did what the queued instructions suggested but hadn't been
done yet: read back through `incidents.py` (this session's own, newest,
least-battle-tested module) like a reviewer looking for bugs, rather than
writing more features. Found two real, meaningful gaps — both about
`incidents.py`'s terminal states, both the same root cause class, neither
caught by rounds 8-14's testing because the existing tests never checked
whether a defined *state* was actually *reachable*, only that scoring/
transitions behaved correctly given the states that DID get hit in
testing.

**Bug 1: `CLOSED_LANDED` was dead — a confirmed diversion could mislabel
itself as a false alarm.** `grep -n "CLOSED_LANDED\|CLOSED_TIMEOUT"
incidents.py` (the same technique that caught round 11's `store.cooldowns`
crash) showed both states only appearing in their own definitions and the
`CLOSED_STATES` tuple/`_maybe_notify`'s kind logic — no `_resolve()` call
anywhere ever actually used them. Consequence: `detect_landed_wrong_
airport`'s Event (+90, the strongest ground-truth signal this system has)
pushes an incident to BEVESTIGD and notifies — correct — but then NOTHING
ever closes that incident once the aircraft goes quiet on the wrong
ground (no more emergency squawk, no more evidence). It would just keep
being reassessed like any other open incident, decay like anything else,
and eventually cross the GESLOTEN_VALS_ALARM ("false alarm") auto-close
path — silently relabeling an ALREADY-CONFIRMED real diversion as having
turned out to be nothing. Fixed: `_check_landed` (renamed from
`_check_landed_at_destination`, since it now does two things) closes as
GESLOTEN_GELAND when the aircraft lands anywhere else while the incident
already carries `wrong_airport` evidence, before decay ever gets a chance
to mislabel it.

**Bug 2, same class: any BEVESTIGD/WAARSCHIJNLIJK incident that simply
goes idle (not just wrong_airport ones) had the identical mislabeling
risk**, via the SAME decay-to-`GESLOTEN_VALS_ALARM` path — e.g. a track
that goes stale/pruned after escalating, with no landing ever observed
either. Fixed by branching on `notified_state`: an incident that was
serious enough to have been notified about (WAARSCHIJNLIJK/BEVESTIGD at
some point) now closes as GESLOTEN_TIMEOUT instead — an honest "we lost
the signal, we don't actually know this was fake" label — while an
incident that never escalated past MOGELIJK still correctly closes as
GESLOTEN_VALS_ALARM (that label IS accurate there). This also means
`GESLOTEN_TIMEOUT` is now reachable for the first time too.

**Bug 3, found while writing the regression test for the fixes above (not
independently spotted by inspection — worth noting the review process
itself surfaced it, not just re-reading the code):** `_check_deviation_
recovered`'s eligibility test (`evidence_types <= set(DEVIATION_EVENT_
TYPES)`, a strict subset check) permanently disabled itself the FIRST time
it ever fired — the very evidence row it adds on a successful recovery
(`"deviation_resolved"`) joins `evidence_types`, and since that source
isn't itself a deviation type, every LATER subset check fails forever,
even for a track that diverges again after recovering and would
legitimately need to recover a second time. Same failure mode for
`weather_explains`/`peer_consensus` (also self-added, also not "stronger"
evidence, also would have permanently blocked recovery once either fired
once). Fixed with a new `_NON_REINFORCING_SOURCES` set excluded from the
eligibility check, so only genuinely stronger evidence (emergency,
wrong_airport, holding, premature_descent, signal_lost) blocks recovery —
not the mechanism's own bookkeeping.

Three new regression tests added to `check_incident_engine_regressions`
(now 10 scenarios total): wrong_airport-then-landed-elsewhere closes
GESLOTEN_GELAND; a previously-BEVESTIGD incident going idle closes
GESLOTEN_TIMEOUT (not a false alarm); recovery fires correctly a second
time after an incident diverges again post-recovery. All three fail
against the pre-fix code (verified by reasoning through the exact
mechanism above, matching this session's established practice of
understanding WHY a test would have caught something, not just that it
now passes) and pass after. `python backtest.py`: 9/9 + all regression-
check groups green. Bounded live smoke test (`serve_all.py`,
`TELEGRAM_BOT_TOKEN=""`, ~65s): no exceptions.

**Takeaway for future rounds**, worth stating explicitly: "every defined
constant should be grep-reachable from real code" is now a two-part check
for this specific system — reachable from *some* code path (round 11's
`store.cooldowns` class of bug), AND reachable *at runtime given the
actual state machine's transition logic* (this round's class of bug,
which `grep` alone can't catch — `CLOSED_LANDED` WAS referenced in
`CLOSED_STATES`/`_maybe_notify`, just never actually assigned by any
`_resolve()` call). The second kind needs either a dedicated regression
test per terminal state (now done, 10/10 scenarios covering it) or a
runtime assertion that every state ever gets exercised.

## Round 16 — 2026-08-07 (`/loop` iteration, autonomous overnight) — real-case incident-engine escalation, two live scoring bugs

Followed the queued instruction to build a backtest case that exercises
`incidents.py` itself (multiple detectors firing in succession for the same
aircraft, score/state actually escalating), not just `detector.py`'s
geometry — every existing incident-engine test
(`check_incident_engine_regressions`) only ever fed it hand-built synthetic
`Event`s, never a real case's actual detector output replayed over time.

**Chose AI850** (already in the suite) as the vehicle: replaying its real
track through `detector.py` shows THREE distinct real event_types firing in
sequence for the same aircraft — `holding_pattern` (~88 times over the
~90min hold near Delhi), then `emergency`/squawk 7700 (~30 times during the
MAYDAY), then `wrong_airport` once at the Gwalior landing — exactly the
multi-detector-escalation shape asked for. Added `incident_mgr`/
`aircraft_class` parameters to `backtest.py`'s `run_case()` so a real Case's
samples can drive `IncidentManager.step()` alongside the existing detector
replay, reusing the exact same orchestration-mirror loop (route resolution,
`just_took_off`, ground-state timing) rather than duplicating it — fidelity
to that mirror is the whole point, a second, slightly-different copy would
defeat it.

**Bug 1 found immediately: `holding_pattern` and `premature_descent` had NO
repeat-hit dampening**, unlike `course_deviation`/`corridor_deviation`
(which already only add a smaller top-up on repeats). Every one of
AI850's ~88 consecutive `holding_pattern` hits added the full +25 —
reaching BEVESTIGD (and dispatching a Telegram notification) within 3
MINUTES of the hold first qualifying, regardless of whether the situation
was actually escalating. Confirmed the same shape independently for
`premature_descent` via the existing EK225 premature-descent case: 22
consecutive fires, would also hit CONFIRMED within ~4min of any sustained
descent, real diversion or not (exactly the EJU3722 "long legitimate TMA
approach" scenario `MASTERPLAN.md` sectie 12 already flagged as worth
investigating). Fixed: `incidents.py`'s `score_for_event` now takes
`is_repeat_type` for these two types the same way it already did for the
deviation types (~1/3-of-first-hit ratio, matching the established
convention): holding-at-destination 25→8, holding-elsewhere 35→12,
premature_descent 25→8.

**Bug 2, more fundamental, found while verifying bug 1's fix actually
engaged: it didn't, silently.** `apply_events`' `is_repeat` check compared
`ev.event_type` (e.g. `"holding_pattern"`) against `_evidence_types_seen()`
— which, despite its name, returns evidence *source* strings (e.g.
`"holding_destination"`), not event_types. Those only happen to be equal
for `course_deviation`/`corridor_deviation`/`premature_descent`/
`wrong_airport`, where `score_for_event` uses the event_type itself as the
source. `holding_pattern`'s source is one of two at_destination-dependent
strings, and `emergency`'s is one of three squawk/confidence-dependent
strings — neither ever equals its event_type, so `is_repeat` silently
evaluated to `False` every time for those, regardless of history. Bug 1's
fresh dampening code was correctly written but structurally unreachable for
the exact case it was meant to fix. Root-caused by literally re-running the
manual repro after adding the dampening code and seeing the score climb
+25/cycle instead of the expected +8/cycle — would not have been caught by
`check_incident_engine_regressions`'s existing synthetic scenarios, since
those only ever feed a SECOND event of the same type by directly calling
`score_for_event(ev, True)`/`False` rather than letting `apply_events`
derive `is_repeat` itself.

Fixed by having `apply_events` compute the prospective source via
`score_for_event(ev, False)` first (source selection doesn't depend on
`is_repeat_type`, only the delta does, so this is safe/pure) and comparing
THAT against `existing_sources`, instead of comparing `ev.event_type`
directly. Also renamed `_evidence_types_seen` → `_evidence_sources_seen`
(all 4 other call sites updated) — the old name was itself part of what let
this bug slip through review, since it reads as if it returns event_types.

**Result of both fixes together, re-verified against the real AI850 track:**
score now climbs +8/cycle after the first hold hit (25, 33, 41, 49, 57 →
WAARSCHIJNLIJK at +4min, 65, 73, 81, 89 → BEVESTIGD at +8min) instead of
jumping to BEVESTIGD in 3 cycles flat — a real escalation curve instead of
an near-instant cliff, while still reaching BEVESTIGD well inside a
defensible window (a hold sustained ~31min total, well past the existing
20-cycle destination-hold gate, is still genuinely unusual). The hold-driven
incident then goes idle once the hold clears (aircraft moves off toward
Jaipur) and — confirmed against REAL data for the first time, round 15's
fix was synthetic-only — correctly times out as `GESLOTEN_TIMEOUT` (not
`GESLOTEN_VALS_ALARM`) since it had already reached BEVESTIGD. A SECOND,
separate incident then opens ~63min later from the real MAYDAY squawk,
escalates straight to BEVESTIGD via `emergency_squawk`, and correctly closes
as `GESLOTEN_GELAND` on the Gwalior landing via `wrong_airport` evidence —
also round 15's other fix, also exercised against real data for the first
time. `incidents.py`'s BEVESTIGD-level escalation lands +2h21m before
AI850's real MAYDAY declaration — the same early-warning property round 1
found at the raw-detector level now confirmed to survive all the way
through the incident engine's scoring/state machine.

New `check_incident_engine_real_case_escalation()` in `backtest.py`, run
against AI850: two distinct incidents open for one aircraft; the hold
incident accumulates >=4 damped `holding_destination` evidence rows (not
one jump); peak_score reaches the CONFIRMED threshold and it closes
`GESLOTEN_TIMEOUT` with the honest resolution reason; the incident is never
mislabeled `GESLOTEN_VALS_ALARM`; the second incident's evidence includes
BOTH `emergency_squawk` and `wrong_airport` (real multi-source
corroboration, not a repeat of one type) and closes `GESLOTEN_GELAND`;
BEVESTIGD is reached before the real MAYDAY. Also added a structural,
case-independent invariant check iterating `incidents.REPEATABLE_EVENT_TYPES`
(now genuinely referenced by code, not just a documentation comment) that
asserts every declared-repeatable event_type actually scores a repeat hit
lower than a first hit — guards against a future new detector type being
added to that tuple without wiring dampening, the exact class of bug 1
above.

**9/9 cases + all 9 regression-check groups pass** (`python backtest.py`),
including the new one. Bounded live smoke test (`serve_all.py`,
`TELEGRAM_BOT_TOKEN=""`/`TELEGRAM_CHAT_ID=""`, ~130s, 3 tier1 cycles,
~13700-13780 real aircraft tracked per cycle): no exceptions, and a nice
unplanned confirmation of the round-15 fix on real persisted data — the
1 open incident reloaded from a prior session was N65440 (the NORDO case
from round 10/11), which went idle during this run and correctly closed
`GESLOTEN_TIMEOUT` ("status onbekend, niet bevestigd vals alarm"), not
`GESLOTEN_VALS_ALARM`, since it carried `notified_state=BEVESTIGD` from
its original escalation despite having decayed back down to BEWAKING by
the time it finally crossed the auto-close threshold this run — exactly
the "honest, don't mislabel a real escalation as nothing" behavior that
fix exists for.

**Continued same round — pulled fresh live data** from the deployed
dashboard (`http://35.227.51.25:8787/api/events`, 200 most recent events)
per the queued instruction to keep hunting false-positive patterns.
`/api/incidents` 404s on that host, confirming the deployed VM predates
round 10's incident-engine phase — all analysis below is against the raw
per-detector-hit pipeline, same as rounds 7-8's live diagnoses.

**`premature_descent` is firing at large scale on ordinary global
traffic**: 44 of 200 events (22%; extrapolated from `stats.events24=14714`,
roughly ~3200/day). Every single one is a LONE, non-repeating hit on a
DIFFERENT aircraft (44 distinct callsigns, zero repeats) — a diverse mix of
major carriers worldwide (United, Southwest, Delta, British Airways, KLM,
Lufthansa, El Al, JetBlue, American, ANA-affiliated regionals, etc.), each
still hundreds to ~2000nm from its filed destination. Computed the actual
ratio of reported remaining-distance to the detector's own (already
3x-generous) expected-top-of-descent distance for all 44: every one is at
least 3.0x over that threshold, several 15-28x over (e.g. UAL325 KDEN->KBOS
at 29400ft, 2155nm out, 24.5x over; VOI505 MMPB->KEWR at 25675ft, 1708nm
out, 22.2x over). Only 6/44 also show a `corridor_deviation` for the same
callsign in the same window — most of these aircraft aren't laterally off
their filed route at all, just doing an ordinary mid-cruise altitude change
the detector reads as "early descent toward landing."

**First checked whether a distance-based cap could fix this — it can't.**
Computed the same ratio for the ONE real, sourced case that actually
exercises this detector (EK225's premature-descent variant,
`backtest_cases.py`): it fires 4654nm from its filed KSFO, at a similar
34000ft-class altitude — a HIGHER ratio (45x) than any of the 44 live noise
cases. That's not a coincidence, it's the detector's actual intended
signature: a genuine diversion makes the filed destination irrelevantly far
away, so "still very far from the filed destination while descending" is
exactly the real signal EK225 provides, not a fluke to filter out.
Tightening the multiplier or capping absolute/relative distance would have
directly broken the only sourced case this detector has ever caught,
while barely touching the noise (whose ratios cluster mostly in the
single-digit-to-teens range, well under EK225's 45x).

**The real distinguishing signal instead: duration.** All 44 live cases are
one-off, non-repeating — the same aircraft never reappears with a lower
altitude/closer distance later in the window — consistent with a brief,
ATC-directed cruise step-down (typically executes over 1-3min at moderate
rate, then levels off) rather than a committed descent. EK225's real
descent, by contrast, sustains ~1575ft/min continuously for 20+ minutes
straight. The detector's existing `premature_descent_samples`/
`premature_descent_min_drop_ft` gate (4 samples/4000ft, i.e. a 4-minute
window) is short enough that an ordinary multi-minute step-down can
complete entirely within it before leveling off — the detector's own
docstring already notes it was hardened once against a *single-step*
step-down-then-level pattern (requiring every consecutive sample to
decrease, not just net decrease), but a step spread across ~4 consecutive
minutes still fits inside the existing window untouched by that earlier
fix.

**Fix (`config.py`):** `premature_descent_samples` 4→8,
`premature_descent_min_drop_ft` 4000→6000 (multiplier left at 3.0,
unchanged, since the distance side of the check is deliberately NOT what's
being tightened here — see above). Doubling the window requires roughly
double the sustained, uninterrupted descent duration before firing, which
a brief step-down-then-level pattern won't clear but a real committed
descent easily does — verified against EK225 specifically: it sustains the
stricter window fine (fires 4min later than before, -7m00s lead instead of
-3m00s, still comfortably useful). Explicitly documented in `config.py` as
NOT independently live-reverified post-change (no redeploy access this
session, so there's no way to re-pull fresh live data through the new
threshold and confirm the noise is actually gone) — a reasoned,
backtest-safe improvement consistent with this project's tune-from-live-
evidence culture (same pattern as round 7's `ROUTE_PLAUSIBLE_PROGRESS_
MULTIPLIER` tightening), flagged honestly as needing live confirmation in
a later round rather than claimed as verified.

`python backtest.py`: 9/9 cases (EK225 premature-descent timing shifted as
described above, otherwise unchanged) + all 9 regression-check groups still
green. Bounded live smoke test (`serve_all.py`, `TELEGRAM_BOT_TOKEN=""`/
`TELEGRAM_CHAT_ID=""`, ~75s, 2 tier1 cycles, ~13850 aircraft tracked): no
exceptions.

**Also worth a future round's attention, not chased further here:**
`signal_lost_near_airport` was the next-largest category live (30/200,
15%) — a quick look showed the same likely root cause as `premature_
descent` above (adsbdb route mismatches: e.g. UAL3928 filed SBGL->KIAH,
signal lost near KEWR at -150ft — Newark isn't plausibly "on the way"
between Rio and Houston, suggesting the filed route itself is wrong for
this callsign, same shape as round 7's AAL974/SWA2820). Deliberately NOT
turned into a third detector-geometry change this round without the same
EK225-style cross-check discipline applied to `premature_descent` above —
two geometry changes already touched in one round is enough surface area;
a rushed third change risks breaking `detect_signal_lost_near_airport`'s
own real case (the UA2078 signal-lost variant) without properly verifying
first. Good next-round target, same method: quantify the pattern, then
check it against the real sourced case before touching thresholds.

**Small cleanup, same round:** found two stale comments (`server.py`'s
`FEED_WINDOW_SECONDS` comment, `backtest.py`'s `check_course_deviation_
holding_suppression` docstring) still describing the `alert_cooldown_
seconds`-gated `enrich_and_dispatch` dispatch mechanism in present tense,
even though it was removed in rounds 10-11 when the incident engine took
over dedup/escalation — found while reading through the codebase for this
round's other changes, not from a targeted grep sweep. Fixed both to
describe current behavior (events are now saved unconditionally, dedup
lives in `incidents.py`'s `notified_state` gate) with the historical
mechanism referenced in clearly past-tense terms. No functional change,
`python backtest.py` re-run clean after.

## Round 17 — 2026-08-07 (`/loop` iteration) — signal_lost_near_airport origin blind spot

Followed up on round 16's flagged next-round item: applied the same
rigor to `signal_lost_near_airport` (30/200 live events, second-largest
category after `corridor_deviation`) before touching anything.

Pulled a fresh live snapshot (`35.227.51.25:8787/api/events`) and listed
every `signal_lost_near_airport` event's origin/dest/nearest-airport
triple (29 events this pull). **Found a clean, distinct pattern: 6/29
(~21%) had `nearest == origin_icao`** — e.g. UPS5899 (KSDF->KLAS, nearest
KSDF), RZO507 (LPPD->LEBL, nearest LPPD), AAL710 (KALB->KCLT, nearest
KALB). The detector already excluded "signal lost near the actual
DESTINATION" as routine (its own comment: "low-altitude coverage gaps
happen there too, not just at diversion targets") but had no equivalent
exclusion for ORIGIN — a freshly-departed aircraft still climbing out
below `signal_lost_max_altitude_ft` (10000ft), whose ADS-B coverage
briefly drops near its own (often smaller/regional) departure field, looks
identical to "lost signal somewhere suspicious" to the unmodified check.

**Considered mirroring `detect_landed_wrong_airport`'s existing origin
handling (round 5's `last_takeoff_ts`/`early_return_max_minutes` check)
before implementing anything — and concluded it doesn't transfer.**
`detect_landed_wrong_airport` uses that time window to let a GENUINE
early-return diversion through despite the origin match, because a
landing is unambiguous ground truth (the aircraft definitely touched back
down). `detect_signal_lost_near_airport` only ever has a last-known
AIRBORNE position — "still climbing out on departure" and "already turned
back and inbound" both fall in the exact same early-post-takeoff time
window, so `last_takeoff_ts` can't distinguish them here the way it can
for an actual touchdown. A genuine early return that DOES land is still
caught correctly by `detect_landed_wrong_airport`'s own logic regardless
— this detector only ever sees the weaker, unconfirmed "vanished near
home" case, which isn't reliable evidence either way. **Fix: unconditional
origin exclusion** (`detector.py`, same one-line shape as the existing
destination exclusion, with a comment explaining why the landed_wrong_
airport pattern doesn't apply here).

Verified safe against the one real case that exercises this detector
(UA2078's signal-lost variant, `backtest_cases.py`) before and after: its
last position is near Luke AFB, never origin (KIAH), so the new exclusion
has zero effect on it — confirmed unchanged at lead -12m00s.

New `check_signal_lost_origin_suppression()` in `backtest.py`: a
should-mostly-NOT-fire assertion (last seen at the aircraft's own filed
origin, KIAH/KPHX route) contrasted with a should-still-fire one (last
seen near a real, unrelated third airport, KDFW) — same
suppression-check pattern as `check_corridor_deviation_bow_suppression`/
`check_course_deviation_holding_suppression`.

`python backtest.py`: 9/9 cases (all unchanged) + all 10 regression-check
groups (9 prior + this new one) green. Bounded live smoke test
(`serve_all.py`, `TELEGRAM_BOT_TOKEN=""`/`TELEGRAM_CHAT_ID=""`, ~90s, 2
tier1 cycles, ~13500 aircraft tracked): no exceptions.

**Still open, deliberately not chased this round either:** the
REMAINING ~79% of live `signal_lost_near_airport` hits (23/29) show
`nearest` airports that are neither origin nor destination, and several
look like the SAME adsbdb-route-mismatch root cause round 16 found for
`premature_descent` — e.g. JBU163 (KATL->KBOS, "nearest" reported as
KSRQ/Sarasota FL, nowhere near a plausible Atlanta-Boston great circle) or
SKW5593 (KBGR->KORD, nearest KMEM/Memphis, similarly implausible for a
Bangor-Chicago routing). Unlike the origin-blind-spot fix above, this
needs the harder investigation round 16 explicitly deferred (quantify
whether `nearest` airports are actually geometrically implausible for the
filed route, not just "not origin/dest," then decide whether the fix
belongs in `route_plausible`'s resolution-time check, a secondary source
cross-check, or the detector itself) — left for a dedicated future round
rather than a second rushed pattern-match fix in the same session.

**Continued same round — self-review pass on `airspace.py`/`classify.py`**
(item 5 of the queued instructions), specifically checking assumptions
that hadn't been independently verified against live data before, not
just re-reading the code:

- **`get_active_hazards` compares `validTimeFrom`/`validTimeTo` directly
  against `time.time()`** — worth checking because if aviationweather.gov
  ever returned these as ISO8601 strings instead of numeric epoch
  timestamps, every call would crash (`str <= float` raises `TypeError`)
  inside `main.py`'s tier1 loop. Pulled a live SIGMET+CWA sample directly
  (`aviationweather.gov/api/data/airsigmet`+`/cwa`) and confirmed both are
  plain Python `int` epoch seconds, not strings — no bug, but now verified
  rather than assumed.
- Same live pull also confirmed `altitudeLow1`/`altitudeHi1` are real
  numeric feet values (or `None`), matching `_parse_hazard`'s comparison
  logic, and that `coords` matches the module's own documented SIGMET-vs-
  CWA lat/lon type difference (numeric vs string) exactly.
- **`classify.py`'s branch order**, probed directly with three edge cases
  not covered by `check_classification_regressions`'s existing synthetic
  cases: an airline-callsign aircraft with a LIGHT (A1) category → `ONBEKEND`
  (correctly falls through both the AIRLINER and GA_PRIVE branches rather
  than being misclassified either way — safe, not suppressed, matches the
  documented "prefer a false negative on suppression over silently missing
  a real airliner" design intent); an airline-callsign aircraft with NO
  category data at all → `ONBEKEND` (same safe fallback, relies on
  `refine_with_hexdb`); a business-jet ICAO type reported with a LIGHT
  category → `GA_PRIVE` instead of `ZAKENJET` (the light-category-and-
  non-airline-callsign branch is checked before the business-jet-type
  fallback) — a real labeling imprecision, but functionally harmless since
  both classes are in `SUPPRESSED_CLASSES` with identical behavior; not
  worth a code change, recorded as verified-and-understood rather than
  silently unexamined.

No code changes from this pass — a clean result is still worth recording:
confirms these two modules hold up under direct scrutiny rather than just
having gone unexamined. `python backtest.py` unaffected (no code changed
this part of the round).

## Round 18 — 2026-08-07 (`/loop` iteration) — wrong_airport route-mismatch investigation, real fix

Followed the queued instruction to investigate the deeper adsbdb-route-
mismatch pattern flagged in round 17 before touching anything, starting
with `route_plausible`'s existing design rather than guessing.

**First result: ruled out tightening `route_plausible`'s ongoing cross-
track bound — with real numbers, not just reasoning.** The ongoing recheck
(`check_progress=False`, used after a route's `ROUTE_REVALIDATION_WINDOW_S`
grace period) only rejects when cross-track distance exceeds the FULL
route length — very loose by design (round 5's finding: a real diversion
that overflies its destination has an unboundedly growing along-track
overrun, so the along-track check has to be dropped for the ongoing case;
cross-track was kept as the remaining guard). Computed the actual cross-
track ratio for round 17's suspected-bad-route-data examples (JBU163
KATL->KBOS with last position near KSRQ: 0.41x of route length; SKW5593
KBGR->KORD near KMEM: 0.43x; SWA2591 KDEN->KCLE near KBNA: 0.31x; RPA5783
KBOS->KJAX near KBNA: 0.44x) against a case that looks like a REAL
diversion in the same batch (UAL426 KBOS->KIAH, landed KORD — confirmed by
BOTH `signal_lost_near_airport` AND a follow-up `wrong_airport` for the
same callsign, unlike every other sample which showed only one detector
firing with no corroboration): **UAL426's own ratio is 0.26x — LOWER than
every suspected-fake case.** There is no cross-track threshold that
separates real diversions from bad route data; tightening this bound would
have rejected a genuine diversion while barely touching the noise. Round
5's own conclusion ("no single number can do that job forever") extends to
this specific idea too — confirmed with numbers, not just re-asserted.

**Second, much bigger result: the SAME investigation surfaced a more severe
problem than premature_descent/signal_lost — `wrong_airport` itself.**
Checked whether any of the 25 live `wrong_airport`/BEVESTIGD hits (the
highest-trust detector — immediate confirmed, immediate Telegram dispatch
on the currently-deployed pre-incident-engine pipeline) had ANY
corroborating evidence from another detector for the same callsign in the
same window: **only 1/25 did** (UAL426, above). The other 24 are single,
isolated `wrong_airport` hits with zero other signal — no emergency, no
deviation, no hold, nothing. A real diversion almost always leaves some
other trace; 24 simultaneous, silent, uncorroborated "confirmed diversions"
in one ~20min window is far more consistent with a systematic data problem
than 24 real incidents. Spot-checked several for plausibility: DLH8NK
(Lufthansa) filed EDDF->LEMG (Frankfurt-Malaga) but landed EDDM (Munich) —
Frankfurt-Munich is an extremely common, high-frequency Lufthansa shuttle
route, while a silent, unreported EDDF->LEMG diversion all the way to
Munich with zero corroborating evidence is not a plausible reading of the
same data. Same shape for TAR542 (EBBR->DTTA filed, landed EDDM), DLH6YM
(EDDF->LFMN filed, landed EDDN/Nuremberg — another routine German
domestic hop), TAR648 (DTTJ->EDDM filed, landed EDDF). Read as: adsbdb's
static, crowdsourced schedule-to-callsign mapping is stale/wrong for these
specific flight numbers on this specific day, and the aircraft flew a
completely normal flight to its ACTUAL scheduled destination.

**Why `route_plausible` structurally can't catch this class of error,
explaining why it slipped through both the strict resolution-time check
and the (already-shown-unfixable) ongoing recheck:** `route_plausible`'s
strict check only runs early, soon after a route is first resolved, when
`dist_from_origin` is still small — at that point ANY vaguely-plausible-
direction destination passes trivially (a plane that just took off from
EDDF looks identical whether truly bound for Munich or Malaga; both are
"south-ish"). The mismatch only becomes geometrically obvious much LATER,
exactly at the point the aircraft is about to land at its REAL destination
— which is the exact moment `detect_landed_wrong_airport` fires. This
isn't a threshold-tuning problem at all; it needs an independent second
opinion on the reference data itself, not a better geometric test.

**Fix: a second, independent route source specifically for `wrong_airport`
events** (`main.py`'s `enrich_events`, mirroring the already-established
`cross_provider_agrees`/`cross_provider_confirms_emergency` pattern for
post-detection enrichment). When a `wrong_airport` Event fires, look up
this exact callsign's route via hexdb.io (already used as `main.py`'s
resolution-time fallback for callsigns adsbdb has nothing for, MASTERPLAN.md
sectie 5) — if hexdb.io names a DIFFERENT destination than adsbdb's filed
`dest_icao`, downgrade confidence to WAARSCHIJNLIJK with an explanatory
note, rather than trusting the unconfirmed reference data as BEVESTIGD. No
hexdb.io data at all leaves confidence unchanged (unconfirmed, not
penalized) — same "disagreement downgrades, silence doesn't" pattern as
the existing ground-state cross-provider check right above it in the same
function. Deliberately its own gate (`route_secondary_source_enabled`),
NOT nested inside the existing `cross_provider_consensus_enabled` block —
caught and fixed a real placement bug while writing this: the first draft
nested it there, which would have silently disabled the new route
cross-check any time a user disables ADS-B PROVIDER consensus (adsb.lol vs
airplanes.live) for an unrelated reason, an entirely different concern
from ROUTE-SOURCE consensus (adsbdb vs hexdb.io).

**Confirmed the one real hard part doesn't apply here — unlike the
cross-track-ratio finding above, this fix has NO false-negative risk for
genuine diversions with a matching hexdb.io route**, since it only ever
downgrades on active disagreement between two independent sources, never
on agreement or silence; a real diversion where hexdb.io's data happens to
also be stale would simply not get this extra protection (an acceptable,
pre-existing residual gap, not a regression).

New `check_wrong_airport_route_crosscheck()` in `backtest.py`: a standalone
async repro with a mocked `providers.lookup_route_hexdb` (same convention
as round 5's `route_lookup_pending_retry` verification — this is
`main.py`'s async enrichment layer, a live-network provider concern the
synthetic `Case`/`detector.py` geometry harness doesn't model, so not
addable as a `Case`). Three scenarios: hexdb.io disagrees -> downgraded
with an explanatory note; hexdb.io agrees -> stays BEVESTIGD; hexdb.io has
no data -> stays BEVESTIGD (unconfirmed, not penalized). All three
isolated from the function's OTHER enrichment branches (cross-provider
consensus, weather, FR24 all disabled in the test config) so only the new
code path is exercised.

**This change lives entirely in `main.py`'s async enrichment layer, never
touched by `backtest.py`'s `Case`/`run_case` harness** — confirmed none of
the 9 real cases (including the 4 that specifically exercise
`detect_landed_wrong_airport`: UA2078, TO3510, ATL-MCO-FLL, TK17) could
possibly be affected by this change, since `run_case` calls
`detect_emergency`/`evaluate()` directly and never goes through
`main.enrich_events`. `python backtest.py`: 9/9 cases (all unchanged,
confirming the above) + all 11 regression-check groups (10 prior + this
new one) green. Bounded live smoke test (`serve_all.py`,
`TELEGRAM_BOT_TOKEN=""`/`TELEGRAM_CHAT_ID=""`, ~100s, 2 tier1 cycles,
~13000-13300 aircraft tracked): no exceptions; a real, live emergency
(GBODB, squawk 7600/NORDO) was correctly detected, opened straight at
BEVESTIGD, and notified exactly once across several repeated 15-18s tier0
hits — confirms the unrelated parts of the live loop are unaffected. No
live `wrong_airport` event happened to fire during this specific short
window to exercise the new code path end-to-end against the real hexdb.io
API — acceptable given the mocked test covers the logic directly and the
function it calls (`providers.lookup_route_hexdb`) was already live-
verified in round 9.

## Round 19 — 2026-08-07 (`/loop` iteration) — course_deviation dead code, live-VM staleness proof

Pulled a fresh live snapshot to apply the same corroboration/plausibility
analysis to `course_deviation` and `holding_pattern` (round 18's item 2).
Small samples this pull (9 and 6 events respectively) — one
`course_deviation` event stood out immediately: SFR325 (FACT->FALA
resolved) fired with the message "aanhoudende koerswijziging ... (nieuwe
koers wijst nu wél richting FALA)" — i.e. the message text itself says the
new heading DOES point toward the filed destination, which should have
been suppressed by round 8's bow-tolerance fix.

**Traced this to its root rather than assuming a live regression.**
`detect_course_deviation`'s stage-2 confirmation (`detector.py`) already
has the real suppression check: `if track.route: ... if angle_diff_deg(
p_latest.track, bearing_to_dest) <= cfg["corridor_deviation_bow_heading_
deg"]: return None` (60° by default). The event's message text comes from
a SEPARATE helper, `_deviation_context`, called immediately after with the
SAME `p_latest` — but using its OWN hardcoded 30° threshold instead of the
same config value. Since 30° is a strict subset of "<=60°", any event that
actually reaches `_deviation_context` (meaning the real check already
found `angle_diff > 60`) can NEVER also satisfy `< 30` — the "wél richting"
message branch is mathematically unreachable from the CURRENT code. Seeing
it on the live dashboard is therefore solid, independent proof the
deployed VM predates round 8's course_deviation bow-tolerance fix (not a
bug in this repo) — consistent with, and a second confirmation of, round
17/18's `/api/incidents` 404 finding that the VM is running stale code.

**Fix: removed the dead branch**, simplifying `_deviation_context` to
unconditionally describe what's already guaranteed true by the time it
runs (route known implies heading now points away from the destination —
that's the only way the caller's suppression check could have let the
event through). No behavior change (the branch was unreachable either
way) — purely removes misleading dead code and the inconsistent duplicate
threshold, with a docstring explaining why this looked live-reachable on
a first read even though it provably isn't. `python backtest.py`: 9/9 +
all 11 regression-check groups unaffected (no detection-logic change).
Bounded live smoke test (`serve_all.py`, `TELEGRAM_BOT_TOKEN=""`/
`TELEGRAM_CHAT_ID=""`, ~75s): no exceptions; the same ongoing real
GBODB (squawk 7600/NORDO) emergency from round 18's smoke test is still
correctly tracked and not re-notifying.

**course_deviation/holding_pattern samples otherwise too small this pull
for a confident corroboration-rate verdict** (9 and 6 events) — 8/9
course_deviation events had no resolved route at all (the pre-existing,
already-acknowledged adsbdb-coverage-gap pattern from round 8's original
diagnosis, MASTERPLAN.md sectie 1a — not new). `holding_pattern`'s 6
samples all show a hold reported near a DIFFERENT airport than the filed
destination (the stronger, non-gated scoring tier) — plausible this is the
SAME adsbdb-route-mismatch root cause as rounds 16-18 (a plane genuinely
holding at its TRUE destination, misclassified as a non-destination hold
purely because the filed destination is wrong), but 6 samples isn't enough
to act on with the same confidence as round 18's 25-sample wrong_airport
finding. Left as a concrete, well-scoped next-round target: pull a larger
holding_pattern sample, check corroboration rate the same way, and if the
pattern holds, extend round 18's hexdb.io cross-check to holding_pattern's
non-destination branch specifically (the same architecture already exists
in `main.py`'s `enrich_events`, this would be a third, analogous
application of the identical pattern — should be quick once the live
evidence justifies it).

## Round 20 — 2026-08-07 (`/loop` iteration) — holding_pattern's origin blind spot

Followed through on round 19's flagged next step: pulled one more live
snapshot and combined it with all 5 pulls collected so far this session
(deduplicated by event `id`) to get a bigger `holding_pattern` sample —
21 unique events, big enough to act on with the same confidence as round
18's 25-sample `wrong_airport` finding.

**Confirmed the exact same origin blind spot round 17 already fixed for
`signal_lost_near_airport`, never applied to `holding_pattern`.** 5/21
(~24%) of the sampled hits — EJU96EM, LOT2163, EXS94BN, BAW705, SWR9YH —
show `nearest == origin_icao`, e.g. LOT2163 (LOT Polish Airlines) filed
EPKK(Kraków)->LIRF(Rome), reported holding at EPKK itself. `detect_
holding_pattern` only ever excluded the DESTINATION match, never origin —
identical gap, different detector.

**Also spotted, but deliberately NOT fixed this round: a third pattern.**
Several holds land near a DIFFERENT airport in the SAME metro area as the
filed destination/origin — EJU58QD (LIMC->LICC, held at LIML — Milan
Malpensa vs. Linate), TAM4732 (held at SBGR, São Paulo's other airport),
RYR4W (held at EGGW/Luton vs. filed EGSS/Stansted, both London), VTE3692
(held at KDAL/Love Field vs. filed KDFW, both Dallas). Plausibly the same
adsbdb-route-mismatch root cause (rounds 16-18) manifesting as "actually
scheduled to the SISTER airport, not the filed one" rather than a fully
wrong city. Real, but needs a "same metro area" concept this codebase
doesn't have yet (`detect_holding_pattern`'s destination check is exact-
ICAO-match only) — flagged as a distinct, well-scoped follow-up rather
than folded into this round's fix.

**Chose a different fix shape than round 17's origin exclusion,
deliberately.** `signal_lost_near_airport` has no sustained-evidence
mechanism (a single last-known-position snapshot), so unconditional
suppression was the safe choice there. `holding_pattern` already tracks a
streak and reserves a long, deliberately patient gate
(`holding_pattern_destination_min_streak`, 20 samples) for the
DESTINATION case specifically so a genuine early precursor (AI850) isn't
lost to a blanket exclusion — the same reasoning applies to origin: an
unconditional suppression would risk hiding a genuine early-return-then-
hold (technical issue shortly after departure, circling near home before
a landing decision), which is exactly the kind of precursor pattern this
detector exists to catch. Fix (`detector.py`): reuse the SAME long-streak
gate for the origin case as already exists for destination — fires only
once sustained far longer than any routine explanation (regional/multi-
leg callsign reuse, stale route data) would predict, rather than
immediately. Kept `at_destination=False` (the stronger scoring tier) for
a hold that survives the gate at origin — unlike an arrival-sequencing
hold at destination, there's no ordinary reason to sustain a tight hold at
one's own departure airport that long, so a validated origin-hold is still
more informative than a validated destination-hold, not less.

New `check_holding_pattern_origin_gating()` in `backtest.py`: circling
near the aircraft's own origin does NOT fire on the first qualifying
window (gated), but DOES fire once sustained past the long streak
(stronger tier); circling near an unrelated third airport is unaffected,
still fires immediately — same pattern as round 17's suppression-check
style. Verified AI850's real case (which specifically exercises the
DESTINATION-hold streak gate) is unaffected — its hold is at VIDP, the
filed destination, never near origin (VAPO), so this change never
executes for that case at all. `python backtest.py`: 9/9 cases (all
unchanged) + all 12 regression-check groups (11 prior + this new one)
green. Bounded live smoke test (`serve_all.py`, `TELEGRAM_BOT_TOKEN=""`/
`TELEGRAM_CHAT_ID=""`, ~80s): no exceptions; a different real, live
emergency this run (N4620D, squawk 7700) correctly detected, opened
straight at BEVESTIGD, notified exactly once across several repeated
tier0 hits.

## Round 21 — 2026-08-07 (`/loop` iteration) — same-metro-area heuristic, one real fix + one important reverted attempt

Mid-round, the user sent a live message: stop scheduling 25-minute wakeup
gaps and continue immediately, and launch a second, parallel background
agent to do useful work too. Complied: dropped the wakeup delay for the
rest of this round, and launched a general-purpose agent scoped to
self-review `notifier.py`/`server.py`/`providers.py`/the dashboard
frontend — files this session hadn't scrutinized as closely as
`detector.py`/`incidents.py`/`airspace.py`/`classify.py`, and deliberately
NOT the files this round was actively editing, to avoid git conflicts.
That agent found and fixed a stale docstring on `server.py`'s
`handle_api_incidents` (claimed the dashboard "didn't consume this yet",
written before round 11 actually wired up the incident panel) — still
running/reporting further findings as this round's own work continued in
parallel.

**Quantified round 20's flagged same-metro-area pattern properly before
building anything**, per the queued instruction. Pulled 3 more live
snapshots and combined ALL 8 pulls collected across this session (1139
unique events, deduplicated by event id) — enough to compute a real
distance distribution instead of eyeballing a handful of examples. For
each `wrong_airport`/`holding_pattern`/`signal_lost_near_airport` event
whose reported "nearest" airport was neither the exact filed origin nor
destination, computed the haversine distance from that airport to
whichever of origin/destination was closer. Genuine sister-airport pairs
(JFK-LGA 9nm, Milan Malpensa-Linate 26nm, Oakland-San Jose 26nm, London
Stansted-Luton 22nm, LAX-area 31nm) clustered tightly under ~31nm, with a
clear gap to the next-closest non-metro case at 46nm — 35nm sits with
margin in that gap, calibrated the same way as `ROUTE_PLAUSIBLE_PROGRESS_
MULTIPLIER` (round 7). Rates at that threshold: `holding_pattern` 7/21
(33%), `signal_lost_near_airport` 18/95 (19%), `wrong_airport` 5/104 (5%,
already partly mitigated by round 18's hexdb.io cross-check).

**Built a generic, cheap heuristic instead of a per-city hardcoded
list**, per the instruction's suggestion to consider something reusable
across detectors: `airports.airports_same_metro(lat1, lon1, lat2, lon2)`,
a plain distance check against the new `AIRPORT_SAME_METRO_RADIUS_NM`
(35.0) constant — no new data source, no maintenance burden as new
multi-airport-city pairs come up worldwide, reuses the already-everywhere
`haversine_nm`.

**Applied it to `holding_pattern`'s destination/origin exclusions —
verified safe and kept.** Extended both `near_dest`/`near_origin` checks
(the origin one from round 20) to also match a same-metro airport, not
just an exact ICAO match. Safe specifically because holding_pattern
already gates BOTH cases behind the long `holding_pattern_destination_
min_streak` — a same-metro match doesn't skip that patience requirement,
it only widens which positions get treated as "the routine, gated case"
instead of "immediate, ungated case." A genuine emergency hold near a
metro-sister airport is delayed by at most the same streak length already
accepted for exact-destination holds (and AI850's real case already
proves that's an acceptable trade — it fires 1h26m before the real
decision even with that gate active), never silently lost. New `check_
holding_pattern_same_metro()` verifies this using REAL, distance-verified
airport coordinates (KDFW/KDAL, confirmed 10.09nm apart via a direct
computation before writing the test, not guessed).

**Attempted the identical extension for `signal_lost_near_airport` —
found it breaks a real case, reverted before committing.** First pass
extended that detector's destination/origin exclusions the same way. `python
backtest.py` immediately caught the damage: the UA2078 signal-lost variant
went from passing to MISSED. Root cause: UA2078's real diversion target,
Luke AFB, is only ~15nm from the filed KPHX — well inside the 35nm
radius — and "divert to the nearest suitable alternate" is exactly the
realistic pattern this detector exists to catch, not noise. Unlike
`holding_pattern`, `signal_lost_near_airport` is a single last-known-
position snapshot with no streak/patience mechanism at all — suppressing
"nearby but not exact" here doesn't delay detection, it silently and
permanently loses it. Reverted the change for this detector specifically,
with a comment explaining why, referencing the exact case that caught it.
This is worth recording as a genuine finding in its own right, same as
round 17's "reviewed, found clean" pattern: the same fix shape isn't
automatically safe just because it worked for a similar-looking detector —
the difference between "has a sustained-evidence mechanism" (holding_
pattern, safe) and "single-shot" (signal_lost_near_airport, unsafe) is
what actually determines whether a same-metro exclusion delays or
destroys detection.

**Deliberately not extended to `wrong_airport`** — its rate (5%) was
already the smallest of the three, and it already has round 18's
independent hexdb.io cross-check covering a meaningfully overlapping
failure mode (a same-metro landing that's ALSO a schedule mismatch would
likely already get downgraded there); adding a second, different
mechanism on top wasn't judged worth the extra surface area this round
given the small remaining signal.

`python backtest.py`: 9/9 cases (all unchanged, including the now-restored
UA2078 signal-lost variant) + all 13 regression-check groups (12 prior +
the new `check_holding_pattern_same_metro`) green. Bounded live smoke test
(`serve_all.py`, `TELEGRAM_BOT_TOKEN=""`/`TELEGRAM_CHAT_ID=""`, ~80s, 2
tier1 cycles, ~12300-12400 aircraft tracked, 2 open incidents correctly
reloaded from the prior session's local db): no exceptions.

## Round 22 — 2026-08-07 (parallel agent, same session as ronde 21) — precisionRate bug, dashboard stat undercounting

Round 21's parallel background agent (launched mid-round, scoped to
`notifier.py`/`server.py`/`providers.py`/the dashboard frontend —
deliberately NOT the files ronde 21 was actively editing) reported back
with two real bugs fixed and one confirmed-clean result. Reviewed its
diffs directly before committing (same discipline as reviewing any other
change this session) — both held up.

**Bug 1: `precisionRate` was silently always `None`, regardless of real
resolution history.** `db.py`'s `count_incidents_by_resolution` grouped
resolved incidents by `resolution_reason` — a free-form human-readable
description (`incidents.py`'s `_resolve(..., description=...)`, e.g.
"geland op EDDM, niet de verwachte bestemming — bevestigde diversie") —
but `server.py`'s `handle_api_incidents` looks up counts by the *state*
constants (`incidents.CLOSED_LANDED`/`CLOSED_FALSE_ALARM`, i.e.
`"GESLOTEN_GELAND"`/`"GESLOTEN_VALS_ALARM"`), which never equal a
resolution_reason string. The lookup could never match anything, so
`resolvedConfirmedDiversion24h`/`resolvedFalseAlarm24h`/`precisionRate`
were always 0/0/`None` no matter how many incidents had actually
resolved — MASTERPLAN.md sectie 10's own "self-observing metriek die laat
zien of het systeem beter wordt" was silently inert since the day the
incident engine shipped (round 10), never caught because `backtest.py`
has no coverage of this function or the API's stats block. Fixed by
grouping by `state` instead. Verified with a standalone in-memory-sqlite
repro (4 synthetic resolved incidents, mixed states): old logic returned
`0/0/None`, new logic correctly returns `confirmed=1, false_alarms=2,
precisionRate=0.333`.

**Bug 2: the dashboard's header "Confirmed active"/"Events · 24h" tiles
re-introduced a bug already fixed once on the backend.** Round 6 fixed
`server.py`'s own stats (deriving them from a real `SELECT COUNT(*)`,
`data.stats.confirmed`/`data.stats.events24`, instead of the 200-row-
capped `events` array) specifically because undercounting on a busy day
is silent and hits exactly when accurate stats matter most. But the
dashboard's `renderVals()` (`Flight Diversions Dashboard.dc.html`) never
actually consumed that fix — it fetched `data.stats` into `serverStats`
but only ever read `.tracked`/`.lastSweepSecondsAgo` from it, computing
the confirmed/24h tiles from the local, capped `events` array the whole
time. Round 16 observed live `events24=14714`/day — the tile would show
≤200 instead. Fixed by sourcing `confirmed`/`events24` from `serverStats`;
left the separate `confCount` object untouched since it's legitimately
still used for the filter-chip counts (a different, correct "count within
the currently loaded feed" meaning there).

**Confirmed clean:** `providers.py` (no dead code — every exported
function has a live call site; a suspicious-looking 5min hexdb.io
route-cache TTL for successful lookups turned out harmless once traced —
`main.py`'s `route_checked` gating means a resolved route is never
re-looked-up repeatedly anyway) and `notifier.py` (already clean since
round 11's rewrite). Also checked the dashboard incident-panel JS for the
specific edge cases this round's task called out (zero-evidence
incidents, an unrecognized state string) — both handled safely, though
the state case can't actually occur since the server only ever sends
`VISIBLE_STATES`.

Also fixed one stale docstring (`server.py`'s `handle_api_incidents`,
which still claimed the dashboard "didn't consume this yet" — the
incident panel actually landed in round 11).

`python -m py_compile db.py server.py`, then `python backtest.py`: 9/9
cases + all 13 regression-check groups green (expected — none of these
changes touch detection logic). Bounded live smoke test (`serve_all.py`,
`TELEGRAM_BOT_TOKEN=""`/`TELEGRAM_CHAT_ID=""`, ~15s, 4 open incidents
correctly reloaded from the local db): no exceptions. The agent's own
report additionally verified `/api/events`/`/api/incidents` directly via
curl during its own smoke test window (`stats.confirmed=44`,
`events24=46`, `precisionRate` correctly `None` since the only
recently-resolved incident in that window closed `GESLOTEN_TIMEOUT`,
intentionally excluded from the ratio).

## Round 23 — 2026-08-07 (`/loop` iteration, continued) — new real case surfaces a significant route_plausible/corridor_deviation interaction gap

Went looking for a new real, sourced backtest case per the queued
instruction, specifically one that exercises the incident engine's
NON-escalation/recovery path with real data — every prior incident-engine
real-case test (AI850, round 16) validates that a genuine precursor
correctly escalates; none yet validated the opposite, equally important
property with sourced data instead of synthetic scenarios.

**Found a strong real candidate via WebSearch**: Delta Air Lines Flight
2778, Memphis(MEM)->Atlanta(ATL), 15 Mar 2025 — a Boeing 717 took a large
detour south over Louisiana to avoid a severe weekend storm system (which
killed at least 40 people across the South/Midwest), nearly doubling flight
time (1h39m vs. the normal ~45-60min), before landing normally at the
originally filed destination. Sourced via Flightradar24-cited coverage
(Yahoo/AOL, both republishing the same original reporting). Reconstructed
the track using Monroe, LA (KMLU) as a plausible bow point — verified
(not guessed) before committing to it: MEM->KMLU->ATL totals a 1.99x
path-length ratio vs. the direct route, matching the source's "nearly
doubling" detail almost exactly, and produces a 173nm cross-track
deviation, more than double `corridor_deviation`'s own 80nm threshold for
this route length.

**Expected `corridor_deviation` to fire — it didn't, and the reason why is
this round's real finding.** Direct computation confirmed the raw geometry
clears the threshold comfortably by ~t=15min. But `python backtest.py`
showed only `course_deviation` firing (at t=34min, the turn back toward
Atlanta) — zero `corridor_deviation` hits anywhere. Traced it precisely:
main.py's/backtest.py's ONGOING `route_plausible` recheck is still in its
STRICT `check_progress=True` window (`ROUTE_REVALIDATION_WINDOW_S`, 20min
after resolution) at t=8min into this flight, and the growing detour
already fails that strict sum-of-distances check by then — `track.route`
gets nulled at t=8min, six to nine minutes before `corridor_deviation`
would ever have gotten a chance to see a valid route and apply its OWN,
more nuanced, heading-aware bow-tolerance judgment (round 8). Once nulled,
`corridor_deviation`'s `if not track.route: return None` blocks it for the
rest of the flight — `course_deviation` is unaffected only because it's
deliberately route-independent by design.

**Checked whether the existing bow-tolerance heading check could bypass
this — it can't, for a structural reason.** The window where
`route_plausible` fails (t=8-19min) is exactly the OUTBOUND leg of the
detour, where the aircraft's heading genuinely points AWAY from Atlanta
(that's the nature of a bow: fly away, then back) — a "still heading
toward destination" bypass, the same logic corridor_deviation itself
already uses, would need to trigger during the leg where heading points
away, which is the opposite of what that check is designed to recognize
as legitimate. This isn't a case of forgetting to reuse an existing
mechanism; the two checks are answering genuinely different questions
that happen to collide during exactly the outbound half of any real bow.

**Checked whether the 1.2x progress-ratio threshold itself could simply be
loosened — it can't, with real numbers proving it, not just
re-asserting round 5's "no single number" conclusion in the abstract.**
Computed DAL2778's own sum-of-distances ratio at the point `route_plausible`
rejects it: ~1.58x. AAL974's confirmed-BAD route-data ratio (round 7,
live-observed) is 1.38x — LOWER than DAL2778's ratio for a completely
legitimate flight. Any threshold loose enough to admit DAL2778 would also
re-admit AAL974/SWA2820's bad data, undoing round 7's own fix. This is the
same "no single number can do both jobs" lesson round 5 already learned
for the DIFFERENT (overflight-past-destination) failure mode, now shown to
independently apply to this (early-lateral-bow) failure mode too, with its
own fresh evidence.

**Deliberately did not rush a fix.** This is a real, structural tension
between two systems (`route_plausible`'s blunt distance-sum strictness,
and `corridor_deviation`'s smarter heading-aware bow-tolerance judgment)
that collide specifically during a route's first 20 minutes — fixing it
properly likely needs a genuinely different signal than either system
currently has (the case's own docstring speculates about trajectory
smoothness/coherence as a possible future direction — a truly mismatched
route, per the UAL840/AAL974/SWA2820 lineage, would look geometrically
INCOHERENT relative to the filed route over time, not just displaced,
the way a real bow does). `route_plausible` backs enough of this system
(the strict check alone protects 3 of the 9 real cases' route resolution
from bad adsbdb data) that a same-session, insufficiently-validated change
here is a worse outcome than documenting the gap precisely and leaving it
for a dedicated future round — matching this session's own established
discipline (round 17's `signal_lost` origin case chose unconditional
suppression specifically BECAUSE a nuanced fix wasn't safely verifiable;
round 21 tried and reverted an unsafe extension of the same shape for
`signal_lost_near_airport` rather than force it through).

**Kept the case in the suite anyway, as a durable regression marker for
this exact gap** — `expected_type="course_deviation"` (what actually,
correctly fires) rather than `corridor_deviation` (what SHOULD also fire
once this is eventually fixed), with the full finding documented in the
case's own docstring so a future session doesn't have to rediscover it:
if a later fix ever properly resolves this tension, this case's report
should start showing `corridor_deviation` in its "also fired" line, a
concrete, checkable signal that the fix landed.

**Found and regression-tested a real, minor, cascading side effect of the
same root cause.** New `check_incident_engine_real_case_recovery()` runs
DAL2778 through the full incident engine (mirroring round 16's AI850
escalation check, but validating the opposite property): the incident
correctly opens on the single `course_deviation` hit, correctly never
escalates past MOGELIJK (peak_score 30, nowhere near the LIKELY threshold
55 — exactly the intended behavior for a single, uncorroborated
geometric hit), and correctly closes rather than lingering open. But
because `route_plausible` nulled the route BEFORE `course_deviation` ever
fired, the resulting incident never learns a `dest_icao` — so when the
aircraft later lands normally at ATL, `_check_landed`'s destination-match
can't recognize it as "the expected destination," and the incident closes
via idle decay (`GESLOTEN_VALS_ALARM`) instead of the more accurate
`GESLOTEN_NORMAAL`. Still functionally correct (never a false escalation,
never left open) — just a labeling imprecision, asserted explicitly in
the new check so it stays a documented, expected property rather than a
silent gap, and so a future fix to the root cause has a test that will
correctly start expecting `GESLOTEN_NORMAAL` instead.

`python backtest.py`: **10/10 cases** (9 prior + DAL2778) detect their
expected type + all 15 regression-check groups (13 prior + the two new
ones: `check_incident_engine_real_case_recovery`) green. No live smoke
test this round — only `backtest.py`/`backtest_cases.py` changed, neither
imported by `main.py`/`serve_all.py`'s live loops.

Sources: https://www.yahoo.com/news/map-shows-journey-doubling-detours-121711497.html,
https://www.aol.com/map-shows-journey-doubling-detours-121711740.html

## Round 24 — 2026-08-07 (`/loop` iteration, continued) — falsy-timestamp bug in `TrackStore.update`

Self-review pass over `state.py` (not yet scrutinized this session — a
second parallel agent was working on `main.py`/`detector.py` at the time,
so this deliberately covered different files).

**Found a real, if subtle, bug: `ts = ts or time.time()`** in `TrackStore.
update` (and the identical pattern in `TrackStore.prune`, and in `db.py`'s
`save_event`/`record_route_observation`) treats an explicitly-passed
`ts=0.0` the same as "not provided" — `0.0` is falsy in Python, so `or`
silently substitutes the real wall-clock time instead of the caller's
actual, intentional `0.0`. Confirmed with a one-line repro: `0.0 or
time.time()` returns the current epoch timestamp, not `0.0`.

Harmless in live production — `main.py` always passes a real epoch `now`
value, which is never literally `0.0`. But real for `backtest.py`'s `Case`
harness: every synthetic track in `backtest_cases.py` starts at `t0 = 0.0`,
and `run_case` calls `store.update(ac, s.t)` with that exact value for the
first sample of most cases (confirmed for AI850 specifically) — the
opening `TrackPoint` of these tracks silently got today's real wall-clock
time instead of the intended `0.0`.

**No observable effect on any of the 10 current cases** (re-ran `python
backtest.py` after the fix: all 10 cases' lead times are byte-for-byte
identical to before) — `HISTORY_MAXLEN=20` means the corrupted first point
gets evicted from the rolling window well before any of today's detectors
read a `.ts` difference that spans back to it. Fixed anyway rather than
left as "harmless today": a future case with fewer samples, or a detector
that reads an early timestamp difference, would have silently gotten wrong
results with no error or warning — exactly the kind of latent bug this
session's own "grep for dangling references after every change" discipline
exists to catch before it bites, not after.

Fixed all 4 occurrences of the identical `x or default` pattern found via
grep (`state.py`'s `TrackStore.update`/`prune`, `db.py`'s `save_event`/
`record_route_observation`) to `x if x is not None else default` — the
other 3 are currently latent (never actually called with an explicit,
possibly-falsy value anywhere in this codebase today) rather than
triggered, fixed anyway to close the whole bug class rather than just the
one call site that happened to trigger it, matching this session's
established practice (round 21's generic `airports_same_metro` helper,
round 17/20's paired origin-exclusion fixes).

**Also fixed a stale docstring found in the same file while at it**:
`db.py`'s `save_event` still described the pre-round-10 "called at the
same point the Telegram alert is sent, only once per cooldown window"
dispatch model — the exact same class of staleness round 16 found and
fixed in `server.py`'s `FEED_WINDOW_SECONDS` comment, just never applied
to this second, nearby copy of the same outdated description.

`python -m py_compile state.py db.py`, then `python backtest.py`: 10/10
cases (all unchanged) + all 15 regression-check groups green. Bounded live
smoke test (`serve_all.py`, `TELEGRAM_BOT_TOKEN=""`/`TELEGRAM_CHAT_ID=""`,
~75s, port 8798 to avoid colliding with the parallel agent's own smoke
test): no exceptions; a different real, live emergency this run (N81051,
squawk 7600/NORDO) correctly detected, opened straight at BEVESTIGD,
notified exactly once across several repeated tier0 hits.
