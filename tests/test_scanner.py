import copy
import json
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scanner  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "gamma_events.json"


@pytest.fixture(scope="module")
def real_events():
    """Echte Gamma-Events (gekürzt auf die vom Scanner gelesenen Felder)."""
    return json.loads(FIXTURE.read_text())


def by_title(events, title):
    return copy.deepcopy(next(e for e in events if e["title"] == title))


def market(name, price, *, active=True, closed=False, volume=1000.0, bid=None,
           ltp=None, other=False, accepting=True, prices=None, mid=None):
    return {
        "id": mid or name,
        "question": f"Will {name} win?",
        "groupItemTitle": name,
        "outcomes": '["Yes", "No"]',
        "outcomePrices": prices if prices is not None else json.dumps([str(price), str(round(1 - price, 4))]),
        "active": active,
        "closed": closed,
        "acceptingOrders": accepting,
        "bestBid": bid,
        "lastTradePrice": ltp,
        "volumeNum": volume,
        "negRisk": True,
        "negRiskOther": other,
    }


def event(title, markets, *, slug=None, neg_risk=True, tags=("counter-strike-2", "esports"), eid=None):
    return {
        "id": eid or (slug or title),
        "slug": slug or title.lower().replace(" ", "-").replace(":", ""),
        "title": title,
        "negRisk": neg_risk,
        "active": True,
        "closed": False,
        "tags": [{"id": str(i), "slug": t, "label": t} for i, t in enumerate(tags)],
        "markets": markets,
    }


# ---------------------------------------------------------------------------
# Regression: inaktive Platzhalter (Dummy-Preis 0.5) wurden als Top 5 gewählt
# ---------------------------------------------------------------------------

def test_placeholders_are_not_selected_as_top_outcomes(real_events):
    ucl = by_title(real_events, "UEFA Champions League: 2027 Champion")
    placeholders = [m for m in ucl["markets"] if m["active"] is False]
    assert len(placeholders) >= 5
    assert all(scanner.get_yes_price(m) == 0.5 for m in placeholders)

    result = scanner.evaluate_event(ucl)

    assert result["ok"], result["reason"]
    names = [t["market"]["groupItemTitle"] for t in result["top_teams"]]
    assert names == ["Barcelona", "Paris Saint-Germain", "Real Madrid", "Arsenal", "Bayern Munich"]
    assert result["top_sum"] == pytest.approx(0.235 + 0.185 + 0.155 + 0.135 + 0.125)
    assert result["roi"] == pytest.approx(1 / 0.835 - 1)
    assert result["arbitrage_type"] == scanner.ARB_SUBSET


def test_real_cs2_major_produces_opportunity_rows(real_events):
    pgl = by_title(real_events, "PGL Major Singapore 2026: Winner")
    df = scanner.build_opportunities(events=[pgl])

    assert len(df) == scanner.TOP_N
    assert set(df["Kategorie"]) == {"CS2"}
    assert df["ROI %"].iloc[0] == pytest.approx(round((1 / 0.805 - 1) * 100, 2))
    assert set(df["Arbitrage-Typ"]) == {scanner.ARB_SUBSET}


def test_old_selection_logic_would_have_rejected_real_events(real_events):
    """Dokumentiert den Root Cause: Top 5 nach Preis ohne Marktstatus = 5 x 0.5."""
    for title in ["UEFA Champions League: 2027 Champion", "PGL Major Singapore 2026: Winner"]:
        ev = by_title(real_events, title)
        prices = sorted(
            (p for p in (scanner.get_yes_price(m) for m in ev["markets"]) if p is not None),
            reverse=True,
        )
        assert sum(prices[:5]) == pytest.approx(2.5)
        assert scanner.evaluate_event(ev)["ok"]


# ---------------------------------------------------------------------------
# Vollständigkeit / MECE
# ---------------------------------------------------------------------------

def test_incomplete_live_set_is_rejected_not_reported_as_huge_roi(real_events):
    afc = by_title(real_events, "AFC Asian Cup 2027 Winner")
    result = scanner.evaluate_event(afc)

    assert not result["ok"]
    assert result["group"] == scanner.REJECT_INCOMPLETE
    assert "unvollständige Outcomes" in result["reason"]


def test_unpriced_markets_are_invalid_prices(real_events):
    jleague = by_title(real_events, "Japan J. League: 2026-27 Winner")
    result = scanner.evaluate_event(jleague)

    assert not result["ok"]
    assert result["group"] == scanner.REJECT_INVALID_PRICES
    assert result["market_counts"][scanner.MARKET_UNPRICED] > 0


def test_complete_real_set_is_labelled_guaranteed(real_events):
    wc = by_title(real_events, "World Cup 2030: Location of Final")
    result = scanner.evaluate_event(wc)

    assert result["ok"]
    assert result["complete"]
    assert result["arbitrage_type"] == scanner.ARB_GUARANTEED
    assert result["top_sum"] == pytest.approx(result["full_sum"])


def test_decided_final_with_placeholders_is_not_guaranteed(real_events):
    worlds = by_title(real_events, "Worlds 2026: Winner")
    result = scanner.evaluate_event(worlds)

    assert not result["ok"]
    assert result["group"] == scanner.REJECT_INCOMPLETE


def test_non_neg_risk_event_is_not_an_arbitrage_basket(real_events):
    val = by_title(real_events, "Valorant Team to Win an International in 2027")
    result = scanner.evaluate_event(val)

    assert not result["ok"]
    assert "negRisk=false" in result["reason"]


def test_complete_small_set_below_top_n_is_kept():
    ev = event("Mini Cup Winner", [market("A", 0.40), market("B", 0.30), market("C", 0.20)])
    result = scanner.evaluate_event(ev)

    assert result["ok"]
    assert result["arbitrage_type"] == scanner.ARB_GUARANTEED
    assert result["roi"] == pytest.approx(1 / 0.9 - 1)


def test_small_set_with_open_placeholder_is_rejected():
    ev = event("Mini Cup Winner", [
        market("A", 0.40), market("B", 0.30), market("C", 0.25),
        market("Team D", 0.5, active=False, volume=0),
    ])
    result = scanner.evaluate_event(ev)

    assert not result["ok"]
    assert result["group"] == scanner.REJECT_INCOMPLETE


def test_explicit_other_is_ignored_while_placeholders_are_open():
    teams = [market(n, p) for n, p in [("A", 0.35), ("B", 0.2), ("C", 0.15), ("D", 0.1), ("E", 0.1)]]
    ev = event("Cup Winner", teams + [
        market("Other", 0.12, other=True),
        market("Team X", 0.5, active=False, volume=0),
    ])
    result = scanner.evaluate_event(ev)

    assert result["ok"]
    assert "Other" not in [t["market"]["groupItemTitle"] for t in result["top_teams"]]
    assert result["market_counts"][scanner.MARKET_OTHER_UNSTABLE] == 1


def test_resolved_event_is_rejected():
    ev = event("Cup Winner", [
        market("A", 1.0, closed=True), market("B", 0.0, closed=True),
        market("C", 0.01), market("D", 0.01), market("E", 0.01), market("F", 0.01),
    ])
    result = scanner.evaluate_event(ev)

    assert not result["ok"]
    assert "bereits entschieden" in result["reason"]


# ---------------------------------------------------------------------------
# Preise / ROI-Mathematik
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("outcomes, prices, expected", [
    ('["Yes", "No"]', '["0.235", "0.765"]', 0.235),
    (["Yes", "No"], [0.3, 0.7], 0.3),
    (["No", "Yes"], ["0.7", "0.3"], 0.3),
    ('["Yes", "No"]', '["abc", "0.5"]', None),
    ('["Yes", "No"]', '["1.5", "0"]', None),
    ('["Yes", "No"]', '["NaN", "0"]', None),
    ('["Yes", "No"]', "not json", None),
    (None, None, None),
])
def test_get_yes_price_parsing(outcomes, prices, expected):
    result = scanner.get_yes_price({"outcomes": outcomes, "outcomePrices": prices})
    assert result == (pytest.approx(expected) if expected is not None else None)


def test_roi_stake_and_payout_math():
    prices = [0.30, 0.20, 0.15, 0.10, 0.05, 0.04, 0.03]
    ev = event("Test Major Winner", [market(f"T{i}", p) for i, p in enumerate(prices)])
    df = scanner.build_opportunities(events=[ev])

    top_sum = sum(prices[:5])
    assert len(df) == 5
    assert df["Summe Top 5"].iloc[0] == pytest.approx(round(top_sum, 4))
    assert df["ROI %"].iloc[0] == pytest.approx(round((1 / top_sum - 1) * 100, 2))
    assert df["Payout $"].iloc[0] == pytest.approx(round(scanner.BANKROLL / top_sum, 2))
    assert df["Stake $"].sum() == pytest.approx(scanner.BANKROLL, abs=0.05)
    for _, row in df.iterrows():
        assert row["Stake $"] / row["YES Preis"] == pytest.approx(row["Payout $"], rel=1e-3)
    assert list(df["YES Preis"]) == sorted(prices[:5], reverse=True)


def test_roi_threshold_boundary():
    below = event("Low Cup Winner", [market(f"T{i}", 0.192) for i in range(5)], slug="low")
    above = event("High Cup Winner", [market(f"T{i}", 0.19) for i in range(5)], slug="high")
    df = scanner.build_opportunities(events=[below, above])

    assert set(df["Event Slug"]) == {"high"}


# ---------------------------------------------------------------------------
# Turnier-Erkennung
# ---------------------------------------------------------------------------

def test_dota_is_always_excluded(real_events):
    blast_slam = by_title(real_events, "BLAST Slam IX: Winner")
    assert scanner.is_dota_event(blast_slam)
    assert scanner.get_event_category(blast_slam) is None

    stats = scanner.ScanStats()
    df = scanner.build_opportunities(events=[blast_slam], stats=stats)
    assert df.empty
    assert stats.dota_excluded == 1

    by_keyword = event("Dota 2 The International Winner", [market("A", 0.1)], tags=("esports",))
    assert scanner.get_event_category(by_keyword) is None


def test_description_keywords_no_longer_create_false_categories(real_events):
    mortgage = by_title(real_events, "Will the 30-year Mortgage Rate hit __ in 2026?")
    assert scanner.get_event_category(mortgage) is None

    riemann = event("Will CMI declare the Riemann Hypothesis solved?", [], tags=("science",))
    riemann["description"] = "premium IEM PGL cs2"
    assert scanner.get_event_category(riemann) is None


def test_single_games_and_non_tournament_markets_are_filtered(real_events):
    game = next(e for e in real_events if "(BO3)" in e["title"])
    assert scanner.get_tournament_rejection(game) is not None

    top_scorer = by_title(real_events, "UEFA Champions League: Top Scorer 2026-27")
    assert scanner.get_tournament_rejection(top_scorer) == "kein Turnier-Keyword im Titel"


@pytest.mark.parametrize("title, tags, expected", [
    ("PGL Major Singapore 2026: Winner", ("cs2", "esports"), "CS2"),
    ("Worlds 2026: Winner", ("lol-worlds-2026", "esports"), "LoL"),
    ("VCT Champions 2027 Winner", ("valorant",), "Valorant"),
    ("EPL: 2027 Champion", ("soccer", "epl"), "Fußball"),
    ("Bundesliga: 2027 Champion", (), "Fußball"),
    ("IEM Cologne 2027 Winner", (), "CS2"),
    ("2026 Rugby League World Cup Winner", ("rugby",), None),
    ("Basketball Champions League Winner", ("basketball",), None),
])
def test_category_detection(title, tags, expected):
    assert scanner.get_event_category(event(title, [], tags=tags)) == expected


# ---------------------------------------------------------------------------
# Duplikate
# ---------------------------------------------------------------------------

def test_duplicate_events_and_markets_are_deduplicated(real_events):
    ucl = by_title(real_events, "UEFA Champions League: 2027 Champion")
    dup = copy.deepcopy(ucl)
    dup["id"] = "other-id"
    ucl["markets"] = ucl["markets"] + copy.deepcopy(ucl["markets"][:10])

    stats = scanner.ScanStats()
    df = scanner.build_opportunities(events=[ucl, dup], stats=stats)

    assert len(df) == scanner.TOP_N
    assert df["Event Slug"].nunique() == 1
    assert not df.duplicated(subset=["Event Slug", "Team / Markt"]).any()
    assert any("Duplikat" in reason for *_, reason in stats.rejected_events)


# ---------------------------------------------------------------------------
# Discovery (Gamma API, gemockt)
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        return self.handler(url, dict(params or {}))


def test_get_events_uses_keyset_pagination_and_dedupes(monkeypatch):
    pages = {
        None: {"events": [{"id": str(i)} for i in range(100)], "next_cursor": "c1"},
        "c1": {"events": [{"id": str(i)} for i in range(90, 130)], "next_cursor": None},
    }

    def handler(url, params):
        assert url == scanner.EVENTS_KEYSET_URL
        assert "offset" not in params
        assert params["closed"] == "false"
        if params.get("tag_slug") == "soccer":
            return FakeResponse(200, pages[params.get("after_cursor")])
        if "order" in params:
            assert params["order"] == "volume24hr"
        return FakeResponse(200, {"events": [{"id": "0"}], "next_cursor": None})

    session = FakeSession(handler)
    monkeypatch.setattr(scanner, "HTTP", session)
    stats = scanner.ScanStats()

    events = scanner.get_events(stats=stats)

    assert len(events) == 130
    assert stats.unique_events == 130
    assert stats.discovery_queries == len(scanner.DISCOVERY_TAG_SLUGS) + 1
    soccer_calls = [p for u, p in session.calls if p.get("tag_slug") == "soccer"]
    assert soccer_calls[0]["exclude_tag_id"] == scanner.GAMES_TAG_ID
    assert soccer_calls[1]["after_cursor"] == "c1"


def test_get_events_falls_back_to_offset_when_keyset_fails(monkeypatch):
    def handler(url, params):
        if url == scanner.EVENTS_KEYSET_URL:
            return FakeResponse(422, {"error": "validation error"})
        assert params["offset"] <= scanner.MAX_OFFSET
        return FakeResponse(200, [{"id": f"{params.get('tag_slug')}-{params['offset']}"}])

    monkeypatch.setattr(scanner, "HTTP", FakeSession(handler))
    monkeypatch.setattr(scanner.time, "sleep", lambda s: None)
    stats = scanner.ScanStats()

    events = scanner.get_events(stats=stats)

    assert len(events) == len(scanner.DISCOVERY_TAG_SLUGS) + 1
    assert stats.discovery_errors == []


def test_total_api_failure_aborts_before_sheets_are_touched(monkeypatch):
    monkeypatch.setattr(scanner, "HTTP", FakeSession(lambda u, p: FakeResponse(500, {"error": "x"})))
    monkeypatch.setattr(scanner.time, "sleep", lambda s: None)
    touched = []
    monkeypatch.setattr(scanner, "connect_google_sheet", lambda: touched.append("sheet"))
    monkeypatch.setattr(scanner, "init_db", lambda: touched.append("db"))

    with pytest.raises(RuntimeError):
        scanner.main()

    assert touched == []


# ---------------------------------------------------------------------------
# Google Sheets (gemockt)
# ---------------------------------------------------------------------------

class FakeWorksheet:
    def __init__(self, title):
        self.title = title
        self.id = abs(hash(title)) % 10000
        self.values = []
        self.formats = []
        self.spreadsheet = self
        self.batch_requests = []

    def clear(self):
        self.values = []

    def update(self, values=None, range_name=None):
        self.values = values

    def get_all_records(self):
        if len(self.values) < 2:
            return []
        header = self.values[0]
        # wie gspread: numerische Strings werden standardmäßig zu Zahlen
        return [
            dict(zip(header, scanner.gspread.utils.numericise_all([str(v) for v in row])))
            for row in self.values[1:]
        ]

    def get_all_values(self):
        return self.values

    def freeze(self, rows=0):
        pass

    def set_basic_filter(self):
        pass

    def format(self, rng, fmt):
        self.formats.append(rng)

    def batch_update(self, body):
        self.batch_requests.extend(body["requests"])


class FakeSpreadsheet:
    title = "Test Sheet"

    def __init__(self):
        self.sheets = {}

    def worksheet(self, title):
        if title not in self.sheets:
            raise scanner.gspread.WorksheetNotFound(title)
        return self.sheets[title]

    def add_worksheet(self, title, rows, cols):
        self.sheets[title] = FakeWorksheet(title)
        return self.sheets[title]


def test_opportunities_reach_google_sheets(monkeypatch, real_events):
    events = [copy.deepcopy(e) for e in real_events]
    sheet = FakeSpreadsheet()

    monkeypatch.setattr(scanner, "get_events", lambda stats=None: events)
    monkeypatch.setattr(scanner, "connect_google_sheet", lambda: sheet)
    monkeypatch.setattr(scanner, "DATABASE_URL", None)

    scanner.main()

    opp = sheet.sheets["Opportunities"].values
    header, rows = opp[0], opp[1:]
    assert header[:12] == [
        "Zeit", "Kategorie", "Event", "Event Slug", "Team / Markt", "YES Preis",
        "Summe Top 5", "ROI %", "Stake $", "Payout $", "Profit $", "Link",
    ]
    assert "Arbitrage-Typ" in header
    slugs = {r[header.index("Event Slug")] for r in rows}
    assert len(slugs) == 3
    assert len(rows) == 5 + 5 + 5

    roi_rule = next(r for r in sheet.sheets["Opportunities"].batch_requests if "addConditionalFormatRule" in r)
    rng = roi_rule["addConditionalFormatRule"]["rule"]["ranges"][0]
    assert rng["startColumnIndex"] == header.index("ROI %")

    history = sheet.sheets["History Summary"].values
    assert history[0][0] == "Snapshot"
    assert len(history) == 1 + 3 * 3

    status = dict((r[0], r[1]) for r in sheet.sheets["Status"].values)
    assert status["Gefundene Zeilen"] == 15


def test_empty_scan_writes_placeholder_text():
    ws = FakeWorksheet("Opportunities")
    assert scanner.write_dataframe_to_sheet(ws, pd.DataFrame()) == 0
    assert ws.values == [["Keine passenden Spots gefunden."]]


def test_detect_resolved_winner_requires_closed_market():
    open_favourite = event("Cup Winner", [market("A", 0.985), market("B", 0.015)])
    assert scanner.detect_resolved_winner(open_favourite) is None

    resolved = event("Cup Winner", [market("A", 1.0, closed=True), market("B", 0.0, closed=True)])
    assert scanner.detect_resolved_winner(resolved) == "Will A win?"


def test_debug_summary_contains_real_counts(real_events):
    stats = scanner.ScanStats()
    scanner.build_opportunities(events=[copy.deepcopy(e) for e in real_events], stats=stats)
    text = "\n".join(stats.summary_lines())

    assert "Tournament Events Detected: 7" in text
    assert "Events With Invalid Prices: 1" in text
    assert "Events With Incomplete Outcomes: 2" in text
    assert "Valid Opportunities: 3 Events / 15 Zeilen" in text
    assert "Dota 2 Excluded: 1" in text


# ---------------------------------------------------------------------------
# PostgreSQL (nur wenn TEST_DATABASE_URL gesetzt ist)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL nicht gesetzt")
def test_postgres_history_roundtrip(monkeypatch, real_events):
    url = os.getenv("TEST_DATABASE_URL")
    monkeypatch.setattr(scanner, "DATABASE_URL", url)

    conn = scanner.psycopg2.connect(url)
    with conn, conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS outcomes")
    conn.close()

    scanner.init_db()
    scanner.migrate_db()
    scanner.init_db()

    df = scanner.build_opportunities(events=[copy.deepcopy(e) for e in real_events])
    scanner.save_outcome_to_db(df)
    scanner.save_outcome_to_db(df)

    conn = scanner.psycopg2.connect(url)
    with conn.cursor() as cur:
        cur.execute("SELECT event_slug, best_roi, top_sum, teams FROM outcomes ORDER BY best_roi DESC")
        rows = cur.fetchall()
    conn.close()

    assert len(rows) == 3
    slug, roi, top_sum, teams = rows[0]
    assert slug.startswith("pgl-major-singapore-2026-winner")
    assert roi == pytest.approx(24.22)
    assert top_sum == pytest.approx(0.805)
    assert teams.count("|") == 4

    pgl = by_title(real_events, "PGL Major Singapore 2026: Winner")
    vitality = next(m for m in pgl["markets"] if m.get("groupItemTitle") == "Vitality")
    for m in pgl["markets"]:
        m["closed"] = True
        m["outcomePrices"] = '["1", "0"]' if m is vitality else '["0", "1"]'
    monkeypatch.setattr(scanner, "fetch_event_by_slug", lambda s: pgl if s == slug else None)
    scanner.resolve_outcomes_db()

    conn = scanner.psycopg2.connect(url)
    with conn.cursor() as cur:
        cur.execute("SELECT winner, top5_won FROM outcomes WHERE event_slug = %s", (slug,))
        winner, top5_won = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM outcomes WHERE winner IS NOT NULL")
        resolved_count = cur.fetchone()[0]
    conn.close()
    assert winner == vitality["question"]
    assert top5_won is True
    assert resolved_count == 1
