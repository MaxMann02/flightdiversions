"""Vliegtuigclassificatie op basis van velden die al gratis in elke
adsb.lol/airplanes.live-response zitten (dbFlags, category — zie
providers._normalize_aircraft) plus de bestaande airline-callsign-regex.
Geen extra API-calls. Zie MASTERPLAN.md sectie 4 voor de volledige
beslisboom, inclusief de hexdb.io-verrijkingsstap die in een latere fase
toegevoegd wordt (dan kan AIRLINER ook herkend worden zonder de
categorie-vereiste hieronder, en kan CARGO als aparte klasse ontstaan).

Gebruik: classify(ac) met ac de genormaliseerde dict uit
providers._normalize_aircraft (of fetch_world_snapshot's output, zelfde
vorm).
"""
import re

AIRLINE_CALLSIGN_RE = re.compile(r"^[A-Z]{3}[0-9]{1,4}[A-Z]?$")

MILITARY = "MILITAIR"
HELICOPTER = "HELIKOPTER"
LIGHT_OTHER = "OVERIG_LICHT"
AIRLINER = "AIRLINER"
GA_PRIVATE = "GA_PRIVE"
BUSINESS_JET = "ZAKENJET"
UNKNOWN = "ONBEKEND"

# Klassen waarvoor de vijf gedrags-detectoren (course_deviation t/m
# signal_lost_near_airport) worden overgeslagen — detect_emergency
# (noodsquawk) blijft voor ELKE klasse actief, ook deze. Zie MASTERPLAN.md
# sectie 4: een noodsquawk van een Cessna of een F-16 is even reëel en vaak
# urgenter dan van een verkeersvliegtuig, dus die uitzondering wordt hier
# bewust niet gemodelleerd — de aanroeper (main.py) regelt dat door
# detect_emergency altijd apart te laten lopen, ongeacht deze set.
SUPPRESSED_CLASSES = {MILITARY, HELICOPTER, LIGHT_OTHER, GA_PRIVATE, BUSINESS_JET}

# Kleine, bewust beperkte lijst bekende zakenjet-ICAO-typecodes. Een
# vals-negatief hier (onbekend type valt terug op ONBEKEND, dus NIET
# onderdrukt) is goedkoper dan een vals-positief (een regionale airliner
# die per ongeluk als zakenjet onderdrukt wordt) — zie MASTERPLAN.md
# sectie 14 voor de afweging.
_BUSINESS_JET_TYPES = {
    "GLF4", "GLF5", "GLF6", "GL5T", "GL6T", "GLEX",
    "CL30", "CL35", "CL60",
    "FA7X", "FA8X", "FA50", "FA6X", "F2TH", "F900",
    "C56X", "C68A", "C700", "C750", "C25A", "C25B", "C25C",
    "LJ35", "LJ40", "LJ45", "LJ60",
    "E50P", "E55P", "PC24",
}

# DO-260B ADS-B emitter category-tabel (A0-D7). Alleen de subset die hier
# gebruikt wordt; zie MASTERPLAN.md sectie 14 voor de bevestiging (H60 en
# AS65 helikopters kwamen live terug als A7, een C-17 als A5).
_LIGHT_CATEGORIES = {"A1", "A2"}          # licht/klein — meestal GA
_AIRLINER_SCALE_CATEGORIES = {"A3", "A4", "A5"}  # groot/high-vortex-large/heavy
_OTHER_LIGHT_CATEGORIES = {"B1", "B2", "B3", "B4", "B5", "B6", "B7"}  # zweefvliegtuig/ballon/parachute/ultralight/UAV/ruimtevaartuig


def classify(ac: dict) -> str:
    """ac: genormaliseerde aircraft-dict (providers._normalize_aircraft-vorm).
    Retourneert één van de klasse-constanten hierboven."""
    db_flags = ac.get("db_flags") or 0
    try:
        if int(db_flags) & 1:
            return MILITARY
    except (TypeError, ValueError):
        pass

    category = (ac.get("category") or "").strip().upper()
    if category == "A7":
        return HELICOPTER
    if category in _OTHER_LIGHT_CATEGORIES:
        return LIGHT_OTHER

    callsign = (ac.get("callsign") or "").strip()
    is_airline_callsign = bool(callsign) and bool(AIRLINE_CALLSIGN_RE.match(callsign))

    if is_airline_callsign and category in _AIRLINER_SCALE_CATEGORIES:
        return AIRLINER
    if category in _LIGHT_CATEGORIES and not is_airline_callsign:
        return GA_PRIVATE

    aircraft_type = (ac.get("type") or "").strip().upper()
    if aircraft_type in _BUSINESS_JET_TYPES:
        return BUSINESS_JET

    return UNKNOWN


def refine_with_hexdb(current_class: str, hexdb_aircraft: dict | None) -> str:
    """Second opinion for aircraft classify() alone left as ONBEKEND/UNKNOWN
    — e.g. a cargo/charter callsign that doesn't match AIRLINE_CALLSIGN_RE,
    or a category that wasn't clearly airliner-scale or clearly light.
    hexdb.io's registered-owner/operator-code fields (not available to
    classify() without a network call) can often resolve the ambiguity. See
    providers.lookup_aircraft_hexdb and MASTERPLAN.md sectie 4, stap 4.
    No-ops (returns current_class unchanged) for any class other than
    UNKNOWN — this only ever refines the one bucket classify() itself
    couldn't place confidently."""
    if current_class != UNKNOWN or not hexdb_aircraft:
        return current_class
    operator_code = (hexdb_aircraft.get("OperatorFlagCode") or "").strip()
    owner = (hexdb_aircraft.get("RegisteredOwners") or "").strip()
    if operator_code:
        # A 3-letter ICAO operator code is only assigned to commercial
        # operators (airline, cargo, charter) — individually-owned GA/
        # business aircraft don't have one.
        return AIRLINER
    if owner:
        # An owner name with no operator code is typical of private
        # ownership rather than a registered commercial operator.
        return GA_PRIVATE
    return current_class
