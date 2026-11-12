import copy
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scanner  # noqa: E402


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

def drop_zero_fields(obj):
    """Wie die Sheets API (proto3-JSON): Felder mit Wert 0 fehlen in Antworten."""
    if isinstance(obj, dict):
        return {
            k: drop_zero_fields(v) for k, v in obj.items()
            if not (isinstance(v, (int, float)) and not isinstance(v, bool) and v == 0)
        }
    if isinstance(obj, list):
        return [drop_zero_fields(v) for v in obj]
    return obj


class FakeWorksheet:
    def __init__(self, title, spreadsheet, sheet_id, rows=1000, cols=26):
        self.title = title
        self.id = sheet_id
        self.values = []
        self.formats = []
        self.spreadsheet = spreadsheet
        self.basic_filter = None
        self.basic_filter_resets = 0
        self.conditional_formats = []
        self.row_count = rows
        self.col_count = cols

    def clear(self):
        self.values = []

    def update(self, values=None, range_name=None):
        # wie values.update der Sheets API: kein automatisches Erweitern des Rasters
        widest = max((len(row) for row in values), default=0)
        if len(values) > self.row_count or widest > self.col_count:
            raise ValueError(f"Range exceeds grid limits. Max rows: {self.row_count}, max columns: {self.col_count}")
        self.values = values

    def add_rows(self, rows):
        self.row_count += rows

    def add_cols(self, cols):
        self.col_count += cols

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

    def set_basic_filter(self, name=None):
        # gspread ohne Range: Filter über das ganze Blatt, ohne Kriterien
        self.basic_filter = {"range": {"sheetId": self.id}}
        self.basic_filter_resets += 1

    def format(self, rng, fmt):
        self.formats.append(rng)

    def visible_data_rows(self):
        """Datenzeilen, die der aktuelle Basic-Filter anzeigt."""
        rows = self.values[1:]
        specs = (self.basic_filter or {}).get("filterSpecs") or []
        visible = []
        for row in rows:
            shown = True
            for spec in specs:
                col = spec.get("columnIndex", 0)
                value = str(row[col]) if col < len(row) else ""
                if value in (spec["filterCriteria"].get("hiddenValues") or []):
                    shown = False
            if shown:
                visible.append(row)
        return visible


class FakeSpreadsheet:
    title = "Test Sheet"

    def __init__(self):
        self.sheets = {}
        self.requests = []
        self.fail_metadata = False

    def worksheet(self, title):
        if title not in self.sheets:
            raise scanner.gspread.WorksheetNotFound(title)
        return self.sheets[title]

    def add_worksheet(self, title, rows, cols):
        self.sheets[title] = FakeWorksheet(title, self, sheet_id=len(self.sheets), rows=rows, cols=cols)
        return self.sheets[title]

    def by_id(self, sheet_id):
        return next(ws for ws in self.sheets.values() if ws.id == sheet_id)

    def batch_update(self, body):
        for req in body["requests"]:
            self.requests.append(copy.deepcopy(req))
            if "setBasicFilter" in req:
                flt = copy.deepcopy(req["setBasicFilter"]["filter"])
                self.by_id(flt["range"]["sheetId"]).basic_filter = flt
            elif "addConditionalFormatRule" in req:
                add = req["addConditionalFormatRule"]
                ws = self.by_id(add["rule"]["ranges"][0]["sheetId"])
                ws.conditional_formats.insert(add.get("index", 0), copy.deepcopy(add["rule"]))
            elif "deleteConditionalFormatRule" in req:
                delete = req["deleteConditionalFormatRule"]
                self.by_id(delete["sheetId"]).conditional_formats.pop(delete["index"])

    def fetch_sheet_metadata(self, params=None):
        if self.fail_metadata:
            raise RuntimeError("quota exceeded")
        title = params["ranges"].strip("'").replace("''", "'")
        ws = self.sheets[title]
        basic_filter = copy.deepcopy(ws.basic_filter)
        if basic_filter and basic_filter.get("filterSpecs"):
            basic_filter["criteria"] = {
                str(s.get("columnIndex", 0)): s["filterCriteria"] for s in basic_filter["filterSpecs"]
            }
        sheet_meta = {
            "properties": {"sheetId": ws.id},
            "conditionalFormats": copy.deepcopy(ws.conditional_formats),
            "data": [{"rowData": [
                {"values": [{"formattedValue": str(v)} if str(v) else {} for v in row]}
                for row in ws.values
            ]}],
        }
        if basic_filter:
            sheet_meta["basicFilter"] = basic_filter
        return drop_zero_fields({"sheets": [sheet_meta]})


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

    opp_ws = sheet.sheets["Opportunities"]
    assert len(opp_ws.conditional_formats) == 1
    rng = opp_ws.conditional_formats[0]["ranges"][0]
    assert rng["startColumnIndex"] == header.index("ROI %")

    assert opp_ws.basic_filter_resets == 0
    assert opp_ws.basic_filter["range"] == {
        "sheetId": opp_ws.id, "startRowIndex": 0, "endRowIndex": len(rows) + 1,
        "startColumnIndex": 0, "endColumnIndex": len(header),
    }

    history = sheet.sheets["History Summary"].values
    assert history[0][0] == "Snapshot"
    assert len(history) == 1 + 3 * 3

    status = dict((r[0], r[1]) for r in sheet.sheets["Status"].values)
    assert status["Gefundene Zeilen"] == 15


def test_empty_scan_keeps_header_row_for_filter():
    sheet = FakeSpreadsheet()
    ws = sheet.add_worksheet("Opportunities", 1000, 25)
    empty = scanner.build_opportunities(events=[])

    assert scanner.write_dataframe_to_sheet(ws, empty) == 0
    assert ws.values == [scanner.OPPORTUNITY_COLUMNS + ["", "Keine passenden Spots gefunden."]]
    assert ws.get_all_records() == []

    ws.values = []
    assert scanner.write_dataframe_to_sheet(ws, pd.DataFrame()) == 0
    assert ws.values == [["Keine passenden Spots gefunden."]]


# ---------------------------------------------------------------------------
# Kategorie-Filter im Opportunities-Tab
# ---------------------------------------------------------------------------

def lol_event():
    return event(
        "LoL MSI 2027 Winner",
        [market(n, p) for n, p in [("Gen.G", 0.3), ("T1", 0.2), ("BLG", 0.15), ("HLE", 0.1), ("G2", 0.1), ("FLY", 0.05)]],
        tags=("league-of-legends", "esports"),
    )


def run_main(monkeypatch, sheet, events):
    monkeypatch.setattr(scanner, "get_events", lambda stats=None: [copy.deepcopy(e) for e in events])
    monkeypatch.setattr(scanner, "connect_google_sheet", lambda: sheet)
    monkeypatch.setattr(scanner, "DATABASE_URL", None)
    scanner.main()
    return sheet.sheets["Opportunities"]


def category_index():
    return scanner.OPPORTUNITY_COLUMNS.index(scanner.CATEGORY_COLUMN)


def test_every_opportunity_has_a_valid_category(real_events):
    df = scanner.build_opportunities(events=[copy.deepcopy(e) for e in real_events] + [lol_event()])

    assert not df.empty
    assert df["Kategorie"].notna().all()
    assert set(df["Kategorie"]) <= set(scanner.CATEGORIES)
    assert set(df["Kategorie"]) == {"CS2", "Fußball", "LoL"}
    assert not df["Event"].str.contains("Dota|BLAST Slam", case=False).any()


def test_category_selection_survives_scanner_updates(monkeypatch, real_events):
    sheet = FakeSpreadsheet()
    ws = run_main(monkeypatch, sheet, real_events)
    assert ws.basic_filter.get("filterSpecs") is None
    assert {r[category_index()] for r in ws.values[1:]} == {"CS2", "Fußball"}

    # Nutzer wählt im Sheet "nur CS2" (Sheets speichert die ausgeblendeten Werte)
    ws.basic_filter["filterSpecs"] = [
        {"columnIndex": category_index(), "filterCriteria": {"hiddenValues": ["Fußball"]}}
    ]
    assert {r[category_index()] for r in ws.visible_data_rows()} == {"CS2"}

    # Nächster Lauf bringt zusätzlich eine LoL-Opportunity
    ws = run_main(monkeypatch, sheet, real_events + [lol_event()])

    assert len(ws.values) - 1 == 20
    assert {r[category_index()] for r in ws.values[1:]} == {"CS2", "Fußball", "LoL"}
    specs = ws.basic_filter["filterSpecs"]
    assert len(specs) == 1
    assert specs[0]["columnIndex"] == category_index()
    hidden = set(specs[0]["filterCriteria"]["hiddenValues"])
    assert "CS2" not in hidden
    assert {"Fußball", "LoL", "Valorant"} <= hidden
    assert {r[category_index()] for r in ws.visible_data_rows()} == {"CS2"}
    assert ws.basic_filter["range"]["endRowIndex"] == 21
    assert ws.basic_filter_resets == 0

    # "Alle Kategorien": Nutzer entfernt die Auswahl -> bleibt nach dem Update ohne Kriterien
    ws.basic_filter.pop("filterSpecs")
    ws = run_main(monkeypatch, sheet, real_events + [lol_event()])
    assert ws.basic_filter.get("filterSpecs") is None
    assert len(ws.visible_data_rows()) == 20


def test_filter_criteria_follow_column_name_and_zero_index():
    header = list(scanner.OPPORTUNITY_COLUMNS)
    old_header = ["Event", "Zeit", "Foo", "Kategorie"]
    state = {
        "basic_filter": {"filterSpecs": [
            {"columnIndex": 3, "filterCriteria": {"hiddenValues": ["LoL"]}},
            # columnIndex 0 fehlt in API-Antworten
            {"filterCriteria": {"condition": {"type": "TEXT_CONTAINS", "values": [{"userEnteredValue": "Major"}]}}},
            {"columnIndex": 2, "filterCriteria": {"hiddenValues": ["x"]}},
        ]},
        "values": [old_header, ["E1", "t", "x", "LoL"], ["E2", "t", "y", "CS2"]],
    }
    df = pd.DataFrame({"Kategorie": ["CS2", "Fußball"]})

    specs = scanner.carry_over_filter_specs(state, header, df)

    assert specs[0]["columnIndex"] == header.index("Kategorie")
    assert set(specs[0]["filterCriteria"]["hiddenValues"]) == set(scanner.CATEGORIES) - {"CS2"}
    assert specs[1]["columnIndex"] == header.index("Event")
    assert specs[1]["filterCriteria"]["condition"]["type"] == "TEXT_CONTAINS"
    assert len(specs) == 2


def test_deprecated_criteria_map_is_supported():
    header = list(scanner.OPPORTUNITY_COLUMNS)
    state = {
        "basic_filter": {"criteria": {"1": {"hiddenValues": ["Fußball"]}}},
        "values": [header, ["t", "CS2"], ["t", "Fußball"]],
    }
    specs = scanner.carry_over_filter_specs(state, header, pd.DataFrame({"Kategorie": ["CS2"]}))
    assert specs[0]["columnIndex"] == 1
    assert "CS2" not in specs[0]["filterCriteria"]["hiddenValues"]


def test_keep_visible_categories():
    keep = scanner.keep_visible_categories
    assert keep({}, ["CS2"], ["LoL"]) == {}
    everything_hidden = {"hiddenValues": ["CS2"]}
    assert keep(everything_hidden, ["CS2"], ["LoL"]) == everything_hidden
    result = keep({"hiddenValues": ["LoL"], "visibleBackgroundColor": {"red": 1}}, ["CS2", "LoL"], ["Valorant"])
    assert set(result["hiddenValues"]) == set(scanner.CATEGORIES) - {"CS2"}
    assert result["visibleBackgroundColor"] == {"red": 1}


def test_filter_survives_empty_scan(monkeypatch, real_events):
    sheet = FakeSpreadsheet()
    ws = run_main(monkeypatch, sheet, real_events)
    ws.basic_filter["filterSpecs"] = [
        {"columnIndex": category_index(), "filterCriteria": {"hiddenValues": ["Fußball"]}}
    ]

    ws = run_main(monkeypatch, sheet, [])
    assert ws.values[0][:len(scanner.OPPORTUNITY_COLUMNS)] == scanner.OPPORTUNITY_COLUMNS
    assert ws.basic_filter["range"]["endRowIndex"] == 1
    assert ws.basic_filter["filterSpecs"][0]["columnIndex"] == category_index()

    ws = run_main(monkeypatch, sheet, real_events)
    assert {r[category_index()] for r in ws.visible_data_rows()} == {"CS2"}


def test_roi_rule_is_replaced_not_duplicated(monkeypatch, real_events):
    sheet = FakeSpreadsheet()
    ws = run_main(monkeypatch, sheet, real_events)
    user_rule = {
        "ranges": [{"sheetId": ws.id, "startColumnIndex": 5, "endColumnIndex": 6}],
        "booleanRule": {"condition": {"type": "NUMBER_LESS", "values": [{"userEnteredValue": "0.1"}]},
                        "format": {"backgroundColor": {"red": 1}}},
    }
    ws.conditional_formats.append(user_rule)
    # gespeicherte Farben sind auf 1/255 gerundet
    ws.conditional_formats[0]["booleanRule"]["format"]["backgroundColor"] = {
        "red": 184 / 255, "green": 224 / 255, "blue": 179 / 255,
    }

    for _ in range(3):
        ws = run_main(monkeypatch, sheet, real_events)

    scanner_rules = [r for r in ws.conditional_formats if scanner.is_scanner_roi_rule(r)]
    assert len(scanner_rules) == 1
    assert user_rule in ws.conditional_formats
    assert len(ws.conditional_formats) == 2


def test_unreadable_filter_state_does_not_break_update(monkeypatch, real_events):
    sheet = FakeSpreadsheet()
    ws = run_main(monkeypatch, sheet, real_events)
    ws.basic_filter["filterSpecs"] = [
        {"columnIndex": category_index(), "filterCriteria": {"hiddenValues": ["Fußball"]}}
    ]
    sheet.fail_metadata = True

    ws = run_main(monkeypatch, sheet, real_events)

    assert len(ws.values) - 1 == 15
    assert ws.basic_filter.get("filterSpecs") is None
    assert ws.basic_filter["range"]["endRowIndex"] == 16


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
