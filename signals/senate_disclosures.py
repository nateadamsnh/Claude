#!/usr/bin/env python3
"""
Senate Financial Disclosure Monitor
=====================================
Extends the politician copy trader to cover US Senators using
Capitol Trades (same scraper, different chamber filter).

Tracked senators (finance, tech, defense, intelligence committees):
  - Mark Warner    (D-VA) — Vice Chair, Senate Intelligence
  - Tommy Tuberville (R-AL) — Armed Services Committee
  - Mike Rounds    (R-SD) — Defense & intelligence focus
  - Dan Sullivan   (R-AK) — Armed Services, defense stocks
  - John Hoeven    (R-ND) — Energy & defense

Runs every 2 hours during market hours.
Shares the copy trader's execution logic — places $500 trades
for any new senator disclosures matching portfolio interest.
"""

import json
import re
import sys
import requests
import logging
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR / "politician-copy-trader"))
sys.path.insert(0, str(Path(__file__).parent))

from signal_utils import (
    get_logger, load_state, save_state, send_signal_email,
    html_table, base_html
)

log = get_logger("senate_disclosures", "senate_disclosures.log")

# Load credentials
with open(BASE_DIR / "config" / "alpaca_credentials.json") as f:
    creds = json.load(f)
BASE_URL = creds["endpoint"]
HEADERS  = {
    "APCA-API-KEY-ID":     creds["api_key"],
    "APCA-API-SECRET-KEY": creds["api_secret"],
}

TRACKED_SENATORS = [
    {"name": "Mark Warner",      "id": "W000805", "party": "Democrat",    "state": "Virginia",
     "notes": "Vice Chair, Senate Intelligence — tech & cybersecurity focus"},
    {"name": "Tommy Tuberville", "id": "T000278", "party": "Republican",  "state": "Alabama",
     "notes": "Armed Services Committee — aggressive stock trader"},
    {"name": "Mike Rounds",      "id": "R000605", "party": "Republican",  "state": "South Dakota",
     "notes": "Armed Services, Intelligence — defense & energy"},
    {"name": "Dan Sullivan",     "id": "S001198", "party": "Republican",  "state": "Alaska",
     "notes": "Armed Services, Commerce — defense & energy stocks"},
    {"name": "John Hoeven",      "id": "H001061", "party": "Republican",  "state": "North Dakota",
     "notes": "Energy & Natural Resources, Appropriations"},
]

TRADE_AMOUNT = 500  # $500 per copied trade
SKIP_KEYWORDS = [
    "treasury", "t-bill", "t bill", "bond", "note", "bill",
    "mutual fund", "money market", "etf", "trust", "index",
    "xsp", "mini spx", "cboe"
]

TICKER_RE = re.compile(r'([A-Z]{1,6}(?:[./][A-Z]{1,2})?):US')


def scrape_senator_trades(senator_id: str) -> list:
    """Scrape Capitol Trades for a senator's recent trades."""
    url = f"https://www.capitoltrades.com/trades?politician={senator_id}&sortBy=-txDate&pageSize=20"
    try:
        r = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html"
        })
        if r.status_code != 200:
            log.error(f"Capitol Trades returned HTTP {r.status_code} for senator {senator_id}")
            return []

        trades = []
        # Try __NEXT_DATA__ JSON first
        next_data = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.DOTALL)
        if next_data:
            try:
                data = json.loads(next_data.group(1))
                props = data.get("props", {}).get("pageProps", {})
                raw_trades = props.get("trades", props.get("data", {}).get("trades", []))
                if isinstance(raw_trades, list):
                    for t in raw_trades:
                        ticker_match = TICKER_RE.search(str(t))
                        if not ticker_match:
                            continue
                        ticker = ticker_match.group(1).replace("/", ".").upper()
                        tx_type = str(t.get("type", t.get("txType", ""))).upper()
                        if "BUY" in tx_type or "PURCHASE" in tx_type:
                            tx_type = "BUY"
                        elif "SELL" in tx_type or "SALE" in tx_type:
                            tx_type = "SELL"
                        else:
                            continue
                        trades.append({
                            "ticker":  ticker,
                            "tx_type": tx_type,
                            "tx_date": str(t.get("txDate", t.get("date", ""))),
                            "size":    str(t.get("size", t.get("amount", ""))),
                            "issuer":  str(t.get("issuer", {}).get("name", ticker) if isinstance(t.get("issuer"), dict) else t.get("issuer", ticker)),
                        })
                return trades
            except Exception:
                pass

        # HTML table fallback: read the row's visible text, not its markup
        for row in re.findall(r'<tr[^>]*>(.*?)</tr>', r.text, re.DOTALL):
            ticker_match = TICKER_RE.search(row)
            if not ticker_match:
                continue
            ticker = ticker_match.group(1).replace("/", ".").upper()
            tokens = [t.strip() for t in re.sub(r'<[^>]+>', '\n', row).split('\n') if t.strip()]
            text = " ".join(tokens)
            type_m = re.search(r'\b(buy|sell)\b', text, re.IGNORECASE)
            if not type_m:
                continue
            tx_type = type_m.group(1).upper()
            # Row shows "<published d Mon yyyy> <traded d Mon yyyy>"; the second is the trade date
            dates = re.findall(r'\b(\d{1,2}) ([A-Z][a-z]{2}) (\d{4})\b', text)
            tx_date = "-".join(dates[1]) if len(dates) > 1 else ""
            size_m = re.search(r'\b\d+[KM]?[–-]\d+[KM]\b', text)
            issuer = ticker
            for i, tok in enumerate(tokens):
                if TICKER_RE.fullmatch(tok) and i > 0:
                    issuer = tokens[i - 1]
                    break
            trades.append({
                "ticker":  ticker,
                "tx_type": tx_type,
                "tx_date": tx_date,
                "size":    size_m.group(0) if size_m else "",
                "issuer":  issuer,
            })
        return trades

    except Exception as e:
        log.error(f"Error scraping senator {senator_id}: {e}")
        return []


def should_skip(ticker: str, issuer: str) -> bool:
    text = f"{ticker} {issuer}".lower()
    return any(kw in text for kw in SKIP_KEYWORDS)


def execute_trade(ticker: str, side: str) -> dict:
    """Place a $500 notional buy, or close the held position on a sell (never opens a short)."""
    if side.lower() == "sell":
        try:
            pos = requests.get(f"{BASE_URL}/positions/{ticker}", headers=HEADERS, timeout=10)
            if pos.status_code == 404:
                return {"status": "skipped (no position)"}
            pos.raise_for_status()
            qty = pos.json()["qty"]
            r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, timeout=10, json={
                "symbol": ticker, "qty": qty, "side": "sell",
                "type": "market", "time_in_force": "day"})
            return r.json()
        except Exception as e:
            return {"error": str(e)}
    payload = {
        "symbol":        ticker,
        "notional":      str(TRADE_AMOUNT),
        "side":          side.lower(),
        "type":          "market",
        "time_in_force": "day",
    }
    try:
        r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json=payload, timeout=10)
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def run(baseline: bool = False):
    log.info("=" * 65)
    log.info(f"SENATE DISCLOSURES  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    log.info("=" * 65)

    # Check market
    try:
        clock_r = requests.get(f"{BASE_URL}/clock", headers=HEADERS, timeout=10)
        clock_r.raise_for_status()
        market_open = clock_r.json().get("is_open", False)
    except Exception as e:
        log.error(f"Failed to check market clock: {e} — defaulting to closed")
        market_open = False

    state    = load_state("senate_disclosures.json")
    seen_keys = set(state.get("seen_keys", []))

    new_trades = []

    for senator in TRACKED_SENATORS:
        name = senator["name"]
        trades = scrape_senator_trades(senator["id"])
        log.info(f"  {name}: {len(trades)} trades fetched")

        for t in trades:
            ticker  = t["ticker"]
            tx_type = t["tx_type"]
            key     = f"{senator['id']}:{ticker}|{t.get('tx_date','')}|{tx_type}"

            if key in seen_keys:
                continue
            if should_skip(ticker, t.get("issuer", "")):
                log.info(f"    [SKIP] {ticker} — keyword filter")
                seen_keys.add(key)
                continue

            if baseline:
                seen_keys.add(key)
                continue
            if not market_open:
                # Leave it unseen so the next in-hours run trades it, instead of dropping it
                log.info(f"    [DEFER] {ticker} {tx_type} — {name} | market closed, retry next run")
                continue

            seen_keys.add(key)
            side = "buy" if tx_type == "BUY" else "sell"

            if market_open:
                order = execute_trade(ticker, side)
                status = order.get("status", "error")
                log.info(f"    [{tx_type}] {ticker} — {name} | order {status}")
            else:
                status = "queued (market closed)"
                log.info(f"    [QUEUED] {ticker} {tx_type} — {name} | market closed")

            new_trades.append({
                "senator": name,
                "ticker":  ticker,
                "side":    tx_type,
                "size":    t.get("size", ""),
                "date":    t.get("tx_date", ""),
                "status":  status,
            })

    state["seen_keys"] = list(seen_keys)[-500:]
    state["last_run"]  = datetime.now().isoformat()
    save_state("senate_disclosures.json", state)

    if baseline:
        log.info(f"  Baseline: marked {len(seen_keys)} current disclosures as seen, no orders placed.")
        log.info("Run complete.\n")
        return
    if not new_trades:
        log.info("  No new senator trades found.")
        log.info("Run complete.\n")
        return

    rows = [[
        t["senator"],
        f"<strong>{t['ticker']}</strong>",
        t["side"],
        t["size"],
        t["date"],
        t["status"],
    ] for t in new_trades]

    table   = html_table(["Senator", "Ticker", "Action", "Size", "Date", "Order Status"], rows)
    content = (
        f"<p><strong>{len(new_trades)}</strong> new senate disclosure(s) detected and traded:</p>"
        f"{table}"
    )
    body_html = base_html("Senate Disclosure Alert", "🏛️", content)
    body_text = "\n".join(
        f"{t['senator']}: {t['side']} {t['ticker']} ({t['size']})" for t in new_trades
    )
    send_signal_email(
        f"🏛️ Senate Trades — {len(new_trades)} new disclosure(s) copied",
        body_text, body_html
    )
    log.info("Run complete.\n")


if __name__ == "__main__":
    run(baseline="--baseline" in sys.argv)
