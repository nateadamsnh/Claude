#!/usr/bin/env python3
"""
Multi-Symbol Wheel Strategy
============================
Runs the wheel independently on each symbol in SYMBOLS. Shared cash is split
evenly across the basket so no single symbol reserves all the collateral.

Stage 1 - Cash Secured Put (CSP):
  Sell puts ~10% OTM, 14-28 DTE (count scaled by cash budget + regime).
  - Expires worthless  -> sell another CSP (repeat Stage 1)
  - Assigned           -> take shares, move to Stage 2
  - 50% profit hit     -> buy to close early, sell another CSP

Stage 2 - Covered Call (CC):
  Sell one call per 100 shares held, ~10% above cost basis, 14-28 DTE.
  - Expires worthless  -> sell another CC (repeat Stage 2)
  - Called away        -> shares sold at strike, return to Stage 1
  - 50% profit hit     -> buy to close early, sell another CC

Rules:
  - Cash budget per symbol = total cash / number of symbols
  - Never sell more CCs than (shares_held // 100) — never go naked
  - Never sell a CC below cost basis (what you paid for shares)
  - Only trade contracts with a real bid AND spread <= 25% of mid
  - Close early at 50% profit
  - Bear regime pauses new CSPs
  - Do nothing outside market hours

State: Trading/strategies/{symbol}_wheel_state.json (one per symbol)
Log  : Trading/logs/wheel.log (shared)

History: QBTS -> MARA 2026-06-12, then opened to a basket 2026-06-22. The old
single-symbol QBTS history is preserved in qbts_wheel_state.json.

KNOWN API QUIRK (fixed 2026-06-22): the per-contract options snapshot query
(?symbols=<occ>) returns empty quotes on this data tier, and the chain is empty
without feed=indicative. All quote lookups go through get_chain() with the
indicative feed — this was the real cause of the chronic "no real bid" skips.
"""

import json
import re
import sys
import requests
import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path
from regime_detector import get_regime, regime_contracts_multiplier

# ── Strategy Symbols ──────────────────────────────────────────────────────────
# The wheel runs independently on each symbol; every symbol keeps its own state
# file so their CSP/CC stages never interfere. Edit this list to change the
# basket. Shared cash is split evenly across symbols (see run()).
SYMBOLS = ["MARA", "SOFI", "IONQ", "DKNG"]

# SYMBOL and STATE_FILE form the "current symbol" context, reassigned per symbol
# inside run(). The per-symbol functions read these module globals.
SYMBOL = SYMBOLS[0]

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent.parent
CONFIG_FILE = BASE_DIR / "config" / "alpaca_credentials.json"
LOG_FILE    = BASE_DIR / "logs" / "wheel.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

def state_file_for(sym: str) -> Path:
    return BASE_DIR / "strategies" / f"{sym.lower()}_wheel_state.json"

STATE_FILE = state_file_for(SYMBOL)

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
PUT_OTM_PCT     = 0.10          # Sell put 10% below current price
CALL_OTM_PCT    = 0.10          # Sell call 10% above cost basis
DTE_MIN         = 14            # Min days to expiration when entering
DTE_MAX         = 28            # Max days to expiration when entering
EARLY_CLOSE_PCT = 0.50          # Close when 50% of premium is profit
NUM_CONTRACTS   = 5             # 5 contracts = 500 shares of exposure
SUMMARY_TIME    = time(15, 50)  # Start logging daily summary after this time
SHORT_PUT_BP_FACTOR = 2.0       # Options buying power reserved per short put,
                                # as a multiple of nominal collateral (strike*100)

# Per-symbol falling-knife guard. The SPY-based regime detector is blind to a
# basket-specific selloff — a name can crater while SPY holds up (e.g. the
# 2026-06 IONQ/DKNG drop while SPY sat +6% above its 200-day SMA). Before
# selling a NEW CSP, pause the symbol if it has fallen more than
# SYMBOL_PAUSE_DROP_PCT below its recent SYMBOL_PAUSE_LOOKBACK-day high, so the
# wheel doesn't keep selling puts into one name's downdraft. Fails OPEN: if the
# bar data can't be fetched, no pause is applied (a data hiccup never silently
# halts the wheel).
SYMBOL_PAUSE_DROP_PCT = 0.25    # pause new CSPs when >25% below the recent high
SYMBOL_PAUSE_LOOKBACK = 10      # trading days used to find that recent high


# ══════════════════════════════════════════════════════════════════════════════
# State helpers
# ══════════════════════════════════════════════════════════════════════════════

def load_state() -> dict:
    defaults = {
        "stage":             "CSP",  # "CSP" or "CC"
        "active_contract":   None,   # OCC symbol of open short option
        "active_order_id":   None,   # order ID of the sell-to-open
        "active_qty":        0,      # number of contracts in the open position
        "order_filled":      False,  # True once the STO order confirms filled
        "premium_sold":      0.0,    # credit per share received (fill price)
        "cost_basis":        None,   # per-share cost basis (CC stage only)
        "shares_held":       0,      # Underlying shares held (CC stage only)
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
    """Returns {symbol: position_dict} for ALL positions (stocks + options).

    Raises on a failed/malformed fetch instead of returning an empty dict, so
    callers can SKIP the run rather than mistake a transient API error for "you
    hold nothing" — the latter previously triggered false expired/assigned
    transitions (e.g. the SOFI duplicate-CSP incident on 2026-06-23 at the open).
    """
    r = requests.get(f"{BASE_URL}/positions", headers=HEADERS, timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"/positions returned HTTP {r.status_code}: {r.text[:200]}")
    positions = r.json()
    if not isinstance(positions, list):
        raise RuntimeError(f"/positions returned non-list payload: {str(positions)[:200]}")
    return {p["symbol"]: p for p in positions}


def contract_expiration(contract_symbol: str) -> date | None:
    """Parse the expiration date out of an OCC option symbol, e.g.
    'SOFI260710P00015500' -> date(2026, 7, 10). Returns None if unparseable."""
    m = re.match(r"^[A-Z]+(\d{2})(\d{2})(\d{2})[CP]\d{8}$", contract_symbol)
    if not m:
        return None
    yy, mm, dd = (int(g) for g in m.groups())
    try:
        return date(2000 + yy, mm, dd)
    except ValueError:
        return None

def get_latest_stock_price(symbol: str) -> float:
    r = requests.get(f"{DATA_URL}/v2/stocks/{symbol}/trades/latest",
                     headers=HEADERS, timeout=10)
    r.raise_for_status()
    return float(r.json()["trade"]["p"])

def symbol_drawdown(symbol: str, current_price: float,
                    lookback: int = SYMBOL_PAUSE_LOOKBACK) -> dict | None:
    """Drawdown of `symbol` from its `lookback`-day high down to the LIVE price.

    The recent high comes from daily bars, but "current" is the live trade price
    passed in by the caller — the daily-bar close on the IEX feed can lag the
    real price intraday, which would understate a fast drop. Returns
    {high, current, drop_pct} or None when bar data is unavailable; callers must
    treat None as "no pause" (fail open) so a data outage can't silently halt
    new CSP sales.
    """
    end   = date.today()
    start = end - timedelta(days=lookback * 2 + 8)   # calendar pad for weekends/holidays
    r = requests.get(
        f"{DATA_URL}/v2/stocks/{symbol}/bars",
        headers=HEADERS,
        params={
            "timeframe":  "1Day",
            "start":      start.isoformat(),
            "end":        end.isoformat(),
            "limit":      lookback + 8,
            "feed":       "iex",
            "adjustment": "raw",
        },
        timeout=15,
    )
    if r.status_code != 200:
        return None
    bars = r.json().get("bars") or []   # key may be present but null on no data
    if len(bars) < 3:                   # too little history to judge a drawdown
        return None
    recent = bars[-lookback:]
    # Recent high spans the lookback bars AND the live price, so an intraday pop
    # above the bar highs doesn't get mistaken for a drawdown.
    high = max([float(b["h"]) for b in recent] + [current_price])
    if high <= 0:
        return None
    return {"high": high, "current": current_price,
            "drop_pct": (high - current_price) / high}


def get_order(order_id: str) -> dict:
    r = requests.get(f"{BASE_URL}/orders/{order_id}", headers=HEADERS, timeout=10)
    return r.json() if r.status_code == 200 else {}

def get_chain(underlying: str, option_type: str) -> dict:
    """Bulk-fetch every snapshot for one underlying + option type using the
    indicative feed. Returns {occ_symbol: snapshot}.

    The per-contract `?symbols=<occ>` query returns empty quotes (bid/ask None)
    on this data tier — and WITHOUT feed=indicative the chain is empty too.
    Pulling the whole chain with feed=indicative is the only path that yields
    real bids, so all quote lookups go through here.
    """
    out: dict = {}
    token = None
    while True:
        params = {"feed": "indicative", "type": option_type, "limit": 1000}
        if token:
            params["page_token"] = token
        r = requests.get(f"{DATA_URL}/v1beta1/options/snapshots/{underlying}",
                         headers=HEADERS, params=params, timeout=20)
        if r.status_code != 200:
            break
        j = r.json()
        out.update(j.get("snapshots", {}))
        token = j.get("next_page_token")
        if not token:
            break
    return out

def snapshot_for(contract_symbol: str) -> dict:
    """Single-contract snapshot lookup via the bulk chain (parses type from OCC)."""
    m = re.match(r"^([A-Z]+)\d{6}([CP])\d{8}$", contract_symbol)
    if not m:
        return {}
    underlying = m.group(1)
    option_type = "call" if m.group(2) == "C" else "put"
    return get_chain(underlying, option_type).get(contract_symbol, {})


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

def _best_contract(contracts: list, target_strike: float, chain: dict,
                   min_strike: float = None, max_strike: float = None) -> dict | None:
    """Among contracts with a real bid (looked up in the pre-fetched chain),
    return the one closest to target strike. Also enforces the spread filter
    (bid-ask <= 25% of mid) so we never sell into an illiquid book."""
    best = None
    best_dist = float("inf")
    for c in contracts:
        strike = float(c["strike_price"])
        if min_strike is not None and strike < min_strike:
            continue
        if max_strike is not None and strike > max_strike:
            continue
        snap  = chain.get(c["symbol"], {})
        quote = snap.get("latestQuote", {})
        bid   = quote.get("bp") or 0
        ask   = quote.get("ap") or 0
        if bid <= 0 or ask <= 0:
            continue
        mid = (bid + ask) / 2
        if mid <= 0 or (ask - bid) / mid > 0.25:   # spread too wide -> illiquid
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
                "mid":    round(mid, 2),
            }
    return best

def find_put_contract(current_price: float) -> dict | None:
    target = current_price * (1 - PUT_OTM_PCT)
    contracts = _get_contracts("put", strike_gte=target - 5, strike_lte=target + 3)
    if not contracts:
        log.warning(f"  No put contracts returned from API near ${target:.2f}")
        return None
    chain  = get_chain(SYMBOL, "put")
    result = _best_contract(contracts, target, chain)
    if not result:
        log.warning(f"  Put contracts found but none have a liquid bid near ${target:.2f}")
    return result

def find_call_contract(cost_basis: float) -> dict | None:
    target = cost_basis * (1 + CALL_OTM_PCT)
    contracts = _get_contracts("call", strike_gte=target - 3, strike_lte=target + 5)
    if not contracts:
        log.warning(f"  No call contracts returned from API near ${target:.2f}")
        return None
    chain  = get_chain(SYMBOL, "call")
    result = _best_contract(contracts, target, chain, min_strike=cost_basis)
    if not result:
        log.warning(f"  Call contracts found but none are above cost basis ${cost_basis:.2f} with a liquid bid")
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Order placement
# ══════════════════════════════════════════════════════════════════════════════

def sell_to_open(contract: dict, label: str, qty: int = NUM_CONTRACTS) -> dict:
    """Sell `qty` contracts at mid-price limit (credit trade)."""
    payload = {
        "symbol":        contract["symbol"],
        "qty":           str(qty),
        "side":          "sell",
        "type":          "limit",
        "limit_price":   str(contract["mid"]),
        "time_in_force": "day",
    }
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json=payload, timeout=10)
    order = r.json()
    total_credit = contract["mid"] * 100 * qty
    if r.status_code in (200, 201):
        log.info(
            f"  [{label}] {contract['symbol']} x{qty} | "
            f"strike ${contract['strike']:.2f} exp {contract['exp']} "
            f"({(date.fromisoformat(contract['exp'])-date.today()).days} DTE) | "
            f"${contract['mid']:.2f}/sh (${total_credit:.2f} total credit) | "
            f"{order.get('id','')[:8]} | {order.get('status')}"
        )
    else:
        log.error(f"  [{label}] ORDER FAILED {r.status_code}: {r.text[:300]}")
    return order

def buy_to_close(contract_symbol: str, limit_price: float, label: str, qty: int) -> dict:
    """Buy `qty` contracts to close at limit price."""
    payload = {
        "symbol":        contract_symbol,
        "qty":           str(qty),
        "side":          "buy",
        "type":          "limit",
        "limit_price":   str(round(limit_price, 2)),
        "time_in_force": "day",
    }
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json=payload, timeout=10)
    order = r.json()
    if r.status_code in (200, 201):
        log.info(
            f"  [{label}] Closing {contract_symbol} x{qty} @ "
            f"${limit_price:.2f}/sh | {order.get('id','')[:8]} | {order.get('status')}"
        )
    else:
        log.error(f"  [{label}] BTC FAILED {r.status_code}: {r.text[:300]}")
    return order


# ══════════════════════════════════════════════════════════════════════════════
# CSP stage logic
# ══════════════════════════════════════════════════════════════════════════════

def run_csp(state: dict, positions: dict, account: dict, cash_budget: float):
    log.info("  -- Stage 1: Cash Secured Put (CSP) --")

    current_price = get_latest_stock_price(SYMBOL)
    # Cap usable cash to this symbol's share of the budget so one symbol can't
    # consume all collateral before the others get a turn.
    cash          = min(float(account["cash"]), cash_budget)
    log.info(f"  {SYMBOL}: ${current_price:.2f} | Cash budget: ${cash:,.2f} "
             f"(account cash ${float(account['cash']):,.2f})")

    # ── No active contract: check regime then sell new puts ──────────────────
    if not state["active_contract"]:
        # Regime check — pause in bear markets
        regime_data = get_regime(verbose=False)
        regime      = regime_data["regime"]
        contracts_to_sell = regime_contracts_multiplier(NUM_CONTRACTS, regime)

        icon = {"BULL": "🟢", "CAUTION": "🟡", "BEAR": "🔴"}.get(regime, "⚪")
        log.info(f"  {icon} Market regime: {regime} "
                 f"(SPY ${regime_data['spy_price']:.2f} vs SMA200 ${regime_data['sma200']:.2f}, "
                 f"{regime_data['pct_vs_200']:+.1f}%)")

        if contracts_to_sell == 0:
            log.warning(
                f"  BEAR regime detected — pausing CSP sales to protect capital. "
                f"Will resume when SPY recovers above 200-day SMA (${regime_data['sma200']:.2f})."
            )
            return

        if regime == "CAUTION":
            log.info(
                f"  CAUTION regime — reducing from {NUM_CONTRACTS} to "
                f"{contracts_to_sell} contracts."
            )

        # Per-symbol falling-knife guard — the SPY regime check above can't see
        # a name-specific selloff. Fails open: None (no data) => no pause.
        dd = symbol_drawdown(SYMBOL, current_price)
        if dd and dd["drop_pct"] >= SYMBOL_PAUSE_DROP_PCT:
            log.warning(
                f"  {SYMBOL} is {dd['drop_pct']*100:.1f}% below its "
                f"{SYMBOL_PAUSE_LOOKBACK}-day high (${dd['high']:.2f} -> "
                f"${dd['current']:.2f}), past the {SYMBOL_PAUSE_DROP_PCT*100:.0f}% "
                f"pause threshold — skipping new CSP to avoid selling into a "
                f"falling knife. Existing positions are unaffected."
            )
            return
        if dd:
            log.info(
                f"  {SYMBOL} {dd['drop_pct']*100:.1f}% below {SYMBOL_PAUSE_LOOKBACK}-day "
                f"high (threshold {SYMBOL_PAUSE_DROP_PCT*100:.0f}%) — OK to sell."
            )

        target_strike = current_price * (1 - PUT_OTM_PCT)
        # Alpaca reserves roughly SHORT_PUT_BP_FACTOR x nominal collateral in
        # options buying power per short put (empirically ~2x on this margin
        # account), so size against that, not bare strike x 100.
        bp_per_contract = target_strike * 100 * SHORT_PUT_BP_FACTOR
        required_bp     = bp_per_contract * contracts_to_sell
        if cash < required_bp:
            affordable = int(cash // bp_per_contract)
            if affordable < contracts_to_sell:
                log.info(
                    f"  Budget ${cash:,.0f} insufficient for {contracts_to_sell} CSPs "
                    f"(need ${required_bp:,.0f} buying power). Scaling down to "
                    f"{affordable} contract(s)."
                )
                contracts_to_sell = affordable
            # If we can't even afford 1 contract, skip this symbol this run
            if contracts_to_sell < 1:
                log.warning(
                    f"  Insufficient budget ${cash:,.0f} for even 1 CSP "
                    f"(need ${bp_per_contract:,.0f}). Skipping {SYMBOL} this run."
                )
                return

        contract = find_put_contract(current_price)
        if not contract:
            log.warning("  No suitable put found. Will retry next check.")
            return

        log.info(
            f"  Selling {contracts_to_sell} CSPs: strike ${contract['strike']:.2f}, "
            f"exp {contract['exp']}, mid ${contract['mid']:.2f}"
        )
        order = sell_to_open(contract, "CSP-STO", qty=contracts_to_sell)
        if order.get("id"):
            state["active_contract"] = contract["symbol"]
            state["active_order_id"] = order["id"]
            state["active_qty"]      = contracts_to_sell
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
            qty     = state.get("active_qty") or NUM_CONTRACTS
            state["premium_sold"]  = fill_px
            state["order_filled"]  = True
            state["total_premium"] += fill_px * 100 * qty
            state["premium_history"].append({
                "date":     str(date.today()),
                "type":     "CSP",
                "contract": state["active_contract"],
                "premium":  fill_px,
                "total":    round(fill_px * 100 * qty, 2),
            })
            log.info(
                f"  CSP confirmed filled @ ${fill_px:.2f}/sh x{qty} "
                f"(${fill_px*100*qty:.2f} total credit) | "
                f"Cumulative premium: ${state['total_premium']:,.2f}"
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
        stock_pos = positions.get(SYMBOL)
        if stock_pos:
            # ASSIGNED
            shares     = float(stock_pos["qty"])
            cost_basis = float(stock_pos["avg_entry_price"])
            log.info(
                f"  !! CSP ASSIGNED at ${cost_basis:.2f} — "
                f"now holding {shares:.0f} {SYMBOL} shares. Moving to Stage 2 (CC)."
            )
            state["stage"]           = "CC"
            state["cost_basis"]      = cost_basis
            state["shares_held"]     = int(shares)
            state["active_contract"] = None
            state["active_order_id"] = None
            state["active_qty"]      = 0
            state["order_filled"]    = False
            state["cycle_count"]    += 1
        else:
            # Sanity guard: a short put that is no longer in positions but has
            # NOT reached expiration and produced NO shares cannot truly have
            # "expired worthless." This means the positions snapshot was stale
            # or incomplete (seen at the 09:30 open). Hold state and retry rather
            # than book a phantom expiry and sell a duplicate CSP next run.
            exp = contract_expiration(contract_symbol)
            if exp and date.today() < exp:
                log.warning(
                    f"  {contract_symbol} missing from positions but expires {exp} "
                    f"({(exp - date.today()).days} DTE) with no shares held — "
                    f"treating as a stale/incomplete positions response, NOT expiry. "
                    f"Holding state, will recheck next run."
                )
                return
            # EXPIRED WORTHLESS
            total_kept = state["premium_sold"] * 100 * (state.get("active_qty") or NUM_CONTRACTS)
            log.info(
                f"  CSP expired worthless! "
                f"Kept ${state['premium_sold']:.2f}/sh (${total_kept:.2f} total). "
                f"Selling new CSP next check."
            )
            state["active_contract"] = None
            state["active_order_id"] = None
            state["active_qty"]      = 0
            state["order_filled"]    = False
            state["cycle_count"]    += 1
        save_state(state)
        return

    # ── Still open: check for 50% profit ─────────────────────────────────────
    snap  = snapshot_for(contract_symbol)
    quote = snap.get("latestQuote", {})
    bid   = quote.get("bp", 0)
    ask   = quote.get("ap", 0)
    current_mid = round((bid + ask) / 2, 2) if bid > 0 else state["premium_sold"]

    sold       = state["premium_sold"]
    profit_pct = (sold - current_mid) / sold if sold > 0 else 0
    exp_date   = contract_symbol[4:10]
    dte        = (date(2000+int(exp_date[:2]), int(exp_date[2:4]), int(exp_date[4:6])) - date.today()).days

    log.info(
        f"  CSP {contract_symbol} ({dte} DTE) | "
        f"sold ${sold:.2f} | now ${current_mid:.2f} | "
        f"P/L {profit_pct*100:+.1f}%  (target: {EARLY_CLOSE_PCT*100:.0f}%)"
    )

    if profit_pct >= EARLY_CLOSE_PCT:
        log.info(f"  >> {EARLY_CLOSE_PCT*100:.0f}% profit target hit! Buying to close.")
        btc = buy_to_close(contract_symbol, current_mid, "CSP-BTC",
                           qty=state.get("active_qty") or NUM_CONTRACTS)
        if btc.get("id"):
            state["active_contract"] = None
            state["active_order_id"] = None
            state["active_qty"]      = 0
            state["order_filled"]    = False
            save_state(state)
    else:
        log.info(
            f"  Holding CSP — "
            f"need {(EARLY_CLOSE_PCT - profit_pct)*100:.1f}% more to close early"
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
        f"  {SYMBOL}: ${current_price:.2f} | "
        f"Holding {shares_held} shares @ ${cost_basis:.2f} basis | "
        f"P/L: ${(current_price - cost_basis) * shares_held:+,.2f}"
    )

    if SYMBOL not in positions:
        log.info(f"  {SYMBOL} position gone — shares were called away. Returning to Stage 1 (CSP).")
        state["stage"]           = "CSP"
        state["cost_basis"]      = None
        state["shares_held"]     = 0
        state["active_contract"] = None
        state["active_order_id"] = None
        state["order_filled"]    = False
        state["cycle_count"]    += 1
        save_state(state)
        return

    # ── No active contract: sell covered calls ────────────────────────────────
    if not state["active_contract"]:
        contract = find_call_contract(cost_basis)
        if not contract:
            log.warning("  No suitable call found. Will retry next check.")
            return
        if contract["strike"] < cost_basis:
            log.warning(
                f"  Best call ${contract['strike']:.2f} < cost basis ${cost_basis:.2f}. "
                f"Not selling — would lock in a loss."
            )
            return

        # Sell one call per 100 shares actually held — never more (would be naked)
        cc_qty = int(shares_held // 100)
        if cc_qty < 1:
            log.warning(f"  Only {shares_held} shares held — fewer than 100, can't sell a covered call.")
            return

        log.info(
            f"  Selling {cc_qty} CCs: strike ${contract['strike']:.2f} "
            f"(${contract['strike']-cost_basis:+.2f} vs basis), "
            f"exp {contract['exp']}, mid ${contract['mid']:.2f}"
        )
        order = sell_to_open(contract, "CC-STO", qty=cc_qty)
        if order.get("id"):
            state["active_contract"] = contract["symbol"]
            state["active_order_id"] = order["id"]
            state["active_qty"]      = cc_qty
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
            qty     = state.get("active_qty") or NUM_CONTRACTS
            state["premium_sold"]  = fill_px
            state["order_filled"]  = True
            state["total_premium"] += fill_px * 100 * qty
            state["premium_history"].append({
                "date":     str(date.today()),
                "type":     "CC",
                "contract": state["active_contract"],
                "premium":  fill_px,
                "total":    round(fill_px * 100 * qty, 2),
            })
            log.info(
                f"  CC confirmed filled @ ${fill_px:.2f}/sh x{qty} "
                f"(${fill_px*100*qty:.2f} total credit) | "
                f"Cumulative premium: ${state['total_premium']:,.2f}"
            )
            save_state(state)
        elif status in ("canceled", "expired", "rejected"):
            log.warning(f"  CC order {status} — clearing, will retry")
            state["active_contract"] = None
            state["active_order_id"] = None
            save_state(state)
        else:
            log.info(f"  CC sell order status: {status} — waiting for fill")
        return

    # ── Filled: check if still in position ───────────────────────────────────
    contract_symbol = state["active_contract"]
    if contract_symbol not in positions:
        if SYMBOL not in positions:
            # CALLED AWAY
            log.info(
                f"  !! CC CALLED AWAY — {shares_held} shares sold at strike. "
                f"Returning to Stage 1 (CSP)."
            )
            state["stage"]           = "CSP"
            state["cost_basis"]      = None
            state["shares_held"]     = 0
            state["active_contract"] = None
            state["active_order_id"] = None
            state["active_qty"]      = 0
            state["order_filled"]    = False
            state["cycle_count"]    += 1
        else:
            # Sanity guard: shares are still held but the short call vanished
            # before its expiration. A covered call can't expire while you keep
            # the shares — this is a stale/incomplete snapshot, not an expiry.
            exp = contract_expiration(contract_symbol)
            if exp and date.today() < exp:
                log.warning(
                    f"  {contract_symbol} missing from positions but expires {exp} "
                    f"({(exp - date.today()).days} DTE) while shares still held — "
                    f"treating as a stale/incomplete positions response, NOT expiry. "
                    f"Holding state, will recheck next run."
                )
                return
            # EXPIRED WORTHLESS
            total_kept = state["premium_sold"] * 100 * (state.get("active_qty") or NUM_CONTRACTS)
            log.info(
                f"  CC expired worthless! "
                f"Kept ${state['premium_sold']:.2f}/sh (${total_kept:.2f} total). "
                f"Selling new CC next check."
            )
            state["active_contract"] = None
            state["active_order_id"] = None
            state["active_qty"]      = 0
            state["order_filled"]    = False
            state["cycle_count"]    += 1
        save_state(state)
        return

    # ── Still open: check for 50% profit ─────────────────────────────────────
    snap  = snapshot_for(contract_symbol)
    quote = snap.get("latestQuote", {})
    bid   = quote.get("bp", 0)
    ask   = quote.get("ap", 0)
    current_mid = round((bid + ask) / 2, 2) if bid > 0 else state["premium_sold"]

    sold       = state["premium_sold"]
    profit_pct = (sold - current_mid) / sold if sold > 0 else 0
    exp_date   = contract_symbol[4:10]
    dte        = (date(2000+int(exp_date[:2]), int(exp_date[2:4]), int(exp_date[4:6])) - date.today()).days

    log.info(
        f"  CC {contract_symbol} ({dte} DTE) | "
        f"sold ${sold:.2f} | now ${current_mid:.2f} | "
        f"P/L {profit_pct*100:+.1f}%  (target: {EARLY_CLOSE_PCT*100:.0f}%)"
    )

    if profit_pct >= EARLY_CLOSE_PCT:
        log.info(f"  >> {EARLY_CLOSE_PCT*100:.0f}% profit target hit! Buying to close.")
        btc = buy_to_close(contract_symbol, current_mid, "CC-BTC",
                           qty=state.get("active_qty") or NUM_CONTRACTS)
        if btc.get("id"):
            state["active_contract"] = None
            state["active_order_id"] = None
            state["active_qty"]      = 0
            state["order_filled"]    = False
            save_state(state)
    else:
        log.info(
            f"  Holding CC — "
            f"need {(EARLY_CLOSE_PCT - profit_pct)*100:.1f}% more to close early"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Daily summary
# ══════════════════════════════════════════════════════════════════════════════

def log_daily_summary(state: dict, account: dict, positions: dict):
    today_str = str(date.today())
    if state.get("last_summary_date") == today_str:
        return

    port_val = float(account["portfolio_value"])
    equity   = float(account["equity"])
    last_eq  = float(account["last_equity"])
    day_pl   = equity - last_eq
    cash     = float(account["cash"])

    log.info("")
    log.info("=" * 65)
    log.info(f"{SYMBOL} WHEEL STRATEGY -- DAILY CLOSE SUMMARY")
    log.info(f"  {datetime.now().strftime('%Y-%m-%d %H:%M ET')}")
    log.info("=" * 65)
    log.info(f"  Portfolio Value  : ${port_val:>12,.2f}")
    log.info(f"  Day P&L          : ${day_pl:>+12,.2f}")
    log.info(f"  Cash Available   : ${cash:>12,.2f}")
    log.info(f"  Current Stage    : {state['stage']}")
    log.info(f"  Active Contract  : {state['active_contract'] or 'None'}")
    log.info(f"  Order Filled     : {state['order_filled']}")
    log.info(f"  Cycle Count      : {state['cycle_count']}")
    log.info(f"  Total Premium    : ${state['total_premium']:>10,.2f}")

    if state["stage"] == "CC" and state.get("cost_basis"):
        stock_pos  = positions.get(SYMBOL, {})
        curr_price = float(stock_pos.get("current_price", 0))
        unreal_pl  = float(stock_pos.get("unrealized_pl", 0))
        log.info(f"  Cost Basis       : ${state['cost_basis']:.2f}/share")
        log.info(f"  Shares Held      : {state['shares_held']}")
        log.info(f"  {SYMBOL} @ Market : ${curr_price:.2f} | Unrealized P/L: ${unreal_pl:+,.2f}")

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

def process_symbol(cash_budget: float):
    """Run one wheel iteration for the current SYMBOL/STATE_FILE context."""
    state     = load_state()
    account   = get_account()       # re-fetched per symbol so cash reflects
    try:
        positions = get_positions() # collateral already reserved by earlier symbols
    except Exception as e:
        log.error(
            f"  {SYMBOL}: positions fetch failed ({e}) — skipping this run to "
            f"avoid acting on incomplete data (no false expiry/assignment)."
        )
        return

    try:
        if state["stage"] == "CSP":
            run_csp(state, positions, account, cash_budget)
        elif state["stage"] == "CC":
            run_cc(state, positions, account)
        else:
            log.error(f"Unknown stage: {state['stage']}")
    except Exception as e:
        log.error(f"  {SYMBOL} strategy error: {e}", exc_info=True)

    if datetime.now().time() >= SUMMARY_TIME:
        state = load_state()
        log_daily_summary(state, account, positions)


def run():
    global SYMBOL, STATE_FILE
    log.info("=" * 65)
    log.info(f"WHEEL STRATEGY ({', '.join(SYMBOLS)})  |  "
             f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 65)

    if not market_is_open():
        log.info("Market is closed. No action taken.")
        return

    # Budget each symbol a fair share of the LIVE options buying power — not
    # cash. Alpaca reserves options_buying_power (~half of cash here) as each
    # CSP is placed, even before fill, so we re-read it per symbol and divide by
    # the symbols still to go. This self-corrects as capital binds/frees.
    for i, sym in enumerate(SYMBOLS):
        SYMBOL     = sym
        STATE_FILE = state_file_for(sym)
        log.info(f"\n--- {sym} ---")
        try:
            acct = get_account()
            obp  = float(acct.get("options_buying_power") or acct.get("cash") or 0)
        except Exception as e:
            log.error(f"  {sym}: could not fetch account: {e}")
            continue
        remaining = len(SYMBOLS) - i
        budget    = obp / remaining if remaining else 0
        log.info(f"  Options buying power ${obp:,.2f} / {remaining} left "
                 f"= ${budget:,.2f} budget")
        try:
            process_symbol(budget)
        except Exception as e:
            log.error(f"  {sym} failed: {e}", exc_info=True)

    log.info("Run complete.\n")


if __name__ == "__main__":
    run()
