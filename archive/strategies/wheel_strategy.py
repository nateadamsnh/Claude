#!/usr/bin/env python
"""
wheel_strategy.py — Cash-Secured Put (CSP) & Covered Call scanner/executor
=============================================================================
Modes:
  scan     (default) — scan symbols for CSP opportunities, rank by annualized return
  execute  — interactive: show scan results then prompt to place orders
  monitor  — show open options positions and P&L

Usage:
  python wheel_strategy.py [scan|execute|monitor] [SYMBOL [SYMBOL ...]]

Examples:
  python wheel_strategy.py                     # scan default symbols
  python wheel_strategy.py scan QBTS IONQ      # scan specific symbols
  python wheel_strategy.py execute             # interactive order placement
  python wheel_strategy.py monitor             # track open positions

Strategy:
  Phase 1 — Sell Cash-Secured Puts (CSPs):
    * Sell OTM puts, collect premium
    * If not assigned -> keep premium, repeat
    * If assigned -> own 100 shares at effective cost = strike - premium

  Phase 2 — Sell Covered Calls (once 100 shares acquired):
    * Sell OTM calls against held shares
    * If not called away -> keep premium, repeat
    * If called away -> profit on shares + premium collected
"""

import json
import re
import sys
import time
import hmac
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import requests

# Telegram
import sys as _sys
_sys.path.insert(0, str(Path(__file__).parent.parent / "config"))
try:
    from telegram_notifier import send as _tg
except Exception:
    def _tg(text: str): pass

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR   = Path(__file__).parent
CREDS_FILE = BASE_DIR.parent / "config" / "alpaca_credentials.json"
STATE_FILE = BASE_DIR.parent / "config" / "wheel_state.json"
LOG_FILE   = BASE_DIR.parent / "logs" / "wheel_strategy.log"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("wheel")

# ---------------------------------------------------------------------------
# Alpaca credentials
# ---------------------------------------------------------------------------
with open(CREDS_FILE) as _f:
    _creds = json.load(_f)

ALPACA_KEY    = _creds["api_key"]
ALPACA_SECRET = _creds["api_secret"]
_raw_ep       = _creds.get("endpoint", "https://paper-api.alpaca.markets/v2").rstrip("/")
# Strip trailing /v2 — we append it ourselves to avoid double /v2/v2
ALPACA_BASE   = _raw_ep[:-3] if _raw_ep.endswith("/v2") else _raw_ep
DATA_URL      = "https://data.alpaca.markets"

HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type":        "application/json",
}

# ---------------------------------------------------------------------------
# Scan parameters  (edit to taste)
# ---------------------------------------------------------------------------
DEFAULT_SYMBOLS  = ["QBTS", "IONQ", "SOXL", "INTC"]  # symbols to scan

DTE_MIN          = 10      # minimum days to expiry
DTE_MAX          = 60      # maximum days to expiry
MAX_SPREAD_PCT   = 25.0    # max bid-ask spread as % of mid (liquidity filter)
MIN_OTM_PCT      = 3.0     # strike must be >= this % below current price (safety margin)
MAX_CONTRACTS    = 5       # max contracts per position (capital limit)
DEFAULT_CONTRACTS = 1      # default qty in execute mode

# ---------------------------------------------------------------------------
# Alpaca REST helpers
# ---------------------------------------------------------------------------

def _get(url: str, params: dict = None) -> dict | list:
    r = requests.get(url, headers=HEADERS, params=params, timeout=15)
    if r.status_code == 404:
        raise ValueError(f"404 Not Found: {url}")
    r.raise_for_status()
    return r.json()


def _post(url: str, body: dict) -> dict:
    r = requests.post(url, headers=HEADERS, json=body, timeout=15)
    if not r.ok:
        err = {}
        try:
            err = r.json()
        except Exception:
            pass
        raise RuntimeError(
            f"POST {url} -> {r.status_code}: {err.get('message', r.text)}"
        )
    return r.json()


def get_account() -> dict:
    return _get(f"{ALPACA_BASE}/v2/account")


def get_positions() -> list:
    return _get(f"{ALPACA_BASE}/v2/positions")


def get_latest_price(symbol: str) -> float:
    """Best-effort current midpoint price for a stock symbol."""
    # Try quotes first
    try:
        data = _get(f"{DATA_URL}/v2/stocks/{symbol}/quotes/latest", {"feed": "iex"})
        q = data.get("quote", {})
        bp = float(q.get("bp") or 0)
        ap = float(q.get("ap") or 0)
        if bp > 0 and ap > 0:
            return (bp + ap) / 2
    except Exception:
        pass

    # Fallback: latest trade
    try:
        data = _get(f"{DATA_URL}/v2/stocks/{symbol}/trades/latest", {"feed": "iex"})
        return float(data["trade"]["p"])
    except Exception:
        pass

    # Fallback: snapshot
    try:
        data = _get(f"{DATA_URL}/v2/stocks/snapshots", {"symbols": symbol, "feed": "iex"})
        snap = data.get(symbol, {})
        lq = snap.get("latestQuote", {})
        bp = float(lq.get("bp") or 0)
        ap = float(lq.get("ap") or 0)
        if bp > 0 and ap > 0:
            return (bp + ap) / 2
        return float((snap.get("latestTrade") or {}).get("p") or 0)
    except Exception:
        pass

    return 0.0


# ---------------------------------------------------------------------------
# OCC symbol parser
# ---------------------------------------------------------------------------

def parse_occ(symbol: str) -> Optional[dict]:
    """
    Parse OCC options symbol into components.

    Format: UNDERLYING  YYMMDD  C/P  8-digit-strike-in-thousandths
    e.g.  QBTS260618P00027000
          underlying = QBTS
          expiry     = 2026-06-18
          type       = put
          strike     = 27.000
    """
    m = re.match(r'^([A-Z.]+)(\d{6})([CP])(\d{8})$', symbol)
    if not m:
        return None
    underlying, date_str, cp, strike_str = m.groups()
    try:
        exp = datetime.strptime(date_str, "%y%m%d").date()
    except ValueError:
        return None
    strike = int(strike_str) / 1000.0
    dte    = (exp - date.today()).days
    return {
        "symbol":     symbol,
        "underlying": underlying,
        "expiry":     exp,
        "dte":        dte,
        "type":       "put" if cp == "P" else "call",
        "strike":     strike,
    }


def is_option_symbol(symbol: str) -> bool:
    return bool(re.match(r'^[A-Z.]+\d{6}[CP]\d{8}$', symbol))


# ---------------------------------------------------------------------------
# Options chain fetch
# ---------------------------------------------------------------------------

def fetch_put_chain(symbol: str) -> list[dict]:
    """Fetch all put option snapshots for a symbol (handles pagination)."""
    url        = f"{DATA_URL}/v1beta1/options/snapshots/{symbol}"
    results    = []
    page_token = None

    while True:
        params = {"feed": "indicative", "limit": 100, "type": "put"}
        if page_token:
            params["page_token"] = page_token

        try:
            data = _get(url, params)
        except Exception as e:
            log.warning(f"[SCAN] Options snapshot error for {symbol}: {e}")
            break

        for occ_sym, snap in (data.get("snapshots") or {}).items():
            parsed = parse_occ(occ_sym)
            if not parsed:
                continue

            greeks = snap.get("greeks") or {}
            quote  = snap.get("latestQuote") or {}
            trade  = snap.get("latestTrade") or {}

            bid = float(quote.get("bp") or 0)
            ask = float(quote.get("ap") or 0)
            mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else float(trade.get("p") or 0)

            parsed.update({
                "bid":   bid,
                "ask":   ask,
                "mid":   mid,
                "delta": float(greeks.get("delta") or 0),
                "theta": float(greeks.get("theta") or 0),
                "iv":    float(snap.get("impliedVolatility") or 0),
                "oi":    int(snap.get("openInterest") or 0),
            })
            results.append(parsed)

        page_token = data.get("next_page_token")
        if not page_token:
            break

    return results


def fetch_call_chain(symbol: str) -> list[dict]:
    """Fetch all call option snapshots for a symbol (handles pagination)."""
    url        = f"{DATA_URL}/v1beta1/options/snapshots/{symbol}"
    results    = []
    page_token = None

    while True:
        params = {"feed": "indicative", "limit": 100, "type": "call"}
        if page_token:
            params["page_token"] = page_token

        try:
            data = _get(url, params)
        except Exception as e:
            log.warning(f"[SCAN] Call chain error for {symbol}: {e}")
            break

        for occ_sym, snap in (data.get("snapshots") or {}).items():
            parsed = parse_occ(occ_sym)
            if not parsed:
                continue

            greeks = snap.get("greeks") or {}
            quote  = snap.get("latestQuote") or {}
            trade  = snap.get("latestTrade") or {}

            bid = float(quote.get("bp") or 0)
            ask = float(quote.get("ap") or 0)
            mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else float(trade.get("p") or 0)

            parsed.update({
                "bid":   bid,
                "ask":   ask,
                "mid":   mid,
                "delta": float(greeks.get("delta") or 0),
                "theta": float(greeks.get("theta") or 0),
                "iv":    float(snap.get("impliedVolatility") or 0),
                "oi":    int(snap.get("openInterest") or 0),
            })
            results.append(parsed)

        page_token = data.get("next_page_token")
        if not page_token:
            break

    return results


# ---------------------------------------------------------------------------
# Opportunity scoring
# ---------------------------------------------------------------------------

def score_csp_opportunities(
    symbol: str,
    chain: list[dict],
    current_price: float,
    cash_available: float,
) -> list[dict]:
    """Filter and rank CSP opportunities from a put chain."""
    opps = []

    for c in chain:
        dte    = c["dte"]
        strike = c["strike"]
        bid    = c["bid"]
        ask    = c["ask"]
        mid    = c["mid"]

        # DTE window
        if dte < DTE_MIN or dte > DTE_MAX:
            continue
        if mid <= 0 or current_price <= 0:
            continue

        # Must be OTM with safety margin
        if strike >= current_price:
            continue
        otm_pct = (current_price - strike) / current_price * 100
        if otm_pct < MIN_OTM_PCT:
            continue

        # Spread filter
        spread_pct = (ask - bid) / mid * 100 if (bid > 0 and ask > 0) else 999.0
        if spread_pct > MAX_SPREAD_PCT:
            continue

        # Economics
        cash_required = strike * 100          # collateral per contract
        premium_pct   = mid / strike * 100    # premium as % of strike
        ann_return    = premium_pct / dte * 365
        breakeven     = strike - mid

        max_qty = min(MAX_CONTRACTS, int(cash_available / cash_required)) if cash_required > 0 else 0
        if max_qty < 1:
            continue

        opps.append({
            **c,
            "current_price": current_price,
            "otm_pct":       round(otm_pct, 1),
            "spread_pct":    round(spread_pct, 1),
            "premium_pct":   round(premium_pct, 2),
            "ann_return":    round(ann_return, 1),
            "breakeven":     round(breakeven, 3),
            "cash_required": cash_required,
            "max_qty":       max_qty,
        })

    opps.sort(key=lambda x: x["ann_return"], reverse=True)
    return opps


def score_cc_opportunities(
    symbol: str,
    chain: list[dict],
    current_price: float,
    avg_cost: float,
) -> list[dict]:
    """Filter and rank covered-call opportunities from a call chain."""
    opps = []

    for c in chain:
        dte    = c["dte"]
        strike = c["strike"]
        bid    = c["bid"]
        ask    = c["ask"]
        mid    = c["mid"]

        if dte < DTE_MIN or dte > DTE_MAX:
            continue
        if mid <= 0 or current_price <= 0:
            continue

        # Must be OTM (call strike above current price)
        if strike <= current_price:
            continue
        otm_pct = (strike - current_price) / current_price * 100
        if otm_pct < MIN_OTM_PCT:
            continue

        spread_pct = (ask - bid) / mid * 100 if (bid > 0 and ask > 0) else 999.0
        if spread_pct > MAX_SPREAD_PCT:
            continue

        premium_pct   = mid / current_price * 100
        ann_return    = premium_pct / dte * 365
        max_gain      = (strike - avg_cost + mid) * 100  # per contract if called away

        opps.append({
            **c,
            "current_price": current_price,
            "avg_cost":      avg_cost,
            "otm_pct":       round(otm_pct, 1),
            "spread_pct":    round(spread_pct, 1),
            "premium_pct":   round(premium_pct, 2),
            "ann_return":    round(ann_return, 1),
            "max_gain":      round(max_gain, 2),
        })

    opps.sort(key=lambda x: x["ann_return"], reverse=True)
    return opps


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "open_positions":          [],
        "closed_positions":        [],
        "total_premium_collected": 0.0,
    }


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

W = 82  # table width


def hdr(title: str):
    print()
    print("=" * W)
    print(f"  {title}")
    print("=" * W)


def print_account_summary(acct: dict, positions: list):
    equity  = float(acct.get("equity",           0) or 0)
    cash    = float(acct.get("cash",             0) or 0)
    pnl     = float(acct.get("unrealized_pl",    0) or 0)
    lvl     = acct.get("options_approved_level", "?")
    print(f"\n  Account Equity  : ${equity:>12,.2f}")
    print(f"  Cash Available  : ${cash:>12,.2f}")
    print(f"  Unrealized P&L  : ${pnl:>+12,.2f}")
    print(f"  Options Level   : {lvl}")
    print(f"  Positions       : {len(positions)}")


def print_csp_table(opps: list[dict], symbol: str, price: float):
    if not opps:
        print(f"\n  No CSP opportunities found for {symbol} (price=${price:.2f})")
        return
    print(f"\n  {symbol}  —  current price ${price:.2f}  —  top {min(len(opps),20)} CSP opportunities\n")
    print(f"  {'#':>3}  {'Strike':>7}  {'Expiry':>10}  {'DTE':>4}  "
          f"{'Bid':>6}  {'Ask':>6}  {'Mid':>6}  {'Sprd':>5}  "
          f"{'OTM%':>5}  {'Ann%':>6}  {'BE':>7}  {'Cash/Ct':>8}")
    print(f"  {'-'*3}  {'-'*7}  {'-'*10}  {'-'*4}  "
          f"{'-'*6}  {'-'*6}  {'-'*6}  {'-'*5}  "
          f"{'-'*5}  {'-'*6}  {'-'*7}  {'-'*8}")
    for i, o in enumerate(opps[:20], 1):
        print(
            f"  {i:>3}  "
            f"${o['strike']:>6.1f}  "
            f"{o['expiry'].strftime('%Y-%m-%d'):>10}  "
            f"{o['dte']:>4}  "
            f"${o['bid']:>5.2f}  "
            f"${o['ask']:>5.2f}  "
            f"${o['mid']:>5.2f}  "
            f"{o['spread_pct']:>4.0f}%  "
            f"{o['otm_pct']:>4.1f}%  "
            f"{o['ann_return']:>5.0f}%  "
            f"${o['breakeven']:>6.2f}  "
            f"${o['cash_required']:>7,.0f}"
        )


def print_cc_table(opps: list[dict], symbol: str, price: float, avg_cost: float):
    if not opps:
        print(f"\n  No CC opportunities found for {symbol}")
        return
    print(f"\n  {symbol}  —  current ${price:.2f}  avg cost ${avg_cost:.2f}  —  top {min(len(opps),15)} Covered Call opportunities\n")
    print(f"  {'#':>3}  {'Strike':>7}  {'Expiry':>10}  {'DTE':>4}  "
          f"{'Bid':>6}  {'Ask':>6}  {'Mid':>6}  {'Sprd':>5}  "
          f"{'OTM%':>5}  {'Ann%':>6}  {'MaxGain':>8}")
    print(f"  {'-'*3}  {'-'*7}  {'-'*10}  {'-'*4}  "
          f"{'-'*6}  {'-'*6}  {'-'*6}  {'-'*5}  "
          f"{'-'*5}  {'-'*6}  {'-'*8}")
    for i, o in enumerate(opps[:15], 1):
        print(
            f"  {i:>3}  "
            f"${o['strike']:>6.1f}  "
            f"{o['expiry'].strftime('%Y-%m-%d'):>10}  "
            f"{o['dte']:>4}  "
            f"${o['bid']:>5.2f}  "
            f"${o['ask']:>5.2f}  "
            f"${o['mid']:>5.2f}  "
            f"{o['spread_pct']:>4.0f}%  "
            f"{o['otm_pct']:>4.1f}%  "
            f"{o['ann_return']:>5.0f}%  "
            f"${o['max_gain']:>7,.2f}"
        )


# ---------------------------------------------------------------------------
# SCAN mode
# ---------------------------------------------------------------------------

def cmd_scan(symbols: list[str]) -> dict:
    hdr("WHEEL STRATEGY — OPPORTUNITY SCAN")

    try:
        acct      = get_account()
        positions = get_positions()
        cash      = float(acct.get("cash", 0) or 0)
    except Exception as e:
        log.error(f"Failed to fetch account: {e}")
        sys.exit(1)

    print_account_summary(acct, positions)

    # Identify covered-call-eligible positions (100+ whole shares, non-option)
    stock_map = {}  # symbol -> position dict
    for p in positions:
        sym = p.get("symbol", "")
        if is_option_symbol(sym):
            continue
        qty = float(p.get("qty", 0) or 0)
        stock_map[sym] = p

    cc_eligible = {s: p for s, p in stock_map.items() if float(p.get("qty", 0)) >= 100}

    if cc_eligible:
        print(f"\n  COVERED CALL ELIGIBLE (>=100 shares):")
        for sym, p in cc_eligible.items():
            qty = float(p.get("qty", 0))
            avg = float(p.get("avg_entry_price", 0))
            print(f"    {sym}: {qty:.0f} shares @ avg ${avg:.2f}")
    else:
        print(f"\n  No 100-share positions — scanning Cash-Secured Puts (CSPs)")

    all_opps: dict[str, tuple] = {}  # symbol -> (opps_list, price, "csp"/"cc")

    # --- CSP scan ---
    for symbol in symbols:
        print(f"\n  Fetching {symbol} put chain...", end="", flush=True)
        try:
            price = get_latest_price(symbol)
            if price <= 0:
                print(f"  price unavailable")
                continue
        except Exception as e:
            print(f"  price error: {e}")
            continue

        try:
            chain = fetch_put_chain(symbol)
            print(f" {len(chain)} contracts found")
        except Exception as e:
            print(f"  chain error: {e}")
            continue

        opps = score_csp_opportunities(symbol, chain, price, cash)
        all_opps[symbol] = (opps, price, "csp")
        print_csp_table(opps, symbol, price)

    # --- Covered call scan for eligible positions ---
    for symbol, pos in cc_eligible.items():
        if symbol in symbols:
            continue  # already scanned above
        print(f"\n  Fetching {symbol} call chain...", end="", flush=True)
        try:
            price = get_latest_price(symbol)
            if price <= 0:
                print("  price unavailable")
                continue
        except Exception as e:
            print(f"  price error: {e}")
            continue

        try:
            chain = fetch_call_chain(symbol)
            print(f" {len(chain)} contracts found")
        except Exception as e:
            print(f"  chain error: {e}")
            continue

        avg_cost = float(pos.get("avg_entry_price", price))
        opps     = score_cc_opportunities(symbol, chain, price, avg_cost)
        all_opps[symbol] = (opps, price, "cc")
        print_cc_table(opps, symbol, price, avg_cost)

    # Summary
    total_opps = sum(len(v[0]) for v in all_opps.values())
    print(f"\n  Scan complete — {total_opps} opportunities across {len(all_opps)} symbols")
    return all_opps


# ---------------------------------------------------------------------------
# EXECUTE mode
# ---------------------------------------------------------------------------

def cmd_execute(symbols: list[str]):
    all_opps = cmd_scan(symbols)

    if not any(v[0] for v in all_opps.values()):
        print("\n  No opportunities found to execute.")
        return

    hdr("EXECUTE — SELECT CONTRACT")

    # Flatten for numbered selection
    flat: list[tuple[str, dict, str]] = []  # (symbol, opp_dict, strategy_type)
    for sym, (opps, price, strat) in all_opps.items():
        for o in opps[:10]:
            flat.append((sym, o, strat))

    if not flat:
        print("  Nothing to execute.")
        return

    print()
    for i, (sym, o, strat) in enumerate(flat, 1):
        label = "CSP" if strat == "csp" else " CC"
        print(
            f"  [{i:>2}] {label}  {o['symbol']:<26}  "
            f"${o['strike']:.1f} {'PUT' if strat == 'csp' else 'CALL'}  "
            f"{o['expiry'].strftime('%b %d')} ({o['dte']}d)  "
            f"mid=${o['mid']:.2f}  ann={o['ann_return']:.0f}%  "
            + (f"BE=${o['breakeven']:.2f}  max {o['max_qty']}x" if strat == "csp"
               else f"MaxGain=${o['max_gain']:,.0f}")
        )

    print()
    raw = input("  Enter number to trade (or q to quit): ").strip()
    if raw.lower() in ("q", "quit", ""):
        print("  Cancelled.")
        return

    try:
        idx = int(raw) - 1
        if not (0 <= idx < len(flat)):
            raise ValueError
    except ValueError:
        print("  Invalid selection.")
        return

    sym, chosen, strat = flat[idx]
    opt_type = "PUT" if strat == "csp" else "CALL"

    print(f"\n  Selected : {chosen['symbol']}")
    print(f"  Type     : SELL {opt_type}  ({strat.upper()})")
    print(f"  Strike   : ${chosen['strike']:.2f}  |  Expiry: {chosen['expiry']}  |  {chosen['dte']} DTE")
    print(f"  Quote    : Bid ${chosen['bid']:.2f}  Ask ${chosen['ask']:.2f}  Mid ${chosen['mid']:.2f}")
    if strat == "csp":
        print(f"  Collateral per contract: ${chosen['cash_required']:,.0f}")
        max_qty = chosen["max_qty"]
    else:
        max_qty = 1  # covered calls: limited by shares held (assume 1 lot for now)

    qty_raw = input(f"\n  Contracts to sell [1-{max_qty}] (default 1): ").strip()
    try:
        qty = int(qty_raw) if qty_raw else DEFAULT_CONTRACTS
        if qty < 1 or qty > max_qty:
            raise ValueError
    except ValueError:
        print(f"  Invalid quantity. Must be 1-{max_qty}.")
        return

    # Default limit = mid (we receive this as the seller)
    limit_price = round(chosen["mid"], 2)
    lp_raw = input(f"  Limit price (default mid={limit_price:.2f}): ").strip()
    if lp_raw:
        try:
            limit_price = round(float(lp_raw), 2)
        except ValueError:
            print("  Invalid price.")
            return

    total_premium = limit_price * qty * 100
    total_cash    = chosen.get("cash_required", 0) * qty

    print(f"\n  +-{'─'*50}-+")
    print(f"  | ORDER SUMMARY                                      |")
    print(f"  +-{'─'*50}-+")
    print(f"  | Action       : SELL {qty} contract(s)  ({strat.upper()})")
    print(f"  | Contract     : {chosen['symbol']}")
    print(f"  | Limit Price  : ${limit_price:.2f}  per contract (x100)")
    print(f"  | Premium In   : ${total_premium:,.2f}")
    if strat == "csp":
        print(f"  | Cash Tied    : ${total_cash:,.0f}  (collateral if assigned)")
        print(f"  | Breakeven    : ${chosen['strike'] - limit_price:.2f}")
    else:
        print(f"  | Max Gain     : ${chosen.get('max_gain', 0):,.2f}  (if called away)")
    print(f"  +-{'─'*50}-+")

    confirm = input("\n  Confirm order? [y/N]: ").strip().lower()
    if confirm != "y":
        print("  Order cancelled.")
        return

    order_body = {
        "symbol":        chosen["symbol"],
        "qty":           str(qty),
        "side":          "sell",
        "type":          "limit",
        "time_in_force": "day",
        "limit_price":   str(limit_price),
        "order_class":   "simple",
    }

    try:
        order    = _post(f"{ALPACA_BASE}/v2/orders", order_body)
        order_id = order.get("id", "?")
        status   = order.get("status", "?")
        print(f"\n  Order submitted!")
        print(f"    Order ID : {order_id}")
        print(f"    Status   : {status}")
        log.info(
            f"[EXECUTE] SELL {qty}x {chosen['symbol']} @ ${limit_price:.2f} "
            f"| strat={strat} order_id={order_id} status={status}"
        )

        # Persist to state
        state = load_state()
        entry = {
            "order_id":      order_id,
            "symbol":        chosen["symbol"],
            "underlying":    sym,
            "strategy":      strat,
            "contracts":     qty,
            "strike":        chosen["strike"],
            "expiry":        chosen["expiry"].isoformat(),
            "dte_at_entry":  chosen["dte"],
            "premium":       limit_price,
            "total_premium": total_premium,
            "opened_at":     datetime.now().isoformat(),
            "status":        "open",
        }
        if strat == "csp":
            entry["cash_reserved"] = total_cash
            entry["breakeven"]     = round(chosen["strike"] - limit_price, 3)

        state["open_positions"].append(entry)
        state["total_premium_collected"] = (
            float(state.get("total_premium_collected", 0)) + total_premium
        )
        save_state(state)
        print(f"    Saved to  : {STATE_FILE}")
        _tg(
            f"<b>Wheel {strat.upper()} Order Placed: {chosen['symbol']}</b>\n"
            f"SELL {qty}x {opt_type} @ ${limit_price:.2f}  |  DTE: {chosen['dte']}\n"
            f"Premium: ${total_premium:,.2f}"
            + (f"  |  BE: ${chosen['strike'] - limit_price:.2f}" if strat == "csp" else "")
        )

    except Exception as e:
        print(f"\n  Order FAILED: {e}")
        log.error(f"[EXECUTE] Order failed: {e}")


# ---------------------------------------------------------------------------
# MONITOR mode
# ---------------------------------------------------------------------------

def cmd_monitor():
    hdr("WHEEL STRATEGY — POSITION MONITOR")

    try:
        acct      = get_account()
        positions = get_positions()
    except Exception as e:
        log.error(f"Failed to fetch account: {e}")
        sys.exit(1)

    print_account_summary(acct, positions)

    # Split positions
    opt_positions   = [p for p in positions if is_option_symbol(p.get("symbol", ""))]
    stock_positions = [p for p in positions if not is_option_symbol(p.get("symbol", ""))]

    # --- Live options ---
    if opt_positions:
        print(f"\n  LIVE OPTIONS POSITIONS ({len(opt_positions)}):\n")
        print(f"  {'Symbol':<26}  {'Qty':>5}  {'Avg':>7}  {'Curr':>7}  {'P&L':>9}  {'P&L%':>7}")
        print(f"  {'-'*26}  {'-'*5}  {'-'*7}  {'-'*7}  {'-'*9}  {'-'*7}")
        total_opt_pnl = 0.0
        for p in opt_positions:
            sym     = p.get("symbol", "")
            qty     = float(p.get("qty", 0) or 0)
            avg     = float(p.get("avg_entry_price", 0) or 0)
            curr    = float(p.get("current_price", 0) or 0)
            pnl     = float(p.get("unrealized_pl", 0) or 0)
            pnl_pct = float(p.get("unrealized_plpc", 0) or 0) * 100
            total_opt_pnl += pnl
            print(
                f"  {sym:<26}  {qty:>5.0f}  ${avg:>6.2f}  ${curr:>6.2f}  "
                f"${pnl:>+8.2f}  {pnl_pct:>+6.1f}%"
            )
            parsed = parse_occ(sym)
            if parsed:
                print(
                    f"      -> {parsed['underlying']} ${parsed['strike']:.2f} "
                    f"{parsed['type'].upper()}  "
                    f"exp {parsed['expiry'].strftime('%Y-%m-%d')} ({parsed['dte']}d)"
                )
        print(f"\n  Total Options P&L : ${total_opt_pnl:+,.2f}")
    else:
        print("\n  No live options positions on Alpaca.")

    # --- Stock positions ---
    if stock_positions:
        print(f"\n  STOCK POSITIONS ({len(stock_positions)}) — covered-call eligibility:\n")
        print(f"  {'Symbol':<8}  {'Qty':>8}  {'Avg':>8}  {'Curr':>8}  {'P&L':>9}  {'P&L%':>7}  {'CC?':>6}")
        print(f"  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*9}  {'-'*7}  {'-'*6}")
        for p in sorted(stock_positions, key=lambda x: float(x.get("qty", 0) or 0), reverse=True):
            sym     = p.get("symbol", "")
            qty     = float(p.get("qty", 0) or 0)
            avg     = float(p.get("avg_entry_price", 0) or 0)
            curr    = float(p.get("current_price", 0) or 0)
            pnl     = float(p.get("unrealized_pl", 0) or 0)
            pnl_pct = float(p.get("unrealized_plpc", 0) or 0) * 100
            cc_ok   = "YES *" if qty >= 100 else "no"
            print(
                f"  {sym:<8}  {qty:>8.2f}  ${avg:>7.2f}  ${curr:>7.2f}  "
                f"${pnl:>+8.2f}  {pnl_pct:>+6.1f}%  {cc_ok:>6}"
            )

    # --- Expiry alerts ---
    alerts = []
    for p in opt_positions:
        parsed = parse_occ(p.get("symbol", ""))
        if parsed and 0 < parsed["dte"] <= 7:
            qty    = float(p.get("qty", 0) or 0)
            action = "CLOSE or ROLL" if qty < 0 else "EXERCISE CHECK"
            alerts.append(f"  !! {p['symbol']}  expires in {parsed['dte']} day(s) — {action}")
    if alerts:
        print(f"\n  EXPIRY ALERTS:")
        for a in alerts:
            print(a)
        _tg("<b>Wheel Expiry Alerts</b>\n" + "\n".join(a.strip() for a in alerts))

    # --- Wheel state file ---
    state             = load_state()
    tracked           = state.get("open_positions", [])
    total_collected   = float(state.get("total_premium_collected", 0))
    closed            = state.get("closed_positions", [])

    print(f"\n  WHEEL STATE ({STATE_FILE.name}):")
    print(f"  Total premium collected (all-time) : ${total_collected:,.2f}")
    print(f"  Closed positions tracked           : {len(closed)}")

    if tracked:
        print(f"\n  Open tracked positions ({len(tracked)}):")
        for pos in tracked:
            print(
                f"    {pos.get('symbol','?')}  "
                f"{pos.get('strategy','?').upper()}  "
                f"{pos.get('contracts',0)}x  "
                f"opened {str(pos.get('opened_at','?'))[:10]}  "
                f"premium=${pos.get('total_premium',0):.2f}  "
                f"exp={pos.get('expiry','?')}"
            )
    else:
        print("  No positions tracked in state file yet.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    args    = sys.argv[1:]
    mode    = "scan"
    symbols = list(DEFAULT_SYMBOLS)

    if args:
        first = args[0].lower()
        if first in ("scan", "execute", "monitor"):
            mode    = first
            symbols = [s.upper() for s in args[1:]] if args[1:] else DEFAULT_SYMBOLS
        else:
            # No mode keyword — treat all args as symbols
            symbols = [s.upper() for s in args]

    log.info(f"[WHEEL] Starting | mode={mode} symbols={symbols}")

    if mode == "scan":
        cmd_scan(symbols)
    elif mode == "execute":
        cmd_execute(symbols)
    elif mode == "monitor":
        cmd_monitor()
    else:
        print(f"Unknown mode: {mode}. Use: scan | execute | monitor")
        sys.exit(1)

    print()
    log.info("[WHEEL] Done.")


if __name__ == "__main__":
    main()
