#!/usr/bin/env python3
"""
Compare Strategies — one-shot report across every registered strategy
=======================================================================
Reads config/strategy_framework.json for the list of registered strategies,
fetches the live Alpaca account once, and for each strategy prints its
allocated equity share alongside its realized P&L/trade stats from
strategy_ledger.py.

Console-only for now — not wired to a schedule. Run manually:
    python compare_strategies.py
"""

import json
import sys
from pathlib import Path

import requests

import capital_allocator
import strategy_ledger

BASE_DIR    = Path(__file__).parent.parent
CONFIG_FILE = BASE_DIR / "config" / "alpaca_credentials.json"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def get_account() -> dict:
    with open(CONFIG_FILE) as f:
        creds = json.load(f)
    headers = {
        "APCA-API-KEY-ID":     creds["api_key"],
        "APCA-API-SECRET-KEY": creds["api_secret"],
    }
    r = requests.get(f"{creds['endpoint']}/account", headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()


def main():
    registry = capital_allocator.load_registry()
    entries  = registry.get("strategies", [])
    if not entries:
        print("No strategies registered in config/strategy_framework.json.")
        return

    account = get_account()

    rows = []
    for e in entries:
        name    = e.get("name")
        enabled = e.get("enabled", True)
        equity  = capital_allocator.allocated_equity(name, account, registry) if enabled else 0.0
        summary = strategy_ledger.summarize(name)
        roi_pct = (summary["realized_pl"] / equity * 100) if equity else 0.0
        rows.append({
            "name":        name,
            "enabled":     enabled,
            "equity":      equity,
            "realized_pl": summary["realized_pl"],
            "roi_pct":     roi_pct,
            "trade_count": summary["trade_count"],
            "win_rate":    summary["win_rate"],
        })

    header = f"{'Strategy':<15}{'Enabled':<9}{'Allocated $':>13}{'Realized P&L':>14}{'ROI %':>9}{'Trades':>8}{'Win %':>8}"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['name']:<15}{str(r['enabled']):<9}{r['equity']:>13,.2f}"
            f"{r['realized_pl']:>14,.2f}{r['roi_pct']:>8.1f}%{r['trade_count']:>8}"
            f"{r['win_rate']*100:>7.1f}%"
        )


if __name__ == "__main__":
    main()
