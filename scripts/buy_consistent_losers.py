#!/usr/bin/env python3
"""
Daily auto-buy for Consistent-History Losers qualifiers.

Reads the most recent qualifying list saved by signals/consistent_losers.py
(computed after the prior trading day's close), buys the largest whole-share
quantity worth ~$500 of each symbol, and attaches a 5% trailing stop-loss
(floor rises with price, Alpaca-managed, never falls on its own).

Whole shares, not a $500 notional order, on purpose: Alpaca's trailing_stop
order type rejects fractional quantities (HTTP 422). From 2026-08-03 (this
task's start) through 2026-08-27, buys were notional/fractional, so every
single trailing-stop attempt failed -- 70 buys, $0 in protective stops ever
placed, discovered only when a portfolio check turned up naked positions.
Fixed 2026-08-27, along with the Task Scheduler start time moving from
9:25 AM to 9:31 AM ET: at 9:25 the market isn't open yet, so wait_for_fill's
poll window was elapsing before the order could fill, silently orphaning it
(the order still filled once the market opened; the script just never found
out, logged "Bought 0/N" every day, and never got to the trailing-stop step).

The flat 3% trailing stop placed here is a SAFETY NET ONLY as of 2026-08-29
-- scripts/dynamic_stop_manager.py (Task Scheduler \Alpaca\DynamicStopManager,
every 15 min market hours) takes each lot over within minutes of purchase,
canceling this native trailing_stop and replacing it with a per-lot staged
stop (3% tight -> freeze at breakeven -> 5% wide trail once up 5%). See that
script's docstring for the full logic. This one still places its own stop
immediately at buy time so a lot is never naked in the gap before the
manager's next cycle picks it up.

Intended to run shortly after the opening bell, so market orders fill at/near
the open using the prior close's signal.

NOT scheduled automatically -- register the Task Scheduler entry yourself.
"""

import json
import math
import sys
import time
from datetime import date
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent / "signals"))
from signal_utils import get_logger, load_state, save_state

BASE_DIR = Path(__file__).parent.parent
with open(BASE_DIR / "config" / "alpaca_credentials.json") as f:
    creds = json.load(f)
BASE_URL = creds["endpoint"]
DATA_URL = "https://data.alpaca.markets"
HEADERS  = {
    "APCA-API-KEY-ID":     creds["api_key"],
    "APCA-API-SECRET-KEY": creds["api_secret"],
}

log = get_logger("buy_consistent_losers", "buy_consistent_losers.log")

NOTIONAL_PER_BUY    = 500
TRAIL_PERCENT       = 3.0
FILL_POLL_SECS      = 2
FILL_POLL_TRIES     = 150  # 5 min -- covers orders placed before the 9:30 open queuing until it fills
MAX_SIGNAL_AGE_DAYS = 4  # weekend/holiday buffer -- older than this, treat as stale


def get_latest_price(symbol: str) -> float:
    r = requests.get(f"{DATA_URL}/v2/stocks/{symbol}/trades/latest", headers=HEADERS, timeout=15)
    r.raise_for_status()
    return float(r.json()["trade"]["p"])


def submit_buy(symbol: str, qty: int) -> str:
    # Whole-share qty, not notional: Alpaca's trailing_stop order type rejects
    # fractional quantities (HTTP 422), so a notional/fractional buy here would
    # make the trailing-stop leg below fail every time, unconditionally.
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json={
        "symbol": symbol,
        "qty": str(qty),
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
    }, timeout=15)
    r.raise_for_status()
    return r.json()["id"]


def wait_for_fill(order_id: str):
    for _ in range(FILL_POLL_TRIES):
        r = requests.get(f"{BASE_URL}/orders/{order_id}", headers=HEADERS, timeout=15)
        r.raise_for_status()
        o = r.json()
        if o["status"] == "filled":
            return float(o["filled_qty"]), float(o["filled_avg_price"])
        if o["status"] in ("canceled", "expired", "rejected"):
            raise RuntimeError(f"order {order_id} ended in status {o['status']}")
        time.sleep(FILL_POLL_SECS)
    raise TimeoutError(f"order {order_id} did not fill within {FILL_POLL_TRIES * FILL_POLL_SECS}s")


def get_position_qty(symbol: str) -> float:
    r = requests.get(f"{BASE_URL}/positions/{symbol}", headers=HEADERS, timeout=15)
    if r.status_code == 404:
        return 0.0
    r.raise_for_status()
    return float(r.json()["qty"])


def sellable_qty(symbol: str) -> int:
    """Whole shares held minus shares already reserved by open sell orders --
    the most a new stop can cover without going short."""
    held = get_position_qty(symbol)
    if held < 1:
        return 0
    r = requests.get(f"{BASE_URL}/orders", headers=HEADERS,
                     params={"status": "open", "symbols": symbol, "limit": 500}, timeout=15)
    r.raise_for_status()
    reserved = sum(float(o["qty"]) - float(o.get("filled_qty") or 0) for o in r.json() if o["side"] == "sell")
    return max(0, math.floor(held) - math.ceil(reserved))


def submit_trailing_stop(symbol: str, qty: float) -> str:
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json={
        "symbol": symbol,
        "qty": str(qty),
        "side": "sell",
        "type": "trailing_stop",
        "trail_percent": str(TRAIL_PERCENT),
        "time_in_force": "gtc",
    }, timeout=15)
    r.raise_for_status()
    return r.json()["id"]


def run():
    log.info("=" * 65)
    log.info(f"BUY CONSISTENT-HISTORY LOSERS  |  {date.today().isoformat()}")
    log.info("=" * 65)

    buy_state = load_state("buy_consistent_losers.json")
    today_str = date.today().isoformat()
    if buy_state.get("last_run") == today_str:
        log.info("  Already ran today — skipping.")
        return

    signal_state    = load_state("consistent_losers.json")
    qualifiers      = signal_state.get("last_qualifiers") or []
    signal_date_str = signal_state.get("last_qualifiers_date") or signal_state.get("last_run")

    if not signal_date_str:
        log.warning("  No consistent_losers signal state found — nothing to buy.")
        return

    age_days = (date.today() - date.fromisoformat(signal_date_str)).days
    if age_days > MAX_SIGNAL_AGE_DAYS:
        log.warning(f"  Signal is {age_days} day(s) old (from {signal_date_str}) — "
                     f"stale, skipping to avoid trading on outdated data.")
        return

    if not qualifiers:
        log.info(f"  Signal from {signal_date_str}: no qualifiers — nothing to buy today.")
        buy_state["last_run"] = today_str
        save_state("buy_consistent_losers.json", buy_state)
        return

    log.info(f"  Signal from {signal_date_str}: {len(qualifiers)} symbol(s) to buy")

    bought = []
    for q in qualifiers:
        symbol = q["symbol"]
        try:
            existing = get_position_qty(symbol)
            if existing < 0:
                # Buying into a short just covers it, and the stop placed
                # below would then sell shares that aren't held and re-open
                # it (MGM, 2026-09-15/16). Shorts are an error to fix by hand.
                log.error(f"    SKIP {symbol}: account is SHORT {existing:g} shares -- not buying "
                           f"(this strategy never shorts; cover it manually)")
                continue
            price = get_latest_price(symbol)
            qty_to_buy = max(1, int(NOTIONAL_PER_BUY // price))
            log.info(f"  {symbol}: price ${price:.2f} -> buying {qty_to_buy} whole share(s) "
                      f"(~${qty_to_buy * price:.2f}, target was ${NOTIONAL_PER_BUY})...")
            buy_id = submit_buy(symbol, qty_to_buy)
            qty, avg_price = wait_for_fill(buy_id)
            log.info(f"    Filled {qty} shares @ ${avg_price:.2f}")
            stop_qty = min(int(qty), sellable_qty(symbol))
            if stop_qty < int(qty):
                log.warning(f"    Only {stop_qty} of {int(qty)} shares are sellable -- stopping {stop_qty}")
            if stop_qty < 1:
                log.error(f"    {symbol}: no sellable shares after the buy -- no stop placed")
                continue
            stop_id = submit_trailing_stop(symbol, stop_qty)
            log.info(f"    Trailing stop placed: {TRAIL_PERCENT}% trail, order {stop_id}")
            bought.append(symbol)
        except Exception as e:
            log.error(f"    ERROR on {symbol}: {e}")

    buy_state["last_run"] = today_str
    buy_state["last_bought"] = bought
    save_state("buy_consistent_losers.json", buy_state)
    log.info(f"  Done. Bought {len(bought)}/{len(qualifiers)}.")


if __name__ == "__main__":
    run()
    # Logged only if run() returned normally -- scripts/heartbeat.py treats a
    # run with no "Run complete." as crashed.
    log.info("Run complete.\n")
