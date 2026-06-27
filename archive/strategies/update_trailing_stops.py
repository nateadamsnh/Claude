#!/usr/bin/env python3
"""
One-shot: after the queued market buys fill at open, replace each symbol's
stop (whatever StrategyManager may have re-placed) with a 5% trailing stop
covering all whole shares (GTC). Mirrors update_iau_stop.py but multi-symbol.

Polls for up to 20 minutes, then exits. Safe to re-run: skips symbols whose
trailing stop already covers the full position.
"""

import json
import sys
import time
import logging
import requests
from math import floor
from pathlib import Path

BASE_DIR    = Path(__file__).parent.parent
CONFIG_FILE = BASE_DIR / "config" / "alpaca_credentials.json"
LOG_FILE    = BASE_DIR / "logs" / "update_trailing_stops.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

with open(CONFIG_FILE) as f:
    creds = json.load(f)
BASE_URL = creds["endpoint"]
HEADERS  = {
    "APCA-API-KEY-ID":     creds["api_key"],
    "APCA-API-SECRET-KEY": creds["api_secret"],
}

# Per-symbol trail percent — QBTX is a 2x leveraged ETF, so it gets a wider
# trail to avoid stopping out on routine doubled volatility.
TRAIL_PERCENTS = {
    "ORCL": 5,
    "XOM":  5,
    "CVX":  5,
    "XLE":  5,
    "QBTX": 15,
}
SYMBOLS = list(TRAIL_PERCENTS)
POLL_SECONDS  = 30
MAX_MINUTES   = 20
MAX_ATTEMPTS  = 3   # per-symbol retries if a race with StrategyManager occurs


def get_position_qty(symbol: str) -> float:
    r = requests.get(f"{BASE_URL}/positions/{symbol}", headers=HEADERS, timeout=10)
    if r.status_code != 200:
        return 0.0
    return float(r.json()["qty"])


def get_open_orders(symbol: str):
    r = requests.get(f"{BASE_URL}/orders", headers=HEADERS,
                     params={"status": "open", "symbols": symbol}, timeout=10)
    return r.json() if r.status_code == 200 else []


def ensure_trailing_stop(symbol: str) -> bool:
    """Cancel any sell stop and place a 5% trailing stop for all whole shares.
    Returns True when the symbol is fully protected."""
    qty = get_position_qty(symbol)
    whole = floor(qty)
    if whole < 1:
        log.warning(f"  {symbol:6s} | no whole shares held — skipping")
        return True

    orders = get_open_orders(symbol)
    if any(o["side"] == "buy" for o in orders):
        log.info(f"  {symbol:6s} | buy still pending — waiting")
        return False

    sells = [o for o in orders if o["side"] == "sell"]
    trailing = [o for o in sells if o.get("type") == "trailing_stop"]
    if trailing and int(float(trailing[0].get("qty") or 0)) >= whole:
        log.info(f"  {symbol:6s} | trailing stop already covers {whole} shares — done")
        return True

    for attempt in range(1, MAX_ATTEMPTS + 1):
        # Clear existing sell stops (incl. plain stops StrategyManager re-placed)
        for o in get_open_orders(symbol):
            if o["side"] == "sell":
                r = requests.delete(f"{BASE_URL}/orders/{o['id']}", headers=HEADERS, timeout=10)
                log.info(f"  {symbol:6s} | cancelled {o['type']} {o['id'][:8]} ({r.status_code})")
        time.sleep(2)

        payload = {
            "symbol":        symbol,
            "qty":           str(whole),
            "side":          "sell",
            "type":          "trailing_stop",
            "trail_percent": str(TRAIL_PERCENTS[symbol]),
            "time_in_force": "gtc",
        }
        r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json=payload, timeout=10)
        if r.status_code == 200:
            o = r.json()
            log.info(f"  {symbol:6s} | [TRAIL] {whole} shares, {TRAIL_PERCENTS[symbol]}% trail "
                     f"| {o.get('id','')[:8]} | {o.get('status')}")
            return True
        log.warning(f"  {symbol:6s} | attempt {attempt} failed {r.status_code}: {r.text[:150]}")
        time.sleep(3)

    log.error(f"  {symbol:6s} | could not place trailing stop after {MAX_ATTEMPTS} attempts")
    return True  # don't block the loop on a persistent error


def run():
    log.info(f"Trailing stop updater started for {', '.join(SYMBOLS)} (max {MAX_MINUTES} min)")
    deadline = time.time() + MAX_MINUTES * 60
    remaining = set(SYMBOLS)

    while remaining and time.time() < deadline:
        for symbol in sorted(remaining):
            try:
                if ensure_trailing_stop(symbol):
                    remaining.discard(symbol)
            except Exception as e:
                log.error(f"  {symbol:6s} | ERROR: {e}")
        if remaining:
            time.sleep(POLL_SECONDS)

    if remaining:
        log.warning(f"Timed out — not updated: {', '.join(sorted(remaining))}. Re-run manually.")
    else:
        log.info("All trailing stops in place. Done.")


if __name__ == "__main__":
    run()
