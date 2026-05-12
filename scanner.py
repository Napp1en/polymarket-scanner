import os
import json
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

EVENT_LIMIT = 250
MAX_PAGES = 4

EVENTS_URL = "https://gamma-api.polymarket.com/events"

SHEET_ID = os.getenv("SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

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
        "champions league winner",
        "champions league champion",
        "europa league winner",
        "premier league winner",
        "bundesliga winner",
        "la liga winner",
        "serie a winner",
        "world cup winner",
        "euro winner",
    ],
}

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
    orders = ["volume_24hr", "volume", "liquidity"]

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
        if str(outcome).lower() == "yes":
            try:
                price = float(price)
                if price >= 0:
                    return price
            except Exception:
                return None

    return None


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


def write_dataframe_to_sheet(ws, df):
    ws.clear()

    if df.empty:
        ws.update(values=[["Keine passenden Spots gefunden."]], range_name="A1")
        return

    values = [df.columns.tolist()] + df.astype(str).values.tolist()
    ws.update(values=values, range_name="A1")


# =====================
# Google Sheets Format
# =====================

def format_google_sheet(ws):
    ws.freeze(rows=1)
    ws.set_basic_filter()

    ws.format("A1:L1", {
        "backgroundColor": {"red": 0.12, "green": 0.31, "blue": 0.47},
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

    ws.format("E:F", {
        "numberFormat": {"type": "NUMBER", "pattern": "0.0000"}
    })

    ws.format("G:G", {
        "numberFormat": {"type": "NUMBER", "pattern": "0.00"}
    })

    ws.format("H:J", {
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
                    "endColumnIndex": 12,
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
                    "startColumnIndex": 6,
                    "endColumnIndex": 7,
                }],
                "booleanRule": {
                    "condition": {
                        "type": "NUMBER_GREATER",
                        "values": [{"userEnteredValue": "10"}],
                    },
                    "format": {
                        "backgroundColor": {"red": 0.72, "green": 0.88, "blue": 0.70},
                        "textFormat": {"bold": True},
                    },
                },
            },
            "index": 0,
        }
    })

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

    print(f"Turnier-Events gefunden: {len(target_events)}")

    for event in target_events:
        markets = event.get("markets", [])
        team_prices = []

        for market in markets:
            yes_price = get_yes_price(market)

            if yes_price is None:
                continue

            team_name = (
                market.get("question")
                or market.get("title")
                or market.get("slug")
            )

            team_prices.append({
                "team": team_name,
                "price": yes_price,
                "market": market,
            })

        if len(team_prices) < TOP_N:
            continue

        team_prices.sort(key=lambda x: x["price"], reverse=True)
        top_teams = team_prices[:TOP_N]

        yes_sum = sum(t["price"] for t in top_teams)

        if yes_sum <= 0:
            continue

        roi = (1 / yes_sum) - 1

        if roi < MIN_RAW_ROI:
            continue

        payout = BANKROLL / yes_sum
        profit = payout - BANKROLL

        for t in top_teams:
            price = t["price"]
            stake = (price / yes_sum) * BANKROLL

            rows.append({
                "Zeit": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "Kategorie": event.get("_scanner_category"),
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
            })

    df = pd.DataFrame(rows)

    if not df.empty:
        df = df.sort_values(by=["ROI %", "Event"], ascending=[False, True])

    return df


# =====================
# History
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
        yes_price = get_yes_price(market)

        if yes_price is None:
            continue

        if yes_price >= 0.98:
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

    df = build_opportunities()
    print("Gefundene Zeilen:", len(df))

    # Datenbank
    init_db()
    save_outcome_to_db(df)
    resolve_outcomes_db()

    # Google Sheet
    sheet = connect_google_sheet()
    print("Google Sheet geöffnet:", sheet.title)
    update_stats_sheet_from_db(sheet)

    opportunities_ws = get_or_create_worksheet(sheet, "Opportunities")
    write_dataframe_to_sheet(opportunities_ws, df)
    format_google_sheet(opportunities_ws)
    color_events_and_roi(opportunities_ws, df)

    # History / Outcome im Sheet
    update_history_summary(sheet, df)
    update_outcome_history(sheet, df)
    resolve_outcome(sheet)

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
    ], range_name="A1")

    print("✅ Google Sheet erfolgreich aktualisiert.")

if __name__ == "__main__":
    main()
