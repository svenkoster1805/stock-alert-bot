STOCK ALERT BOT V4 — CATALYST FASTLANE
========================================

DOEL
----
Geen standaard "HIGH RISK OPPORTUNITY" berichten meer. V4 stuurt alleen nog
zelfstandige, verse gebeurtenissen met een materiële bedrijfsimpact. Denk aan:
- definitieve overnames / verkoop van bedrijfsonderdelen;
- grote contracten, orders of geselecteerde technologie;
- FDA/EMA goedkeuring of positieve pivotal trial;
- earnings inclusief hogere guidance;
- concrete strategische partnerships met named partner én harde details.

WAT IS VERWIJDERD?
------------------
- GEEN maximaal aantal alerts per dag.
- GEEN minimale 90 minuten tussen alerts.
- GEEN 24-uurs ticker cooldown.
- GEEN standaard high-risk scoring-spam.

In plaats daarvan: dezelfde gebeurtenis wordt 36 uur niet opnieuw gemeld.
Een nieuw, losstaand event bij hetzelfde aandeel mag dus wél meteen binnenkomen.

WAAROM DIT SNELLER IS
---------------------
De bronvolgorde is nu:
1. FMP officiële company press releases;
2. SEC 8-K/6-K filings;
3. FMP stock/general news;
4. Google News alleen als discovery/back-up, nooit als losse flashbron.

Een alert hoeft NIET eerst op Gemini te wachten. Eerst gaat er een korte,
bron-gecontroleerde FIRST SIGNAL uit. Daarna voert Gemini een tweede review uit
en BEWERKT hij dezelfde Telegram-melding. Daardoor ontvang je één melding in
plaats van extra spam, met direct nieuws en daarna context.

VERSHEID
--------
- officiële primaire bron: maximaal 30 min oud;
- trusted media: maximaal 18 min oud;
- Google-resultaat: maximaal 15 min én oorspronkelijke artikeltijd moet worden
  bevestigd na redirect/metadata-check.
- oude recaps, MSN/Quiver/opinion titels en "shares surge"-verhalen worden
  geblokkeerd.
- als de koers al >=14% is bewogen en het artikel niet piepjong is, wordt het
  als te laat gelogd en niet gestuurd.

FASTLANE-POORT
--------------
Een melding wordt alleen verzonden als hij alle onderstaande punten haalt:
1. verse publicatietijd;
2. bron voldoende betrouwbaar;
3. event is materieel en niet alleen algemeen AI/gerucht;
4. geen offering/dilution/going concern/other hard risk in hetzelfde stuk;
5. ticker is betrouwbaar gekoppeld;
6. marktbeweging wijst niet op een late achtervolgingssituatie;
7. hetzelfde event is nog niet eerder gemeld.

GITHUB ACTIONS: WAT KAN WEL/NIET?
---------------------------------
De workflow draait elke 5 minuten op werkdagen tussen 06:00 en 22:59 UTC.
Dat is het snelste schema dat GitHub Actions ondersteunt. De workflow is op
minuut 2,7,12... gepland, dus niet op het drukke :00 moment.

GitHub Actions is een goede gratis fallback, maar geen seconde-nieuwsfeed. In
continuous_worker.py staat dezelfde bot als 45-seconden-worker voor later,
wanneer je hem op een echt always-on platform zet. Daar zijn geen nieuwe API
secrets voor nodig.

INSTALLATIE
-----------
1. Download deze zip.
2. Open je bestaande GitHub-repository van de bot.
3. Upload/overschrijf ALLE bestanden, inclusief .github/workflows/stock-alert.yml.
4. Laat de bestaande GitHub Secrets staan:
   FMP_API_KEY
   GEMINI_API_KEY  (optioneel, maar aanbevolen voor tweede review)
   TELEGRAM_BOT_TOKEN
   TELEGRAM_CHAT_ID
5. GitHub -> Actions -> Stock Alert Bot V4 Catalyst Fastlane -> Run workflow.
6. Kies run_mode = test en test_message = true.
7. Daarna draait de vijf-minuten fastlane automatisch.

BELANGRIJKE LOGS
----------------
- filtered_log.json: waarom iets stil is geblokkeerd;
- latency_log.json: hoe oud een event was bij detectie;
- source_health.json: welke feeds werkten of faalden;
- alerts_log.json: alleen echte alerts;
- performance_log.json: 1/3/7-dagen prijsontwikkeling van alerts;
- event_memory.json: alleen dedupe per gebeurtenis, niet per ticker.

De bot plaatst geen orders. Een alert is research en moet altijd worden
gecontroleerd op de originele bron en actuele liquiditeit.
