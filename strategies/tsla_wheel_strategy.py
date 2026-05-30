#!/usr/bin/env python3
"""
TSLA Wheel Strategy
====================
Stage 1 - Cash Secured Put (CSP):
  Sell 1 put ~10% OTM, 14-28 DTE. Collect premium.
  - Expires worthless  -> sell another CSP (repeat Stage 1)
  - Assigned           -> take 100 shares, move to Stage 2
  - 50% profit hit     -> buy to close early, sell another CSP

Stage 2 - Covered Call (CC):
  Sell 1 call ~10% above cost basis, 14-28 DTE. Collect premium.
  - Expires worthless  -> sell another CC (repeat Stage 2)
  - Called away        -> shares sold at strike, return to Stage 1
  - 50% profit hit     -> buy to close early, sell another CC

Rules:
  - Never sell a CSP unless cash >= strike * 100
  - Never sell a CC below cost basis (what you actually paid for shares)
  - Close early at 50% profit (buy back at <= 50% of credit received)
  - Check every 15 min during market hours (09:30-16:00 ET)
  - Daily summary logged near market close (~15:50)
  - Do nothing outside market hours

State: Trading/strategies/tsla_wheel_state.json
Log  : Trading/logs/tsla_wheel.log
"""

import json
import sys
import requests
import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent.parent
CONFIG_FILE = BASE_DIR / "config" / "alpaca_credentials.json"
STATE_FILE  = BASE_DIR / "strategies" / "tsla_wheel_state.json"
LOG_FILE    = BASE_DIR / "logs" / "tsla_wheel.log"
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

BASE_URL = creds["endpoint"]   # https://paper-api.alpaca.markets/v2
HEADERS  = {
    "APCA-API-KEY-ID":     creds["api_key"],
    "APCA-API-SECRET-KEY": creds["api_secret"],
}
DATA_URL = "https://data.alpaca.markets"

# ── Strategy Parameters ────────────────────────────────────────────────────────
SYMBOL          = "TSLA"
PUT_OTM_PCT     = 0.10          # Sell put 10% below current price
CALL_OTM_PCT    = 0.10          # Sell call 10% above cost basis
DTE_MIN         = 14            # Min days to expiration when entering
DTE_MAX         = 28            # Max days to expiration when entering
EARLY_CLOSE_PCT = 0.50          # Close position once 50% of premium is profit
NUM_CONTRACTS   = 1             # Contracts per trade (1 = 100 shares)
SUMMARY_TIME    = time(15, 50)  # Start logging daily summary after this time


# ══════════════════════════════════════════════════════════════════════════════
# State helpers
# ══════════════════════════════════════════════════════════════════════════════

def load_state() -> dict:
    defaults = {
        "stage":             "CSP",  # "CSP" or "CC"
        "active_contract":   None,   # OCC symbol of open short option
        "active_order_id":   None,   # order ID of the sell-to-open
        "order_filled":      False,  # True once the STO order confirms filled
        "premium_sold":      0.0,    # credit per share received (fill price)
        "cost_basis":        None,   # per-share cost basis (CC stage only)
        "shares_held":       0,      # TSLA shares held (CC stage only)
        "total_premium":     0.0,    # cumulative premium collected all time
        "cycle_count":       0,      # completed CSP->CC->CSP cycles
        "premium_history":   [],     # list of {date, type, contract, premium, total}
        "last_summary_date": None,   # prevent duplicate daily summaries
    }
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            saved = json.load(f)
        for k, v in defaults.items():
            saved.setdefault(k, v)
        return saved
    return defaults

def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ══════════════════════════════════════════════════════════════════════════════
# Alpaca helpers
# ══════════════════════════════════════════════════════════════════════════════

def market_is_open() -> bool:
    r = requests.get(f"{BASE_URL}/clock", headers=HEADERS, timeout=10)
    return r.json().get("is_open", False)

def get_account() -> dict:
    return requests.get(f"{BASE_URL}/account", headers=HEADERS, timeout=10).json()

def get_positions() -> dict:
    """Returns {symbol: position_dict} for ALL positions (stocks + options)."""
    r = requests.get(f"{BASE_URL}/positions", headers=HEADERS, timeout=10)
    positions = r.json() if r.status_code == 200 else []
    if not isinstance(positions, list):
        return {}
    return {p["symbol"]: p for p in positions}

def get_latest_stock_price(symbol: str) -> float:
    r = requests.get(f"{DATA_URL}/v2/stocks/{symbol}/trades/latest",
                     headers=HEADERS, timeout=10)
    r.raise_for_status()
    return float(r.json()["trade"]["p"])

def get_order(order_id: str) -> dict:
    r = requests.get(f"{BASE_URL}/orders/{order_id}", headers=HEADERS, timeout=10)
    return r.json() if r.status_code == 200 else {}

def get_option_snapshot(contract_symbol: str) -> dict:
    """Get bid/ask/Greeks snapshot for one options contract."""
    r = requests.get(
        f"{DATA_URL}/v1beta1/options/snapshots/{SYMBOL}",
        headers=HEADERS,
        params={"symbols": contract_symbol},
        timeout=10,
    )
    if r.status_code == 200:
        return r.json().get("snapshots", {}).get(contract_symbol, {})
    return {}


# ══════════════════════════════════════════════════════════════════════════════
# Contract selection
# ══════════════════════════════════════════════════════════════════════════════

def _get_contracts(option_type: str, strike_gte: float, strike_lte: float) -> list:
    today   = date.today()
    exp_min = (today + timedelta(days=DTE_MIN)).strftime("%Y-%m-%d")
    exp_max = (today + timedelta(days=DTE_MAX)).strftime("%Y-%m-%d")
    r = requests.get(
        f"{BASE_URL}/options/contracts",
        headers=HEADERS,
        params={
            "underlying_symbol":   SYMBOL,
            "type":                option_type,
            "strike_price_gte":    str(round(strike_gte, 2)),
            "strike_price_lte":    str(round(strike_lte, 2)),
            "expiration_date_gte": exp_min,
            "expiration_date_lte": exp_max,
            "limit": 50,
        },
        timeout=10,
    )
    return r.json().get("option_contracts", []) if r.status_code == 200 else []

def _best_contract(contracts: list, target_strike: float,
                   min_strike: float = None, max_strike: float = None) -> dict | None:
    """Among contracts with a real bid, return the one with strike closest to target."""
    best = None
    best_dist = float("inf")
    for c in contracts:
        strike = float(c["strike_price"])
        if min_strike is not None and strike < min_strike:
            continue
        if max_strike is not None and strike > max_strike:
            continue
        snap  = get_option_snapshot(c["symbol"])
        quote = snap.get("latestQuote", {})
        bid   = quote.get("bp", 0)
        ask   = quote.get("ap", 0)
        if bid <= 0:
            continue
        dist = abs(strike - target_strike)
        if dist < best_dist:
            best_dist = dist
            best = {
                "symbol": c["symbol"],
                "strike": strike,
                "exp":    c["expiration_date"],
                "bid":    bid,
                "ask":    ask,
                "mid":    round((bid + ask) / 2, 2),
            }
    return best

def find_put_contract(current_price: float) -> dict | None:
    """Find the put closest to 10% OTM with a real bid, 14-28 DTE."""
    target = current_price * (1 - PUT_OTM_PCT)
    contracts = _get_contracts("put",
                               strike_gte=target - 30,
                               strike_lte=target + 10)
    if not contracts:
        log.warning(f"  No put contracts returned from API near ${target:.0f}")
        return None
    result = _best_contract(contracts, target)
    if not result:
        log.warning(f"  Put contracts found but none have a real bid near ${target:.0f}")
    return result

def find_call_contract(cost_basis: float) -> dict | None:
    """Find the call closest to 10% above cost basis with a real bid, 14-28 DTE.
    Never returns a strike below cost_basis."""
    target = cost_basis * (1 + CALL_OTM_PCT)
    contracts = _get_contracts("call",
                               strike_gte=target - 10,
                               strike_lte=target + 30)
    if not contracts:
        log.warning(f"  No call contracts returned from API near ${target:.0f}")
        return None
    result = _best_contract(contracts, target, min_strike=cost_basis)
    if not result:
        log.warning(f"  Call contracts found but none are above cost basis ${cost_basis:.2f} with a real bid")
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Order placement
# ══════════════════════════════════════════════════════════════════════════════

def sell_to_open(contract: dict, label: str) -> dict:
    """Sell 1 contract at mid-price limit (credit trade)."""
    payload = {
        "symbol":        contract["symbol"],
        "qty":           str(NUM_CONTRACTS),
        "side":          "sell",
        "type":          "limit",
        "limit_price":   str(contract["mid"]),
        "time_in_force": "day",
    }
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json=payload, timeout=10)
    order = r.json()
    if r.status_code in (200, 201):
        log.info(
            f"  [{label}] {contract['symbol']} | strike ${contract['strike']:.2f} "
            f"exp {contract['exp']} | credit ${contract['mid']:.2f}/sh "
            f"(${contract['mid']*100:.2f} total) | {order.get('id','')[:8]} | {order.get('status')}"
        )
    else:
        log.error(f"  [{label}] ORDER FAILED {r.status_code}: {r.text[:300]}")
    return order

def buy_to_close(contract_symbol: str, limit_price: float, label: str) -> dict:
    """Buy 1 contract to close at limit price."""
    payload = {
        "symbol":        contract_symbol,
        "qty":           str(NUM_CONTRACTS),
        "side":          "buy",
        "type":          "limit",
        "limit_price":   str(round(limit_price, 2)),
        "time_in_force": "day",
    }
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json=payload, timeout=10)
    order = r.json()
    if r.status_code in (200, 201):
        log.info(
            f"  [{label}] Closing {contract_symbol} @ ${limit_price:.2f} | "
            f"{order.get('id','')[:8]} | {order.get('status')}"
        )
    else:
        log.error(f"  [{label}] BTC FAILED {r.status_code}: {r.text[:300]}")
    return order


# ══════════════════════════════════════════════════════════════════════════════
# CSP stage logic
# ══════════════════════════════════════════════════════════════════════════════

def run_csp(state: dict, positions: dict, account: dict):
    log.info("  -- Stage 1: Cash Secured Put (CSP) --")

    current_price = get_latest_stock_price(SYMBOL)
    cash          = float(account["cash"])
    log.info(f"  TSLA: ${current_price:.2f} | Cash: ${cash:,.2f}")

    # ── No active contract: sell a new put ────────────────────────────────────
    if not state["active_contract"]:
        target_strike  = current_price * (1 - PUT_OTM_PCT)
        required_cash  = target_strike * 100 * NUM_CONTRACTS
        if cash < required_cash:
            log.warning(
                f"  Insufficient cash ${cash:,.0f} for CSP "
                f"(need ${required_cash:,.0f} for strike ~${target_strike:.0f}). Skipping."
            )
            return

        contract = find_put_contract(current_price)
        if not contract:
            log.warning("  No suitable put found. Will retry next check.")
            return

        log.info(
            f"  Selling CSP: strike ${contract['strike']:.2f}, "
            f"exp {contract['exp']} ({(date.fromisoformat(contract['exp'])-date.today()).days} DTE), "
            f"mid ${contract['mid']:.2f}"
        )
        order = sell_to_open(contract, "CSP-STO")
        if order.get("id"):
            state["active_contract"] = contract["symbol"]
            state["active_order_id"] = order["id"]
            state["order_filled"]    = False
            state["premium_sold"]    = contract["mid"]   # provisional; updated on fill confirm
            save_state(state)
        return

    # ── Pending: wait for STO fill ────────────────────────────────────────────
    if not state["order_filled"]:
        order  = get_order(state["active_order_id"])
        status = order.get("status", "unknown")
        if status == "filled":
            fill_px = float(order.get("filled_avg_price") or state["premium_sold"])
            state["premium_sold"]  = fill_px
            state["order_filled"]  = True
            state["total_premium"] += fill_px * 100 * NUM_CONTRACTS
            state["premium_history"].append({
                "date":     str(date.today()),
                "type":     "CSP",
                "contract": state["active_contract"],
                "premium":  fill_px,
                "total":    round(fill_px * 100, 2),
            })
            log.info(
                f"  CSP confirmed filled @ ${fill_px:.2f}/sh "
                f"(${fill_px*100:.2f} credit) | "
                f"Total premium collected: ${state['total_premium']:,.2f}"
            )
            save_state(state)
        elif status in ("canceled", "expired", "rejected"):
            log.warning(f"  CSP order {status} — clearing, will place fresh order next run")
            state["active_contract"] = None
            state["active_order_id"] = None
            save_state(state)
        else:
            log.info(f"  CSP sell order status: {status} — waiting for fill")
        return

    # ── Filled: check if still in position ───────────────────────────────────
    contract_symbol = state["active_contract"]
    if contract_symbol not in positions:
        # Contract gone — expired or assigned
        tsla_pos = positions.get(SYMBOL)
        if tsla_pos:
            # ASSIGNED: we now own shares
            shares     = float(tsla_pos["qty"])
            cost_basis = float(tsla_pos["avg_entry_price"])
            log.info(
                f"  !! CSP ASSIGNED at ${cost_basis:.2f} — "
                f"now holding {shares:.0f} TSLA shares. Moving to Stage 2 (CC)."
            )
            state["stage"]           = "CC"
            state["cost_basis"]      = cost_basis
            state["shares_held"]     = int(shares)
            state["active_contract"] = None
            state["active_order_id"] = None
            state["order_filled"]    = False
            state["cycle_count"]    += 1
        else:
            # EXPIRED WORTHLESS
            log.info(
                f"  CSP expired worthless! "
                f"Kept ${state['premium_sold']:.2f}/sh (${state['premium_sold']*100:.2f}). "
                f"Selling new CSP next check."
            )
            state["active_contract"] = None
            state["active_order_id"] = None
            state["order_filled"]    = False
            state["cycle_count"]    += 1
        save_state(state)
        return

    # ── Still open: monitor for 50% profit ───────────────────────────────────
    snap  = get_option_snapshot(contract_symbol)
    quote = snap.get("latestQuote", {})
    bid   = quote.get("bp", 0)
    ask   = quote.get("ap", 0)
    if bid > 0:
        current_mid = round((bid + ask) / 2, 2)
    else:
        current_mid = state["premium_sold"]  # no quote — assume unchanged

    sold        = state["premium_sold"]
    profit_pct  = (sold - current_mid) / sold if sold > 0 else 0

    log.info(
        f"  CSP {contract_symbol} | "
        f"sold ${sold:.2f} | now ${current_mid:.2f} | "
        f"P/L {profit_pct*100:+.1f}%  (target: {EARLY_CLOSE_PCT*100:.0f}% profit)"
    )

    if profit_pct >= EARLY_CLOSE_PCT:
        log.info(f"  >> {EARLY_CLOSE_PCT*100:.0f}% profit target hit! Buying to close.")
        btc = buy_to_close(contract_symbol, current_mid, "CSP-BTC")
        if btc.get("id"):
            # Clear contract — will sell a fresh CSP next check
            state["active_contract"] = None
            state["active_order_id"] = None
            state["order_filled"]    = False
            save_state(state)
    else:
        log.info(
            f"  Holding CSP — "
            f"need {(EARLY_CLOSE_PCT - profit_pct)*100:.1f}% more gain to close early"
        )


# ══════════════════════════════════════════════════════════════════════════════
# CC stage logic
# ══════════════════════════════════════════════════════════════════════════════

def run_cc(state: dict, positions: dict, account: dict):
    log.info("  -- Stage 2: Covered Call (CC) --")

    current_price = get_latest_stock_price(SYMBOL)
    cost_basis    = state["cost_basis"]
    shares_held   = state["shares_held"]
    log.info(
        f"  TSLA: ${current_price:.2f} | "
        f"Holding {shares_held} shares @ ${cost_basis:.2f} basis | "
        f"P/L: ${(current_price - cost_basis) * shares_held:+,.2f}"
    )

    # Verify shares still held
    if SYMBOL not in positions:
        log.info("  TSLA position gone — shares were called away. Returning to Stage 1 (CSP).")
        state["stage"]           = "CSP"
        state["cost_basis"]      = None
        state["shares_held"]     = 0
        state["active_contract"] = None
        state["active_order_id"] = None
        state["order_filled"]    = False
        state["cycle_count"]    += 1
        save_state(state)
        return

    # ── No active contract: sell a new covered call ───────────────────────────
    if not state["active_contract"]:
        contract = find_call_contract(cost_basis)
        if not contract:
            log.warning("  No suitable call found. Will retry next check.")
            return
        if contract["strike"] < cost_basis:
            log.warning(
                f"  Best available call strike ${contract['strike']:.2f} < "
                f"cost basis ${cost_basis:.2f}. Not selling — would lock in a loss."
            )
            return

        log.info(
            f"  Selling CC: strike ${contract['strike']:.2f} "
            f"(${contract['strike'] - cost_basis:+.2f} vs basis), "
            f"exp {contract['exp']} ({(date.fromisoformat(contract['exp'])-date.today()).days} DTE), "
            f"mid ${contract['mid']:.2f}"
        )
        order = sell_to_open(contract, "CC-STO")
        if order.get("id"):
            state["active_contract"] = contract["symbol"]
            state["active_order_id"] = order["id"]
            state["order_filled"]    = False
            state["premium_sold"]    = contract["mid"]
            save_state(state)
        return

    # ── Pending: wait for STO fill ────────────────────────────────────────────
    if not state["order_filled"]:
        order  = get_order(state["active_order_id"])
        status = order.get("status", "unknown")
        if status == "filled":
            fill_px = float(order.get("filled_avg_price") or state["premium_sold"])
            state["premium_sold"]  = fill_px
            state["order_filled"]  = True
            state["total_premium"] += fill_px * 100 * NUM_CONTRACTS
            state["premium_history"].append({
                "date":     str(date.today()),
                "type":     "CC",
                "contract": state["active_contract"],
                "premium":  fill_px,
                "total":    round(fill_px * 100, 2),
            })
            log.info(
                f"  CC confirmed filled @ ${fill_px:.2f}/sh "
                f"(${fill_px*100:.2f} credit) | "
                f"Total premium collected: ${state['total_premium']:,.2f}"
            )
            save_state(state)
        elif status in ("canceled", "expired", "rejected"):
            log.warning(f"  CC order {status} — clearing, will place fresh order next run")
            state["active_contract"] = None
            state["active_order_id"] = None
            save_state(state)
        else:
            log.info(f"  CC sell order status: {status} — waiting for fill")
        return

    # ── Filled: check if still in position ───────────────────────────────────
    contract_symbol = state["active_contract"]
    if contract_symbol not in positions:
        # Contract gone
        if SYMBOL not in positions:
            # CALLED AWAY: shares + contract both gone
            log.info(
                f"  !! CC CALLED AWAY — shares sold at ${contract_symbol} strike. "
                f"Returning to Stage 1 (CSP)."
            )
            state["stage"]           = "CSP"
            state["cost_basis"]      = None
            state["shares_held"]     = 0
            state["active_contract"] = None
            state["active_order_id"] = None
            state["order_filled"]    = False
            state["cycle_count"]    += 1
        else:
            # EXPIRED WORTHLESS
            log.info(
                f"  CC expired worthless! "
                f"Kept ${state['premium_sold']:.2f}/sh (${state['premium_sold']*100:.2f}). "
                f"Selling new CC next check."
            )
            state["active_contract"] = None
            state["active_order_id"] = None
            state["order_filled"]    = False
            state["cycle_count"]    += 1
        save_state(state)
        return

    # ── Still open: monitor for 50% profit ───────────────────────────────────
    snap  = get_option_snapshot(contract_symbol)
    quote = snap.get("latestQuote", {})
    bid   = quote.get("bp", 0)
    ask   = quote.get("ap", 0)
    current_mid = round((bid + ask) / 2, 2) if bid > 0 else state["premium_sold"]

    sold       = state["premium_sold"]
    profit_pct = (sold - current_mid) / sold if sold > 0 else 0

    log.info(
        f"  CC {contract_symbol} | "
        f"sold ${sold:.2f} | now ${current_mid:.2f} | "
        f"P/L {profit_pct*100:+.1f}%  (target: {EARLY_CLOSE_PCT*100:.0f}% profit)"
    )

    if profit_pct >= EARLY_CLOSE_PCT:
        log.info(f"  >> {EARLY_CLOSE_PCT*100:.0f}% profit target hit! Buying to close.")
        btc = buy_to_close(contract_symbol, current_mid, "CC-BTC")
        if btc.get("id"):
            state["active_contract"] = None
            state["active_order_id"] = None
            state["order_filled"]    = False
            save_state(state)
    else:
        log.info(
            f"  Holding CC — "
            f"need {(EARLY_CLOSE_PCT - profit_pct)*100:.1f}% more gain to close early"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Daily summary
# ══════════════════════════════════════════════════════════════════════════════

def log_daily_summary(state: dict, account: dict, positions: dict):
    today_str = str(date.today())
    if state.get("last_summary_date") == today_str:
        return  # already logged today

    port_val   = float(account["portfolio_value"])
    equity     = float(account["equity"])
    last_eq    = float(account["last_equity"])
    day_pl     = equity - last_eq
    cash       = float(account["cash"])

    log.info("")
    log.info("=" * 65)
    log.info("TSLA WHEEL STRATEGY -- DAILY CLOSE SUMMARY")
    log.info(f"  {datetime.now().strftime('%Y-%m-%d %H:%M ET')}")
    log.info("=" * 65)
    log.info(f"  Portfolio Value  : ${port_val:>12,.2f}")
    log.info(f"  Day P&L          : ${day_pl:>+12,.2f}")
    log.info(f"  Cash Available   : ${cash:>12,.2f}")
    log.info(f"  Current Stage    : {state['stage']}")
    log.info(f"  Active Contract  : {state['active_contract'] or 'None (will open next session)'}")
    log.info(f"  Order Filled     : {state['order_filled']}")
    log.info(f"  Cycle Count      : {state['cycle_count']}")
    log.info(f"  Total Premium    : ${state['total_premium']:>10,.2f}")

    if state["stage"] == "CC" and state.get("cost_basis"):
        tsla_pos   = positions.get(SYMBOL, {})
        curr_price = float(tsla_pos.get("current_price", 0))
        unreal_pl  = float(tsla_pos.get("unrealized_pl", 0))
        log.info(f"  Cost Basis       : ${state['cost_basis']:.2f}/share")
        log.info(f"  Shares Held      : {state['shares_held']}")
        log.info(f"  TSLA @ Market    : ${curr_price:.2f} | Unrealized P/L: ${unreal_pl:+,.2f}")

    if state["premium_history"]:
        log.info("")
        log.info("  Premium History:")
        for h in state["premium_history"][-10:]:
            log.info(
                f"    {h['date']} | {h['type']:3s} | "
                f"{h['contract']:32s} | "
                f"${h['premium']:.2f}/sh | ${h['total']:.2f} total"
            )

    log.info("=" * 65)

    state["last_summary_date"] = today_str
    save_state(state)


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def run():
    log.info("=" * 65)
    log.info(f"TSLA Wheel Strategy  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 65)

    if not market_is_open():
        log.info("Market is closed. No action taken.")
        return

    state     = load_state()
    account   = get_account()
    positions = get_positions()

    try:
        if state["stage"] == "CSP":
            run_csp(state, positions, account)
        elif state["stage"] == "CC":
            run_cc(state, positions, account)
        else:
            log.error(f"Unknown stage: {state['stage']}")
    except Exception as e:
        log.error(f"Strategy error: {e}", exc_info=True)

    # Daily summary near market close
    if datetime.now().time() >= SUMMARY_TIME:
        state = load_state()  # reload in case it was updated above
        log_daily_summary(state, account, positions)

    log.info("Run complete.\n")


if __name__ == "__main__":
    run()
