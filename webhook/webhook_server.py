#!/usr/bin/env python3
"""
Momentum Strategy — Webhook Execution Server
=============================================
Receives trade signals from momentum_strategy.py (or any compatible source),
executes market orders on Alpaca, and fires Telegram alerts on every event.

Endpoints:
  POST /webhook    — receive and execute a trade signal
  GET  /health     — server status, Alpaca connectivity, account equity
  GET  /positions  — all open positions with unrealized P&L

Signal payload (POST /webhook):
  {
    "secret":    "<webhook_secret from webhook_config.json>",
    "action":    "buy" | "scale_in" | "exit" | "kill_switch",
    "symbol":    "SOXL",        # omit only for kill_switch
    "trade_usd": 500.0,         # buy / scale_in only
    "reason":    "EMA cross"    # optional, shown in Telegram
  }

Response (success):
  {"status": "ok", "action": "buy", "symbol": "SOXL", "order_id": "abc123"}

Security:
  - Shared secret validated with constant-time HMAC comparison (no timing attacks)
  - Bound to 127.0.0.1 by default (LAN-only; change host to expose externally)
  - Rate-limited to 30 requests / minute per IP

Setup:
  pip install flask flask-limiter requests waitress
  Edit C:\\Users\\Nathaniel\\Documents\\Trading\\config\\webhook_config.json
  python webhook_server.py

Auto-start via Task Scheduler:
  schtasks /create /tn "WebhookServer" /tr "python C:\\...\\webhook_server.py"
           /sc ONLOGON /delay 0001:00 /f
"""

import hashlib
import hmac
import json
import logging
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, jsonify, request

# ── Optional dependencies (graceful degradation) ──────────────────────────────
try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    _LIMITER_OK = True
except ImportError:
    _LIMITER_OK = False

try:
    from waitress import serve as _waitress_serve
    _WAITRESS_OK = True
except ImportError:
    _WAITRESS_OK = False

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
CONFIG_DIR = BASE_DIR.parent / "config"
CREDS_FILE = CONFIG_DIR / "alpaca_credentials.json"
CFG_FILE   = CONFIG_DIR / "webhook_config.json"
LOG_DIR    = BASE_DIR.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE   = LOG_DIR / "webhook_server.log"

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

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ── Config loading ─────────────────────────────────────────────────────────────

def _require_json(path: Path, label: str) -> dict:
    if not path.exists():
        log.critical(f"[STARTUP] {label} not found: {path}")
        sys.exit(1)
    with open(path) as f:
        return json.load(f)


CREDS = _require_json(CREDS_FILE, "Alpaca credentials")
CFG   = _require_json(CFG_FILE,   "Webhook config")

_server_cfg  = CFG.get("server",  {})
_tg_cfg      = CFG.get("telegram",{})
_trade_cfg   = CFG.get("trading", {})

WEBHOOK_SECRET   = _server_cfg.get("webhook_secret", "")
HOST             = _server_cfg.get("host", "127.0.0.1")
PORT             = int(_server_cfg.get("port", 5050))

# credentials endpoint already includes /v2 (e.g. https://paper-api.alpaca.markets/v2)
# strip it so we can add our own path prefixes cleanly
_raw_endpoint    = CREDS["endpoint"].rstrip("/")
ALPACA_TRADE_URL = _raw_endpoint[:-3] if _raw_endpoint.endswith("/v2") else _raw_endpoint
ALPACA_DATA_URL  = "https://data.alpaca.markets"
ALPACA_HEADERS   = {
    "APCA-API-KEY-ID":     CREDS["api_key"],
    "APCA-API-SECRET-KEY": CREDS["api_secret"],
    "Content-Type":        "application/json",
}

DEFAULT_TRADE_USD = float(_trade_cfg.get("default_trade_usd", 500.0))
MAX_TRADE_USD     = float(_trade_cfg.get("max_trade_usd",     2000.0))
PAPER_MODE        = bool(_trade_cfg.get("paper_mode",         True))

TG_TOKEN   = _tg_cfg.get("bot_token", "")
TG_CHAT    = _tg_cfg.get("chat_id",   "")
TG_ENABLED = bool(TG_TOKEN and TG_CHAT
                  and "PASTE" not in TG_TOKEN
                  and len(TG_TOKEN) > 10)

_START_TS = datetime.now(timezone.utc)


# ── Custom exceptions ──────────────────────────────────────────────────────────

class _RateLimitError(Exception):
    """Alpaca returned 429 — respect Retry-After header."""
    def __init__(self, msg: str, retry_after: float = 5.0):
        super().__init__(msg)
        self.retry_after = retry_after


class _TransientError(Exception):
    """Alpaca returned 50x — safe to retry."""


class _NotionalFallback(Exception):
    """Alpaca rejected notional order (422) — retry with integer qty."""


# ── Retry wrapper ──────────────────────────────────────────────────────────────

def _retry(fn, max_tries: int = 3, base_delay: float = 1.0):
    """
    Call fn() up to max_tries times.
    Backs off exponentially on _RateLimitError and _TransientError.
    Returns (result, None) on success or (None, error_str) on exhaustion.
    """
    last_err = "unknown"
    for attempt in range(max_tries):
        try:
            return fn(), None
        except _RateLimitError as exc:
            delay = exc.retry_after or base_delay * (2 ** attempt)
            log.warning(f"[RETRY] 429 rate limit — waiting {delay:.1f}s "
                        f"(attempt {attempt + 1}/{max_tries})")
            time.sleep(delay)
            last_err = str(exc)
        except _TransientError as exc:
            delay = base_delay * (2 ** attempt)
            log.warning(f"[RETRY] Transient {exc} — waiting {delay:.1f}s "
                        f"(attempt {attempt + 1}/{max_tries})")
            time.sleep(delay)
            last_err = str(exc)
        except (_NotionalFallback, Exception):
            raise  # propagate immediately; caller decides
    return None, f"Max retries exceeded: {last_err}"


# ── Alpaca low-level HTTP ──────────────────────────────────────────────────────

def _ag(path: str) -> dict:
    """Alpaca GET — raises _RateLimitError / _TransientError on recoverable codes."""
    r = requests.get(f"{ALPACA_TRADE_URL}{path}",
                     headers=ALPACA_HEADERS, timeout=10)
    if r.status_code == 429:
        raise _RateLimitError("429", retry_after=float(
            r.headers.get("Retry-After", 5)))
    if r.status_code in (502, 503, 504):
        raise _TransientError(r.status_code)
    r.raise_for_status()
    return r.json()


def _ap(path: str, body: dict) -> requests.Response:
    """Alpaca POST — returns raw Response so caller inspects status."""
    r = requests.post(f"{ALPACA_TRADE_URL}{path}",
                      headers=ALPACA_HEADERS, json=body, timeout=10)
    if r.status_code == 429:
        raise _RateLimitError("429", retry_after=float(
            r.headers.get("Retry-After", 5)))
    if r.status_code in (502, 503, 504):
        raise _TransientError(r.status_code)
    return r


def _ad(path: str) -> requests.Response:
    """Alpaca DELETE — returns raw Response."""
    r = requests.delete(f"{ALPACA_TRADE_URL}{path}",
                        headers=ALPACA_HEADERS, timeout=10)
    if r.status_code == 429:
        raise _RateLimitError("429", retry_after=float(
            r.headers.get("Retry-After", 5)))
    if r.status_code in (502, 503, 504):
        raise _TransientError(r.status_code)
    return r


# ── Alpaca business logic ──────────────────────────────────────────────────────

def get_clock() -> dict:
    result, err = _retry(lambda: _ag("/v2/clock"))
    if err:
        raise RuntimeError(f"Clock check failed: {err}")
    return result


def is_market_open() -> bool:
    try:
        return bool(get_clock().get("is_open", False))
    except Exception as exc:
        log.warning(f"[MARKET] Hours check failed: {exc} — assuming closed")
        return False


def get_account() -> dict:
    result, err = _retry(lambda: _ag("/v2/account"))
    if err:
        raise RuntimeError(f"Account fetch failed: {err}")
    return result


def get_all_positions() -> list:
    result, err = _retry(lambda: _ag("/v2/positions"))
    if err:
        raise RuntimeError(f"Positions fetch failed: {err}")
    return result if isinstance(result, list) else []


def get_position(symbol: str) -> dict | None:
    try:
        result, err = _retry(lambda: _ag(f"/v2/positions/{symbol}"))
        return result if not err else None
    except Exception:
        return None


def _last_price(symbol: str) -> float:
    """Fetch latest mid-price from Alpaca data API."""
    r = requests.get(
        f"{ALPACA_DATA_URL}/v2/stocks/{symbol}/quotes/latest",
        headers=ALPACA_HEADERS, timeout=10)
    r.raise_for_status()
    q  = r.json().get("quote", {})
    bp = float(q.get("bp") or 0)
    ap = float(q.get("ap") or 0)
    if bp > 0 and ap > 0:
        return (bp + ap) / 2.0
    if bp > 0:
        return bp
    raise RuntimeError(f"No valid quote for {symbol}")


def place_order(symbol: str, trade_usd: float, side: str = "buy") -> dict:
    """
    Submit a market order for *trade_usd* notional.
    Falls back to integer-qty order if notional is rejected (422).
    Raises RuntimeError on unrecoverable failure.
    """
    if trade_usd <= 0:
        raise ValueError(f"trade_usd must be positive, got {trade_usd}")
    if trade_usd > MAX_TRADE_USD:
        raise ValueError(f"${trade_usd:.2f} exceeds MAX_TRADE_USD ${MAX_TRADE_USD:.2f}")

    # ── Attempt 1: notional order (supports fractional shares) ────────────────
    def _notional():
        resp = _ap("/v2/orders", {
            "symbol":        symbol,
            "notional":      round(trade_usd, 2),
            "side":          side,
            "type":          "market",
            "time_in_force": "day",
        })
        if resp.status_code == 422:
            raise _NotionalFallback(resp.json().get("message", "422"))
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Order {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    try:
        order, err = _retry(_notional)
        if err:
            raise RuntimeError(err)
        return order
    except _NotionalFallback as nf:
        log.info(f"[ORDER] Notional rejected for {symbol} ({nf}) — "
                 f"falling back to qty-based order")

    # ── Attempt 2: integer qty order ──────────────────────────────────────────
    price = _last_price(symbol)
    qty   = max(1, int(trade_usd / price))

    def _qty_order():
        resp = _ap("/v2/orders", {
            "symbol":        symbol,
            "qty":           qty,
            "side":          side,
            "type":          "market",
            "time_in_force": "day",
        })
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Qty order {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    order, err = _retry(_qty_order)
    if err:
        raise RuntimeError(f"Qty order failed for {symbol}: {err}")
    return order


def close_position(symbol: str) -> dict | None:
    """Close entire position for *symbol*. Returns None if no position exists."""
    if not get_position(symbol):
        log.info(f"[CLOSE] No open position for {symbol}")
        return None

    def _close():
        resp = _ad(f"/v2/positions/{symbol}")
        if resp.status_code == 404:
            return None
        if resp.status_code not in (200, 201, 204):
            raise RuntimeError(f"Close {resp.status_code}: {resp.text[:200]}")
        return resp.json() if resp.text else {"status": "closed", "symbol": symbol}

    result, err = _retry(_close)
    if err:
        raise RuntimeError(f"Close position {symbol} failed: {err}")
    return result


def close_all_positions() -> int:
    """Kill-switch: close every open position. Returns number closed."""
    def _close_all():
        resp = requests.delete(
            f"{ALPACA_TRADE_URL}/v2/positions",
            headers=ALPACA_HEADERS,
            json={"cancel_orders": True},
            timeout=15,
        )
        if resp.status_code == 429:
            raise _RateLimitError("429", retry_after=float(
                resp.headers.get("Retry-After", 5)))
        if resp.status_code in (502, 503, 504):
            raise _TransientError(resp.status_code)
        if resp.status_code not in (200, 204, 207):
            raise RuntimeError(f"Close-all {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json() if resp.text else []
        except Exception:
            return []

    result, err = _retry(_close_all)
    if err:
        raise RuntimeError(f"Kill-switch failed: {err}")
    return len(result) if isinstance(result, list) else 0


# ── Deduplication cache ────────────────────────────────────────────────────────
# Prevent double-execution if the strategy fires the same signal twice in rapid
# succession (e.g., network retry).  Window: 30 seconds.

_DEDUP_WINDOW = 30
_seen: deque = deque(maxlen=64)   # (sha256_hex, timestamp)


def _is_duplicate(payload: dict) -> bool:
    """True if identical payload (secret excluded) seen within DEDUP_WINDOW."""
    raw = json.dumps({k: v for k, v in payload.items() if k != "secret"},
                     sort_keys=True).encode()
    sig = hashlib.sha256(raw).hexdigest()
    now = time.monotonic()
    # evict stale entries
    while _seen and (now - _seen[0][1]) > _DEDUP_WINDOW:
        _seen.popleft()
    if any(h == sig for h, _ in _seen):
        return True
    _seen.append((sig, now))
    return False


# ── Secret validation ──────────────────────────────────────────────────────────

def _verify(incoming: str) -> bool:
    """Constant-time comparison — prevents timing-side-channel attacks."""
    return hmac.compare_digest(
        incoming.encode("utf-8"),
        WEBHOOK_SECRET.encode("utf-8"),
    )


# ── Telegram ───────────────────────────────────────────────────────────────────

def _tg_send(text: str):
    """Fire-and-forget Telegram message. Never raises."""
    if not TG_ENABLED:
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
            timeout=8,
        )
        if not r.ok:
            log.warning(f"[TG] Send failed {r.status_code}: {r.text[:100]}")
    except Exception as exc:
        log.warning(f"[TG] Exception: {exc}")


def _tg_trade(action: str, symbol: str, trade_usd: float,
              order: dict, reason: str = ""):
    icon = {"buy": "🟢", "scale_in": "🔵", "exit": "🔴",
            "kill_switch": "🛑"}.get(action, "⚪")
    raw_fill = order.get("filled_avg_price") or order.get("limit_price")
    fill_str = f"${float(raw_fill):.4f}" if raw_fill else "pending"
    mode     = "PAPER" if PAPER_MODE else "LIVE"
    now      = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _tg_send(
        f"{icon} <b>[{mode}] {action.upper()}: {symbol}</b>\n"
        f"Notional : ${trade_usd:.2f}\n"
        f"Fill     : {fill_str}\n"
        f"Order ID : {order.get('id', 'n/a')}\n"
        f"Reason   : {reason or 'n/a'}\n"
        f"Time     : {now}"
    )


def _tg_kill(n_closed: int, reason: str):
    mode = "PAPER" if PAPER_MODE else "LIVE"
    now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _tg_send(
        f"🛑 <b>[{mode}] KILL-SWITCH TRIGGERED</b>\n"
        f"Positions closed : {n_closed}\n"
        f"Reason           : {reason}\n"
        f"Time             : {now}"
    )


def _tg_error(context: str, error: str):
    mode = "PAPER" if PAPER_MODE else "LIVE"
    now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _tg_send(
        f"⚠️ <b>[{mode}] WEBHOOK ERROR</b>\n"
        f"Context : {context}\n"
        f"Error   : {error[:300]}\n"
        f"Time    : {now}"
    )


# ── Flask app ──────────────────────────────────────────────────────────────────

app = Flask(__name__)

if _LIMITER_OK:
    limiter = Limiter(app=app, key_func=get_remote_address,
                      default_limits=["60 per minute"])
    log.info("[STARTUP] Rate limiting: 60 req/min per IP")
else:
    limiter = None
    log.warning("[STARTUP] flask-limiter not installed — "
                "run: pip install flask-limiter")


def _rate(rule: str):
    """Apply limiter.limit() if available, else identity decorator."""
    def deco(fn):
        return limiter.limit(rule)(fn) if limiter else fn
    return deco


# ── POST /webhook ──────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
@_rate("30 per minute")
def webhook():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1. Parse body
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        log.warning(f"[WEBHOOK] {ts} — non-JSON body from {request.remote_addr}")
        return jsonify({"status": "error",
                        "message": "JSON body required"}), 400

    # 2. Authenticate
    if not _verify(str(data.get("secret", ""))):
        log.warning(f"[WEBHOOK] {ts} — AUTH FAILED from {request.remote_addr}")
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    # 3. Validate action
    action = str(data.get("action", "")).lower()
    if action not in ("buy", "scale_in", "exit", "kill_switch"):
        return jsonify({"status": "error",
                        "message": f"Unknown action '{action}'"}), 400

    symbol    = str(data.get("symbol", "")).upper().strip()
    trade_usd = float(data.get("trade_usd", DEFAULT_TRADE_USD))
    reason    = str(data.get("reason", ""))

    if action != "kill_switch" and not symbol:
        return jsonify({"status": "error",
                        "message": "symbol is required"}), 400

    # 4. Deduplicate
    if _is_duplicate(data):
        log.info(f"[WEBHOOK] {ts} — duplicate signal ignored "
                 f"({action} {symbol})")
        return jsonify({"status": "skipped",
                        "message": "Duplicate signal — ignored"}), 200

    # 5. Market hours gate
    if not is_market_open():
        msg = "Market is closed — signal received but not executed"
        log.info(f"[WEBHOOK] {ts} — {msg}")
        return jsonify({"status": "market_closed", "message": msg}), 200

    # 6. Execute
    log.info(f"[WEBHOOK] {ts} | {action.upper()} {symbol} "
             f"${trade_usd:.2f} | {reason}")
    try:
        if action in ("buy", "scale_in"):
            order = place_order(symbol, trade_usd, side="buy")
            _tg_trade(action, symbol, trade_usd, order, reason)
            log.info(f"[ORDER] {action.upper()} {symbol} "
                     f"notional=${trade_usd:.2f} id={order.get('id')}")
            return jsonify({
                "status":   "ok",
                "action":   action,
                "symbol":   symbol,
                "order_id": order.get("id"),
            }), 200

        if action == "exit":
            result = close_position(symbol)
            if result:
                _tg_trade("exit", symbol, 0.0, result, reason)
                log.info(f"[ORDER] EXIT {symbol} closed | {reason}")
                return jsonify({
                    "status": "ok",
                    "action": "exit",
                    "symbol": symbol,
                }), 200
            return jsonify({
                "status":  "skipped",
                "message": f"No open position for {symbol}",
            }), 200

        if action == "kill_switch":
            n = close_all_positions()
            _tg_kill(n, reason)
            log.warning(f"[KILL-SWITCH] {n} positions closed | {reason}")
            return jsonify({
                "status":            "ok",
                "action":            "kill_switch",
                "positions_closed":  n,
            }), 200

    except Exception as exc:
        log.exception(f"[WEBHOOK] Execution error on {action} {symbol}: {exc}")
        _tg_error(f"{action.upper()} {symbol or 'ALL'}", str(exc))
        return jsonify({"status": "error", "message": str(exc)}), 500


# ── GET /health ────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    elapsed = (datetime.now(timezone.utc) - _START_TS).total_seconds()
    uptime  = f"{int(elapsed // 3600)}h {int((elapsed % 3600) // 60)}m"

    alpaca_ok   = False
    market_open = False
    equity      = None
    n_pos       = 0
    alpaca_msg  = "unreachable"

    try:
        acct        = get_account()
        equity      = round(float(acct.get("equity", 0)), 2)
        alpaca_ok   = True
        market_open = is_market_open()
        n_pos       = len(get_all_positions())
        alpaca_msg  = "ok"
    except Exception as exc:
        alpaca_msg = str(exc)[:120]

    return jsonify({
        "status":     "ok",
        "uptime":     uptime,
        "paper_mode": PAPER_MODE,
        "telegram":   TG_ENABLED,
        "rate_limit": _LIMITER_OK,
        "waitress":   _WAITRESS_OK,
        "alpaca": {
            "connected":   alpaca_ok,
            "market_open": market_open,
            "equity_usd":  equity,
            "open_positions": n_pos,
            "message":     alpaca_msg,
        },
    }), 200


# ── GET /positions ─────────────────────────────────────────────────────────────

@app.route("/positions", methods=["GET"])
def positions():
    try:
        raw = get_all_positions()
        out = [{
            "symbol":         p.get("symbol"),
            "qty":            float(p.get("qty", 0)),
            "avg_entry":      float(p.get("avg_entry_price", 0)),
            "current_price":  float(p.get("current_price", 0)),
            "market_value":   float(p.get("market_value", 0)),
            "unrealized_pnl": float(p.get("unrealized_pl", 0)),
            "unrealized_pct": round(float(p.get("unrealized_plpc", 0)) * 100, 2),
        } for p in raw]
        return jsonify({"status": "ok", "count": len(out),
                        "positions": out}), 200
    except Exception as exc:
        log.exception(f"[POSITIONS] {exc}")
        return jsonify({"status": "error", "message": str(exc)}), 500


# ── Global error handlers ──────────────────────────────────────────────────────

@app.errorhandler(429)
def too_many_requests(_e):
    log.warning(f"[RATE LIMIT] 429 from {request.remote_addr}")
    return jsonify({"status": "error",
                    "message": "Rate limit exceeded — max 30 req/min"}), 429


@app.errorhandler(404)
def not_found(_e):
    return jsonify({"status": "error",
                    "message": f"No endpoint at {request.path}"}), 404


@app.errorhandler(500)
def internal_error(exc):
    log.exception(f"[500] Unhandled: {exc}")
    _tg_error("Unhandled 500", str(exc))
    return jsonify({"status": "error",
                    "message": "Internal server error"}), 500


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not WEBHOOK_SECRET or "CHANGE_ME" in WEBHOOK_SECRET:
        log.critical("[STARTUP] webhook_secret is not set in webhook_config.json — aborting")
        sys.exit(1)

    mode_str = "PAPER" if PAPER_MODE else "** LIVE MONEY **"
    log.info(f"[STARTUP] Webhook server  : http://{HOST}:{PORT}")
    log.info(f"[STARTUP] Alpaca endpoint : {ALPACA_TRADE_URL}")
    log.info(f"[STARTUP] Trading mode    : {mode_str}")
    log.info(f"[STARTUP] Telegram alerts : {'ON' if TG_ENABLED else 'OFF -- check webhook_config.json'}")
    log.info(f"[STARTUP] WSGI server     : {'waitress (production)' if _WAITRESS_OK else 'Flask dev (pip install waitress for production)'}")
    log.info(f"[STARTUP] Log file        : {LOG_FILE}")
    log.info(f"[STARTUP] Endpoints       : POST /webhook | GET /health | GET /positions")

    _tg_send(
        f"🚀 <b>Webhook server started</b>\n"
        f"Host   : {HOST}:{PORT}\n"
        f"Mode   : {mode_str}\n"
        f"Symbols: SOXL, INTC, IONQ\n"
        f"Time   : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    if _WAITRESS_OK:
        log.info("[STARTUP] Serving with waitress (production WSGI)")
        _waitress_serve(app, host=HOST, port=PORT, threads=4)
    else:
        log.info("[STARTUP] Serving with Flask dev server")
        app.run(host=HOST, port=PORT, debug=False, use_reloader=False,
                threaded=True)
