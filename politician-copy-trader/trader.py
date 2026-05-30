#!/usr/bin/env python3
"""
Alpaca order execution for copy-trading politician stock picks.
Uses notional (dollar-amount) market orders for buys; closes full position for sells.
"""

import json
import logging
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
