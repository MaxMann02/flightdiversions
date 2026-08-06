# flightdiversions

Live, wereldwijde monitor die vliegtuigen flagt die uitwijken: noodsquawks
(7700/7600/7500), landingen op een andere luchthaven dan verwacht, en
plotselinge aanhoudende koerswijzigingen tijdens de cruise-fase. Meldingen
gaan naar Telegram. Volledig gratis, geen API-keys nodig (behalve je eigen
Telegram bot-token).

## Hoe het werkt

Drie lagen, elke laag met een kleinere, duurdere selectie:

1. **Tier 0** (elke 15s): wereldwijde sweep op squawk 7700/7600/7500 via
   [adsb.lol](https://api.adsb.lol) en [airplanes.live](https://api.airplanes.live).
   Bevestigde noodmeldingen, direct alert.
2. **Tier 1** (elke 60s): wereldwijde snapshot van alle vliegtuigposities
   (via adsb.lol), bijgehouden per toestel om trajecten te herkennen:
   landing op onverwachte luchthaven, of plotselinge koerswijziging die
   aanhoudt over twee cycli (om orbits/ATC-vectoren niet te verwarren met
   een echte uitwijking).
3. **Tier 2** (on-demand): verwachte route (herkomst/bestemming) per
   callsign opgezocht via [adsbdb.com](https://api.adsbdb.com), gecached.
   Alleen voor callsigns die op een lijnvlucht-patroon lijken, in kleine
   batches per cyclus om die gratis dienst niet te overbelasten.

Zie `main.py`, `detector.py` en `providers.py` voor de exacte logica —
de commentaren daar leggen uit welke aannames op basis van live testen zijn
bijgesteld (bijv. waarom "koersafwijking t.o.v. rechte lijn naar bestemming"
niet werkt, en waarom een enkele plotselinge bocht niet genoeg is).

## Betrouwbaarheid van de signalen

- **BEVESTIGD** (noodsquawk, of landing op onverwachte luchthaven): hoge
  betrouwbaarheid, stuur je zo.
- **MOGELIJK** (aanhoudende koerswijziging): een "misschien, kijk even mee"
  signaal. Dit heeft een inherente basisruis, omdat normale luchtvaartroutes
  (airways) net als een uitwijking soms een scherpe bocht bij een waypoint
  maken. Er is geen gratis bron voor het echte ingediende vliegplan om dat
  verschil hard te maken — vertrouw dus vooral op BEVESTIGD-meldingen, en zie
  MOGELIJK als een vroege hint.

Bekende beperking: adsbdb's route-database is betrouwbaar voor
lijnvluchten (één callsign = één vaste bestemming), maar niet voor kleine
regionale/pendel-operators die dezelfde callsign meerdere keren per dag voor
verschillende trajecten gebruiken (bijv. Alaska bush-vluchten) — daarvoor is
een specifieke uitzondering ingebouwd (landing terug op het eigen
vertrekpunt wordt genegeerd), maar niet elke variant daarvan is te vangen.

## Installatie

```bash
pip install -r requirements.txt
```

## Telegram bot instellen

1. Stuur `/newbot` naar [@BotFather](https://t.me/BotFather) op Telegram,
   volg de stappen. Je krijgt een token zoals `123456789:AAExample...`.
2. Stuur zelf een berichtje naar je nieuwe bot (bijv. "hoi"), anders kan de
   bot geen chat_id vinden.
3. Haal je chat_id op:
   ```bash
   curl "https://api.telegram.org/bot<JOUW_TOKEN>/getUpdates"
   ```
   Zoek `"chat":{"id":...}` in de response — dat getal is je `telegram_chat_id`.
4. Kopieer `config.json.example` naar `config.json` en vul token + chat_id in.

## Draaien

```bash
python main.py
```

Draait continu (Ctrl+C om te stoppen). Zonder `config.json` / zonder
Telegram-gegevens logt hij events alleen naar de terminal, zonder ze te
versturen — handig om eerst te testen.

## Instellingen (config.json)

| Sleutel | Betekenis |
|---|---|
| `tier0_interval_seconds` | Hoe vaak de noodsquawk-sweep draait (standaard 15s) |
| `tier1_interval_seconds` | Hoe vaak de wereldwijde positie-sweep draait (standaard 60s) |
| `alert_cooldown_seconds` | Minimale tijd tussen twee meldingen voor hetzelfde toestel + type (standaard 1800s) |
| `course_deviation_deg` | Hoeveel graden een bocht moet zijn om als "plotseling" te tellen (standaard 90) |

## Dashboard

```bash
python server.py
```

Start een lokale webserver (standaard op poort 8787, aan te passen via de
env var `DASHBOARD_PORT`) die `Flight Diversions Dashboard.dc.html` serveert
en een live JSON API (`/api/events`) eronder. De monitor (`main.py`) en de
dashboard-server zijn losse processen die dezelfde sqlite-database delen —
je kunt ze onafhankelijk starten en stoppen. Elk event dat `main.py`
daadwerkelijk afvuurt (d.w.z. de alert-cooldown doorstaat) wordt opgeslagen
en verschijnt binnen enkele seconden op het dashboard.

## Online hosten (24/7, bereikbaar via een link)

Voor lokaal draaien (`python main.py` + `python server.py` in twee
terminals) verandert niets. Voor 24/7 hosting op een gratis platform is er
één extra bestand: `serve_all.py` draait de scan-loops (tier0/tier1) én de
dashboard-webserver samen in één proces — nodig omdat de meeste gratis
hosting-tiers je maar één "altijd aan"-proces geven, geen aparte
achtergrond-worker.

```bash
python serve_all.py
```

Er is ook een `Dockerfile` (bouwt en start `serve_all.py`, luistert op de
`PORT` env var die vrijwel elk platform automatisch instelt) voor
platforms die een container verwachten.

**Secrets via environment variables, niet via `config.json`.** `config.json`
staat in `.gitignore` en hoort nooit in een git-repo of Docker-image
terecht te komen. Elke sleutel uit `config.py`'s `_DEFAULTS` kan in plaats
daarvan als env var meegegeven worden, in hoofdletters — bijv.
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TIER1_INTERVAL_SECONDS`. Elk
hosting-platform heeft hiervoor een plek in zijn dashboard/CLI om env vars
in te stellen zonder ze ergens op te slaan in de code.

**Let op: sqlite-data overleeft een herstart niet zonder persistente
opslag.** `data/flightdiversions.sqlite3` (event-geschiedenis, geleerde
routes, cooldowns) en `data/airports.csv` staan op de lokale schijf van het
proces. De meeste gratis tiers hebben een *ephemeral* filesystem: bij elke
herstart/redeploy is de inhoud weg (de airports.csv wordt gewoon opnieuw
gedownload, geen probleem — de event-geschiedenis/cooldowns zijn dan wel
leeg). Als dat een probleem is: zoek een platform met een gratis
persistent volume, of accepteer dat het dashboard alleen sinds de laatste
herstart laat zien (de Telegram-meldingen zelf werken sowieso, ongeacht
persistentie).

## Afhankelijkheden van derden

Dit project leunt op twee gratis, vrijwilligers-gedreven community-projecten
(adsb.lol, airplanes.live) en één gratis lookup-dienst (adsbdb.com). Geen
van drie heeft een SLA — ze kunnen throttlen, wijzigen, of tijdelijk down
zijn. `providers.py` vangt fouten per request af zodat de monitor
doordraait op de resterende bronnen. `airplanes.live`'s voorwaarden staan
niet-commercieel/educatief gebruik toe — precies wat dit is.
