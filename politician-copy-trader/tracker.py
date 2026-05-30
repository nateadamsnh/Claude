#!/usr/bin/env python3
"""
P&L tracker for politician copy trades.
Reads executed_trades from state.json and prints a performance report.
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import requests

log = logging.getLogger(__name__)

BASE_DIR    = Path(__file__).parent
STATE_FILE  = BASE_DIR / "state.json"
LOG_FILE    = BASE_DIR.parent / "logs" / "copy_trader.log"

# Reuse Alpaca creds for price lookups
CONFIG_FILE = BASE_DIR.parent / "config" / "alpaca_credentials.json"
with open(CONFIG_FILE) as f:
    _creds = json.load(f)

_HEADERS = {
    "APCA-API-KEY-ID":     _creds["api_key"],
    "APCA-API-SECRET-KEY": _creds["api_secret"],
}
DATA_URL = "https://data.alpaca.markets/v2"


def _get_latest_price(symbol: str) -> float:
    try:
        r = requests.get(
            f"{DATA_URL}/stocks/{symbol}/trades/latest",
            headers=_HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        return float(r.json()["trade"]["p"])
    except Exception:
        return 0.0


def _refresh_fills(state: dict):
    """Pull latest fill data from Alpaca for any executed trades that aren't filled yet."""
    BASE_URL = _creds["endpoint"]
    for t in state.get("executed_trades", []):
        if t.get("closed"):
            continue
        if t.get("filled_qty") and float(t["filled_qty"]) > 0:
            continue  # already have fill data
        order_id = t.get("order_id")
        if not order_id:
            continue
        try:
            r = requests.get(f"{BASE_URL}/orders/{order_id}", headers=_HEADERS, timeout=10)
            if r.status_code == 200:
                o = r.json()
                t["filled_qty"]       = o.get("filled_qty")
                t["filled_avg_price"] = o.get("filled_avg_price")
                t["order_status"]     = o.get("status")
        except Exception:
            pass


def generate_report() -> str:
    if not STATE_FILE.exists():
        return "No state file found. No trades executed yet."

    with open(STATE_FILE) as f:
        state = json.load(f)

    _refresh_fills(state)
    # Save refreshed fill data back to state
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

    executed = state.get("executed_trades", [])
    if not executed:
        return "No executed trades to report on yet."

    lines = []
    lines.append("=" * 72)
    lines.append(f"COPY TRADER P&L REPORT  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Politician: {state.get('politician_name', 'Unknown')}")
    lines.append("=" * 72)

    total_invested = 0.0
    total_current  = 0.0
    open_trades    = []
    closed_trades  = []

    for t in executed:
        if t.get("closed"):
            closed_trades.append(t)
        else:
            open_trades.append(t)

    # Open positions
    if open_trades:
        lines.append(f"\n{'OPEN POSITIONS':─<72}")
        lines.append(f"{'Symbol':<8} {'Invested':>10} {'Current':>10} {'P&L $':>10} {'P&L %':>8}  {'Traded'}")
        lines.append("─" * 72)
        for t in sorted(open_trades, key=lambda x: x.get("executed_at", "")):
            symbol    = t["symbol"]
            invested  = float(t.get("notional_usd", 0))
            shares    = float(t.get("filled_qty", 0) or 0)
            price_now = _get_latest_price(symbol)
            current   = shares * price_now if shares and price_now else None

            total_invested += invested

            if current is None:
                # Order not yet filled — use invested as placeholder for totals
                total_current += invested
                lines.append(
                    f"{symbol:<8} {invested:>10.2f} {'(pending)':>10}  {'---':>9}    {'---':>7}   {t.get('tx_date','')}"
                )
            else:
                pnl_usd  = current - invested
                pnl_pct  = (pnl_usd / invested * 100) if invested else 0.0
                total_current += current
                sign = "+" if pnl_usd >= 0 else ""
                lines.append(
                    f"{symbol:<8} {invested:>10.2f} {current:>10.2f} "
                    f"{sign}{pnl_usd:>9.2f} {sign}{pnl_pct:>7.1f}%  {t.get('tx_date','')}"
                )

    # Closed positions
    if closed_trades:
        lines.append(f"\n{'CLOSED POSITIONS':─<72}")
        lines.append(f"{'Symbol':<8} {'Invested':>10} {'Proceeds':>10} {'P&L $':>10} {'P&L %':>8}  {'Traded'}")
        lines.append("─" * 72)
        for t in sorted(closed_trades, key=lambda x: x.get("closed_at", "")):
            symbol   = t["symbol"]
            invested = float(t.get("notional_usd", 0))
            proceeds = float(t.get("close_proceeds", 0) or 0)
            pnl_usd  = proceeds - invested
            pnl_pct  = (pnl_usd / invested * 100) if invested else 0.0

            total_invested += invested
            total_current  += proceeds if proceeds else invested

            sign = "+" if pnl_usd >= 0 else ""
            lines.append(
                f"{symbol:<8} {invested:>10.2f} {proceeds:>10.2f} "
                f"{sign}{pnl_usd:>9.2f} {sign}{pnl_pct:>7.1f}%  {t.get('tx_date','')}"
            )

    # Summary
    total_pnl     = total_current - total_invested
    total_pnl_pct = (total_pnl / total_invested * 100) if total_invested else 0.0
    sign = "+" if total_pnl >= 0 else ""

    lines.append("─" * 72)
    lines.append(
        f"{'TOTAL':<8} {total_invested:>10.2f} {total_current:>10.2f} "
        f"{sign}{total_pnl:>9.2f} {sign}{total_pnl_pct:>7.1f}%"
    )
    lines.append(f"\nOpen trades  : {len(open_trades)}")
    lines.append(f"Closed trades: {len(closed_trades)}")
    lines.append(f"Seen trades  : {len(state.get('seen_trade_keys', []))}")
    lines.append("=" * 72)

    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(generate_report())
