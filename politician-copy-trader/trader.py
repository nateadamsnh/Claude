#!/usr/bin/env python3
"""
Alpaca order execution for copy-trading politician stock picks.
Uses notional (dollar-amount) market orders for buys; closes full position for sells.

Protective stop-loss added 2026-09-27: nothing previously managed downside
risk between a copied buy and whatever eventual disclosure told us to sell --
the -10% "loss alert" in main.py only ever sent a notification, it never
closed anything. attach_protective_stop() below places an Alpaca-managed
GTC trailing stop after a buy fills, covering only whole shares (Alpaca's
trailing_stop order type rejects fractional qty -- a $500 notional buy is
almost always fractional, so a small remainder, typically well under $20,
stays uncovered rather than trying to stop a partial share). place_sell_all()
now cancels any resting stop first, so a copied politician sell isn't
blocked by shares the stop already has reserved.
"""

import json
import logging
import math
import requests
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

BASE_DIR    = Path(__file__).parent.parent
CONFIG_FILE = BASE_DIR / "config" / "alpaca_credentials.json"

with open(CONFIG_FILE) as f:
    _creds = json.load(f)

BASE_URL = _creds["endpoint"]
DATA_URL = "https://data.alpaca.markets/v2"
HEADERS  = {
    "APCA-API-KEY-ID":     _creds["api_key"],
    "APCA-API-SECRET-KEY": _creds["api_secret"],
}


# ── Market status ──────────────────────────────────────────────────────────────

def is_market_open() -> bool:
    r = requests.get(f"{BASE_URL}/clock", headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json().get("is_open", False)


def get_next_open() -> str:
    r = requests.get(f"{BASE_URL}/clock", headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json().get("next_open", "unknown")


# ── Account ────────────────────────────────────────────────────────────────────

def get_account() -> dict:
    r = requests.get(f"{BASE_URL}/account", headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()


def get_buying_power() -> float:
    return float(get_account().get("buying_power", 0))


# ── Positions ──────────────────────────────────────────────────────────────────

def get_position(symbol: str) -> Optional[dict]:
    r = requests.get(f"{BASE_URL}/positions/{symbol}", headers=HEADERS, timeout=10)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def get_all_positions() -> list:
    r = requests.get(f"{BASE_URL}/positions", headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()


def get_latest_price(symbol: str) -> Optional[float]:
    try:
        r = requests.get(
            f"{DATA_URL}/stocks/{symbol}/trades/latest",
            headers=HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        return float(r.json()["trade"]["p"])
    except Exception as e:
        log.warning(f"Could not get price for {symbol}: {e}")
        return None


# ── Orders ─────────────────────────────────────────────────────────────────────

def place_buy(symbol: str, notional: float) -> dict:
    """Buy $notional worth of symbol at market price."""
    payload = {
        "symbol":        symbol,
        "notional":      f"{notional:.2f}",
        "side":          "buy",
        "type":          "market",
        "time_in_force": "day",
    }
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json=payload, timeout=15)
    if r.status_code == 422:
        # Alpaca rejects notional for some symbols — fall back to whole shares
        price = get_latest_price(symbol)
        if price and price > 0:
            qty = max(1, int(notional / price))
            return _place_qty_buy(symbol, qty)
        raise RuntimeError(f"Cannot place buy for {symbol}: {r.text}")
    r.raise_for_status()
    order = r.json()
    log.info(
        f"  [BUY ] ${notional:.2f} notional {symbol} | "
        f"ID: {order.get('id')} | Status: {order.get('status')}"
    )
    return order


def _place_qty_buy(symbol: str, qty: int) -> dict:
    payload = {
        "symbol":        symbol,
        "qty":           str(qty),
        "side":          "buy",
        "type":          "market",
        "time_in_force": "day",
    }
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json=payload, timeout=15)
    r.raise_for_status()
    order = r.json()
    log.info(
        f"  [BUY ] {qty} shares {symbol} | "
        f"ID: {order.get('id')} | Status: {order.get('status')}"
    )
    return order


def place_sell_all(symbol: str) -> Optional[dict]:
    """Close the entire position in symbol, if we hold one."""
    position = get_position(symbol)
    if not position:
        log.info(f"  [SELL] No position in {symbol} — skipping.")
        return None

    qty = float(position.get("qty", 0))
    if qty <= 0:
        log.info(f"  [SELL] Zero qty for {symbol} — skipping.")
        return None

    # Release shares our own protective stop has reserved before selling the
    # whole position, or this sell can fail/short against the resting order.
    canceled = cancel_stops(symbol)
    if canceled:
        log.info(f"  [SELL] Canceled {canceled} resting stop order(s) on {symbol} before selling.")

    payload = {
        "symbol":        symbol,
        "qty":           position["qty"],
        "side":          "sell",
        "type":          "market",
        "time_in_force": "day",
    }
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json=payload, timeout=15)
    r.raise_for_status()
    order = r.json()
    log.info(
        f"  [SELL] {qty:.4f} shares {symbol} | "
        f"ID: {order.get('id')} | Status: {order.get('status')}"
    )
    return order


# ── Protective stop-loss ────────────────────────────────────────────────────────
# Added 2026-09-27. See module docstring for why. Coverage is always measured
# from OPEN ORDERS AT THE BROKER, never from state.json -- the same principle
# scripts/dynamic_stop_manager.py used to become self-healing against drift
# (state.json can be stale or wrong; the broker's own open-orders list can't
# be). This means a missed or failed stop placement on one run just gets
# retried as "uncovered" on the next one, with no bookkeeping to get out of
# sync.

TRAIL_PERCENT = 10.0  # matches the pre-existing -10% loss-alert threshold in
                       # main.py's config -- this replaces "notify at -10%"
                       # with "actually exit around -10%"


def get_open_orders(symbol: str) -> list:
    r = requests.get(f"{BASE_URL}/orders", headers=HEADERS,
                      params={"status": "open", "symbols": symbol, "limit": 500}, timeout=10)
    r.raise_for_status()
    return r.json()


def cancel_order(order_id: str):
    r = requests.delete(f"{BASE_URL}/orders/{order_id}", headers=HEADERS, timeout=10)
    if r.status_code not in (200, 204, 404):  # 404 = already gone, nothing to cancel
        r.raise_for_status()


def cancel_stops(symbol: str) -> int:
    """Cancel any open trailing-stop sell orders on symbol. Returns how many."""
    canceled = 0
    for o in get_open_orders(symbol):
        if o.get("side") == "sell" and o.get("type") == "trailing_stop":
            cancel_order(o["id"])
            canceled += 1
    return canceled


def sellable_qty(symbol: str) -> int:
    """Whole shares held minus shares already reserved by open sell orders --
    the most a new stop can cover without double-reserving or risking a
    short. Floors fractional holdings down (see module docstring)."""
    position = get_position(symbol)
    held = float(position["qty"]) if position else 0.0
    if held < 1:
        return 0
    reserved = sum(
        float(o["qty"]) - float(o.get("filled_qty") or 0)
        for o in get_open_orders(symbol) if o.get("side") == "sell"
    )
    return max(0, math.floor(held) - math.ceil(reserved))


def submit_trailing_stop(symbol: str, qty: int) -> dict:
    payload = {
        "symbol":        symbol,
        "qty":           str(qty),
        "side":          "sell",
        "type":          "trailing_stop",
        "trail_percent": str(TRAIL_PERCENT),
        "time_in_force": "gtc",
    }
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json=payload, timeout=15)
    r.raise_for_status()
    order = r.json()
    log.info(
        f"  [STOP] {qty} shares {symbol} trailing {TRAIL_PERCENT}% | "
        f"ID: {order.get('id')} | Status: {order.get('status')}"
    )
    return order


def attach_protective_stops():
    """Ensure every currently-held symbol has a trailing stop covering its
    uncovered whole shares. Safe to call every run: sellable_qty() is 0 (a
    no-op) for anything already fully covered by a resting stop, and this
    covers brand-new fills, a slow-to-fill order that wasn't covered yet on
    a prior run, and multiple buys of the same symbol accumulating shares --
    all without needing to know which run bought what."""
    try:
        positions = get_all_positions()
    except Exception as e:
        log.error(f"  Could not fetch positions for stop-loss pass: {e}")
        return
    for pos in positions:
        symbol = pos["symbol"]
        try:
            qty = sellable_qty(symbol)
            if qty >= 1:
                submit_trailing_stop(symbol, qty)
        except Exception as e:
            log.error(f"  Stop-loss failed for {symbol}: {e}")
