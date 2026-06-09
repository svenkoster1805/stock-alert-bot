STOCK ALERT BOT V2 - ADVANCED
=============================

Deze versie is uitgebreid en bedoeld voor 24/7 Telegram-alerts via GitHub Actions.

Wat hij doet:
- Checkt elke 15 minuten:
  1. FMP Biggest Gainers
  2. FMP Stock News
  3. FMP General News
  4. Google News RSS met opportunity-zoektermen
  5. SEC 8-K / 6-K filings
- Laat Gemini AI elk signaal analyseren.
- Stuurt Telegram-alerts met:
  - ticker
  - kans-score
  - risico-score
  - hype-score
  - urgentie-score
  - reden van beweging
  - checklijst vóór handelen
  - position_type
- Houdt een eigen logboek bij:
  - seen.json: voorkomt dubbele alerts
  - alerts_log.json: alle verstuurde alerts
  - performance_log.json: latere 1d/3d/7d performancecheck
- Stuurt automatisch een dagrapport als er iets te rapporteren is.

BELANGRIJK:
Dit is research, geen persoonlijk financieel advies.
De bot koopt/verkoopt niets automatisch.
Jij beslist zelf in Trading 212.

BESTANDEN
=========
requirements.txt
stock_alert_bot_cloud.py
seen.json
alerts_log.json
performance_log.json
.github/workflows/stock-alert.yml

INSTALLATIE IN HET KORT
=======================
1. Maak GitHub repo: stock-alert-bot
2. Upload alle bestanden uit deze ZIP.
3. Zet deze secrets in GitHub:
   - FMP_API_KEY
   - GEMINI_API_KEY
   - TELEGRAM_BOT_TOKEN
   - TELEGRAM_CHAT_ID
4. Ga naar Actions -> Stock Alert Bot -> Run workflow.
5. Vul test_message = true om meteen een testbericht te krijgen.

AANPASSEN
=========
In stock_alert_bot_cloud.py kun je aanpassen:

MIN_MOVE_PERCENT = 5
MIN_OPPORTUNITY_SCORE = 7
MIN_URGENCY_SCORE = 6
MAX_AI_ANALYSES_PER_RUN = 14
MAX_ALERTS_PER_RUN = 6

Meer meldingen:
MIN_OPPORTUNITY_SCORE = 6

Minder meldingen:
MIN_OPPORTUNITY_SCORE = 8

Sneller draaien:
Open .github/workflows/stock-alert.yml en verander:
cron: "*/15 * * * *"

LET OP: sneller draaien gebruikt meer gratis API-calls.

SELF LEARNING / LOGBOEK
=======================
De bot leert niet magisch de markt voorspellen.
Hij wordt slimmer door data bij te houden:
- Welke event_types gaf hij alerts voor?
- Welke scores hadden die alerts?
- Hoe bewoog het aandeel later na 1/3/7 dagen?
- Welke soorten alerts lijken vaker goed/slecht?

Daarom maakt hij alerts_log.json en performance_log.json.
Later kunnen we hiermee de score-regels finetunen.
