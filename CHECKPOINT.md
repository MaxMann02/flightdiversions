# CHECKPOINT — fundamentele review van de zekerheidslogica

Sessie gestart 2026-08-08. Doel: één keer vanaf de grond kritisch nadenken over
HOE dit systeem tot een zekerheidsoordeel komt, in plaats van opnieuw losse
symptomen te repareren. Zie MASTERPLAN.md / BACKTEST_LOG.md voor de
voorgeschiedenis.

**Werkwijze:** bevinding voor bevinding. Elke bevinding wordt hier volledig
uitgeschreven (probleem + redenering + exacte fix + validatie) vóórdat er iets
geïmplementeerd wordt, zodat een onderbreking nooit redenering kost.

**Statuslegenda:** `open` → nog niet geïmplementeerd · `geïmplementeerd` → code
aangepast · `gevalideerd` → `python backtest.py` bevestigt het verwachte gedrag.

---

## 0. De rode draad achter alle bevindingen

Het huidige model is: elk detector-hit levert punten op, punten worden bij
elkaar opgeteld, en drie drempels (25/55/85) vertalen de som naar
MOGELIJK/WAARSCHIJNLIJK/BEVESTIGD. `_confirmed_bar_met` is de enige plek waar
iets anders dan de som meetelt, en die kijkt alleen of *alle* bewijsbronnen
toevallig `course_deviation`/`corridor_deviation` zijn.

Dat model doet drie stilzwijgende aannames die geen van drieën houdbaar zijn:

1. **Additiviteit ⇒ onafhankelijkheid.** Punten van verschillende detectoren
   worden opgeteld alsof het onafhankelijke waarnemingen zijn. Dat is niet zo:
   *elke* gedragsdetector behalve `detect_emergency` rekent tegen
   `track.route["destination_*"]`. Eén foute route laat ze allemáál tegelijk
   afgaan. Het systeem leest dat als "vijf detectoren zijn het eens" terwijl
   het één fout is, vijf keer waargenomen. (→ bevinding 4)

2. **Herhaling ⇒ zekerheid.** Een aanhoudend signaal blijft elke cyclus punten
   opleveren; de "repeat-demping" is een vaste breuk (~1/3), dus de reeks
   divergeert. De enige rem is `incident_score_max=150`, ruim bóven de
   BEVESTIGD-drempel. Herhaling van hetzelfde signaal sluit alleen *ruis* uit,
   niet de *systematische* onschuldige verklaringen (verouderde routedata,
   weer, ATC-vertraging) — die overleven elke hoeveelheid herhaling. (→
   bevindingen 3 en 8)

3. **Zwijgen ⇒ instemming.** Tweede bronnen kunnen alleen naar beneden
   corrigeren bij actieve tegenspraak. Geen data van hexdb.io telt precies zo
   zwaar als bevestiging door hexdb.io. Voor het hoogste zekerheidsniveau is
   dat omgekeerd: "niet gecontroleerd" hoort géén BEVESTIGD op te leveren. (→
   bevindingen 1 en 2)

De fix is daarom niet "andere getallen", maar: **de score bepaalt hooguit het
plafond; de *structuur* van het bewijs bepaalt of dat plafond bereikbaar is.**
Concreet ingevoerd in bevindingen 2–4: bewijs-*dimensies* (twee hits in
dezelfde dimensie zijn dezelfde waarneming, geen corroboratie), per-bron
verzadigingsplafonds, en een expliciete route-vertrouwensstatus.

Belangrijk randfeit dat de kosten van strenger-zijn laag maakt: **notificatie
gebeurt al bij WAARSCHIJNLIJK** (`_maybe_notify`: `new_state in (LIKELY,
CONFIRMED)`). BEVESTIGD strenger maken kost dus vrijwel geen meldingen — het
maakt alleen het hoogste label weer betekenisvol.

---

## Bevinding 1 — De confidence-degradatie van `wrong_airport` is volledig inert

**Status:** gevalideerd (2026-08-08) — `check_wrong_airport_evidence_weight` in
`backtest.py`, alle 6 assertions OK, rest van de suite ongewijzigd groen.
Geïmplementeerd in `incidents.py` (`score_for_event`, `_WRONG_AIRPORT_SOURCES`,
`_check_landed`), `main.py` (`enrich_events`) en `detector.py` (`Event`-doc).

### 1. Wat er mis is
`incidents.py:143-144` (`score_for_event`):
```python
if et == "wrong_airport":
    return 90.0, "wrong_airport", "geland op onverwachte luchthaven"
```
`score_for_event` leest `ev.confidence` **alleen** in de `emergency`-tak.
Ondertussen doet `main.py:46-88` (`enrich_events`) twee vetos die uitsluitend
`ev.confidence` op `"WAARSCHIJNLIJK"` zetten en tekst aan `ev.message` plakken:

- `main.py:46-51` — cross-provider-oneens over de *grondstatus*
  (`providers.cross_provider_agrees`);
- `main.py:79-88` — hexdb.io noemt een *andere bestemming* dan adsbdb.

Beide vetos hebben nul effect op de incident-state: het incident krijgt nog
steeds 90 punten, 90 ≥ `incident_score_confirmed_threshold` (85), dus BEVESTIGD
en een 🚨-Telegram, precies alsof er geen tegenspraak was.

### 2. Waarom dit een probleem is
Dit is dezelfde fout die ronde 24 wél opmerkte voor `premature_descent`/
`signal_lost_near_airport` — de comment in `main.py:119-131` schrijft letterlijk
op dat `score_for_event` `ev.confidence` niet raadpleegt "for these two event
types (confirmed by reading it — only `emergency` does)". Uit precies die
observatie volgt dat `wrong_airport`'s eigen, al bestaande degradatie óók
cosmetisch is — die conclusie is toen niet getrokken, want de aandacht ging naar
de twee detectoren die op dat moment in de live-data opvielen.

**Scenario:** DLH8NK. adsbdb: EDDF→LEMG. hexdb.io: EDDF→EDDM. Het toestel landt
op EDDM.
- `detect_landed_wrong_airport` vuurt, confidence BEVESTIGD.
- `enrich_events` ziet de hexdb-tegenspraak, logt een warning, zet
  `ev.confidence = "WAARSCHIJNLIJK"`, plakt de uitleg aan het bericht.
- `score_for_event` → 90.0 → state BEVESTIGD → Telegram "🚨 BEVESTIGD".

Het systeem heeft dus zelf vastgesteld dat de referentiedata betwist wordt, dat
opgeschreven in het bericht, en vervolgens exact hetzelfde gedaan als zonder die
vaststelling.

### 3. De exacte fix
**a. `detector.py`** — de `route_source_disputed`-vlag bestaat al op `Event` en
hoeft niet aangepast te worden; alleen de docstring uitbreiden zodat
`wrong_airport` erin genoemd wordt (nu staat er dat alleen
premature_descent/signal_lost hem zetten).

**b. `main.py`, `enrich_events`,** in het bestaande `wrong_airport`-hexdb-blok
(regel 79-88): naast de bestaande `ev.confidence`/`ev.message`-wijziging ook
`ev.route_source_disputed = True` zetten.

**c. `incidents.py`, `score_for_event`,** de `wrong_airport`-tak vervangen door:
```python
    if et == "wrong_airport":
        if ev.route_source_disputed:
            # Tweede routebron (hexdb.io) noemt een ANDERE bestemming dan de
            # gefilede — dan is niet de landing verdacht maar de referentie
            # waartegen we hem afmeten. ~1/3 van het normale gewicht, dezelfde
            # dempingsratio als signal_lost_disputed/premature_descent_disputed.
            return 30.0, "wrong_airport_disputed", "geland op onverwachte luchthaven (tweede routebron betwist de bestemming)"
        if ev.confidence != "BEVESTIGD":
            # enrich_events kon de grondstatus niet bevestigen bij de tweede
            # ADS-B-provider: de WAARNEMING zelf (staat dit toestel hier echt
            # aan de grond) is dan betwist, niet de referentiedata. Zwaarder
            # gedempt dan een betwiste route, want zonder betrouwbare
            # grondwaarneming is er helemaal geen "geland"-feit meer.
            return 25.0, "wrong_airport_unconfirmed", "geland op onverwachte luchthaven (grondstatus niet bevestigd door tweede bron)"
        return 90.0, "wrong_airport", "geland op onverwachte luchthaven"
```

**d.** `_check_landed` (`incidents.py:376`) test op `"wrong_airport" in
self._evidence_sources_seen(...)` om als GESLOTEN_GELAND (bevestigde diversie)
af te sluiten. Dat moet de nieuwe bronnamen meenemen, anders sluit een betwiste
wrong_airport-landing niet meer netjes af:
```python
        if _WRONG_AIRPORT_SOURCES & self._evidence_sources_seen(inc["id"]):
```
met bovenin het bestand:
```python
# Alle bronnamen die score_for_event voor een wrong_airport-Event kan
# produceren (vol vertrouwen / betwiste routebron / onbevestigde grondstatus).
_WRONG_AIRPORT_SOURCES = {"wrong_airport", "wrong_airport_disputed", "wrong_airport_unconfirmed"}
```

### 4. Validatie
Nieuwe case in `backtest.py`: `check_wrong_airport_evidence_weight()`.
- Bouw drie `detector.Event`-objecten met `event_type="wrong_airport"`:
  (i) confidence BEVESTIGD, `route_source_disputed=False`;
  (ii) confidence BEVESTIGD, `route_source_disputed=True`;
  (iii) confidence WAARSCHIJNLIJK, `route_source_disputed=False`.
- **Vóór de fix:** `score_for_event(ev, False)[0] == 90.0` voor alle drie.
- **Na de fix:** respectievelijk `90.0` / `30.0` / `25.0`, met bronnen
  `wrong_airport` / `wrong_airport_disputed` / `wrong_airport_unconfirmed`.
- Aanvullend, via `IncidentManager`: een incident met alléén (ii) of (iii) komt
  onder `incident_score_possible_threshold` (25) resp. precies erop, en bereikt
  dus niet BEVESTIGD. Vóór de fix: BEVESTIGD in beide gevallen.
- Bestaande `check_wrong_airport_route_crosscheck()` moet groen blijven (die
  test `ev.confidence`, niet de score).

---

## Bevinding 2 — "Geen tweede mening beschikbaar" wordt behandeld als "tweede mening bevestigt"

**Status:** gevalideerd (2026-08-08) — `check_route_corroboration` in `backtest.py`,
6/6 assertions OK. Geïmplementeerd in `airports.py`
(`route_corroborated_by_progress`), `state.py`, `detector.py`, `main.py`
(tier1_loop + enrich_events) en `incidents.py`. De EK225-geometrie corroboreert
echt (4808nm resterend van 7030nm route), de DLH8NK-vorm niet (afstand tot de
gefilede bestemming daalt daar helemaal niet: 1002nm > 981nm routelengte).

### 1. Wat er mis is
`main.py:79-88` en `main.py:149-199` degraderen alleen bij *actieve*
tegenspraak. De comment op regel 76-78 verwoordt het als bewuste keuze: "Only
downgrades on an active DISAGREEMENT... no hexdb.io data at all leaves
confidence unchanged (unconfirmed, not penalized)."

Gevolg: een `wrong_airport`-Event waarvoor hexdb.io niets heeft, krijgt de
volle 90 punten en dus meteen BEVESTIGD (`incidents.py:143`,
`_state_for_score`). Er bestaat nergens een representatie van "de route is
nooit geverifieerd".

### 2. Waarom dit een probleem is
Voor de *lagere* niveaus is "zwijgen straft niet" precies goed — je wilt geen
echte diversie missen omdat een gratis bron toevallig geen data heeft. Voor het
*hoogste* niveau is het precies verkeerd: BEVESTIGD hoort te betekenen dat er
geen redelijke alternatieve verklaring meer over is, en "de gefilede bestemming
klopt niet" is bij dit systeem niet een theoretische maar de **gemeten
hoofdverklaring**: BACKTEST_LOG ronde 18 vond dat 24 van 25 bemonsterde live
`wrong_airport`/BEVESTIGD-hits nul corroborerend bewijs hadden en het patroon
van verouderde scheduledata vertoonden (DLH8NK EDDF→LEMG vs. geland EDDM), en
ronde 24 vond dat 6 van 8 bemonsterde hexdb-lookups een ándere bestemming
noemden — in alle zes precies de luchthaven waar het event op afging.

Met andere woorden: bij dit systeem is de a-priori kans dat een onbevestigde
gefilede bestemming fout is, in de orde van tientallen procenten. Dan is
"onbevestigd" gelijkstellen aan "bevestigd" niet conservatief maar de dominante
foutbron.

**Scenario:** RYR49MG. adsbdb: LROP→EGGD. hexdb.io heeft dit keer géén data
(HEXDB_RETRY_COOLDOWN_S actief na een time-out). Het toestel landt op LIRA.
`wrong_airport` → 90 → BEVESTIGD → 🚨-Telegram "bevestigde diversie", terwijl
de werkelijke route (blijkens ronde 24's steekproef) EYVI→LIRA was: een
volstrekt normale landing op de eigen bestemming.

### 3. De exacte fix
Voer een expliciete **route-corroboratie**-status in, met drie in plaats van
twee waarden: bevestigd / betwist / ongeverifieerd — waarbij *ongeverifieerd*
de default is en BEVESTIGD blokkeert.

**a. `airports.py`**, onderaan toevoegen:
```python
# Hoeveel van de gefilede route we het toestel zelf hebben zien afleggen
# voordat we de route als door EIGEN WAARNEMING gecorroboreerd beschouwen.
# 0.30 = het toestel is minstens 30% dichter bij de gefilede bestemming
# gekomen dan het vertrekpunt lag, zonder dat de afgelegde weg meer dan
# ROUTE_PLAUSIBLE_PROGRESS_MULTIPLIER x de directe afstand werd. Een verkeerd
# gematchte schedule (de gemeten hoofdfoutbron, zie CHECKPOINT bevinding 2)
# haalt dat vrijwel nooit: die wijst naar een bestemming waar het toestel
# juist niet naartoe vliegt, dus de resterende afstand daalt niet.
ROUTE_CORROBORATION_MIN_PROGRESS = 0.30


def route_corroborated_by_progress(origin_lat, origin_lon, dest_lat, dest_lon,
                                   cur_lat, cur_lon) -> bool:
    """True als we dit toestel de gefilede route daadwerkelijk een substantieel
    stuk hebben zien afleggen — eigen waarneming als tweede mening over de
    (enkele, crowdsourced) schedulebron waar de route vandaan komt. Anders dan
    route_plausible (dat alleen ONmogelijke routes wegfiltert) is dit een
    POSITIEF corroboratiesignaal: route_plausible zegt "niet aantoonbaar fout",
    dit zegt "aantoonbaar gevlogen"."""
    route_len = haversine_nm(origin_lat, origin_lon, dest_lat, dest_lon)
    if route_len < 1:
        return False
    if not route_plausible(origin_lat, origin_lon, dest_lat, dest_lon,
                           cur_lat, cur_lon, check_progress=True):
        return False
    dist_to_dest = haversine_nm(cur_lat, cur_lon, dest_lat, dest_lon)
    return dist_to_dest <= route_len * (1.0 - ROUTE_CORROBORATION_MIN_PROGRESS)
```

**b. `state.py`**, veld op `AircraftTrack`:
```python
    # Of de gefilede route van deze track onafhankelijk gecorroboreerd is
    # (eigen waargenomen vertrek vanaf het gefilede vertrekpunt, of zelf
    # waargenomen voortgang langs de gefilede route). Blijft True zolang
    # track.route ongewijzigd is; wordt met de route mee gereset. Zie
    # CHECKPOINT.md bevinding 2 — dit is wat "de route is geverifieerd"
    # onderscheidt van "niemand heeft de route tegengesproken".
    route_corroborated: bool = False
```

**c. `main.py`, `tier1_loop`** — overal waar `track.route` wordt (her)toegewezen
of losgelaten, `track.route_corroborated = False` meezetten:
- regel ~409 (`t.route = route`) → `t.route_corroborated = False`
- regel ~491 (`track.route = None`) → `track.route_corroborated = False`

en direct ná de bestaande per-cyclus `route_plausible`-hercheck (dus alleen
voor tracks die nog een route hebben) toevoegen:
```python
                # Positieve route-corroboratie (CHECKPOINT.md bevinding 2):
                # twee onafhankelijke manieren om de gefilede route zelf te
                # bevestigen in plaats van hem alleen niet te weerleggen.
                if track.route and not track.route_corroborated:
                    if track.pending_origin and (
                        track.pending_origin["icao"] == track.route["origin_icao"]
                        or airports_same_metro(track.pending_origin["lat"], track.pending_origin["lon"],
                                               track.route["origin_lat"], track.route["origin_lon"])
                    ):
                        # We hebben dit toestel zélf zien vertrekken vanaf het
                        # gefilede vertrekpunt — dan is de schedule-match voor
                        # DEZE leg, niet voor een andere leg van dezelfde
                        # callsign (de foutbron uit detect_landed_wrong_airport's
                        # eigen origin-comment).
                        track.route_corroborated = True
                    elif ac.get("lat") is not None and route_corroborated_by_progress(
                        track.route["origin_lat"], track.route["origin_lon"],
                        track.route["destination_lat"], track.route["destination_lon"],
                        ac["lat"], ac["lon"],
                    ):
                        track.route_corroborated = True
```
(import `airports_same_metro` en `route_corroborated_by_progress` uit
`airports`.)

**d. `main.py`, `enrich_events`** — de derde tak toevoegen aan beide
hexdb-blokken: hexdb.io die *dezelfde* bestemming noemt is positieve
corroboratie, niet slechts "geen tegenspraak":
```python
            if hexdb_pair is not None and hexdb_pair[1] == ev.dest_icao:
                ev.route_corroborated = True
```

**e. `detector.py`** — veld op `Event`:
```python
    # Zie CHECKPOINT.md bevinding 2. Gezet door main.py (eigen waargenomen
    # vertrek/voortgang langs de gefilede route) en door enrich_events (tweede
    # routebron noemt DEZELFDE bestemming). incidents.py gebruikt dit als
    # harde voorwaarde voor BEVESTIGD bij alle route-afhankelijke evidence:
    # zonder corroboratie blijft de gefilede bestemming een onbevestigde
    # aanname, en een oordeel dat volledig op die aanname rust kan per
    # definitie niet "geen redelijk alternatief meer" zijn.
    route_corroborated: bool = False
```
en aan het einde van `evaluate()`, vlak vóór `return events`:
```python
    for ev in events:
        if not ev.route_corroborated:
            ev.route_corroborated = bool(track.route_corroborated)
```
plus dezelfde regel voor het los aangeroepen `detect_signal_lost_near_airport`
(zetten in `main.py` op de aanroepplek, regel ~541).

**f. `incidents.py`** — de corroboratie wordt als evidence-rij met delta 0.0
vastgelegd (persistent, zichtbaar in de evidence-tijdlijn, geen
schemawijziging) en meegewogen in `_confirmed_bar_met` (zie bevinding 4 voor de
volledige nieuwe poort).

### 4. Validatie
Nieuwe `check_route_corroboration()` in `backtest.py`:
- `route_corroborated_by_progress` op de EK225-geometrie (DXB→SFO) vlak vóór de
  ommekeer bij Nyagan → verwacht `True` (het toestel heeft echt richting SFO
  gevlogen).
- Dezelfde functie met de DLH8NK-geometrie (EDDF→LEMG gefiled, positie boven
  EDDM) → verwacht `False`.
- Via `IncidentManager`: een `wrong_airport`-Event met
  `route_corroborated=False` → state blijft WAARSCHIJNLIJK (score 90 ≥ 85 maar
  poort dicht); met `route_corroborated=True` → BEVESTIGD.
  **Vóór de fix:** beide BEVESTIGD.

---

## Bevinding 3 — Herhaling van één signaal bereikt BEVESTIGD binnen minuten

**Status:** gevalideerd (2026-08-08) — `check_saturation_caps` in `backtest.py`,
8/8 assertions OK. Gemeten na de fix: 30 cycli onafgebroken
`holding_non_destination` → 70 (WAARSCHIJNLIJK), `premature_descent` → 60,
`emergency_status` → 55, `emergency_status_low_trust` → 24 (onder de
dashboarddrempel). AI850's hold-gedreven incident: peak 65 i.p.v. 150, meldtijd
onveranderd (+2h25m vóór de echte MAYDAY).

### 1. Wat er mis is
`incidents.py:125-175` (`score_for_event`) geeft bij herhaling een vaste
fractie van het eerste gewicht, en `_apply_delta` telt dat onbeperkt op. De
enige rem is `incident_score_max` (150) — ruim bóven
`incident_score_confirmed_threshold` (85).

Uitgerekend, bij `tier1_interval_seconds = 60`:

| bron | 1e hit | herhaling | cycli tot ≥85 | wandkloktijd |
|---|---|---|---|---|
| `holding_non_destination` | 35 | 12 | 6 | **6 min** |
| `holding_destination` | 25 | 8 | 9 (ná de 20-cycli-poort) | 29 min |
| `premature_descent` | 25 | 8 | 9 | 9 min |
| `course_deviation` | 30 | 10 | 7 | (geblokkeerd door `_confirmed_bar_met`) |

`holding_non_destination` heeft bovendien géén streak-eis: `detect_holding_
pattern` vuurt bij de eerste 5-samples-window met ≥270° rotatie binnen 15nm,
waarbij "niet-bestemming" betekent: de dichtstbijzijnde *large* luchthaven
binnen 90 km (≈48nm) is niet de gefilede origin/destination en ligt niet binnen
35nm van een van beide.

### 2. Waarom dit een probleem is
Dit is precies het onderscheid dat het systeem niet maakt: **hetzelfde signaal
dat lang aanhoudt versus iets wezenlijk anders dat erbij komt.** Nu is er geen
verschil — aanhouden alléén haalt de top.

De onderliggende redenering die wél klopt: herhaling elimineert *ruis*-hypothesen
(een enkele foute fix, een decodeglitch, één ATC-vector). Herhaling elimineert
géén *systematische* hypothesen — verouderde routedata, een weersomleiding, een
ATC-vertraging, of gewoon een naderingsprocedure bij een druk veld — want die
voorspellen juist dat het signaal blíjft. Een maat van zekerheid die met
herhaling onbeperkt doorgroeit, groeit dus door in een richting waar het bewijs
niet heen wijst.

De bestaande `REPEATABLE_EVENT_TYPES`-demping (ronde 16) erkende het probleem
maar loste het niet op: een vaste fractie vertraagt een divergerende reeks, ze
convergeert er niet van.

**Scenario:** een toestel EHAM→LEMG dat in werkelijkheid (verouderde schedule)
naar EDDM vliegt en daar in de normale aankomststack 10 minuten wacht. Nearest
large = EDDM ≠ LEMG, niet binnen 35nm van EHAM/LEMG →
`holding_non_destination`. Cyclus 1: 35 (MOGELIJK). Cyclus 3: 59 →
WAARSCHIJNLIJK + Telegram. Cyclus 6: 95 → **BEVESTIGD** + 🚨. Zes minuten
normale aankomstholding, nul corroboratie, hoogste zekerheidsniveau.

### 3. De exacte fix
Per-bron **verzadigingsplafond**: de totale bijdrage van één bewijsbron aan één
incident is begrensd, en elk plafond op een "zachte" bron ligt onder
`incident_score_confirmed_threshold`. De plafonds zijn zo gekozen dat een
volledig volgehouden enkelvoudig signaal precies uitkomt op het niveau dat het
verdient (meestal WAARSCHIJNLIJK: gemeld, maar niet bevestigd).

In `incidents.py`, bovenin:
```python
# Maximale TOTALE bijdrage van één bewijsbron aan één incident.
#
# Herhaling van hetzelfde signaal sluit RUIS uit (een enkele foute fix, een
# decodeglitch, één ATC-vector) maar niet de SYSTEMATISCHE onschuldige
# verklaringen (verouderde routedata, weersomleiding, ATC-vertraging,
# naderingsprocedure) — die voorspellen juist dat het signaal blijft. Een
# score die met herhaling onbeperkt doorgroeit, groeit dus door in een
# richting waar het bewijs niet heen wijst. Daarom convergeert elke bron naar
# een eigen plafond in plaats van te divergeren.
#
# Elk plafond op een provisionele bron ligt ONDER
# incident_score_confirmed_threshold (85): volgehouden enkelvoudig bewijs komt
# uit op WAARSCHIJNLIJK (dat notificeert al — zie _maybe_notify), nooit op
# BEVESTIGD. Alleen emergency_squawk (bewuste 4-cijferige piloothandeling) en
# wrong_airport (fysiek waargenomen landing) liggen erboven; die twee worden
# los begrensd door _confirmed_bar_met.
_SOURCE_SCORE_CAP = {
    "emergency_squawk":            150.0,
    "emergency_status":             55.0,
    "emergency_status_low_trust":   24.0,  # net onder de MOGELIJK-drempel (25)
    "wrong_airport":                90.0,
    "wrong_airport_disputed":       30.0,
    "wrong_airport_unconfirmed":    25.0,
    "signal_lost":                  40.0,
    "signal_lost_disputed":         13.0,
    "premature_descent":            60.0,
    "premature_descent_disputed":   16.0,
    "course_deviation":             55.0,
    "corridor_deviation":           55.0,
    "holding_destination":          65.0,
    "holding_non_destination":      70.0,
}
# Onbekende (toekomstige) bron: conservatief onder de WAARSCHIJNLIJK-drempel.
_DEFAULT_SOURCE_SCORE_CAP = 40.0
```

Het plafond wordt bijgehouden als **meevervallende** bijdrage per bron, zodat
de invariant is: *de score die aan bron S toe te schrijven is, is nooit groter
dan `cap_S`.* Concreet in `IncidentManager`:

- `inc["source_contrib"]: dict[str, float]` in de in-memory incident-dict. Bij
  `__init__`/laden uit de DB: `min(cap_S, som van positieve deltas voor S)`.
- In `_apply_delta`, vóór het optellen:
```python
        contrib = inc.setdefault("source_contrib", {}) if inc else {}
        cap = _SOURCE_SCORE_CAP.get(source, _DEFAULT_SOURCE_SCORE_CAP)
        if delta > 0:
            room = max(0.0, cap - contrib.get(source, 0.0))
            delta = min(delta, room)
            contrib[source] = contrib.get(source, 0.0) + delta
```
  (voor een nieuw incident idem, ná het aanmaken van de dict.)
- In `reassess`, bij het toepassen van `decay` op `inc["score"]`, dezelfde
  factor op elke waarde in `inc["source_contrib"]` toepassen — anders zou een
  bron die zijn plafond raakte en daarna verviel nooit meer kunnen bijdragen,
  terwijl het onderliggende signaal gewoon doorloopt.
- Een delta van 0.0 na afkapping levert nog steeds een evidence-rij op (de
  tijdlijn moet blijven laten zien dát het signaal aanhoudt), maar verandert de
  score niet.

### 4. Validatie
Uitbreiding van `check_incident_engine_regressions()`:
- **`holding_non_destination` 30 cycli achter elkaar**, geen ander bewijs.
  *Vóór de fix:* state BEVESTIGD, score 150. *Na de fix:* score 70, state
  WAARSCHIJNLIJK.
- **`premature_descent` 30 cycli**, geen ander bewijs. *Vóór:* BEVESTIGD/150.
  *Na:* 60 / WAARSCHIJNLIJK.
- **`emergency_status_low_trust` 30 cycli** (conspicuity-squawk-artefact).
  *Vóór:* BEVESTIGD/150. *Na:* 24 / BEWAKING — komt niet eens op het dashboard.
- **`emergency_squawk`** blijft onmiddellijk BEVESTIGD (regressiebewaking dat
  het plafond echte noodsituaties niet raakt).
- `check_incident_engine_real_case_escalation` (AI850): de hold-gedreven
  assertie "escalates all the way to BEVESTIGD (peak_score=150)" wordt
  "escaleert naar WAARSCHIJNLIJK (peak_score=65) en daar stopt het, want een
  urenlange hold bij de eigen bestemming heeft gewone verklaringen
  (weer/congestie) die door langer wachten niet verdwijnen; BEVESTIGD volgt pas
  bij de MAYDAY-squawk". De meldingstiming verandert niet — WAARSCHIJNLIJK
  notificeert al, dus de +2h21m voorsprong op de echte MAYDAY blijft staan.

---

## Bevinding 4 — Detectoren worden als onafhankelijk bewijs opgeteld terwijl ze één gedeelde aanname hebben

**Status:** gevalideerd (2026-08-08) — `check_confirmed_bar_structure` in
`backtest.py`, 5/5 assertions OK. Het letterlijke verouderde-schedule-scenario
(corridor_deviation + premature_descent + holding_non_destination, score 90)
komt nu op WAARSCHIJNLIJK i.p.v. BEVESTIGD; met gecorroboreerde route wél op
BEVESTIGD.

### 1. Wat er mis is
`_apply_delta` telt deltas van verschillende `source`-strings zonder meer bij
elkaar op, en `_confirmed_bar_met` (`incidents.py:212-223`) accepteert elke
verzameling bronnen die niet volledig binnen `{course_deviation,
corridor_deviation}` valt.

Maar op één na rekenen *alle* detectoren tegen dezelfde grootheid:

| detector | gebruikt `track.route`? |
|---|---|
| `detect_emergency` | nee |
| `detect_course_deviation` | ja (bow-tolerantie, `detector.py:272-275`) |
| `detect_route_corridor_deviation` | ja (kern van de meting) |
| `detect_premature_descent` | ja (afstand tot bestemming) |
| `detect_holding_pattern` | ja (harde eis, `detector.py:547-551`) |
| `detect_landed_wrong_airport` | ja (definitie van "verkeerd") |
| `detect_signal_lost_near_airport` | ja (definitie van "niet-bestemming") |

Eén foute bestemming laat ze allemaal tegelijk afgaan, en het scoremodel leest
dat als onafhankelijke corroboratie.

### 2. Waarom dit een probleem is
Optellen van bewijs veronderstelt (log-likelihood-gewijs) conditionele
onafhankelijkheid gegeven de hypothese. Hier is er één alternatieve hypothese —
*"de gefilede route klopt niet"* — die **elk** van die waarnemingen tegelijk
voorspelt. Meer verschillende detectoren maken die hypothese niet
onwaarschijnlijker; ze zijn er juist allemaal een voorspelling van.

Dat is precies waarom de bestaande `_DEVIATION_ONLY_SOURCES`-poort niet werkt
zoals bedoeld: die filtert op de *namen* van de bronnen, terwijl het probleem in
hun *gedeelde input* zit.

**Scenario:** callsign met verouderde schedule EHAM→LEMG, werkelijk EHAM→EDDM.
- `corridor_deviation`: ver van de EHAM→LEMG-lijn, koers wijst niet naar
  LEMG → +30.
- `premature_descent`: daalt naar EDDM terwijl het "nog 1000nm van LEMG" is →
  +25.
- `holding_non_destination`: wacht boven EDDM, nearest large ≠ LEMG → +35.
- Totaal 90 ≥ 85. `_confirmed_bar_met`: drie verschillende bronnen, geen
  deelverzameling van `_DEVIATION_ONLY_SOURCES` → poort open → **BEVESTIGD**.

Drie "onafhankelijke" detectoren, één fout.

### 3. De exacte fix
**a. Bewijs-dimensies.** Twee hits in dezelfde dimensie zijn dezelfde
waarneming, niet corroboratie. In `incidents.py`:
```python
# Bewijs-DIMENSIES. Twee evidence-rijen in dezelfde dimensie zijn dezelfde
# waarneming die zich herhaalt of vanuit een tweede hoek gezien wordt, geen
# onafhankelijke corroboratie (course_deviation en corridor_deviation zijn
# letterlijk twee metingen van dezelfde laterale afwijking).
DIM_DECLARED = "declared"          # bewuste piloot-/ATC-handeling (squawk/status)
DIM_GROUND_TRUTH = "ground_truth"  # fysiek waargenomen aan de grond elders
DIM_VANISHED = "vanished"          # afgeleide landing uit signaalverlies
DIM_VERTICAL = "vertical"          # hoogteprofiel klopt niet met de bestemming
DIM_LATERAL = "lateral"            # laterale koers-/corridorafwijking
DIM_LOITER = "loiter"              # wachtpatroon

_DIMENSION_FOR_SOURCE = {
    "emergency_squawk": DIM_DECLARED,
    "emergency_status": DIM_DECLARED,
    "emergency_status_low_trust": DIM_DECLARED,
    "wrong_airport": DIM_GROUND_TRUTH,
    "wrong_airport_disputed": DIM_GROUND_TRUTH,
    "wrong_airport_unconfirmed": DIM_GROUND_TRUTH,
    "signal_lost": DIM_VANISHED,
    "signal_lost_disputed": DIM_VANISHED,
    "premature_descent": DIM_VERTICAL,
    "premature_descent_disputed": DIM_VERTICAL,
    "course_deviation": DIM_LATERAL,
    "corridor_deviation": DIM_LATERAL,
    "holding_destination": DIM_LOITER,
    "holding_non_destination": DIM_LOITER,
}
# Dimensies die de hypothese "de gefilede route klopt gewoon niet" NIET
# verklaart. Alleen een bewuste piloothandeling valt daaronder: een 7700
# hangt niet af van welke bestemming er gefiled staat.
_ROUTE_INDEPENDENT_DIMENSIONS = {DIM_DECLARED}
_SOFT_DIMENSIONS = {DIM_VANISHED, DIM_VERTICAL, DIM_LATERAL, DIM_LOITER}
```

**b. Nieuwe BEVESTIGD-poort** — `_confirmed_bar_met` volledig vervangen:
```python
    def _confirmed_bar_met(self, incident_id: int | None, current_source: str | None = None,
                            route_corroborated_now: bool = False) -> bool:
        """Of dit incident BEVESTIGD MAG heten, los van of de score de drempel
        haalt. BEVESTIGD betekent in dit systeem: er blijft geen redelijke
        alternatieve verklaring over. Dat is een uitspraak over de STRUCTUUR
        van het bewijs, niet over de hoogte van een som — zie CHECKPOINT.md
        bevinding 4 voor de volledige redenering.

        De alternatieve verklaringen die daadwerkelijk uitgesloten moeten
        worden, en wat elk ervan uitsluit:
          - sensorruis / decodeartefact  -> herhaling (zit al in de score);
          - de gefilede route is verouderd/fout -> route-corroboratie;
          - weer- of luchtruimomleiding, ATC-vertraging, normale
            aankomstsequencing -> bewijs in meer dan één dimensie, en geen
            actieve benigne verklaring.
        """
        sources = self._evidence_sources_seen(incident_id) if incident_id is not None else set()
        if current_source is not None:
            sources = sources | {current_source}
        reinforcing = sources - _NON_REINFORCING_SOURCES
        if not reinforcing:
            return False

        # 1. Een noodsquawk is een bewuste 4-cijferige piloothandeling. Geen
        #    datakwaliteits- of routine-operatieverklaring produceert die, dus
        #    hij hoeft door niets anders gesteund te worden.
        if "emergency_squawk" in reinforcing:
            return True

        # 2. Al het overige bewijs is een gevolgtrekking TEN OPZICHTE VAN de
        #    gefilede route. Zolang die route zelf alleen op één crowdsourced
        #    schedulebron rust, is de belangrijkste alternatieve verklaring
        #    ("de route klopt niet") niet uitgesloten — en die verklaart in
        #    één klap élk route-afhankelijk signaal tegelijk.
        if not (route_corroborated_now or "route_corroborated" in sources):
            return False

        # 3. Een benigne verklaring die het waargenomen gedrag dekt (actief
        #    SIGMET/CWA/TFR op deze positie, of meerdere toestellen die
        #    tegelijk hetzelfde doen) blijft gelden zolang het bewijs binnen
        #    de dimensies valt die zij verklaart — zie bevinding 6.
        dims = {_DIMENSION_FOR_SOURCE.get(s, s) for s in reinforcing}
        if (sources & _BENIGN_EXPLANATION_SOURCES) and not (dims & {DIM_GROUND_TRUTH}):
            return False

        # 4. Een waargenomen landing op een andere dan de (nu gecorroboreerde)
        #    gefilede bestemming is fysiek grondbewijs: dat is een diversie.
        if DIM_GROUND_TRUTH in dims:
            return True

        # 5. Anders: minstens twee wezenlijk verschillende soorten
        #    abnormaliteit. Eén soort, hoe lang ook volgehouden, blijft
        #    verenigbaar met een gewone operationele verklaring.
        return len(dims & _SOFT_DIMENSIONS) >= 2
```

**c.** `_resolve_state` krijgt de extra parameter `route_corroborated_now` en
geeft hem door; `_apply_delta` krijgt `route_corroborated: bool = False` en:
- legt bij `True` eenmalig een evidence-rij `("route_corroborated", 0.0,
  "route onafhankelijk bevestigd (eigen waarneming of tweede routebron)")` vast;
- geeft hem door aan `_resolve_state`.

**d.** `apply_events` geeft `route_corroborated=ev.route_corroborated` mee.

**e.** `_NON_REINFORCING_SOURCES` uitbreiden met `"route_corroborated"` — het is
context, geen bezwarend bewijs.

### 4. Validatie
Nieuwe `check_confirmed_bar_structure()` in `backtest.py`:
- **Het scenario uit §2 letterlijk** (corridor_deviation + premature_descent +
  holding_non_destination, `route_corroborated=False`): *vóór de fix*
  BEVESTIGD; *na de fix* WAARSCHIJNLIJK.
- Hetzelfde met `route_corroborated=True` → BEVESTIGD (twee zachte dimensies,
  route bevestigd) — bewijst dat de poort niet gewoon dicht staat.
- `course_deviation` + `corridor_deviation`, route gecorroboreerd, score ruim
  boven 85 → blijft WAARSCHIJNLIJK (één dimensie: `lateral`). Dit vervangt de
  bestaande, zwakkere `_DEVIATION_ONLY_SOURCES`-assertie.
- `emergency_squawk` alleen, geen route → BEVESTIGD (regressiebewaking).
- `wrong_airport` + `route_corroborated` → BEVESTIGD; zonder →
  WAARSCHIJNLIJK (overlapt met bevinding 2's validatie).

---

## Bevinding 5 — De ontlastende checks draaien alleen in cycli zónder vers bewijs

**Status:** gevalideerd (2026-08-08) — nieuwe assertions in
`check_airspace_regressions`. Geïmplementeerd als `IncidentManager.
apply_context_checks()`, aangeroepen vanuit `step()` buiten de `if not
events`-tak. Gemeten: een incident dat 5 cycli achtereen vers
`corridor_deviation`-bewijs krijgt binnen een actieve hazard-polygoon krijgt nu
wél de `weather_explains`-rij (vóór de fix: nooit).

### 1. Wat er mis is
`incidents.py:585-588` (`step`):
```python
        if not events:
            t = self.reassess(hex_id, ac, now, hazards)
```
en `reassess` is de enige plek die `_check_deviation_recovered`,
`_check_weather_explains` en `_check_peer_consensus` aanroept
(`incidents.py:492-513`).

### 2. Waarom dit een probleem is
Het gevaarlijkste scenario van dit hele systeem is een detector die *elke
cyclus opnieuw* vuurt (bevinding 3: dat is de route naar BEVESTIGD in minuten).
Precies in dat scenario is `events` nooit leeg, dus draait `reassess` nooit, en
worden de drie ontlastende checks **structureel overgeslagen**. Ze draaien
alleen bij incidenten die al gestopt zijn met bewijs produceren — en die
vervallen sowieso al vanzelf.

De exculpatoire logica is dus precies daar actief waar ze niet nodig is, en
precies daar uitgeschakeld waar ze het verschil zou maken.

**Scenario:** een toestel wijkt 120nm uit om een actief SIGMET-gebied heen.
`corridor_deviation` vuurt elke cyclus. `main.py` haalt elke cyclus keurig de
SIGMET-polygonen op (`main.py:565`) en geeft ze mee aan `step()`. `step()` ziet
`events` niet leeg → `reassess` wordt overgeslagen → `_check_weather_explains`
kijkt nooit naar die polygonen. Het incident klimt door terwijl de verklaring
elke 60 seconden opnieuw wordt opgehaald en weggegooid.

### 3. De exacte fix
In `incidents.py` een aparte context-stap die *altijd* draait, los van of er
vers bewijs was. `step()` wordt:
```python
        transitions = []
        if events:
            transitions.extend(self.apply_events(hex_id, callsign, aircraft_class, events, now))

        if hex_id not in self._open:
            return transitions

        landed_t = self._check_landed(hex_id, ac, now)
        if landed_t:
            transitions.append(landed_t)
            return transitions

        # Benigne-verklaringcontext draait ELKE cyclus, ook cycli mét vers
        # bewijs. Zie CHECKPOINT.md bevinding 5: een detector die elke cyclus
        # opnieuw vuurt is juist het scenario waarin deze checks nodig zijn,
        # en dat is precies het scenario waarin ze vroeger nooit draaiden.
        # Beide checks zijn intern al eenmalig-per-incident, dus dit levert
        # geen herhaalde aftrek op.
        inc = self._open.get(hex_id)
        if inc is not None and self.cfg.get("weather_sigmet_enabled"):
            explained = self._check_weather_explains(inc, hazards, now)
            if explained:
                t = self._maybe_notify(explained[0], explained[1], explained[2], now)
                if t:
                    transitions.append(t)
        inc = self._open.get(hex_id)
        if inc is not None and self.cfg.get("peer_consensus_enabled"):
            consensus = self._check_peer_consensus(inc, now)
            if consensus:
                t = self._maybe_notify(consensus[0], consensus[1], consensus[2], now)
                if t:
                    transitions.append(t)

        if hex_id not in self._open:
            return transitions
        if not events:
            t = self.reassess(hex_id, ac, now, hazards)
            if t:
                transitions.append(t)
        return transitions
```
en `reassess` verliest zijn eigen weer-/peer-blokken (regels 499-513) —
`_check_deviation_recovered` blijft er wél in, want herstel is alleen zinvol als
er deze cyclus geen bewijs van voortdurende afwijking was.

### 4. Validatie
Uitbreiding van `check_airspace_regressions()`:
- Incident dat **elke cyclus** een `corridor_deviation`-Event krijgt, positie
  binnen een hand-gebouwde hazard-polygoon. *Vóór de fix:* geen
  `weather_explains`-evidence-rij, score klimt door. *Na de fix:*
  `weather_explains` staat in de evidence-rijen na de eerste cyclus, en
  `_confirmed_bar_met` blijft dicht.
- Regressiebewaking: bij een incident zónder vers bewijs blijft het gedrag
  identiek (weather_explains nog steeds precies één keer).

---

## Bevinding 6 — Weer/peer-consensus zijn eenmalige aftrekposten en worden ook toegepast op bewijs dat ze niet verklaren

**Status:** gevalideerd (2026-08-08) — nieuwe assertions in
`check_airspace_regressions`. Beide checks zijn nu gescoped op
`_BENIGN_EXPLAINABLE_DIMENSIONS` ({lateraal, wachtpatroon}), en de verklaring
werkt als persistente blokkade op BEVESTIGD (punt 3 van `_confirmed_bar_met`)
in plaats van alleen als eenmalige score-aftrek. Gemeten: een `wrong_airport`-
incident binnen dezelfde polygoon blijft 90/BEVESTIGD (vóór: 40/MOGELIJK), en
drie geclusterde massale-diversie-incidenten blijven alle drie BEVESTIGD (vóór:
elk -55).

### 1. Wat er mis is
`_check_weather_explains` (`incidents.py:425-441`) en `_check_peer_consensus`
(`incidents.py:461-471`) doen elk precies één keer per incident een score-aftrek
(-50 resp. -55), zonder te kijken wélk bewijs het incident draagt.

### 2. Waarom dit een probleem is
Drie afzonderlijke problemen:

**(a) Een verklaring verloopt niet, een score-aftrek wel.** Een
`holding_non_destination` levert 12 punten per cyclus; de -50 is na 5 cycli
weggepoetst. Maar het feit "er ligt een actief SIGMET op deze positie" is na 5
minuten nog even waar. Een eenmalige aftrek modelleert een verklaring als een
tijdelijke korting in plaats van als een structureel alternatief.

**(b) Ze worden toegepast op bewijs dat ze niet kunnen verklaren.** Een incident
met `wrong_airport`-bewijs (het toestel is fysiek elders geland) dat toevallig
binnen een SIGMET-polygoon ligt, krijgt -50 en zakt van BEVESTIGD (90) naar
MOGELIJK (40). Maar *slecht weer verklaart geen landing op een ander vliegveld —
het is juist de meest voorkomende oorzaak van een echte diversie.* De check
verlaagt hier de zekerheid van precies het geval dat hij zou moeten versterken.
Wat weersvermijding wél verklaart is laterale uitwijking en wachten, niet
"geland op een ander veld" en niet een noodsquawk.

**(c) Peer-consensus onderdrukt massale diversies.** De premisse ("meerdere
toestellen doen tegelijk hetzelfde ⇒ gedeelde oorzaak ⇒ geen individuele
diversie") klopt voor *enroute-uitwijkingen*. Voor *landingen* klopt hij
omgekeerd: als een luchthaven sluit en tien toestellen wijken uit naar hun
alternates, is dat tien echte diversies — het nieuwswaardigste geval dat dit
systeem kent — en de huidige regel onderdrukt ze alle tien.

### 3. De exacte fix
**a.** Beide checks krijgen een dimensie-scope: ze doen alleen iets als het
bezwarende bewijs van het incident volledig binnen de dimensies valt die ze
kúnnen verklaren.
```python
# Dimensies die een actieve weers-/luchtruimverklaring of een gedeelde
# regionale oorzaak daadwerkelijk kan verklaren: eromheen vliegen en wachten.
# NIET: een waargenomen landing elders (weer is juist de meest voorkomende
# oorzaak van een ECHTE diversie), een noodsquawk, of een aanhoudende daling
# ver van de bestemming. Zie CHECKPOINT.md bevinding 6.
_BENIGN_EXPLAINABLE_DIMENSIONS = {DIM_LATERAL, DIM_LOITER}
_BENIGN_EXPLANATION_SOURCES = {"weather_explains", "peer_consensus"}
```
en in beide checks, vlak na de bestaande "al eens toegepast"-guard:
```python
        if not self._evidence_within_dimensions(inc["id"], _BENIGN_EXPLAINABLE_DIMENSIONS):
            return None
```
met:
```python
    def _evidence_within_dimensions(self, incident_id: int, dims: set) -> bool:
        """True als ALLE bezwarende evidence van dit incident in `dims` valt."""
        reinforcing = self._evidence_sources_seen(incident_id) - _NON_REINFORCING_SOURCES
        if not reinforcing:
            return False
        return {_DIMENSION_FOR_SOURCE.get(s, s) for s in reinforcing}.issubset(dims)
```

**b.** De verklaring wordt persistent in plaats van eenmalig: de
evidence-`source` (`weather_explains`/`peer_consensus`) blijft staan, en punt 3
van de nieuwe `_confirmed_bar_met` (bevinding 4) blokkeert BEVESTIGD zolang die
bron aanwezig is en er geen grondbewijs bij is gekomen. De score-aftrek blijft
staan als hij is (dat is een legitieme Bayesiaanse korting), maar hij is niet
langer het enige mechanisme.

**c.** Het scoping-effect maakt de aftrek automatisch onmogelijk zodra er
`wrong_airport`/`signal_lost`/`emergency`/`premature_descent`-bewijs ligt —
probleem (b) en (c) zijn daarmee weg, inclusief het massale-diversiescenario
(elk van die tien incidenten draagt `wrong_airport`, dus valt buiten
`_BENIGN_EXPLAINABLE_DIMENSIONS`).

### 4. Validatie
Uitbreiding van `check_airspace_regressions()`:
- Incident met alléén `corridor_deviation`-bewijs binnen een hazard-polygoon →
  `weather_explains` wordt toegepast (ongewijzigd t.o.v. nu).
- Incident met `wrong_airport`-bewijs binnen dezelfde polygoon. *Vóór de fix:*
  score 90 → 40, state zakt naar MOGELIJK. *Na de fix:* geen
  `weather_explains`-rij, score blijft 90.
- Drie geclusterde incidenten die elk `wrong_airport`-bewijs dragen. *Vóór:*
  elk -55. *Na:* geen `peer_consensus`-rij (massale diversie wordt niet
  onderdrukt).
- Drie geclusterde incidenten met alleen `course_deviation` → `peer_consensus`
  nog steeds toegepast (ongewijzigd).

---

## Bevinding 7 — Eén token-hit uit een tweede bron ontgrendelt een score die vrijwel volledig uit één bron komt

**Status:** gevalideerd (2026-08-08) — assertions 11-13 in
`check_incident_engine_regressions`. Gemeten: 9 cycli course_deviation → 55
(verzadigd, WAARSCHIJNLIJK); + 1 premature_descent → 80 (nog steeds
WAARSCHIJNLIJK); + nog 1 → 88 → BEVESTIGD.

### 1. Wat er mis is
`_confirmed_bar_met` (huidige versie) kijkt alleen of de *verzameling* bronnen
niet volledig binnen `_DEVIATION_ONLY_SOURCES` valt. Hoevéél elke bron aan de
score heeft bijgedragen, telt niet mee. Ondertussen blijft de score van de
geblokkeerde bron gewoon doorgroeien (`_apply_delta` past de cap alleen toe op
de *state*, niet op de score — zie `_resolve_state`'s docstring:
"Score/decay/notification bookkeeping elsewhere is untouched").

### 2. Waarom dit een probleem is
**Scenario:** een `course_deviation` die 12 cycli aanhoudt: 30 + 11×10 = 140.
State is WAARSCHIJNLIJK (poort dicht). Dan vuurt één keer `premature_descent`:
+25 → 165 → afgekapt op 150. De poort gaat open want de bronverzameling is nu
`{course_deviation, premature_descent}`. State springt in één stap naar
BEVESTIGD — op een score die voor 85% uit één afgekapte bron komt, ontgrendeld
door de zwakste evidence-tier die het systeem kent.

Het kwalitatieve verschil dat de poort zou moeten afdwingen, wordt bereikt met
een symbolische bijdrage.

### 3. De exacte fix
Volgt rechtstreeks uit de combinatie van bevindingen 3 en 4, zonder extra
mechanisme — dit is de reden om ze in die vorm te doen:
- de per-bron plafonds (bevinding 3) maken 140 uit één bron onmogelijk:
  `course_deviation` stopt bij 55;
- de dimensie-poort (bevinding 4) eist dat de tweede bron in een ándere
  dimensie zit én dat de route gecorroboreerd is.

Na de fix: 55 (lateral, verzadigd) + 25 (vertical) = 80 < 85 → nog geen
BEVESTIGD. Pas bij een tweede descent-hit (55 + 33 = 88 ≥ 85), mét
gecorroboreerde route en zonder actieve benigne verklaring, gaat de poort open.
Dat is een toestel dat aantoonbaar van zijn eigen, geverifieerde route af is
*én* aantoonbaar aan het dalen is ver van die bestemming, zonder weersverklaring
— daar blijft inderdaad weinig onschuldigs over.

Er is dus **geen aparte code voor bevinding 7**; hij staat hier omdat de
verwachte uitkomst apart gevalideerd moet worden.

### 4. Validatie
Nieuwe assertie in `check_confirmed_bar_structure()`:
- 12 cycli `course_deviation`, daarna 1× `premature_descent`, route
  gecorroboreerd. *Vóór de fix:* score 150, BEVESTIGD. *Na de fix:* score 80,
  WAARSCHIJNLIJK.
- Idem plus een tweede `premature_descent`-hit: score 88, BEVESTIGD.

---

## Bevinding 8 — `emergency`-bewijs heeft géén herhalingsdemping, en de "laag vertrouwen"-cap wordt daardoor omzeild

**Status:** gevalideerd (2026-08-08) — via bevinding 3's plafonds, plus de
expliciete assertie in `check_confirmed_bar_structure` dat het
emergency-STATUSveld de BEVESTIGD-poort ook mét ander bewijs en een
gecorroboreerde route niet opent (`_confirmed_bar_met` punt 1 test op de
bronnaam `emergency_squawk`, niet op de declared-dimensie).

### 1. Wat er mis is
`REPEATABLE_EVENT_TYPES` (`incidents.py:69`) is
`DEVIATION_EVENT_TYPES + ("holding_pattern", "premature_descent")` — `emergency`
staat er niet in, en `score_for_event`'s `emergency`-tak negeert
`is_repeat_type` volledig (`incidents.py:134-142`).

`detect_emergency` vuurt zolang de conditie waar is: in `tier1_loop` elke
cyclus (via `evaluate`), en in `tier0_loop` elke 15 seconden voor toestellen die
daadwerkelijk 7700/7600/7500 squawken.

### 2. Waarom dit een probleem is
Het project heeft met live bewijs vastgesteld dat het ADS-B `emergency`-
subveld onbetrouwbaar is (AUA96J/AUA12K/CFG6UA, alle squawk 1000, MASTERPLAN
sectie 1c/7) en heeft daar twee veiligheden voor gebouwd:
`_normalize_aircraft`'s `reserved`→`none`, en `enrich_events`' cap naar
WAARSCHIJNLIJK bij een conspicuity-squawk of niet-bevestigde tweede bron, wat
het gewicht van 35 naar 8 brengt.

Beide veiligheden worden door herhaling omzeild, want 8 punten per cyclus
divergeert net zo goed als 35:

| pad | per cyclus | interval | tijd tot ≥85 |
|---|---|---|---|
| `emergency_status` (discrete squawk, bevestigd/onbereikbaar) | +35 | 60s | **3 min** |
| `emergency_status_low_trust` (conspicuity-squawk, live bewezen artefact) | +8 | 60s | **11 min** |

Een toestel met een blijvend fout `emergency`-veld op squawk 1000 — exact het
gedocumenteerde artefact — bereikt dus alsnog BEVESTIGD, alleen wat later. De
cap die daarvoor gebouwd is, vertraagt hem en stopt hem niet.

**Scenario:** AUA96J squawkt 1000 met een blijvend niet-`none` emergency-veld.
`enrich_events` herkent de conspicuity-squawk en cap't naar WAARSCHIJNLIJK →
`emergency_status_low_trust`, 8 punten. Na 4 cycli: 32 → zichtbaar op het
dashboard. Na 7: 56 → WAARSCHIJNLIJK + Telegram. Na 11: 88 → **BEVESTIGD** +
🚨. Precies het geval dat het systeem had geïdentificeerd als decodeartefact.

### 3. De exacte fix
Volledig afgedekt door bevinding 3's per-bron plafonds, mits de plafonds voor
de emergency-bronnen correct staan (ze staan al in de tabel daar):
- `emergency_status_low_trust` → **24.0**, bewust nét onder
  `incident_score_possible_threshold` (25): een gedocumenteerd decodeartefact
  komt in zijn eentje niet eens op het dashboard, maar draagt nog wel bij aan
  een incident dat ánder bewijs heeft.
- `emergency_status` → **55.0**, precies `incident_score_likely_threshold`: een
  volgehouden, cross-provider-bevestigd emergency-veld op een discrete squawk is
  een echte melding waard, maar zonder squawk of ander bewijs niet "bevestigd".
- `emergency_squawk` → **150.0** (effectief ongelimiteerd): een echte 7700 moet
  onmiddellijk en blijvend BEVESTIGD zijn.

Daarnaast: `_confirmed_bar_met` punt 1 test expliciet op `"emergency_squawk"`,
niet op "een declared-dimensie" — zodat `emergency_status` de poort niet in
zijn eentje opent, ook niet als de score er ooit toch bovenuit zou komen door
combinatie met ander bewijs. Dat is bewust: het onderscheid tussen de
4-cijferige piloothandeling en het losse statusveld is precies wat het project
al drie ronden lang aan het verdedigen is.

### 4. De validatie
Zie bevinding 3's validatie (de drie emergency-assertions daar), plus:
- `emergency_status` 30 cycli + `route_corroborated` + één
  `corridor_deviation` → dims = {declared, lateral}, score 55+30 = 85 ≥ 85, maar
  `_confirmed_bar_met` punt 5 telt alleen `_SOFT_DIMENSIONS` (declared zit daar
  niet in) → één zachte dimensie → **WAARSCHIJNLIJK**, niet BEVESTIGD.
  Regressiebewaking dat het statusveld de poort niet opent.

---

## Nog niet uitgewerkt / bewust buiten scope gehouden

Opgemerkt tijdens de analyse, niet volledig doordacht — niet stilzwijgend laten
vallen:

- **Geen disconfirmatie-mechanisme.** `_check_deviation_recovered` is de enige
  weg terug, en die weigert te draaien zodra er ander bewijs dan
  course/corridor_deviation ligt. Een toestel dat na een `premature_descent`
  gewoon weer naar cruise klimt levert positieve tegenbewijs op dat nergens
  geconsumeerd wordt; de enige uitweg is 0.85-verval.
- **`peer_consensus_radius_deg` is een harde rastercel** (2° ≈ 120nm): twee
  toestellen 1nm uit elkaar maar aan weerszijden van een celgrens tellen niet
  mee, twee toestellen 160nm uit elkaar in dezelfde cel wel. MASTERPLAN sectie
  14 noemt dit zelf al als ongetuned.
- **`nearest_large(max_km=90.0)`** in `detect_holding_pattern`: 48nm is ruim, en
  in dicht luchtruim is vrijwel elke positie binnen 48nm van een *large*
  luchthaven die niet de gefilede bestemming is.
- **Geen persistentie-eis op een enkele 7700-sample.** tier0 draait elke 15s, dus
  twee opeenvolgende waarnemingen eisen kost 15 seconden en sluit een
  enkelvoudig decodeartefact uit. Geen live bewijs dat dit nu misgaat, dus niet
  aangepakt op verdenking alleen.


---

## Extra tijdens implementatie gevonden (niet in de oorspronkelijke analyse)

### A. `backtest.py`'s `run_case` voedde `signal_lost_near_airport` nooit aan de incident-engine
`run_case`'s `goes_missing`-blok voegde het Event alleen aan `fired` toe (voor
de detector-rapportage), maar riep — anders dan `main.py`'s tier1_loop, die het
gewoon in `events_by_hex` stopt — nooit `incident_mgr.step()` aan. Daardoor was
`signal_lost_near_airport` de enige detector wiens bewijs in een backtest nooit
de zekerheidslogica bereikte. Onzichtbaar zolang de engine alleen met
handgebouwde Events werd getest; wel relevant zodra de incident-uitkomst van een
echte case wordt geassert. Opgelost, mét route-corroboratie-stempel, zoals
main.py het doet.

### B. `run_case` spiegelde `record_takeoff` niet
`run_case` zette wel `track.last_takeoff_ts` maar niet `track.pending_origin`,
terwijl `main.py` beide via `store.record_takeoff()` zet. Daardoor kon de
"zelf waargenomen vertrek vanaf het gefilede vertrekpunt"-corroboratieweg
(bevinding 2) in de backtest nooit aangaan. Zichtbaar geworden doordat TO3510
(een echte vroege terugkeer naar Orly) ongecorroboreerd bleef terwijl productie
hem meteen zou corroboreren. Opgelost.

### C. De zekerheidsuitkomst van elke echte case wordt nu expliciet vastgelegd
`check_real_case_confidence_outcomes` draait álle cases uit
`backtest_cases.py` door de volledige incident-engine en assert per case of
BEVESTIGD terecht wel/niet bereikt wordt. Reden: elke andere nieuwe check
bewijst dat iets nu terecht NIET bevestigd wordt, en een regel die nooit
BEVESTIGD zegt is even nutteloos als een die het altijd zegt. Deze tabel is de
tegenhanger, zodat een volgende tuningronde in één oogopslag ziet of hij één
van beide kanten heeft gebroken.

Uitkomst na alle fixes (10 cases, 118 assertions groen):

| case | bereikt BEVESTIGD | waarom |
|---|---|---|
| AI850 (hold-incident) | nee | één dimensie (loiter) |
| AI850 (nood-incident) | ja | MAYDAY-squawk + landing elders |
| AF9 turnback | ja | noodsquawk (route-onafhankelijk) |
| UA2078 (geland Luke AFB) | ja | grondbewijs + gecorroboreerde route |
| UA2078 (signaalverlies-variant) | **nee** | redeneert uit afwezigheid van data; een dekkingsgat is een gewone verklaring |
| EK225 (beide varianten) | ja | grondbewijs op LHR na gecorroboreerde DXB→SFO-leg |
| TO3510 vroege terugkeer | ja | grondbewijs; route gecorroboreerd via zelf waargenomen vertrek |
| ATL→MCO→FLL | ja | grondbewijs |
| TK17 → Manchester | ja | grondbewijs |
| DAL2778 (echte weersomweg) | nee | 30 punten, sluit als vals alarm |

De twee UA2078-varianten zijn dezelfde echte diversie, anders waargenomen, en
komen nu bewust op verschillende niveaus uit — dat is precies het onderscheid
waar deze hele ronde over ging.


---

## Bevinding 9 — De "zelf waargenomen vertrek"-corroboratieweg bevestigt de verkeerde helft van de route

**Status:** gevalideerd (2026-08-08) — gevonden tijdens implementatie, bij
zelfcontrole van bevinding 2. Twee nieuwe assertions in
`check_route_corroboration`: het DLH8NK-scenario (kloppende origin, foute
bestemming) haalt geen BEVESTIGD meer, en een `early_return`-Event haalt het
wél zonder enige route-corroboratie. `check_real_case_confidence_outcomes`
bleef groen: TO3510 bevestigt nu via `wrong_airport_early_return` als enige
bewijsbron, in plaats van via de verwijderde origin-corroboratieweg.

### 1. Wat er mis is
Bevinding 2 gaf `route_corroborated` drie manieren om waar te worden. Eén
daarvan — "wij zagen dit toestel zelf vertrekken vanaf het gefilede
vertrekpunt" (`main.py`'s tier1_loop, `track.pending_origin` vs.
`track.route["origin_icao"]`) — bevestigt alleen de **vertrek**helft van de
route.

Maar élke route-afhankelijke detector meet tegen de **bestemming**. En
`wrong_airport` — de zwaarste bron, 90 punten, en de enige die in zijn eentje
BEVESTIGD kan halen — gaat *volledig* over de bestemming.

### 2. Waarom dit een probleem is
Het laat precies de casus door die deze hele ronde motiveerde. DLH8NK: adsbdb
filede EDDF→LEMG, het toestel vloog in werkelijkheid EDDF→EDDM.
- De **origin klopt** (EDDF), alleen de bestemming niet.
- Wij zien het toestel vertrekken vanaf EDDF → `pending_origin` = EDDF =
  `route["origin_icao"]` → `route_corroborated = True`.
- Het landt op EDDM → `wrong_airport`, 90 punten, GROUND_TRUTH-dimensie, route
  "gecorroboreerd" → **BEVESTIGD**.

Dat is exact het oude gedrag, met een extra stap ertussen. Omdat dit systeem
wereldwijd continu volgt, is "wij zagen het vertrekken" bovendien géén zeldzame
situatie maar het normale geval — dus dit gat zou een groot deel van bevinding
2's winst weer weggeven.

Het onderscheid dat de vertrek-observatie wél maakt, is een ánder: zij sluit de
*verkeerde-leg*-fout uit (callsign vliegt A→B en later B→A; adsbdb heeft alleen
A→B). Ze sluit de *verkeerde-bestemming*-fout niet uit. Twee verschillende
foutbronnen, en ik had ze onder één vlag geschoven.

### 3. Waarom de vroege-terugkeercasus dan toch moet blijven werken
De reden dat ik die weg had toegevoegd, was TO3510 (echte vroege terugkeer
Orly→Mykonos→Orly): zonder hem bleef die ongecorroboreerd. Maar bij nader
inzien is dat bewijs helemaal niet route-afhankelijk. De waarneming is:

> wij hebben dit toestel zelf zien opstijgen van luchthaven X en binnen 45
> minuten weer zien landen op diezelfde luchthaven X.

Daar komt de gefilede bestemming niet in voor. Dat is abnormaal ongeacht waar
het toestel volgens de schedule heen zou gaan, en het is volledig zelfstandig
waargenomen — beide uiteinden, door onszelf. Het hoort dus een eigen
route-onafhankelijke bewijsbron te zijn, naast de noodsquawk, in plaats van via
een te zwakke route-corroboratie te worden binnengesmokkeld.

### 4. De exacte fix
**a. `main.py`** — de `track.pending_origin`-tak uit het route-corroboratieblok
verwijderen. `route_corroborated` betekent voortaan uitsluitend: de
**bestemming** is bevestigd (tweede routebron noemt dezelfde bestemming, óf wij
zagen het toestel ≥30% van de route richting die bestemming afleggen). Idem in
`backtest.py`'s `run_case`-spiegel.

**b. `detector.py`** — nieuw veld op `Event`:
```python
    early_return: bool = False
```
en in `detect_landed_wrong_airport`'s early-return-tak `early_return=True`
meegeven op het Event, maar **alleen** als wij ook het vertrekpunt zelf hebben
waargenomen én dat hetzelfde veld is als waar het nu landt:
```python
        early_return = bool(track.pending_origin) and track.pending_origin["icao"] == nearest["icao"]
```
Die extra eis maakt de waarneming sluitend: zonder hem zou "geland op wat
volgens de schedule het vertrekpunt is" nog steeds op foute routedata kunnen
berusten.

**c. `incidents.py`, `score_for_event`** — eigen bron vóór de bestaande
wrong_airport-takken:
```python
        if ev.early_return:
            return 90.0, "wrong_airport_early_return", "kort na vertrek teruggekeerd naar het vertrekveld (beide uiteinden zelf waargenomen)"
```
met `"wrong_airport_early_return"` toegevoegd aan `_WRONG_AIRPORT_SOURCES` en
aan `_DIMENSION_FOR_SOURCE` (→ `DIM_GROUND_TRUTH`), en `_SOURCE_SCORE_CAP` 90.0.

**d. `incidents.py`, `_confirmed_bar_met`** — punt 1 uitbreiden:
```python
        if reinforcing & _ROUTE_INDEPENDENT_SOURCES:
            return True
```
met
```python
# Bronnen waarvan de waarneming zelf niet van de gefilede route afhangt, en die
# daarom geen route-corroboratie nodig hebben:
#  - emergency_squawk: bewuste 4-cijferige piloothandeling;
#  - wrong_airport_early_return: opstijgen en binnen 45 min terugkeren naar
#    hetzelfde veld, beide uiteinden door onszelf waargenomen.
_ROUTE_INDEPENDENT_SOURCES = {"emergency_squawk", "wrong_airport_early_return"}
```

### 5. Validatie
- Nieuwe assertie in `check_route_corroboration`: een `wrong_airport`-Event met
  alléén een waargenomen vertrek vanaf de gefilede origin (en géén
  bestemmingscorroboratie) bereikt **geen** BEVESTIGD. *Vóór deze fix:* wél
  (het DLH8NK-gat).
- Nieuwe assertie: een `early_return`-Event bereikt **wel** BEVESTIGD, ook
  zonder enige route-corroboratie.
- `check_real_case_confidence_outcomes` moet ongewijzigd groen blijven: TO3510
  haalt BEVESTIGD via de nieuwe early-return-bron in plaats van via de
  verwijderde corroboratieweg.
