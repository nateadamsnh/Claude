#!/usr/bin/env python3
"""
One-shot: after the $3,000 IAU market buy fills at open, replace the
100-share trailing stop with one covering all whole shares (5% trail, GTC).
Polls for up to 20 minutes after launch, then exits.
Safe to re-run: exits cleanly if the stop already covers the full position.
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
LOG_FILE    = BASE_DIR / "logs" / "update_iau_stop.log"
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

SYMBOL        = "IAU"
TRAIL_PERCENT = 5
POLL_SECONDS  = 30
MAX_MINUTES   = 20


def get_position_qty() -> float:
    r = requests.get(f"{BASE_URL}/positions/{SYMBOL}", headers=HEADERS, timeout=10)
    if r.status_code != 200:
        return 0.0
    return float(r.json()["qty"])


def get_open_orders():
    r = requests.get(f"{BASE_URL}/orders", headers=HEADERS,
                     params={"status": "open", "symbols": SYMBOL}, timeout=10)
    return r.json() if r.status_code == 200 else []


def run():
    log.info(f"IAU stop updater started — waiting for buy fill (max {MAX_MINUTES} min)")
    deadline = time.time() + MAX_MINUTES * 60

    while time.time() < deadline:
        orders = get_open_orders()
        pending_buys = [o for o in orders if o["side"] == "buy"]
        if pending_buys:
            log.info(f"  Buy still pending ({pending_buys[0]['status']}) — waiting...")
            time.sleep(POLL_SECONDS)
            continue

        qty = get_position_qty()
        whole = floor(qty)
        if whole < 1:
            log.warning("  No IAU position found — nothing to protect. Exiting.")
            return

        trailing = [o for o in orders
                    if o.get("type") == "trailing_stop" and o["side"] == "sell"]
        covered = sum(int(float(o.get("qty") or 0)) for o in trailing)
        if covered >= whole:
            log.info(f"  Trailing stop already covers {covered}/{whole} shares. Done.")
            return

        for o in trailing:
            r = requests.delete(f"{BASE_URL}/orders/{o['id']}", headers=HEADERS, timeout=10)
            log.info(f"  Cancelled old trailing stop {o['id'][:8]} ({r.status_code})")
        time.sleep(2)  # let the cancel settle so shares free up

        payload = {
            "symbol":        SYMBOL,
            "qty":           str(whole),
            "side":          "sell",
            "type":          "trailing_stop",
            "trail_percent": str(TRAIL_PERCENT),
            "time_in_force": "gtc",
        }
        r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json=payload, timeout=10)
        if r.status_code == 200:
            o = r.json()
            log.info(f"  [STOP] {SYMBOL} trailing stop placed: {whole} shares, "
                     f"{TRAIL_PERCENT}% trail | {o.get('id','')[:8]} | {o.get('status')}")
        else:
            log.error(f"  Stop placement failed {r.status_code}: {r.text[:200]}")
        return

    log.warning("  Timed out waiting for buy fill — stop NOT updated. Re-run manually.")


if __name__ == "__main__":
    run()
