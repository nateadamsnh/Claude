#!/usr/bin/env python3
"""
liquidate_all.py — Close every open position in the Alpaca paper account.

Run this script manually. It will:
  1. Fetch and display all open positions with current P&L
  2. Ask for explicit confirmation before doing anything
  3. Cancel all open orders first (required before closing positions)
  4. Submit market close for every position via DELETE /v2/positions
  5. Confirm what was submitted

This script does NOT run automatically. You must type YES to proceed.
"""

import json
import requests
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
with open(BASE_DIR / "config" / "alpaca_credentials.json") as f:
    creds = json.load(f)

BASE_URL = creds["endpoint"]   # https://paper-api.alpaca.markets/v2
HEADERS  = {
    "APCA-API-KEY-ID":     creds["api_key"],
    "APCA-API-SECRET-KEY": creds["api_secret"],
}


def get(path):
    r = requests.get(f"{BASE_URL}{path}", headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()


def delete(path, params=None):
    r = requests.delete(f"{BASE_URL}{path}", headers=HEADERS, params=params, timeout=15)
    return r


def main():
    print("=" * 60)
    print("  ALPACA PAPER ACCOUNT — FULL LIQUIDATION SCRIPT")
    print("=" * 60)

    # ── 1. Fetch account summary ──────────────────────────────────
    acct = get("/account")
    print(f"\n  Account:       {acct['account_number']}")
    print(f"  Portfolio val: ${float(acct['portfolio_value']):>12,.2f}")
    print(f"  Cash:          ${float(acct['cash']):>12,.2f}")
    print(f"  Buying power:  ${float(acct['buying_power']):>12,.2f}")

    # ── 2. Fetch all open positions ───────────────────────────────
    positions = get("/positions")
    if not positions:
        print("\n  No open positions found. Nothing to do.")
        sys.exit(0)

    print(f"\n  Open positions ({len(positions)}):\n")
    print(f"  {'Symbol':<10} {'Qty':>8} {'Entry':>10} {'Current':>10} {'P&L $':>12} {'P&L %':>8}")
    print(f"  {'-'*10} {'-'*8} {'-'*10} {'-'*10} {'-'*12} {'-'*8}")

    total_market_val = 0.0
    total_unrealized = 0.0
    for p in sorted(positions, key=lambda x: x["symbol"]):
        sym      = p["symbol"]
        qty      = float(p["qty"])
        entry    = float(p["avg_entry_price"])
        current  = float(p["current_price"])
        pl_usd   = float(p["unrealized_pl"])
        pl_pct   = float(p["unrealized_plpc"]) * 100
        mkt_val  = float(p["market_value"])
        total_market_val += mkt_val
        total_unrealized += pl_usd
        print(f"  {sym:<10} {qty:>8.2f} ${entry:>9.2f} ${current:>9.2f} ${pl_usd:>11,.2f} {pl_pct:>7.2f}%")

    print(f"\n  Total market value: ${total_market_val:>12,.2f}")
    print(f"  Total unrealized:   ${total_unrealized:>12,.2f}")

    # ── 3. Fetch open orders ──────────────────────────────────────
    orders = get("/orders?status=open")
    print(f"\n  Open orders to cancel first: {len(orders)}")

    # ── 4. Confirmation ───────────────────────────────────────────
    print("\n" + "!" * 60)
    print("  WARNING: This will close ALL positions and cancel ALL orders.")
    print("  This action cannot be undone.")
    print("!" * 60)
    confirm = input("\n  Type YES to proceed, anything else to abort: ").strip()

    if confirm != "YES":
        print("\n  Aborted. No changes made.")
        sys.exit(0)

    # ── 5. Cancel all open orders first ──────────────────────────
    if orders:
        print(f"\n  Cancelling {len(orders)} open order(s)...")
        r = delete("/orders")
        if r.status_code in (200, 207):
            print(f"  Orders cancelled (status {r.status_code})")
        else:
            print(f"  Warning: order cancel returned {r.status_code} — {r.text[:200]}")

    # ── 6. Close all positions ────────────────────────────────────
    print(f"\n  Submitting close for all {len(positions)} position(s)...")
    r = delete("/positions", params={"cancel_orders": "true"})

    if r.status_code == 207:
        results = r.json()
        ok  = [x for x in results if x.get("status") == 200]
        err = [x for x in results if x.get("status") != 200]
        print(f"\n  Submitted:  {len(ok)} position(s) closed successfully")
        if err:
            print(f"  Errors:     {len(err)} position(s) failed:")
            for e in err:
                print(f"    {e.get('symbol','?')} — {e.get('body', {}).get('message','unknown error')}")
    elif r.status_code == 200:
        print(f"\n  All positions submitted for closure (status 200).")
    else:
        print(f"\n  Unexpected response {r.status_code}: {r.text[:400]}")
        sys.exit(1)

    print("\n  Done. Orders are submitted as market orders and will fill")
    print("  at the next available price. Check your Alpaca dashboard")
    print("  to confirm all fills.")
    print("=" * 60)


if __name__ == "__main__":
    main()
