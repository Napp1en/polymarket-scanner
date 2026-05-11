import os
import json
import requests
import pandas as pd
import gspread
from datetime import datetime
from google.oauth2.service_account import Credentials

# =====================
# Einstellungen
# =====================

BANKROLL = 100
TOP_N = 5
MIN_RAW_ROI = 0.00  # 0.05 = nur ab 5% ROI
EVENT_LIMIT = 250
MAX_PAGES = 4

EVENTS_URL = "https://gamma-api.polymarket.com/events"

SHEET_ID = os.getenv("SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

KEYWORDS = [
    "counter-strike",
    "counter strike",
    "cs2",
    "pgl",
    "pgl astana",
    "pgl bucharest",
    "pgl major",
    "blast premier",
    "iem cologne",
    "iem katowice",
    "esl pro league",
    "fissure playground",
]


# =====================
# Hilfsfunktionen
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


def get_events(limit=EVENT_LIMIT, max_pages=MAX_PAGES):
    events_by_id = {}

    orders = [
        "volume_24hr",
        "volume",
        "liquidity",
    ]

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


def is_cs_event(event):
    text = " ".join([
        str(event.get("title", "")),
        str(event.get("slug", "")),
        str(event.get("description", "")),
        str(event.get("seriesSlug", "")),
        str(event.get("category", "")),
    ]).lower()

    return any(k in text for k in KEYWORDS)


def get_yes_price(market):
    outcomes = parse_list(market.get("outcomes"))
    prices = parse_list(market.get("outcomePrices"))

    if not outcomes or not prices:
        return None

    for outcome, price in zip(outcomes, prices):
        if str(outcome).lower() == "yes":
            try:
                price = float(price)
                if price > 0:
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


def get_or_create_worksheet(sheet, title, rows=1000, cols=20):
    try:
        return sheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return sheet.add_worksheet(title=title, rows=rows, cols=cols)


def write_dataframe_to_sheet(ws, df):
    format_google_sheet(opportunities_ws)
    ws.clear()

    if df.empty:
        ws.update("A1", [["Keine passenden Spots gefunden."]])
        return

    values = [df.columns.tolist()] + df.astype(str).values.tolist()
    ws.update("A1", values)

    ws.freeze(rows=1)
    ws.set_basic_filter()

def format_google_sheet(ws):
    ws.freeze(rows=1)
    ws.set_basic_filter()

    # Header dunkelblau + weiß + fett
    ws.format("A1:K1", {
        "backgroundColor": {"red": 0.12, "green": 0.31, "blue": 0.47},
        "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "bold": True},
        "horizontalAlignment": "CENTER",
    })

    # Ganze Tabelle etwas ordentlicher
    ws.format("A:K", {
        "verticalAlignment": "MIDDLE",
        "wrapStrategy": "WRAP",
    })

    # Zahlenformate
    ws.format("D:E", {"numberFormat": {"type": "NUMBER", "pattern": "0.0000"}})
    ws.format("F:F", {"numberFormat": {"type": "NUMBER", "pattern": "0.00"}})
    ws.format("G:I", {"numberFormat": {"type": "CURRENCY", "pattern": "$0.00"}})

    # Spaltenbreiten
    requests = [
        {"updateDimensionProperties": {
            "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 150},
            "fields": "pixelSize"
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2},
            "properties": {"pixelSize": 260},
            "fields": "pixelSize"
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 2, "endIndex": 3},
            "properties": {"pixelSize": 420},
            "fields": "pixelSize"
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 10, "endIndex": 11},
            "properties": {"pixelSize": 420},
            "fields": "pixelSize"
        }},
    ]

    ws.spreadsheet.batch_update({"requests": requests})


# =====================
# Scanner
# =====================

def build_opportunities():
    rows = []

    events = get_events()
    print(f"Events geladen: {len(events)}")

    cs_events = [e for e in events if is_cs_event(e)]
    print(f"CS Events gefunden: {len(cs_events)}")

    print("\nGefundene CS Events:")
    for e in cs_events:
        print("-", e.get("title"), "|", e.get("slug"))

    for event in cs_events:
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
                "Event": event.get("title"),
                "Team / Markt": t["team"],
                "YES Preis": round(price, 4),
                f"Summe Top {TOP_N}": round(yes_sum, 4),
                "ROI %": round(roi * 100, 2),
                "Stake $": round(stake, 2),
                "Payout $": round(payout, 2),
                "Profit $": round(profit, 2),
                "Volumen Event": event.get("volume"),
                "Link": "https://polymarket.com/event/" + str(event.get("slug", "")),
            })

    df = pd.DataFrame(rows)

    if not df.empty:
        df = df.sort_values(by=["ROI %", "Event"], ascending=[False, True])

    return df


def main():
    print("Starte Polymarket CS Scanner...")

    df = build_opportunities()

    sheet = connect_google_sheet()

    opportunities_ws = get_or_create_worksheet(sheet, "Opportunities")
    write_dataframe_to_sheet(opportunities_ws, df)

    status_ws = get_or_create_worksheet(sheet, "Status", rows=20, cols=5)
    status_ws.clear()
    status_ws.update("A1", [
        ["Letztes Update", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
        ["Gefundene Zeilen", len(df)],
        ["Bankroll pro Spot", BANKROLL],
        ["Top N", TOP_N],
        ["Min ROI", MIN_RAW_ROI],
    ])

    print("✅ Google Sheet erfolgreich aktualisiert.")


if __name__ == "__main__":
    main()
