"""Incident-levenscyclus: continue scoring + state machine bovenop
detector.py's Events, in plaats van het oude "vuur eenmalig, vergeet"
model. Zie MASTERPLAN.md sectie 3 voor het volledige ontwerp.

STATUS (2026-08-06, zie BACKTEST_LOG.md ronde 10): dit bestand is af en
zelfstandig getest (zie backtest.py's check_incident_engine_regressions),
en is inmiddels — al een aantal rondes — ook daadwerkelijk aangesloten op
main.py's tier0_loop/tier1_loop, notifier.notify_incident_transition en
server.py. Zie MASTERPLAN.md sectie 11, fase 3 voor hoe die aansluiting is
uitgevoerd.

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
#
# Deze demping vertraagt een divergerende reeks alleen, ze begrenst hem
# niet: een vaste breuk van het eerste gewicht blijft bij herhaling
# onbeperkt optellen. Wat de bijdrage per bron daadwerkelijk begrenst is
# _SOURCE_SCORE_CAP, verderop in dit bestand — zie CHECKPOINT.md
# bevinding 3.
REPEATABLE_EVENT_TYPES = DEVIATION_EVENT_TYPES + ("holding_pattern", "premature_descent")

# Evidence sources that are themselves negative-only/context, never a sign
# the incident is more serious than a plain deviation — excluded from
# _check_deviation_recovered's "is this incident still just a deviation"
# test so a PRIOR recovery (or a weather/peer-consensus note) doesn't
# permanently disable recovery detection for the rest of the incident's
# life. See that method's docstring / BACKTEST_LOG.md ronde 15.
_NON_REINFORCING_SOURCES = {"deviation_resolved", "weather_explains", "peer_consensus",
                            "route_corroborated", "benign_explanation_released",
                            "signal_lost_refuted"}

# Alle bronnamen die score_for_event voor een wrong_airport-Event kan
# produceren (vol vertrouwen / betwiste routebron / onbevestigde grondstatus).
# _check_landed test hierop om een echte diversie als GESLOTEN_GELAND af te
# sluiten in plaats van hem te laten wegvervallen — zie CHECKPOINT.md
# bevinding 1.
_WRONG_AIRPORT_SOURCES = {"wrong_airport", "wrong_airport_disputed", "wrong_airport_unconfirmed",
                          "wrong_airport_early_return"}

# Bronnen waarvan de WAARNEMING zelf niet van de gefilede route afhangt, en die
# daarom als enige geen route-corroboratie nodig hebben om BEVESTIGD te halen
# (zie _confirmed_bar_met punt 1):
#
#  - emergency_squawk (7700) en unlawful_squawk (7500, na zijn persistentie-eis):
#    een bewuste 4-cijferige piloothandeling. Twee dingen moeten hier los van
#    elkaar waar zijn, en tot bevinding 15 werd alleen het eerste beargumenteerd:
#      (i)  BETROUWBAARHEID — geen datakwaliteits- of routine-operatieverklaring
#           produceert deze code, en welke bestemming er gefiled staat doet er
#           niet aan toe;
#      (ii) RELEVANTIE — de code voorspelt daadwerkelijk een UITWIJKING, want
#           dat is het enige wat dit systeem beweert als het BEVESTIGD zegt.
#    Voor 7700 (algemene noodsituatie) en 7500 (kaping) gelden ze allebei: die
#    eindigen vrijwel altijd op een ander veld dan gefiled. Voor 7600 geldt (i)
#    net zo goed maar (ii) juist NIET — de voorgeschreven lost-comms-procedure
#    is het vluchtplan naar de gefilede bestemming afvliegen. 7600 heeft daarom
#    een eigen bron (nordo_squawk) die hier bewust buiten valt. Zie CHECKPOINT.md
#    bevinding 15.
#  - wrong_airport_early_return: wij hebben dit toestel zelf zien opstijgen van
#    luchthaven X en binnen early_return_max_minutes weer zien landen op
#    diezelfde luchthaven X. Beide uiteinden zijn onze eigen waarneming, de
#    gefilede bestemming komt er niet in voor, en een toestel dat kort na
#    vertrek terugkeert is abnormaal ongeacht waar het volgens de schedule heen
#    zou gaan. Zie CHECKPOINT.md bevinding 9 voor waarom dit een eigen bron is
#    en niet (zoals eerst) via route-corroboratie liep: "wij zagen het
#    vertrekken vanaf de gefilede origin" bevestigt de VERTREKhelft van de
#    route, terwijl wrong_airport volledig over de BESTEMMING gaat — dat liet
#    precies het DLH8NK-scenario (kloppende origin, foute bestemming) weer door.
_ROUTE_INDEPENDENT_SOURCES = {"emergency_squawk", "unlawful_squawk", "wrong_airport_early_return"}

# Bronnamen die alleen ontstaan wanneer een TWEEDE routebron (hexdb.io) een
# ANDERE bestemming noemt dan de gefilede — zie main.py's enrich_events. Hun
# aanwezigheid in de evidence-tabel is de persistente vastlegging dat de
# referentiedata achter dit incident BETWIST is; daar is geen apart schemaveld
# voor nodig. Zie _confirmed_bar_met punt 2b en CHECKPOINT.md bevinding 10.
_ROUTE_DISPUTED_SOURCES = {"wrong_airport_disputed", "signal_lost_disputed",
                           "premature_descent_disputed"}

# De twee gedegradeerde wrong_airport-varianten, als bronnamen. Gebruikt door
# _check_landed om de afsluitreden eerlijk te houden (CHECKPOINT.md bevinding
# 13); _DIMENSION_FOR_SOURCE mapt ze daarnaast op DIM_GROUND_TRUTH_PROVISIONAL.
_PROVISIONAL_GROUND_TRUTH_SOURCES = {"wrong_airport_disputed", "wrong_airport_unconfirmed"}

# ---------------------------------------------------------------------------
# Bewijs-DIMENSIES (CHECKPOINT.md bevinding 4)
#
# De oude opzet telde deltas van verschillende `source`-strings zonder meer bij
# elkaar op en behandelde dat als onafhankelijke corroboratie. Dat klopt niet:
# op detect_emergency na meet ELKE detector tegen track.route["destination_*"].
# Eén foute gefilede bestemming laat corridor_deviation, premature_descent,
# holding_pattern, signal_lost_near_airport én wrong_airport tegelijk afgaan —
# dan lijken vijf detectoren het eens, terwijl het één fout is die vijf keer
# wordt waargenomen. Optellen van bewijs veronderstelt conditionele
# onafhankelijkheid; hier is er één alternatieve hypothese ("de route klopt
# niet") die alle waarnemingen tegelijk voorspelt, dus meer detectoren maken
# die hypothese niet onwaarschijnlijker.
#
# Een dimensie is de SOORT abnormaliteit. Twee evidence-rijen in dezelfde
# dimensie zijn dezelfde waarneming die zich herhaalt of vanuit een tweede hoek
# gezien wordt (course_deviation en corridor_deviation zijn letterlijk twee
# metingen van dezelfde laterale afwijking), geen onafhankelijke corroboratie.
DIM_DECLARED = "declared"          # bewuste piloot-/ATC-handeling (squawk/statusveld)
DIM_GROUND_TRUTH = "ground_truth"  # fysiek waargenomen aan de grond, ergens anders
DIM_VANISHED = "vanished"          # afgeleide landing uit signaalverlies
DIM_VERTICAL = "vertical"          # hoogteprofiel klopt niet met de bestemming
DIM_LATERAL = "lateral"            # laterale koers-/corridorafwijking
DIM_LOITER = "loiter"              # wachtpatroon

# Grondbewijs waarvan de PREMISSE van _confirmed_bar_met punt 4 niet vaststaat.
# Punt 4 zegt: "een waargenomen landing elders IS de diversie". Dat rust op twee
# dingen die allebei moeten kloppen — de grondwaarneming zelf, en de bestemming
# waar we hem tegen afmeten. Bij wrong_airport_unconfirmed is de eerste actief
# betwist (de tweede ADS-B-provider bevestigde de grondstatus niet), bij
# wrong_airport_disputed de tweede (hexdb.io noemt een andere bestemming). Ze
# houden hun al gedempte score en tellen gewoon mee als soft-dimensie voor punt
# 5, maar ze mogen punt 4's kortsluiting-op-één-bewijsrij niet omzetten — zie
# CHECKPOINT.md bevinding 10.
DIM_GROUND_TRUTH_PROVISIONAL = "ground_truth_provisional"

_DIMENSION_FOR_SOURCE = {
    "emergency_squawk": DIM_DECLARED,
    "unlawful_squawk": DIM_DECLARED,
    "nordo_squawk": DIM_DECLARED,
    "emergency_status": DIM_DECLARED,
    "emergency_status_low_trust": DIM_DECLARED,
    "wrong_airport": DIM_GROUND_TRUTH,
    "wrong_airport_disputed": DIM_GROUND_TRUTH_PROVISIONAL,
    "wrong_airport_unconfirmed": DIM_GROUND_TRUTH_PROVISIONAL,
    "wrong_airport_early_return": DIM_GROUND_TRUTH,
    "signal_lost": DIM_VANISHED,
    "signal_lost_disputed": DIM_VANISHED,
    "premature_descent": DIM_VERTICAL,
    "premature_descent_disputed": DIM_VERTICAL,
    "course_deviation": DIM_LATERAL,
    "corridor_deviation": DIM_LATERAL,
    "holding_destination": DIM_LOITER,
    "holding_non_destination": DIM_LOITER,
}
# Dimensies die "één soort abnormaliteit" vertegenwoordigen: twee daarvan
# tegelijk is het kwalitatieve verschil dat _confirmed_bar_met zoekt. DECLARED
# staat er bewust NIET in — zie punt 1/5 van _confirmed_bar_met.
# DIM_GROUND_TRUTH_PROVISIONAL staat er wél in: een betwiste grondwaarneming is
# nog steeds een ANDERE soort waarneming dan een koersafwijking, ze mag alleen
# niet in haar eentje bevestigen (CHECKPOINT.md bevinding 10).
_SOFT_DIMENSIONS = {DIM_VANISHED, DIM_VERTICAL, DIM_LATERAL, DIM_LOITER,
                    DIM_GROUND_TRUTH_PROVISIONAL}

# Dimensies waarvan de WAARNEMING volledig bestaat uit "waar bevindt dit toestel
# zich ten opzichte van een lijn die maar één crowdsourced bron beweert". Zolang
# die lijn niet onafhankelijk bevestigd is, verklaart de hypothese "de gefilede
# bestemming klopt niet" ze alle drie tegelijk en even goed — meer van dit soort
# bewijs maakt die hypothese dus niet onwaarschijnlijker, hoeveel detectoren er
# ook afgaan.
#
# Bevinding 4 heeft dat al vastgesteld, maar de conclusie alleen in de
# BEVESTIGD-POORT verwerkt (via bewijs-dimensies). De SCORE telde ze daarna nog
# gewoon bij elkaar op: corridor_deviation (plafond 55) + premature_descent
# (plafond 60) = 115. En juist de score bepaalt of er een Telegram uitgaat —
# _maybe_notify vuurt op WAARSCHIJNLIJK (55), wat corridor_deviation in zijn
# eentje al haalt. Elke eerdere ronde verdedigde het aanscherpen van de poort
# met "dat kost vrijwel geen meldingen"; aan de kant waar de meldingen vandaan
# komen was nog nooit iets aangescherpt. Zie CHECKPOINT.md bevinding 16.
#
# DIM_GROUND_TRUTH, DIM_VANISHED en DIM_DECLARED staan er bewust NIET in: die
# bevatten een waarneming met eigen inhoud (een landing, een verdwijning op een
# aanwijsbaar veld, een bewuste piloothandeling) die niet volledig wordt
# weggeschreven door de aanname dat de gefilede bestemming fout is.
_ROUTE_GEOMETRY_DIMENSIONS = {DIM_LATERAL, DIM_VERTICAL, DIM_LOITER}

# Dimensies die een actieve weers-/luchtruimverklaring of een gedeelde
# regionale oorzaak daadwerkelijk kán verklaren: eromheen vliegen en wachten.
# NIET een waargenomen landing elders (slecht weer is juist de meest
# voorkomende oorzaak van een ECHTE diversie — daar de zekerheid van verlagen
# is precies verkeerd om), niet een noodsquawk, en niet een aanhoudende daling
# ver van de bestemming. Zie CHECKPOINT.md bevinding 6.
_BENIGN_EXPLAINABLE_DIMENSIONS = {DIM_LATERAL, DIM_LOITER}
_BENIGN_EXPLANATION_SOURCES = {"weather_explains", "peer_consensus"}

# De aftrek van een benigne verklaring codeert de uitspraak "dit incident is
# waarschijnlijk gewoon weer/een gedeelde oorzaak". Komt er daarna bewijs bij
# dat die verklaring niet dekt, dan is die uitspraak over het incident als
# geheel niet meer waar en hoort de aftrek terug — anders scoort een incident
# dat er een ONVERKLAARDE abnormaliteit bij kreeg lager dan een identiek
# incident waar hetzelfde bewijs in de andere volgorde binnenkwam, precies de
# volgorde-afhankelijkheid van CHECKPOINT.md bevinding 11.
_BENIGN_RELEASE_SOURCE = "benign_explanation_released"

# ---------------------------------------------------------------------------
# Weerlegging (CHECKPOINT.md bevinding 12)
#
# Verval en weerlegging zijn niet hetzelfde. Verval zegt "we hebben al een tijd
# niets meer gehoord"; weerlegging zegt "we hebben nu iets gehoord dat de
# eerdere gevolgtrekking onmogelijk maakt". Alleen het tweede hoort een
# dimensie uit de BEVESTIGD-poort te halen. Tot deze ronde kende dit systeem
# alleen het eerste: _confirmed_bar_met las een bronnenverzameling die alleen
# groeit, en stelde daarop een vraag in de tegenwoordige tijd.
#
# Een weerlegging geldt alleen zolang er daarna geen NIEUW bewijs in diezelfde
# dimensie is binnengekomen — anders zou één herstel de dimensie permanent
# uitschakelen, precies de zelfuitschakelende-subsettest-fout die ronde 15 in
# _check_deviation_recovered heeft opgelost. Daarom vergelijkt
# _active_dimensions TIJDSTEMPELS, niet aanwezigheid.
_REFUTES_DIMENSION = {
    "signal_lost_refuted": DIM_VANISHED,
    "deviation_resolved": DIM_LATERAL,
}

# Bronnen die de "verdwenen, dus vermoedelijk geland"-gevolgtrekking dragen.
_VANISHED_SOURCES = {"signal_lost", "signal_lost_disputed"}

# ---------------------------------------------------------------------------
# Verzadigingsplafonds per bewijsbron (CHECKPOINT.md bevinding 3 en 8)
#
# Maximale TOTALE bijdrage van één bewijsbron aan één incident.
#
# Herhaling van hetzelfde signaal sluit RUIS uit (een enkele foute fix, een
# decodeglitch, één ATC-vector) maar niet de SYSTEMATISCHE onschuldige
# verklaringen (verouderde routedata, weersomleiding, ATC-vertraging, normale
# aankomstsequencing) — die voorspellen juist dat het signaal blíjft. Een score
# die met herhaling onbeperkt doorgroeit, groeit dus door in een richting waar
# het bewijs niet heen wijst.
#
# De oude REPEATABLE_EVENT_TYPES-demping (ronde 16) erkende dit maar loste het
# niet op: een vaste breuk (~1/3) vertraagt een divergerende reeks, ze
# convergeert er niet van. De enige rem was incident_score_max (150), ruim BOVEN
# de BEVESTIGD-drempel (85), zodat aanhouden alléén de top haalde:
# holding_non_destination in 6 cycli (~6 min), premature_descent in 9,
# emergency_status in 3, en zelfs het als decodeartefact gedocumenteerde
# emergency_status_low_trust in 11. Nu convergeert elke bron naar een eigen
# plafond in plaats van te divergeren.
#
# Elk plafond op een provisionele bron ligt ONDER
# incident_score_confirmed_threshold (85): volgehouden enkelvoudig bewijs komt
# uit op WAARSCHIJNLIJK — dat notificeert al (zie _maybe_notify), dus dit kost
# vrijwel geen meldingen en maakt alleen het hoogste label weer betekenisvol.
# Alleen emergency_squawk (bewuste 4-cijferige piloothandeling) en
# wrong_airport (fysiek waargenomen landing) liggen erboven; die twee worden
# apart begrensd door _confirmed_bar_met.
_SOURCE_SCORE_CAP = {
    # Een echte 7700 moet onmiddellijk én blijvend BEVESTIGD zijn.
    "emergency_squawk":            150.0,
    # 7500 na de persistentie-eis in score_for_event: dan vol vertrouwen.
    "unlawful_squawk":             150.0,
    # 7600 (radiostoring). Boven incident_score_possible_threshold (25) zodat
    # het op het dashboard staat, onder incident_score_likely_threshold (55)
    # zodat het nooit op eigen kracht notificeert: de voorgeschreven
    # lost-comms-procedure is DOORVLIEGEN naar de gefilede bestemming, dus dit
    # signaal wijst niet naar een uitwijking. Zie CHECKPOINT.md bevinding 15.
    "nordo_squawk":                 33.0,
    # WAS 55.0 — exact de WAARSCHIJNLIJK-drempel, dus zo gekozen dat het losse
    # ADS-B-statusveld in twee tier0-cycli (30s) in zijn eentje een Telegram
    # stuurde. De rechtvaardiging daarvoor was cross-provider-corroboratie, en
    # main.py's eigen conspicuity-commentaar stelt al vast dat die corroboratie
    # NIET onafhankelijk is (adsb.lol en airplanes.live delen feeders én
    # decodeconventie voor juist dit subveld). Dat argument gaat over de
    # pijplijn, niet over de squawk, dus het geldt op elke squawk. 24 zet het
    # statusveld terug op wat het is: een aanwijzing die alleen samen met ander
    # bewijs iets waard is. Zie CHECKPOINT.md bevinding 15.
    "emergency_status":             24.0,
    # Bewust nét onder de MOGELIJK-drempel (25): het gedocumenteerde
    # conspicuity-squawk-decodeartefact (AUA96J/AUA12K/CFG6UA) komt in zijn
    # eentje niet eens op het dashboard, maar draagt nog wel bij aan een
    # incident dat ander bewijs heeft.
    "emergency_status_low_trust":   24.0,
    "wrong_airport":                90.0,
    "wrong_airport_disputed":       30.0,
    "wrong_airport_unconfirmed":    25.0,
    "wrong_airport_early_return":   90.0,
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
        # De drie noodsquawks zijn operationeel niet hetzelfde signaal, en dit
        # systeem stelt één specifieke vraag: wijkt dit toestel UIT? Ze werden
        # tot nu toe als één ding behandeld (`ev.squawk in EMERGENCY_SQUAWKS` ->
        # 100 punten -> onmiddellijk BEVESTIGD). Zie CHECKPOINT.md bevinding 15.
        if ev.squawk == "7700":
            # Algemene noodsituatie: de enige van de drie die sterk met
            # uitwijken samenhangt (medisch, drukverlies, motorstoring, rook
            # eindigen vrijwel allemaal op een ander veld dan gefiled), en
            # daarmee de enige die in zijn eentje mag bevestigen.
            return 100.0, "emergency_squawk", f"noodsquawk {ev.squawk}"
        if ev.squawk == "7500":
            # Kaping. Echte gevallen zijn wereldwijd een handvol per decennium;
            # een KORTSTONDIGE 7500 is vrijwel altijd doordraaien tijdens het
            # instellen van een andere code — de bekende reden dat ATC hier
            # spurious alerts van krijgt. Daarom een persistentie-eis van één
            # cyclus (~15s op tier0) via het bestaande is_repeat_type-mechanisme:
            # de eerste waarneming blijft onder incident_score_possible_threshold
            # (25) en is dus niet eens zichtbaar. Houdt hij aan, dan telt hij vol
            # mee en mag hij bevestigen.
            return (100.0 if is_repeat_type else 20.0), "unlawful_squawk", f"noodsquawk {ev.squawk} (kaping)"
        if ev.squawk == "7600":
            # Radiostoring. ICAO Annex 2 / 14 CFR 91.185 schrijven bij verlies
            # van radioverbinding voor om het ingediende vluchtplan naar de
            # BESTEMMING af te vliegen — dit is dus geen bewijs voor een
            # uitwijking maar eerder het tegendeel. In de praktijk bovendien een
            # nuisance-reeks (N81051: 26 rijen in vier minuten). Blijft
            # zichtbaar als context, notificeert en bevestigt nooit alleen.
            return (8.0 if is_repeat_type else 25.0), "nordo_squawk", f"noodsquawk {ev.squawk} (radiostoring)"
        if ev.confidence == "WAARSCHIJNLIJK":
            # Already capped by main.py's enrich_events (conspicuity squawk
            # or cross-provider disagreement) — see providers.
            # CONSPICUITY_SQUAWKS / cross_provider_confirms_emergency.
            return 8.0, "emergency_status_low_trust", "emergency-status (laag vertrouwen — conspicuity-squawk of niet bevestigd)"
        return 20.0, "emergency_status", "emergency-status op discrete squawk"
    if et == "wrong_airport":
        # CHECKPOINT.md bevinding 1: main.py's enrich_events kende voor
        # wrong_airport al twee vetos, maar allebei zetten ze alléén
        # ev.confidence — en deze functie las ev.confidence buiten de
        # emergency-tak nergens. Beide vetos waren daardoor volledig inert:
        # een betwiste landing kreeg dezelfde 90 punten (>= de BEVESTIGD-
        # drempel van 85) als een onbetwiste, inclusief 🚨-Telegram. Ronde 24
        # schreef die observatie ("only `emergency` does") wel op voor
        # premature_descent/signal_lost, maar trok de conclusie niet door naar
        # wrong_airport's eigen, al bestaande degradatie.
        if ev.early_return:
            # Zelf waargenomen opstijgen van X en binnen
            # early_return_max_minutes weer landen op X — zie
            # _ROUTE_INDEPENDENT_SOURCES en detector.Event.early_return. Vol
            # gewicht, en de enige wrong_airport-variant die zonder
            # bestemmingscorroboratie mag bevestigen, omdat de waarneming de
            # gefilede bestemming helemaal niet gebruikt. Vóór de
            # route_source_disputed-tak: hexdb.io's mening over de BESTEMMING
            # is niet relevant voor een gebeurtenis die zich volledig op het
            # VERTREKveld afspeelt.
            return 90.0, "wrong_airport_early_return", "kort na vertrek teruggekeerd naar het zelf waargenomen vertrekveld"
        if ev.route_source_disputed:
            # Tweede routebron (hexdb.io) noemt een ANDERE bestemming: dan is
            # niet de landing verdacht maar de referentie waartegen we hem
            # afmeten. ~1/3 van het normale gewicht, dezelfde dempingsratio
            # als signal_lost_disputed/premature_descent_disputed.
            return 30.0, "wrong_airport_disputed", "geland op onverwachte luchthaven (tweede routebron betwist de bestemming)"
        if ev.confidence != "BEVESTIGD":
            # De tweede ADS-B-provider bevestigde de grondstatus niet: hier is
            # de WAARNEMING zelf betwist (staat dit toestel hier echt aan de
            # grond), niet de referentiedata. Zwaarder gedempt dan een
            # betwiste route — zonder betrouwbare grondwaarneming is er
            # helemaal geen "geland"-feit meer om te interpreteren.
            return 25.0, "wrong_airport_unconfirmed", "geland op onverwachte luchthaven (grondstatus niet bevestigd door tweede bron)"
        return 90.0, "wrong_airport", "geland op onverwachte luchthaven"
    if et == "signal_lost_near_airport":
        if ev.route_source_disputed:
            # main.py's enrich_events found a second, independent route
            # source (hexdb.io) naming a DIFFERENT destination than adsbdb's
            # filed one for this callsign — see the comment there
            # (BACKTEST_LOG.md ronde 24) for the live-data justification.
            # ~1/3 of the normal weight, the same damping ratio already
            # established for REPEATABLE_EVENT_TYPES's repeat hits (25->8,
            # 35->12, 30->10) — a lone disputed hit alone then stays below
            # the dashboard-visibility threshold (25), while genuine
            # corroboration from any OTHER detector still surfaces it.
            return 13.0, "signal_lost_disputed", "signaal verloren nabij niet-bestemming, laag (tweede routebron betwist de bestemming)"
        return 40.0, "signal_lost", "signaal verloren nabij niet-bestemming, laag"
    if et == "holding_pattern":
        if ev.at_destination:
            return (8.0 if is_repeat_type else 25.0), "holding_destination", "ongewoon lang wachtpatroon bij bestemming"
        return (12.0 if is_repeat_type else 35.0), "holding_non_destination", "wachtpatroon bij niet-bestemming"
    if et in DEVIATION_EVENT_TYPES:
        return (10.0 if is_repeat_type else 30.0), et, f"{et} (koers wijst niet meer naar bestemming)"
    if et == "premature_descent":
        if ev.route_source_disputed:
            # Same route_source_disputed reasoning as signal_lost_near_
            # airport above — see that branch's comment and main.py's
            # enrich_events. Repeat dampening still applies on top (a
            # sustained disputed descent shouldn't climb unboundedly either,
            # same class of bug BACKTEST_LOG.md ronde 16 fixed for the
            # non-disputed case) — ~1/3 of the disputed first-hit weight,
            # same ratio as the non-disputed repeat tier (25->8).
            return (3.0 if is_repeat_type else 8.0), "premature_descent_disputed", "vroegtijdige daling (tweede routebron betwist de bestemming)"
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

    def _evidence_dimensions(self, sources: set) -> set:
        """De verzameling bewijs-DIMENSIES achter een verzameling
        bronnamen — zie _DIMENSION_FOR_SOURCE. Niet-bezwarende bronnen
        (herstel, weer, peer-consensus, route-corroboratie) tellen niet mee."""
        reinforcing = sources - _NON_REINFORCING_SOURCES
        return {_DIMENSION_FOR_SOURCE.get(s, s) for s in reinforcing}

    def _active_dimensions(self, incident_id: int | None, extra_source: str | None = None) -> set:
        """De bewijs-dimensies die op DIT MOMENT nog overeind staan.

        _evidence_dimensions leest een verzameling bronnamen die alleen groeit;
        deze methode leest de evidence-rijen mét tijdstempel en laat een
        dimensie vervallen zodra de laatste WEERLEGGING ervan nieuwer is dan het
        laatste bezwarende bewijs erin. Komt er daarna opnieuw bewijs in die
        dimensie binnen, dan telt zij vanzelf weer mee — geen enkele weerlegging
        schakelt een dimensie permanent uit. Zie CHECKPOINT.md bevinding 12.

        extra_source: een bron die op het punt staat weggeschreven te worden
        maar nog niet persistent is — zelfde not-yet-persisted timing als
        _confirmed_bar_met's current_source, en per definitie het nieuwste
        bewijs."""
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
            latest_evidence[_DIMENSION_FOR_SOURCE.get(extra_source, extra_source)] = float("inf")
        return {dim for dim, ts in latest_evidence.items()
                if ts > latest_refutation.get(dim, float("-inf"))}

    def _benign_explanation_scope(self, incident_id: int) -> tuple[bool, bool]:
        """(mag er een benigne-verklaringrij komen, dekt zij ALLES).

        Twee losse vragen die vroeger één subset-test waren
        (_evidence_within_dimensions) — en dat maakte de uitkomst afhankelijk
        van de VOLGORDE waarin de detectoren toevallig vuurden, zie
        CHECKPOINT.md bevinding 11: de test werd op één moment geëvalueerd en
        het resultaat was daarna permanent, want beide checks zijn eenmalig per
        incident. Vuurde de laterale detector eerst, dan slaagde de subset-test
        en werd de weersverklaring vastgelegd; vuurde de daling eerst, dan
        faalde hij en werd hij daarna nooit meer waar, omdat de bewijsset
        alleen groeit. Identiek bewijs in dezelfde SIGMET-polygoon kwam zo uit
        op WAARSCHIJNLIJK of op BEVESTIGD, puur op volgorde.

          - VASTLEGGEN mag zodra dit incident ÉÉN bewijsrij heeft die de
            verklaring dekt (lateraal/wachtpatroon). Dat er een actieve
            hazard-polygoon op deze positie ligt is dan een feit over dit
            incident, ongeacht wat er verder nog aan bewijs ligt — en dat feit
            hoort niet te verdwijnen doordat er later een dimensie bijkomt die
            de verklaring niet dekt.
          - AFTREKKEN mag alleen als de verklaring ALLES dekt wat het incident
            draagt. Dekt zij maar een deel, dan houdt de rest zijn score
            (bevinding 6: weer verklaart geen landing elders — dat is juist de
            meest voorkomende oorzaak van een ECHTE diversie) en doet de rij
            alleen mee als poortvoorwaarde in _confirmed_bar_met punt 3.
        """
        dims = self._active_dimensions(incident_id)
        if not (dims & _BENIGN_EXPLAINABLE_DIMENSIONS):
            return False, False
        return True, dims <= _BENIGN_EXPLAINABLE_DIMENSIONS

    def _confirmed_bar_met(self, incident_id: int | None, current_source: str | None = None,
                            route_corroborated_now: bool = False) -> bool:
        """Of dit incident BEVESTIGD MAG heten, los van of de score de drempel
        haalt. BEVESTIGD betekent in dit systeem: er blijft geen redelijke
        alternatieve verklaring meer over voor wat we waarnemen. Dat is een
        uitspraak over de STRUCTUUR van het bewijs, niet over de hoogte van
        een som — de som weet immers niet waar hij vandaan komt.

        De alternatieve verklaringen die daadwerkelijk uitgesloten moeten
        worden, en wat elk ervan uitsluit:

          - sensorruis / decodeartefact          -> herhaling (zit al in de
            score, en convergeert nu netjes via _SOURCE_SCORE_CAP);
          - de gefilede route is verouderd/fout  -> route-corroboratie
            (punt 2) EN geen actieve tegenspraak van een tweede routebron
            (punt 2b). Dit is bij dit systeem niet theoretisch maar
            de GEMETEN hoofdfoutbron: 24/25 live wrong_airport/BEVESTIGD-hits
            zonder corroboratie (ronde 18) en 6/8 hexdb-lookups die een andere
            bestemming noemden (ronde 24);
          - weers-/luchtruimomleiding, ATC-vertraging, normale
            aankomstsequencing -> bewijs in meer dan één dimensie (punt 5) en
            geen actieve benigne verklaring (punt 3).

        current_source: de bron die op het punt staat weggeschreven te worden
        maar nog niet persistent is — zelfde not-yet-persisted timing als
        is_repeat's lookup in apply_events."""
        sources = self._evidence_sources_seen(incident_id) if incident_id is not None else set()
        if current_source is not None:
            sources = sources | {current_source}
        reinforcing = sources - _NON_REINFORCING_SOURCES
        if not reinforcing:
            return False

        # 1. Bewijs waarvan de waarneming zelf niet van de gefilede route
        #    afhangt (noodsquawk, of een zelf waargenomen vroege terugkeer
        #    naar hetzelfde vertrekveld) hoeft door niets anders gesteund te
        #    worden — zie _ROUTE_INDEPENDENT_SOURCES. Bewust getest op
        #    BRONNAAM en niet op DIM_DECLARED: het losse ADS-B emergency-
        #    STATUSVELD (emergency_status*) is live onbetrouwbaar gebleken
        #    (MASTERPLAN sectie 1c/7) en mag deze poort niet in zijn eentje
        #    openen — precies het onderscheid dat dit project sinds ronde 7
        #    verdedigt.
        if reinforcing & _ROUTE_INDEPENDENT_SOURCES:
            return True

        # 2. Al het overige bewijs is een gevolgtrekking TEN OPZICHTE VAN de
        #    gefilede route. Zolang die route alleen op één crowdsourced
        #    schedulebron rust, is "de route klopt niet" niet uitgesloten — en
        #    die ene hypothese verklaart in één klap élk route-afhankelijk
        #    signaal tegelijk, dus meer detectoren maken haar niet
        #    onwaarschijnlijker. Zie CHECKPOINT.md bevinding 2/4.
        if not (route_corroborated_now or "route_corroborated" in sources):
            return False

        # 2b. Corroboratie en tegenspraak sluiten elkaar niet uit: de
        #     corroboratie kan uit onze EIGEN waargenomen voortgang komen
        #     (main.py's route_corroborated_by_progress) terwijl hexdb.io
        #     tegelijk een ANDERE bestemming noemt, en detector.py stempelt
        #     track.route_corroborated vervolgens op élk Event — ook op het
        #     betwiste. Punt 2 was een kale OR en las dat als "de route staat
        #     vast". Bij onenigheid tussen twee referentiebronnen is de
        #     eerlijke toestand onbeslist, en onbeslist is precies de
        #     hypothese die élk route-afhankelijk signaal tegelijk verklaart.
        #     Dit is nadrukkelijk niet "hexdb wint" — ronde 24 mat dat hexdb
        #     even goed jaren oude data kan hebben (UAL1601) — maar "bij
        #     onenigheid geen BEVESTIGD". Zie CHECKPOINT.md bevinding 10.
        if sources & _ROUTE_DISPUTED_SOURCES:
            return False

        # Bewust _active_dimensions en niet _evidence_dimensions: punten 4 en 5
        # stellen een vraag in de tegenwoordige tijd ("wat voor soorten
        # abnormaliteit zien we NU"), dus mogen ze niet lezen uit een
        # verzameling die alleen geschiedenis kent. Zie bevinding 12.
        dims = self._active_dimensions(incident_id, current_source)

        # 3. Een benigne verklaring (actief SIGMET/CWA/TFR op deze positie, of
        #    meerdere toestellen die tegelijk hetzelfde doen) verloopt niet en
        #    verzwakt niet door herhaling: zolang zij geldt, blijft zij een
        #    redelijk alternatief, hoe lang het gedrag ook aanhoudt. Zij
        #    NEUTRALISEERT daarom de dimensies die zij daadwerkelijk verklaart
        #    — eromheen vliegen en wachten — en wat daarna overblijft moet de
        #    poort op eigen kracht halen.
        #
        #    Was een harde blokkade op het hele incident. Dat was tegelijk te
        #    grof en, door bevinding 11, te vaak afwezig: het blokkeerde ook
        #    bewijs waar weer niets mee te maken heeft (laag verdwijnen bij een
        #    andere luchthaven terwijl je daalt), terwijl het in het scenario
        #    waar het om ging vaak helemaal niet werd vastgelegd. De aftrek per
        #    dimensie doet allebei preciezer. Grondbewijs valt hier sowieso
        #    buiten: een landing elders wordt door weersvermijding niet
        #    verklaard (bevinding 6).
        if sources & _BENIGN_EXPLANATION_SOURCES:
            dims = dims - _BENIGN_EXPLAINABLE_DIMENSIONS

        # 4. Een waargenomen landing op een andere dan de (nu gecorroboreerde)
        #    gefilede bestemming is fysiek grondbewijs — dat ís de diversie.
        if DIM_GROUND_TRUTH in dims:
            return True

        # 5. Anders: minstens twee wezenlijk verschillende soorten
        #    abnormaliteit. Eén soort, hoe lang ook volgehouden, blijft
        #    verenigbaar met een gewone operationele verklaring — herhaling
        #    sluit ruis uit, geen systematische oorzaak.
        return len(dims & _SOFT_DIMENSIONS) >= 2

    def _resolve_state(self, incident_id: int | None, score: float, current_source: str | None = None,
                        route_corroborated_now: bool = False) -> str:
        """_state_for_score's raw threshold state, capped at WAARSCHIJNLIJK
        when it would otherwise read BEVESTIGD but _confirmed_bar_met says
        the evidence behind that score doesn't actually earn it yet. Score/
        decay/notification bookkeeping elsewhere is untouched — only what
        state gets exposed and notified changes."""
        state = _state_for_score(score, self.cfg)
        if state == CONFIRMED and not self._confirmed_bar_met(incident_id, current_source, route_corroborated_now):
            return LIKELY
        return state

    def _source_contrib(self, inc: dict) -> dict:
        """Per-bron lopende bijdrage aan de HUIDIGE score van dit incident
        (CHECKPOINT.md bevinding 3). Invariant: de score die aan bron S toe te
        schrijven is, is nooit groter dan _SOURCE_SCORE_CAP[S].

        Bij het herladen van een incident uit de DB (procesherstart) wordt dit
        conservatief gereconstrueerd als min(cap, som van alle positieve
        deltas van die bron) — dat kan de resterende ruimte onderschatten als
        er sinds die deltas verval is opgetreden, wat de veilige kant is."""
        contrib = inc.get("source_contrib")
        if contrib is None:
            contrib = {}
            for row in db_module.get_incident_evidence(self.db, inc["id"]):
                if row["delta"] and row["delta"] > 0:
                    contrib[row["source"]] = contrib.get(row["source"], 0.0) + row["delta"]
            for src in contrib:
                contrib[src] = min(contrib[src], _SOURCE_SCORE_CAP.get(src, _DEFAULT_SOURCE_SCORE_CAP))
            inc["source_contrib"] = contrib
        return contrib

    def _record_route_corroboration(self, inc: dict, now: float, route_corroborated: bool):
        """Legt eenmalig vast dat de gefilede route achter dit incident
        onafhankelijk bevestigd is. Delta 0.0 en opgenomen in
        _NON_REINFORCING_SOURCES: het is context die de BEVESTIGD-poort
        ontgrendelt, geen bezwarend bewijs dat de score verhoogt."""
        if not route_corroborated:
            return
        if "route_corroborated" in self._evidence_sources_seen(inc["id"]):
            return
        db_module.add_incident_evidence(
            self.db, inc["id"], now, "route_corroborated", 0.0,
            "route onafhankelijk bevestigd (eigen waargenomen vertrek/voortgang of tweede routebron)", None,
        )

    def _unverified_route_geometry_cap(self) -> float:
        """Maximale GEZAMENLIJKE bijdrage van alle route-meetkundige dimensies
        zolang de gefilede route niet onafhankelijk bevestigd is: net onder de
        notificatiedrempel. Zie _ROUTE_GEOMETRY_DIMENSIONS en CHECKPOINT.md
        bevinding 16.

        Bewust één GEDEELD plafond en niet per bron: het punt is juist dat die
        bronnen onder de openstaande alternatieve hypothese ("de gefilede
        bestemming klopt niet") één en dezelfde waarneming zijn. Per bron
        plafonneren zou ze opnieuw als onafhankelijk behandelen."""
        return self.cfg["incident_score_likely_threshold"] - 1.0

    def _cap_delta(self, inc: dict | None, delta: float, source: str,
                    route_verified: bool = False) -> float:
        """Kapt `delta` af op de resterende ruimte onder deze bron z'n
        verzadigingsplafond, en boekt de toegekende bijdrage bij. Een
        afgekapte delta van 0.0 levert nog steeds een evidence-rij op — de
        tijdlijn moet blijven laten zien DAT het signaal aanhoudt, ook als het
        de zekerheid niet meer verhoogt.

        route_verified: of de gefilede route achter dit incident onafhankelijk
        bevestigd is. Zo niet, dan geldt er bovenop het per-bron plafond een
        tweede, GEDEELD plafond voor alle route-meetkundige dimensies samen —
        die zijn dan immers niet meer dan verschillende metingen van dezelfde
        onbevestigde aanname."""
        if delta <= 0:
            return delta
        cap = _SOURCE_SCORE_CAP.get(source, _DEFAULT_SOURCE_SCORE_CAP)
        contrib = self._source_contrib(inc) if inc is not None else {}
        granted = min(delta, max(0.0, cap - contrib.get(source, 0.0)))
        if not route_verified and _DIMENSION_FOR_SOURCE.get(source) in _ROUTE_GEOMETRY_DIMENSIONS:
            used = sum(v for s, v in contrib.items()
                       if _DIMENSION_FOR_SOURCE.get(s) in _ROUTE_GEOMETRY_DIMENSIONS)
            granted = min(granted, max(0.0, self._unverified_route_geometry_cap() - used))
        contrib[source] = contrib.get(source, 0.0) + granted
        return granted

    def _apply_delta(self, hex_id: str, now: float, delta: float, source: str, description: str,
                      detector_confidence: str | None, callsign: str = "", aircraft_class: str | None = None,
                      origin_icao: str | None = None, dest_icao: str | None = None,
                      lat: float | None = None, lon: float | None = None, alt: float | None = None,
                      squawk: str | None = None,
                      route_corroborated: bool = False) -> tuple[dict, str | None, str]:
        """Core mutation: find-or-create the open incident for hex_id, add
        one evidence row, recompute score/state, persist, return
        (incident, old_state_or_None, new_state) — old_state is None for a
        brand-new incident.

        route_corroborated: of de gefilede route achter dit bewijs
        onafhankelijk bevestigd is (zie detector.Event.route_corroborated).
        Wordt eenmalig als evidence-rij met delta 0.0 vastgelegd, zodat de
        vaststelling persistent en zichtbaar in de tijdlijn is zonder
        schemawijziging, en meegewogen in _confirmed_bar_met."""
        inc = self._open.get(hex_id)
        old_state = None
        # Dezelfde bron van waarheid als _confirmed_bar_met punt 2 gebruikt (de
        # vlag van dit Event, OF een eerder vastgelegde route_corroborated-rij),
        # zodat de score en de poort niet uit elkaar kunnen lopen. Zie
        # CHECKPOINT.md bevinding 16.
        route_verified = bool(route_corroborated) or (
            inc is not None and "route_corroborated" in self._evidence_sources_seen(inc["id"])
        )
        if inc is None:
            delta = self._cap_delta(None, delta, source, route_verified)
            initial_score = max(0.0, min(self.cfg.get("incident_score_max", 150), delta))
            state = self._resolve_state(None, initial_score, current_source=source,
                                        route_corroborated_now=route_corroborated)
            incident_id = db_module.create_incident(self.db, hex_id, callsign, state, initial_score, now, aircraft_class)
            inc = {
                "id": incident_id, "hex": hex_id, "callsign": callsign, "state": state,
                "score": initial_score, "peak_score": initial_score, "opened_ts": now,
                "last_evidence_ts": now, "resolved_ts": None, "resolution_reason": None,
                "origin_icao": None, "dest_icao": None, "last_lat": None, "last_lon": None,
                "last_alt": None, "last_squawk": None, "aircraft_class": aircraft_class,
                "notified_state": None,
                "source_contrib": {source: max(0.0, delta)},
            }
            self._open[hex_id] = inc
            self._record_route_corroboration(inc, now, route_corroborated)
        else:
            old_state = inc["state"]
            self._record_route_corroboration(inc, now, route_corroborated)
            delta = self._cap_delta(inc, delta, source, route_verified)
            inc["score"] = max(0.0, min(self.cfg.get("incident_score_max", 150), inc["score"] + delta))
            inc["peak_score"] = max(inc["peak_score"], inc["score"])
            inc["last_evidence_ts"] = now
            inc["state"] = self._resolve_state(inc["id"], inc["score"], current_source=source,
                                               route_corroborated_now=route_corroborated)

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
                route_corroborated=getattr(ev, "route_corroborated", False),
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
        landed_sources = _WRONG_AIRPORT_SOURCES & self._evidence_sources_seen(inc["id"])
        if landed_sources:
            # De afsluitzin is het enige dat van dit incident overblijft zodra
            # het gesloten is — voor een latere lezer én voor een latere
            # tuningronde die naar historische uitkomsten kijkt. Een incident
            # waarvan de grondwaarneming óf de gefilede bestemming door een
            # tweede bron betwist is, heeft de BEVESTIGD-poort bewust nooit
            # gehaald (zie _confirmed_bar_met punt 2b/4); dan hoort er ook geen
            # "bevestigde" in de afsluitreden te staan. GESLOTEN_GELAND blijft
            # wel de juiste state: het toestel is hier daadwerkelijk geland,
            # alleen de duiding ervan is onzeker. Zie CHECKPOINT.md bevinding 13.
            if landed_sources - _PROVISIONAL_GROUND_TRUTH_SOURCES:
                reden = f"geland op {nearest['icao']}, niet de verwachte bestemming — bevestigde diversie"
            else:
                reden = (f"geland op {nearest['icao']}, niet de verwachte bestemming — niet bevestigd "
                         f"(grondstatus of bestemming betwist door een tweede bron)")
            return self._resolve(hex_id, now, CLOSED_LANDED, reden)
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
        point re-discounting every cycle once already noted.

        Scoped to _BENIGN_EXPLAINABLE_DIMENSIONS as of CHECKPOINT.md
        bevinding 6. It used to apply to ANY incident whose last position
        happened to fall inside a hazard polygon, regardless of what
        evidence that incident carried. That is wrong in the one direction
        that matters most: an incident carrying wrong_airport evidence (the
        aircraft is physically on the ground at another airport) got -50 and
        dropped from BEVESTIGD to MOGELIJK — but bad weather does not
        explain landing somewhere else, it is the single most common cause
        of a REAL diversion. The check was lowering confidence in exactly
        the case it should have raised it. What weather-avoidance does
        explain is flying around something and waiting, i.e. the lateral and
        loiter dimensions — nothing else."""
        if not hazards or inc.get("last_lat") is None:
            return None
        if "weather_explains" in self._evidence_sources_seen(inc["id"]):
            return None
        applies, covers_all = self._benign_explanation_scope(inc["id"])
        if not applies:
            return None
        hazard = explain_position(inc["last_lat"], inc["last_lon"], inc.get("last_alt"), hazards)
        if not hazard:
            return None
        # Dekt de verklaring niet alles wat dit incident draagt, dan wordt zij
        # wél vastgelegd (delta 0.0) maar trekt zij niets af — zie
        # _benign_explanation_scope. De rij telt dan alleen mee als
        # poortvoorwaarde in _confirmed_bar_met punt 3.
        # -min(50, score) in plaats van een vaste -50: _apply_delta klemt de
        # score op 0, dus een vaste -50 verwijdert een onbekend deel ervan en
        # is daarna niet meer precies terug te draaien. Door de aftrek nooit
        # groter te maken dan wat er staat, is de weggeschreven delta exact wat
        # er is weggehaald — en dat is wat _release_benign_deduction eventueel
        # weer teruggeeft.
        return self._apply_delta(
            inc["hex"], now, -min(50.0, inc["score"]) if covers_all else 0.0, "weather_explains",
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
        """Scoped to _BENIGN_EXPLAINABLE_DIMENSIONS as of CHECKPOINT.md
        bevinding 6, for a sharper version of the same problem as
        _check_weather_explains. The premise — "several aircraft are doing
        the same thing here at the same time, so it's a shared cause rather
        than an individual diversion" — holds for enroute DEVIATIONS. For
        LANDINGS it is exactly inverted: an airport closing and ten aircraft
        each diverting to their alternate is ten real diversions, the most
        newsworthy thing this system can see, and the unscoped rule
        suppressed every one of them (each is inside the same grid cell, so
        each one's peers are the other nine)."""
        if "peer_consensus" in self._evidence_sources_seen(inc["id"]):
            return None
        applies, covers_all = self._benign_explanation_scope(inc["id"])
        if not applies:
            return None
        # -1: peer count already excludes this incident itself, so N-1
        # others in the same cell means N total including this one.
        if self._peer_consensus_count(inc) < self.cfg["peer_consensus_min_aircraft"] - 1:
            return None
        # Zelfde -min(cap, score)-reden als in _check_weather_explains.
        return self._apply_delta(
            inc["hex"], now, -min(55.0, inc["score"]) if covers_all else 0.0, "peer_consensus",
            "meerdere andere toestellen wijken tegelijk op vergelijkbare wijze uit in dit gebied — mogelijk gedeelde oorzaak, geen individuele diversie", None,
        )

    def _check_signal_lost_refuted(self, inc: dict, ac: dict | None, now: float):
        """signal_lost_near_airport is de enige bewijsbron die uit AFWEZIGHEID
        van data redeneert: het toestel is van de radar, laag en dicht bij een
        andere luchthaven dan de gefilede, dús zal het daar wel geland zijn.
        Wordt hetzelfde toestel daarna gewoon weer gevolgd en is het niet aan de
        grond, dan is die gevolgtrekking niet zwakker geworden maar WEERLEGD:
        het is niet geland, het zat in een dekkingsgat — de meest alledaagse
        verklaring die er voor deze waarneming bestaat, en precies de reden dat
        deze bron in de uitkomsttabel van CHECKPOINT.md al als de zwakste
        stond.

        Bewust ook geldig als het toestel laag terugkomt: weerlegd wordt de
        GEVOLGTREKKING dat het al geland is, niet de mogelijkheid dat het straks
        alsnog ergens anders landt. Gebeurt dat, dan levert
        detect_landed_wrong_airport echt grondbewijs op in plaats van een
        gevolgtrekking uit stilte — een sterker signaal, geen zwakker.

        Niet eenmalig-per-incident (anders dan weer/peer-consensus): verdwijnt
        het toestel later opnieuw, dan hoort die nieuwe ronde opnieuw weerlegd
        te kunnen worden. De tijdstempelvergelijking hieronder zorgt dat er
        alleen een rij bijkomt als er sinds de vorige weerlegging nieuw
        verdwijnbewijs is geweest.

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
        # De bijdrage wordt op 0 gezet in plaats van geblokkeerd: verdwijnt het
        # toestel later opnieuw, dan mag een nieuw signal_lost-Event gewoon weer
        # scoren onder zijn eigen verzadigingsplafond.
        contrib = self._source_contrib(inc)
        removed = 0.0
        for src in _VANISHED_SOURCES:
            removed += contrib.get(src, 0.0)
            contrib[src] = 0.0
        return self._apply_delta(
            inc["hex"], now, -removed, "signal_lost_refuted",
            "toestel wordt weer gevolgd — de veronderstelde landing heeft niet plaatsgevonden", None,
        )

    def _release_benign_deduction(self, inc: dict, now: float):
        """Geeft een eerder toegekende benigne aftrek terug zodra dit incident
        bewijs is gaan dragen dat die verklaring niet dekt.

        Zonder dit blijft er een rest-volgorde-afhankelijkheid over na
        CHECKPOINT.md bevinding 11: de VASTLEGGING van de verklaring is nu
        volgorde-onafhankelijk, maar de AFTREK niet. Vuurt de laterale detector
        eerst, dan dekt de verklaring op dat moment alles en wordt er
        afgetrokken; komt daarna een daling binnen, dan blijft die aftrek
        staan. Vuurt de daling eerst, dan wordt er nooit afgetrokken. Zelfde
        bewijs, zelfde weer, verschil in score (gemeten: 61 vs 91) — en dat
        verschil kan bij drie dimensies alsnog het oordeel kantelen, omdat de
        score dan weer langs de BEVESTIGD-drempel gaat.

        Teruggegeven wordt exact wat er is afgetrokken (de negatieve deltas van
        de benigne bronnen, die dankzij het -min(cap, score)-patroon precies de
        werkelijke verwijdering zijn), verminderd met wat er al eerder is
        teruggegeven. De verklaringrij zelf blijft staan: zij is nog steeds
        waar voor de dimensies die zij dekt, en blijft die in
        _confirmed_bar_met punt 3 neutraliseren. Alleen haar score-effect op
        het incident als geheel vervalt."""
        rows = db_module.get_incident_evidence(self.db, inc["id"])
        deducted = -sum(r["delta"] for r in rows
                        if r["source"] in _BENIGN_EXPLANATION_SOURCES and r["delta"] and r["delta"] < 0)
        if deducted <= 0:
            return None
        released = sum(r["delta"] for r in rows if r["source"] == _BENIGN_RELEASE_SOURCE and r["delta"])
        outstanding = deducted - released
        if outstanding <= 0:
            return None
        _applies, covers_all = self._benign_explanation_scope(inc["id"])
        if covers_all:
            return None
        return self._apply_delta(
            inc["hex"], now, outstanding, _BENIGN_RELEASE_SOURCE,
            "eerdere benigne verklaring dekt niet meer al het bewijs — aftrek teruggedraaid", None,
        )

    def apply_context_checks(self, hex_id: str, now: float, hazards: list | None = None) -> list[dict]:
        """Benigne-verklaringcontext (weer/luchtruim, peer-consensus) voor één
        open incident. Draait ELKE cyclus, ook cycli mét vers bewijs — zie
        step() en CHECKPOINT.md bevinding 5.

        Stond vroeger in reassess(), die alleen draait als er GEEN vers bewijs
        was. Daardoor werden deze checks structureel overgeslagen in precies
        het scenario waarvoor ze bestaan: een detector die elke cyclus opnieuw
        vuurt (een aanhoudende uitwijking om weer heen, een lange hold) is
        juist het geval waarin een incident doorklimt, en dat is ook het geval
        waarin `events` nooit leeg is. main.py haalde de SIGMET-polygonen elke
        cyclus keurig op en gaf ze door, waarna ze nooit werden geraadpleegd.
        Beide checks zijn intern al eenmalig-per-incident, dus vaker aanroepen
        levert geen herhaalde aftrek op."""
        transitions = []
        for enabled, check in (
            (self.cfg.get("weather_sigmet_enabled"), lambda i: self._check_weather_explains(i, hazards, now)),
            (self.cfg.get("peer_consensus_enabled"), lambda i: self._check_peer_consensus(i, now)),
            # Draait ná beide checks en altijd (los van welke vlag de aftrek
            # ooit heeft opgeleverd): een aftrek die niet meer klopt hoort ook
            # teruggedraaid te worden als de bijbehorende bron intussen is
            # uitgezet. Zie _release_benign_deduction.
            (True, lambda i: self._release_benign_deduction(i, now)),
        ):
            if not enabled:
                continue
            inc = self._open.get(hex_id)
            if inc is None:
                break
            result = check(inc)
            if result:
                t = self._maybe_notify(result[0], result[1], result[2], now)
                if t:
                    transitions.append(t)
        return transitions

    def reassess(self, hex_id: str, ac: dict | None, now: float, hazards: list | None = None) -> dict | None:
        """Decay + recovery-detection + auto-close for one open incident that
        did NOT receive fresh evidence this cycle (see step()) —
        MASTERPLAN.md sectie 3.5/6. The weather/peer-consensus context checks
        moved to apply_context_checks(), which runs every cycle instead
        (CHECKPOINT.md bevinding 5); recovery-detection stays here, since
        "the deviation resolved" is only meaningful in a cycle that brought
        no fresh evidence of it continuing.

        hazards is still accepted (and unused here) so main.py's and
        backtest.py's call signatures don't have to distinguish."""
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

        decay = self.cfg["incident_score_decay_factor_per_cycle"]
        old_score = inc["score"]
        inc["score"] = inc["score"] * decay
        # De per-bron bijdrage vervalt mee (CHECKPOINT.md bevinding 3). Anders
        # zou een bron die zijn verzadigingsplafond raakte en daarna verviel
        # nooit meer kunnen bijdragen, terwijl het onderliggende signaal
        # gewoon doorloopt — het plafond hoort de score te begrenzen die op
        # enig MOMENT aan die bron toe te schrijven is, niet het totaal dat
        # hij ooit heeft opgeleverd.
        contrib = self._source_contrib(inc)
        for src in contrib:
            contrib[src] *= decay
        if inc["score"] == old_score:
            return None
        old_state = inc["state"]
        inc["state"] = self._resolve_state(inc["id"], inc["score"])
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
        resolution, then the benign-explanation context checks (every
        cycle), and finally — only when there was NO fresh evidence this
        cycle, so a score bump isn't immediately partially undone by the
        same cycle's decay — recovery-detection/decay/auto-close. Returns a
        list of transition dicts to dispatch as notifications. hazards: this
        cycle's pre-fetched airspace.get_active_hazards() result, or None to
        skip the weather check — see apply_context_checks().

        The context checks deliberately sit OUTSIDE the `if not events`
        branch (CHECKPOINT.md bevinding 5): a detector that re-fires every
        cycle is exactly the situation in which an incident climbs, and it
        was also exactly the situation in which those checks never ran."""
        transitions = []
        if events:
            transitions.extend(self.apply_events(hex_id, callsign, aircraft_class, events, now))

        if hex_id not in self._open:
            return transitions

        landed_t = self._check_landed(hex_id, ac, now)
        if landed_t:
            transitions.append(landed_t)
        # Op _open testen en niet op `landed_t`: _check_landed sluit het
        # incident via _resolve(), en dat geeft alléén een transition terug als
        # er ook genotificeerd moet worden. Een landing die stilletjes afsluit
        # (nooit boven MOGELIJK geweest) leverde dus None op terwijl het
        # incident wél weg was, waarna de rest van step() doorliep op een
        # gesloten incident. Dat viel niet op zolang alles daaronder
        # self._open.get() gebruikte.
        if hex_id not in self._open:
            return transitions

        # Weerlegging draait ELKE cyclus, net als de context-checks en om
        # dezelfde reden (bevinding 5): een toestel dat terugkomt en meteen weer
        # bewijs oplevert is het geval waarin de weerlegging het hardst nodig is
        # én het geval waarin reassess() nooit draait. Zie bevinding 12.
        refuted = self._check_signal_lost_refuted(self._open[hex_id], ac, now)
        if refuted:
            t = self._maybe_notify(refuted[0], refuted[1], refuted[2], now)
            if t:
                transitions.append(t)
            if hex_id not in self._open:
                return transitions

        transitions.extend(self.apply_context_checks(hex_id, now, hazards))

        if hex_id not in self._open:
            return transitions

        if not events:
            t = self.reassess(hex_id, ac, now, hazards)
            if t:
                transitions.append(t)
        return transitions
