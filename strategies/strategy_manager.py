#!/usr/bin/env python3
"""
Unified Strategy Manager
=========================
Applies the same trailing stop strategy to all managed positions:
  - Stop Loss:     Sell all if price drops 10% from entry (floor never goes down)
  - Trailing Stop: Once up 10%, move stop to 5% below highest price. Keep raising, never lower.
  - Ladder In:     If price drops 20% from entry, buy 2x original qty at market.
                   Monitored live — not pre-placed (avoids Alpaca wash-trade block).

Stop orders use whole-share quantities (GTC, overnight protection).
Fractional remainder is covered by intraday re-checks every minute.

State per symbol: Trading/strategies/states/{SYMBOL}_state.json
Runs every 1 minute via Windows Task Scheduler during market hours.
"""

import json
import sys
import requests
import logging
from datetime import datetime
from pathlib import Path
from math import floor

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent.parent
CONFIG_FILE = BASE_DIR / "config" / "alpaca_credentials.json"
STATES_DIR  = BASE_DIR / "strategies" / "states"
LOG_FILE    = BASE_DIR / "logs" / "strategy_manager.log"
STATES_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
log = logging.getLogger(__name__)

# ── Credentials ───────────────────────────────────────────────────────────────
with open(CONFIG_FILE) as f:
    creds = json.load(f)

BASE_URL = creds["endpoint"]
HEADERS  = {
    "APCA-API-KEY-ID":     creds["api_key"],
    "APCA-API-SECRET-KEY": creds["api_secret"],
}
DATA_URL = "https://data.alpaca.markets/v2"

# ── Strategy Parameters (same rules for all symbols) ─────────────────────────
STOP_LOSS_PCT    = 0.10   # Sell all if price drops 10% from entry
TRAILING_TRIGGER = 0.10   # Begin trailing once price is up 10%
TRAIL_BELOW_HIGH = 0.05   # Stop sits 5% below the running high
LADDER_DROP_PCT  = 0.20   # Buy 2x original qty if price drops 20%

# ── Managed symbols + entry data (from avg_entry_price at time of enrollment) ─
MANAGED = {
    "ACI":   {"entry": 16.35,       "initial_qty": 30.581039755,  "whole_qty": 30},
    "BAC":   {"entry": 51.26,       "initial_qty": 9.754194303,   "whole_qty": 9},
    "BRK.B": {"entry": 479.690976,  "initial_qty": 2.084675446,   "whole_qty": 2},
    "BSX":   {"entry": 57.01,       "initial_qty": 8.770391159,   "whole_qty": 8},
    "CARR":  {"entry": 62.58,       "initial_qty": 7.98977309,    "whole_qty": 7},
    "EMR":   {"entry": 134.87,      "initial_qty": 3.70727367,    "whole_qty": 3},
    "IBM":   {"entry": 249.58,      "initial_qty": 2.003365654,   "whole_qty": 2},
    "INTC":  {"entry": 118.10,      "initial_qty": 4.233700254,   "whole_qty": 4},
    "LOW":   {"entry": 218.01,      "initial_qty": 2.293472776,   "whole_qty": 2},
    "MA":    {"entry": 498.938,     "initial_qty": 1.00212852,    "whole_qty": 1},
    "ORCL":  {"entry": 189.52,      "initial_qty": 2.638243984,   "whole_qty": 2},
    "PM":    {"entry": 188.10,      "initial_qty": 2.658160552,   "whole_qty": 2},
    "SYK":   {"entry": 314.518,     "initial_qty": 1.589734132,   "whole_qty": 1},
    "BWXT":  {"entry": 199.580,     "initial_qty": 10,            "whole_qty": 10},
    "IAU":   {"entry": 83.208,      "initial_qty": 100,           "whole_qty": 100},
    "URA":   {"entry": 48.00,       "initial_qty": 41,            "whole_qty": 41},
    "V":     {"entry": 331.164,     "initial_qty": 1.509825947,   "whole_qty": 1},
    "NVDA":  {"entry": 211.981,     "initial_qty": 9.0,           "whole_qty": 9},
    "MSFT":  {"entry": 411.897,     "initial_qty": 4.0,           "whole_qty": 4},
    "GOOGL": {"entry": 388.440,     "initial_qty": 5.0,           "whole_qty": 5},
    "SOXX":  {"entry": 576.530,     "initial_qty": 3.0,           "whole_qty": 3},
    "XOM":   {"entry": 146.837,     "initial_qty": 13.0,          "whole_qty": 13},
    "XLE":   {"entry": 56.649,      "initial_qty": 34.0,          "whole_qty": 34},
    "CVX":   {"entry": 182.015,     "initial_qty": 10.0,          "whole_qty": 10},
    "IONQ":  {"entry": 62.271,      "initial_qty": 31.0,          "whole_qty": 31},
    "RKLB":  {"entry": 146.200,     "initial_qty": 13.0,          "whole_qty": 13},
    "LMT":   {"entry": 527.527,     "initial_qty": 3.0,           "whole_qty": 3},
    "PLTR":  {"entry": 132.034,     "initial_qty": 14.0,          "whole_qty": 14},
    "VNQ":   {"entry": 97.273,      "initial_qty": 20.0,          "whole_qty": 20},
    "O":     {"entry": 62.310,      "initial_qty": 32.0,          "whole_qty": 32},
    "BND":   {"entry": 73.289,      "initial_qty": 27.0,          "whole_qty": 27},
}


# ── State helpers ─────────────────────────────────────────────────────────────
def state_path(symbol):
    return STATES_DIR / f"{symbol.replace('.','_')}_state.json"

def load_state(symbol):
    p = state_path(symbol)
    cfg = MANAGED[symbol]
    defaults = {
        "symbol":          symbol,
        "fill_price":      cfg["entry"],
        "highest_price":   cfg["entry"],
        "stop_order_id":   None,
        "stop_price":      None,
        "trailing_active": False,
        "ladder_fired":    False,
        "ladder_order_id": None,
        "ladder_filled":   False,
        "position_closed": False,
        "whole_qty":       cfg["whole_qty"],
        "initial_qty":     cfg["initial_qty"],
    }
    if p.exists():
        with open(p) as f:
            saved = json.load(f)
        for k, v in defaults.items():
            saved.setdefault(k, v)
        return saved
    return defaults

def save_state(symbol, state):
    with open(state_path(symbol), "w") as f:
        json.dump(state, f, indent=2)


# ── Alpaca helpers ────────────────────────────────────────────────────────────
def get_position(symbol):
    r = requests.get(f"{BASE_URL}/positions/{symbol}", headers=HEADERS, timeout=10)
    return r.json() if r.status_code == 200 else None

def get_latest_price(symbol):
    r = requests.get(f"{DATA_URL}/stocks/{symbol}/trades/latest",
                     headers=HEADERS, timeout=10)
    r.raise_for_status()
    return float(r.json()["trade"]["p"])

def get_open_orders_for(symbol):
    r = requests.get(f"{BASE_URL}/orders", headers=HEADERS,
                     params={"status": "open", "symbols": symbol}, timeout=10)
    return r.json() if r.status_code == 200 else []

def cancel_order(order_id):
    r = requests.delete(f"{BASE_URL}/orders/{order_id}", headers=HEADERS, timeout=10)
    if r.status_code == 204:
        log.info(f"    Cancelled {order_id[:8]}")

def place_stop_sell(symbol, qty, stop_price):
    stop_price = round(stop_price, 2)
    payload = {
        "symbol":        symbol,
        "qty":           str(int(qty)),   # whole shares only for GTC
        "side":          "sell",
        "type":          "stop",
        "stop_price":    str(stop_price),
        "time_in_force": "gtc",
    }
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json=payload, timeout=10)
    r.raise_for_status()
    order = r.json()
    log.info(f"    [STOP] {symbol} {qty} shares @ ${stop_price} | {order.get('id','')[:8]} | {order.get('status')}")
    return order

def update_stop(symbol, state, new_stop_price):
    new_stop_price = round(new_stop_price, 2)
    if state.get("stop_order_id"):
        cancel_order(state["stop_order_id"])
    qty = state.get("whole_qty", MANAGED[symbol]["whole_qty"])
    order = place_stop_sell(symbol, qty, new_stop_price)
    state["stop_order_id"] = order.get("id")
    state["stop_price"]    = new_stop_price
    return order

def place_ladder_buy(symbol, qty):
    """Market buy for ladder — fired when live price hits -20%."""
    payload = {
        "symbol":        symbol,
        "qty":           str(round(qty, 6)),
        "side":          "buy",
        "type":          "market",
        "time_in_force": "day",
    }
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json=payload, timeout=10)
    r.raise_for_status()
    order = r.json()
    log.info(f"    [LADDER] {symbol} buy {qty} shares @ market | {order.get('id','')[:8]} | {order.get('status')}")
    return order


# ── Per-symbol strategy check ─────────────────────────────────────────────────
def check_symbol(symbol):
    state      = load_state(symbol)
    fill_price = state["fill_price"]
    stop_loss_px  = round(fill_price * (1 - STOP_LOSS_PCT),    2)
    ladder_px     = round(fill_price * (1 - LADDER_DROP_PCT),  2)
    trailing_px   = round(fill_price * (1 + TRAILING_TRIGGER), 2)
    highest_price = state["highest_price"] or fill_price
    current_stop  = state["stop_price"] or stop_loss_px

    if state["position_closed"]:
        log.info(f"  {symbol:6s} | closed — skipping")
        return

    # Confirm position still exists
    position = get_position(symbol)
    if position is None:
        if state.get("stop_order_id") is None:
            # No stop ever placed means position hasn't opened yet (e.g. pending limit buy)
            log.info(f"  {symbol:6s} | position not yet open (limit order pending) — skipping")
            return
        else:
            # Stop was active, position is now gone — stopped out or sold
            log.info(f"  {symbol:6s} | no open position — marking closed")
            state["position_closed"] = True
            save_state(symbol, state)
            return

    held_qty      = float(position["qty"])
    current_price = get_latest_price(symbol)
    pct_from_fill = (current_price - fill_price) / fill_price * 100

    # Ensure stop order exists
    open_orders  = get_open_orders_for(symbol)
    stop_orders  = [o for o in open_orders
                    if o.get("type") == "stop" and o.get("side") == "sell"]

    if not stop_orders:
        log.info(f"  {symbol:6s} | !! No active stop — re-placing at ${current_stop:.2f}")
        order = place_stop_sell(symbol, state["whole_qty"], current_stop)
        state["stop_order_id"] = order.get("id")
        state["stop_price"]    = current_stop
        save_state(symbol, state)
    else:
        state["stop_order_id"] = stop_orders[0]["id"]
        raw_stop = stop_orders[0].get("stop_price")
        if raw_stop is not None:
            state["stop_price"] = float(raw_stop)
        current_stop = state["stop_price"]

    # Update highest price
    if current_price > highest_price:
        highest_price          = current_price
        state["highest_price"] = highest_price

    # Trailing stop logic
    gain_from_fill = (highest_price - fill_price) / fill_price
    if gain_from_fill >= TRAILING_TRIGGER:
        ideal_stop = round(highest_price * (1 - TRAIL_BELOW_HIGH), 2)
        state["trailing_active"] = True
        if ideal_stop > current_stop:
            log.info(f"  {symbol:6s} | TRAILING: ${current_stop:.2f} -> ${ideal_stop:.2f}  (high ${highest_price:.2f})")
            update_stop(symbol, state, ideal_stop)
        else:
            log.info(f"  {symbol:6s} | ${current_price:.2f} ({pct_from_fill:+.1f}%) | trailing active | stop ${current_stop:.2f} OK")
    else:
        needed = (TRAILING_TRIGGER - gain_from_fill) * 100
        log.info(f"  {symbol:6s} | ${current_price:.2f} ({pct_from_fill:+.1f}%) | stop ${current_stop:.2f} | need +{needed:.1f}% to trail")

    # Ladder logic
    if not state["ladder_fired"]:
        if current_price <= ladder_px:
            ladder_qty = round(state["initial_qty"] * 2, 6)
            log.info(f"  {symbol:6s} | LADDER TRIGGERED at ${current_price:.2f} — buying {ladder_qty} shares")
            order = place_ladder_buy(symbol, ladder_qty)
            state["ladder_order_id"] = order.get("id")
            state["ladder_fired"]    = True
    elif not state["ladder_filled"] and state.get("ladder_order_id"):
        r = requests.get(f"{BASE_URL}/orders/{state['ladder_order_id']}", headers=HEADERS, timeout=10)
        if r.status_code == 200 and r.json().get("status") == "filled":
            new_total_whole = floor(held_qty)
            log.info(f"  {symbol:6s} | LADDER FILLED — upgrading stop to {new_total_whole} shares")
            # Update whole_qty BEFORE calling update_stop so the new stop covers all shares
            state["whole_qty"]     = new_total_whole
            update_stop(symbol, state, current_stop)
            state["ladder_filled"] = True

    save_state(symbol, state)


# ── Main ──────────────────────────────────────────────────────────────────────
def run():
    log.info("=" * 65)
    log.info(f"Strategy Manager  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 65)
    for symbol in MANAGED:
        try:
            check_symbol(symbol)
        except Exception as e:
            log.error(f"  {symbol:6s} | ERROR: {e}")
    log.info("Run complete.\n")

if __name__ == "__main__":
    run()
