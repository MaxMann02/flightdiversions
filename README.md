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

## Van losse melding naar levend incident

Een detector-hit is geen directe Telegram-melding meer. `incidents.py`
houdt per vliegtuig een **incident** bij dat over meerdere cycli heen
opnieuw beoordeeld wordt: het opent stil (niet zichtbaar) bij het eerste
signaal, wordt pas **MOGELIJK** zodra er genoeg bewijs is, kan verder
escaleren naar **WAARSCHIJNLIJK** of **BEVESTIGD** als extra signalen
(een tweede detector, aanhoudend gedrag, geen weer-verklaring) dat
bevestigen — of juist vanzelf weer verdwijnen als het toestel terugkeert
naar zijn koers, blijkt te landen waar verwacht, of het bewijs verjaart
zonder herbevestiging. Telegram meldt alleen nog bij het bereiken van
WAARSCHIJNLIJK/BEVESTIGD en bij het intrekken daarvan als vals alarm — niet
meer bij elke ruwe detector-hit. Vliegtuigen die duidelijk militair, GA/
prive, een helikopter of een zakenjet zijn (afgeleid uit gratis ADS-B-
velden, zie `classify.py`) worden voor de gedrags-detectoren helemaal
overgeslagen — een noodsquawk blijft voor iedereen actief. Zie
`MASTERPLAN.md` voor het volledige ontwerp (dit was oorspronkelijk een
eenmalig-dispatch-systeem; de sectie-1-diagnose daar legt met echte
productiedata uit waarom dat niet volstond).

`/api/incidents` (naast het bestaande `/api/events`) geeft deze levende
incidenten inclusief tijdlijn van bewijs — zichtbaar op het dashboard in
de "Active incidents"-sectie (elke 5s ververst).

Bekende beperking: adsbdb's route-database is betrouwbaar voor
lijnvluchten (één callsign = één vaste bestemming), maar niet voor kleine
regionale/pendel-operators die dezelfde callsign meerdere keren per dag voor
verschillende trajecten gebruiken (bijv. Alaska bush-vluchten) — daarvoor is
een specifieke uitzondering ingebouwd (landing terug op het eigen
vertrekpunt wordt genegeerd), maar niet elke variant daarvan is te vangen.
Sinds `MASTERPLAN.md` fase 1 is `hexdb.io` een tweede, gratis routebron
naast adsbdb — vult een deel van adsbdb's dekkingsgaten aan, lost dit niet
volledig op.

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
| `course_deviation_deg` | Hoeveel graden een bocht moet zijn om als "plotseling" te tellen (standaard 90) |
| `classification_suppress_non_airliner` | Militair/GA/prive/helikopter/zakenjet overslaan voor de gedrags-detectoren (standaard aan) |
| `incident_score_possible_threshold` / `_likely_threshold` / `_confirmed_threshold` | Score-drempels voor MOGELIJK/WAARSCHIJNLIJK/BEVESTIGD |
| `weather_sigmet_enabled` | Actief SIGMET/CWA-weer meewegen als mogelijke verklaring voor een afwijking |
| `peer_consensus_enabled` | Meerdere gelijktijdig uitwijkende toestellen in hetzelfde gebied als signaal meewegen |

Zie `config.py` voor de volledige, becommentarieerde lijst — elke sleutel
legt inline uit waarom de standaardwaarde is zoals hij is.

## Dashboard

```bash
python server.py
```

Start een lokale webserver (standaard op poort 8787, aan te passen via de
env var `DASHBOARD_PORT`) die `Flight Diversions Dashboard.dc.html` serveert
en een live JSON API (`/api/events` + `/api/incidents`) eronder. De monitor
(`main.py`) en de dashboard-server zijn losse processen die dezelfde
sqlite-database delen — je kunt ze onafhankelijk starten en stoppen. Elk
ruwe detector-signaal wordt direct opgeslagen (geen cooldown meer — dat
was het oude model) en verschijnt binnen enkele seconden op het dashboard
onder "Recent events"; of een signaal ook een Telegram-melding oplevert
hangt af van het incident waar het bewijs voor is (zie hierboven).

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

### Oracle Cloud Always Free (aanbevolen — écht gratis, altijd aan)

1. Account aanmaken op [signup.oraclecloud.com](https://signup.oraclecloud.com)
   (identiteitsverificatie via creditcard, wordt niet belast binnen de
   gratis grenzen).
2. Console > Compute > Instances > **Create Instance**:
   - Image: Canonical Ubuntu (22.04+)
   - Shape: `VM.Standard.A1.Flex` (Ampere/ARM, Always Free), bv. 1 OCPU / 6GB.
     Krijg je "out of capacity"? Probeer een andere Availability Domain, of
     val terug op `VM.Standard.E2.1.Micro` (AMD, ook Always Free, minder
     resources maar ruim genoeg voor deze app).
   - Zorg dat "Assign a public IPv4 address" aanstaat.
   - Laat Oracle een SSH-sleutelpaar genereren en **download de private key**.
3. Open de poort in Oracle's firewall: ga naar de instance > het
   subnet-linkje > de Security List > **Add Ingress Rules** > Source CIDR
   `0.0.0.0/0`, TCP, Destination Port `8787`.
4. SSH naar de VM: `ssh -i pad/naar/key.key ubuntu@<PUBLIC_IP>`.
5. Op de VM:
   ```bash
   git clone https://github.com/MaxMann02/flightdiversions.git
   cd flightdiversions
   TELEGRAM_BOT_TOKEN=xxx TELEGRAM_CHAT_ID=yyy bash deploy/setup_oracle_vm.sh
   ```
   Dit zet een venv op, installeert dependencies, en registreert een
   systemd-service (`flightdiversions`) die automatisch herstart bij een
   crash of VM-reboot — dat is de "24/7"-garantie.
6. Dashboard/link: `http://<PUBLIC_IP>:8787`.

Later Telegram-gegevens aanpassen: bewerk
`/etc/systemd/system/flightdiversions.service`, dan
`sudo systemctl daemon-reload && sudo systemctl restart flightdiversions`.

### Google Cloud Free Tier (alternatief — ook écht gratis, altijd aan)

Zelfde soort "voor altijd gratis" VM als Oracle hierboven, andere partij —
handig als Oracle's accountverificatie vastloopt (een bekend, wijdverspreid
probleem bij hen). Alleen gratis in de regio's `us-west1`, `us-central1` of
`us-east1` — geen functioneel probleem, het dashboard laadt vanuit de VS
iets trager, de scan-loops en Telegram-meldingen zelf merken daar niets van.

1. Account op [console.cloud.google.com](https://console.cloud.google.com)
   (creditcard voor verificatie, geen kosten binnen de gratis grenzen).
2. Maak een project aan, dan **Compute Engine > VM instances > Create
   Instance**:
   - Region: **verplicht** een van `us-west1` / `us-central1` / `us-east1`
   - Machine type: `e2-micro` (E2-serie)
   - Boot disk: Ubuntu 22.04+ LTS, standaard schijf ≤30GB (binnen gratis grens)
   - Create
3. **VPC network > Firewall > Create Firewall Rule**: naam bv.
   `allow-8787`, Source IP ranges `0.0.0.0/0`, Protocols/ports `tcp:8787`.
4. Klik in de console op **SSH** naast je instance — opent direct een
   terminal in de browser, geen sleutelbeheer nodig.
5. Zelfde commando's als bij Oracle (stap 5 hierboven):
   ```bash
   git clone https://github.com/MaxMann02/flightdiversions.git
   cd flightdiversions
   TELEGRAM_BOT_TOKEN=xxx TELEGRAM_CHAT_ID=yyy bash deploy/setup_oracle_vm.sh
   ```
   (het script heet naar Oracle maar is gewoon generieke Ubuntu-setup, werkt hier identiek)
6. Dashboard/link: `http://<EXTERNAL_IP>:8787` (extern IP staat bij de
   instance in de console).

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
