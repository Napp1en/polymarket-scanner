import os
import json
import requests
import pandas as pd
import gspread
import psycopg2
from psycopg2.extras import execute_values
from datetime import datetime
from google.oauth2.service_account import Credentials

# =====================
# Einstellungen
# =====================

BANKROLL = 100
TOP_N = 5
MIN_RAW_ROI = 0.05          # 0.05 = 5%
MIN_HISTORY_ROI = 5.0      # 5.0 = 5%

EVENT_LIMIT = 250
MAX_PAGES = 4

EVENTS_URL = "https://gamma-api.polymarket.com/events"
BOOK_URL = "https://clob.polymarket.com/book"

SHEET_ID = os.getenv("SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
DATABASE_URL = os.getenv("DATABASE_URL")

CATEGORIES = {
    "CS2": [
        "counter-strike", "counter strike", "cs2", "pgl", "blast premier",
        "iem", "esl pro league", "fissure"
    ],
    "LoL": [
        "league of legends",
        "world championship league of legends",
        "mid-season invitational",
        "msi league of legends",
        "lck winner",
        "lec winner",
        "lpl winner"
    ],
    "Dota 2": [
        "dota", "the international", "dreamleague", "riyadh masters"
    ],
    "Valorant": [
        "valorant", "vct", "valorant champions", "masters valorant"
    ],
    "Fußball": [
        "champions league",
        "europa league",
        "premier league",
        "bundesliga",
        "la liga",
        "serie a",
        "world cup",
        "euro"
    ],
    "Basketball": [
        "nba champion", "nba finals", "euroleague"
    ],
    "Tennis": [
        "wimbledon", "french open", "us open", "australian open",
        "atp finals", "wta finals"
    ],
}


# =====================
# Helper
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


def event_text(event):
    return " ".join([
        str(event.get("title", "")),
        str(event.get("slug", "")),
        str(event.get("description", "")),
        str(event.get("seriesSlug", "")),
        str(event.get("category", "")),
    ]).lower()


def get_event_category(event):
    text = event_text(event)

    for category, keywords in CATEGORIES.items():
        if any(k in text for k in keywords):
            return category

    return None


def get_events(limit=EVENT_LIMIT, max_pages=MAX_PAGES):
    events_by_id = {}
    orders = ["volume_24hr"]

    for order in orders:
        print(f"Lade Events nach: {order}")

        for page in range(max_pages):
            params = {
                "active": "true",
                "closed": "false",
                "limit": limit,
                "offset": page * limit,
                "order": order,
                "ascending": "false",
            }

            r = requests.get(EVENTS_URL, params=params, timeout=20)
            r.raise_for_status()
            batch = r.json()

            if not batch:
                break

            for e in batch:
                event_id = e.get("id") or e.get("slug")
                events_by_id[event_id] = e

            print(f"  Seite {page + 1}: {len(batch)} Events")

            if len(batch) < limit:
                break

    return list(events_by_id.values())


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

            if isinstance(data, dict) and data.get("slug") == slug:
                return data

        except Exception as e:
            print(f"Fehler beim Laden von Event {slug}: {e}")

    return None


def get_yes_token_id(market):
    outcomes = parse_list(market.get("outcomes"))
    token_ids = parse_list(market.get("clobTokenIds"))

    for outcome, token_id in zip(outcomes, token_ids):
        if str(outcome).lower() == "yes":
            return str(token_id)

    return None


def get_orderbook(token_id):
    try:
        r = requests.get(BOOK_URL, params={"token_id": token_id}, timeout=10)

        if r.status_code != 200:
            return []

        data = r.json()
        asks = data.get("asks", [])

        clean_asks = []

        for ask in asks:
            try:
                price = float(ask["price"])
                size = float(ask["size"])

                if price > 0 and size > 0:
                    clean_asks.append({
                        "price": price,
                        "size": size,
                    })

            except Exception:
                continue

        clean_asks.sort(key=lambda x: x["price"])
        return clean_asks

    except Exception:
        return []


def best_ask(asks):
    if not asks:
        return None
    return asks[0]["price"]


def total_depth(asks):
    return sum(level["size"] for level in asks)


def cost_to_buy_shares(asks, shares_needed):
    remaining = shares_needed
    cost = 0.0

    for level in asks:
        take = min(remaining, level["size"])
        cost += take * level["price"]
        remaining -= take

        if remaining <= 1e-9:
            return cost

    return None


def find_equal_payout(selected, bankroll):
    max_possible_q = min(total_depth(team["asks"]) for team in selected)

    low = 0.0
    high = max_possible_q

    for _ in range(60):
        mid = (low + high) / 2

        total_cost = 0.0
        possible = True

        for team in selected:
            cost = cost_to_buy_shares(team["asks"], mid)

            if cost is None:
                possible = False
                break

            total_cost += cost

        if possible and total_cost <= bankroll:
            low = mid
        else:
            high = mid

    payout = low
    rows = []
    real_cost = 0.0

    for team in selected:
        cost = cost_to_buy_shares(team["asks"], payout)

        if cost is None or payout <= 0:
            continue

        avg_price = cost / payout
        real_cost += cost

        rows.append({
            "team": team["team"],
            "best_ask": best_ask(team["asks"]),
            "avg_price": avg_price,
            "stake": cost,
            "shares": payout,
            "depth": total_depth(team["asks"]),
        })

    return payout, real_cost, rows


# =====================
# Google Sheets
# =====================

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


def get_or_create_worksheet(sheet, title, rows=1000, cols=30):
    try:
        return sheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return sheet.add_worksheet(title=title, rows=rows, cols=cols)


def write_dataframe_to_sheet(ws, df):
    ws.clear()

    if df.empty:
        ws.update(values=[["Keine passenden Spots gefunden."]], range_name="A1")
        return

    values = [df.columns.tolist()] + df.astype(str).values.tolist()
    ws.update(values=values, range_name="A1")


def format_opportunities_sheet(ws):
    ws.freeze(rows=1)
    ws.set_basic_filter()

    ws.format("A1:P1", {
        "backgroundColor": {"red": 0.12, "green": 0.31, "blue": 0.47},
        "textFormat": {
            "foregroundColor": {"red": 1, "green": 1, "blue": 1},
            "bold": True,
        },
        "horizontalAlignment": "CENTER",
    })

    ws.format("A:P", {
        "verticalAlignment": "MIDDLE",
        "wrapStrategy": "WRAP",
    })

    requests = [
        {"updateDimensionProperties": {"range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}, "properties": {"pixelSize": 150}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2}, "properties": {"pixelSize": 110}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 2, "endIndex": 3}, "properties": {"pixelSize": 280}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 4, "endIndex": 5}, "properties": {"pixelSize": 430}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 15, "endIndex": 16}, "properties": {"pixelSize": 420}, "fields": "pixelSize"}},
    ]

    ws.spreadsheet.batch_update({"requests": requests})


def color_events_and_roi(ws, df):
    if df.empty:
        return

    requests = []

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
                    "endColumnIndex": 16,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": event_map[event]
                    }
                },
                "fields": "userEnteredFormat.backgroundColor",
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
        {"updateDimensionProperties": {"range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}, "properties": {"pixelSize": 160}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2}, "properties": {"pixelSize": 120}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 2, "endIndex": 3}, "properties": {"pixelSize": 300}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 8, "endIndex": 9}, "properties": {"pixelSize": 500}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 11, "endIndex": 12}, "properties": {"pixelSize": 420}, "fields": "pixelSize"}},
    ]

    ws.spreadsheet.batch_update({"requests": requests})


# =====================
# Scanner
# =====================

def build_opportunities():
    rows = []

    events = get_events()
    print(f"Events geladen: {len(events)}")

    target_events = []

    for e in events:
        category = get_event_category(e)
        if category:
            e["_scanner_category"] = category
            target_events.append(e)

    print(f"Ziel-Events gefunden: {len(target_events)}")

    for event in target_events:
        markets = event.get("markets", [])
        team_prices = []

        for market in markets:
            token_id = get_yes_token_id(market)

            if not token_id:
                continue

            asks = get_orderbook(token_id)

            if not asks:
                continue

            team_name = (
                market.get("question")
                or market.get("title")
                or market.get("slug")
            )

            team_prices.append({
                "team": team_name,
                "asks": asks,
                "best_ask": best_ask(asks),
                "market": market,
            })

        if len(team_prices) < TOP_N:
            continue

        team_prices.sort(key=lambda x: x["best_ask"], reverse=True)
        top_teams = team_prices[:TOP_N]

        payout, real_cost, execution_rows = find_equal_payout(top_teams, BANKROLL)

        if payout <= 0 or real_cost <= 0:
            continue

        profit = payout - real_cost
        roi = profit / real_cost
        effective_sum = real_cost / payout

        if roi < MIN_RAW_ROI:
            continue

        for row in execution_rows:
            rows.append({
                "Zeit": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "Kategorie": event.get("_scanner_category"),
                "Event": event.get("title"),
                "Event Slug": event.get("slug"),
                "Team / Markt": row["team"],
                "Best Ask": round(row["best_ask"], 4),
                "Avg Buy Price": round(row["avg_price"], 4),
                "Shares": round(row["shares"], 2),
                "Stake $": round(row["stake"], 2),
                "Real Cost $": round(real_cost, 2),
                "Real Payout $": round(payout, 2),
                "Real Profit $": round(profit, 2),
                "Real ROI %": round(roi * 100, 2),
                f"Effektive Summe Top {TOP_N}": round(effective_sum, 4),
                "Depth Shares": round(row["depth"], 2),
                "Link": "https://polymarket.com/event/" + str(event.get("slug", "")),
            })

    df = pd.DataFrame(rows)

    if not df.empty:
        df = df.sort_values(by=["Real ROI %", "Event"], ascending=[False, True])

    return df


# =====================
# History Summary
# =====================

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
        roi = float(group["Real ROI %"].iloc[0])

        if roi < MIN_HISTORY_ROI:
            continue

        top_sum = float(group[f"Effektive Summe Top {TOP_N}"].iloc[0])
        profit = float(group["Real Profit $"].iloc[0])
        payout = float(group["Real Payout $"].iloc[0])
        cost = float(group["Real Cost $"].iloc[0])
        link = group["Link"].iloc[0]
        timestamp = group["Zeit"].iloc[0]

        teams = " | ".join(
            f"{row['Team / Markt']} @ avg {row['Avg Buy Price']}"
            for _, row in group.iterrows()
        )

        snapshot_rows.append({
            "Snapshot": "Current",
            "Kategorie": category,
            "Event": event,
            "Event Slug": slug,
            "Zeit": timestamp,
            "Real ROI %": roi,
            f"Effektive Summe Top {TOP_N}": top_sum,
            "Real Profit $": profit,
            "Real Payout $": payout,
            "Real Cost $": cost,
            "Teams": teams,
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
        group = group[group["Real ROI %"].astype(float) >= MIN_HISTORY_ROI]

        if group.empty:
            continue

        group = group.sort_values("Real ROI %")

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
                "Real ROI %": row["Real ROI %"],
                f"Effektive Summe Top {TOP_N}": row[f"Effektive Summe Top {TOP_N}"],
                "Real Profit $": row["Real Profit $"],
                "Real Payout $": row["Real Payout $"],
                "Real Cost $": row["Real Cost $"],
                "Teams": row["Teams"],
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


# =====================
# DB
# =====================

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
            real_payout FLOAT,
            real_cost FLOAT,
            real_profit FLOAT,
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
        roi = float(group["Real ROI %"].iloc[0])

        if roi < MIN_HISTORY_ROI:
            continue

        top_sum = float(group[f"Effektive Summe Top {TOP_N}"].iloc[0])
        payout = float(group["Real Payout $"].iloc[0])
        real_cost = float(group["Real Cost $"].iloc[0])
        profit = float(group["Real Profit $"].iloc[0])

        teams = " | ".join(
            f"{row['Team / Markt']} @ avg {row['Avg Buy Price']}"
            for _, row in group.iterrows()
        )

        rows.append((
            slug,
            event,
            category,
            roi,
            top_sum,
            payout,
            real_cost,
            profit,
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
            event_slug, event, category, best_roi, top_sum,
            real_payout, real_cost, real_profit,
            teams, winner, top5_won, first_seen, last_updated
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
            real_payout = CASE
                WHEN EXCLUDED.best_roi > outcomes.best_roi
                THEN EXCLUDED.real_payout
                ELSE outcomes.real_payout
            END,
            real_cost = CASE
                WHEN EXCLUDED.best_roi > outcomes.best_roi
                THEN EXCLUDED.real_cost
                ELSE outcomes.real_cost
            END,
            real_profit = CASE
                WHEN EXCLUDED.best_roi > outcomes.best_roi
                THEN EXCLUDED.real_profit
                ELSE outcomes.real_profit
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
        outcomes = parse_list(market.get("outcomes"))
        prices = parse_list(market.get("outcomePrices"))

        if not outcomes or not prices:
            continue

        for outcome, price in zip(outcomes, prices):
            try:
                price = float(price)
            except Exception:
                continue

            if str(outcome).lower() == "yes" and price >= 0.98:
                return (
                    market.get("question")
                    or market.get("title")
                    or market.get("slug")
                )

    return None


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

        teams = parse_team_names_from_history(teams_text)
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

    ws = get_or_create_worksheet(sheet, "Stats", rows=1000, cols=12)

    conn = psycopg2.connect(DATABASE_URL)

    queries = {
        "Gesamt": """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE top5_won IS NOT NULL) AS resolved,
                COUNT(*) FILTER (WHERE top5_won = true) AS wins,
                COUNT(*) FILTER (WHERE top5_won = false) AS losses,
                ROUND(AVG(best_roi)::numeric, 2) AS avg_real_roi
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
                ROUND(AVG(best_roi)::numeric, 2) AS avg_real_roi
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
                ROUND(AVG(best_roi)::numeric, 2) AS avg_real_roi
            FROM outcomes
            GROUP BY roi_range
            ORDER BY MIN(best_roi);
        """
    }

    all_rows = []

    for title, query in queries.items():
        df = pd.read_sql_query(query, conn)
        df = df.fillna("")

        all_rows.append([title])
        all_rows.append(df.columns.tolist())

        for _, row in df.iterrows():
            all_rows.append(row.astype(str).tolist())

        all_rows.append([])

    conn.close()

    ws.clear()
    ws.update(values=all_rows, range_name="A1")
    ws.freeze(rows=1)
    ws.set_basic_filter()

    print("✅ Stats Sheet aktualisiert.")


# =====================
# Main
# =====================

def main():
    print("Starte Polymarket Scanner mit realem Orderbook-ROI...")

    df = build_opportunities()
    print("Gefundene Zeilen:", len(df))

    init_db()
    save_outcome_to_db(df)
    resolve_outcomes_db()

    sheet = connect_google_sheet()
    print("Google Sheet geöffnet:", sheet.title)

    opportunities_ws = get_or_create_worksheet(sheet, "Opportunities")
    write_dataframe_to_sheet(opportunities_ws, df)
    format_opportunities_sheet(opportunities_ws)
    color_events_and_roi(opportunities_ws, df)

    update_history_summary(sheet, df)
    update_stats_sheet_from_db(sheet)

    status_ws = get_or_create_worksheet(sheet, "Status", rows=20, cols=5)
    status_ws.clear()
    status_ws.update(values=[
        ["Letztes Update", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
        ["Gefundene Zeilen", len(df)],
        ["Bankroll pro Spot", BANKROLL],
        ["Top N", TOP_N],
        ["Min Real ROI", MIN_RAW_ROI],
        ["Min History ROI %", MIN_HISTORY_ROI],
        ["ROI Basis", "Orderbook depth / real execution"],
    ], range_name="A1")

    print("✅ Google Sheet erfolgreich aktualisiert.")


if __name__ == "__main__":
    main()
