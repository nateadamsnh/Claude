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

Protective stop-loss added 2026-09-27 (mirrors the same fix in
politician-copy-trader/trader.py -- see that module's docstring for the
full rationale): nothing previously managed downside risk between a copied
buy and whatever eventual disclosure told us to sell. attach_protective_stops()
below places a 10% Alpaca-managed GTC trailing stop on any held whole shares
not already covered by one, checked fresh against the broker's open orders
every run (not local state, so it self-heals across runs). Only whole
shares -- Alpaca's trailing_stop order type rejects fractional qty, and a
$500 notional buy is almost always fractional, so a small remainder stays
uncovered. execute_trade()'s sell path cancels any resting stop first, so
a copied sell isn't blocked by shares the stop already reserves.

These functions duplicate politician-copy-trader/trader.py's equivalents
rather than importing them, matching this file's existing style (it
already duplicates BASE_URL/HEADERS/SKIP_KEYWORDS/TRADE_AMOUNT instead of
importing trader.py, despite the sys.path insert below making that
possible) -- keeps the two scripts independent of each other at runtime.
"""

import json
import math
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

TRAIL_PERCENT = 10.0  # kept identical to politician-copy-trader/trader.py's
                       # TRAIL_PERCENT so both scripts protect a copied
                       # position the same way regardless of which one bought it


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


def get_open_orders(ticker: str) -> list:
    r = requests.get(f"{BASE_URL}/orders", headers=HEADERS,
                      params={"status": "open", "symbols": ticker, "limit": 500}, timeout=10)
    r.raise_for_status()
    return r.json()


def cancel_order(order_id: str):
    r = requests.delete(f"{BASE_URL}/orders/{order_id}", headers=HEADERS, timeout=10)
    if r.status_code not in (200, 204, 404):  # 404 = already gone, nothing to cancel
        r.raise_for_status()


def cancel_stops(ticker: str) -> int:
    """Cancel any open trailing-stop sell orders on ticker. Returns how many."""
    canceled = 0
    for o in get_open_orders(ticker):
        if o.get("side") == "sell" and o.get("type") == "trailing_stop":
            cancel_order(o["id"])
            canceled += 1
    return canceled


def sellable_qty(ticker: str) -> int:
    """Whole shares held minus shares already reserved by open sell orders --
    the most a new stop can cover without double-reserving or risking a short."""
    pos = requests.get(f"{BASE_URL}/positions/{ticker}", headers=HEADERS, timeout=10)
    if pos.status_code == 404:
        return 0
    pos.raise_for_status()
    held = float(pos.json().get("qty", 0))
    if held < 1:
        return 0
    reserved = sum(
        float(o["qty"]) - float(o.get("filled_qty") or 0)
        for o in get_open_orders(ticker) if o.get("side") == "sell"
    )
    return max(0, math.floor(held) - math.ceil(reserved))


def submit_trailing_stop(ticker: str, qty: int) -> dict:
    payload = {
        "symbol":        ticker,
        "qty":           str(qty),
        "side":          "sell",
        "type":          "trailing_stop",
        "trail_percent": str(TRAIL_PERCENT),
        "time_in_force": "gtc",
    }
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json=payload, timeout=15)
    r.raise_for_status()
    order = r.json()
    log.info(f"    [STOP] {qty} shares {ticker} trailing {TRAIL_PERCENT}% | ID: {order.get('id')}")
    return order


def attach_protective_stops():
    """Ensure every currently-held symbol has a trailing stop covering its
    uncovered whole shares. Coverage is measured from open orders at the
    broker, not local state, so this is self-healing across runs -- and
    across both this script and the politician copy trader, since either
    one can be the one that bought a given symbol."""
    try:
        r = requests.get(f"{BASE_URL}/positions", headers=HEADERS, timeout=10)
        r.raise_for_status()
        positions = r.json()
    except Exception as e:
        log.error(f"  Could not fetch positions for stop-loss pass: {e}")
        return
    for pos in positions:
        ticker = pos["symbol"]
        try:
            qty = sellable_qty(ticker)
            if qty >= 1:
                submit_trailing_stop(ticker, qty)
        except Exception as e:
            log.error(f"  Stop-loss failed for {ticker}: {e}")


def execute_trade(ticker: str, side: str) -> dict:
    """Place a $500 notional buy, or close the held position on a sell (never opens a short)."""
    if side.lower() == "sell":
        try:
            canceled = cancel_stops(ticker)
            if canceled:
                log.info(f"    [SELL] Canceled {canceled} resting stop order(s) on {ticker} before selling.")
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

    # Reconcile pass, independent of whether this run found any new trades --
    # picks up a fill that wasn't covered yet, or shares bought by the other
    # copy-trading script.
    if market_open:
        attach_protective_stops()
    else:
        log.info("  Market CLOSED — skipping stop-loss reconcile pass.")

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
