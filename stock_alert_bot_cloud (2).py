import os
import re
import json
import hashlib
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus

import requests
import feedparser


# =========================
# Secrets uit GitHub Actions
# =========================

FMP_API_KEY = os.getenv("FMP_API_KEY", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TEST_MESSAGE = os.getenv("TEST_MESSAGE", "false").strip().lower() == "true"


# =========================
# Instellingen
# =========================

SEEN_FILE = "seen.json"
ALERTS_LOG_FILE = "alerts_log.json"
PERFORMANCE_LOG_FILE = "performance_log.json"

MIN_MOVE_PERCENT = 5
MIN_OPPORTUNITY_SCORE = 7
MIN_URGENCY_SCORE = 6
MIN_RISK_SCORE_ALERT = 8

MAX_AI_ANALYSES_PER_RUN = 14
MAX_ALERTS_PER_RUN = 6
MAX_CANDIDATES_PER_SOURCE = 40

GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
]

OPPORTUNITY_KEYWORDS = [
    "surge", "surges", "soars", "jumps", "rallies", "shares rise",
    "shares jump", "up after", "after securing", "secures", "securing",
    "contract", "major contract", "15-year", "lease", "data center",
    "ai", "artificial intelligence", "commercial launch", "launches",
    "official launch", "market launch", "fda approval", "approval",
    "phase 3", "clinical trial", "strategic partnership", "partnership",
    "acquisition", "merger", "earnings beat", "beats estimates",
    "raises guidance", "guidance raised", "billion", "$", "wins deal",
    "deal worth", "government contract", "new order", "backlog",
    "patent", "breakthrough", "expanded agreement", "supply agreement",
]

RISK_KEYWORDS = [
    "offering", "stock offering", "share offering", "dilution",
    "bankruptcy", "reverse split", "going concern", "investigation",
    "lawsuit", "sec investigation", "delisting", "short report",
    "fraud", "debt", "cash burn", "misses estimates", "cuts guidance",
    "material weakness", "restatement", "resignation", "default",
]

GOOGLE_NEWS_QUERIES = [
    '"shares surge" stock "contract" OR "partnership"',
    '"shares jump" stock "AI" OR "data center"',
    '"stock" "secures" "contract" "shares"',
    '"stock" "FDA approval" "shares"',
    '"pre-market mover" stock news',
    '"stock" "commercial launch" "shares"',
    '"stock" "raises guidance" "shares"',
    '"stock" "up after" "billion"',
    '"small cap" stock "major contract"',
    '"stock" "strategic partnership" "shares jump"',
]


# =========================
# Basisfuncties
# =========================

def now_utc():
    return datetime.now(timezone.utc)


def now_iso():
    return now_utc().isoformat()


def today_key():
    return now_utc().strftime("%Y-%m-%d")


def require_env():
    missing = []
    for name, value in {
        "FMP_API_KEY": FMP_API_KEY,
        "GEMINI_API_KEY": GEMINI_API_KEY,
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
    }.items():
        if not value:
            missing.append(name)

    if missing:
        raise ValueError("Missing GitHub Secrets: " + ", ".join(missing))


def load_json_file(path, default):
    if not os.path.exists(path):
        return default

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json_file(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_seen():
    return load_json_file(SEEN_FILE, {})


def save_seen(seen):
    items = list(seen.items())[-1500:]
    save_json_file(SEEN_FILE, dict(items))


def load_alerts_log():
    return load_json_file(ALERTS_LOG_FILE, [])


def save_alerts_log(log):
    save_json_file(ALERTS_LOG_FILE, log[-800:])


def load_performance_log():
    return load_json_file(PERFORMANCE_LOG_FILE, [])


def save_performance_log(log):
    save_json_file(PERFORMANCE_LOG_FILE, log[-1500:])


def make_id(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def clean_html(text):
    text = re.sub(r"<[^>]+>", " ", str(text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_percent(value):
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace("%", "").replace("+", "").replace(",", ".").strip()
    try:
        return float(text)
    except Exception:
        return 0.0


def parse_float(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace("$", "").replace(",", "").strip()
    try:
        return float(text)
    except Exception:
        return None


def text_has_keywords(text):
    t = text.lower()
    return any(k.lower() in t for k in OPPORTUNITY_KEYWORDS + RISK_KEYWORDS)


def extract_symbol_from_text(text):
    patterns = [
        r"\(([A-Z]{1,5})\)",
        r"\bNASDAQ:\s*([A-Z]{1,5})\b",
        r"\bNYSE:\s*([A-Z]{1,5})\b",
        r"\bAMEX:\s*([A-Z]{1,5})\b",
        r"\bTicker:\s*([A-Z]{1,5})\b",
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(1)
    return ""


# =========================
# Telegram
# =========================

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message[:3900],
        "disable_web_page_preview": False,
    }
    r = requests.post(url, json=payload, timeout=25)
    if r.status_code != 200:
        print("Telegram fout:", r.status_code, r.text)
    else:
        print("Telegram bericht verzonden.")


def send_test_message():
    send_telegram(
        "✅ Test gelukt: jouw ADVANCED 24/7 stock alert bot draait via GitHub Actions.\n\n"
        "Hij zoekt naar snelle stijgers, nieuws, SEC-filings, kansen én risico's."
    )


# =========================
# FMP / data
# =========================

def fmp_get(endpoint, params=None):
    if params is None:
        params = {}
    params["apikey"] = FMP_API_KEY
    url = f"https://financialmodelingprep.com/stable/{endpoint}"
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def fmp_quote(symbol):
    if not symbol:
        return None
    try:
        data = fmp_get("quote", {"symbol": symbol})
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict):
            return data
    except Exception as e:
        print("FMP quote fout:", symbol, e)
    return None


def get_biggest_gainers():
    candidates = []
    try:
        data = fmp_get("biggest-gainers")
    except Exception as e:
        print("FMP biggest-gainers fout:", e)
        return candidates

    if not isinstance(data, list):
        return candidates

    for item in data[:MAX_CANDIDATES_PER_SOURCE]:
        symbol = item.get("symbol") or item.get("ticker") or ""
        name = item.get("name") or item.get("companyName") or symbol
        raw_change = (
            item.get("changePercentage")
            or item.get("changesPercentage")
            or item.get("percentageChange")
            or item.get("change")
            or item.get("changes")
            or 0
        )
        move_pct = parse_percent(raw_change)
        price = parse_float(item.get("price"))

        if move_pct < MIN_MOVE_PERCENT:
            continue

        candidates.append({
            "source": "FMP Biggest Gainers",
            "symbol": symbol,
            "company": name,
            "title": f"{name} ({symbol}) top gainer: +{move_pct}%",
            "summary": f"{name} staat bij de grootste stijgers. Prijs: {price}. Beweging: +{move_pct}%.",
            "url": "",
            "move_pct": move_pct,
            "price_at_signal": price,
            "dedupe_mode": "symbol_daily",
        })

    return candidates


def get_fmp_news(endpoint, label):
    candidates = []
    try:
        data = fmp_get(endpoint, {"page": 0, "limit": 50})
    except Exception as e:
        print(label, "fout:", e)
        return candidates

    if not isinstance(data, list):
        return candidates

    for item in data:
        title = item.get("title") or ""
        text = item.get("text") or item.get("content") or item.get("summary") or ""
        url = item.get("url") or item.get("link") or ""
        publisher = item.get("site") or item.get("publisher") or label
        symbol = item.get("symbol") or item.get("symbols") or ""

        if isinstance(symbol, list):
            symbol = ",".join(symbol)

        full = clean_html(f"{title} {text} {symbol}")

        if not text_has_keywords(full):
            continue

        candidates.append({
            "source": f"{label} - {publisher}",
            "symbol": str(symbol) if symbol else extract_symbol_from_text(full),
            "company": "",
            "title": clean_html(title),
            "summary": clean_html(text)[:1100],
            "url": url,
            "move_pct": 0,
            "price_at_signal": None,
            "dedupe_mode": "url_or_title",
        })

    return candidates


def get_google_news_candidates():
    candidates = []
    for query in GOOGLE_NEWS_QUERIES:
        rss_url = "https://news.google.com/rss/search?q=" + quote_plus(query + " when:1d") + "&hl=en-US&gl=US&ceid=US:en"
        try:
            feed = feedparser.parse(rss_url)
        except Exception as e:
            print("Google RSS fout:", e)
            continue

        for entry in feed.entries[:12]:
            title = clean_html(entry.get("title", ""))
            summary = clean_html(entry.get("summary", ""))
            link = entry.get("link", "")
            full = f"{title} {summary}"

            if not text_has_keywords(full):
                continue

            candidates.append({
                "source": "Google News RSS",
                "symbol": extract_symbol_from_text(full),
                "company": "",
                "title": title,
                "summary": summary[:1100],
                "url": link,
                "move_pct": 0,
                "price_at_signal": None,
                "dedupe_mode": "url_or_title",
            })

    return candidates


def get_sec_filings():
    candidates = []
    urls = [
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&count=100&output=atom",
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=6-K&count=100&output=atom",
    ]
    headers = {"User-Agent": "stock-alert-bot student research bot contact@example.com"}

    for url in urls:
        try:
            r = requests.get(url, headers=headers, timeout=30)
            r.raise_for_status()
            feed = feedparser.parse(r.text)
        except Exception as e:
            print("SEC feed fout:", e)
            continue

        for entry in feed.entries[:30]:
            title = clean_html(entry.get("title", ""))
            summary = clean_html(entry.get("summary", ""))
            link = entry.get("link", "")
            full = f"{title} {summary}"

            if not text_has_keywords(full):
                continue

            candidates.append({
                "source": "SEC EDGAR 8-K/6-K",
                "symbol": extract_symbol_from_text(full),
                "company": "",
                "title": title,
                "summary": summary[:1100],
                "url": link,
                "move_pct": 0,
                "price_at_signal": None,
                "dedupe_mode": "url_or_title",
            })

    return candidates


# =========================
# AI Analyse
# =========================

def safe_json_loads(text):
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        return json.loads(m.group(0))
    raise ValueError("Geen geldige JSON ontvangen van AI")


def make_learning_context(performance_log):
    # Simpele context uit eerdere resultaten. Niet te groot maken.
    if not performance_log:
        return "Nog geen eerdere performance-data beschikbaar."

    completed = [x for x in performance_log if x.get("return_1d") is not None or x.get("return_3d") is not None or x.get("return_7d") is not None]
    if not completed:
        return "Er zijn al alerts gelogd, maar nog geen 1d/3d/7d performance-resultaten."

    by_event = {}
    for x in completed:
        event = x.get("event_type", "unknown")
        by_event.setdefault(event, []).append(x)

    lines = []
    for event, rows in list(by_event.items())[:8]:
        returns = []
        for r in rows:
            for key in ["return_1d", "return_3d", "return_7d"]:
                if r.get(key) is not None:
                    returns.append(float(r[key]))
        if returns:
            avg = sum(returns) / len(returns)
            lines.append(f"{event}: {len(rows)} alerts, gemiddelde gemeten return {avg:.2f}%")

    return "\n".join(lines) if lines else "Nog te weinig bruikbare performance-data."


def gemini_analyze(candidate, performance_log):
    learning_context = make_learning_context(performance_log)

    prompt = f"""
Je bent een aandelen-researchbot voor snelle kansen én risico's.

DOEL:
De gebruiker wil snel op de hoogte zijn van aandelen die hard kunnen bewegen door concreet nieuws:
contracten, AI-deals, data-center leases, FDA approvals, earnings beats, launches, partnerships,
overnames, SEC-filings, grote koersbewegingen.

BELANGRIJKE REGELS:
- Geef GEEN persoonlijk financieel advies.
- Zeg niet letterlijk: koop dit aandeel.
- Geef research-informatie zodat de gebruiker zelf kan controleren.
- Wees streng bij hype, lage liquiditeit, penny stocks, pump & dump, verwatering en aandelen die al hard gestegen zijn.
- Benoem als iets alleen geschikt is als kleine speculatieve positie.
- Benoem wat de gebruiker vóór handelen moet checken.
- Schrijf in simpel Nederlands.

EERDERE BOT-LEERPUNTEN:
{learning_context}

SIGNAAL:
Bron: {candidate["source"]}
Ticker/symbool: {candidate["symbol"]}
Bedrijf: {candidate["company"]}
Titel: {candidate["title"]}
Samenvatting: {candidate["summary"]}
Koersbeweging volgens bron: {candidate["move_pct"]}%
Prijs bij signaal indien bekend: {candidate.get("price_at_signal")}
Link: {candidate["url"]}

Geef ALLEEN geldig JSON terug met exact deze velden:
{{
  "symbol": "",
  "company": "",
  "event_type": "",
  "opportunity_score": 1,
  "risk_score": 1,
  "hype_score": 1,
  "urgency_score": 1,
  "short_summary_nl": "",
  "why_it_could_move": "",
  "main_risks": "",
  "research_action": "",
  "position_type": "",
  "check_before_buying": "",
  "learning_note": ""
}}

Score uitleg:
opportunity_score 1-10 = kans dat dit signaal koersgevoelig is.
risk_score 1-10 = risico op verlies, hype, verwatering, te laat instappen of slecht nieuws.
hype_score 1-10 = kans dat dit vooral hype/pump is.
urgency_score 1-10 = hoe snel iemand dit zou moeten onderzoeken.

research_action moet één van deze zijn:
- negeren
- watchlist
- verder onderzoeken
- hoog risico, alleen kleine speculatieve kans
- risico-alert, waarschijnlijk te laat of te gevaarlijk

position_type moet één van deze zijn:
- geen positie
- lange termijn research
- kleine speculatieve positie
- alleen volgen
"""

    last_error = None

    for model in GEMINI_MODELS:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.2,
                    "response_mime_type": "application/json"
                }
            }
            r = requests.post(url, params={"key": GEMINI_API_KEY}, json=payload, timeout=45)
            r.raise_for_status()
            data = r.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return safe_json_loads(text)
        except Exception as e:
            last_error = e
            print(f"Gemini model {model} fout:", e)

    raise last_error


# =========================
# Alert/performance logic
# =========================

def build_dedupe_id(candidate):
    if candidate.get("dedupe_mode") == "symbol_daily" and candidate.get("symbol"):
        base = f'{candidate["source"]}|{candidate["symbol"]}|{today_key()}'
    else:
        base = f'{candidate["source"]}|{candidate["symbol"]}|{candidate["title"]}|{candidate["url"]}'
    return make_id(base)


def should_alert(candidate, analysis):
    opp = int(analysis.get("opportunity_score", 0))
    risk = int(analysis.get("risk_score", 0))
    urgency = int(analysis.get("urgency_score", 0))
    move = float(candidate.get("move_pct", 0))

    if opp >= MIN_OPPORTUNITY_SCORE and urgency >= MIN_URGENCY_SCORE:
        return True
    if risk >= MIN_RISK_SCORE_ALERT and move >= 8:
        return True
    if move >= 10:
        return True
    return False


def build_message(candidate, analysis):
    opp = int(analysis.get("opportunity_score", 0))
    risk = int(analysis.get("risk_score", 0))
    hype = int(analysis.get("hype_score", 0))
    urgency = int(analysis.get("urgency_score", 0))

    if risk >= 8 and opp < 8:
        icon = "⚠️ RISK ALERT"
    elif opp >= 8:
        icon = "🔥 HIGH RISK OPPORTUNITY"
    else:
        icon = "🚨 STOCK RESEARCH ALERT"

    symbol = analysis.get("symbol") or candidate.get("symbol") or "Onbekend"
    company = analysis.get("company") or candidate.get("company") or "Onbekend"

    return f"""{icon}

Ticker:
{symbol}

Bedrijf:
{company}

Bron:
{candidate["source"]}

Koers/signaal:
{candidate["move_pct"]}% beweging gemeld

Event type:
{analysis.get("event_type")}

Scores:
Kans: {opp}/10
Risico: {risk}/10
Hype: {hype}/10
Urgentie: {urgency}/10

Kort:
{analysis.get("short_summary_nl")}

Waarom kan dit bewegen?
{analysis.get("why_it_could_move")}

Belangrijkste risico's:
{analysis.get("main_risks")}

Research-actie:
{analysis.get("research_action")}

Type positie:
{analysis.get("position_type")}

Check vóór je iets doet:
{analysis.get("check_before_buying")}

Leer-notitie:
{analysis.get("learning_note")}

Titel:
{candidate["title"]}

Link:
{candidate["url"] if candidate["url"] else "Geen link"}

Let op: dit is research, geen koopadvies. Altijd zelf controleren.
"""


def collect_candidates():
    candidates = []
    print("Ophalen: FMP biggest gainers")
    candidates.extend(get_biggest_gainers())

    print("Ophalen: FMP stock news")
    candidates.extend(get_fmp_news("news/stock-latest", "FMP Stock News"))

    print("Ophalen: FMP general news")
    candidates.extend(get_fmp_news("news/general-latest", "FMP General News"))

    print("Ophalen: Google News RSS")
    candidates.extend(get_google_news_candidates())

    print("Ophalen: SEC filings")
    candidates.extend(get_sec_filings())

    unique = []
    ids = set()
    for c in candidates:
        cid = make_id(f'{c["source"]}|{c["symbol"]}|{c["title"]}|{c["url"]}')
        if cid in ids:
            continue
        ids.add(cid)
        unique.append(c)

    unique = sorted(unique, key=lambda x: float(x.get("move_pct", 0)), reverse=True)
    return unique


def log_alert(alerts_log, performance_log, candidate, analysis):
    symbol = analysis.get("symbol") or candidate.get("symbol") or ""
    price = candidate.get("price_at_signal")

    # Als de prijs niet bekend is, probeer FMP quote
    if symbol and not price:
        quote = fmp_quote(symbol)
        if quote:
            price = parse_float(quote.get("price") or quote.get("previousClose"))

    alert_id = make_id(f'{symbol}|{candidate["title"]}|{candidate["url"]}|{now_iso()}')

    row = {
        "alert_id": alert_id,
        "time": now_iso(),
        "symbol": symbol,
        "company": analysis.get("company") or candidate.get("company") or "",
        "source": candidate.get("source"),
        "title": candidate.get("title"),
        "url": candidate.get("url"),
        "event_type": analysis.get("event_type"),
        "opportunity_score": analysis.get("opportunity_score"),
        "risk_score": analysis.get("risk_score"),
        "hype_score": analysis.get("hype_score"),
        "urgency_score": analysis.get("urgency_score"),
        "research_action": analysis.get("research_action"),
        "position_type": analysis.get("position_type"),
        "price_at_alert": price,
    }

    alerts_log.append(row)

    if symbol and price:
        performance_log.append({
            **row,
            "return_1d": None,
            "return_3d": None,
            "return_7d": None,
            "checked_1d": False,
            "checked_3d": False,
            "checked_7d": False,
        })


def update_performance_log(performance_log):
    changed = False
    now = now_utc()

    for row in performance_log:
        symbol = row.get("symbol")
        start_price = parse_float(row.get("price_at_alert"))
        alert_time_raw = row.get("time")

        if not symbol or not start_price or not alert_time_raw:
            continue

        try:
            alert_time = datetime.fromisoformat(alert_time_raw.replace("Z", "+00:00"))
        except Exception:
            continue

        age = now - alert_time

        checks = [
            ("1d", timedelta(days=1), "checked_1d", "return_1d"),
            ("3d", timedelta(days=3), "checked_3d", "return_3d"),
            ("7d", timedelta(days=7), "checked_7d", "return_7d"),
        ]

        needed = [x for x in checks if age >= x[1] and not row.get(x[2])]
        if not needed:
            continue

        quote = fmp_quote(symbol)
        if not quote:
            continue

        current_price = parse_float(quote.get("price"))
        if not current_price:
            continue

        ret = ((current_price - start_price) / start_price) * 100

        for _, _, checked_key, return_key in needed:
            row[return_key] = round(ret, 2)
            row[checked_key] = True
            row["last_checked_price"] = current_price
            row["last_checked_time"] = now_iso()
            changed = True

    return changed


def build_learning_report(performance_log):
    completed = [
        x for x in performance_log
        if x.get("return_1d") is not None or x.get("return_3d") is not None or x.get("return_7d") is not None
    ]

    if not completed:
        return None

    # Niet elke run rapport sturen. Alleen 1x per dag rond 08/09 UTC niet exact gegarandeerd.
    hour = now_utc().hour
    if hour not in [7, 8, 9]:
        return None

    grouped = {}
    for x in completed:
        event = x.get("event_type") or "unknown"
        grouped.setdefault(event, []).append(x)

    lines = ["📊 BOT LEARNING REPORT", "", "Gemeten resultaten per event type:"]

    for event, rows in list(grouped.items())[:10]:
        returns = []
        for r in rows:
            for key in ["return_1d", "return_3d", "return_7d"]:
                if r.get(key) is not None:
                    returns.append(float(r[key]))
        if returns:
            avg = sum(returns) / len(returns)
            lines.append(f"- {event}: {len(rows)} alerts, gem. return {avg:.2f}%")

    lines.append("")
    lines.append("Let op: dit is alleen een logboek, geen garantie voor toekomstige resultaten.")
    return "\n".join(lines)


def maybe_send_learning_report(performance_log, seen):
    report_key = "learning_report_" + today_key()
    if seen.get(report_key):
        return

    report = build_learning_report(performance_log)
    if report:
        send_telegram(report)
        seen[report_key] = {"time": now_iso(), "type": "learning_report"}


# =========================
# Main
# =========================

def main():
    require_env()

    print("Advanced stock alert bot gestart:", now_iso())

    seen = load_seen()
    alerts_log = load_alerts_log()
    performance_log = load_performance_log()

    if TEST_MESSAGE:
        send_test_message()

    if update_performance_log(performance_log):
        print("Performance log bijgewerkt.")

    maybe_send_learning_report(performance_log, seen)

    candidates = collect_candidates()
    print(f"Kandidaten gevonden: {len(candidates)}")

    analyses_done = 0
    alerts_sent = 0

    for candidate in candidates:
        cid = build_dedupe_id(candidate)

        if cid in seen:
            continue

        seen[cid] = {
            "time": now_iso(),
            "title": candidate["title"],
            "source": candidate["source"],
            "symbol": candidate["symbol"],
            "url": candidate["url"],
        }

        if analyses_done >= MAX_AI_ANALYSES_PER_RUN:
            continue

        analyses_done += 1

        try:
            analysis = gemini_analyze(candidate, performance_log)
        except Exception as e:
            print("AI analyse fout:", e)
            continue

        print(
            "Analyse:",
            candidate["source"],
            candidate["symbol"],
            "Kans:",
            analysis.get("opportunity_score"),
            "Risico:",
            analysis.get("risk_score"),
            candidate["title"][:90]
        )

        if should_alert(candidate, analysis):
            message = build_message(candidate, analysis)
            send_telegram(message)
            log_alert(alerts_log, performance_log, candidate, analysis)
            alerts_sent += 1

        if alerts_sent >= MAX_ALERTS_PER_RUN:
            break

    save_seen(seen)
    save_alerts_log(alerts_log)
    save_performance_log(performance_log)

    print(f"Bot klaar. Analyses: {analyses_done}. Alerts: {alerts_sent}.")


if __name__ == "__main__":
    main()
