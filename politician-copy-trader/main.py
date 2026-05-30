#!/usr/bin/env python3
"""
Politician Copy Trader — Multi-politician Orchestrator
========================================================
1. Loops through every politician in config.json.
2. Scrapes Capitol Trades for each one's latest trades.
3. Finds trades not yet seen, queues new tradeable ones.
4. Executes queued trades when market is open (holds in pending otherwise).
5. Fires Windows desktop notifications on new trades and gain milestones.
6. Persists all state to state.json.

Scheduled hourly Mon–Fri 9:30 AM–6 PM via Windows Task Scheduler.
"""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import scraper
import trader
import notifier

BASE_DIR    = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE  = BASE_DIR / "state.json"
LOG_FILE    = BASE_DIR.parent / "logs" / "copy_trader.log"

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────

with open(CONFIG_FILE) as f:
    CONFIG = json.load(f)

POLITICIANS      = CONFIG["politicians"]
TRADE_AMOUNT     = CONFIG["trading"]["trade_amount_usd"]
SCALE_BY_SIZE    = CONFIG["trading"]["scale_by_size"]
SIZE_TIERS       = CONFIG["trading"]["size_tiers"]
SKIP_KEYWORDS    = CONFIG["trading"]["skip_keywords"]
NOTIF_CFG        = CONFIG.get("notifications", {})
NOTIFY_ENABLED   = NOTIF_CFG.get("enabled", True)
NOTIFY_TRADES    = NOTIF_CFG.get("notify_new_trades", True)
NOTIFY_GAIN      = NOTIF_CFG.get("notify_gain", True)
GAIN_MILESTONES  = sorted(NOTIF_CFG.get("gain_milestones_pct", [5, 10, 25, 50, 100]))


# ── State ──────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    defaults = {
        "seen_trade_keys":       [],
        "pending_trades":        [],
        "executed_trades":       [],
        "notified_milestones":   {},   # key: "{symbol}|{milestone}" -> True
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


# ── Trade sizing ───────────────────────────────────────────────────────────────

def resolve_amount(size_range: str) -> float:
    if not SCALE_BY_SIZE:
        return float(TRADE_AMOUNT)
    size_upper = size_range.upper()
    for tier_key, amount in SIZE_TIERS.items():
        if tier_key.upper().replace(" ", "") in size_upper:
            return float(amount)
    return float(TRADE_AMOUNT)


# ── Gain milestone checker ─────────────────────────────────────────────────────

def check_gain_notifications(state: dict):
    """Scan open positions for milestone gains and fire notifications."""
    if not NOTIFY_GAIN:
        return

    notified = state.setdefault("notified_milestones", {})
    data_url = "https://data.alpaca.markets/v2"

    # Build map: symbol -> (politician_name, notional_usd)
    sym_map = {}
    for et in state["executed_trades"]:
        if et.get("closed"):
            continue
        sym  = et.get("symbol")
        pol  = et.get("politician", "Congress")
        cost = float(et.get("notional_usd", 0) or 0)
        qty  = float(et.get("filled_qty", 0) or 0)
        if sym and qty > 0:
            sym_map.setdefault(sym, {"politician": pol, "total_cost": 0, "total_qty": 0})
            sym_map[sym]["total_cost"] += cost
            sym_map[sym]["total_qty"]  += qty

    for sym, info in sym_map.items():
        if info["total_qty"] <= 0 or info["total_cost"] <= 0:
            continue
        price = trader.get_latest_price(sym)
        if not price:
            continue

        current   = info["total_qty"] * price
        pnl_usd   = current - info["total_cost"]
        pnl_pct   = (pnl_usd / info["total_cost"]) * 100

        for milestone in GAIN_MILESTONES:
            mkey = f"{sym}|+{milestone}"
            if pnl_pct >= milestone and not notified.get(mkey):
                notifier.gain_milestone(sym, info["politician"], pnl_pct, pnl_usd)
                notified[mkey] = True
                log.info(f"  [MILESTONE] {sym} hit +{milestone}% (currently {pnl_pct:+.1f}%)")

        # Loss alert at -10% (once)
        loss_key = f"{sym}|-10"
        if pnl_pct <= -10 and not notified.get(loss_key):
            notifier.loss_alert(sym, info["politician"], pnl_pct, pnl_usd)
            notified[loss_key] = True
            log.info(f"  [LOSS ALERT] {sym} at {pnl_pct:+.1f}%")


# ── Main ───────────────────────────────────────────────────────────────────────

def run():
    log.info("=" * 65)
    log.info(f"COPY TRADER  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info(f"Watching {len(POLITICIANS)} politicians")
    log.info("=" * 65)

    state     = load_state()
    seen_keys = set(state["seen_trade_keys"])
    all_new   = []

    # ── Step 1: Scrape all politicians ────────────────────────────────────────
    for pol in POLITICIANS:
        pol_id   = pol["id"]
        pol_name = pol["name"]
        ret      = pol.get("return_2025_pct")
        ret_str  = f" ({ret:+.1f}% in 2025)" if ret else ""
        log.info(f"\n-- {pol_name}{ret_str} --")

        try:
            trades = scraper.fetch_trades(pol_id, page_size=20)
        except Exception as e:
            log.warning(f"  Scrape failed for {pol_name}: {e}")
            continue

        for t in trades:
            # Prefix key with politician ID to avoid cross-politician collisions
            full_key = f"{pol_id}:{t.unique_key}"

            # Also accept legacy keys without prefix (for Ro Khanna's existing trades)
            if full_key in seen_keys or t.unique_key in seen_keys:
                continue

            seen_keys.add(full_key)

            if not t.is_tradeable(SKIP_KEYWORDS):
                log.info(f"  [SKIP] {t.ticker} ({t.company}) — not tradeable")
                continue

            log.info(
                f"  [NEW ] {t.tx_type.upper()} {t.alpaca_symbol} "
                f"({t.company}) on {t.tx_date} | size: {t.size_range}"
            )
            all_new.append((pol_name, t))

    state["seen_trade_keys"] = list(seen_keys)

    # ── Step 2: Queue new trades ──────────────────────────────────────────────
    pending_keys = {p["unique_key"] for p in state["pending_trades"]}
    for pol_name, t in all_new:
        full_key = f"{POLITICIANS[[p['id'] for p in POLITICIANS].index(next(p['id'] for p in POLITICIANS if p['name'] == pol_name))]  if False else ''}:{t.unique_key}"
        # Build a simple lookup
        pol_id = next((p["id"] for p in POLITICIANS if p["name"] == pol_name), "")
        full_key = f"{pol_id}:{t.unique_key}"
        if full_key not in pending_keys:
            state["pending_trades"].append({
                "unique_key":          full_key,
                "symbol":              t.alpaca_symbol,
                "tx_type":             t.tx_type,
                "tx_date":             t.tx_date,
                "filed_date":          t.filed_date,
                "size_range":          t.size_range,
                "company":             t.company,
                "politician":          pol_name,
                "price_at_disclosure": t.price,
            })

    save_state(state)
    log.info(f"\nNew trades queued : {len(all_new)}")
    log.info(f"Pending (total)   : {len(state['pending_trades'])}")

    # ── Step 3: Send new-trade notifications ──────────────────────────────────
    if NOTIFY_ENABLED and NOTIFY_TRADES:
        for pol_name, t in all_new:
            notifier.new_trade(pol_name, t.alpaca_symbol, t.tx_type, t.company, t.size_range)

    # ── Step 4: Execute pending trades if market is open ──────────────────────
    if not state["pending_trades"]:
        log.info("No pending trades to execute.")
    else:
        market_open = trader.is_market_open()
        if not market_open:
            log.info(f"Market CLOSED. Will execute at next open: {trader.get_next_open()}")
        else:
            buying_power  = trader.get_buying_power()
            executed_keys = set()
            log.info(f"Market OPEN — buying power: ${buying_power:,.2f}")

            for pending in list(state["pending_trades"]):
                symbol  = pending["symbol"]
                tx_type = pending["tx_type"]
                amount  = resolve_amount(pending["size_range"])

                try:
                    if tx_type == "buy":
                        if buying_power < amount:
                            log.warning(f"  Insufficient buying power for {symbol} (${amount:.2f})")
                            continue
                        order = trader.place_buy(symbol, amount)
                        buying_power -= amount
                        state["executed_trades"].append({
                            **pending,
                            "executed_at":      datetime.now(timezone.utc).isoformat(),
                            "order_id":         order.get("id"),
                            "order_status":     order.get("status"),
                            "notional_usd":     amount,
                            "filled_qty":       order.get("filled_qty"),
                            "filled_avg_price": order.get("filled_avg_price"),
                            "closed":           False,
                        })

                    elif tx_type == "sell":
                        order = trader.place_sell_all(symbol)
                        if order:
                            for et in state["executed_trades"]:
                                if et["symbol"] == symbol and not et.get("closed"):
                                    et["closed"]         = True
                                    et["closed_at"]      = datetime.now(timezone.utc).isoformat()
                                    et["close_order_id"] = order.get("id")
                            state["executed_trades"].append({
                                **pending,
                                "executed_at":  datetime.now(timezone.utc).isoformat(),
                                "order_id":     order.get("id"),
                                "order_status": order.get("status"),
                                "notional_usd": 0,
                                "closed":       True,
                            })

                    executed_keys.add(pending["unique_key"])

                except Exception as e:
                    log.error(f"  Order failed for {symbol}: {e}")

            state["pending_trades"] = [
                p for p in state["pending_trades"]
                if p["unique_key"] not in executed_keys
            ]
            save_state(state)
            log.info(f"Executed: {len(executed_keys)} | Still pending: {len(state['pending_trades'])}")

    # ── Step 5: Check gain milestones ──────────────────────────────────────────
    if NOTIFY_ENABLED:
        check_gain_notifications(state)
        save_state(state)

    log.info("\nRun complete.\n")


if __name__ == "__main__":
    run()
