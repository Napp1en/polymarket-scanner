import os
import re
import json
import time
from collections import Counter
import requests
import pandas as pd
import gspread
from datetime import datetime
from google.oauth2.service_account import Credentials
import psycopg2
from psycopg2.extras import execute_values

DATABASE_URL = os.getenv("DATABASE_URL")

# =====================
# Einstellungen
# =====================

BANKROLL = 100
TOP_N = 5
MIN_RAW_ROI = 0.05
MIN_HISTORY_ROI = 5.0

# Gamma liefert max. 100 Events pro Seite.
EVENT_LIMIT = 100
# Max. Seiten pro Discovery-Abfrage (Tag-Abfragen brauchen aktuell 1-4 Seiten).
MAX_PAGES = 10
# Zusätzlicher Sicherheits-Scan: Top-Events nach 24h-Volumen (fängt Events ohne passende Tags).
GENERAL_SCAN_PAGES = 5

EVENTS_URL = "https://gamma-api.polymarket.com/events"
# /events akzeptiert offset nur bis 2000 (danach HTTP 422) -> Keyset-Pagination nutzen.
EVENTS_KEYSET_URL = "https://gamma-api.polymarket.com/events/keyset"
MAX_OFFSET = 2000

# Gamma-Tag "Games" markiert Einzelspiele (Match-Märkte) – für Turnier-Discovery irrelevant.
GAMES_TAG_ID = 100639
DISCOVERY_TAG_SLUGS = [
    "counter-strike-2",
    "cs2",
    "league-of-legends",
    "valorant",
    "esports",
    "soccer",
]

# Ein negRisk-Event, dessen handelbare Outcomes zusammen weniger als diesen Wert kosten,
# während noch Platzhalter / Märkte ohne Preis offen sind, ist unvollständig: die fehlende
# Wahrscheinlichkeit liegt bei nicht handelbaren Outcomes, der Basket-ROI wäre Fantasie.
MIN_LIVE_PROBABILITY_SUM = 0.90
# Ab diesem YES-Preis gilt ein geschlossener Markt als "YES aufgelöst" (Event entschieden).
RESOLVED_YES_PRICE = 0.98

SHEET_ID = os.getenv("SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

# Fallback-Keywords (Wortgrenzen) auf Titel / Slug / Serie / Tags – NICHT auf die Beschreibung,
# dort erzeugen Kürzel wie "iem" oder "pgl" massenhaft Fehltreffer ("Riemann", ...).
CATEGORIES = {
    "CS2": [
        "counter-strike", "counter strike", "cs2", "csgo", "cs:go", "pgl major",
        "blast premier", "blast rivals", "blast open", "blast bounty", "iem",
        "esl pro league", "fissure"
    ],
    "LoL": [
        "league of legends",
        "world championship league of legends",
        "mid-season invitational",
        "msi league of legends",
        "lck winner",
        "lec winner",
        "lpl winner",
        "lol worlds",
        "lck", "lec", "lpl",
    ],
    "Valorant": [
        "valorant", "vct", "valorant champions", "masters valorant"
    ],
    "Fußball": [
        "champions league winner",
        "champions league champion",
        "europa league winner",
        "premier league winner",
        "bundesliga winner",
        "la liga winner",
        "serie a winner",
        "world cup winner",
        "euro winner",
        "soccer", "uefa", "fifa", "champions league", "europa league",
        "conference league", "premier league", "epl", "bundesliga", "la liga",
        "laliga", "serie a", "ligue 1", "eredivisie", "primeira liga", "mls cup",
        "copa libertadores", "copa america", "afcon", "dfb-pokal", "fa cup",
    ],
}

# Primäre Kategorie-Erkennung über Gamma-Tags (exakte Slugs bzw. Präfixe).
CATEGORY_TAGS = {
    "CS2": {"counter-strike-2", "counter-strike", "cs2", "csgo"},
    "LoL": {"league-of-legends"},
    "Valorant": {"valorant"},
    "Fußball": {
        "soccer", "ucl", "uefa-champions-league", "champions-league",
        "europa-league", "uefa-europa-league", "uefa-conference-league",
        "epl", "premier-league", "bundesliga", "la-liga", "serie-a",
        "fifa-world-cup",
    },
}
CATEGORY_TAG_PREFIXES = {
    "LoL": ("lol-",),
}

# Andere Sportarten, die Fußball-Keywords enthalten ("Rugby League World Cup",
# "Basketball Champions League", "T20 ... Champions League").
NON_SOCCER_KEYWORDS = [
    "rugby", "cricket", "t20", "basketball", "euroleague", "hockey", "handball",
    "volleyball", "nfl", "nba", "nhl", "mlb", "lacrosse",
]

# Dota 2 bleibt komplett ausgeschlossen (Tags + Keywords), egal welche Kategorie sonst passt.
DOTA_TAGS = {"dota-2", "dota", "dota2"}
DOTA_KEYWORDS = ["dota", "dota 2", "the international", "dreamleague", "riyadh masters"]

# EXCLUDE_KEYWORDS und TOURNAMENT_WORDS werden (mit Wortgrenzen) nur gegen den
# Event-Titel geprüft. Auf der langen Beschreibung trifft z.B. "group " fast immer.
EXCLUDE_KEYWORDS = [
    " vs ",
    "bo1",
    "bo2",
    "bo3",
    "bo5",
    "matchup",
    "map ",
    "group ",
    "playoffs",
    "qualifier",
    "round ",
]

TOURNAMENT_WORDS = [
    "winner",
    "champion",
    "who will win",
    "to win",
    "win the",
    "championship",
    "tournament",
    "major",
    "finals",
    "world cup",
    "worlds",
    "league title",
]


# =====================
# Helpers
# =====================

def parse_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return []
    return []


def to_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def normalize_text(*parts):
    text = " ".join(str(p) for p in parts if p).lower()
    return f"{text} {text.replace('-', ' ')}"


_KEYWORD_PATTERNS = {}


def contains_keyword(text, keyword):
    kw = keyword.strip().lower()
    pattern = _KEYWORD_PATTERNS.get(kw)
    if pattern is None:
        pattern = re.compile(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])")
        _KEYWORD_PATTERNS[kw] = pattern
    return pattern.search(text) is not None


def event_tags(event):
    return [tag for tag in (event.get("tags") or []) if isinstance(tag, dict)]


def event_tag_slugs(event):
    return {str(tag.get("slug", "")).lower() for tag in event_tags(event) if tag.get("slug")}


def event_text(event):
    tag_text = " ".join(
        f"{tag.get('label', '')} {tag.get('slug', '')}"
        for tag in event_tags(event)
    )

    return normalize_text(
        event.get("title", ""),
        event.get("slug", ""),
        event.get("seriesSlug", ""),
        event.get("category", ""),
        event.get("subcategory", ""),
        tag_text,
    )


def is_dota_event(event):
    if event_tag_slugs(event) & DOTA_TAGS:
        return True
    text = normalize_text(event.get("title", ""), event.get("slug", ""), event.get("seriesSlug", ""))
    return any(contains_keyword(text, k) for k in DOTA_KEYWORDS)


def get_event_category(event):
    if is_dota_event(event):
        return None

    slugs = event_tag_slugs(event)

    for category, tags in CATEGORY_TAGS.items():
        prefixes = CATEGORY_TAG_PREFIXES.get(category, ())
        if slugs & tags or (prefixes and any(s.startswith(prefixes) for s in slugs)):
            return category

    text = event_text(event)

    for category, keywords in CATEGORIES.items():
        if category == "Fußball" and any(contains_keyword(text, k) for k in NON_SOCCER_KEYWORDS):
            continue
        if any(contains_keyword(text, k) for k in keywords):
            return category

    return None


def get_tournament_rejection(event):
    """None, wenn das Event ein Turnier-Markt ist, sonst der Ausschlussgrund."""
    tag_ids = {str(tag.get("id")) for tag in event_tags(event)}
    if str(GAMES_TAG_ID) in tag_ids or "games" in event_tag_slugs(event):
        return "Einzelspiel (Gamma-Tag 'Games')"

    title = normalize_text(event.get("title", ""))

    for keyword in EXCLUDE_KEYWORDS:
        if contains_keyword(title, keyword):
            return f"Ausschluss-Keyword im Titel: '{keyword.strip()}'"

    if not any(contains_keyword(title, k) for k in TOURNAMENT_WORDS):
        return "kein Turnier-Keyword im Titel"

    return None


# =====================
# Diagnose
# =====================

REJECT_INVALID_PRICES = "invalid_prices"
REJECT_INCOMPLETE = "incomplete"
REJECT_BELOW_ROI = "below_roi"
REJECT_OTHER = "other"

MAX_REJECTION_LOG_LINES = 300


class ScanStats:
    def __init__(self):
        self.api_events_loaded = 0
        self.unique_events = 0
        self.discovery_queries = 0
        self.discovery_errors = []
        self.dota_excluded = 0
        self.category_matches = Counter()
        self.non_tournament = Counter()
        self.tournament_events = Counter()
        self.rejections = Counter()
        self.rejected_events = []
        self.market_exclusions = Counter()
        self.opportunity_events = 0
        self.opportunity_rows = 0
        self.guaranteed_events = 0
        self.full_set_arbitrage_events = 0
        self.sheet_rows_written = None
        self.duration_seconds = None

    def reject(self, category, event, group, reason):
        self.rejections[group] += 1
        self.rejected_events.append((category, event.get("title"), event.get("slug"), reason))

    def summary_lines(self):
        lines = [
            "========== SCANNER DEBUG ==========",
            "",
            f"API Events Loaded: {self.api_events_loaded} ({self.discovery_queries} Discovery-Abfragen)",
            f"Unique Events: {self.unique_events}",
            f"Discovery Errors: {len(self.discovery_errors)}",
        ]
        lines += [f"  - {err}" for err in self.discovery_errors]
        lines += [
            f"Dota 2 Excluded: {self.dota_excluded}",
            f"Category Matches (vor Turnier-Filter): {sum(self.category_matches.values())}",
            f"Not Tournament Markets: {sum(self.non_tournament.values())}",
        ]
        lines += [f"  - {reason}: {n}" for reason, n in self.non_tournament.most_common()]
        lines += [
            "",
            f"Tournament Events Detected: {sum(self.tournament_events.values())}",
            "",
        ]
        lines += [f"{cat}: {self.tournament_events.get(cat, 0)}" for cat in CATEGORIES]
        lines += [
            "",
            "Excluded Markets: " + (", ".join(
                f"{status}={n}" for status, n in sorted(self.market_exclusions.items())
            ) or "0"),
            f"Events With Invalid Prices: {self.rejections[REJECT_INVALID_PRICES]}",
            f"Events With Incomplete Outcomes: {self.rejections[REJECT_INCOMPLETE]}",
            f"Events Below ROI Threshold: {self.rejections[REJECT_BELOW_ROI]}",
            f"Events Excluded By Other Filters: {self.rejections[REJECT_OTHER]}",
            "",
            f"Valid Opportunities: {self.opportunity_events} Events / {self.opportunity_rows} Zeilen",
            f"  davon vollständiges Set (garantiert, theoretisch): {self.guaranteed_events}",
            f"Full-Set Arbitrage (alle Outcomes < 1, Set vollständig): {self.full_set_arbitrage_events}",
            "Google Sheets Rows Written: "
            + ("nicht geschrieben" if self.sheet_rows_written is None else str(self.sheet_rows_written)),
        ]
        if self.duration_seconds is not None:
            lines.append(f"Scan-Dauer: {self.duration_seconds:.1f}s")
        lines += ["", "==================================="]
        return lines

    def print_rejections(self):
        if not self.rejected_events:
            return
        print(f"Abgelehnte Turnier-Events ({len(self.rejected_events)}):", flush=True)
        for category, title, slug, reason in self.rejected_events[:MAX_REJECTION_LOG_LINES]:
            print(f"  [REJECT] {category} | {title} ({slug}) | {reason}", flush=True)
        hidden = len(self.rejected_events) - MAX_REJECTION_LOG_LINES
        if hidden > 0:
            print(f"  ... {hidden} weitere ausgeblendet", flush=True)

    def print_summary(self):
        print("\n".join(self.summary_lines()), flush=True)


# =====================
# Gamma API
# =====================

HTTP = requests.Session()
# /events-Seiten mit limit > 50 liefert Gamma nur gzip-komprimiert aus.
HTTP.headers.update({"Accept-Encoding": "gzip, deflate"})


def gamma_get(url, params, timeout=30, retries=2):
    for attempt in range(retries + 1):
        try:
            response = HTTP.get(url, params=params, timeout=timeout)
        except requests.RequestException:
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise

        if response.status_code in (429, 500, 502, 503, 504) and attempt < retries:
            time.sleep(2 ** attempt)
            continue

        return response


def fetch_event_query(label, params, limit, max_pages, stats):
    """Lädt eine Discovery-Abfrage per Keyset-Pagination (Fallback: offset bis MAX_OFFSET).

    Gibt (events, ok) zurück; ok=False bei API-/Netzwerkfehler.
    """
    events = []
    cursor = None
    offset = 0
    use_keyset = True

    for page in range(max_pages):
        if use_keyset:
            url = EVENTS_KEYSET_URL
            query = {**params, "closed": "false", "limit": limit}
            if cursor:
                query["after_cursor"] = cursor
        else:
            if offset > MAX_OFFSET:
                break
            url = EVENTS_URL
            query = {**params, "active": "true", "closed": "false", "limit": limit, "offset": offset}

        try:
            response = gamma_get(url, query)
            if response.status_code != 200:
                if use_keyset and page == 0:
                    print(
                        f"  {label}: Keyset-Endpoint Fehler {response.status_code} – "
                        f"Fallback auf offset-Pagination",
                        flush=True,
                    )
                    use_keyset = False
                    response = gamma_get(EVENTS_URL, {
                        **params, "active": "true", "closed": "false", "limit": limit, "offset": 0,
                    })
                if response.status_code != 200:
                    raise ValueError(f"HTTP {response.status_code}: {response.text[:300]}")
            data = response.json()
        except (requests.RequestException, ValueError) as e:
            stats.discovery_errors.append(f"{label} (Seite {page + 1}): {e}")
            print(f"  Gamma API Fehler bei {label}, Seite {page + 1}: {e}", flush=True)
            return events, False

        if isinstance(data, dict):
            batch = data.get("events") or []
            cursor = data.get("next_cursor")
        else:
            batch = data if isinstance(data, list) else []
            cursor = None

        events.extend(batch)

        if not batch or len(batch) < limit:
            break
        if use_keyset and not cursor:
            break
        offset += limit

    return events, True


def get_events(limit=EVENT_LIMIT, max_pages=MAX_PAGES, stats=None):
    """Turnier-Discovery: Tag-Abfragen (ohne Einzelspiele) + Top-Events nach 24h-Volumen.

    Ein ungefilterter offset-Scan sieht nur die ältesten ~2000 der >19.000 offenen Events
    (Gamma sortiert ohne `order` nach ID, offset > 2000 liefert HTTP 422).
    """
    stats = stats if stats is not None else ScanStats()

    queries = [
        (f"Tag '{slug}'", {"tag_slug": slug, "exclude_tag_id": GAMES_TAG_ID}, max_pages)
        for slug in DISCOVERY_TAG_SLUGS
    ]
    queries.append(("Top 24h-Volumen", {"order": "volume24hr", "ascending": "false"}, GENERAL_SCAN_PAGES))

    events_by_id = {}
    successful = 0

    for label, params, pages in queries:
        batch, ok = fetch_event_query(label, params, limit, pages, stats)
        stats.discovery_queries += 1
        stats.api_events_loaded += len(batch)
        successful += int(ok)

        new = 0
        for event in batch:
            event_id = event.get("id") or event.get("slug")
            if event_id and event_id not in events_by_id:
                events_by_id[event_id] = event
                new += 1

        print(f"  {label}: {len(batch)} Events ({new} neu)", flush=True)

    if successful == 0:
        raise RuntimeError(
            "Gamma API: keine Discovery-Abfrage erfolgreich – Abbruch, "
            "damit bestehende Google-Sheets-Daten nicht überschrieben werden."
        )

    events = list(events_by_id.values())
    stats.unique_events = len(events)

    print(f"Insgesamt eindeutige Events geladen: {len(events)}", flush=True)

    return events

def fetch_event_by_slug(slug):
    for closed in ["false", "true"]:
        params = {
            "slug": slug,
            "closed": closed,
            "limit": 1,
        }

        try:
            r = requests.get(EVENTS_URL, params=params, timeout=20)
            r.raise_for_status()
            data = r.json()

            if isinstance(data, list):
                for e in data:
                    if e.get("slug") == slug:
                        return e
            elif isinstance(data, dict) and data.get("slug") == slug:
                return data

        except Exception as e:
            print(f"Fehler beim Laden von Event {slug}: {e}")

    return None


def get_yes_price(market):
    outcomes = parse_list(market.get("outcomes"))
    prices = parse_list(market.get("outcomePrices"))

    if not outcomes or not prices:
        return None

    for outcome, price in zip(outcomes, prices):
        if str(outcome).strip().lower() == "yes":
            price = to_float(price)
            if price is not None and 0 <= price <= 1:
                return price
            return None

    return None


ARB_GUARANTEED = "Vollständig – garantiert (theoretisch)"
ARB_ALL_LIVE = "Alle handelbaren Outcomes – Platzhalter offen (nicht garantiert)"
ARB_SUBSET = f"Top {TOP_N} Teilmenge – nicht garantiert"

MARKET_LIVE = "live"
MARKET_CLOSED = "geschlossen"
MARKET_PLACEHOLDER = "platzhalter_inaktiv"
MARKET_NOT_ACCEPTING = "keine_orders"
MARKET_INVALID_PRICE = "ungueltiger_preis"
MARKET_UNPRICED = "ohne_preis"
MARKET_OTHER_UNSTABLE = "other_variabel"

UNTRADABLE_STATUSES = (
    MARKET_PLACEHOLDER, MARKET_NOT_ACCEPTING, MARKET_INVALID_PRICE, MARKET_UNPRICED,
    MARKET_OTHER_UNSTABLE,
)


def has_price_discovery(market):
    """False für aktive Märkte ohne Bid, ohne Trade und ohne Volumen.

    Gamma meldet dort den Mittelwert des leeren Orderbuchs (z.B. 0.495) als outcomePrice.
    """
    volume = to_float(market.get("volumeNum"))
    if volume is None:
        volume = to_float(market.get("volume"))
    return any(
        (value or 0) > 0
        for value in (
            to_float(market.get("bestBid")),
            to_float(market.get("lastTradePrice")),
            volume,
        )
    )


def classify_market(market):
    price = get_yes_price(market)

    if market.get("closed") is True:
        return MARKET_CLOSED, price
    # Inaktive negRisk-Platzhalter ("Other", "Team H", ...) haben den Dummy-Preis 0.5.
    if market.get("active") is False:
        return MARKET_PLACEHOLDER, price
    if market.get("acceptingOrders") is False:
        return MARKET_NOT_ACCEPTING, price
    if price is None:
        return MARKET_INVALID_PRICE, None
    if not has_price_discovery(market):
        return MARKET_UNPRICED, price
    return MARKET_LIVE, price


def is_mutually_exclusive(event):
    if event.get("negRisk") is True:
        return True
    markets = event.get("markets") or []
    return bool(markets) and all(m.get("negRisk") is True for m in markets)


def market_name(market):
    return market.get("question") or market.get("title") or market.get("slug")


def evaluate_event(event, top_n=TOP_N, min_roi=MIN_RAW_ROI):
    """Bewertet ein Turnier-Event anhand der theoretischen YES-Preise (outcomePrices).

    Ergebnis-Dict: ok, group, reason, market_counts und bei ok zusätzlich
    top_teams, top_sum, roi, live_count, untradable_count, full_sum, arbitrage_type.
    """
    result = {"ok": False, "group": REJECT_OTHER, "reason": "", "market_counts": Counter()}

    if not is_mutually_exclusive(event):
        result["reason"] = (
            "Outcomes nicht gegenseitig ausschließend (negRisk=false) – "
            "Summe der YES-Preise ergibt keinen Arbitrage-Basket"
        )
        return result

    counts = result["market_counts"]
    live = []
    resolved_yes = None
    seen_markets = set()

    for market in event.get("markets") or []:
        market_key = market.get("id") or market.get("conditionId") or market.get("slug")
        if market_key in seen_markets:
            continue
        seen_markets.add(market_key)

        status, price = classify_market(market)
        counts[status] += 1

        if status == MARKET_CLOSED and price is not None and price >= RESOLVED_YES_PRICE:
            resolved_yes = market
        elif status == MARKET_LIVE:
            live.append({"team": market_name(market), "price": price, "market": market})

    if resolved_yes is not None:
        result["reason"] = f"Event bereits entschieden: {market_name(resolved_yes)}"
        return result

    # Augmented negRisk: solange Platzhalter offen sind, ändert sich die Definition des
    # expliziten "Other"-Outcomes, wenn Platzhalter benannt werden (Polymarket-Doku).
    if counts[MARKET_PLACEHOLDER]:
        unstable = [t for t in live if t["market"].get("negRiskOther") is True]
        if unstable:
            live = [t for t in live if t["market"].get("negRiskOther") is not True]
            counts[MARKET_LIVE] -= len(unstable)
            counts[MARKET_OTHER_UNSTABLE] += len(unstable)

    untradable = sum(counts[s] for s in UNTRADABLE_STATUSES)

    if not live:
        result["group"] = REJECT_INVALID_PRICES
        result["reason"] = (
            f"keine handelbaren Outcomes mit gültigem YES-Preis "
            f"({counts[MARKET_PLACEHOLDER]} Platzhalter, {counts[MARKET_UNPRICED]} ohne Preis, "
            f"{counts[MARKET_INVALID_PRICE]} ungültig, {counts[MARKET_CLOSED]} geschlossen)"
        )
        return result

    complete = untradable == 0
    full_sum = sum(t["price"] for t in live)

    if not complete and full_sum < MIN_LIVE_PROBABILITY_SUM:
        result["group"] = REJECT_INCOMPLETE
        result["reason"] = (
            f"unvollständige Outcomes: {len(live)} handelbare Outcomes summieren nur "
            f"{full_sum:.4f} < {MIN_LIVE_PROBABILITY_SUM}, {untradable} nicht handelbar "
            f"(Platzhalter/ohne Preis)"
        )
        return result

    if len(live) < 2 or (len(live) < top_n and not complete):
        result["group"] = REJECT_INCOMPLETE
        result["reason"] = (
            f"nur {len(live)} handelbare Outcomes (< {top_n}) und Set nicht vollständig "
            f"({untradable} nicht handelbar)"
            if not complete else
            f"nur {len(live)} handelbares Outcome"
        )
        return result

    live.sort(key=lambda t: t["price"], reverse=True)
    top_teams = live[:top_n]
    top_sum = sum(t["price"] for t in top_teams)

    if top_sum <= 0:
        result["group"] = REJECT_INVALID_PRICES
        result["reason"] = f"Summe Top {len(top_teams)} = 0"
        return result

    roi = (1 / top_sum) - 1
    covers_all_live = len(top_teams) == len(live)

    if covers_all_live and complete:
        arbitrage_type = ARB_GUARANTEED
    elif covers_all_live:
        arbitrage_type = ARB_ALL_LIVE
    else:
        arbitrage_type = ARB_SUBSET

    result.update({
        "top_teams": top_teams,
        "top_sum": top_sum,
        "roi": roi,
        "live_count": len(live),
        "untradable_count": untradable,
        "full_sum": full_sum,
        "complete": complete,
        "arbitrage_type": arbitrage_type,
    })

    if roi < min_roi:
        result["group"] = REJECT_BELOW_ROI
        result["reason"] = (
            f"ROI {roi * 100:.2f}% < {min_roi * 100:.2f}% "
            f"(Summe Top {len(top_teams)} = {top_sum:.4f})"
        )
        return result

    result["ok"] = True
    result["group"] = None
    return result


def connect_google_sheet():
    if not SHEET_ID:
        raise ValueError("SHEET_ID fehlt in Railway Variables.")

    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON fehlt in Railway Variables.")

    creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)

    return client.open_by_key(SHEET_ID)


def get_or_create_worksheet(sheet, title, rows=1000, cols=25):
    try:
        return sheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return sheet.add_worksheet(title=title, rows=rows, cols=cols)


EMPTY_OPPORTUNITIES_TEXT = "Keine passenden Spots gefunden."


def write_dataframe_to_sheet(ws, df):
    ws.clear()

    if df.empty:
        header = df.columns.tolist()
        if header:
            # Kopfzeile (= Filterzeile) bleibt stehen; Hinweis rechts außerhalb des Filterbereichs.
            ws.update(values=[header + ["", EMPTY_OPPORTUNITIES_TEXT]], range_name="A1")
        else:
            ws.update(values=[[EMPTY_OPPORTUNITIES_TEXT]], range_name="A1")
        return 0

    values = [df.columns.tolist()] + df.astype(str).values.tolist()
    ws.update(values=values, range_name="A1")
    return len(values) - 1


# =====================
# Google Sheets Filter
# =====================

CATEGORY_COLUMN = "Kategorie"


def quote_sheet_title(title):
    return "'" + str(title).replace("'", "''") + "'"


def read_sheet_state(ws):
    """Basic-Filter, Conditional Formats und bisherigen Inhalt eines Tabs (1 API-Call).

    Muss vor ws.clear() gelesen werden. Bei Fehlern {}: der Filter wird dann ohne
    Auswahl neu gesetzt, der Scanner läuft weiter.
    """
    try:
        meta = ws.spreadsheet.fetch_sheet_metadata(params={
            "includeGridData": "true",
            "ranges": quote_sheet_title(ws.title),
            "fields": (
                "sheets(properties(sheetId),basicFilter,conditionalFormats,"
                "data(rowData(values(formattedValue))))"
            ),
        })
    except Exception as e:
        print(
            f"⚠️ Filter-Status von '{ws.title}' nicht lesbar ({e}) – "
            f"Filter wird ohne Auswahl neu gesetzt.",
            flush=True,
        )
        return {}

    for sheet_meta in meta.get("sheets", []):
        # Die Sheets API lässt Felder mit Wert 0 weg (sheetId, columnIndex, ...).
        if sheet_meta.get("properties", {}).get("sheetId", 0) != ws.id:
            continue
        values = [
            [cell.get("formattedValue", "") for cell in row.get("values", [])]
            for data in sheet_meta.get("data", [])
            for row in data.get("rowData", [])
        ]
        return {
            "basic_filter": sheet_meta.get("basicFilter") or {},
            "conditional_formats": sheet_meta.get("conditionalFormats") or [],
            "values": values,
        }

    return {}


def keep_visible_categories(criteria, old_values, new_values):
    """"Nach Werten filtern" speichert nur die ausgeblendeten Werte.

    Ohne Anpassung würde eine Auswahl "nur CS2" eine später neu auftauchende Kategorie
    anzeigen. Deshalb bleibt die bisher sichtbare Auswahl sichtbar, alles andere wird
    ausgeblendet.
    """
    hidden = set(criteria.get("hiddenValues") or [])
    if not hidden:
        return criteria

    visible = set(old_values) - hidden
    if not visible:
        return criteria

    universe = set(CATEGORIES) | set(new_values) | set(old_values) | hidden
    return {**criteria, "hiddenValues": sorted(universe - visible)}


def carry_over_filter_specs(state, header, df):
    """Überträgt die im Sheet gewählten Filterkriterien per Spaltenname auf die neuen Daten."""
    basic_filter = state.get("basic_filter") or {}
    specs = basic_filter.get("filterSpecs")
    if not specs and basic_filter.get("criteria"):
        specs = [
            {"columnIndex": int(col), "filterCriteria": criteria}
            for col, criteria in basic_filter["criteria"].items()
        ]
    if not specs:
        return []

    old_rows = state.get("values") or []
    old_header = old_rows[0] if old_rows else []
    old_header_known = any(name in header for name in old_header)
    carried = []

    for spec in specs:
        criteria = spec.get("filterCriteria")
        if not criteria:
            continue

        old_col = spec.get("columnIndex", 0)
        name = old_header[old_col] if old_col < len(old_header) else None

        if name in header:
            new_col = header.index(name)
        elif not old_header_known and old_col < len(header):
            new_col, name = old_col, header[old_col]
        else:
            print(f"  Filter auf entfallener Spalte '{name}' wird verworfen.", flush=True)
            continue

        if name == CATEGORY_COLUMN:
            old_values = [
                row[old_col] for row in old_rows[1:]
                if old_col < len(row) and row[old_col]
            ]
            new_values = df[CATEGORY_COLUMN].dropna().astype(str).tolist() if CATEGORY_COLUMN in df else []
            criteria = keep_visible_categories(criteria, old_values, new_values)

        carried.append({"columnIndex": new_col, "filterCriteria": criteria})

    return carried


def apply_basic_filter(ws, num_cols, num_rows, filter_specs):
    """Setzt den Basic-Filter auf Kopfzeile + Datenzeilen und wendet ihn neu an.

    Werte-Updates per API blenden Zeilen nicht neu ein/aus; erst setBasicFilter
    wertet die Kriterien für die neuen Daten aus.
    """
    basic_filter = {
        "range": {
            "sheetId": ws.id,
            "startRowIndex": 0,
            "endRowIndex": num_rows + 1,
            "startColumnIndex": 0,
            "endColumnIndex": num_cols,
        }
    }
    if filter_specs:
        basic_filter["filterSpecs"] = filter_specs

    ws.spreadsheet.batch_update({"requests": [{"setBasicFilter": {"filter": basic_filter}}]})


def describe_filter(filter_specs, header, df):
    if not filter_specs:
        return "keine Auswahl aktiv (alle Kategorien sichtbar)"

    parts = []
    for spec in filter_specs:
        name = header[spec["columnIndex"]]
        hidden = spec["filterCriteria"].get("hiddenValues")
        if name == CATEGORY_COLUMN and hidden is not None and CATEGORY_COLUMN in df:
            visible = sorted(set(df[CATEGORY_COLUMN].astype(str)) - set(hidden))
            parts.append(f"{name}: sichtbar = {', '.join(visible) or '-'}")
        else:
            parts.append(f"{name}: Kriterium übernommen")
    return "; ".join(parts)


def update_opportunities_sheet(sheet, df):
    """Schreibt den Opportunities-Tab und erhält die im Sheet gewählte Filterauswahl."""
    ws = get_or_create_worksheet(sheet, "Opportunities")
    header = df.columns.tolist() or list(OPPORTUNITY_COLUMNS)

    state = read_sheet_state(ws)
    filter_specs = carry_over_filter_specs(state, header, df)

    rows_written = write_dataframe_to_sheet(ws, df)
    format_google_sheet(ws, num_cols=len(header))
    color_events_and_roi(ws, df, existing_rules=state.get("conditional_formats"))
    apply_basic_filter(ws, len(header), rows_written, filter_specs)

    print(f"Opportunities-Filter: {describe_filter(filter_specs, header, df)}", flush=True)
    return rows_written


def column_letter(index):
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


# =====================
# Google Sheets Format
# =====================

def format_google_sheet(ws, num_cols=12):
    last_col = column_letter(max(num_cols, 12) - 1)

    # Den Basic-Filter setzt apply_basic_filter() – ein set_basic_filter() ohne
    # Kriterien hier würde die im Sheet gewählte Kategorie bei jedem Lauf löschen.
    ws.freeze(rows=1)

    ws.format(f"A1:{last_col}1", {
        "backgroundColor": {"red": 0.12, "green": 0.31, "blue": 0.47},
        "textFormat": {
            "foregroundColor": {"red": 1, "green": 1, "blue": 1},
            "bold": True,
        },
        "horizontalAlignment": "CENTER",
    })

    ws.format(f"A:{last_col}", {
        "verticalAlignment": "MIDDLE",
        "wrapStrategy": "WRAP",
    })

    # F = YES Preis, G = Summe Top N, H = ROI %, I:K = Stake / Payout / Profit
    ws.format("F:G", {
        "numberFormat": {"type": "NUMBER", "pattern": "0.0000"}
    })

    ws.format("H:H", {
        "numberFormat": {"type": "NUMBER", "pattern": "0.00"}
    })

    ws.format("I:K", {
        "numberFormat": {"type": "CURRENCY", "pattern": "$0.00"}
    })

    requests = [
        {"updateDimensionProperties": {
            "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 150},
            "fields": "pixelSize"
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2},
            "properties": {"pixelSize": 120},
            "fields": "pixelSize"
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 2, "endIndex": 3},
            "properties": {"pixelSize": 260},
            "fields": "pixelSize"
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 3, "endIndex": 4},
            "properties": {"pixelSize": 420},
            "fields": "pixelSize"
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 11, "endIndex": 12},
            "properties": {"pixelSize": 420},
            "fields": "pixelSize"
        }},
    ]

    ws.spreadsheet.batch_update({"requests": requests})


ROI_HIGHLIGHT_THRESHOLD = "10"
ROI_HIGHLIGHT_COLOR = {"red": 0.72, "green": 0.88, "blue": 0.70}


def is_scanner_roi_rule(rule):
    boolean_rule = rule.get("booleanRule") or {}
    condition = boolean_rule.get("condition") or {}
    values = condition.get("values") or [{}]
    color = (boolean_rule.get("format") or {}).get("backgroundColor") or {}
    # Sheets speichert Farben in 1/255-Schritten (0.72 -> 0.7215...).
    return (
        condition.get("type") == "NUMBER_GREATER"
        and values[0].get("userEnteredValue") == ROI_HIGHLIGHT_THRESHOLD
        and all(abs(color.get(c, 0) - v) < 0.01 for c, v in ROI_HIGHLIGHT_COLOR.items())
    )


def color_events_and_roi(ws, df, existing_rules=None):
    if df.empty:
        return

    num_cols = len(df.columns)
    roi_col = df.columns.get_loc("ROI %")

    # Die ROI-Regel des Scanners ersetzen statt bei jedem Lauf eine weitere anzuhängen.
    stale_rule_indexes = [i for i, rule in enumerate(existing_rules or []) if is_scanner_roi_rule(rule)]
    requests = [
        {"deleteConditionalFormatRule": {"sheetId": ws.id, "index": index}}
        for index in sorted(stale_rule_indexes, reverse=True)
    ]

    event_colors = [
        {"red": 1.00, "green": 0.95, "blue": 0.80},
        {"red": 0.86, "green": 0.92, "blue": 0.98},
        {"red": 0.89, "green": 0.95, "blue": 0.86},
        {"red": 0.99, "green": 0.89, "blue": 0.82},
        {"red": 0.91, "green": 0.88, "blue": 0.95},
    ]

    event_map = {}

    for i, event in enumerate(df["Event"].tolist()):
        if event not in event_map:
            event_map[event] = event_colors[len(event_map) % len(event_colors)]

        row_index = i + 1

        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": ws.id,
                    "startRowIndex": row_index,
                    "endRowIndex": row_index + 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": num_cols,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": event_map[event]
                    }
                },
                "fields": "userEnteredFormat.backgroundColor",
            }
        })

    requests.append({
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [{
                    "sheetId": ws.id,
                    "startRowIndex": 1,
                    "endRowIndex": len(df) + 1,
                    "startColumnIndex": roi_col,
                    "endColumnIndex": roi_col + 1,
                }],
                "booleanRule": {
                    "condition": {
                        "type": "NUMBER_GREATER",
                        "values": [{"userEnteredValue": ROI_HIGHLIGHT_THRESHOLD}],
                    },
                    "format": {
                        "backgroundColor": ROI_HIGHLIGHT_COLOR,
                        "textFormat": {"bold": True},
                    },
                },
            },
            "index": 0,
        }
    })

    ws.spreadsheet.batch_update({"requests": requests})

def format_history_summary(ws):
    ws.freeze(rows=1)
    ws.set_basic_filter()

    ws.format("A1:L1", {
        "backgroundColor": {"red": 0.10, "green": 0.30, "blue": 0.50},
        "textFormat": {
            "foregroundColor": {"red": 1, "green": 1, "blue": 1},
            "bold": True,
        },
        "horizontalAlignment": "CENTER",
    })

    ws.format("A:L", {
        "verticalAlignment": "MIDDLE",
        "wrapStrategy": "WRAP",
    })

    requests = [
        {"updateDimensionProperties": {"range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}, "properties": {"pixelSize": 140}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2}, "properties": {"pixelSize": 120}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 2, "endIndex": 3}, "properties": {"pixelSize": 280}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 8, "endIndex": 9}, "properties": {"pixelSize": 500}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 11, "endIndex": 12}, "properties": {"pixelSize": 420}, "fields": "pixelSize"}},
    ]

    ws.spreadsheet.batch_update({"requests": requests})
    values = ws.get_all_values()

    requests = []

    for i, row in enumerate(values[1:], start=1):
        try:
            roi = float(row[5])  # ROI %
            top_sum = float(row[6])  # Summe Top 5
        except (ValueError, IndexError):
            continue

        # ROI Farben
        if roi > 10:
            color = {"red": 0.70, "green": 0.90, "blue": 0.70}
        elif roi > 5:
            color = {"red": 1.00, "green": 0.95, "blue": 0.70}
        else:
            color = None

        # Strong Edge überschreibt
        if top_sum < 0.90:
            color = {"red": 0.65, "green": 0.85, "blue": 1.00}

        if color:
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": i,
                        "endRowIndex": i + 1,
                        "startColumnIndex": 5,
                        "endColumnIndex": 7,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": color,
                            "textFormat": {"bold": True}
                        }
                    },
                    "fields": "userEnteredFormat.backgroundColor,userEnteredFormat.textFormat",
                }
            })

    if requests:
        ws.spreadsheet.batch_update({"requests": requests})

# =====================
# Scanner
# =====================

OPPORTUNITY_COLUMNS = [
    "Zeit", "Kategorie", "Event", "Event Slug", "Team / Markt", "YES Preis",
    f"Summe Top {TOP_N}", "ROI %", "Stake $", "Payout $", "Profit $", "Link",
    "Handelbare Outcomes", "Nicht handelbare Outcomes", "Summe alle YES", "Arbitrage-Typ",
]


def build_opportunities(events=None, stats=None):
    stats = stats if stats is not None else ScanStats()
    started = time.time()
    rows = []

    if events is None:
        events = get_events(stats=stats)
    else:
        stats.unique_events = len(events)

    print(f"Events geladen: {len(events)}")

    seen_slugs = set()
    scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for event in events:
        if is_dota_event(event):
            stats.dota_excluded += 1
            continue

        category = get_event_category(event)
        if not category:
            continue
        stats.category_matches[category] += 1

        not_tournament = get_tournament_rejection(event)
        if not_tournament:
            stats.non_tournament[not_tournament] += 1
            continue

        slug = event.get("slug") or event.get("id")
        if slug in seen_slugs:
            stats.reject(category, event, REJECT_OTHER, "Duplikat (Slug bereits verarbeitet)")
            continue
        seen_slugs.add(slug)

        event["_scanner_category"] = category
        stats.tournament_events[category] += 1

        if event.get("closed") is True or event.get("active") is False:
            stats.reject(category, event, REJECT_OTHER, "Event geschlossen oder inaktiv")
            continue

        evaluation = evaluate_event(event)

        for status, n in evaluation["market_counts"].items():
            if status != MARKET_LIVE:
                stats.market_exclusions[status] += n

        if evaluation.get("complete") and evaluation.get("full_sum", 1) < 1:
            stats.full_set_arbitrage_events += 1

        if not evaluation["ok"]:
            stats.reject(category, event, evaluation["group"], evaluation["reason"])
            continue

        top_teams = evaluation["top_teams"]
        yes_sum = evaluation["top_sum"]
        roi = evaluation["roi"]
        payout = BANKROLL / yes_sum
        profit = payout - BANKROLL

        stats.opportunity_events += 1
        if evaluation["arbitrage_type"] == ARB_GUARANTEED:
            stats.guaranteed_events += 1

        for t in top_teams:
            price = t["price"]
            stake = (price / yes_sum) * BANKROLL

            rows.append({
                "Zeit": scan_time,
                "Kategorie": category,
                "Event": event.get("title"),
                "Event Slug": event.get("slug"),
                "Team / Markt": t["team"],
                "YES Preis": round(price, 4),
                f"Summe Top {TOP_N}": round(yes_sum, 4),
                "ROI %": round(roi * 100, 2),
                "Stake $": round(stake, 2),
                "Payout $": round(payout, 2),
                "Profit $": round(profit, 2),
                "Link": "https://polymarket.com/event/" + str(event.get("slug", "")),
                "Handelbare Outcomes": evaluation["live_count"],
                "Nicht handelbare Outcomes": evaluation["untradable_count"],
                "Summe alle YES": round(evaluation["full_sum"], 4),
                "Arbitrage-Typ": evaluation["arbitrage_type"],
            })

    df = pd.DataFrame(rows, columns=OPPORTUNITY_COLUMNS)

    if not df.empty:
        df = df.drop_duplicates(subset=["Event Slug", "Team / Markt"], keep="first")
        df = df.sort_values(
            by=["ROI %", "Event", "YES Preis"],
            ascending=[False, True, False],
            kind="mergesort",
        ).reset_index(drop=True)

    stats.opportunity_rows = len(df)
    stats.duration_seconds = time.time() - started

    print(f"Turnier-Events gefunden: {sum(stats.tournament_events.values())}")
    stats.print_rejections()

    return df


# =====================
# History
# =====================

def migrate_db():
    if not DATABASE_URL:
        return

    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    try:
        cur.execute("ALTER TABLE outcomes ADD COLUMN real_payout FLOAT;")
    except:
        pass

    try:
        cur.execute("ALTER TABLE outcomes ADD COLUMN real_profit FLOAT;")
    except:
        pass

    try:
        cur.execute("ALTER TABLE outcomes ADD COLUMN real_cost FLOAT;")
    except:
        pass

    conn.commit()
    cur.close()
    conn.close()

def init_db():
    if not DATABASE_URL:
        print("DATABASE_URL fehlt – DB wird übersprungen.")
        return

    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS outcomes (
            event_slug TEXT PRIMARY KEY,
            event TEXT,
            category TEXT,
            best_roi FLOAT,
            top_sum FLOAT,
            teams TEXT,
            winner TEXT,
            top5_won BOOLEAN,
            first_seen TIMESTAMP,
            last_updated TIMESTAMP
        )
    """)

    conn.commit()
    cur.close()
    conn.close()


def save_outcome_to_db(df):
    if df.empty or not DATABASE_URL:
        return

    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    grouped = df.groupby(["Event", "Event Slug", "Kategorie"], dropna=False)
    rows = []

    for (event, slug, category), group in grouped:
        roi = float(group["ROI %"].iloc[0])

        if roi < MIN_HISTORY_ROI:
            continue

        top_sum = float(group[f"Summe Top {TOP_N}"].iloc[0])

        teams = " | ".join(
            f"{row['Team / Markt']} @ {row['YES Preis']}"
            for _, row in group.iterrows()
        )

        rows.append((
            slug,
            event,
            category,
            roi,
            top_sum,
            teams,
            None,
            None,
            datetime.now(),
            datetime.now()
        ))

    if not rows:
        cur.close()
        conn.close()
        return

    execute_values(cur, """
        INSERT INTO outcomes (
            event_slug, event, category, best_roi, top_sum, teams,
            winner, top5_won, first_seen, last_updated
        ) VALUES %s
        ON CONFLICT (event_slug) DO UPDATE SET
            best_roi = CASE
                WHEN EXCLUDED.best_roi > outcomes.best_roi
                THEN EXCLUDED.best_roi
                ELSE outcomes.best_roi
            END,
            top_sum = CASE
                WHEN EXCLUDED.best_roi > outcomes.best_roi
                THEN EXCLUDED.top_sum
                ELSE outcomes.top_sum
            END,
            teams = CASE
                WHEN EXCLUDED.best_roi > outcomes.best_roi
                THEN EXCLUDED.teams
                ELSE outcomes.teams
            END,
            last_updated = EXCLUDED.last_updated
    """, rows)

    conn.commit()
    cur.close()
    conn.close()


def resolve_outcomes_db():
    if not DATABASE_URL:
        return

    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    cur.execute("""
        SELECT event_slug, teams FROM outcomes
        WHERE winner IS NULL
    """)

    rows = cur.fetchall()

    for slug, teams_text in rows:
        event = fetch_event_by_slug(slug)
        winner = detect_resolved_winner(event)

        if not winner:
            continue

        teams = [t.split("@")[0].strip().lower() for t in teams_text.split("|")]
        winner_lower = winner.lower()

        top5_won = any(team in winner_lower or winner_lower in team for team in teams)

        cur.execute("""
            UPDATE outcomes
            SET winner = %s,
                top5_won = %s,
                last_updated = %s
            WHERE event_slug = %s
        """, (
            winner,
            top5_won,
            datetime.now(),
            slug
        ))

    conn.commit()
    cur.close()
    conn.close()

def update_stats_sheet_from_db(sheet):
    if not DATABASE_URL:
        print("DATABASE_URL fehlt – Stats werden übersprungen.")
        return

    ws = get_or_create_worksheet(sheet, "Stats", rows=1000, cols=10)

    conn = psycopg2.connect(DATABASE_URL)

    queries = {
        "Gesamt": """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE top5_won IS NOT NULL) AS resolved,
                COUNT(*) FILTER (WHERE top5_won = true) AS wins,
                COUNT(*) FILTER (WHERE top5_won = false) AS losses,
                ROUND(AVG(best_roi)::numeric, 2) AS avg_roi
            FROM outcomes;
        """,
        "Nach Kategorie": """
            SELECT
                category,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE top5_won IS NOT NULL) AS resolved,
                COUNT(*) FILTER (WHERE top5_won = true) AS wins,
                COUNT(*) FILTER (WHERE top5_won = false) AS losses,
                ROUND(
                    100.0 * COUNT(*) FILTER (WHERE top5_won = true)
                    / NULLIF(COUNT(*) FILTER (WHERE top5_won IS NOT NULL), 0),
                    2
                ) AS winrate_percent,
                ROUND(AVG(best_roi)::numeric, 2) AS avg_roi
            FROM outcomes
            GROUP BY category
            ORDER BY total DESC;
        """,
        "Nach ROI Bereich": """
            SELECT
                CASE
                    WHEN best_roi < 8 THEN '5-8%'
                    WHEN best_roi < 10 THEN '8-10%'
                    WHEN best_roi < 15 THEN '10-15%'
                    ELSE '15%+'
                END AS roi_range,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE top5_won IS NOT NULL) AS resolved,
                COUNT(*) FILTER (WHERE top5_won = true) AS wins,
                COUNT(*) FILTER (WHERE top5_won = false) AS losses,
                ROUND(
                    100.0 * COUNT(*) FILTER (WHERE top5_won = true)
                    / NULLIF(COUNT(*) FILTER (WHERE top5_won IS NOT NULL), 0),
                    2
                ) AS winrate_percent,
                ROUND(AVG(best_roi)::numeric, 2) AS avg_roi
            FROM outcomes
            GROUP BY roi_range
            ORDER BY MIN(best_roi);
        """
    }

    all_rows = []

    for title, query in queries.items():
        df = pd.read_sql_query(query, conn)

        all_rows.append([title])
        all_rows.append(df.columns.tolist())

        df = df.fillna("")

        for _, row in df.iterrows():
            all_rows.append(row.astype(str).tolist())

        all_rows.append([])

    conn.close()

    ws.clear()
    ws.update(values=all_rows, range_name="A1")
    ws.freeze(rows=1)
    ws.set_basic_filter()

    ws.format("A1:J1", {
        "backgroundColor": {"red": 0.12, "green": 0.31, "blue": 0.47},
        "textFormat": {
            "foregroundColor": {"red": 1, "green": 1, "blue": 1},
            "bold": True,
        },
    })

    print("✅ Stats Sheet aktualisiert.")

def update_db_outcomes_sheet(sheet):
    if not DATABASE_URL:
        return

    ws = get_or_create_worksheet(sheet, "DB Outcomes", rows=1000, cols=12)

    conn = psycopg2.connect(DATABASE_URL)

    query = """
        SELECT
            category,
            event,
            best_roi,
            top_sum,
            real_payout,
            real_profit,
            winner,
            top5_won,
            last_updated
        FROM outcomes
        ORDER BY best_roi DESC;
    """

    df = pd.read_sql_query(query, conn)
    conn.close()

    df = df.fillna("")

    ws.clear()

    if df.empty:
        ws.update(values=[["Keine DB Outcomes vorhanden."]], range_name="A1")
        return

    ws.update(
        values=[df.columns.tolist()] + df.astype(str).values.tolist(),
        range_name="A1"
    )

    ws.freeze(rows=1)
    ws.set_basic_filter()

def update_history_summary(sheet, df):
    if df.empty:
        return

    ws = get_or_create_worksheet(sheet, "History Summary", rows=1000, cols=25)

    try:
        existing = pd.DataFrame(ws.get_all_records())
    except Exception:
        existing = pd.DataFrame()

    snapshot_rows = []

    grouped = df.groupby(["Kategorie", "Event", "Event Slug"], dropna=False)

    for (category, event, slug), group in grouped:
        roi = float(group["ROI %"].iloc[0])

        if roi < MIN_HISTORY_ROI:
            continue

        top_sum = float(group[f"Summe Top {TOP_N}"].iloc[0])
        profit = float(group["Profit $"].iloc[0])
        link = group["Link"].iloc[0]
        timestamp = group["Zeit"].iloc[0]

        teams = " | ".join(
            f"{row['Team / Markt']} @ {row['YES Preis']}"
            for _, row in group.iterrows()
        )

        snapshot_rows.append({
            "Snapshot": "Current",
            "Kategorie": category,
            "Event": event,
            "Event Slug": slug,
            "Zeit": timestamp,
            "ROI %": roi,
            f"Summe Top {TOP_N}": top_sum,
            "Profit $": profit,
            "Teams": teams,
            "Top 5 gewonnen?": "",
            "Gewinner": "",
            "Link": link,
        })

    current = pd.DataFrame(snapshot_rows)

    if current.empty and existing.empty:
        ws.clear()
        ws.update(values=[["Keine History-Spots mit ROI > 5% gefunden."]], range_name="A1")
        return

    if existing.empty or "Event Slug" not in existing.columns:
        combined = current
    else:
        combined = pd.concat([existing, current], ignore_index=True)

    combined = combined.drop_duplicates(
        subset=["Event Slug", "Zeit"],
        keep="last"
    )

    result_rows = []

    for slug, group in combined.groupby("Event Slug"):
        group = group[group["ROI %"].astype(float) >= MIN_HISTORY_ROI]

        if group.empty:
            continue

        group = group.sort_values("ROI %")

        lowest = group.iloc[0]
        highest = group.iloc[-1]
        middle = group.iloc[len(group) // 2]

        for label, row in [
            ("Lowest ROI > 5%", lowest),
            ("Middle ROI", middle),
            ("Highest ROI", highest),
        ]:
            result_rows.append({
                "Snapshot": label,
                "Kategorie": row["Kategorie"],
                "Event": row["Event"],
                "Event Slug": row["Event Slug"],
                "Zeit": row["Zeit"],
                "ROI %": row["ROI %"],
                f"Summe Top {TOP_N}": row[f"Summe Top {TOP_N}"],
                "Profit $": row["Profit $"],
                "Teams": row["Teams"],
                "Top 5 gewonnen?": row.get("Top 5 gewonnen?", ""),
                "Gewinner": row.get("Gewinner", ""),
                "Link": row["Link"],
            })

    result = pd.DataFrame(result_rows)

    if result.empty:
        ws.clear()
        ws.update(values=[["Keine History-Spots mit ROI > 5% gefunden."]], range_name="A1")
        return

    result = result.sort_values(["Event", "Snapshot"])

    ws.clear()
    ws.update(
        values=[result.columns.tolist()] + result.astype(str).values.tolist(),
        range_name="A1"
    )
    format_history_summary(ws)

    ws.freeze(rows=1)
    ws.set_basic_filter()

def update_outcome_history(sheet, df):
    if df.empty:
        return

    ws = get_or_create_worksheet(sheet, "Outcome", rows=2000, cols=25)

    try:
        existing = pd.DataFrame(ws.get_all_records())
    except Exception:
        existing = pd.DataFrame()

    rows = []

    grouped = df.groupby(["Event", "Event Slug"], dropna=False)

    for (event, slug), group in grouped:
        roi = float(group["ROI %"].iloc[0])

        if roi < MIN_HISTORY_ROI:
            continue

        timestamp = group["Zeit"].iloc[0]
        top_sum = float(group[f"Summe Top {TOP_N}"].iloc[0])
        link = group["Link"].iloc[0]

        teams = " | ".join(
            f"{row['Team / Markt']} @ {row['YES Preis']}"
            for _, row in group.iterrows()
        )

        rows.append({
            "Zeit": timestamp,
            "Event": event,
            "Event Slug": slug,
            "ROI %": roi,
            f"Summe Top {TOP_N}": top_sum,
            "Teams": teams,
            "Gewinner": "",
            "Top 5 gewonnen?": "",
            "Link": link,
        })

    new_df = pd.DataFrame(rows)

    if existing.empty:
        combined = new_df
    else:
        combined = pd.concat([existing, new_df], ignore_index=True)

    combined["ROI %"] = combined["ROI %"].astype(float)

    combined = (
        combined.sort_values("ROI %", ascending=False)
        .drop_duplicates(subset=["Event Slug"], keep="first")
        )

    ws.clear()
    ws.update(
        values=[combined.columns.tolist()] + combined.astype(str).values.tolist(),
        range_name="A1"
    )

    ws.freeze(rows=1)
    ws.set_basic_filter()

def resolve_outcome(sheet):
    ws = get_or_create_worksheet(sheet, "Outcome", rows=2000, cols=25)

    try:
        df = pd.DataFrame(ws.get_all_records())
    except Exception:
        return

    if df.empty:
        return

    changed = False

    for idx, row in df.iterrows():
        if row.get("Top 5 gewonnen?"):
            continue

        slug = row.get("Event Slug")
        event = fetch_event_by_slug(slug)

        winner = detect_resolved_winner(event)

        if not winner:
            continue

        teams = parse_team_names_from_history(row["Teams"])

        winner_lower = winner.lower()

        top5 = any(team in winner_lower for team in teams)

        df.at[idx, "Gewinner"] = winner
        df.at[idx, "Top 5 gewonnen?"] = "TRUE" if top5 else "FALSE"

        changed = True

    if changed:
        ws.clear()
        ws.update(
            values=[df.columns.tolist()] + df.astype(str).values.tolist(),
            range_name="A1"
        )

def parse_team_names_from_history(teams_text):
    teams = []

    for part in str(teams_text).split("|"):
        name = part.split("@")[0].strip()
        if name:
            teams.append(name.lower())

    return teams


def detect_resolved_winner(event):
    if not event:
        return None

    markets = event.get("markets", [])

    for market in markets:
        # Nur aufgelöste Märkte: ein offener Favorit bei 0.98 ist noch kein Gewinner.
        if market.get("closed") is not True:
            continue

        yes_price = get_yes_price(market)

        if yes_price is None:
            continue

        if yes_price >= RESOLVED_YES_PRICE:
            return (
                market.get("question")
                or market.get("title")
                or market.get("slug")
            )

    return None


def resolve_history_results(sheet):
    ws = get_or_create_worksheet(sheet, "History Summary", rows=1000, cols=25)

    try:
        df = pd.DataFrame(ws.get_all_records())
    except Exception:
        return

    if df.empty or "Event Slug" not in df.columns:
        return

    changed = False
    event_cache = {}

    for idx, row in df.iterrows():
        current_status = str(row.get("Top 5 gewonnen?", "")).strip()

        if current_status:
            continue

        slug = row.get("Event Slug")

        if not slug:
            continue

        if slug not in event_cache:
            event_cache[slug] = fetch_event_by_slug(slug)

        event = event_cache[slug]
        winner = detect_resolved_winner(event)

        if not winner:
            continue

        top_teams = parse_team_names_from_history(row.get("Teams", ""))
        winner_lower = winner.lower()

        top5_won = any(team in winner_lower or winner_lower in team for team in top_teams)

        df.at[idx, "Gewinner"] = winner
        df.at[idx, "Top 5 gewonnen?"] = "JA" if top5_won else "NEIN"
        changed = True

    if changed:
        ws.clear()
        ws.update(
            values=[df.columns.tolist()] + df.astype(str).values.tolist(),
            range_name="A1"
        )
        ws.freeze(rows=1)
        ws.set_basic_filter()
        print("✅ History Results aktualisiert.")
    else:
        print("Keine neuen resolved History-Ergebnisse gefunden.")


# =====================
# Main
# =====================

def main():
    print("Starte Polymarket Scanner...")
    stats = ScanStats()

    try:
        # Bricht bei komplettem API-Ausfall ab, bevor Sheets/DB angefasst werden.
        df = build_opportunities(stats=stats)
        print("Gefundene Zeilen:", len(df))

        # Datenbank
        init_db()
        migrate_db()
        init_db()
        save_outcome_to_db(df)
        resolve_outcomes_db()

        # Google Sheet
        sheet = connect_google_sheet()
        print("Google Sheet geöffnet:", sheet.title)
        update_stats_sheet_from_db(sheet)
        update_db_outcomes_sheet(sheet)

        stats.sheet_rows_written = update_opportunities_sheet(sheet, df)

        # History / Outcome im Sheet
        update_history_summary(sheet, df)

        # Status
        status_ws = get_or_create_worksheet(sheet, "Status", rows=20, cols=5)
        status_ws.clear()
        status_ws.update(values=[
            ["Letztes Update", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
            ["Gefundene Zeilen", len(df)],
            ["Bankroll pro Spot", BANKROLL],
            ["Top N", TOP_N],
            ["Min ROI", MIN_RAW_ROI],
            ["Min History ROI %", MIN_HISTORY_ROI],
            ["Events geladen", stats.unique_events],
            ["Turnier-Events", sum(stats.tournament_events.values())],
            ["Opportunity-Events", stats.opportunity_events],
            ["Discovery-Fehler", len(stats.discovery_errors)],
        ], range_name="A1")

        print("✅ Google Sheet erfolgreich aktualisiert.")
    finally:
        stats.print_summary()

if __name__ == "__main__":
    main()
