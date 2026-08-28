#!/usr/bin/env python3
"""
Momentum Accumulation & Trend-Riding Strategy
Phase 1 (Core Logic) + Phase 3 (Automation Loop)
=================================================

Assets     : SOXL, INTC, IONQ  (F/NVDA cut after walk-forward OOS validation)
Timeframe  : 1-hour bars for EMA signals and trailing stop
Entry      : 9 EMA crosses above 21 EMA AND price is >= 5% below 30-day high
Scale-in   : Martingale -- add 1.5x if position loses >= 4%; max 3 adds
Exit       : Trail stop = lowest low of last 6 one-hour bars
Kill-switch: Close all + halt today if daily equity drops >= 10%
Alerts     : Email on every entry, scale-in, exit, and kill-switch event

Run via Windows Task Scheduler every 30 min Mon-Fri 9:30 AM - 6:00 PM
"""

import json
import logging
import math
import smtplib
import sys
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
import pandas as pd

# ── Telegram ───────────────────────────────────────────────────────────────────
import sys as _sys
_sys.path.insert(0, str(Path(__file__).parent.parent / "config"))
try:
    from telegram_notifier import send as _tg
except Exception:
    def _tg(text: str): pass

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
CREDS_FILE = BASE_DIR.parent / "config" / "alpaca_credentials.json"
EMAIL_FILE = BASE_DIR.parent / "config" / "email_config.json"
STATE_FILE = BASE_DIR / "strategy_state.json"
LOG_FILE   = BASE_DIR.parent / "logs" / "momentum_strategy.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── Strategy Parameters (tune these) ──────────────────────────────────────────
SYMBOLS            = ["SOXL", "INTC", "IONQ"]  # F/NVDA removed: failed walk-forward OOS validation
BASE_TRADE_USD     = 500.0   # $ notional for initial entry
WEBHOOK_CFG_FILE   = BASE_DIR.parent / "config" / "webhook_config.json"
EMA_FAST           = 9       # Fast EMA period (bars)
EMA_SLOW           = 21      # Slow EMA period (bars)
DIP_THRESHOLD_PCT  = 5.0     # Min % below 30-day high to qualify entry (tuned from 10)
MARTINGALE_TRIGGER = 4.0     # % adverse move from avg cost to add (lowered from 7: fires before trail stop)
MARTINGALE_SCALE   = 1.5     # Size multiplier per Martingale add
MAX_ADDS           = 3       # Max scale-in additions (4 total legs max)
DAILY_KILL_PCT     = 10.0    # % daily equity drop to halt all trading
TRAIL_BARS         = 6       # N bars for trailing stop low lookback (tuned from 3)
HIGH_LOOKBACK_DAYS = 30      # Days back to compute 30-day high
LOOKBACK_BARS      = 120     # 1-hour bars to fetch per run

# ── Credentials ────────────────────────────────────────────────────────────────
with open(CREDS_FILE) as f:
    CREDS = json.load(f)

TRADE_URL = CREDS["endpoint"]
DATA_URL  = "https://data.alpaca.markets"
HEADERS   = {
    "APCA-API-KEY-ID":     CREDS["api_key"],
    "APCA-API-SECRET-KEY": CREDS["api_secret"],
    "Content-Type":        "application/json",
}

with open(EMAIL_FILE) as f:
    EMAIL_CFG = json.load(f)

# ── Webhook routing (optional) ─────────────────────────────────────────────────
# If webhook_config.json has a valid secret, trade signals are POSTed to the
# webhook server (which executes on Alpaca + sends Telegram alerts).
# If the server is unreachable, execution falls back to direct Alpaca calls.

_WEBHOOK_URL    = None
_WEBHOOK_SECRET = None

try:
    if WEBHOOK_CFG_FILE.exists():
        with open(WEBHOOK_CFG_FILE) as _wf:
            _wcfg = json.load(_wf)
        _wsrv   = _wcfg.get("server", {})
        _wsec   = _wsrv.get("webhook_secret", "")
        if _wsec and "CHANGE_ME" not in _wsec:
            _whost          = _wsrv.get("host", "127.0.0.1")
            _wport          = _wsrv.get("port", 5050)
            _WEBHOOK_URL    = f"http://{_whost}:{_wport}/webhook"
            _WEBHOOK_SECRET = _wsec
            # replace localhost alias so connection works on Windows
            _WEBHOOK_URL    = _WEBHOOK_URL.replace("0.0.0.0", "127.0.0.1")
except Exception as _we:
    log.warning(f"[WEBHOOK] Config load failed: {_we} — using direct Alpaca execution")


def post_signal(action: str, symbol: str = None,
                trade_usd: float = None, reason: str = "") -> dict | None:
    """
    POST a trade signal to the webhook server.
    Returns the parsed response dict on success, or None if the server is
    unavailable (caller should fall back to direct Alpaca execution).
    """
    if not _WEBHOOK_URL:
        return None
    payload = {"secret": _WEBHOOK_SECRET, "action": action, "reason": reason}
    if symbol:
        payload["symbol"] = symbol
    if trade_usd is not None:
        payload["trade_usd"] = trade_usd
    try:
        r = requests.post(_WEBHOOK_URL, json=payload, timeout=10)
        r.raise_for_status()
        resp = r.json()
        log.info(f"[WEBHOOK] {action.upper()} {symbol or ''} "
                 f"-> {resp.get('status')} | order_id={resp.get('order_id', 'n/a')}")
        return resp
    except requests.exceptions.ConnectionError:
        log.warning(f"[WEBHOOK] Server unreachable at {_WEBHOOK_URL} "
                    f"— falling back to direct Alpaca execution")
        return None
    except Exception as exc:
        log.warning(f"[WEBHOOK] Signal failed: {exc} "
                    f"— falling back to direct Alpaca execution")
        return None


# ── State persistence ──────────────────────────────────────────────────────────

def load_state() -> dict:
    defaults = {
        "halted_until":        None,   # ISO date string "2026-05-23"
        "halt_reason":         None,
        "daily_equity_open":   None,
        "daily_equity_date":   None,
        "positions":           {},     # symbol -> position dict
        "trade_log":           [],
        "martingale_triggers": 0,
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
        json.dump(state, f, indent=2, default=str)


# ── Email ──────────────────────────────────────────────────────────────────────

def send_email(subject: str, body: str):
    """Send plain-text email alert. Fails silently."""
    try:
        msg = MIMEMultipart()
        msg["From"]    = EMAIL_CFG["sender_email"]
        msg["To"]      = EMAIL_CFG["recipient"]
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP(EMAIL_CFG["smtp_host"], EMAIL_CFG["smtp_port"]) as s:
            s.starttls()
            s.login(EMAIL_CFG["sender_email"], EMAIL_CFG["app_password"])
            s.send_message(msg)
        log.info(f"[EMAIL] Sent: {subject}")
    except Exception as e:
        log.warning(f"Email failed: {e}")


# ── Alpaca REST helpers ────────────────────────────────────────────────────────

def get_account() -> dict:
    r = requests.get(f"{TRADE_URL}/account", headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()


def is_market_open() -> bool:
    r = requests.get(f"{TRADE_URL}/clock", headers=HEADERS, timeout=10)
    return r.json().get("is_open", False)


def place_order(symbol: str, notional: float, side: str = "buy") -> dict:
    """Notional market order. Falls back to qty-based if 422."""
    payload = {
        "symbol":        symbol,
        "notional":      str(round(notional, 2)),
        "side":          side,
        "type":          "market",
        "time_in_force": "day",
    }
    r = requests.post(f"{TRADE_URL}/orders", headers=HEADERS, json=payload, timeout=10)
    if r.status_code == 422:
        # Fallback: qty-based order using latest price
        price = get_latest_price(symbol)
        if price:
            qty = math.floor(notional / price * 100) / 100
            payload = {"symbol": symbol, "qty": str(qty), "side": side,
                       "type": "market", "time_in_force": "day"}
            r = requests.post(f"{TRADE_URL}/orders", headers=HEADERS, json=payload, timeout=10)
    r.raise_for_status()
    return r.json()


def close_position(symbol: str) -> dict | None:
    """Close entire position for one symbol."""
    r = requests.delete(f"{TRADE_URL}/positions/{symbol}", headers=HEADERS, timeout=10)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def close_all_positions():
    """Kill-switch: liquidate everything immediately."""
    r = requests.delete(
        f"{TRADE_URL}/positions",
        headers=HEADERS,
        json={"cancel_orders": True},
        timeout=15,
    )
    log.warning(f"[KILL-SWITCH] close_all_positions status: {r.status_code}")
    return r.status_code


def get_latest_price(symbol: str) -> float | None:
    r = requests.get(
        f"{DATA_URL}/v2/stocks/{symbol}/trades/latest",
        headers=HEADERS,
        params={"feed": "iex"},
        timeout=10,
    )
    if r.ok:
        return float(r.json().get("trade", {}).get("p", 0) or 0) or None
    return None


# ── Market data ────────────────────────────────────────────────────────────────

def get_1h_bars(symbol: str) -> pd.DataFrame:
    """Fetch recent 1-hour OHLCV bars."""
    start = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    r = requests.get(
        f"{DATA_URL}/v2/stocks/{symbol}/bars",
        headers=HEADERS,
        params={"timeframe": "1Hour", "start": start,
                "limit": LOOKBACK_BARS, "feed": "sip"},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json().get("bars", [])
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    df["t"] = pd.to_datetime(df["t"])
    return df.sort_values("t").reset_index(drop=True)


def get_30d_high(symbol: str) -> float | None:
    """Return the highest high over the last 30 calendar days (daily bars)."""
    start = (datetime.now(timezone.utc) - timedelta(days=HIGH_LOOKBACK_DAYS + 5)).isoformat()
    r = requests.get(
        f"{DATA_URL}/v2/stocks/{symbol}/bars",
        headers=HEADERS,
        params={"timeframe": "1Day", "start": start,
                "limit": HIGH_LOOKBACK_DAYS + 5, "feed": "sip"},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json().get("bars", [])
    if not data:
        return None
    df = pd.DataFrame(data)
    return float(df["h"].max())


# ── Indicators ─────────────────────────────────────────────────────────────────

def add_emas(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema_fast"] = df["c"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_slow"] = df["c"].ewm(span=EMA_SLOW, adjust=False).mean()
    return df


def ema_crossed_up(df: pd.DataFrame) -> bool:
    """True if fast EMA just crossed above slow EMA on the most recent completed bar."""
    if len(df) < 2:
        return False
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    return (float(prev["ema_fast"]) <= float(prev["ema_slow"])) and \
           (float(curr["ema_fast"]) >  float(curr["ema_slow"]))


def trailing_stop(df: pd.DataFrame) -> float | None:
    """Lowest low of the last TRAIL_BARS bars."""
    if len(df) < TRAIL_BARS:
        return None
    return float(df["l"].tail(TRAIL_BARS).min())


# ── Average cost helper ────────────────────────────────────────────────────────

def avg_cost_of(pos: dict) -> float:
    entries   = pos.get("entries", [])
    total_val = sum(e["fill_price"] * e["qty"] for e in entries)
    total_qty = sum(e["qty"] for e in entries)
    return total_val / total_qty if total_qty > 0 else 0.0


# ── Kill-switch ────────────────────────────────────────────────────────────────

def check_kill_switch(state: dict, equity: float) -> bool:
    """
    Record today's opening equity once per day.
    If equity drops >= DAILY_KILL_PCT% from the opening value:
      - Close every position
      - Halt the strategy for the rest of today
      - Fire a kill-switch email
    Returns True if halted.
    """
    today = datetime.now(timezone.utc).date().isoformat()

    if state["daily_equity_date"] != today:
        state["daily_equity_open"] = equity
        state["daily_equity_date"] = today
        log.info(f"New trading day. Opening equity: ${equity:,.2f}")
        return False

    if state["daily_equity_open"] and state["daily_equity_open"] > 0:
        drop_pct = (equity - state["daily_equity_open"]) / state["daily_equity_open"] * 100
        if drop_pct <= -DAILY_KILL_PCT:
            reason = (
                f"Daily equity dropped {drop_pct:.2f}% "
                f"(open ${state['daily_equity_open']:,.2f} -> now ${equity:,.2f})"
            )
            log.warning(f"[KILL-SWITCH] {reason}")
            _wh = post_signal("kill_switch", reason=reason)
            if not _wh:
                close_all_positions()
            state["positions"]    = {}
            state["halted_until"] = today
            state["halt_reason"]  = reason
            send_email(
                "[Momentum KILL-SWITCH] All positions closed — strategy halted",
                f"KILL-SWITCH TRIGGERED\n{'='*50}\n\n"
                f"{reason}\n\n"
                f"All open positions have been liquidated.\n"
                f"Trading is halted for the rest of today ({today}).\n"
                f"Strategy will resume automatically tomorrow.\n",
            )
            _tg(
                f"<b>KILL-SWITCH TRIGGERED</b>\n"
                f"{reason}\n"
                f"All positions liquidated. Halted for today."
            )
            return True
    return False


# ── Main strategy loop ─────────────────────────────────────────────────────────

def run():
    log.info("=" * 65)
    log.info(f"MOMENTUM STRATEGY  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info(f"Symbols: {', '.join(SYMBOLS)}")
    log.info("=" * 65)

    state = load_state()
    today = datetime.now(timezone.utc).date().isoformat()

    # Check halt
    if state.get("halted_until") == today:
        log.info(f"[HALTED] Strategy is halted today. Reason: {state.get('halt_reason')}")
        return

    # Market check
    if not is_market_open():
        log.info("Market is CLOSED. Nothing to do.")
        return

    # Account
    acct         = get_account()
    equity       = float(acct.get("equity", 0))
    buying_power = float(acct.get("buying_power", 0))
    log.info(f"Equity: ${equity:,.2f}  |  Buying Power: ${buying_power:,.2f}")

    # Kill-switch check
    if check_kill_switch(state, equity):
        save_state(state)
        return

    actions = []

    for symbol in SYMBOLS:
        log.info(f"\n-- {symbol} --")

        try:
            df = get_1h_bars(symbol)
            if df.empty or len(df) < EMA_SLOW + TRAIL_BARS + 2:
                log.warning(f"  Not enough bars for {symbol} — skipping")
                continue

            df          = add_emas(df)
            current     = float(df.iloc[-1]["c"])
            trail_level = trailing_stop(df)

            log.info(
                f"  Price: ${current:.2f} | "
                f"EMA{EMA_FAST}: ${df.iloc[-1]['ema_fast']:.2f} | "
                f"EMA{EMA_SLOW}: ${df.iloc[-1]['ema_slow']:.2f} | "
                f"Trail stop: ${trail_level:.2f}" if trail_level else
                f"  Price: ${current:.2f} | EMA{EMA_FAST}: ${df.iloc[-1]['ema_fast']:.2f} | "
                f"EMA{EMA_SLOW}: ${df.iloc[-1]['ema_slow']:.2f} | Trail stop: n/a"
            )

            pos = state["positions"].get(symbol)

            # ─────────────────────────────────────────────────────────────────
            # MANAGE OPEN POSITION
            # ─────────────────────────────────────────────────────────────────
            if pos:
                avg   = avg_cost_of(pos)
                qty   = pos["total_qty"]
                adds  = pos.get("add_count", 0)
                pnl_p = ((current - avg) / avg * 100) if avg else 0

                log.info(f"  Open: {qty:.4f} shares @ avg ${avg:.2f} ({pnl_p:+.1f}%)")

                # Update trailing stop (only ever moves up)
                if trail_level is not None:
                    if pos.get("trail_stop") is None or trail_level > pos["trail_stop"]:
                        pos["trail_stop"] = trail_level

                curr_trail = pos.get("trail_stop")
                log.info(f"  Effective trail stop: ${curr_trail:.2f}" if curr_trail else "  Trail stop not set yet")

                # ── EXIT: trailing stop hit ───────────────────────────────────
                if curr_trail and current <= curr_trail:
                    log.info(f"  [EXIT] Price ${current:.2f} hit trail stop ${curr_trail:.2f}")
                    _exit_reason = f"TRAIL_STOP @ ${curr_trail:.2f}"
                    _wh = post_signal("exit", symbol, reason=_exit_reason)
                    order = _wh if _wh else close_position(symbol)
                    if order:
                        pnl_usd = (current - avg) * qty
                        body = (
                            f"TRAILING STOP EXIT\n{'='*50}\n\n"
                            f"Symbol     : {symbol}\n"
                            f"Exit price : ${current:.2f}\n"
                            f"Avg cost   : ${avg:.2f}\n"
                            f"Qty        : {qty:.4f}\n"
                            f"P&L        : ${pnl_usd:+.2f} ({pnl_p:+.1f}%)\n"
                            f"Trail stop : ${curr_trail:.2f}\n"
                            f"Adds used  : {adds} / {MAX_ADDS}\n"
                            f"Closed at  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                        )
                        send_email(f"[Momentum] EXIT {symbol}: {pnl_p:+.1f}%", body)
                        _tg(
                            f"<b>Momentum EXIT: {symbol}</b>\n"
                            f"Price: ${current:.2f}  |  Avg: ${avg:.2f}\n"
                            f"P&amp;L: ${pnl_usd:+.2f} ({pnl_p:+.1f}%)\n"
                            f"Reason: trail stop ${curr_trail:.2f}"
                        )
                        actions.append(f"EXIT {symbol} {pnl_p:+.1f}%")
                        state["trade_log"].append({
                            "symbol": symbol, "action": "EXIT",
                            "price": current, "qty": qty,
                            "pnl_usd": round(pnl_usd, 2), "pnl_pct": round(pnl_p, 2),
                            "reason": "TRAIL_STOP",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        })
                        del state["positions"][symbol]
                    continue

                # ── SCALE-IN: Martingale add ──────────────────────────────────
                if pnl_p <= -MARTINGALE_TRIGGER and adds < MAX_ADDS:
                    add_notional = BASE_TRADE_USD * (MARTINGALE_SCALE ** adds)
                    if buying_power >= add_notional:
                        log.info(f"  [SCALE-IN #{adds+1}] Adding ${add_notional:.0f} at ${current:.2f}")
                        _si_reason = f"MARTINGALE_ADD_{adds+1} pnl={pnl_p:+.1f}%"
                        _wh = post_signal("scale_in", symbol, add_notional, _si_reason)
                        order = _wh if _wh else place_order(symbol, add_notional, "buy")
                        add_qty = add_notional / current

                        pos["entries"].append({
                            "fill_price": current,
                            "qty":        add_qty,
                            "notional":   add_notional,
                            "add_num":    adds + 1,
                            "timestamp":  datetime.now(timezone.utc).isoformat(),
                        })
                        pos["total_qty"]      += add_qty
                        pos["add_count"]       = adds + 1
                        state["martingale_triggers"] += 1
                        buying_power          -= add_notional

                        new_avg = avg_cost_of(pos)
                        body = (
                            f"SCALE-IN (Martingale Add #{adds+1})\n{'='*50}\n\n"
                            f"Symbol       : {symbol}\n"
                            f"Add price    : ${current:.2f}\n"
                            f"Add size     : ${add_notional:.0f}\n"
                            f"New avg cost : ${new_avg:.2f}\n"
                            f"Position P&L : {pnl_p:+.1f}%\n"
                            f"Adds used    : {adds+1} / {MAX_ADDS}\n"
                            f"Adds left    : {MAX_ADDS - (adds+1)}\n"
                            f"Time         : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                        )
                        send_email(f"[Momentum] SCALE-IN #{adds+1} {symbol}", body)
                        _tg(
                            f"<b>Momentum SCALE-IN #{adds+1}: {symbol}</b>\n"
                            f"Add: ${add_notional:.0f} @ ${current:.2f}\n"
                            f"New avg: ${new_avg:.2f}  |  Position P&amp;L: {pnl_p:+.1f}%\n"
                            f"Adds used: {adds+1}/{MAX_ADDS}"
                        )
                        actions.append(f"SCALE-IN #{adds+1} {symbol}")
                        state["trade_log"].append({
                            "symbol": symbol, "action": f"SCALE_IN_{adds+1}",
                            "price": current, "notional": add_notional,
                            "reason": f"MARTINGALE -{abs(pnl_p):.1f}%",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        })
                    else:
                        log.warning(f"  SCALE-IN #{adds+1} skipped — insufficient buying power (need ${add_notional:.0f}, have ${buying_power:.0f})")

                elif adds >= MAX_ADDS and pnl_p <= -MARTINGALE_TRIGGER:
                    log.info(f"  Max adds reached ({MAX_ADDS}). Holding position, waiting for trail exit.")

            # ─────────────────────────────────────────────────────────────────
            # CHECK ENTRY SIGNAL (no open position)
            # ─────────────────────────────────────────────────────────────────
            else:
                high_30d = get_30d_high(symbol)
                if not high_30d:
                    log.warning(f"  Could not fetch 30-day high for {symbol}")
                    continue

                dip_pct = ((high_30d - current) / high_30d * 100) if high_30d > 0 else 0
                crossed = ema_crossed_up(df)

                log.info(
                    f"  30d high: ${high_30d:.2f} | "
                    f"Dip from high: {dip_pct:.1f}% (need >= {DIP_THRESHOLD_PCT}%) | "
                    f"EMA cross: {crossed}"
                )

                if crossed and dip_pct >= DIP_THRESHOLD_PCT and buying_power >= BASE_TRADE_USD:
                    log.info(f"  [ENTRY] Signal confirmed -- BUY ${BASE_TRADE_USD:.0f} of {symbol}")
                    _entry_reason = (f"EMA{EMA_FAST}/{EMA_SLOW} cross + "
                                     f"dip {dip_pct:.1f}% from 30d high")
                    _wh = post_signal("buy", symbol, BASE_TRADE_USD, _entry_reason)
                    order = _wh if _wh else place_order(symbol, BASE_TRADE_USD, "buy")
                    est_qty = BASE_TRADE_USD / current

                    state["positions"][symbol] = {
                        "entries": [{
                            "fill_price": current,
                            "qty":        est_qty,
                            "notional":   BASE_TRADE_USD,
                            "add_num":    0,
                            "timestamp":  datetime.now(timezone.utc).isoformat(),
                        }],
                        "total_qty":  est_qty,
                        "add_count":  0,
                        "trail_stop": trail_level,
                        "opened_at":  datetime.now(timezone.utc).isoformat(),
                        "order_id":   order.get("id"),
                    }
                    buying_power -= BASE_TRADE_USD

                    body = (
                        f"NEW ENTRY\n{'='*50}\n\n"
                        f"Symbol      : {symbol}\n"
                        f"Entry price : ${current:.2f}\n"
                        f"Size        : ${BASE_TRADE_USD:.0f}\n"
                        f"30d high    : ${high_30d:.2f}\n"
                        f"Dip pct     : {dip_pct:.1f}%\n"
                        f"EMA {EMA_FAST}/{EMA_SLOW}: ${df.iloc[-1]['ema_fast']:.2f} / ${df.iloc[-1]['ema_slow']:.2f}\n"
                        f"Init trail  : ${trail_level:.2f}\n"
                        f"Max adds    : {MAX_ADDS} (each adds {MARTINGALE_SCALE}x)\n"
                        f"Time        : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    )
                    send_email(f"[Momentum] ENTRY {symbol} @ ${current:.2f}", body)
                    _tg(
                        f"<b>Momentum ENTRY: {symbol}</b>\n"
                        f"Price: ${current:.2f}  |  Size: ${BASE_TRADE_USD:.0f}\n"
                        f"Dip: {dip_pct:.1f}% from 30d high  |  EMA cross confirmed\n"
                        f"Trail stop: ${trail_level:.2f}"
                    )
                    actions.append(f"ENTRY {symbol} @ ${current:.2f}")
                    state["trade_log"].append({
                        "symbol": symbol, "action": "ENTRY",
                        "price": current, "notional": BASE_TRADE_USD,
                        "reason": f"EMA_CROSS + DIP_{dip_pct:.1f}%",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })

                else:
                    reasons = []
                    if not crossed:
                        reasons.append(f"no EMA{EMA_FAST}/EMA{EMA_SLOW} cross")
                    if dip_pct < DIP_THRESHOLD_PCT:
                        reasons.append(f"dip only {dip_pct:.1f}% (need {DIP_THRESHOLD_PCT}%)")
                    if buying_power < BASE_TRADE_USD:
                        reasons.append(f"low buying power ${buying_power:.0f}")
                    log.info(f"  No entry: {', '.join(reasons)}")

        except Exception as e:
            log.error(f"  Error on {symbol}: {e}", exc_info=True)

    save_state(state)

    if actions:
        log.info(f"\nActions this run: {', '.join(actions)}")
    else:
        log.info("\nNo actions taken this run.")
    log.info(f"Martingale triggers (lifetime): {state['martingale_triggers']}")
    log.info("Run complete.\n")


if __name__ == "__main__":
    run()
