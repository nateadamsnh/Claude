#!/usr/bin/env python3
"""
place_stops.py
==============
One-shot script: cancel stale GTC stop-loss orders and place updated ones.

Set DRY_RUN = False to actually submit orders.
"""

import json
import sys
from pathlib import Path

import requests

# ── Config ────────────────────────────────────────────────────────────────────
DRY_RUN = True   # Flip to False to execute for real

BASE_DIR    = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config" / "alpaca_credentials.json"

with open(CONFIG_FILE) as f:
    creds = json.load(f)

BASE_URL = creds["endpoint"]
HEADERS  = {
    "APCA-API-KEY-ID":     creds["api_key"],
    "APCA-API-SECRET-KEY": creds["api_secret"],
}

# ── Stop orders to place ───────────────────────────────────────────────────────
# Each entry: (symbol, qty, stop_price)
# Qty matches exactly the shares you want protected by this stop order.
STOPS = [
    # New stops — currently unprotected (5% below current price as of 2026-06-18)
    ("ACN",  3.0,    123.12),   # current $129.60
    ("BAH",  0.44,    64.60),   # current $67.995
    ("CACI", 0.99,   446.07),   # current $469.55
    ("NOC",  0.91,   495.81),   # current $521.90
    ("IBM",  1.854,  233.60),   # current $245.89
    ("CVX",  0.5908, 164.91),   # current $173.59
    ("XOM",  0.5866, 130.04),   # current $136.88
    ("XLE",  0.6909,  50.89),   # current $53.57
    ("IAU",  0.1301,  75.59),   # current $79.57
    ("ORCL", 0.45,   174.29),   # current $183.46
    # Tighten existing stops — current stops too loose (~9-10% below vs 5% target)
    # SOXX omitted — existing stop $607.32 already ≈5% below current $638.89
    ("VNQ",  20.0,    93.23),   # existing $87.55 (9% loose); current $96.24
    ("BND",  27.0,    69.66),   # existing $65.96 (10% loose); current $73.40
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def fetch_open_orders() -> list[dict]:
    r = requests.get(
        f"{BASE_URL}/orders",
        headers=HEADERS,
        params={"status": "open", "limit": 500},
        timeout=10,
    )
    if r.status_code != 200:
        print(f"ERROR fetching open orders: {r.status_code} {r.text}")
        sys.exit(1)
    return r.json()


def cancel_order(order: dict) -> bool:
    oid   = order["id"]
    sym   = order["symbol"]
    stop  = order.get("stop_price", "?")
    qty   = order.get("qty", "?")
    if DRY_RUN:
        print(f"  [DRY RUN] CANCEL  {sym}  stop=${stop}  qty={qty}  id={oid}")
        return True
    r = requests.delete(f"{BASE_URL}/orders/{oid}", headers=HEADERS, timeout=10)
    if r.status_code in (200, 204):
        print(f"  CANCELLED  {sym}  stop=${stop}  qty={qty}  id={oid}")
        return True
    print(f"  ERROR cancelling {sym} order {oid}: {r.status_code} {r.text}")
    return False


def place_stop(symbol: str, qty: float, stop_price: float) -> bool:
    payload = {
        "symbol":      symbol,
        "qty":         str(qty),
        "side":        "sell",
        "type":        "stop",
        "time_in_force": "gtc",
        "stop_price":  str(stop_price),
    }
    if DRY_RUN:
        print(f"  [DRY RUN] PLACE   {symbol}  stop=${stop_price}  qty={qty}  GTC")
        return True
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json=payload, timeout=10)
    if r.status_code in (200, 201):
        oid = r.json().get("id", "?")
        print(f"  PLACED     {symbol}  stop=${stop_price}  qty={qty}  GTC  id={oid}")
        return True
    print(f"  ERROR placing stop for {symbol}: {r.status_code} {r.text}")
    return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if DRY_RUN:
        print("=" * 60)
        print("  DRY RUN — no orders will be submitted")
        print("  Set DRY_RUN = False at the top of this script to execute")
        print("=" * 60)

    target_symbols = {sym for sym, _, _ in STOPS}

    print("\nFetching open orders...")
    open_orders = fetch_open_orders()

    # Find existing stop/stop_limit sell orders for our target symbols
    existing_stops: dict[str, list[dict]] = {sym: [] for sym in target_symbols}
    for o in open_orders:
        sym  = o.get("symbol", "")
        side = o.get("side", "")
        typ  = o.get("type", "")
        if sym in target_symbols and side == "sell" and typ in ("stop", "stop_limit"):
            existing_stops[sym].append(o)

    cancelled = 0
    placed    = 0
    errors    = 0

    for symbol, qty, stop_price in STOPS:
        print(f"\n-- {symbol} " + "-" * 46)

        # Cancel any existing stops for this symbol
        for o in existing_stops[symbol]:
            ok = cancel_order(o)
            if ok:
                cancelled += 1
            else:
                errors += 1

        # Place the new stop
        ok = place_stop(symbol, qty, stop_price)
        if ok:
            placed += 1
        else:
            errors += 1

    print()
    print("=" * 60)
    print(f"  Stops placed:    {placed}")
    print(f"  Stops cancelled: {cancelled}")
    print(f"  Errors:          {errors}")
    if DRY_RUN:
        print()
        print("  Flip DRY_RUN = False to submit for real.")
    print("=" * 60)


if __name__ == "__main__":
    main()
