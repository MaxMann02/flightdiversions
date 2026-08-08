# CHECKPOINT — fundamentele review van de zekerheidslogica

Sessie gestart 2026-08-08. Doel: één keer vanaf de grond kritisch nadenken over
HOE dit systeem tot een zekerheidsoordeel komt, in plaats van opnieuw losse
symptomen te repareren. Zie MASTERPLAN.md / BACKTEST_LOG.md voor de
voorgeschiedenis.

**Werkwijze:** bevinding voor bevinding. Elke bevinding wordt hier volledig
uitgeschreven (probleem + redenering + exacte fix + validatie) vóórdat er iets
geïmplementeerd wordt, zodat een onderbreking nooit redenering kost.

**STAND VAN ZAKEN (2026-08-08):** alle 9 bevindingen zijn geïmplementeerd,
gevalideerd (`python backtest.py`: 10/10 cases, 120 assertions, geen FAIL) en
gemerged naar `main` (commit `be08c5b`, merge `49d82b1`, gepusht — de VM pulled
main). Een live smoke test van 2 minuten (8222 toestellen, Telegram uit) draaide
zonder exceptions en ving een echte 7700 die correct direct BEVESTIGD werd.
Een volgende sessie hoeft fase 1 dus niet over te doen; wat nog openstaat, staat
onderaan dit bestand onder "Nog niet uitgewerkt".

**STAND VAN ZAKEN SESSIE 2 (2026-08-08):** bevindingen 10-13 zijn erbij gekomen,
alle vier geïmplementeerd en gevalideerd (`python backtest.py`: 10/10 cases,
**153** assertions, geen FAIL — 33 nieuwe). Ze zitten alle vier in de
BEVESTIGD-poort en volgen uit één observatie: die poort beslist sinds ronde 1 op
de STRUCTUUR van het bewijs, maar las die structuur af uit een verzameling
bronnamen die (a) alleen groeit, (b) betwist niet van onbetwist bewijs
onderscheidt, en (c) op één moment bevroren werd. Nieuw in de code:
`_ROUTE_DISPUTED_SOURCES` (punt 2b: bij onenigheid tussen routebronnen geen
BEVESTIGD), `DIM_GROUND_TRUTH_PROVISIONAL`, `_benign_explanation_scope` +
`_release_benign_deduction` (vastleggen/aftrekken/poorten uit elkaar),
`_active_dimensions` + `_REFUTES_DIMENSION` (weerlegging op tijdstempel) en
`_check_signal_lost_refuted`. Van de vier resterende punten onder "Nog niet
uitgewerkt" is daarmee het eerste (geen disconfirmatie-mechanisme) gedeeltelijk
afgehandeld — zie bevinding 12 punt 5 voor wat er bewust van over is.

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
  → **Sessie 2, bevinding 12: gedeeltelijk opgelost.** Het raamwerk bestaat nu
  (`_REFUTES_DIMENSION` + `_active_dimensions`, weerlegging op tijdstempel zodat
  niets permanent uitgeschakeld raakt) en `DIM_VANISHED` wordt daadwerkelijk
  weerlegd zodra het toestel weer gevolgd wordt. `DIM_VERTICAL`/`DIM_LOITER`
  bewust nog niet: daarvoor is per-bewijsrij bijgehouden hoogte/positie nodig
  die er niet is (`inc["last_alt"]` wordt door elke volgende rij overschreven).
  Zie bevinding 12 punt 5.
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

---

# SESSIE 2 (2026-08-08) — tweede ronde, voortbouwend op bevindingen 1-9

Bevindingen 1-9 zijn als vaststaand behandeld en niet opnieuw afgeleid. Deze
ronde is begonnen bij de vraag die na ronde 1 openbleef: de zekerheidspoort
`_confirmed_bar_met` beslist nu op de STRUCTUUR van het bewijs, maar die
structuur wordt afgelezen uit een verzameling bronnamen die (a) alleen groeit,
(b) geen onderscheid maakt tussen bewijs waarvan de premisse vaststaat en
bewijs waarvan de premisse betwist is, en (c) op één moment wordt bevroren.
Alle vier de bevindingen hieronder komen daaruit voort.

**Empirisch vastgesteld vóór het schrijven**, niet uit lezing afgeleid — zie de
probe-uitvoer per bevinding.

---

## Bevinding 10 — Betwist bewijs opent dezelfde poort als onbetwist bewijs

**Status:** gevalideerd (2026-08-08) — `check_disputed_evidence_gate` in
`backtest.py`, 5 assertions. Gemeten: het disputed-scenario zakt van
121/BEVESTIGD naar 121/WAARSCHIJNLIJK, een onbevestigde grondstatus levert
`ground_truth_provisional` in plaats van `ground_truth`, en onbetwist
grondbewijs blijft BEVESTIGD (140).

### 1. Wat er precies mis is

Twee losse plekken, één onderliggende fout.

**10a. `incidents.py:420` (`_confirmed_bar_met`, punt 2).**
```python
        if not (route_corroborated_now or "route_corroborated" in sources):
            return False
```
Dit is een kale OR op "is er corroboratie". Er staat nergens een test op
tegenspraak. Corroboratie en tegenspraak sluiten elkaar echter niet uit:
`track.route_corroborated` wordt gezet door `main.py:538-547` op grond van onze
EIGEN waargenomen voortgang, terwijl `ev.route_source_disputed` los daarvan
wordt gezet door `main.py:91` / `main.py:209` als hexdb.io een ANDERE
bestemming noemt. `detector.py:726-729` stempelt vervolgens
`track.route_corroborated` op élk Event, inclusief het betwiste. Beide vlaggen
staan dan tegelijk aan, en de poort leest dat als "de route staat vast".

**10b. `incidents.py:141-144` + `incidents.py:436-437` (punt 4).**
```python
    "wrong_airport": DIM_GROUND_TRUTH,
    "wrong_airport_disputed": DIM_GROUND_TRUTH,
    "wrong_airport_unconfirmed": DIM_GROUND_TRUTH,
```
```python
        if DIM_GROUND_TRUTH in dims:
            return True
```
Punt 4 is de sterkste kortsluiting in het hele model: één bewijsrij, geen
tweede dimensie nodig. Zijn premisse staat er letterlijk boven — "een
waargenomen landing ... is fysiek grondbewijs". Maar `wrong_airport_unconfirmed`
betekent per definitie dat de tweede ADS-B-provider de GRONDSTATUS níét
bevestigde (`main.py:47-52`), dus dat de waarneming waar punt 4 op steunt juist
niet vaststaat. Bevinding 1 heeft die twijfel alleen in de SCORE verwerkt
(90 → 25), niet in de structuur — en de score is precies wat door andere
bronnen weer aangevuld kan worden.

### 2. Waarom dit een probleem is

Dit is dezelfde fout als bevinding 4, één niveau hoger. Bevinding 4 stelde vast
dat meerdere detectoren die allemaal tegen dezelfde gefilede bestemming meten
geen onafhankelijke corroboratie zijn. De reparatie was: eis dat die bestemming
onafhankelijk bevestigd is. Maar "onafhankelijk bevestigd" is geïmplementeerd
als "er bestaat ergens een bevestiging", niet als "de bronnen zijn het eens".
Zodra twee referentiebronnen elkaar tegenspreken is de eerlijke toestand
*onbeslist*, niet *bevestigd* — en onbeslist is exact de hypothese ("de
gefilede bestemming klopt niet") die élk route-afhankelijk signaal tegelijk
verklaart.

Let op wat hier NIET beweerd wordt: hexdb.io is niet betrouwbaarder dan adsbdb.
Ronde 24 heeft live vastgesteld dat het omgekeerde ook voorkomt (UAL1601's
hexdb-route was 8 jaar oud). Juist daarom is "hexdb wint" verkeerd — maar
"adsbdb wint zolang wij zelf voortgang zagen" is even ongefundeerd. Bij
onenigheid hoort het hoogste zekerheidsniveau dicht te blijven, meer niet.
WAARSCHIJNLIJK blijft bereikbaar en notificeert al (`_maybe_notify`), dus dit
kost geen melding.

**Concreet scenario (gemeten, niet bedacht).** Een toestel met gefilede route
EHAM→EGLL waarvan wij de voortgang richting EGLL zelf hebben gezien
(`route_corroborated_by_progress` → `track.route_corroborated = True`), wijkt af
en landt elders. hexdb.io noemt een andere bestemming → `route_source_disputed`
→ `wrong_airport_disputed` (30 punten). Probe-uitvoer:

```
  betwiste-route-variant bronnen: ['corridor_deviation', 'premature_descent',
                                   'route_corroborated', 'wrong_airport_disputed']
  score 121  state BEVESTIGD
```

Twee bronnen spreken elkaar tegen over de bestemming, en het systeem meldt
BEVESTIGD met een 🚨. En voor 10b, dezelfde opzet met een onbevestigde
grondstatus:

```
  bronnen: ['corridor_deviation', 'premature_descent',
            'route_corroborated', 'wrong_airport_unconfirmed']
  dims:    ['ground_truth', 'lateral', 'vertical']
  score 116  state BEVESTIGD
```

### 3. De exacte fix

**a. `incidents.py`, bij de dimensie-constanten, drie nieuwe:**

```python
# Bronnamen die alleen ontstaan wanneer een TWEEDE routebron (hexdb.io) een
# ANDERE bestemming noemt dan de gefilede — zie main.py's enrich_events. Hun
# aanwezigheid is de persistente vastlegging dat de referentiedata achter dit
# incident BETWIST is; er is geen apart schemaveld voor nodig.
_ROUTE_DISPUTED_SOURCES = {"wrong_airport_disputed", "signal_lost_disputed",
                           "premature_descent_disputed"}

# De gedegradeerde wrong_airport-varianten als bronnamen (voor _check_landed).
_PROVISIONAL_GROUND_TRUTH_SOURCES = {"wrong_airport_disputed", "wrong_airport_unconfirmed"}

# Grondbewijs waarvan de PREMISSE van punt 4 niet vaststaat. Punt 4 zegt: "een
# waargenomen landing elders IS de diversie". Dat rust op twee dingen die
# allebei moeten kloppen — de grondwaarneming zelf, en de bestemming waar we
# hem tegen afmeten. Bij deze twee bronnen is precies één van die twee actief
# betwist door een tweede bron. Ze houden hun (al gedempte) score en tellen
# gewoon mee als soft-dimensie voor punt 5, maar ze mogen punt 4's
# kortsluiting-op-één-bewijsrij niet omzetten.
DIM_GROUND_TRUTH_PROVISIONAL = "ground_truth_provisional"
```

**b. `_DIMENSION_FOR_SOURCE` aanpassen:**
```python
    "wrong_airport_disputed": DIM_GROUND_TRUTH_PROVISIONAL,
    "wrong_airport_unconfirmed": DIM_GROUND_TRUTH_PROVISIONAL,
```
(`wrong_airport` en `wrong_airport_early_return` blijven `DIM_GROUND_TRUTH`.)

**c. `_SOFT_DIMENSIONS` uitbreiden:**
```python
_SOFT_DIMENSIONS = {DIM_VANISHED, DIM_VERTICAL, DIM_LATERAL, DIM_LOITER,
                    DIM_GROUND_TRUTH_PROVISIONAL}
```

**d. `_confirmed_bar_met`, direct ná de bestaande punt-2-test, een punt 2b:**
```python
        # 2b. Corroboratie en tegenspraak sluiten elkaar niet uit: de
        #     corroboratie kan uit onze EIGEN waargenomen voortgang komen
        #     (main.py's route_corroborated_by_progress) terwijl hexdb.io
        #     tegelijk een andere bestemming noemt. Punt 2 was een kale OR en
        #     las dat als "de route staat vast". Bij onenigheid tussen twee
        #     referentiebronnen is de eerlijke toestand onbeslist — en
        #     onbeslist is precies de hypothese die élk route-afhankelijk
        #     signaal tegelijk verklaart. Niet "hexdb wint" (ronde 24 mat dat
        #     hexdb even goed fout kan zijn), maar "bij onenigheid geen
        #     BEVESTIGD". Zie CHECKPOINT.md bevinding 10.
        if sources & _ROUTE_DISPUTED_SOURCES:
            return False
```

### 4. Validatie

Nieuwe check `check_disputed_evidence_gate` in `backtest.py`:
- `corridor_deviation` ×3 + `premature_descent` ×3 + `wrong_airport` met
  `route_source_disputed=True`, route gecorroboreerd. *Vóór:* score 121,
  BEVESTIGD. *Na:* score 121, WAARSCHIJNLIJK.
- Idem met `confidence="WAARSCHIJNLIJK"` (onbevestigde grondstatus) en zónder
  de verticale dimensie, zodat punt 4 het enige is dat kan bevestigen.
  *Vóór:* punt 4 ontgrendelt op één bewijsrij. *Na:* punt 4 blijft dicht,
  punt 5 vereist een tweede dimensie.
- Regressie: onbetwist `wrong_airport` + gecorroboreerde route blijft
  BEVESTIGD (punt 4 moet niet gewoon dicht komen te staan).
- `check_real_case_confidence_outcomes` moet ongewijzigd blijven — geen enkele
  echte case zet `route_source_disputed` (dat gebeurt in `enrich_events`, die
  netwerk nodig heeft).

---

## Bevinding 11 — Of de weersverklaring wordt vastgelegd hangt af van de VOLGORDE waarin detectoren vuurden

**Status:** gevalideerd (2026-08-08) — `check_benign_explanation_scope` in
`backtest.py`, 7 assertions. Gemeten: beide volgordes komen nu uit op exact
91.0/WAARSCHIJNLIJK mét `weather_explains`-rij (vóór: 61/WAARSCHIJNLIJK vs
91/BEVESTIGD), idem voor peer-consensus, en het "vliegt de polygoon pas later
in"-scenario zakt van 88/BEVESTIGD naar 88/WAARSCHIJNLIJK.

### 1. Wat er precies mis is

`incidents.py:363-368`:
```python
    def _evidence_within_dimensions(self, incident_id: int, dims: set) -> bool:
        own = self._evidence_dimensions(self._evidence_sources_seen(incident_id))
        return bool(own) and own.issubset(dims)
```
gebruikt door `_check_weather_explains` (`incidents.py:732`) en
`_check_peer_consensus` (`incidents.py:773`) als toegangsvoorwaarde:

```python
        if not self._evidence_within_dimensions(inc["id"], _BENIGN_EXPLAINABLE_DIMENSIONS):
            return None
```

Deze subset-test wordt geëvalueerd op het moment dat de check draait, en het
resultaat wordt permanent: beide checks zijn eenmalig per incident
(`if "weather_explains" in self._evidence_sources_seen(...)`). Slaagt de test
niet, dan wordt er niets vastgelegd — en de volgende cyclus is de bewijsset
alleen maar verder gegroeid, dus slaagt hij nóóit meer.

Gevolg: of een incident ooit een `weather_explains`-rij krijgt, hangt af van of
de laterale detector toevallig vóór of ná de verticale vuurde.

### 2. Waarom dit een probleem is

Twee dingen die niets met elkaar te maken hebben zijn in één test gepropt:
*mag deze verklaring worden vastgelegd* en *hoeveel score trekt zij af*.
Bevinding 6 heeft terecht vastgesteld dat de aftrek niet mag gelden voor bewijs
dat de verklaring niet dekt (een landing elders wordt niet door weer verklaard).
Maar dat is als een subset-test op de HELE bewijsverzameling geïmplementeerd, en
daarmee is de verklaring ook uit de ADMINISTRATIE verdwenen zodra er één
bewijsrij bijkomt die zij niet dekt.

Dat compoundeert precies de verkeerde kant op:
1. de tweede dimensie schakelt de benigne verklaring uit (punt 3 van
   `_confirmed_bar_met` vuurt nooit, want `weather_explains` staat er niet), en
2. diezelfde tweede dimensie ontgrendelt punt 5 (`len(dims & _SOFT_DIMENSIONS)
   >= 2`).

Eén extra signaal verwijdert dus de ontlastende verklaring én levert de
bevestigende voorwaarde. Terwijl een toestel dat om een onweersgebied heen
vliegt én daarbij daalt (ATC-daling, uit ijsvorming zakken, onder de bui door)
de meest doodgewone waarneming is die er bestaat.

**Concreet scenario (gemeten).** Identiek bewijs, identiek SIGMET-gebied,
identieke positie — alleen de volgorde verschilt:

```
lateraal EERST, daarna daling    weather_explains=True    score  61  WAARSCHIJNLIJK
daling EERST, daarna lateraal    weather_explains=False   score  91  BEVESTIGD
afwisselend, daling begint       weather_explains=False   score  91  BEVESTIGD
```

En de realistische variant — het toestel wijkt eerst af en vliegt de polygoon
pás daarna in, wat de normale gang van zaken is omdat `main.py` de polygonen
tegen de HUIDIGE positie meet:

```
  bronnen ['corridor_deviation', 'premature_descent', 'route_corroborated']
  weather_explains=False  score 88  state BEVESTIGD
```

Hetzelfde geldt één-op-één voor `_check_peer_consensus` (gemeten: 61/
WAARSCHIJNLIJK vs 91/BEVESTIGD op dezelfde volgordewissel).

### 3. De exacte fix

Drie taken uit elkaar trekken: **vastleggen**, **aftrekken**, **poorten**.

**a. `incidents.py`: `_evidence_within_dimensions` vervangen door**
```python
    def _benign_explanation_scope(self, incident_id: int) -> tuple[bool, bool]:
        """(mag er een benigne-verklaringrij komen, dekt zij ALLES).

        Twee losse vragen die vroeger één subset-test waren — en dat maakte de
        uitkomst afhankelijk van de VOLGORDE waarin de detectoren vuurden (zie
        CHECKPOINT.md bevinding 11), omdat de test op één moment werd
        geëvalueerd en het resultaat daarna permanent was:

          - VASTLEGGEN mag zodra dit incident ÉÉN bewijsrij heeft die de
            verklaring dekt (lateraal/wachtpatroon). Dat de polygoon actief is
            op deze positie is dan een feit over dit incident, ongeacht wat er
            verder nog aan bewijs ligt — en het hoort niet verloren te gaan
            doordat er later een dimensie bijkomt die zij niet dekt.
          - AFTREKKEN mag alleen als de verklaring ALLES dekt wat het incident
            draagt. Dekt zij maar een deel, dan houdt de rest zijn score
            (bevinding 6: weer verklaart geen landing elders) en doet de rij
            alleen mee als poortvoorwaarde in _confirmed_bar_met.
        """
        dims = self._evidence_dimensions(self._evidence_sources_seen(incident_id))
        if not (dims & _BENIGN_EXPLAINABLE_DIMENSIONS):
            return False, False
        return True, dims <= _BENIGN_EXPLAINABLE_DIMENSIONS
```

**b. `_check_weather_explains`: de gate-regel vervangen door**
```python
        applies, covers_all = self._benign_explanation_scope(inc["id"])
        if not applies:
            return None
```
en de `_apply_delta`-aanroep krijgt `-50.0 if covers_all else 0.0` in plaats
van het vaste `-50.0`. De beschrijving blijft ongewijzigd — een rij met delta
0.0 is nog steeds de vastlegging dat de verklaring geldt, alleen zonder aftrek.

**c. `_check_peer_consensus`: idem**, met `-55.0 if covers_all else 0.0`. De
volgorde van de bestaande gates (eenmaligheid, dan scope, dan het
`peer_consensus_min_aircraft`-quorum) blijft zoals hij is.

**d. `_confirmed_bar_met`, punt 3 vervangen.** De harde blokkade wordt een
dimensie-aftrek, zodat de verklaring precies neutraliseert wat zij verklaart en
de rest het op eigen kracht moet halen:
```python
        # 3. Een benigne verklaring (actief SIGMET/CWA/TFR op deze positie, of
        #    meerdere toestellen die tegelijk hetzelfde doen) verloopt niet en
        #    verzwakt niet door herhaling. Zij NEUTRALISEERT de dimensies die
        #    zij daadwerkelijk verklaart — eromheen vliegen en wachten — en wat
        #    daarna overblijft moet de poort op eigen kracht halen. Vroeger was
        #    dit een harde blokkade op het hele incident; dat blokkeerde ook
        #    bewijs waar het weer niets mee te maken heeft (laag verdwijnen bij
        #    een andere luchthaven) en is nu preciezer.
        if sources & _BENIGN_EXPLANATION_SOURCES:
            dims = dims - _BENIGN_EXPLAINABLE_DIMENSIONS
```
Punt 4 en punt 5 draaien daarna ongewijzigd op de zo verkleinde `dims`.

Uitkomsten die hiermee vastliggen (alle vijf bewust):

| bewijs | benigne verklaring | resterende dims | BEVESTIGD |
|---|---|---|---|
| lateraal | weer | leeg | nee (ongewijzigd) |
| lateraal + wachtpatroon | weer | leeg | nee (ongewijzigd) |
| lateraal + daling | weer | {verticaal} | **nee (was: ja)** |
| lateraal + daling + verdwenen | weer | {verticaal, verdwenen} | **ja (was: nee)** |
| lateraal + landing elders | weer | {grondbewijs} | ja (ongewijzigd, bevinding 6) |

De vierde rij is een bewuste versoepeling: weersvermijding verklaart eromheen
vliegen, niet laag verdwijnen bij een andere luchthaven terwijl je daalt. De
oude blokkade gooide dat op één hoop.

### 4. Validatie

Nieuwe check `check_benign_explanation_scope` in `backtest.py`:
- Dezelfde zes Events in beide volgordes, dezelfde hand-gebouwde
  hazard-polygoon. *Vóór:* 61/WAARSCHIJNLIJK vs 91/BEVESTIGD. *Na:* beide
  61/WAARSCHIJNLIJK, en beide met een `weather_explains`-rij.
- Idem voor peer-consensus. *Vóór:* 61 vs 91. *Na:* beide gelijk.
- Het "vliegt de polygoon pas later in"-scenario. *Vóór:* 88/BEVESTIGD zonder
  `weather_explains`-rij. *Na:* `weather_explains` aanwezig, geen BEVESTIGD.
- Aftrek-regressie (bevinding 6 mag niet terugdraaien): een incident met
  `wrong_airport`-bewijs binnen dezelfde polygoon houdt zijn score (de nieuwe
  rij heeft delta 0.0) en blijft BEVESTIGD.
- Uitbreidingsassertie: lateraal + daling + `signal_lost` binnen een polygoon
  bereikt wél BEVESTIGD (rij 4 van de tabel).
- `check_airspace_regressions` moet ongewijzigd groen blijven.

---

## Bevinding 12 — Weerlegd bewijs blijft als dimensie meetellen

**Status:** gevalideerd (2026-08-08) — `check_evidence_refutation` in
`backtest.py`, 6 assertions over één doorlopende incident-levensloop. Gemeten:
90/BEVESTIGD → weerlegd naar 42.5/MOGELIJK zodra het toestel weer gevolgd wordt
→ nieuw lateraal bewijs komt niet verder dan 55/WAARSCHIJNLIJK → opnieuw
verdwijnen laat de dimensie weer meetellen (95/BEVESTIGD).

### 1. Wat er precies mis is

`incidents.py:423` (`_confirmed_bar_met`):
```python
        dims = self._evidence_dimensions(sources)
```
`sources` komt uit `_evidence_sources_seen`, dat alle ooit weggeschreven
evidence-rijen van dit incident leest. Die verzameling groeit alleen. Punt 5
stelt vervolgens een vraag in de tegenwoordige tijd — "zijn er nu minstens twee
wezenlijk verschillende soorten abnormaliteit" — op een verzameling die alleen
geschiedenis kent.

Het scherpste geval is `signal_lost_near_airport`. Dat is de enige bewijsbron
die uit AFWEZIGHEID van data redeneert: het toestel is van de radar verdwenen,
laag en dicht bij een andere luchthaven dan de gefilede, dús zal het daar wel
geland zijn. Zodra hetzelfde toestel weer gewoon getrackt wordt is die
gevolgtrekking niet zwakker geworden maar **weerlegd** — het is niet geland, het
zat in een dekkingsgat. `main.py:581` zet `track.missing_cycles = 0` en verder
gebeurt er niets: de incident-engine hoort er nooit van.

Er is überhaupt maar één weg terug in dit systeem
(`_check_deviation_recovered`), en die weigert te draaien zodra er ander bewijs
dan course/corridor_deviation ligt (`incidents.py:693`). Positief tegenbewijs
wordt dus nergens geconsumeerd; de enige uitweg is 0.85-verval per cyclus.

### 2. Waarom dit een probleem is

Verval en weerlegging zijn niet hetzelfde. Verval zegt "we hebben al een tijdje
niets meer gehoord". Weerlegging zegt "we hebben nu iets gehoord dat de eerdere
gevolgtrekking onmogelijk maakt". Een systeem dat alleen het eerste kent, houdt
een weerlegde gevolgtrekking gewoon in het dossier tot de klok hem heeft
uitgewist — en tot die tijd blijft zij als volwaardige dimensie meetellen voor
het hoogste zekerheidsniveau.

Dit raakt precies de dimensie waarvan het project zelf al heeft vastgesteld dat
zij het zwakst is: de UA2078-signaalverliesvariant staat in de uitkomsttabel
hierboven expliciet als "redeneert uit afwezigheid van data; een dekkingsgat is
een gewone verklaring". Die conclusie is één keer per case getrokken, maar er is
geen mechanisme dat hem uitvoert zodra het dekkingsgat zich daadwerkelijk als
dekkingsgat openbaart.

**Concreet scenario (gemeten).** Een toestel verdwijnt laag bij een
niet-bestemming (`signal_lost`, 40 punten, dimensie `vanished`) terwijl er ook
laterale afwijking ligt. Twee dimensies → BEVESTIGD, 🚨 verstuurd. Drie cycli
later wordt het toestel weer gevolgd, op 35.000 voet, koers richting bestemming:

```
  bronnen: ['corridor_deviation', 'route_corroborated', 'signal_lost']
  score 90 state BEVESTIGD
  na 3 cycli waarin het toestel WEER GETRACKT wordt:
    bronnen ['corridor_deviation', 'route_corroborated', 'signal_lost']
    score 55 state WAARSCHIJNLIJK
    signal_lost nog steeds bezwarend bewijs: JA
```

De score zakt alleen door tijdsverval; `signal_lost` staat er nog, de dimensie
`vanished` telt nog mee, en één nieuw `corridor_deviation`-hitje tilt het
incident meteen terug naar BEVESTIGD — op grond van een landing die aantoonbaar
niet heeft plaatsgevonden.

### 3. De exacte fix

Een algemeen weerleggingsmechanisme, plus één concrete weerlegger. Bewust
gescheiden, zodat latere weerleggers (zie punt 5 hieronder) mechanisch zijn.

**a. `incidents.py`, bij de dimensie-constanten:**
```python
# Bronnen die een eerder vastgestelde dimensie WEERLEGGEN in plaats van hem te
# verzwakken. Verval zegt "we hebben al een tijd niets gehoord"; weerlegging
# zegt "we hebben nu iets gehoord dat de eerdere gevolgtrekking onmogelijk
# maakt". Alleen het tweede hoort de dimensie uit de BEVESTIGD-poort te halen.
#
# Een weerlegging geldt alleen zolang er daarna geen NIEUW bewijs in diezelfde
# dimensie is binnengekomen — anders zou één herstel de dimensie permanent
# uitschakelen, precies de zelfuitschakelende-subsettest-fout die ronde 15 in
# _check_deviation_recovered heeft opgelost. Daarom wordt er op TIJDSTEMPEL
# vergeleken, niet op aanwezigheid.
_REFUTES_DIMENSION = {
    "signal_lost_refuted": DIM_VANISHED,
    "deviation_resolved": DIM_LATERAL,
}

# Bronnen die de "verdwenen, dus vermoedelijk geland"-gevolgtrekking dragen.
_VANISHED_SOURCES = {"signal_lost", "signal_lost_disputed"}
```
en `"signal_lost_refuted"` toevoegen aan `_NON_REINFORCING_SOURCES`.

**b. `incidents.py`, nieuwe methode naast `_evidence_dimensions`:**
```python
    def _active_dimensions(self, incident_id: int | None, extra_source: str | None = None) -> set:
        """De bewijs-dimensies die op DIT MOMENT nog overeind staan.

        _evidence_dimensions leest een verzameling bronnamen die alleen groeit;
        deze methode leest de rijen mét tijdstempel en laat een dimensie
        vervallen zodra de laatste weerlegging ervan NIEUWER is dan het laatste
        bezwarende bewijs erin. Zie CHECKPOINT.md bevinding 12."""
        rows = db_module.get_incident_evidence(self.db, incident_id) if incident_id is not None else []
        latest_evidence: dict[str, float] = {}
        latest_refutation: dict[str, float] = {}
        for row in rows:
            src, ts = row["source"], row["ts"]
            refuted_dim = _REFUTES_DIMENSION.get(src)
            if refuted_dim is not None:
                latest_refutation[refuted_dim] = max(latest_refutation.get(refuted_dim, ts), ts)
                continue
            if src in _NON_REINFORCING_SOURCES:
                continue
            dim = _DIMENSION_FOR_SOURCE.get(src, src)
            latest_evidence[dim] = max(latest_evidence.get(dim, ts), ts)
        if (extra_source is not None and extra_source not in _NON_REINFORCING_SOURCES
                and extra_source not in _REFUTES_DIMENSION):
            # Staat op het punt weggeschreven te worden, dus per definitie het
            # nieuwste bewijs — zelfde not-yet-persisted timing als
            # _confirmed_bar_met's current_source.
            latest_evidence[_DIMENSION_FOR_SOURCE.get(extra_source, extra_source)] = float("inf")
        return {dim for dim, ts in latest_evidence.items()
                if ts > latest_refutation.get(dim, float("-inf"))}
```

**c. `_confirmed_bar_met`: `dims = self._evidence_dimensions(sources)` vervangen
door**
```python
        dims = self._active_dimensions(incident_id, current_source)
```

**d. `incidents.py`, nieuwe check:**
```python
    def _check_signal_lost_refuted(self, inc: dict, ac: dict | None, now: float):
        """signal_lost_near_airport is de enige bewijsbron die uit AFWEZIGHEID
        van data redeneert: het toestel is van de radar, laag en dicht bij een
        andere luchthaven dan de gefilede, dus zal het daar wel geland zijn.
        Wordt hetzelfde toestel daarna gewoon weer gevolgd en is het niet aan de
        grond, dan is die gevolgtrekking niet zwakker geworden maar weerlegd:
        het is niet geland, het zat in een dekkingsgat — de meest alledaagse
        verklaring die er voor deze waarneming bestaat.

        Bewust ook geldig als het toestel laag terugkomt: weerlegd wordt de
        GEVOLGTREKKING dat het al geland is, niet de mogelijkheid dat het straks
        alsnog ergens anders landt. Gebeurt dat, dan levert detect_landed_wrong_
        airport echt grondbewijs op in plaats van een gevolgtrekking uit stilte.

        Niet eenmalig-per-incident (anders dan weer/peer-consensus): verdwijnt
        het toestel later opnieuw, dan hoort die nieuwe ronde opnieuw weerlegd
        te kunnen worden. De tijdstempelvergelijking hieronder zorgt dat er
        alleen een rij bijkomt als er sindsdien nieuw verdwijnbewijs was.

        Zie CHECKPOINT.md bevinding 12."""
        if ac is None or ac.get("on_ground"):
            return None
        rows = db_module.get_incident_evidence(self.db, inc["id"])
        last_vanished = max((r["ts"] for r in rows if r["source"] in _VANISHED_SOURCES), default=None)
        if last_vanished is None:
            return None
        last_refuted = max((r["ts"] for r in rows if r["source"] == "signal_lost_refuted"), default=None)
        if last_refuted is not None and last_refuted >= last_vanished:
            return None
        contrib = self._source_contrib(inc)
        removed = 0.0
        for src in _VANISHED_SOURCES:
            removed += contrib.get(src, 0.0)
            contrib[src] = 0.0
        return self._apply_delta(
            inc["hex"], now, -removed, "signal_lost_refuted",
            "toestel wordt weer gevolgd — de veronderstelde landing heeft niet plaatsgevonden", None,
        )
```
De bijdrage wordt op 0 gezet in plaats van geblokkeerd: verdwijnt het toestel
later opnieuw, dan mag een nieuw `signal_lost`-Event gewoon weer scoren.

**e. `step()`: de weerlegging draait elke cyclus**, direct vóór
`apply_context_checks`:
```python
        refuted = self._check_signal_lost_refuted(self._open[hex_id], ac, now)
        if refuted:
            t = self._maybe_notify(refuted[0], refuted[1], refuted[2], now)
            if t:
                transitions.append(t)
        if hex_id not in self._open:
            return transitions
```
Bewust NIET in `reassess()`: dat is precies de fout uit bevinding 5 — een
toestel dat terugkomt en meteen weer bewijs oplevert, is het geval waarin de
weerlegging het hardst nodig is en waarin `reassess` nooit draait.

### 4. Validatie

Nieuwe check `check_evidence_refutation` in `backtest.py`:
- `signal_lost` + `corridor_deviation` ×3, route gecorroboreerd, daarna cycli
  mét live `ac`-data (airborne, 35.000ft). *Vóór:* 90/BEVESTIGD, daarna
  55/WAARSCHIJNLIJK met `signal_lost` nog als bezwarend bewijs. *Na:* de
  `signal_lost_refuted`-rij staat er en de dimensie `vanished` telt niet meer
  mee.
- Vervolgassertie: een nieuw `corridor_deviation`-Event ná de weerlegging
  brengt het incident **niet** terug naar BEVESTIGD. *Vóór:* wel.
- Terugkeerassertie (geen zelfuitschakeling): na de weerlegging een nieuw
  `signal_lost`-Event → `vanished` telt weer mee, BEVESTIGD weer bereikbaar.
- Regressie: een toestel dat verdwijnt en verdwenen blijft (`ac=None`) houdt
  zijn `signal_lost`-bewijs volledig — `check_real_case_confidence_outcomes`
  moet voor de UA2078-signaalverliesvariant ongewijzigd `nee` blijven en voor
  alle andere cases ongewijzigd.

### 5. Bewust NIET in deze bevinding meegenomen

`DIM_VERTICAL` en `DIM_LOITER` zijn even goed weerlegbaar (het toestel klimt
terug naar kruishoogte; het verlaat het wachtpatroon richting bestemming), maar
daarvoor is per-bewijsrij bijgehouden hoogte/positie nodig die er nu niet is —
`inc["last_alt"]` wordt door élke volgende evidence-rij overschreven, dus daar
valt geen betrouwbare "terug naar het niveau van vóór de daling"-test op te
bouwen. Het raamwerk hierboven (`_REFUTES_DIMENSION` + tijdstempelvergelijking)
maakt het toevoegen ervan mechanisch zodra die bookkeeping er is. Bewust niet
half gedaan.

---

## Bevinding 13 — Een nooit-bevestigd incident wordt bij afsluiting alsnog "bevestigde diversie" genoemd

**Status:** gevalideerd (2026-08-08) — assertions (d) en (e) van
`check_disputed_evidence_gate`. Gemeten: een MOGELIJK-incident met onbevestigde
grondstatus sluit nu als `GESLOTEN_GELAND` met "niet bevestigd" in de reden;
onbetwist grondbewijs houdt "bevestigde diversie".

### 1. Wat er precies mis is

`incidents.py:660-664` (`_check_landed`):
```python
        if _WRONG_AIRPORT_SOURCES & self._evidence_sources_seen(inc["id"]):
            return self._resolve(
                hex_id, now, CLOSED_LANDED,
                f"geland op {nearest['icao']}, niet de verwachte bestemming — bevestigde diversie",
            )
```
`_WRONG_AIRPORT_SOURCES` bevat óók `wrong_airport_disputed` en
`wrong_airport_unconfirmed` (`incidents.py:90-91`) — precies de twee varianten
die bevinding 1 heeft gedegradeerd omdat de waarneming of de referentie betwist
is.

### 2. Waarom dit een probleem is

Het hele project draait om het verschil tussen "mogelijk", "waarschijnlijk" en
"bevestigd". Hier zet de afsluitregel dat onderscheid in één zin overboord: een
incident dat de zekerheidspoort bewust nooit heeft laten passeren, wordt in het
dossier en op het dashboard weggeschreven met het woord "bevestigde".

Dat is niet cosmetisch. `resolution_reason` is wat er van dit incident overblijft
zodra het gesloten is — het is de enige zin die een latere lezer (en een latere
tuningronde die naar historische uitkomsten kijkt) nog ziet. Een archief waarin
betwiste en onbetwiste diversies allebei "bevestigd" heten, is precies het
soort meetfout waar dit project zich verder juist tegen wapent.

**Concreet scenario (gemeten).** Eén `wrong_airport`-Event met onbevestigde
grondstatus, daarna het toestel aan de grond bij EGKK:

```
  vóór landing: score 25 state MOGELIJK
  na landing: state='GESLOTEN_GELAND'
    reden: 'geland op EGKK, niet de verwachte bestemming — bevestigde diversie'
```

MOGELIJK in, "bevestigde diversie" uit.

### 3. De exacte fix

`incidents.py`, `_check_landed`, het `_WRONG_AIRPORT_SOURCES`-blok vervangen
door:
```python
        landed_sources = _WRONG_AIRPORT_SOURCES & self._evidence_sources_seen(inc["id"])
        if landed_sources:
            # De afsluitzin is het enige dat van dit incident overblijft. Een
            # incident waarvan de grondwaarneming óf de gefilede bestemming
            # door een tweede bron betwist is, heeft de BEVESTIGD-poort bewust
            # nooit gehaald (zie _confirmed_bar_met punt 2b/4 en CHECKPOINT.md
            # bevinding 10) — dan hoort er ook geen "bevestigde" in de
            # afsluitreden te staan. GESLOTEN_GELAND blijft wel de juiste
            # state: het toestel is hier daadwerkelijk geland, alleen de
            # duiding ervan is onzeker.
            if landed_sources - _PROVISIONAL_GROUND_TRUTH_SOURCES:
                reden = f"geland op {nearest['icao']}, niet de verwachte bestemming — bevestigde diversie"
            else:
                reden = (f"geland op {nearest['icao']}, niet de verwachte bestemming — niet bevestigd "
                         f"(grondstatus of bestemming betwist door een tweede bron)")
            return self._resolve(hex_id, now, CLOSED_LANDED, reden)
```
(`_PROVISIONAL_GROUND_TRUTH_SOURCES` komt uit bevinding 10.)

### 4. Validatie

Assertie toegevoegd aan `check_disputed_evidence_gate`:
- `wrong_airport` met `confidence="WAARSCHIJNLIJK"`, daarna aan de grond bij een
  niet-bestemming. *Vóór:* `resolution_reason` bevat "bevestigde diversie".
  *Na:* bevat "niet bevestigd", state blijft `GESLOTEN_GELAND`.
- Regressie: onbetwist `wrong_airport` sluit nog steeds met "bevestigde
  diversie".

---

## Extra tijdens implementatie van sessie 2 gevonden

### D. De benigne AFTREK bleef volgorde-afhankelijk nadat de VASTLEGGING het niet meer was
Bevinding 11's fix maakte het vastleggen van de verklaring volgorde-onafhankelijk,
maar niet de aftrek: vuurde de laterale detector eerst, dan dekte de verklaring
op dat moment álles en werd er -50 afgetrokken; kwam de daling daarna, dan bleef
die aftrek staan. Vuurde de daling eerst, dan werd er nooit afgetrokken. Gemeten
ná de eerste helft van de fix: hetzelfde bewijs kwam uit op score 61 of 91 —
beide WAARSCHIJNLIJK, dus het oordeel was al gerepareerd, maar bij een derde
dimensie gaat de score weer langs de BEVESTIGD-drempel en kan dat verschil
alsnog kantelen.

Opgelost met `_release_benign_deduction` (draait elke cyclus vanuit
`apply_context_checks`): zodra het incident bewijs draagt dat de verklaring niet
dekt, wordt exact de eerder toegekende aftrek teruggegeven. Daarvoor moest de
aftrek zelf ook exact worden: `-min(50.0, inc["score"])` in plaats van een vaste
`-50.0`, want `_apply_delta` klemt de score op 0 en een vaste -50 verwijdert dan
een onbekend deel dat naderhand niet meer precies terug te draaien is. De
verklaringrij zelf blijft staan — zij is nog steeds waar voor de dimensies die
zij dekt en blijft die in punt 3 neutraliseren; alleen haar score-effect op het
incident als geheel vervalt. Gemeten na deze toevoeging: beide volgordes exact
91.0/WAARSCHIJNLIJK.

### E. `_check_landed` sloot een incident soms zonder dat `step()` dat merkte
`step()` deed:
```python
        landed_t = self._check_landed(hex_id, ac, now)
        if landed_t:
            transitions.append(landed_t)
            return transitions
```
Maar `_check_landed` sluit het incident via `_resolve()`, en die geeft alléén een
transition terug als er ook genotificeerd moet worden (`_maybe_notify`). Een
landing die stilletjes afsluit — een incident dat nooit boven MOGELIJK is
gekomen, precies het geval dat bevinding 13 aanwijst — leverde dus `None` op
terwijl het incident wél uit `self._open` verdwenen was, waarna de rest van
`step()` doorliep op een gesloten incident. Dat viel niet op zolang alles
daaronder `self._open.get()` gebruikte; de eerste directe `self._open[hex_id]`
(de weerleggingscheck van bevinding 12) sloeg er meteen op stuk met een
`KeyError`. Nu test `step()` op `hex_id not in self._open` in plaats van op de
returnwaarde.

### F. `signal_lost` mét live positie wordt onmiddellijk weerlegd — en dat hoort zo
Bij het schrijven van `check_benign_explanation_scope` assertion (e) bleek de
eerste opzet te falen: `dims` bevatte geen `vanished`. Oorzaak was de testopzet,
niet de code — de helper voedde élke stap een live `ac`, en dan weerlegt de
engine de "verdwenen, dus geland"-gevolgtrekking terecht in dezelfde cyclus.

In productie kan dat niet: `detect_signal_lost_near_airport` draait in
`main.py`'s lus over toestellen die juist NIET in de snapshot zitten, en die
`step()`-aanroep geeft `ac=None` mee (`main.py:647`). `backtest.py`'s `run_case`
spiegelt dat al expliciet. Vastgelegd door de test nu ook met `ac=None` te
voeren, met die reden erbij — en tegelijk is dit de bevestiging dat de
weerlegging niet alleen bij verval maar meteen bij het eerste levensteken
aangrijpt.

Bijkomend gevolg dat de moeite van vermelden waard is: ná een verdwijning kan er
geen nieuw lateraal/verticaal bewijs meer bijkomen, want die detectoren hebben
live posities nodig. `vanished` kan dus alleen samen met andere dimensies
bestaan als het verdwijnen het LAATSTE is wat er gebeurt — wat het risico van
bevinding 12 juist scherper maakt: dat is precies de toestand waarin het
incident blijft hangen tot het vervalt.

### G. Wat bevinding 10b in de praktijk wél en niet verandert (eerlijke afbakening)
Punt 4's kortsluiting-op-één-bewijsrij was voor de gedegradeerde varianten al
moeilijk uitbuitbaar via de SCORE alleen: `wrong_airport_unconfirmed` zit op
plafond 25 en `wrong_airport_disputed` op 30, dus in hun eentje halen ze de
BEVESTIGD-drempel (85) nooit. De structurele fix verandert daarom vooral twee
dingen, en dat is minder spectaculair dan de gemeten 116/BEVESTIGD uit de probe
suggereert (die score kwam in werkelijkheid vooral uit lateraal + verticaal, dus
uit punt 5, niet uit punt 4):

1. Een benigne verklaring (weer/peer-consensus) is niet langer uitgeschakeld
   door een betwiste grondwaarneming — punt 3 zonderde alleen `DIM_GROUND_TRUTH`
   uit, en dat is nu terecht beperkt tot onbetwist grondbewijs.
2. De poort hangt niet meer af van de vraag of de verzadigingsplafonds op hun
   huidige waarden blijven staan. Dat is precies het soort verborgen koppeling
   waar deze hele ronde over gaat: een tuningronde die `_SOURCE_SCORE_CAP`
   verhoogt, hoort daarmee niet ongemerkt een structurele poort te openen.

Het echte werk tegen betwist bewijs doet 10a (punt 2b): dat blokkeert het
disputed-scenario ongeacht hoe hoog de score komt.
