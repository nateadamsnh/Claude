#!/usr/bin/env python3
"""
TSLA Trailing Stop Strategy
============================
Rules:
  - Entry:         10 shares at market (already placed)
  - Stop Loss:     Sell all 10 shares if price drops 10% from fill price (floor never goes down)
  - Trailing Stop: Once price rises 10% from fill, move stop to 5% below highest price.
                   Every time price makes a new high, raise the stop again. Never lower it.
  - Ladder In:     If live price drops 20% below fill price, buy 20 additional shares at market.
                   NOTE: Ladder is NOT pre-placed as a standing order — Alpaca blocks a buy+sell
                   on the same ticker simultaneously (wash-trade guard). Instead, the script
                   monitors price every minute and fires a market buy the moment the -20% level
                   is breached.

This script is designed to be run on a schedule (every 1 minute during market hours).
State is persisted to tsla_strategy_state.json so it survives restarts.
"""

import json
import sys
import requests
import logging
from datetime import datetime
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent.parent
CONFIG_FILE = BASE_DIR / "config" / "alpaca_credentials.json"
STATE_FILE  = BASE_DIR / "strategies" / "tsla_strategy_state.json"
LOG_FILE    = BASE_DIR / "logs" / "tsla_strategy.log"

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
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

# ── Strategy Parameters ────────────────────────────────────────────────────────
SYMBOL              = "TSLA"
INITIAL_QTY         = 10
LADDER_QTY          = 20
STOP_LOSS_PCT       = 0.10   # Sell all if price drops 10% from fill
TRAILING_TRIGGER    = 0.10   # Begin trailing once price is up 10%
TRAIL_BELOW_HIGH    = 0.05   # Trail stop sits 5% below the running high
LADDER_DROP_PCT     = 0.20   # Buy 20 more shares if price drops 20% from fill

# Order ID for the initial market buy placed earlier
INITIAL_ORDER_ID = "b11007b2-3c79-48b6-868d-4a03f77ecb3e"


# ── State Helpers ─────────────────────────────────────────────────────────────
def load_state() -> dict:
    defaults = {
        "fill_price":      None,   # Actual fill price of the entry buy
        "highest_price":   None,   # Highest price seen since fill
        "stop_order_id":   None,   # Active stop-loss order ID
        "stop_price":      None,   # Current stop price
        "trailing_active": False,  # Whether trailing logic has kicked in
        "ladder_fired":    False,  # Whether the live-price ladder buy was triggered
        "ladder_order_id": None,
        "ladder_filled":   False,
        "position_closed": False,  # True once we no longer hold TSLA
    }
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            saved = json.load(f)
        # Migrate old key names if present
        if "ladder_placed" in saved and "ladder_fired" not in saved:
            saved["ladder_fired"] = saved.pop("ladder_placed")
        # Fill in any missing keys with defaults
        for k, v in defaults.items():
            saved.setdefault(k, v)
        return saved
    return defaults


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    log.debug("State saved.")


# ── Alpaca API Helpers ────────────────────────────────────────────────────────
def get_order(order_id: str) -> dict:
    r = requests.get(f"{BASE_URL}/orders/{order_id}", headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()


def get_position(symbol: str):
    r = requests.get(f"{BASE_URL}/positions/{symbol}", headers=HEADERS, timeout=10)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def get_latest_price(symbol: str) -> float:
    r = requests.get(
        f"{DATA_URL}/stocks/{symbol}/trades/latest",
        headers=HEADERS,
        timeout=10,
    )
    r.raise_for_status()
    return float(r.json()["trade"]["p"])


def cancel_order(order_id: str):
    r = requests.delete(f"{BASE_URL}/orders/{order_id}", headers=HEADERS, timeout=10)
    if r.status_code == 204:
        log.info(f"  Cancelled order {order_id}")
    else:
        log.warning(f"  Cancel returned {r.status_code}: {r.text}")


def place_stop_sell(qty: int, stop_price: float) -> dict:
    stop_price = round(stop_price, 2)
    payload = {
        "symbol":        SYMBOL,
        "qty":           str(qty),
        "side":          "sell",
        "type":          "stop",
        "stop_price":    str(stop_price),
        "time_in_force": "gtc",
    }
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json=payload, timeout=10)
    r.raise_for_status()
    order = r.json()
    log.info(
        f"  [STOP SELL] {qty} shares @ stop ${stop_price} | "
        f"ID: {order.get('id')} | Status: {order.get('status')}"
    )
    return order


def update_stop(state: dict, new_stop_price: float, qty: int = None) -> dict:
    """Cancel existing stop and place a new one at new_stop_price.
    Uses qty if provided, otherwise falls back to total_qty in state or INITIAL_QTY."""
    new_stop_price = round(new_stop_price, 2)
    if qty is None:
        qty = state.get("total_qty", INITIAL_QTY)
    if state["stop_order_id"]:
        cancel_order(state["stop_order_id"])
    new_order = place_stop_sell(qty, new_stop_price)
    state["stop_order_id"] = new_order.get("id")
    state["stop_price"]    = new_stop_price
    state["total_qty"]     = qty
    return new_order


def place_ladder_market_buy(qty: int) -> dict:
    """Fire a market buy for the ladder — used when live price breaches -20%."""
    payload = {
        "symbol":        SYMBOL,
        "qty":           str(qty),
        "side":          "buy",
        "type":          "market",
        "time_in_force": "day",
    }
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json=payload, timeout=10)
    r.raise_for_status()
    order = r.json()
    log.info(
        f"  [LADDER BUY] Market buy {qty} shares | "
        f"ID: {order.get('id')} | Status: {order.get('status')}"
    )
    return order


def is_market_open() -> bool:
    r = requests.get(f"{BASE_URL}/clock", headers=HEADERS, timeout=10)
    return r.json().get("is_open", False)


# ── Main Strategy Runner ──────────────────────────────────────────────────────
def run():
    log.info("=" * 65)
    log.info(f"TSLA Strategy Check  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 65)

    state = load_state()

    # ── Already done? ──────────────────────────────────────────────────────────
    if state["position_closed"]:
        log.info("Position is closed. Strategy complete. Nothing to do.")
        return

    # ── Step 1: Wait for entry fill ───────────────────────────────────────────
    if state["fill_price"] is None:
        log.info(f"Checking initial buy order {INITIAL_ORDER_ID} ...")
        order = get_order(INITIAL_ORDER_ID)
        status = order["status"]
        log.info(f"  Status: {status}")

        if status == "filled":
            fill_price = float(order["filled_avg_price"])
            state["fill_price"]    = fill_price
            state["highest_price"] = fill_price
            log.info(f"  Filled at ${fill_price:.2f}")
            save_state(state)
        elif status in ("canceled", "expired", "rejected"):
            log.error(f"  Entry order {status} — strategy aborted.")
            state["position_closed"] = True
            save_state(state)
            return
        else:
            log.info(f"  Not filled yet (market may be closed). Will retry next run.")
            return

    fill_price     = state["fill_price"]
    stop_loss_px   = round(fill_price * (1 - STOP_LOSS_PCT), 2)
    ladder_px      = round(fill_price * (1 - LADDER_DROP_PCT), 2)
    trailing_px    = round(fill_price * (1 + TRAILING_TRIGGER), 2)

    log.info(f"Fill Price       : ${fill_price:.2f}")
    log.info(f"Hard Stop Loss   : ${stop_loss_px:.2f}  (-{STOP_LOSS_PCT*100:.0f}%)")
    log.info(f"Trailing Trigger : ${trailing_px:.2f}  (+{TRAILING_TRIGGER*100:.0f}%)")
    log.info(f"Ladder Buy Level : ${ladder_px:.2f}  (-{LADDER_DROP_PCT*100:.0f}%)")

    # ── Step 2: Place initial stop loss (only once) ───────────────────────────
    if state["stop_order_id"] is None:
        log.info("Placing initial stop-loss order ...")
        order = place_stop_sell(INITIAL_QTY, stop_loss_px)
        state["stop_order_id"] = order.get("id")
        state["stop_price"]    = stop_loss_px
        save_state(state)

    # ── Step 3: Ladder buy is price-monitored (not pre-placed) ───────────────
    log.info(f"Ladder Buy Level : ${ladder_px:.2f}  — monitoring live price each minute")

    # ── Step 4: Confirm we still hold a position ──────────────────────────────
    position = get_position(SYMBOL)
    if position is None:
        log.info("No open TSLA position — likely stopped out. Strategy complete.")
        state["position_closed"] = True
        save_state(state)
        return

    held_qty = float(position.get("qty", 0))
    log.info(f"Position         : {held_qty} shares held")

    # ── Step 5: Get current price ─────────────────────────────────────────────
    current_price  = get_latest_price(SYMBOL)
    highest_price  = state["highest_price"] or fill_price
    current_stop   = state["stop_price"] or stop_loss_px
    pct_from_fill  = (current_price - fill_price) / fill_price * 100
    pct_gain_high  = (highest_price - fill_price) / fill_price * 100

    log.info(f"Current Price    : ${current_price:.2f}  ({pct_from_fill:+.2f}% from fill)")
    log.info(f"Highest Price    : ${highest_price:.2f}  ({pct_gain_high:+.2f}% from fill)")
    log.info(f"Current Stop     : ${current_stop:.2f}")

    # ── Step 6: Update highest price seen ────────────────────────────────────
    if current_price > highest_price:
        highest_price          = current_price
        state["highest_price"] = highest_price
        log.info(f"  New high! Highest price updated to ${highest_price:.2f}")

    # ── Step 7: Trailing stop logic ───────────────────────────────────────────
    gain_from_fill = (highest_price - fill_price) / fill_price

    if gain_from_fill >= TRAILING_TRIGGER:
        # Trailing is live — stop should be 5% below the running high
        ideal_stop = round(highest_price * (1 - TRAIL_BELOW_HIGH), 2)
        state["trailing_active"] = True

        if ideal_stop > current_stop:
            log.info(
                f"  [TRAILING] Raising stop: ${current_stop:.2f} → ${ideal_stop:.2f} "
                f"(high ${highest_price:.2f} - {TRAIL_BELOW_HIGH*100:.0f}%)"
            )
            update_stop(state, ideal_stop)
            save_state(state)
        else:
            log.info(
                f"  [TRAILING] Active. Stop at ${current_stop:.2f} — "
                f"already at or above ideal (${ideal_stop:.2f}). No change."
            )
    else:
        needed = TRAILING_TRIGGER - gain_from_fill
        log.info(
            f"  [TRAILING] Not active yet. Need {needed*100:.1f}% more gain "
            f"(trigger at ${trailing_px:.2f})."
        )
        save_state(state)

    # ── Step 8: Live-price ladder trigger ────────────────────────────────────
    if not state["ladder_fired"]:
        if current_price <= ladder_px:
            log.info(
                f"  [LADDER] Price ${current_price:.2f} hit ladder level ${ladder_px:.2f} "
                f"(-{LADDER_DROP_PCT*100:.0f}%)! Firing market buy for {LADDER_QTY} shares ..."
            )
            order = place_ladder_market_buy(LADDER_QTY)
            state["ladder_order_id"] = order.get("id")
            state["ladder_fired"]    = True
            save_state(state)
        else:
            pct_to_ladder = (current_price - ladder_px) / fill_price * 100
            log.info(
                f"  [LADDER] Not triggered. Price needs to drop {pct_to_ladder:.1f}% more "
                f"to reach ${ladder_px:.2f}."
            )
    elif not state["ladder_filled"] and state["ladder_order_id"]:
        ladder_order = get_order(state["ladder_order_id"])
        if ladder_order["status"] == "filled":
            filled_qty  = float(ladder_order.get("filled_qty", LADDER_QTY))
            filled_px   = ladder_order.get("filled_avg_price", ladder_px)
            total_qty   = int(held_qty)  # position qty already reflects the new shares
            log.info(
                f"  [LADDER] FILLED! {filled_qty:.0f} shares @ ${filled_px} — "
                f"now holding {total_qty} total shares."
            )
            # Cancel existing stop (covers old qty) and re-place for full position
            log.info(
                f"  [LADDER] Upgrading stop-loss to cover all {total_qty} shares "
                f"@ ${current_stop:.2f} ..."
            )
            update_stop(state, current_stop, qty=total_qty)
            state["ladder_filled"] = True
            state["total_qty"]     = total_qty
            save_state(state)

    log.info("Run complete.\n")


if __name__ == "__main__":
    run()
