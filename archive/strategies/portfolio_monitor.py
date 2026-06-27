#!/usr/bin/env python3
"""
Portfolio Monitor — All Positions
===================================
Runs every 5 minutes during market hours via Windows Task Scheduler.

For every managed position it:
  1. Pulls current position and P&L from Alpaca
  2. Verifies the stop-loss order is still active — re-places if missing
  3. Evaluates trailing stop logic and raises the floor if warranted
  4. Logs a full summary to logs/portfolio_monitor.log

Covers ALL managed symbols:
  - QBTS  (individual state file)
  - ACI, BAC, BRK.B, BSX, CARR, EMR, IBM, INTC,
    LOW, MA, ORCL, PM, SYK, V  (strategy_manager state files)
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
LOG_FILE    = BASE_DIR / "logs" / "portfolio_monitor.log"
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

# ── Strategy parameters (same for all) ───────────────────────────────────────
STOP_LOSS_PCT    = 0.10
TRAILING_TRIGGER = 0.10
TRAIL_BELOW_HIGH = 0.05
LADDER_DROP_PCT  = 0.20

# ── All managed symbols and their state file locations ────────────────────────
def state_file_for(symbol):
    """Return the correct state file path for a given symbol."""
    if symbol == "QBTS":
        return BASE_DIR / "strategies" / "qbts_strategy_state.json"
    return BASE_DIR / "strategies" / "states" / f"{symbol.replace('.','_')}_state.json"

ALL_SYMBOLS = [
    "QBTS",
    "ACI", "BAC", "BRK.B", "BSX", "CARR", "EMR", "IBM", "INTC",
    "BWXT", "IAU", "LOW", "MA", "ORCL", "PM", "SYK", "URA", "V",
    "BND", "CVX", "GOOGL", "IONQ", "LMT", "MSFT", "NVDA", "O", "PLTR", "RKLB", "SOXX", "VNQ", "XLE", "XOM",
]


# ── Alpaca helpers ────────────────────────────────────────────────────────────
def get_account():
    r = requests.get(f"{BASE_URL}/account", headers=HEADERS, timeout=10)
    return r.json()

def get_all_positions():
    r = requests.get(f"{BASE_URL}/positions", headers=HEADERS, timeout=10)
    return {p["symbol"]: p for p in r.json()} if r.status_code == 200 else {}

def get_all_open_orders():
    r = requests.get(f"{BASE_URL}/orders", headers=HEADERS,
                     params={"status": "open", "limit": 100}, timeout=10)
    orders = r.json() if r.status_code == 200 else []
    result = {}
    for o in orders:
        sym = o["symbol"]
        result.setdefault(sym, []).append(o)
    return result

def get_latest_price(symbol):
    r = requests.get(f"{DATA_URL}/stocks/{symbol}/trades/latest",
                     headers=HEADERS, timeout=10)
    return float(r.json()["trade"]["p"])

def cancel_order(order_id):
    r = requests.delete(f"{BASE_URL}/orders/{order_id}", headers=HEADERS, timeout=10)
    log.info(f"      Cancelled {order_id[:8]} (HTTP {r.status_code})")

def place_stop_sell(symbol, qty, stop_price):
    stop_price = round(stop_price, 2)
    payload = {
        "symbol":        symbol,
        "qty":           str(int(qty)),
        "side":          "sell",
        "type":          "stop",
        "stop_price":    str(stop_price),
        "time_in_force": "gtc",
    }
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json=payload, timeout=10)
    order = r.json()
    log.info(f"      [NEW STOP] {symbol} {int(qty)} shares @ ${stop_price} | {order.get('id','')[:8]} | {order.get('status','error')}")
    return order

def load_state(symbol):
    p = state_file_for(symbol)
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return {}

def save_state(symbol, state):
    with open(state_file_for(symbol), "w") as f:
        json.dump(state, f, indent=2)

def session_label():
    h = datetime.now().hour
    if h < 11:   return "MORNING OPEN CHECK"
    elif h < 14: return "MIDDAY P&L CHECK"
    else:        return "END-OF-DAY REVIEW"


# ── Per-symbol check ──────────────────────────────────────────────────────────
def check_symbol(symbol, positions, open_orders_by_symbol):
    state = load_state(symbol)

    if not state or state.get("position_closed"):
        log.info(f"  {symbol:6s} | no active strategy — skipping")
        return

    fill_price    = state.get("fill_price")
    if not fill_price:
        log.info(f"  {symbol:6s} | state not initialized — skipping")
        return

    highest_price = state.get("highest_price") or fill_price
    stop_loss_px  = round(fill_price * (1 - STOP_LOSS_PCT),    2)
    ladder_px     = round(fill_price * (1 - LADDER_DROP_PCT),  2)
    trailing_px   = round(fill_price * (1 + TRAILING_TRIGGER), 2)
    current_stop  = state.get("stop_price") or stop_loss_px

    # Determine protected qty (whole shares for GTC)
    whole_qty = state.get("whole_qty") or state.get("total_qty") or state.get("initial_qty", 1)
    whole_qty = int(floor(float(whole_qty)))

    # Check position still open
    position = positions.get(symbol)
    if not position:
        log.info(f"  {symbol:6s} | no open position — marking closed")
        state["position_closed"] = True
        save_state(symbol, state)
        return

    held_qty      = float(position["qty"])
    current_price = get_latest_price(symbol)
    mkt_value     = float(position["market_value"])
    unreal_pl     = float(position["unrealized_pl"])
    unreal_plpc   = float(position["unrealized_plpc"]) * 100
    pct_from_fill = (current_price - fill_price) / fill_price * 100

    log.info(f"  {symbol:6s} | ${current_price:.2f} ({pct_from_fill:+.1f}%) | MV ${mkt_value:,.0f} | P&L ${unreal_pl:+.2f} ({unreal_plpc:+.1f}%)")

    # ── Verify stop order is active ───────────────────────────────────────────
    sym_orders  = open_orders_by_symbol.get(symbol, [])
    stop_orders = [o for o in sym_orders if o.get("type") == "stop" and o.get("side") == "sell"]

    if not stop_orders:
        log.warning(f"  {symbol:6s} | !! MISSING STOP — re-placing {whole_qty} shares @ ${current_stop:.2f}")
        order = place_stop_sell(symbol, whole_qty, current_stop)
        state["stop_order_id"] = order.get("id")
        state["stop_price"]    = current_stop
        save_state(symbol, state)
    else:
        live_stop = float(stop_orders[0]["stop_price"])
        log.info(f"      Stop OK: ${live_stop:.2f} ({stop_orders[0]['id'][:8]}...)")
        if live_stop != current_stop:
            state["stop_price"]    = live_stop
            state["stop_order_id"] = stop_orders[0]["id"]
            current_stop           = live_stop
            save_state(symbol, state)

    # ── Update highest price ──────────────────────────────────────────────────
    if current_price > highest_price:
        highest_price          = current_price
        state["highest_price"] = highest_price
        log.info(f"      ** NEW HIGH: ${highest_price:.2f}")

    # ── Trailing stop evaluation ──────────────────────────────────────────────
    gain_from_fill = (highest_price - fill_price) / fill_price
    if gain_from_fill >= TRAILING_TRIGGER:
        ideal_stop = round(highest_price * (1 - TRAIL_BELOW_HIGH), 2)
        state["trailing_active"] = True
        if ideal_stop > current_stop:
            log.info(f"      >> TRAILING: ${current_stop:.2f} -> ${ideal_stop:.2f} (high ${highest_price:.2f})")
            if state.get("stop_order_id"):
                cancel_order(state["stop_order_id"])
            order = place_stop_sell(symbol, whole_qty, ideal_stop)
            state["stop_order_id"] = order.get("id")
            state["stop_price"]    = ideal_stop
        else:
            log.info(f"      Trailing active (+{gain_from_fill*100:.1f}%) — stop ${current_stop:.2f} is current")
    else:
        needed = (TRAILING_TRIGGER - gain_from_fill) * 100
        log.info(f"      Trailing: need +{needed:.1f}% more (trigger at ${trailing_px:.2f})")

    # ── Ladder status ─────────────────────────────────────────────────────────
    if state.get("ladder_fired"):
        ladder_status = "FILLED" if state.get("ladder_filled") else "PENDING"
        log.info(f"      Ladder: {ladder_status}")
    else:
        gap = ((current_price - ladder_px) / fill_price) * 100
        log.info(f"      Ladder: not triggered (${current_price:.2f} vs ${ladder_px:.2f}, need -{gap:.1f}% more)")

    save_state(symbol, state)


# ── Main ──────────────────────────────────────────────────────────────────────
def run():
    label = session_label()
    log.info("=" * 65)
    log.info(f"PORTFOLIO MONITOR  |  {label}")
    log.info(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S ET')}")
    log.info("=" * 65)

    # Fetch everything in bulk upfront (one API call each)
    acct              = get_account()
    positions         = get_all_positions()
    open_orders_by_sym = get_all_open_orders()

    port_val = float(acct["portfolio_value"])
    cash     = float(acct["cash"])
    equity   = float(acct["equity"])
    last_eq  = float(acct["last_equity"])
    day_pl   = equity - last_eq

    log.info(f"  Portfolio Value  : ${port_val:>12,.2f}")
    log.info(f"  Day P&L          : ${day_pl:>+12,.2f}")
    log.info(f"  Cash             : ${cash:>12,.2f}")
    log.info(f"  Open Positions   : {len(positions)}")
    log.info("")

    for symbol in ALL_SYMBOLS:
        try:
            check_symbol(symbol, positions, open_orders_by_sym)
        except Exception as e:
            log.error(f"  {symbol:6s} | ERROR: {e}")
        log.info("")

    log.info("=" * 65)
    log.info("Monitor run complete.\n")


if __name__ == "__main__":
    run()
