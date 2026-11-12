"""PostgreSQL-Integration: Verbindung, Migration, Upsert, Auflösung, Stats/DB Outcomes.

Tests mit echter Datenbank laufen nur mit TEST_DATABASE_URL (eigene Test-DB, nie Produktion);
jeder Test bekommt ein eigenes Schema, das danach wieder gelöscht wird.
"""
import copy
import os
import uuid
import warnings

import pandas as pd
import psycopg2
import pytest

from test_scanner import FakeSpreadsheet, by_title
import scanner

TEST_DB = os.getenv("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not TEST_DB, reason="TEST_DATABASE_URL nicht gesetzt")

# Schema der ersten Version (Mai 2026), noch ohne real_*-Spalten.
LEGACY_DDL = """
    CREATE TABLE outcomes (
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
"""

UCL = "UEFA Champions League: 2027 Champion"
PGL = "PGL Major Singapore 2026: Winner"
WC = "World Cup 2030: Location of Final"


@pytest.fixture
def db_url(monkeypatch):
    schema = f"scanner_test_{uuid.uuid4().hex[:12]}"
    admin = psycopg2.connect(TEST_DB)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema}")

    url = f"{TEST_DB}{'&' if '?' in TEST_DB else '?'}options=-csearch_path%3D{schema}"
    monkeypatch.setattr(scanner, "DATABASE_URL", url)
    yield url

    with admin.cursor() as cur:
        cur.execute(f"DROP SCHEMA {schema} CASCADE")
    admin.close()


def sql(url, query, params=None):
    conn = psycopg2.connect(url)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall() if cur.description else None
    finally:
        conn.close()


def table_columns(url):
    return [r[0] for r in sql(url, """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'outcomes'
        ORDER BY ordinal_position
    """)]


def open_db():
    status = scanner.DBStatus()
    conn = scanner.open_database(status)
    assert conn is not None, status.connection
    return conn, status


def migrated(url):
    conn, status = open_db()
    ok, _ = scanner.run_db_step(conn, status, "Migration", scanner.migrate_db)
    assert ok
    conn.close()


def opportunities(real_events, *titles):
    events = [copy.deepcopy(e) for e in real_events if not titles or e["title"] in titles]
    return scanner.build_opportunities(events=events)


def slug_of(df, title):
    return df.loc[df["Event"] == title, "Event Slug"].iloc[0]


def resolved_pgl(real_events, winner="Vitality"):
    pgl = by_title(real_events, PGL)
    for m in pgl["markets"]:
        m["closed"] = True
        m["outcomePrices"] = '["1", "0"]' if m.get("groupItemTitle") == winner else '["0", "1"]'
    return pgl


def run_scanner(monkeypatch, sheet, events, resolved_events=None):
    resolved_events = resolved_events or {}
    monkeypatch.setattr(scanner, "get_events", lambda stats=None: [copy.deepcopy(e) for e in events])
    monkeypatch.setattr(scanner, "connect_google_sheet", lambda: sheet)
    monkeypatch.setattr(
        scanner, "fetch_events_by_slugs",
        lambda slugs: {s: copy.deepcopy(resolved_events[s]) for s in slugs if s in resolved_events},
    )
    scanner.main()


def status_block(output):
    block = output.split("========== DATABASE STATUS ==========")[1]
    return dict(
        line.split(": ", 1) for line in block.splitlines()
        if ": " in line and not line.startswith("  ")
    )


def seed_sheet_tab(sheet, title, values):
    ws = sheet.add_worksheet(title, 1000, 12)
    ws.values = copy.deepcopy(values)
    return ws


OLD_STATS = [["Gesamt"], ["total", "resolved", "wins", "losses", "avg_roi"], ["99", "50", "30", "20", "9.5"], []]
OLD_DB_OUTCOMES = [
    ["category", "event", "best_roi", "top_sum", "real_payout", "real_profit", "winner", "top5_won", "last_updated"],
    ["CS2", "Historic Major", "12.5", "0.88", "", "", "Will X win?", "True", "2026-06-01 10:00:00"],
]


# ---------------------------------------------------------------------------
# Verbindung, Secrets, Ausfall (ohne echte DB)
# ---------------------------------------------------------------------------

def test_secrets_are_redacted(monkeypatch):
    url = "postgresql://postgres:S3cr3t-P%40ss@postgres.railway.internal:5432/railway"
    monkeypatch.setattr(scanner, "DATABASE_URL", f"  '{url}'\n")

    text = scanner.redact_secrets(f"failed for {url} with S3cr3t-P@ss / S3cr3t-P%40ss and password=abc")
    assert "S3cr3t" not in text
    assert "postgres.railway.internal:5432" not in text
    assert "password=***" in text

    other = scanner.redact_secrets("dsn postgresql://admin:hunter22@db.example.com/x")
    assert "hunter22" not in other and "postgresql://***@db.example.com/x" in other


def test_database_url_is_cleaned_and_described():
    assert scanner.clean_database_url('  "postgresql://u:p@h/db" ') == "postgresql://u:p@h/db"
    assert scanner.clean_database_url("   ") is None
    describe = scanner.describe_database_url
    assert describe("postgresql://u:p@postgres.railway.internal:5432/railway") == "Railway Private Network"
    assert describe("postgres://u:p@roundhouse.proxy.rlwy.net:12345/railway") == "Railway Public Proxy"
    assert describe("host=localhost dbname=x") == "Key/Value-DSN"
    assert describe("mysql://u:p@h/db") == "unbekanntes URL-Format"


def test_connection_uses_timeouts(monkeypatch):
    seen = {}

    def fake_connect(dsn, **kwargs):
        seen.update(kwargs, dsn=dsn)
        raise psycopg2.OperationalError("timeout expired")

    monkeypatch.setattr(scanner, "DATABASE_URL", "postgresql://u:pw123@10.255.255.1:5432/railway")
    monkeypatch.setattr(scanner.psycopg2, "connect", fake_connect)
    monkeypatch.setattr(scanner, "DB_CONNECT_RETRIES", 0)

    status = scanner.DBStatus()
    assert scanner.open_database(status) is None
    assert seen["connect_timeout"] == scanner.DB_CONNECT_TIMEOUT
    assert seen["application_name"] == "polymarket-scanner"
    assert status.connection == "FEHLER (OperationalError: timeout expired)"


def test_missing_database_url_is_reported(monkeypatch, real_events, capsys):
    monkeypatch.setattr(scanner, "DATABASE_URL", None)
    sheet = FakeSpreadsheet()
    run_scanner(monkeypatch, sheet, real_events)

    status = status_block(capsys.readouterr().out)
    assert status["DATABASE_URL configured"] == "NO"
    assert "Stats" not in sheet.sheets and "DB Outcomes" not in sheet.sheets
    assert dict((r[0], r[1]) for r in sheet.sheets["Status"].values)["PostgreSQL"] == "nicht konfiguriert"


def test_unreachable_database_keeps_scanner_and_sheets_working(monkeypatch, real_events, capsys):
    monkeypatch.setattr(scanner, "DATABASE_URL", "postgresql://postgres:TopSecret99@127.0.0.1:1/railway")
    monkeypatch.setattr(scanner, "DB_CONNECT_RETRIES", 1)
    monkeypatch.setattr(scanner.time, "sleep", lambda s: None)
    sheet = FakeSpreadsheet()
    stats_ws = seed_sheet_tab(sheet, "Stats", OLD_STATS)
    outcomes_ws = seed_sheet_tab(sheet, "DB Outcomes", OLD_DB_OUTCOMES)

    run_scanner(monkeypatch, sheet, real_events)

    output = capsys.readouterr().out
    assert "TopSecret99" not in output
    status = status_block(output)
    assert status["DATABASE_URL configured"] == "YES (localhost)"
    assert status["PostgreSQL connection"].startswith("FEHLER (OperationalError")
    assert status["Records inserted"] == "–"
    assert len(sheet.sheets["Opportunities"].values) == 16
    assert stats_ws.values == OLD_STATS
    assert outcomes_ws.values == OLD_DB_OUTCOMES
    assert dict((r[0], r[1]) for r in sheet.sheets["Status"].values)["PostgreSQL"] == "FEHLER"


def test_grid_grows_for_large_tabs():
    sheet = FakeSpreadsheet()
    ws = sheet.add_worksheet("DB Outcomes", 1000, 12)
    values = [["h"] * 13] + [["x"] * 13 for _ in range(1500)]
    scanner.write_values(ws, values)
    assert ws.row_count == 1501 and ws.col_count == 13
    assert ws.values == values


def test_sheet_cells_never_contain_nan():
    df = pd.DataFrame.from_records(
        [("CS2", 1.5, None, pd.Timestamp("2026-11-12 15:00:00")), ("LoL", None, True, None)],
        columns=["category", "real_payout", "top5_won", "last_updated"],
    )
    assert scanner.dataframe_rows(df) == [
        ["CS2", "1.5", "", "2026-11-12 15:00:00"],
        ["LoL", "", "True", ""],
    ]


# ---------------------------------------------------------------------------
# Schema und Migration
# ---------------------------------------------------------------------------

@needs_db
def test_migration_creates_full_schema_and_is_idempotent(db_url):
    conn, status = open_db()
    ok, changes = scanner.run_db_step(conn, status, "Migration", scanner.migrate_db)
    assert ok and changes == ["Tabelle outcomes angelegt"]

    ok, changes = scanner.run_db_step(conn, status, "Migration", scanner.migrate_db)
    assert ok and changes == []

    scanner.run_db_step(conn, status, "Schema-Check", scanner.check_outcomes_table, writes=False)
    conn.close()
    assert status.table == "OK" and status.records_before == 0
    assert table_columns(db_url) == [name for name, _ in scanner.OUTCOME_SCHEMA]


@needs_db
def test_migration_repairs_partial_legacy_table_without_touching_rows(db_url):
    # Fall, den die alte migrate_db() nicht reparieren konnte: real_payout existiert,
    # das erste ALTER scheitert und bricht die Transaktion für die übrigen ab.
    sql(db_url, LEGACY_DDL + "; ALTER TABLE outcomes ADD COLUMN real_payout FLOAT")
    sql(db_url, """
        INSERT INTO outcomes VALUES
        ('old-major', 'Old Major', 'CS2', 12.5, 0.88, 'A @ 0.3 | B @ 0.2', 'Will A win?', TRUE,
         '2026-05-12 10:00', '2026-05-20 10:00', 112.0),
        ('blast-slam-ix-winner', 'BLAST Slam IX', 'Dota 2', 7.0, 0.93, 'X @ 0.4', NULL, NULL,
         '2026-05-12 11:00', '2026-05-12 11:00', NULL)
    """)
    before = sql(db_url, "SELECT * FROM outcomes ORDER BY event_slug")

    conn, status = open_db()
    scanner.sync_outcomes_db(conn, status, scanner.build_opportunities(events=[]))
    conn.close()

    assert status.migration == "OK (Spalte real_profit ergänzt, Spalte real_cost ergänzt)"
    assert status.table == "OK"
    assert status.records_before == 2 and status.records_after == 2
    assert table_columns(db_url)[-3:] == ["real_payout", "real_profit", "real_cost"]
    after = sql(db_url, "SELECT * FROM outcomes ORDER BY event_slug")
    assert [row[:11] for row in after] == before
    assert all(row[11:] == (None, None) for row in after)


@needs_db
def test_missing_unique_key_is_added_when_slugs_are_unique(db_url, real_events):
    sql(db_url, LEGACY_DDL.replace("TEXT PRIMARY KEY", "TEXT"))
    sql(db_url, "INSERT INTO outcomes (event_slug, event, best_roi) VALUES ('a', 'A', 6.0)")

    conn, status = open_db()
    scanner.sync_outcomes_db(conn, status, opportunities(real_events))
    conn.close()

    assert "eindeutiger Index auf event_slug ergänzt" in status.migration
    assert status.table == "OK"
    assert (status.inserted, status.updated) == (3, 0)


@needs_db
def test_duplicate_slugs_block_writes_but_never_delete_data(db_url, real_events):
    sql(db_url, LEGACY_DDL.replace("TEXT PRIMARY KEY", "TEXT"))
    sql(db_url, "INSERT INTO outcomes (event_slug, event, best_roi) VALUES ('a', 'A', 6.0), ('a', 'A', 7.0)")

    conn, status = open_db()
    scanner.sync_outcomes_db(conn, status, opportunities(real_events))
    conn.close()

    assert status.table.startswith("PROBLEM") and "1 doppelte Slugs" in status.table
    assert status.inserted is None
    assert sql(db_url, "SELECT event_slug, best_roi FROM outcomes ORDER BY best_roi") == [("a", 6.0), ("a", 7.0)]


@needs_db
def test_session_timeouts_are_set(db_url):
    conn = scanner.connect_db()
    with conn.cursor() as cur:
        cur.execute("SHOW statement_timeout")
        statement_timeout = cur.fetchone()[0]
        cur.execute("SHOW lock_timeout")
        lock_timeout = cur.fetchone()[0]
        cur.execute("SHOW application_name")
        application_name = cur.fetchone()[0]
    conn.close()
    assert (statement_timeout, lock_timeout, application_name) == ("30s", "5s", "polymarket-scanner")


# ---------------------------------------------------------------------------
# Schreiben: Upsert und Auflösung
# ---------------------------------------------------------------------------

@needs_db
def test_upsert_counts_and_preserves_history(db_url, real_events):
    migrated(db_url)
    df = opportunities(real_events)
    pgl, ucl, wc = slug_of(df, PGL), slug_of(df, UCL), slug_of(df, WC)
    sql(db_url, """
        INSERT INTO outcomes
            (event_slug, event, category, best_roi, top_sum, teams, winner, top5_won,
             first_seen, last_updated, real_payout, real_profit, real_cost)
        VALUES
            (%s, 'PGL', 'CS2', 30.0, 0.77, 'old teams', NULL, NULL,
             '2026-01-01', '2026-01-02', 123.0, 23.0, 100.0),
            (%s, 'UCL', 'Fußball', 10.0, 0.91, 'old ucl', 'Will X win?', FALSE,
             '2026-02-01', '2026-02-02', NULL, NULL, NULL),
            ('blast-slam-ix-winner', 'BLAST Slam IX', 'Dota 2', 7.0, 0.93, 'X', NULL, NULL,
             '2026-03-01', '2026-03-01', NULL, NULL, NULL)
    """, (pgl, ucl))

    conn, status = open_db()
    ok, counts = scanner.run_db_step(conn, status, "Upsert", scanner.save_outcome_to_db, df)
    conn.close()
    assert ok and counts == (1, 2, 1)

    rows = {r[0]: r for r in sql(db_url, """
        SELECT event_slug, best_roi, top_sum, teams, winner, top5_won, first_seen,
               last_updated, real_payout, real_profit, real_cost, category
        FROM outcomes
    """)}
    assert len(rows) == 4

    # Höherer historischer ROI bleibt, ebenso first_seen und real_*; last_updated wird aktualisiert.
    assert rows[pgl][1:4] == (30.0, 0.77, "old teams")
    assert str(rows[pgl][6]) == "2026-01-01 00:00:00"
    assert rows[pgl][7].year == 2026 and str(rows[pgl][7]) > "2026-01-02"
    assert rows[pgl][8:11] == (123.0, 23.0, 100.0)

    # Neuer ROI-Höchstwert ersetzt best_roi/top_sum/teams, Gewinner bleibt unangetastet.
    assert rows[ucl][1] == pytest.approx(19.76)
    assert rows[ucl][2] == pytest.approx(0.835)
    assert "Barcelona" in rows[ucl][3]
    assert rows[ucl][4:6] == ("Will X win?", False)

    assert rows[wc][1] > 5 and rows[wc][4] is None and rows[wc][6] is not None
    assert rows["blast-slam-ix-winner"][11] == "Dota 2"


@needs_db
def test_duplicate_slugs_in_one_scan_are_merged(db_url):
    migrated(db_url)
    df = pd.DataFrame([
        {"Event": "Old title", "Event Slug": "same", "Kategorie": "CS2", "ROI %": 8.0,
         "Summe Top 5": 0.92, "Team / Markt": "Will A win?", "YES Preis": 0.4},
        {"Event": "New title", "Event Slug": "same", "Kategorie": "CS2", "ROI %": 9.0,
         "Summe Top 5": 0.91, "Team / Markt": "Will B win?", "YES Preis": 0.3},
        {"Event": "Below", "Event Slug": "low", "Kategorie": "CS2", "ROI %": 4.0,
         "Summe Top 5": 0.96, "Team / Markt": "Will C win?", "YES Preis": 0.3},
    ])

    conn, status = open_db()
    ok, counts = scanner.run_db_step(conn, status, "Upsert", scanner.save_outcome_to_db, df)
    conn.close()

    assert ok and counts == (1, 0, 0)
    assert sql(db_url, "SELECT event_slug, best_roi, teams FROM outcomes") == [("same", 9.0, "Will B win? @ 0.3")]


@needs_db
def test_resolution_is_batched_and_never_overwrites_a_winner(db_url, monkeypatch):
    migrated(db_url)
    sql(db_url, """
        INSERT INTO outcomes (event_slug, event, teams, winner, top5_won, last_updated)
        SELECT 'open-' || i, 'Open ' || i, 'Will A win? @ 0.3 | Will B win? @ 0.2', NULL, NULL, '2026-01-01'
        FROM generate_series(0, 43) i;
        INSERT INTO outcomes (event_slug, event, teams, winner, top5_won, last_updated) VALUES
            ('done', 'Done', 'Will A win? @ 0.3', 'Will A win?', TRUE, '2026-01-01'),
            ('no-teams', 'No teams', NULL, NULL, NULL, '2026-01-01');
    """)

    def closed_event(slug, winner):
        markets = [
            {"question": q, "closed": True, "outcomes": '["Yes", "No"]',
             "outcomePrices": '["1", "0"]' if q == winner else '["0", "1"]'}
            for q in ("Will A win?", "Will B win?", "Will Z win?")
        ]
        return {"slug": slug, "markets": markets}

    api = {
        "open-0": closed_event("open-0", "Will A win?"),
        "open-1": closed_event("open-1", "Will Z win?"),
        "no-teams": closed_event("no-teams", "Will A win?"),
        "done": closed_event("done", "Will Z win?"),
    }
    calls = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    def fake_gamma_get(url, params):
        slugs = [v for k, v in params if k == "slug"]
        calls.append(slugs)
        return Response([api.get(s, {"slug": s, "markets": []}) for s in slugs])

    monkeypatch.setattr(scanner, "gamma_get", fake_gamma_get)

    conn, status = open_db()
    ok, result = scanner.run_db_step(conn, status, "Auflösung", scanner.resolve_outcomes_db)
    conn.close()

    assert ok and result == (45, 3)
    assert [len(c) for c in calls] == [40, 5]
    assert "done" not in sum(calls, [])
    rows = {r[0]: r[1:] for r in sql(db_url, "SELECT event_slug, winner, top5_won FROM outcomes")}
    assert rows["open-0"] == ("Will A win?", True)
    assert rows["open-1"] == ("Will Z win?", False)
    assert rows["no-teams"] == ("Will A win?", None)
    assert rows["done"] == ("Will A win?", True)
    assert rows["open-2"] == (None, None)


@needs_db
def test_lost_connection_skips_remaining_steps(db_url, real_events):
    migrated(db_url)
    conn, status = open_db()

    def terminate(conn, status):
        with conn.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(pg_backend_pid())")

    ok, _ = scanner.run_db_step(conn, status, "Abbruch", terminate)
    assert not ok and conn.closed
    ok, _ = scanner.run_db_step(conn, status, "Upsert", scanner.save_outcome_to_db, opportunities(real_events))
    assert not ok
    assert status.errors[-1] == "Upsert: übersprungen (DB-Verbindung verloren)"
    assert status.committed_text() == "NO – Rollback: Abbruch"
    assert sql(db_url, "SELECT COUNT(*) FROM outcomes") == [(0,)]


# ---------------------------------------------------------------------------
# Google Sheets: Stats, DB Outcomes, History Summary
# ---------------------------------------------------------------------------

STATS_SEED = """
    INSERT INTO outcomes (event_slug, event, category, best_roi, top_sum, teams, winner, top5_won,
                          first_seen, last_updated) VALUES
        ('a', 'A', 'CS2', 6.0, 0.94, 't', 'w', TRUE, '2026-01-01', '2026-01-01 10:00'),
        ('b', 'B', 'CS2', 9.0, 0.91, 't', 'w', FALSE, '2026-01-01', '2026-01-01 11:00'),
        ('c', 'C', 'LoL', 12.0, 0.89, 't', 'w', TRUE, '2026-01-01', '2026-01-01 12:00'),
        ('d', 'D', 'Fußball', 20.0, 0.83, 't', NULL, NULL, '2026-01-01', '2026-01-01 13:00'),
        ('e', 'E', 'Dota 2', 7.0, 0.93, 't', 'w', TRUE, '2026-01-01', '2026-01-01 14:00')
"""


def legacy_sheet_rows(url, query):
    """Ausgabe der bisherigen Implementierung (pd.read_sql_query + fillna + astype(str))."""
    conn = psycopg2.connect(url)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df = pd.read_sql_query(query, conn).fillna("")
    conn.close()
    return [[str(v) for v in row] for row in df.astype(object).values.tolist()]


@needs_db
def test_stats_and_db_outcomes_reflect_database(db_url):
    migrated(db_url)
    sql(db_url, STATS_SEED)
    sheet = FakeSpreadsheet()
    conn, status = open_db()

    assert scanner.update_stats_sheet_from_db(sheet, conn, status)
    assert scanner.update_db_outcomes_sheet(sheet, conn, status)
    conn.close()
    assert status.stats_query == "OK (5 Datensätze)"
    assert status.db_outcomes_query == "OK (5 Zeilen)"

    stats = sheet.sheets["Stats"].values
    assert stats[:3] == [["Gesamt"], ["total", "resolved", "wins", "losses", "avg_roi"], ["5", "4", "3", "1", "10.8"]]
    by_category = {r[0]: r for r in stats[6:10]}
    assert by_category["CS2"] == ["CS2", "2", "2", "1", "1", "50.0", "7.5"]
    assert by_category["Fußball"] == ["Fußball", "1", "0", "0", "0", "", "20.0"]
    assert by_category["Dota 2"][1] == "1"
    ranges = {r[0]: r for r in stats[13:17]}
    assert ranges["5-8%"][1:6] == ["2", "2", "2", "0", "100.0"]
    assert ranges["15%+"][1:6] == ["1", "0", "0", "0", ""]

    # Gleiche Zellwerte wie die bisherige pd.read_sql_query-Ausgabe
    position = 0
    for title, query in scanner.STATS_QUERIES.items():
        legacy = legacy_sheet_rows(db_url, query)
        assert stats[position] == [title]
        assert sorted(stats[position + 2:position + 2 + len(legacy)]) == sorted(legacy)
        position += len(legacy) + 3

    outcomes = sheet.sheets["DB Outcomes"].values
    assert outcomes[0] == ["category", "event", "best_roi", "top_sum", "real_payout", "real_profit",
                           "winner", "top5_won", "last_updated"]
    assert [r[1] for r in outcomes[1:]] == ["D", "C", "B", "E", "A"]
    assert outcomes[1][6:9] == ["", "", "2026-01-01 13:00:00"]
    assert outcomes[2][6:8] == ["w", "True"]
    assert outcomes[3][7] == "False"
    assert outcomes[1:] == legacy_sheet_rows(db_url, scanner.DB_OUTCOMES_QUERY)


@needs_db
def test_failed_query_does_not_overwrite_tab(db_url, monkeypatch):
    migrated(db_url)
    sql(db_url, STATS_SEED)
    sheet = FakeSpreadsheet()
    stats_ws = seed_sheet_tab(sheet, "Stats", OLD_STATS)
    monkeypatch.setattr(scanner, "STATS_QUERIES", {"Gesamt": "SELECT no_such_column FROM outcomes"})

    conn, status = open_db()
    assert not scanner.update_stats_sheet_from_db(sheet, conn, status)
    # Rollback lässt die Verbindung benutzbar
    assert scanner.update_db_outcomes_sheet(sheet, conn, status)
    conn.close()

    assert stats_ws.values == OLD_STATS
    assert status.stats_query == "FEHLER – Tab nicht überschrieben"
    assert status.errors == ['Stats-Abfrage: UndefinedColumn: column "no_such_column" does not exist']
    assert len(sheet.sheets["DB Outcomes"].values) == 6


@needs_db
def test_empty_database_does_not_overwrite_tabs(db_url):
    migrated(db_url)
    sheet = FakeSpreadsheet()
    stats_ws = seed_sheet_tab(sheet, "Stats", OLD_STATS)
    outcomes_ws = seed_sheet_tab(sheet, "DB Outcomes", OLD_DB_OUTCOMES)

    conn, status = open_db()
    assert not scanner.update_stats_sheet_from_db(sheet, conn, status)
    assert not scanner.update_db_outcomes_sheet(sheet, conn, status)

    assert stats_ws.values == OLD_STATS
    assert outcomes_ws.values == OLD_DB_OUTCOMES
    assert status.db_outcomes_query == "OK, aber DB leer – Tab behält 1 Zeilen"

    # Ohne bisherige Daten wird der leere Zustand normal angezeigt.
    fresh = FakeSpreadsheet()
    assert scanner.update_stats_sheet_from_db(fresh, conn, status)
    assert scanner.update_db_outcomes_sheet(fresh, conn, status)
    conn.close()
    assert fresh.sheets["Stats"].values[2] == ["0", "0", "0", "0", ""]
    assert fresh.sheets["DB Outcomes"].values == [["Keine DB Outcomes vorhanden."]]


@needs_db
def test_scanner_runs_keep_database_and_sheets_in_sync(db_url, monkeypatch, real_events, capsys):
    sheet = FakeSpreadsheet()

    run_scanner(monkeypatch, sheet, real_events)
    first = status_block(capsys.readouterr().out)
    assert first["DATABASE_URL configured"] == "YES (localhost)"
    assert first["PostgreSQL connection"] == "OK"
    assert first["Database migration"] == "OK (Tabelle outcomes angelegt)"
    assert first["Outcomes table"] == "OK"
    assert first["Existing records"] == "0 (davon aufgelöst: 0)"
    assert first["Records inserted"] == "3"
    assert first["Records updated"] == "0 (neuer ROI-Höchstwert: 0)"
    assert first["Transaction committed"] == "YES (Migration, Upsert, Auflösung)"
    assert first["Records after run"] == "3"
    assert first["Stats query"] == "OK (3 Datensätze)"
    assert first["DB Outcomes query"] == "OK (3 Zeilen)"
    assert len(sheet.sheets["DB Outcomes"].values) == 4
    status_tab = dict((r[0], r[1]) for r in sheet.sheets["Status"].values)
    assert (status_tab["PostgreSQL"], status_tab["DB Datensätze"]) == ("OK", 3)

    pgl_event = resolved_pgl(real_events)
    run_scanner(monkeypatch, sheet, real_events, {pgl_event["slug"]: pgl_event})
    second = status_block(capsys.readouterr().out)
    assert second["Existing records"] == "3 (davon aufgelöst: 0)"
    assert second["Records inserted"] == "0"
    assert second["Records updated"] == "3 (neuer ROI-Höchstwert: 0)"
    assert second["Results resolved"] == "1 (von 3 offenen geprüft)"
    assert second["History results"] == "OK (1 aufgelöste Events in DB, 3 Zeilen ergänzt)"

    winner = next(m["question"] for m in pgl_event["markets"] if m.get("groupItemTitle") == "Vitality")
    assert sql(db_url, "SELECT winner, top5_won FROM outcomes WHERE event_slug = %s", (pgl_event["slug"],)) == [
        (winner, True)
    ]
    outcomes = sheet.sheets["DB Outcomes"].values
    pgl_row = next(r for r in outcomes if r[1] == PGL)
    assert pgl_row[6:8] == [winner, "True"]
    assert sheet.sheets["Stats"].values[2][:4] == ["3", "1", "1", "0"]

    history = sheet.sheets["History Summary"].values
    header = history[0]
    pgl_history = [r for r in history[1:] if r[header.index("Event")] == PGL]
    assert len(pgl_history) == 3
    assert all(r[header.index("Gewinner")] == winner for r in pgl_history)
    assert all(r[header.index("Top 5 gewonnen?")] == "JA" for r in pgl_history)

    # Dritter Lauf: Quelle meldet nichts mehr – gespeicherte Ergebnisse bleiben erhalten.
    run_scanner(monkeypatch, sheet, real_events)
    third = status_block(capsys.readouterr().out)
    assert third["Results resolved"] == "0 (von 2 offenen geprüft)"
    assert sql(db_url, "SELECT winner FROM outcomes WHERE event_slug = %s", (pgl_event["slug"],)) == [(winner,)]
    history = sheet.sheets["History Summary"].values
    assert all(r[header.index("Top 5 gewonnen?")] == "JA" for r in history[1:] if r[header.index("Event")] == PGL)


# ---------------------------------------------------------------------------
# Read-only Diagnose (python scanner.py --db-check)
# ---------------------------------------------------------------------------

@needs_db
def test_db_check_is_read_only(db_url, capsys):
    assert scanner.check_database() == 1
    output = capsys.readouterr().out
    assert "Outcomes table: FEHLT (wird beim nächsten Scanner-Lauf angelegt)" in output
    assert sql(db_url, "SELECT to_regclass('outcomes') IS NULL") == [(True,)]

    migrated(db_url)
    sql(db_url, STATS_SEED)
    before = sql(db_url, "SELECT * FROM outcomes ORDER BY event_slug")

    assert scanner.check_database() == 0
    status = status_block(capsys.readouterr().out)
    assert status["Outcomes table"] == "OK"
    assert status["Existing records"] == "5 (davon aufgelöst: 4)"
    assert status["Stats query"] == "OK (5 Datensätze)"
    assert status["DB Outcomes query"] == "OK (5 Zeilen)"
    assert status["Transaction committed"] == "– (keine Schreibvorgänge)"
    assert sql(db_url, "SELECT * FROM outcomes ORDER BY event_slug") == before
