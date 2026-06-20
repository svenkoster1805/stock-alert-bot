"""
Stock Alert Bot V4 — Catalyst Fastlane

Design goals
------------
1. Alert as early as the data sources make possible.
2. Send only fresh, source-backed corporate catalysts — no generic gainers,
   opinions, recycled headlines or automatic "high risk" spam.
3. Remove daily limits and global cool-downs. Dedupe only the *same event*.
4. Send the first flash without waiting for an LLM, then edit the same Telegram
   message with a stricter secondary review. This keeps speed and quality.

This tool is for research and monitoring. It does not place trades and does not
make promises about future returns.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote_plus, urlparse

import feedparser
import requests

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
FMP_API_KEY = os.getenv("FMP_API_KEY", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
RUN_MODE = (os.getenv("RUN_MODE", "fast") or "fast").strip().lower()
TEST_MESSAGE = os.getenv("TEST_MESSAGE", "false").strip().lower() in {"1", "true", "yes", "y"}

SEEN_FILE = "seen.json"
EVENT_MEMORY_FILE = "event_memory.json"
ALERTS_LOG_FILE = "alerts_log.json"
FILTERED_LOG_FILE = "filtered_log.json"
SIGNALS_LOG_FILE = "signals_log.json"
PERFORMANCE_LOG_FILE = "performance_log.json"
LATENCY_LOG_FILE = "latency_log.json"
SOURCE_HEALTH_FILE = "source_health.json"

# Freshness. "Primary" means official company release / SEC filing. "Media"
# means Reuters/AP/FT/Bloomberg etc. Google RSS is never a flash source by
# itself; it must resolve to a real publisher and pass the same checks.
PRIMARY_MAX_AGE_MINUTES = 30
MEDIA_MAX_AGE_MINUTES = 18
GOOGLE_MAX_AGE_MINUTES = 15
MAX_AI_FOLLOWUPS = 3
MIN_PRICE_IF_KNOWN = 1.00
MIN_MARKET_CAP_IF_KNOWN = 60_000_000
LATE_MOVE_PCT = 14.0
LATE_MOVE_MIN_AGE = 10
EVENT_DEDUP_HOURS = 36

FMP_BASE = "https://financialmodelingprep.com/stable"
SEC_HEADERS = {
    "User-Agent": "SvenKosterStockResearchBot/4.2 student-research@example.com",
    "Accept-Language": "en-US,en;q=0.8",
}
HTTP_HEADERS = {
    **SEC_HEADERS,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Sources are intentionally conservative. A non-listed publisher can still be
# used for logging, but it cannot independently trigger a fast Telegram alert.
BLOCKED_SOURCE_WORDS = [
    "msn", "quiver", "motley fool", "zacks", "investorplace", "tipranks",
    "stocktwits", "seeking alpha", "benzinga", "opinion", "opinions",
]
OFFICIAL_SOURCE_WORDS = [
    "sec.gov", "businesswire", "business wire", "globenewswire",
    "globe newswire", "prnewswire", "pr newswire", "investor relations",
    "press release", "news release",
]
TRUSTED_MEDIA_WORDS = [
    "reuters", "associated press", "bloomberg", "financial times",
    "wall street journal", "dow jones", "cnbc", "barron's", "barrons",
    "the verge", "stat", "fierce biotech", "endpoints news", "biopharma dive",
]

# The event types that can move an equity for more than a random headline.
EVENT_RULES = {
    "M_AND_A": [
        r"\bdefinitive agreement\b", r"\bto be acquired\b", r"\bacquire[sd]?\b",
        r"\bmerger\b", r"\bacquisition\b", r"\btakeover\b",
    ],
    "ASSET_SALE": [
        r"\bsale of\b", r"\bdivestiture\b", r"\bsell(?:s|ing)? .*\bfor \$",
        r"\basset sale\b", r"\bstrategic sale\b",
    ],
    "MATERIAL_CONTRACT": [
        r"\bcontract worth\b", r"\b(?:multi|several)[ -]?billion\b",
        r"\bwins? (?:a )?(?:major|large|multi-year)? ?contract\b",
        r"\bawarded (?:a )?(?:contract|order)\b", r"\bselected (?:for|as)\b",
        r"\bmajor order\b", r"\bcommercial agreement\b",
    ],
    "REGULATORY": [
        r"\bfda (?:approval|approved|clearance|cleared)\b",
        r"\bema (?:approval|approved)\b", r"\bregulatory approval\b",
    ],
    "CLINICAL": [
        r"\bphase 3 .*?(?:met|meets|positive|success)\b",
        r"\bpositive phase 3\b", r"\bpivotal trial .*?positive\b",
    ],
    "EARNINGS_GUIDANCE": [
        r"\braises? guidance\b", r"\bincreases? guidance\b",
        r"\bbeats? .*? (?:and|with) .*?guidance\b",
        r"\bforecast .*?above\b",
    ],
    "BUYBACK": [r"\bshare repurchase\b", r"\bshare buyback\b", r"\bbuyback program\b"],
    # Allowed only with named partner + concrete implementation / dollar value.
    "CONCRETE_PARTNERSHIP": [
        r"\bstrategic partnership\b", r"\bcollaboration\b", r"\bpartnership\b",
        r"\buses? .*?(?:modules|technology|platform)\b",
    ],
}

RISK_OR_REJECTION_PATTERNS = [
    r"\bshare offering\b", r"\boffering of\b", r"\bdilution\b", r"\bat-the-market\b",
    r"\breverse split\b", r"\bgoing concern\b", r"\bbankruptcy\b", r"\bdelisting\b",
    r"\bdefault\b", r"\brestatement\b", r"\bmaterial weakness\b", r"\bclass action\b",
    r"\bsec investigation\b", r"\bshort report\b",
]
SPECULATION_PATTERNS = [
    r"\brumou?r\b", r"\breportedly\b", r"\bcould\b", r"\bmay\b", r"\bpotential\b",
    r"\bopinions?\b", r"\bcatalyst\b", r"\bspeculation\b", r"\bconsidering\b",
]
RECAP_PATTERNS = [
    r"\bwhy .*? stock\b", r"\bwhat to know\b", r"\bstock to watch\b",
    r"\bshares surge\b", r"\bstock jumps\b", r"\bafter .*? surge\b",
    r"\bopinion\b", r"\bexplained\b", r"\brecap\b",
]

# Google is discovery only. It feeds the official/media resolver; it cannot
# create a flash as an unverified Google-only snippet.
GOOGLE_QUERIES = [
    '"definitive agreement" stock',
    '"contract worth" OR "major contract" stock',
    '"FDA approval" OR "FDA clearance" stock',
    '"raises guidance" stock',
    '"sale of" OR divestiture stock',
    '"strategic partnership" "modules" stock',
]

GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]

# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def local_now() -> datetime:
    if ZoneInfo:
        return utc_now().astimezone(ZoneInfo("Europe/Amsterdam"))
    return utc_now()


def now_iso() -> str:
    return utc_now().isoformat()


def load_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default


def save_json(path: str, value: Any) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)


def trim_list(value: List[Any], keep: int) -> List[Any]:
    return value[-keep:] if isinstance(value, list) else []


def compact_text(value: Any, max_len: int = 3000) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()[:max_len]


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def domain(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower().replace("www.", "")
    except Exception:
        return ""


def parse_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        if isinstance(value, (float, int)):
            return float(value)
        return float(str(value).replace("$", "").replace(",", "").replace("%", "").replace("+", "").strip())
    except Exception:
        return None


def parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, time.struct_time):
        return datetime(*value[:6], tzinfo=timezone.utc)
    text = str(value).strip()
    try:
        dt = parsedate_to_datetime(text)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        normalized = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def as_iso(value: Optional[datetime]) -> str:
    return value.astimezone(timezone.utc).isoformat() if value else ""


def age_minutes(value: Optional[datetime]) -> Optional[int]:
    if not value:
        return None
    return max(0, int((utc_now() - value).total_seconds() // 60))


def regex_any(text: str, patterns: Iterable[str]) -> bool:
    return any(re.search(pattern, text, flags=re.I | re.S) for pattern in patterns)


def regex_count(text: str, patterns: Iterable[str]) -> int:
    return sum(1 for pattern in patterns if re.search(pattern, text, flags=re.I | re.S))


def valid_symbol(symbol: str) -> bool:
    return bool(re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", (symbol or "").upper()))


def extract_symbol(text: str) -> str:
    # Prefer an explicit ticker between brackets / parentheses / after "ticker".
    patterns = [r"\(([A-Z]{1,6})\)", r"\bticker\s*[:=-]?\s*([A-Z]{1,6})\b", r"\bNYSE\s*[:=-]?\s*([A-Z]{1,6})\b", r"\bNASDAQ\s*[:=-]?\s*([A-Z]{1,6})\b"]
    for pattern in patterns:
        hit = re.search(pattern, text or "", flags=re.I)
        if hit:
            candidate = hit.group(1).upper()
            if valid_symbol(candidate):
                return candidate
    return ""


def event_family(text: str) -> str:
    for name, patterns in EVENT_RULES.items():
        if regex_any(text, patterns):
            return name
    return ""


def has_numbered_detail(text: str) -> bool:
    return bool(re.search(r"(?:\$|€|£)\s?\d[\d,.]*(?:\s?(?:million|billion|m|bn))?|\b\d+(?:\.\d+)?\s?(?:million|billion|m|bn|years?)\b", text, flags=re.I))


def has_named_counterparty(text: str) -> bool:
    # Heuristic: formal deal language often contains "with/to/from <Proper Name>".
    return bool(re.search(r"\b(?:with|to|from|by)\s+[A-Z][A-Za-z0-9&.'-]+(?:\s+[A-Z][A-Za-z0-9&.'-]+){0,4}", text))


def normalize_event_phrase(text: str) -> str:
    clean = re.sub(r"\([^)]*\)", " ", text.lower())
    clean = re.sub(r"https?://\S+", " ", clean)
    clean = re.sub(r"[^a-z0-9 ]", " ", clean)
    stop = {
        "stock", "shares", "share", "company", "announces", "announced", "reports", "report",
        "after", "with", "from", "the", "and", "for", "into", "this", "that", "will", "has",
        "have", "its", "their", "today", "news", "update", "surge", "surges", "jump", "jumps",
    }
    words = [w for w in clean.split() if len(w) > 2 and w not in stop]
    return " ".join(words[:9])

# ---------------------------------------------------------------------------
# Logging and state
# ---------------------------------------------------------------------------
def log_filtered(log: List[Dict[str, Any]], candidate: Dict[str, Any], reason: str) -> None:
    log.append({
        "time": now_iso(), "reason": reason,
        "symbol": candidate.get("symbol", ""), "title": candidate.get("title", ""),
        "publisher": candidate.get("publisher", ""), "age_minutes": candidate.get("age_minutes"),
        "url": candidate.get("resolved_url") or candidate.get("url", ""),
    })


def log_source_health(source_health: Dict[str, Any], source: str, ok: bool, detail: str = "") -> None:
    row = source_health.setdefault(source, {"success": 0, "errors": 0, "last": "", "detail": ""})
    row["success" if ok else "errors"] = int(row.get("success", 0)) + 1
    row["last"] = now_iso()
    row["detail"] = detail[:250]

# ---------------------------------------------------------------------------
# HTTP and market data
# ---------------------------------------------------------------------------
def fmp_get(endpoint: str, params: Optional[Dict[str, Any]] = None) -> Any:
    query = dict(params or {})
    query["apikey"] = FMP_API_KEY
    response = requests.get(f"{FMP_BASE}/{endpoint}", params=query, headers=HTTP_HEADERS, timeout=20)
    response.raise_for_status()
    return response.json()


def fmp_quote(symbol: str) -> Dict[str, Any]:
    if not valid_symbol(symbol):
        return {}
    try:
        data = fmp_get("quote", {"symbol": symbol})
        if isinstance(data, list) and data:
            return data[0] or {}
        if isinstance(data, dict):
            return data
    except Exception as exc:
        print("quote failed", symbol, exc)
    return {}


def fmp_search_ticker(company_hint: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9 .&'-]", " ", company_hint or "").strip()
    if len(cleaned) < 3:
        return ""
    for endpoint in ("search-name", "search-ticker"):
        try:
            rows = fmp_get(endpoint, {"query": cleaned[:120], "limit": 5})
            if not isinstance(rows, list):
                continue
            for row in rows:
                ticker = str(row.get("symbol") or row.get("ticker") or "").upper().strip()
                exchange = str(row.get("exchangeShortName") or row.get("exchange") or "").upper()
                if valid_symbol(ticker) and exchange not in {"OTC", "PNK"}:
                    return ticker
        except Exception:
            continue
    return ""


def enrich_market(candidate: Dict[str, Any]) -> Dict[str, Any]:
    symbol = candidate.get("symbol") or ""
    quote = fmp_quote(symbol)
    if not quote:
        candidate["market"] = {"available": False}
        return candidate
    price = parse_float(quote.get("price"))
    volume = parse_float(quote.get("volume"))
    avg_volume = parse_float(quote.get("avgVolume"))
    change = parse_float(quote.get("changesPercentage") or quote.get("changePercentage"))
    candidate["market"] = {
        "available": True,
        "price": price,
        "market_cap": parse_float(quote.get("marketCap")),
        "volume": volume,
        "avg_volume": avg_volume,
        "relative_volume": round(volume / avg_volume, 2) if volume and avg_volume else None,
        "change_pct": change,
        "exchange": quote.get("exchange") or quote.get("exchangeShortName") or "",
    }
    return candidate

# ---------------------------------------------------------------------------
# Source collection
# ---------------------------------------------------------------------------
def source_score(candidate: Dict[str, Any]) -> int:
    source_kind = candidate.get("source_kind", "")
    joined = f"{candidate.get('publisher', '')} {domain(candidate.get('resolved_url') or candidate.get('url') or '')}".lower()
    if any(word in joined for word in BLOCKED_SOURCE_WORDS):
        return 0
    if source_kind in {"sec", "fmp_press_release"}:
        return 4
    if any(word in joined for word in OFFICIAL_SOURCE_WORDS):
        return 4
    if any(word in joined for word in TRUSTED_MEDIA_WORDS):
        return 3
    # Google must resolve to an actual publisher; unknown publisher gets 1 only.
    if source_kind in {"fmp_stock", "fmp_general", "google"}:
        return 1
    return 0


def add_candidate(rows: List[Dict[str, Any]], *, source_kind: str, publisher: str, title: str,
                  summary: str, url: str, published: Any, symbol: str = "", company: str = "") -> None:
    published_dt = parse_datetime(published)
    text = f"{title} {summary}"
    rows.append({
        "source_kind": source_kind,
        "publisher": compact_text(publisher, 160),
        "title": compact_text(title, 700),
        "summary": compact_text(summary, 2200),
        "url": url or "",
        "resolved_url": "",
        "published_at": as_iso(published_dt),
        "age_minutes": age_minutes(published_dt),
        "original_time_verified": source_kind in {"sec", "fmp_press_release", "fmp_stock", "fmp_general"},
        "symbol": (symbol or extract_symbol(text)).upper().strip(),
        "company": compact_text(company, 160),
        "event_family": event_family(text),
        "has_numbered_detail": has_numbered_detail(text),
        "has_named_counterparty": has_named_counterparty(text),
        "text": compact_text(text, 4000),
    })


def collect_fmp(endpoint: str, source_kind: str, label: str, limit: int, source_health: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        data = fmp_get(endpoint, {"page": 0, "limit": limit})
        log_source_health(source_health, source_kind, True, f"{len(data) if isinstance(data, list) else 0} rows")
    except Exception as exc:
        print(source_kind, "failed", exc)
        log_source_health(source_health, source_kind, False, str(exc))
        return out
    if not isinstance(data, list):
        return out
    for item in data:
        symbol = item.get("symbol") or item.get("symbols") or ""
        if isinstance(symbol, list):
            symbol = symbol[0] if symbol else ""
        add_candidate(
            out,
            source_kind=source_kind,
            publisher=item.get("site") or item.get("publisher") or label,
            title=item.get("title") or "",
            summary=item.get("text") or item.get("content") or item.get("summary") or "",
            url=item.get("url") or item.get("link") or "",
            published=item.get("publishedDate") or item.get("published_at") or item.get("date"),
            symbol=str(symbol or ""),
            company=item.get("company") or "",
        )
    return out


def collect_google(source_health: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        for query in GOOGLE_QUERIES:
            rss = "https://news.google.com/rss/search?q=" + quote_plus(query + " when:1h") + "&hl=en-US&gl=US&ceid=US:en"
            feed = feedparser.parse(rss)
            for entry in feed.entries[:10]:
                source = entry.get("source") or {}
                publisher = source.get("title") if isinstance(source, dict) else str(source)
                add_candidate(
                    out,
                    source_kind="google",
                    publisher=publisher or "Google News",
                    title=entry.get("title") or "",
                    summary=entry.get("summary") or "",
                    url=entry.get("link") or "",
                    published=entry.get("published_parsed") or entry.get("updated_parsed") or entry.get("published"),
                )
        log_source_health(source_health, "google", True, f"{len(out)} rows")
    except Exception as exc:
        print("google failed", exc)
        log_source_health(source_health, "google", False, str(exc))
    return out


def collect_sec(source_health: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    feeds = [
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&count=80&output=atom",
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=6-K&count=50&output=atom",
    ]
    try:
        for feed_url in feeds:
            response = requests.get(feed_url, headers=SEC_HEADERS, timeout=20)
            response.raise_for_status()
            feed = feedparser.parse(response.text)
            for entry in feed.entries[:50]:
                title = compact_text(entry.get("title") or "")
                summary = compact_text(entry.get("summary") or "")
                # SEC titles frequently include the company name after the form.
                company = re.sub(r"^\s*(?:8-K|6-K)\s*[-–:]?\s*", "", title, flags=re.I)
                add_candidate(
                    out,
                    source_kind="sec",
                    publisher="SEC.gov",
                    title=title,
                    summary=summary,
                    url=entry.get("link") or "",
                    published=entry.get("published_parsed") or entry.get("updated_parsed"),
                    symbol=extract_symbol(title + " " + summary),
                    company=company,
                )
        log_source_health(source_health, "sec", True, f"{len(out)} rows")
    except Exception as exc:
        print("SEC failed", exc)
        log_source_health(source_health, "sec", False, str(exc))
    return out


def resolve_article_metadata(candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve redirects and verify original publication time for non-primary feeds."""
    if candidate.get("source_kind") in {"sec", "fmp_press_release"}:
        candidate["source_score"] = source_score(candidate)
        return candidate
    url = candidate.get("url") or ""
    if not url:
        candidate["source_score"] = source_score(candidate)
        return candidate
    try:
        response = requests.get(url, headers=HTTP_HEADERS, timeout=14, allow_redirects=True)
        response.raise_for_status()
        candidate["resolved_url"] = response.url or url
        html = response.text[:350000]
    except Exception:
        candidate["source_score"] = source_score(candidate)
        return candidate

    # OpenGraph / Schema.org dates. Prefer a verifiable older date because a
    # newly republished article must never masquerade as a new event.
    patterns = [
        r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+name=["\'](?:date|publishdate|pub_date|datepublished)["\'][^>]+content=["\']([^"\']+)',
        r'"datePublished"\s*:\s*"([^"]+)"',
        r'"uploadDate"\s*:\s*"([^"]+)"',
    ]
    extracted = None
    for pattern in patterns:
        hit = re.search(pattern, html, flags=re.I)
        if hit:
            extracted = parse_datetime(hit.group(1))
            if extracted:
                break
    if extracted:
        current = parse_datetime(candidate.get("published_at"))
        if not current or extracted < current:
            candidate["published_at"] = as_iso(extracted)
            candidate["age_minutes"] = age_minutes(extracted)
        candidate["original_time_verified"] = True

    title_hit = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.I | re.S)
    if title_hit:
        candidate["resolved_title"] = compact_text(title_hit.group(1), 500)
    desc_hit = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)', html, flags=re.I)
    if desc_hit:
        candidate["page_description"] = compact_text(desc_hit.group(1), 1100)
    candidate["text"] = compact_text(
        f"{candidate.get('title', '')} {candidate.get('summary', '')} {candidate.get('resolved_title', '')} {candidate.get('page_description', '')}", 5000
    )
    candidate["event_family"] = event_family(candidate["text"]) or candidate.get("event_family", "")
    candidate["has_numbered_detail"] = has_numbered_detail(candidate["text"])
    candidate["has_named_counterparty"] = has_named_counterparty(candidate["text"])
    candidate["source_score"] = source_score(candidate)
    return candidate


def candidate_key(candidate: Dict[str, Any]) -> str:
    return sha("|".join([
        candidate.get("source_kind", ""), candidate.get("publisher", ""),
        candidate.get("title", ""), candidate.get("url", ""), candidate.get("published_at", ""),
    ]))


def unique_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    keys = set()
    for candidate in candidates:
        key = candidate_key(candidate)
        if key not in keys:
            keys.add(key)
            out.append(candidate)
    return out

# ---------------------------------------------------------------------------
# Hard quality gate before the first Telegram message
# ---------------------------------------------------------------------------
def fill_symbol(candidate: Dict[str, Any]) -> Dict[str, Any]:
    if valid_symbol(candidate.get("symbol", "")):
        return candidate
    hint = candidate.get("company") or candidate.get("title") or ""
    candidate["symbol"] = fmp_search_ticker(hint)
    return candidate


def pre_reason(candidate: Dict[str, Any]) -> str:
    text = candidate.get("text", "")
    age = candidate.get("age_minutes")
    source_kind = candidate.get("source_kind", "")
    score = int(candidate.get("source_score") or source_score(candidate))
    family = candidate.get("event_family") or event_family(text)

    if age is None:
        return "geen betrouwbare oorspronkelijke publicatietijd"
    if source_kind == "google" and not candidate.get("original_time_verified"):
        return "Google-result zonder bevestigde oorspronkelijke publicatietijd"
    if score <= 0:
        return "bron is opinie/recap of onvoldoende betrouwbaar"
    if regex_any(text, RISK_OR_REJECTION_PATTERNS):
        return "negatief corporate-risk signaal (offering/dilution/etc.)"
    if regex_any(text, RECAP_PATTERNS) and source_kind != "sec":
        return "recap/koersartikel in plaats van de oorspronkelijke gebeurtenis"
    if not family:
        return "geen materiële eventcategorie"
    if regex_any(text, SPECULATION_PATTERNS) and family == "CONCRETE_PARTNERSHIP":
        return "speculatieve partnership-taal zonder bevestigd commercieel feit"

    primary = score >= 4
    max_age = PRIMARY_MAX_AGE_MINUTES if primary else MEDIA_MAX_AGE_MINUTES
    if source_kind == "google":
        max_age = GOOGLE_MAX_AGE_MINUTES
    if age > max_age:
        return f"te oud voor fastlane ({age} min, limiet {max_age})"

    # A generic partnership only belongs in the fast lane with hard evidence.
    if family == "CONCRETE_PARTNERSHIP" and not (candidate.get("has_numbered_detail") or candidate.get("has_named_counterparty")):
        return "partnership zonder concrete tegenpartij/implementatiedetail"

    hard_families = {"M_AND_A", "ASSET_SALE", "MATERIAL_CONTRACT", "REGULATORY", "CLINICAL", "EARNINGS_GUIDANCE", "BUYBACK"}
    if family not in hard_families and score < 3:
        return "zachte catalyst zonder sterke bron"

    # Trusted media must show either a number, named counterparty, or a very
    # direct hard event. Primary sources are allowed without a number.
    if score == 3 and not (candidate.get("has_numbered_detail") or candidate.get("has_named_counterparty") or family in {"REGULATORY", "M_AND_A", "EARNINGS_GUIDANCE"}):
        return "media-item mist concrete financiële/contractuele details"
    if score < 3 and family != "CONCRETE_PARTNERSHIP":
        return "onvoldoende primaire of trusted bronbevestiging"

    return ""


def deterministic_score(candidate: Dict[str, Any]) -> Tuple[int, List[str]]:
    text = candidate.get("text", "")
    family = candidate.get("event_family") or ""
    score = int(candidate.get("source_score") or 0) * 2
    reasons = []
    if candidate.get("has_numbered_detail"):
        score += 2
        reasons.append("concrete financiële/omvangsdetail")
    if candidate.get("has_named_counterparty"):
        score += 1
        reasons.append("genoemde tegenpartij")
    if family in {"M_AND_A", "ASSET_SALE", "REGULATORY", "EARNINGS_GUIDANCE"}:
        score += 3
        reasons.append(f"materieel event: {family}")
    elif family in {"MATERIAL_CONTRACT", "CLINICAL"}:
        score += 2
        reasons.append(f"materieel event: {family}")
    elif family == "CONCRETE_PARTNERSHIP":
        score += 1
        reasons.append("concrete partnership/integratie")
    age = candidate.get("age_minutes")
    if age is not None and age <= 8:
        score += 2
        reasons.append("extreem vers")
    elif age is not None and age <= 18:
        score += 1
        reasons.append("vers")
    if regex_any(text, SPECULATION_PATTERNS):
        score -= 3
    return score, reasons


def event_key(candidate: Dict[str, Any]) -> str:
    day = (candidate.get("published_at") or now_iso())[:10]
    return sha("|".join([
        candidate.get("symbol") or "UNKNOWN",
        candidate.get("event_family") or "UNKNOWN",
        day,
        normalize_event_phrase(candidate.get("title") or candidate.get("text") or "")[:120],
    ]))


def is_duplicate_event(memory: Dict[str, Any], candidate: Dict[str, Any]) -> bool:
    key = event_key(candidate)
    row = memory.get(key)
    if not row:
        return False
    then = parse_datetime(row.get("first_alert_at"))
    return bool(then and utc_now() - then < timedelta(hours=EVENT_DEDUP_HOURS))


def late_market_reason(candidate: Dict[str, Any]) -> str:
    market = candidate.get("market") or {}
    change = abs(parse_float(market.get("change_pct")) or 0.0)
    age = candidate.get("age_minutes") or 0
    if change >= LATE_MOVE_PCT and age >= LATE_MOVE_MIN_AGE:
        return f"koers is al {change:.1f}% bewogen; fastlane te laat"
    price = parse_float(market.get("price"))
    cap = parse_float(market.get("market_cap"))
    if price is not None and price < MIN_PRICE_IF_KNOWN:
        return "prijs onder minimum voor fastlane"
    if cap is not None and cap < MIN_MARKET_CAP_IF_KNOWN:
        return "microcap onder minimum voor fastlane"
    return ""

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def telegram_request(method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    response = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}",
        json=payload,
        timeout=25,
    )
    try:
        body = response.json()
    except Exception:
        body = {"ok": False, "raw": response.text[:500]}
    if not response.ok or not body.get("ok"):
        print("Telegram error", method, response.status_code, body)
    return body


def send_telegram(text: str) -> Optional[int]:
    body = telegram_request("sendMessage", {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text[:4096],
        "disable_web_page_preview": True,
    })
    try:
        return int(body["result"]["message_id"])
    except Exception:
        return None


def edit_telegram(message_id: int, text: str) -> None:
    if not message_id:
        return
    telegram_request("editMessageText", {
        "chat_id": TELEGRAM_CHAT_ID,
        "message_id": message_id,
        "text": text[:4096],
        "disable_web_page_preview": True,
    })


def market_line(candidate: Dict[str, Any]) -> str:
    market = candidate.get("market") or {}
    if not market.get("available"):
        return "Marktdata: nu niet beschikbaar"
    bits = []
    if market.get("price") is not None:
        bits.append(f"prijs {market['price']}")
    if market.get("change_pct") is not None:
        bits.append(f"dag {market['change_pct']:+.2f}%")
    if market.get("relative_volume") is not None:
        bits.append(f"rel. volume {market['relative_volume']}x")
    return "Marktdata: " + (" | ".join(bits) if bits else "beschikbaar, maar onvolledig")


def first_flash_message(candidate: Dict[str, Any], score: int, reasons: List[str]) -> str:
    symbol = candidate.get("symbol") or "?"
    company = candidate.get("company") or ""
    source = candidate.get("publisher") or candidate.get("source_kind")
    age = candidate.get("age_minutes")
    family = (candidate.get("event_family") or "EVENT").replace("_", " ")
    why = "; ".join(reasons[:3]) or "verse, bron-gecontroleerde corporate catalyst"
    return (
        f"⚡ LIVE CATALYST — EERSTE SIGNAAL\n\n"
        f"{symbol}{' — ' + company if company else ''}\n"
        f"Type: {family}\n"
        f"Bron: {source} | leeftijd: {age if age is not None else '?'} min\n"
        f"Waarom hij door de fastlane komt: {why}.\n"
        f"{market_line(candidate)}\n\n"
        f"Wat gebeurde er:\n{candidate.get('title')}\n\n"
        f"Dit is een eerste bron-gecontroleerde melding, geen koopknop. Dezelfde melding wordt nu nog inhoudelijk gecheckt en bijgewerkt.\n"
        f"Link: {candidate.get('resolved_url') or candidate.get('url') or 'niet beschikbaar'}"
    )

# ---------------------------------------------------------------------------
# Secondary LLM review — edits the same message, never creates routine spam
# ---------------------------------------------------------------------------
def json_from_text(text: str) -> Dict[str, Any]:
    try:
        return json.loads(text)
    except Exception:
        hit = re.search(r"\{.*\}", text or "", flags=re.S)
        if hit:
            return json.loads(hit.group(0))
    raise ValueError("no JSON from Gemini")


def gemini_review(candidate: Dict[str, Any]) -> Dict[str, Any]:
    if not GEMINI_API_KEY:
        return {"verdict": "UNAVAILABLE", "summary_nl": "AI-review niet beschikbaar; beoordeel de primaire bron zelf.", "risk_nl": "Geen AI-review uitgevoerd.", "invalidation_nl": "Controleer officiële details en marktreactie."}

    prompt = f"""
Je beoordeelt één vers, bron-gecontroleerd corporate event voor een Nederlandse
belegger. Dit is research, geen koopadvies. Wees extreem streng en verzin geen
contractwaarde, goedkeuring of zekerheid.

Gegevens:
ticker={candidate.get('symbol')}
company={candidate.get('company')}
source={candidate.get('publisher')}
source_score={candidate.get('source_score')}
event={candidate.get('event_family')}
published_at={candidate.get('published_at')}
age_minutes={candidate.get('age_minutes')}
title={candidate.get('title')}
summary={candidate.get('summary')}
article_snippet={candidate.get('page_description','')}
market={json.dumps(candidate.get('market', {}), ensure_ascii=False)}

Geef alleen JSON met exact:
{{
  "verdict":"TRADEABLE|WAIT_FOR_CONFIRMATION|WATCH|REJECT",
  "confidence":1,
  "summary_nl":"max 2 zinnen, alleen feiten uit bron",
  "why_nl":"max 2 zinnen waarom dit economisch relevant kan zijn",
  "risk_nl":"max 2 zinnen met belangrijkste onbekende/risico",
  "invalidation_nl":"één concrete reden waarom dit geen trade is",
  "is_already_priced":false,
  "notes":"max 1 zin"
}}
Gebruik REJECT voor gerucht, oud nieuws, hype zonder economische details of een
nieuwsfeit dat de titel niet ondersteunt. Gebruik WAIT_FOR_CONFIRMATION wanneer
er nog geen contractwaarde, formele voorwaarden of officiële bevestiging is.
"""

    last_error: Optional[Exception] = None
    for model in GEMINI_MODELS:
        try:
            response = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                params={"key": GEMINI_API_KEY},
                headers={"Content-Type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.0, "response_mime_type": "application/json"},
                },
                timeout=35,
            )
            response.raise_for_status()
            raw = response.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json_from_text(raw)
        except Exception as exc:
            last_error = exc
    print("Gemini review failed", last_error)
    return {"verdict": "UNAVAILABLE", "confidence": 0, "summary_nl": "AI-review tijdelijk niet beschikbaar.", "why_nl": "De eerste melding is uitsluitend op bron en eventregels verstuurd.", "risk_nl": "Controleer de originele bron zelf.", "invalidation_nl": "Geen tweede review beschikbaar.", "is_already_priced": False, "notes": ""}


def reviewed_message(candidate: Dict[str, Any], review: Dict[str, Any], deterministic: int) -> str:
    verdict = str(review.get("verdict") or "WATCH")
    icons = {"TRADEABLE": "🟢", "WAIT_FOR_CONFIRMATION": "🟡", "WATCH": "🔵", "REJECT": "⚠️", "UNAVAILABLE": "⚪"}
    icon = icons.get(verdict, "🔵")
    status = verdict.replace("_", " ")
    symbol = candidate.get("symbol") or "?"
    company = candidate.get("company") or ""
    age = candidate.get("age_minutes")
    source = candidate.get("publisher") or candidate.get("source_kind")

    if verdict == "REJECT":
        header = f"⚠️ LIVE CATALYST — REVIEW INGETROKKEN"
    else:
        header = f"{icon} LIVE CATALYST — {status}"

    return (
        f"{header}\n\n"
        f"{symbol}{' — ' + company if company else ''}\n"
        f"Bron: {source} | origineel nieuws: {age if age is not None else '?'} min oud\n"
        f"{market_line(candidate)}\n"
        f"Fastlane-score: {deterministic}/14 | review-confidence: {review.get('confidence', '?')}/10\n\n"
        f"Feiten:\n{review.get('summary_nl', '')}\n\n"
        f"Waarom relevant:\n{review.get('why_nl', '')}\n\n"
        f"Risico / onbekend:\n{review.get('risk_nl', '')}\n\n"
        f"Wanneer niet doen:\n{review.get('invalidation_nl', '')}\n\n"
        f"Titel: {candidate.get('title')}\n"
        f"Link: {candidate.get('resolved_url') or candidate.get('url') or 'niet beschikbaar'}\n\n"
        f"Researchsignaal, geen koopadvies."
    )

# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------
def prioritize(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def key(c: Dict[str, Any]) -> Tuple[int, int, int]:
        score = int(c.get("source_score") or 0)
        fresh = -int(c.get("age_minutes") if c.get("age_minutes") is not None else 99999)
        event_bonus = 1 if c.get("event_family") in {"M_AND_A", "ASSET_SALE", "REGULATORY", "EARNINGS_GUIDANCE"} else 0
        return score, event_bonus, fresh
    return sorted(candidates, key=key, reverse=True)


def append_latency(latency_log: List[Dict[str, Any]], candidate: Dict[str, Any]) -> None:
    latency_log.append({
        "time": now_iso(), "symbol": candidate.get("symbol"),
        "publisher": candidate.get("publisher"), "source_kind": candidate.get("source_kind"),
        "event_family": candidate.get("event_family"), "news_age_minutes": candidate.get("age_minutes"),
        "original_time_verified": candidate.get("original_time_verified", False),
    })


def append_alert(alerts: List[Dict[str, Any]], candidate: Dict[str, Any], review: Dict[str, Any], score: int) -> None:
    market = candidate.get("market") or {}
    alerts.append({
        "time": now_iso(), "symbol": candidate.get("symbol"), "company": candidate.get("company"),
        "event_key": event_key(candidate), "event_family": candidate.get("event_family"),
        "title": candidate.get("title"), "publisher": candidate.get("publisher"),
        "url": candidate.get("resolved_url") or candidate.get("url"),
        "news_age_minutes": candidate.get("age_minutes"), "price_at_alert": market.get("price"),
        "day_change_pct": market.get("change_pct"), "relative_volume": market.get("relative_volume"),
        "fastlane_score": score, "review": review,
    })


def update_performance(performance: List[Dict[str, Any]], alerts: List[Dict[str, Any]]) -> None:
    # Update 1/3/7 day return snapshots only. This is learning data, not a
    # claim that a signal "worked" after one price tick.
    now = utc_now()
    existing = {row.get("event_key"): row for row in performance if isinstance(row, dict)}
    for alert in alerts[-300:]:
        key = alert.get("event_key")
        symbol = alert.get("symbol")
        start = parse_float(alert.get("price_at_alert"))
        created = parse_datetime(alert.get("time"))
        if not key or not valid_symbol(symbol or "") or not start or not created:
            continue
        row = existing.setdefault(key, {"event_key": key, "symbol": symbol, "start_price": start, "created": alert.get("time")})
        for days in (1, 3, 7):
            field = f"return_{days}d"
            if row.get(field) is not None or now - created < timedelta(days=days):
                continue
            quote = fmp_quote(symbol)
            price = parse_float(quote.get("price")) if quote else None
            if price:
                row[field] = round(((price - start) / start) * 100, 2)
                row[f"checked_{days}d"] = now_iso()
    performance[:] = list(existing.values())[-2000:]


def run_scan(state: Dict[str, Any]) -> None:
    seen: Dict[str, Any] = state["seen"]
    memory: Dict[str, Any] = state["memory"]
    alerts: List[Dict[str, Any]] = state["alerts"]
    filtered: List[Dict[str, Any]] = state["filtered"]
    signals: List[Dict[str, Any]] = state["signals"]
    latency: List[Dict[str, Any]] = state["latency"]
    source_health: Dict[str, Any] = state["source_health"]

    all_candidates: List[Dict[str, Any]] = []
    # Primary lane first: official releases and SEC filings.
    all_candidates += collect_fmp("news/press-releases-latest", "fmp_press_release", "FMP Press Releases", 60, source_health)
    all_candidates += collect_sec(source_health)
    # Strong discovery sources second.
    all_candidates += collect_fmp("news/stock-latest", "fmp_stock", "FMP Stock News", 70, source_health)
    all_candidates += collect_fmp("news/general-latest", "fmp_general", "FMP General News", 50, source_health)
    all_candidates += collect_google(source_health)

    candidates = unique_candidates(all_candidates)
    print("raw candidates", len(candidates))

    shortlisted: List[Dict[str, Any]] = []
    for candidate in candidates:
        cid = candidate_key(candidate)
        if cid in seen:
            continue
        seen[cid] = {"first_seen": now_iso(), "title": candidate.get("title"), "publisher": candidate.get("publisher")}
        candidate["source_score"] = source_score(candidate)

        # Resolve only items that look even remotely like a material event.
        text = candidate.get("text", "")
        if not candidate.get("event_family") or regex_any(text, RISK_OR_REJECTION_PATTERNS):
            log_filtered(filtered, candidate, "geen relevante catalyst of negatief corporate risk")
            continue
        candidate = resolve_article_metadata(candidate)
        candidate = fill_symbol(candidate)
        if not valid_symbol(candidate.get("symbol", "")):
            log_filtered(filtered, candidate, "ticker kon niet betrouwbaar worden gekoppeld")
            continue
        candidate = enrich_market(candidate)

        reason = pre_reason(candidate)
        if reason:
            log_filtered(filtered, candidate, reason)
            continue
        late = late_market_reason(candidate)
        if late:
            log_filtered(filtered, candidate, late)
            continue
        if is_duplicate_event(memory, candidate):
            log_filtered(filtered, candidate, "zelfde event al gemeld; event-level dedupe")
            continue

        score, reasons = deterministic_score(candidate)
        # Threshold: no routine alert below this. Partnership cases need more
        # evidence because the market is full of vague AI tie-up headlines.
        threshold = 9 if candidate.get("event_family") == "CONCRETE_PARTNERSHIP" else 8
        if score < threshold:
            log_filtered(filtered, candidate, f"fastlane-score te laag ({score} < {threshold})")
            continue
        candidate["fastlane_score"] = score
        candidate["fastlane_reasons"] = reasons
        shortlisted.append(candidate)

    # No daily cap and no 90-minute gap. New *independent* material events may
    # all alert. Limit AI follow-up, not the actual event collection.
    shortlisted = prioritize(shortlisted)
    print("qualified first flashes", len(shortlisted))

    ai_count = 0
    for candidate in shortlisted:
        key = event_key(candidate)
        score = int(candidate.get("fastlane_score") or 0)
        message_id = send_telegram(first_flash_message(candidate, score, candidate.get("fastlane_reasons") or []))
        memory[key] = {
            "first_alert_at": now_iso(), "symbol": candidate.get("symbol"),
            "title": candidate.get("title"), "event_family": candidate.get("event_family"),
        }
        append_latency(latency, candidate)

        if ai_count < MAX_AI_FOLLOWUPS:
            review = gemini_review(candidate)
            ai_count += 1
        else:
            review = {"verdict": "UNAVAILABLE", "confidence": 0, "summary_nl": "Secondaire review niet uitgevoerd wegens drukte.", "why_nl": "De bron- en eventregels zijn wel gehaald.", "risk_nl": "Controleer details in de originele bron.", "invalidation_nl": "Geen tweede review beschikbaar."}

        signals.append({"time": now_iso(), "symbol": candidate.get("symbol"), "title": candidate.get("title"), "score": score, "review": review})
        if message_id:
            edit_telegram(message_id, reviewed_message(candidate, review, score))
        append_alert(alerts, candidate, review, score)


def save_state(state: Dict[str, Any]) -> None:
    save_json(SEEN_FILE, dict(list(state["seen"].items())[-8000:]))
    save_json(EVENT_MEMORY_FILE, dict(list(state["memory"].items())[-3000:]))
    save_json(ALERTS_LOG_FILE, trim_list(state["alerts"], 3000))
    save_json(FILTERED_LOG_FILE, trim_list(state["filtered"], 12000))
    save_json(SIGNALS_LOG_FILE, trim_list(state["signals"], 3000))
    save_json(PERFORMANCE_LOG_FILE, trim_list(state["performance"], 2000))
    save_json(LATENCY_LOG_FILE, trim_list(state["latency"], 5000))
    save_json(SOURCE_HEALTH_FILE, state["source_health"])


def require_secrets() -> None:
    missing = [name for name, value in {
        "FMP_API_KEY": FMP_API_KEY,
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
    }.items() if not value]
    if missing:
        raise RuntimeError("Missing GitHub Secrets: " + ", ".join(missing))


def main() -> None:
    require_secrets()
    print("V4 Catalyst Fastlane start", now_iso(), "mode", RUN_MODE)
    if TEST_MESSAGE or RUN_MODE == "test":
        send_telegram(
            "✅ V4 Catalyst Fastlane test gelukt.\n\n"
            "Deze versie heeft geen daglimiet en geen 90-minutenregel. Hij zoekt eerst "
            "officiële persberichten/SEC, controleert oorspronkelijke publicatietijd en "
            "stuurt alleen onafhankelijke verse corporate catalysts."
        )
        return

    state = {
        "seen": load_json(SEEN_FILE, {}),
        "memory": load_json(EVENT_MEMORY_FILE, {}),
        "alerts": load_json(ALERTS_LOG_FILE, []),
        "filtered": load_json(FILTERED_LOG_FILE, []),
        "signals": load_json(SIGNALS_LOG_FILE, []),
        "performance": load_json(PERFORMANCE_LOG_FILE, []),
        "latency": load_json(LATENCY_LOG_FILE, []),
        "source_health": load_json(SOURCE_HEALTH_FILE, {}),
    }
    update_performance(state["performance"], state["alerts"])
    run_scan(state)
    save_state(state)
    print("V4 Catalyst Fastlane done")


if __name__ == "__main__":
    main()
