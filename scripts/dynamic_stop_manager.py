#!/usr/bin/env python3
"""
Dynamic Stop-Loss Manager (per-lot, staged trail)
====================================================
Replaces buy_consistent_losers.py's flat 3% Alpaca-native trailing_stop with
a per-lot staged stop, specified directly by Nathaniel (2026-08-29):

  1. TIGHT stage: trail 3% below the highest price since THIS LOT's entry.
  2. Once that 3% trail would reach or exceed the lot's own entry price
     (highest_price * 0.97 >= entry_price), switch to BREAKEVEN_HOLD: freeze
     the stop at exactly the entry price (stop trailing) and leave it there.
  3. Once price reaches or exceeds entry_price * 1.05 (+5% from entry),
     switch to WIDE_TRAIL: resume trailing, now 5% below the highest price,
     for the rest of the lot's life.

Alpaca's native trailing_stop order type can't express this (one fixed
percent for the order's whole life, no freeze/resume), so this hand-rolls
it: computes the target stop price itself every run and replaces the
resting order (cancel + resubmit a plain "stop" order) whenever the target
price or stage has changed.

Tracked per LOT, not per symbol -- several symbols (HRL, FHB, ...) have
multiple $500 buys on different days at different prices, and each lot
keeps its own independent stage/entry price, matching how
buy_consistent_losers.py already places one stop per individual buy.

buy_consistent_losers.py still attaches its own flat 3% native trailing
stop immediately at buy time (unchanged) -- that remains the safety net
covering the gap between a buy and this manager's next run. This script
finds that native stop, cancels it, and takes the lot over into the staged
system (see absorb_new_lots()).

Runs every 15 min during market hours, Mon-Fri (Task Scheduler
\\Alpaca\\DynamicStopManager). Idempotent -- safe to run anytime; it only
replaces an order when the target price or stage has actually changed.
"""

import json
import math
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent / "signals"))
from signal_utils import get_logger, load_state, save_state

BASE_DIR = Path(__file__).parent.parent
with open(BASE_DIR / "config" / "alpaca_credentials.json") as f:
    creds = json.load(f)
BASE_URL = creds["endpoint"]
DATA_URL = "https://data.alpaca.markets"
HEADERS  = {
    "APCA-API-KEY-ID":     creds["api_key"],
    "APCA-API-SECRET-KEY": creds["api_secret"],
}

log = get_logger("dynamic_stop_manager", "dynamic_stop_manager.log")

TIGHT_PCT             = 0.03   # stage 1: trail 3% below highest since entry
BREAKEVEN_MULT        = 1.05   # must reach entry * 1.05 to leave breakeven hold
WIDE_PCT              = 0.05   # stage 3: trail 5% below highest since entry
NEW_LOT_LOOKBACK_DAYS = 5      # how far back to look for not-yet-tracked buy fills
STATE_FILE            = "dynamic_stops.json"


_ISO_RE = re.compile(r"^(.*T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(.*)$")


def parse_iso(ts: str) -> datetime:
    """Parse an Alpaca timestamp.

    Alpaca returns fractional seconds at whatever precision it happens to
    have -- '...:01.41339+00:00' is 5 digits. Python 3.10's
    datetime.fromisoformat accepts ONLY exactly 3 or 6 fractional digits
    and raises ValueError on anything else (3.11+ parses full ISO 8601, so
    this is version-specific). That made this crash intermittently, purely
    depending on the digits in a fill timestamp -- and because the crash
    landed before save_state(), every run from 2026-08-31 to 2026-09-02
    silently threw away its work. Normalize the fraction to 6 digits."""
    ts = ts.strip().replace("Z", "+00:00")
    m = _ISO_RE.match(ts)
    if not m:
        return datetime.fromisoformat(ts)
    head, frac, tail = m.group(1), m.group(2) or "", m.group(3) or ""
    return datetime.fromisoformat(f"{head}.{(frac + '000000')[:6]}{tail}")


def get_latest_price(symbol: str) -> float:
    r = requests.get(f"{DATA_URL}/v2/stocks/{symbol}/trades/latest", headers=HEADERS, timeout=15)
    r.raise_for_status()
    return float(r.json()["trade"]["p"])


def get_positions() -> dict:
    r = requests.get(f"{BASE_URL}/positions", headers=HEADERS, timeout=15)
    r.raise_for_status()
    return {p["symbol"]: p for p in r.json()}


def get_open_orders() -> list:
    r = requests.get(f"{BASE_URL}/orders", headers=HEADERS, params={"status": "open", "limit": 500}, timeout=15)
    r.raise_for_status()
    return r.json()


def cancel_order(order_id: str):
    r = requests.delete(f"{BASE_URL}/orders/{order_id}", headers=HEADERS, timeout=15)
    if r.status_code not in (200, 204):
        raise RuntimeError(f"cancel failed: {r.status_code} {r.text[:150]}")


def sellable_qty(symbol: str, ignore_order_id: str = None) -> int:
    """Whole shares of `symbol` that can be sold right now without going short:
    held whole shares minus shares already reserved by other open sell orders.
    Read fresh from the broker immediately before every sell.

    Every sell in this script goes through this. Before 2026-09-22 nothing
    checked holdings before selling, so state lots for shares that were long
    gone kept issuing sells -- Alpaca rejected most (~450 403s/day) but not
    all, and KBR (2026-09-09) and MGM (2026-09-14, again 09-16) went SHORT.
    `ignore_order_id` excludes an order just canceled, which can briefly
    still be listed as open while the cancel settles."""
    positions = get_positions()
    held = float(positions[symbol]["qty"]) if symbol in positions else 0.0
    if held < 1:
        return 0
    reserved = 0
    for o in get_open_orders():
        if o["symbol"] == symbol and o["side"] == "sell" and o["id"] != ignore_order_id:
            reserved += float(o["qty"]) - float(o.get("filled_qty") or 0)
    return max(0, math.floor(held) - math.ceil(reserved))


def allocate_lots_to_holdings(lots: list, held_whole: dict, open_order_ids: set) -> tuple:
    """Trim tracked lots so each symbol's lots never total more than the whole
    shares actually held. Returns (kept, dropped, resized).

    Holdings are the truth; the state file is only bookkeeping. Lots with a
    live resting stop are kept first (they're known-real), then the rest in
    their original order. A lot that only partly fits is shrunk and its stop
    flagged for re-placement at the new size (stop_price=None forces it)."""
    order = sorted(range(len(lots)), key=lambda i: lots[i].get("stop_order_id") not in open_order_ids)
    remaining = dict(held_whole)
    kept_idx, dropped, resized = set(), [], []
    for i in order:
        lot = lots[i]
        avail = remaining.get(lot["symbol"], 0)
        if avail < 1:
            dropped.append(lot)
            continue
        if lot["qty"] > avail:
            resized.append((lot["symbol"], lot["qty"], avail))
            lot["qty"] = avail
            lot["stop_price"] = None
        remaining[lot["symbol"]] = avail - lot["qty"]
        kept_idx.add(i)
    return [lots[i] for i in range(len(lots)) if i in kept_idx], dropped, resized


def submit_stop(symbol: str, qty: int, stop_price: float) -> str:
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json={
        "symbol": symbol,
        "qty": str(qty),
        "side": "sell",
        "type": "stop",
        "stop_price": str(round(stop_price, 2)),
        "time_in_force": "gtc",
    }, timeout=15)
    r.raise_for_status()
    return r.json()["id"]


def submit_market_sell(symbol: str, qty: int) -> str:
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json={
        "symbol": symbol,
        "qty": str(qty),
        "side": "sell",
        "type": "market",
        "time_in_force": "day",
    }, timeout=15)
    r.raise_for_status()
    return r.json()["id"]


def compute_stage(entry_price: float, highest_price: float, current_stage: str):
    """Returns (stage, target_stop). Stage only ever advances forward."""
    if current_stage == "tight":
        candidate = highest_price * (1 - TIGHT_PCT)
        if candidate >= entry_price:
            return "breakeven_hold", entry_price
        return "tight", candidate
    if current_stage == "breakeven_hold":
        if highest_price >= entry_price * BREAKEVEN_MULT:
            return "wide_trail", highest_price * (1 - WIDE_PCT)
        return "breakeven_hold", entry_price
    return "wide_trail", highest_price * (1 - WIDE_PCT)


def absorb_new_lots(state: dict, open_orders: list):
    """Find filled buy orders not yet represented as a tracked lot, create
    lot entries for them, and cancel their paired native trailing_stop
    (placed by buy_consistent_losers.py) so this manager takes over."""
    tracked_ids = {lot["buy_order_id"] for lot in state["lots"]}
    start = (date.today() - timedelta(days=NEW_LOT_LOOKBACK_DAYS)).isoformat()
    r = requests.get(f"{BASE_URL}/orders", headers=HEADERS, params={
        "status": "closed", "side": "buy", "limit": 200, "direction": "desc", "after": f"{start}T00:00:00Z"
    }, timeout=20)
    r.raise_for_status()
    recent_buys = [o for o in r.json() if o["status"] == "filled"]

    trailing_open = [o for o in open_orders if o["type"] == "trailing_stop" and o["side"] == "sell"]

    for o in recent_buys:
        if o["id"] in tracked_ids:
            continue
        symbol = o["symbol"]
        qty = int(float(o["filled_qty"]))  # buys are whole-share since the 2026-08-27 fix
        if qty < 1:
            continue
        entry_price = float(o["filled_avg_price"])
        filled_at = parse_iso(o["filled_at"])

        # find its paired native trailing_stop: same symbol/qty, created shortly after this fill
        match = None
        for t in trailing_open:
            if t["symbol"] != symbol or int(float(t["qty"])) != qty:
                continue
            created = parse_iso(t["created_at"])
            if 0 <= (created - filled_at).total_seconds() <= 900:
                match = t
                break

        if not match:
            log.warning(f"  New buy {symbol} qty={qty} (order {o['id'][:8]}) has no matching native "
                         f"trailing_stop yet -- skipping absorption this cycle, will retry next run.")
            continue

        try:
            can_sell = sellable_qty(symbol, ignore_order_id=match["id"])
            cancel_order(match["id"])
        except Exception as e:
            log.error(f"  Could not cancel native stop {match['id'][:8]} for {symbol}: {e}")
            continue
        trailing_open.remove(match)
        if can_sell < 1:
            # The buy didn't leave shares to protect -- e.g. it covered an
            # existing short (MGM, 2026-09-15). Its native stop would have
            # sold shares that aren't there, i.e. re-opened the short.
            log.warning(f"  New buy {symbol} qty={qty} left no sellable shares (it likely covered a "
                         f"short); canceled its native stop and not tracking it.")
            continue
        if can_sell < qty:
            log.warning(f"  New buy {symbol}: only {can_sell} of {qty} shares sellable -- tracking {can_sell}.")
            qty = can_sell

        try:
            current_price = get_latest_price(symbol)
        except Exception as e:
            log.error(f"  Could not fetch price for new lot {symbol}: {e}")
            current_price = entry_price

        lot = {
            "buy_order_id":  o["id"],
            "symbol":        symbol,
            "qty":           qty,
            "entry_price":   entry_price,
            "highest_price": max(entry_price, current_price),
            "stage":         "tight",
            "stop_order_id": None,
            "stop_price":    None,
        }
        stage, target = compute_stage(lot["entry_price"], lot["highest_price"], lot["stage"])
        lot["stage"] = stage
        try:
            order_id = submit_stop(symbol, qty, target)
            lot["stop_order_id"] = order_id
            lot["stop_price"] = round(target, 2)
            log.info(f"  Absorbed new lot {symbol} qty={qty} entry=${entry_price:.2f} -> "
                      f"stage={stage} stop=${target:.2f} (order {order_id[:8]})")
        except Exception as e:
            log.error(f"  Could not place initial stop for new lot {symbol}: {e}")
            continue
        state["lots"].append(lot)


def reconcile_uncovered_positions(state: dict):
    """Adopt any held whole shares that no resting stop order protects.

    Tracked state can drift from broker reality -- a run that dies partway,
    a stop canceled out of band, a lot dropped as closed while shares
    remained. Whatever the cause, the symptom is the dangerous one: real
    shares sitting with no stop and nothing watching them. Rather than
    trusting the state file to be complete, this compares it against actual
    positions every run and anchors any orphaned shares at the current
    price (the same fresh-anchor treatment Nathaniel chose for the
    2026-08-29 migration). Self-healing beats hand-patching, since the
    whole failure mode here is state silently diverging from the broker.

    Coverage is measured from OPEN STOP ORDERS at the broker, not from the
    state file. Shares reserved by a stop this script hasn't absorbed yet
    (buy_consistent_losers.py's native trailing stop on a fresh buy) are
    already protected, and Alpaca rejects a second sell order against the
    same reserved shares with a 403 -- so counting tracked lots instead of
    real orders both misreads safety and spams failures."""
    positions = get_positions()
    covered = {}
    for o in get_open_orders():
        # any open sell reserves shares -- counting only stops would let a
        # pending market sell plus a new orphan stop oversell the position
        if o["side"] == "sell":
            remaining = float(o["qty"]) - float(o.get("filled_qty") or 0)
            covered[o["symbol"]] = covered.get(o["symbol"], 0) + math.ceil(remaining)

    for symbol, p in positions.items():
        whole = math.floor(float(p["qty"]))   # negative for a short -> never adopted
        orphan = whole - covered.get(symbol, 0)
        if orphan < 1:
            continue
        try:
            current_price = get_latest_price(symbol)
            stage, target = compute_stage(current_price, current_price, "tight")
            order_id = submit_stop(symbol, orphan, target)
        except Exception as e:
            log.error(f"  ORPHAN {symbol} qty={orphan} has no stop and could not be protected: {e}")
            continue
        state["lots"].append({
            "buy_order_id":  f"orphan-{symbol}-{date.today().isoformat()}",
            "symbol":        symbol,
            "qty":           orphan,
            "entry_price":   round(current_price, 2),
            "highest_price": round(current_price, 2),
            "stage":         stage,
            "stop_order_id": order_id,
            "stop_price":    round(target, 2),
        })
        log.info(f"  ADOPTED orphan {symbol} qty={orphan} (untracked, unprotected) -> "
                  f"anchored at ${current_price:.2f}, stop ${target:.2f}")


def run():
    log.info("=" * 90)
    log.info(f"DYNAMIC STOP MANAGER  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    log.info("=" * 90)

    state = load_state(STATE_FILE)
    if "lots" not in state:
        state["lots"] = []
    # Persist whatever progress was made even if a later step raises. The
    # original version only saved at the very end, so an exception anywhere
    # threw away every reconciliation and stop replacement from that run --
    # which is exactly what a fromisoformat crash did for three days, while
    # the log kept re-reporting the same already-closed lots each cycle.
    try:
        _run_inner(state)
    finally:
        save_state(STATE_FILE, state)
        log.info(f"  {len(state['lots'])} lots actively managed.")
    # Outside the finally on purpose: scripts/heartbeat.py treats a run with
    # no "Run complete." as crashed, so it must not be logged on an exception.
    log.info("Run complete.\n")


def _run_inner(state: dict):
    open_orders = get_open_orders()
    open_order_ids = {o["id"] for o in open_orders}

    # 1) reconcile lots whose stop has fired (no longer open) -- they're closed
    still_active = []
    for lot in state["lots"]:
        if lot["stop_order_id"] and lot["stop_order_id"] not in open_order_ids:
            try:
                o = requests.get(f"{BASE_URL}/orders/{lot['stop_order_id']}", headers=HEADERS, timeout=15).json()
                exit_price = float(o.get("filled_avg_price") or lot["stop_price"])
                pnl = (exit_price - lot["entry_price"]) * lot["qty"]
                log.info(f"  CLOSED {lot['symbol']} qty={lot['qty']} entry=${lot['entry_price']:.2f} "
                          f"exit=${exit_price:.2f} stage={lot['stage']} pnl=${pnl:.2f}")
            except Exception as e:
                log.warning(f"  {lot['symbol']} stop order {lot['stop_order_id'][:8]} no longer open "
                             f"(assumed filled) -- {e}")
            continue  # drop from active tracking
        still_active.append(lot)
    state["lots"] = still_active

    # 1b) holdings are the truth: drop/shrink lots for shares no longer held.
    # Step 1 only retires lots whose stop order id is known and gone; a lot
    # whose replacement failed (stop_order_id None) or whose shares left by
    # another sell was never retired, and kept trying to sell. That drift
    # reached 26 ghost lots and produced the KBR/MGM shorts.
    held_whole = {s: math.floor(float(p["qty"])) for s, p in get_positions().items() if float(p["qty"]) >= 1}
    kept, dropped, resized = allocate_lots_to_holdings(state["lots"], held_whole, open_order_ids)
    for lot in dropped:
        log.warning(f"  RETIRED ghost lot {lot['symbol']} qty={lot['qty']} -- broker holds no untracked "
                     f"shares for it (sold elsewhere or never held)")
        if lot.get("stop_order_id") in open_order_ids:
            try:
                cancel_order(lot["stop_order_id"])
            except Exception as e:
                log.error(f"  Could not cancel stop {lot['stop_order_id'][:8]} of retired {lot['symbol']} lot: {e}")
    for sym, old_qty, new_qty in resized:
        log.warning(f"  RESIZED {sym} lot {old_qty} -> {new_qty} shares to match holdings")
    state["lots"] = kept

    # 2) absorb any new buys not yet tracked
    absorb_new_lots(state, open_orders)
    open_orders = get_open_orders()  # refresh after absorption

    # 3) re-evaluate every active lot
    price_cache = {}
    reevaluated = []
    for lot in state["lots"]:
        symbol = lot["symbol"]
        if symbol not in price_cache:
            try:
                price_cache[symbol] = get_latest_price(symbol)
            except Exception as e:
                log.error(f"  Could not fetch price for {symbol}: {e}")
                reevaluated.append(lot)
                continue
        current_price = price_cache[symbol]
        lot["highest_price"] = max(lot["highest_price"], current_price)

        new_stage, target = compute_stage(lot["entry_price"], lot["highest_price"], lot["stage"])
        target = round(target, 2)
        # A lot with no resting order is unprotected and must ALWAYS be
        # re-placed, even when the computed target is unchanged -- otherwise
        # a lot whose replacement failed on an earlier run stays naked
        # forever, because "nothing changed" looks like "nothing to do".
        changed = (lot["stop_order_id"] is None
                   or new_stage != lot["stage"]
                   or lot["stop_price"] is None
                   or abs(target - lot["stop_price"]) >= 0.01)

        if not changed:
            reevaluated.append(lot)
            time.sleep(0.15)
            continue

        if target >= current_price:
            # The new stage's target has already been passed by price (e.g. a
            # stage transition computed off a stale highest jumps the target
            # above where price has since pulled back to). A resting stop
            # order at or above the current price is invalid -- and more to
            # the point, a real trailing stop would already have fired here.
            # Close it now instead of silently leaving the lot unprotected.
            if lot["stop_order_id"]:
                try:
                    cancel_order(lot["stop_order_id"])
                except Exception:
                    pass  # may already be gone/filled -- fall through and try to sell
            try:
                # This is the path that shorted MGM on 2026-09-14: its stop had
                # filled 16 min earlier and this sold the same 12 shares again.
                qty = min(lot["qty"], sellable_qty(symbol, ignore_order_id=lot["stop_order_id"]))
                if qty < 1:
                    log.warning(f"  {symbol} lot qty={lot['qty']} has no sellable shares left -- "
                                 f"already closed elsewhere; retiring it without selling.")
                    time.sleep(0.15)
                    continue
                sell_id = submit_market_sell(symbol, qty)
                lot["qty"] = qty
                pnl = (current_price - lot["entry_price"]) * lot["qty"]
                log.info(f"  STOPPED OUT {symbol} qty={lot['qty']} entry=${lot['entry_price']:.2f} "
                          f"~exit=${current_price:.2f} stage={lot['stage']}->{new_stage} "
                          f"(target ${target} had already been passed) pnl=~${pnl:.2f} order {sell_id[:8]}")
            except Exception as e:
                log.error(f"  URGENT: {symbol} stage triggered (target ${target} >= price ${current_price}) "
                           f"but could not close the lot: {e} -- leaving unprotected, will retry next run.")
                reevaluated.append(lot)
            time.sleep(0.15)
            continue

        if lot["stop_order_id"]:
            try:
                cancel_order(lot["stop_order_id"])
            except Exception as e:
                log.error(f"  Could not cancel old stop for {symbol}: {e}")
                reevaluated.append(lot)
                time.sleep(0.15)
                continue
        try:
            qty = min(lot["qty"], sellable_qty(symbol, ignore_order_id=lot["stop_order_id"]))
            if qty < 1:
                log.warning(f"  {symbol} lot qty={lot['qty']} has no sellable shares left -- "
                             f"already closed elsewhere; retiring it.")
                time.sleep(0.15)
                continue
            if qty < lot["qty"]:
                log.warning(f"  {symbol} lot shrunk {lot['qty']} -> {qty} to match sellable shares")
                lot["qty"] = qty
            order_id = submit_stop(symbol, qty, target)
        except Exception as e:
            log.error(f"  URGENT: {symbol} old stop canceled but replacement failed: {e} -- "
                       f"leaving unprotected, will retry next run.")
            lot["stop_order_id"] = None
            reevaluated.append(lot)
            time.sleep(0.15)
            continue

        log.info(f"  {symbol:6s} qty={lot['qty']:>4d} entry=${lot['entry_price']:.2f} "
                  f"high=${lot['highest_price']:.2f}  {lot['stage']}->{new_stage}  "
                  f"stop ${lot['stop_price']}->${target}")
        lot["stage"] = new_stage
        lot["stop_price"] = target
        lot["stop_order_id"] = order_id
        reevaluated.append(lot)
        time.sleep(0.15)

    state["lots"] = reevaluated

    # 4) LAST: adopt any shares still left without a resting stop. Runs after
    # step 3 so lots whose stop was merely stale have already been re-placed
    # and are counted as covered -- otherwise their shares would look
    # orphaned here and get a duplicate lot.
    reconcile_uncovered_positions(state)


if __name__ == "__main__":
    run()
