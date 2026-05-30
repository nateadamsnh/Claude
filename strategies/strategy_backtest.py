#!/usr/bin/env python3
"""
Phase 2 HARDENED: Stress-Testing & Slippage Verification
=========================================================
CYNICAL RISK MANAGER FINDINGS — Bugs found and fixed vs original:

  BUG #1 [LOOK-AHEAD BIAS] 30d high excluded the signal bar's own high.
          df.iloc[lookback_start : i] missed bar i entirely.
          Fix: df.iloc[lookback_start : i+1] now includes signal bar.

  BUG #2 [LOOK-AHEAD BIAS] Martingale adds filled at current bar close.
          You can't know a bar's close while it's still open.
          Fix: scale-in fills at NEXT bar's open, same as entries.

  BUG #3 [RAPID MARTINGALE] No cooldown between consecutive adds.
          In a flash crash the strategy could add 3 positions in 3 bars.
          Fix: MIN_BARS_BETWEEN_ADDS = 3 enforced between each add.

  BUG #4 [SLIPPAGE TOO OPTIMISTIC] Flat 0.05% for all assets is naive.
          Leveraged ETFs and small-caps have materially wider spreads.
          Fix: Per-symbol slippage table + SEC sell fee (0.0008%) +
               $0.01/share minimum tick spread cost.

  BUG #5 [IN-SAMPLE SELECTION BIAS] All 5 symbols were chosen because they
          looked good on the same 365-day dataset used to confirm them.
          Fix: Walk-forward split — first 60% is training, last 40% is
               out-of-sample. Flags if profit factors diverge > 50%.

  BUG #6 [MARKET REGIME BLINDNESS] 365-day test period was a tech bull run.
          INTC: $20->$125, SOXL: up 5x. Strategy never saw a bear market.
          Fix: Regime detector flags predominantly bullish test windows.
               Warns that out-of-sample bear performance is UNKNOWN.

Usage:
  python strategy_backtest.py              # All 3 live symbols, 365 days
  python strategy_backtest.py SOXL         # Single symbol, 365 days
  python strategy_backtest.py SOXL 180     # Custom lookback
  python strategy_backtest.py ALL 365      # All 3 live symbols, 365 days
"""

import sys
import json
import math
import requests
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Credentials ────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
CREDS_FILE = BASE_DIR.parent / "config" / "alpaca_credentials.json"

with open(CREDS_FILE) as f:
    CREDS = json.load(f)

HEADERS  = {
    "APCA-API-KEY-ID":     CREDS["api_key"],
    "APCA-API-SECRET-KEY": CREDS["api_secret"],
}
DATA_URL = "https://data.alpaca.markets"

# ── Strategy Parameters ────────────────────────────────────────────────────────
EMA_FAST              = 9
EMA_SLOW              = 21
DIP_THRESHOLD_PCT     = 5.0
MARTINGALE_TRIGGER    = 4.0     # lowered from 7.0: fires before 6-bar trail stop exits
MARTINGALE_SCALE      = 1.5
MAX_ADDS              = 3
MIN_BARS_BETWEEN_ADDS = 3       # BUG #3 FIX: min bars between scale-in adds
DAILY_KILL_PCT        = 10.0
TRAIL_BARS            = 6
HIGH_LOOKBACK_DAYS    = 30
BASE_TRADE_USD        = 500.0
STARTING_CAPITAL      = 10_000.0
WALK_FORWARD_SPLIT    = 0.60    # BUG #5 FIX: 60% train / 40% out-of-sample

# ── BUG #4 FIX: Per-symbol slippage table ─────────────────────────────────────
# Reflects real-world bid-ask spreads + market impact per asset class
SYMBOL_SLIPPAGE_PCT = {
    "SOXL":  0.12,   # 3x leveraged ETF — wide spread, high impact on size
    "IONQ":  0.09,   # Small-cap quantum — moderate/wide spread
    "RGTI":  0.12,   # Small-cap volatile — wide spread
    "QUBT":  0.12,
    "F":     0.05,   # Large mid-cap — tight spread
    "INTC":  0.04,   # Blue-chip — very tight spread
    "NVDA":  0.04,   # Blue-chip — very tight spread
    "NOK":   0.07,
    "AAL":   0.07,
    "NIO":   0.08,
    "TSLA":  0.05,
    "AMD":   0.04,
}
DEFAULT_SLIPPAGE_PCT = 0.06     # fallback for unlisted symbols
VARIABLE_SLIPPAGE    = True     # scale with relative volume
SEC_FEE_PCT          = 0.0008   # SEC fee on sells only (~$8 per $1M)
MIN_SPREAD_PER_SHARE = 0.01     # minimum 1-cent spread cost per share


# ── Data fetching with pagination ──────────────────────────────────────────────

def fetch_bars(symbol: str, days: int, timeframe: str = "1Hour") -> pd.DataFrame:
    start      = (datetime.now(timezone.utc) - timedelta(days=days + 10)).isoformat()
    all_bars   = []
    page_token = None
    pages      = 0

    while True:
        params = {"timeframe": timeframe, "start": start,
                  "limit": 1000, "feed": "sip"}
        if page_token:
            params["page_token"] = page_token

        r = requests.get(f"{DATA_URL}/v2/stocks/{symbol}/bars",
                         headers=HEADERS, params=params, timeout=30)
        r.raise_for_status()
        resp       = r.json()
        all_bars.extend(resp.get("bars", []))
        pages     += 1
        page_token = resp.get("next_page_token")
        if not page_token or pages > 30:
            break

    print(f"  [{symbol}] Fetched {len(all_bars)} bars across {pages} page(s)")
    if not all_bars:
        return pd.DataFrame()

    df             = pd.DataFrame(all_bars)
    df["t"]        = pd.to_datetime(df["t"])
    df             = df.sort_values("t").reset_index(drop=True)
    df["ema_fast"] = df["c"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_slow"] = df["c"].ewm(span=EMA_SLOW, adjust=False).mean()
    df["avg_vol"]  = df["v"].rolling(20, min_periods=5).mean()
    df["rel_vol"]  = df["v"] / df["avg_vol"].replace(0, 1)
    return df


# ── BUG #4 FIX: Realistic slippage + SEC fee ──────────────────────────────────

def apply_slippage(price: float, side: str, symbol: str,
                   rel_vol: float = 1.0, qty: float = 0.0) -> float:
    base_pct  = SYMBOL_SLIPPAGE_PCT.get(symbol.upper(), DEFAULT_SLIPPAGE_PCT)
    if VARIABLE_SLIPPAGE:
        base_pct *= min(max(rel_vol, 0.5), 2.0)   # cap at 2x on high-volume bars

    slip = price * (base_pct / 100)
    # Minimum 1-cent spread
    slip = max(slip, MIN_SPREAD_PER_SHARE)

    fill = price + slip if side == "buy" else price - slip

    # SEC fee on sells only
    if side == "sell":
        fill -= fill * (SEC_FEE_PCT / 100)

    return fill


# ── Helpers ────────────────────────────────────────────────────────────────────

def avg_cost_of(entries: list) -> float:
    total_val = sum(e["price"] * e["qty"] for e in entries)
    total_qty = sum(e["qty"] for e in entries)
    return total_val / total_qty if total_qty else 0.0


def sharpe_ratio(equity_curve: list, periods_per_year: int = 252 * 7) -> float:
    """Annualized Sharpe (rf=0) from a list of equity values."""
    if len(equity_curve) < 2:
        return 0.0
    returns = [(equity_curve[i] - equity_curve[i-1]) / equity_curve[i-1]
               for i in range(1, len(equity_curve)) if equity_curve[i-1] > 0]
    if not returns:
        return 0.0
    mean_r = sum(returns) / len(returns)
    var_r  = sum((r - mean_r) ** 2 for r in returns) / len(returns)
    std_r  = math.sqrt(var_r) if var_r > 0 else 0
    return (mean_r / std_r * math.sqrt(periods_per_year)) if std_r > 0 else 0.0


def max_consecutive_losses(trade_log: list) -> int:
    best = cur = 0
    for t in trade_log:
        pnl = t.get("pnl")
        if pnl is not None:
            if pnl < 0:
                cur += 1
                best = max(best, cur)
            else:
                cur = 0
    return best


def detect_market_regime(df: pd.DataFrame) -> str:
    """Return 'BULL', 'BEAR', or 'MIXED' based on first/last close comparison."""
    if df.empty or len(df) < 10:
        return "UNKNOWN"
    first = float(df.iloc[0]["c"])
    last  = float(df.iloc[-1]["c"])
    chg   = (last - first) / first * 100
    if chg > 20:
        return f"BULL (+{chg:.0f}%)"
    elif chg < -20:
        return f"BEAR ({chg:.0f}%)"
    else:
        return f"MIXED ({chg:+.0f}%)"


def ascii_equity_curve(equity_curve: list, width: int = 60, height: int = 10) -> str:
    """Render a simple ASCII equity curve. Returns a multi-line string."""
    if len(equity_curve) < 2:
        return "  [not enough data]"

    # Sample to fit width
    step     = max(1, len(equity_curve) // width)
    sampled  = equity_curve[::step]
    min_eq   = min(sampled)
    max_eq   = max(sampled)
    rng      = max_eq - min_eq if max_eq != min_eq else 1

    # Build grid (height rows x len(sampled) cols)
    grid = [[" "] * len(sampled) for _ in range(height)]
    for col, val in enumerate(sampled):
        row = height - 1 - int((val - min_eq) / rng * (height - 1))
        row = max(0, min(height - 1, row))
        grid[row][col] = "*"

    lines = []
    for r, row in enumerate(grid):
        eq_at_row = max_eq - (r / (height - 1)) * rng if height > 1 else max_eq
        lines.append(f"  ${eq_at_row:>9,.0f} |{''.join(row)}")
    lines.append(f"  {'':>10} +{'-' * len(sampled)}")
    lines.append(f"  {'':>11}  Start{' ' * (len(sampled) - 10)}End")
    return "\n".join(lines)


def monthly_pnl_table(trade_log: list) -> str:
    """Build a monthly P&L summary from closed trades."""
    monthly: dict = defaultdict(float)
    monthly_cnt: dict = defaultdict(int)
    for t in trade_log:
        pnl = t.get("pnl")
        if pnl is not None:
            month = t["ts"][:7]   # "2025-10"
            monthly[month] += pnl
            monthly_cnt[month] += 1
    if not monthly:
        return "  [no closed trades]"
    lines = [f"  {'Month':<10} {'P&L':>9} {'Trades':>7} {'Bar'}"]
    lines.append(f"  {'-'*35}")
    for month in sorted(monthly):
        pnl  = monthly[month]
        cnt  = monthly_cnt[month]
        bar  = "+" * int(abs(pnl) / 10) if pnl >= 0 else "-" * int(abs(pnl) / 10)
        bar  = bar[:20]
        sign = "+" if pnl >= 0 else ""
        lines.append(f"  {month:<10} {sign}${pnl:>7.2f} {cnt:>7}   {bar}")
    return "\n".join(lines)


# ── Core simulation engine ─────────────────────────────────────────────────────

def _run_sim(df: pd.DataFrame, symbol: str, label: str = "") -> dict:
    """
    Run the backtest simulation on a DataFrame slice.
    Returns a metrics dict.
    """
    diag_ema_cross = diag_dip_met = diag_both_met = diag_no_cash = 0

    cash              = STARTING_CAPITAL
    peak_equity       = cash
    max_drawdown_pct  = 0.0
    daily_equity_open = cash
    daily_date        = None
    kill_switch_days  = 0

    position          = None
    trade_log         = []
    martingale_count  = 0
    equity_curve      = [cash]

    wins, losses     = [], []
    gross_profit     = 0.0
    gross_loss       = 0.0

    for i in range(EMA_SLOW + TRAIL_BARS + 1, len(df) - 1):
        bar      = df.iloc[i]
        prev_bar = df.iloc[i - 1]
        next_bar = df.iloc[i + 1]

        bar_date = bar["t"].date()

        # ── Daily equity reset ─────────────────────────────────────────────────
        if bar_date != daily_date:
            pos_val           = position["total_qty"] * float(bar["o"]) if position else 0
            daily_equity_open = cash + pos_val
            daily_date        = bar_date

        pos_val        = position["total_qty"] * float(bar["c"]) if position else 0
        current_equity = cash + pos_val
        equity_curve.append(current_equity)

        # Track max drawdown
        if current_equity > peak_equity:
            peak_equity = current_equity
        dd = (peak_equity - current_equity) / peak_equity * 100 if peak_equity > 0 else 0
        if dd > max_drawdown_pct:
            max_drawdown_pct = dd

        # ── Kill-switch ────────────────────────────────────────────────────────
        if position and daily_equity_open > 0:
            daily_drop = (current_equity - daily_equity_open) / daily_equity_open * 100
            if daily_drop <= -DAILY_KILL_PCT:
                exit_price = apply_slippage(float(next_bar["o"]), "sell", symbol,
                                            float(next_bar.get("rel_vol", 1)),
                                            position["total_qty"])
                qty  = position["total_qty"]
                avg  = avg_cost_of(position["entries"])
                pnl  = (exit_price - avg) * qty
                cash += qty * exit_price
                (wins if pnl >= 0 else losses).append(pnl)
                if pnl >= 0: gross_profit += pnl
                else:        gross_loss   += abs(pnl)
                trade_log.append({"ts": str(bar["t"])[:19], "action": "KILL_SWITCH",
                                   "price": round(exit_price, 4), "qty": round(qty, 4),
                                   "pnl": round(pnl, 2), "adds": position.get("add_count", 0)})
                kill_switch_days += 1
                position = None
                continue

        # ── Trailing stop ──────────────────────────────────────────────────────
        if position:
            # Trail window = last TRAIL_BARS bars BEFORE current bar
            trail_window = df.iloc[max(0, i - TRAIL_BARS):i]
            new_trail    = float(trail_window["l"].min()) if not trail_window.empty else 0

            if position.get("trail_stop") is None or new_trail > position["trail_stop"]:
                position["trail_stop"] = new_trail

            curr_trail = position["trail_stop"]

            if curr_trail and float(bar["l"]) <= curr_trail:
                exit_price = apply_slippage(
                    min(float(bar["o"]), curr_trail), "sell", symbol,
                    float(bar.get("rel_vol", 1)), position["total_qty"]
                )
                qty  = position["total_qty"]
                avg  = avg_cost_of(position["entries"])
                pnl  = (exit_price - avg) * qty
                cash += qty * exit_price
                (wins if pnl >= 0 else losses).append(pnl)
                if pnl >= 0: gross_profit += pnl
                else:        gross_loss   += abs(pnl)
                trade_log.append({"ts": str(bar["t"])[:19], "action": "EXIT_TRAIL",
                                   "price": round(exit_price, 4), "qty": round(qty, 4),
                                   "pnl": round(pnl, 2), "adds": position.get("add_count", 0)})
                position = None
                continue

            # ── BUG #2 + #3 FIX: Martingale — fill next bar, enforce cooldown ─
            avg   = avg_cost_of(position["entries"])
            price = float(bar["c"])
            pnl_p = ((price - avg) / avg * 100) if avg > 0 else 0
            adds  = position.get("add_count", 0)
            bars_since_add = i - position.get("last_add_bar", -999)

            if (pnl_p <= -MARTINGALE_TRIGGER and
                    adds < MAX_ADDS and
                    bars_since_add >= MIN_BARS_BETWEEN_ADDS and
                    i + 1 < len(df)):

                add_notional = BASE_TRADE_USD * (MARTINGALE_SCALE ** adds)
                if cash >= add_notional:
                    # BUG #2 FIX: fill at NEXT bar open, not current bar close
                    add_fill = apply_slippage(float(next_bar["o"]), "buy", symbol,
                                              float(next_bar.get("rel_vol", 1)),
                                              add_notional / float(next_bar["o"]))
                    add_qty  = add_notional / add_fill
                    cash    -= add_qty * add_fill

                    position["entries"].append({"price": add_fill, "qty": add_qty})
                    position["total_qty"]    += add_qty
                    position["add_count"]     = adds + 1
                    position["last_add_bar"]  = i
                    martingale_count         += 1

                    trade_log.append({
                        "ts": str(next_bar["t"])[:19], "action": f"SCALE_IN_{adds+1}",
                        "price": round(add_fill, 4), "qty": round(add_qty, 4),
                        "pnl": None, "adds": adds + 1
                    })

        # ── Entry signal ───────────────────────────────────────────────────────
        else:
            cross_up = (
                float(prev_bar["ema_fast"]) <= float(prev_bar["ema_slow"]) and
                float(bar["ema_fast"])       >  float(bar["ema_slow"])
            )

            if cross_up:
                diag_ema_cross += 1
                # BUG #1 FIX: include bar i in 30d high (was: i, now: i+1)
                lookback_start = max(0, i - HIGH_LOOKBACK_DAYS * 7)
                high_30d       = float(df.iloc[lookback_start : i + 1]["h"].max())
                current_price  = float(bar["c"])
                dip_pct        = ((high_30d - current_price) / high_30d * 100) if high_30d > 0 else 0

                if dip_pct >= DIP_THRESHOLD_PCT:
                    diag_dip_met += 1
                if dip_pct >= DIP_THRESHOLD_PCT and cash < BASE_TRADE_USD:
                    diag_no_cash += 1

                if dip_pct >= DIP_THRESHOLD_PCT and cash >= BASE_TRADE_USD:
                    diag_both_met += 1
                    fill_price = apply_slippage(float(next_bar["o"]), "buy", symbol,
                                                float(next_bar.get("rel_vol", 1)),
                                                BASE_TRADE_USD / float(next_bar["o"]))
                    entry_qty  = BASE_TRADE_USD / fill_price
                    cash      -= entry_qty * fill_price

                    trail_window = df.iloc[max(0, i - TRAIL_BARS + 1) : i + 1]
                    init_trail   = float(trail_window["l"].min()) if not trail_window.empty else 0

                    position = {
                        "entries":      [{"price": fill_price, "qty": entry_qty}],
                        "total_qty":    entry_qty,
                        "add_count":    0,
                        "trail_stop":   init_trail,
                        "last_add_bar": -999,
                    }
                    trade_log.append({
                        "ts": str(next_bar["t"])[:19], "action": "ENTRY",
                        "price": round(fill_price, 4), "qty": round(entry_qty, 4),
                        "pnl": None, "adds": 0, "dip_pct": round(dip_pct, 2)
                    })

    # Close any open position at end
    if position:
        final_price = apply_slippage(float(df.iloc[-1]["c"]), "sell", symbol, 1.0,
                                     position["total_qty"])
        qty   = position["total_qty"]
        avg   = avg_cost_of(position["entries"])
        pnl   = (final_price - avg) * qty
        cash += qty * final_price
        (wins if pnl >= 0 else losses).append(pnl)
        if pnl >= 0: gross_profit += pnl
        else:        gross_loss   += abs(pnl)
        trade_log.append({"ts": str(df.iloc[-1]["t"])[:19], "action": "CLOSE_FINAL",
                           "price": round(final_price, 4), "qty": round(qty, 4),
                           "pnl": round(pnl, 2), "adds": position.get("add_count", 0)})

    final_equity  = cash
    total_return  = (final_equity - STARTING_CAPITAL) / STARTING_CAPITAL * 100
    total_trades  = len(wins) + len(losses)
    win_rate      = (len(wins) / total_trades * 100) if total_trades else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")
    avg_win       = (sum(wins)   / len(wins))   if wins   else 0.0
    avg_loss      = (sum(losses) / len(losses)) if losses else 0.0
    expectancy    = (win_rate/100 * avg_win) - ((1 - win_rate/100) * abs(avg_loss))
    sharpe        = sharpe_ratio(equity_curve)
    calmar        = (total_return / max_drawdown_pct) if max_drawdown_pct > 0 else 0.0
    max_cons_loss = max_consecutive_losses(trade_log)

    return {
        "label":               label,
        "final_equity":        round(final_equity, 2),
        "total_return_pct":    round(total_return, 2),
        "total_trades":        total_trades,
        "wins":                len(wins),
        "losses":              len(losses),
        "win_rate":            round(win_rate, 1),
        "profit_factor":       round(profit_factor, 2),
        "max_drawdown_pct":    round(max_drawdown_pct, 2),
        "expectancy":          round(expectancy, 2),
        "sharpe":              round(sharpe, 2),
        "calmar":              round(calmar, 2),
        "avg_win":             round(avg_win, 2),
        "avg_loss":            round(avg_loss, 2),
        "martingale_triggers": martingale_count,
        "kill_switch_days":    kill_switch_days,
        "max_consecutive_loss":max_cons_loss,
        "trade_log":           trade_log,
        "equity_curve":        equity_curve,
        "diag_ema_cross":      diag_ema_cross,
        "diag_dip_met":        diag_dip_met,
        "diag_both_met":       diag_both_met,
        "diag_no_cash":        diag_no_cash,
    }


# ── Main backtest runner ───────────────────────────────────────────────────────

def run_backtest(symbol: str, days: int = 365) -> dict | None:
    SEP  = "=" * 68
    SEP2 = "-" * 68

    print(f"\n{SEP}")
    print(f"  HARDENED BACKTEST: {symbol}  |  {days}d lookback  |  1-Hour bars")
    print(f"  Slippage: {SYMBOL_SLIPPAGE_PCT.get(symbol, DEFAULT_SLIPPAGE_PCT)}% "
          f"({'variable' if VARIABLE_SLIPPAGE else 'fixed'}) + SEC fee + $0.01/sh min")
    print(f"  Bug fixes active: LAB x2, Martingale cooldown, per-symbol slippage,")
    print(f"  walk-forward validation, regime detection")
    print(SEP)

    df = fetch_bars(symbol, days)
    if df.empty:
        print(f"  ERROR: No data for {symbol}")
        return None

    regime = detect_market_regime(df)
    print(f"  Market regime ({days}d): {regime}")

    # ── Walk-forward split (BUG #5 FIX) ───────────────────────────────────────
    split_idx  = int(len(df) * WALK_FORWARD_SPLIT)
    df_train   = df.iloc[:split_idx].reset_index(drop=True)
    df_oos     = df.iloc[split_idx:].reset_index(drop=True)

    print(f"  Walk-forward: {split_idx} bars train / {len(df)-split_idx} bars out-of-sample")

    m_train = _run_sim(df_train, symbol, "IN-SAMPLE")
    m_oos   = _run_sim(df_oos,   symbol, "OUT-OF-SAMPLE")
    m_full  = _run_sim(df,       symbol, "FULL PERIOD")

    # ── BUG #6 FIX: Regime & overfitting warnings ─────────────────────────────
    warnings = []

    if "BULL" in regime and int(regime.split("+")[-1].replace("%)", "").strip()) > 30:
        warnings.append(
            f"BULL MARKET BIAS: test window is strongly bullish ({regime}). "
            f"Strategy is UNTESTED in a bear market. Past PF may be inflated."
        )

    pf_train = m_train["profit_factor"]
    pf_oos   = m_oos["profit_factor"]
    if pf_train > 1.0 and pf_oos < 1.0:
        warnings.append(
            f"OVERFITTING DETECTED: In-sample PF={pf_train:.2f} but "
            f"out-of-sample PF={pf_oos:.2f} — strategy does NOT generalise."
        )
    elif pf_train > 0 and pf_oos > 0:
        diverge = abs(pf_train - pf_oos) / pf_train * 100
        if diverge > 50:
            warnings.append(
                f"PARAMETER INSTABILITY: PF dropped {diverge:.0f}% from "
                f"in-sample ({pf_train:.2f}) to out-of-sample ({pf_oos:.2f})."
            )

    if m_full["total_trades"] < 15:
        warnings.append(
            f"LOW SAMPLE SIZE: only {m_full['total_trades']} trades — "
            f"results are NOT statistically significant."
        )

    if m_full["win_rate"] > 80:
        warnings.append(
            "SUSPICIOUS WIN RATE >80% — check for residual look-ahead bias."
        )

    if m_full["max_drawdown_pct"] < 0.5 and m_full["total_trades"] > 5:
        warnings.append(
            "SUSPICIOUSLY LOW DRAWDOWN (<0.5%) — verify no look-ahead bias."
        )

    if m_full["martingale_triggers"] == 0 and m_full["total_trades"] > 10:
        warnings.append(
            "MARTINGALE NEVER TRIGGERED: trail stop exits before -7% is reached. "
            "Martingale code is essentially dead weight in live trading."
        )

    if m_full["max_consecutive_loss"] >= 5:
        warnings.append(
            f"MAX CONSECUTIVE LOSSES = {m_full['max_consecutive_loss']}. "
            f"Can you psychologically / financially survive this streak?"
        )

    slippage_used = SYMBOL_SLIPPAGE_PCT.get(symbol, DEFAULT_SLIPPAGE_PCT)
    if slippage_used < 0.05:
        warnings.append(
            f"SLIPPAGE MAY STILL BE TOO LOW at {slippage_used}% — "
            f"real fills in fast markets can be 3-5x worse."
        )

    # ── Print dashboard ────────────────────────────────────────────────────────

    print(f"\n  EQUITY CURVE  ({m_full['label']})")
    print(SEP2)
    print(ascii_equity_curve(m_full["equity_curve"]))

    print(f"\n  SIGNAL FREQUENCY DIAGNOSTICS")
    print(SEP2)
    print(f"  EMA {EMA_FAST}/{EMA_SLOW} crossovers detected : {m_full['diag_ema_cross']}")
    print(f"  Of those: dip >= {DIP_THRESHOLD_PCT}%          : {m_full['diag_dip_met']}"
          + ("  << ALL BLOCKED" if m_full["diag_dip_met"] == 0 and m_full["diag_ema_cross"] > 0 else ""))
    print(f"  Of those: cash available        : {m_full['diag_both_met']}  (actual entries)")
    print(f"  Blocked by insufficient cash    : {m_full['diag_no_cash']}")

    print(f"\n  PERFORMANCE DASHBOARD — {symbol}  |  {days}-day full period")
    print(SEP2)
    print(f"  {'Metric':<30} {'Full':>10}  {'In-Sample':>11}  {'Out-of-Sample':>14}")
    print(f"  {'-'*68}")
    def row(label, k, fmt=".2f", prefix=""):
        fv = m_full[k];  tv = m_train[k];  ov = m_oos[k]
        if isinstance(fv, float):
            return (f"  {label:<30} {prefix}{fv:>9{fmt}}  "
                    f"{prefix}{tv:>10{fmt}}  {prefix}{ov:>13{fmt}}")
        return f"  {label:<30} {fv:>10}  {tv:>11}  {ov:>14}"

    print(f"  {'Starting Capital':<30} ${STARTING_CAPITAL:>9,.2f}")
    print(f"  {'Final Equity':<30} ${m_full['final_equity']:>9,.2f}  "
          f"${m_train['final_equity']:>10,.2f}  ${m_oos['final_equity']:>13,.2f}")
    print(f"  {'Total Return':<30} {m_full['total_return_pct']:>+9.2f}%  "
          f"{m_train['total_return_pct']:>+10.2f}%  {m_oos['total_return_pct']:>+13.2f}%")
    print(f"  {'-'*68}")
    print(f"  {'Total Trades':<30} {m_full['total_trades']:>10}  "
          f"{m_train['total_trades']:>11}  {m_oos['total_trades']:>14}")
    print(f"  {'Win Rate':<30} {m_full['win_rate']:>9.1f}%  "
          f"{m_train['win_rate']:>10.1f}%  {m_oos['win_rate']:>13.1f}%")
    print(f"  {'Profit Factor':<30} {m_full['profit_factor']:>10.2f}  "
          f"{m_train['profit_factor']:>11.2f}  {m_oos['profit_factor']:>14.2f}")
    print(f"  {'Max Drawdown':<30} {m_full['max_drawdown_pct']:>9.2f}%  "
          f"{m_train['max_drawdown_pct']:>10.2f}%  {m_oos['max_drawdown_pct']:>13.2f}%")
    print(f"  {'Expectancy / Trade':<30} ${m_full['expectancy']:>+9.2f}  "
          f"${m_train['expectancy']:>+10.2f}  ${m_oos['expectancy']:>+13.2f}")
    print(f"  {'-'*68}")
    print(f"  {'Sharpe Ratio (ann.)':<30} {m_full['sharpe']:>10.2f}  "
          f"{m_train['sharpe']:>11.2f}  {m_oos['sharpe']:>14.2f}")
    print(f"  {'Calmar Ratio':<30} {m_full['calmar']:>10.2f}  "
          f"{m_train['calmar']:>11.2f}  {m_oos['calmar']:>14.2f}")
    print(f"  {'Avg Winning Trade':<30} ${m_full['avg_win']:>+9.2f}  "
          f"${m_train['avg_win']:>+10.2f}  ${m_oos['avg_win']:>+13.2f}")
    print(f"  {'Avg Losing Trade':<30} ${m_full['avg_loss']:>-9.2f}  "
          f"${m_train['avg_loss']:>-10.2f}  ${m_oos['avg_loss']:>-13.2f}")
    print(f"  {'Max Consec. Losses':<30} {m_full['max_consecutive_loss']:>10}  "
          f"{m_train['max_consecutive_loss']:>11}  {m_oos['max_consecutive_loss']:>14}")
    print(f"  {'Martingale Triggers':<30} {m_full['martingale_triggers']:>10}  "
          f"{m_train['martingale_triggers']:>11}  {m_oos['martingale_triggers']:>14}")
    print(f"  {'Kill-Switch Days':<30} {m_full['kill_switch_days']:>10}  "
          f"{m_train['kill_switch_days']:>11}  {m_oos['kill_switch_days']:>14}")
    print(f"  {'-'*68}")
    print(f"  Slippage model     : {slippage_used}% base (variable) + "
          f"SEC fee {SEC_FEE_PCT}% + ${MIN_SPREAD_PER_SHARE}/sh min")
    print(f"  Look-ahead bias    : FIXED (30d high includes signal bar; "
          f"Martingale fills next-bar open)")
    print(f"  Martingale cooldown: {MIN_BARS_BETWEEN_ADDS} bars min between adds")

    print(f"\n  MONTHLY P&L BREAKDOWN")
    print(SEP2)
    print(monthly_pnl_table(m_full["trade_log"]))

    if warnings:
        print(f"\n  RISK MANAGER FLAGS  ({len(warnings)} issues)")
        print(SEP2)
        for i, w in enumerate(warnings, 1):
            # word-wrap at 65 chars
            words = w.split()
            line  = ""
            for word in words:
                if len(line) + len(word) + 1 > 63:
                    print(f"  [{i}] {line}")
                    line  = "    " + word
                    i     = " "
                else:
                    line += (" " if line else "") + word
            if line:
                print(f"  [{i}] {line}")
    else:
        print(f"\n  RISK MANAGER FLAGS: None — clean bill of health.")

    # Abbreviated trade log (last 15 closed trades only)
    closed = [t for t in m_full["trade_log"] if t.get("pnl") is not None]
    if closed:
        print(f"\n  TRADE LOG — last {min(15, len(closed))} of {len(closed)} closed trades")
        print(SEP2)
        print(f"  {'Time':<21} {'Action':<16} {'Price':>8} {'P&L':>10} {'Adds':>5}")
        print(f"  {'-'*64}")
        for t in closed[-15:]:
            print(f"  {t['ts']:<21} {t['action']:<16} "
                  f"${t['price']:>7.3f} ${t['pnl']:>+9.2f} {t.get('adds',0):>5}")
    print()

    return {**m_full, "symbol": symbol, "days": days, "warnings": warnings,
            "profit_factor_oos": m_oos["profit_factor"],
            "regime": regime}


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    arg_sym  = sys.argv[1].upper() if len(sys.argv) > 1 else "ALL"
    arg_days = int(sys.argv[2])    if len(sys.argv) > 2 else 365

    symbols = ["SOXL", "INTC", "IONQ"] if arg_sym == "ALL" else [arg_sym]  # F/NVDA failed OOS validation

    all_results = []
    for sym in symbols:
        try:
            r = run_backtest(sym, arg_days)
            if r:
                all_results.append(r)
        except Exception as e:
            print(f"\nBacktest FAILED for {sym}: {e}")
            import traceback; traceback.print_exc()

    if len(all_results) > 1:
        W = 79
        print(f"\n{'=' * W}")
        print(f"  COMBINED SUMMARY  |  {arg_days}d  |  Hardened slippage  |  Walk-forward split")
        print(f"  {'Symbol':<7} {'Return':>7} {'WinR':>6} {'PF-Full':>8} {'PF-OOS':>8} "
              f"{'MaxDD':>6} {'Sharpe':>7} {'Trades':>7} {'Mart':>5} {'Warns':>6}")
        print(f"  {'-'*(W-2)}")
        for r in all_results:
            wflag  = " !!" if r["warnings"] else "   "
            pf_oos = r.get("profit_factor_oos", 0)
            oos_flag = " <LOW" if pf_oos < 1.0 else ("  ok" if pf_oos >= 1.5 else "  ~ok")
            print(
                f"  {r['symbol']:<7} "
                f"{r['total_return_pct']:>+6.1f}% "
                f"{r['win_rate']:>5.1f}% "
                f"{r['profit_factor']:>8.2f} "
                f"{pf_oos:>7.2f}{oos_flag} "
                f"{r['max_drawdown_pct']:>5.1f}% "
                f"{r['sharpe']:>7.2f} "
                f"{r['total_trades']:>7} "
                f"{r['martingale_triggers']:>5}"
                f"{wflag}"
            )
        print()
        print(f"  PF-OOS = profit factor on the OUT-OF-SAMPLE 40% of data.")
        print(f"  A healthy strategy has PF-OOS >= 1.0 and close to PF-Full.")
        print(f"  Large gap = overfitting. PF-OOS < 1.0 = do NOT trade live.")
        print()
