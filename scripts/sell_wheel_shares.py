#!/usr/bin/env python3
"""
One-off: sell the leftover options-wheel share lots (500 each of DKNG, IONQ, MARA,
SMCI, SOFI) on the Alpaca PAPER account.

    python scripts\\sell_wheel_shares.py                 # dry run: shows the plan, sends nothing
    python scripts\\sell_wheel_shares.py --execute       # places the sells after you type SELL
    python scripts\\sell_wheel_shares.py --execute --close-calls
                                                        # also buys back short calls first
                                                        # (needed to sell SOFI before its call expires)

Safety rules built in:
  - Refuses to run against anything but the paper endpoint.
  - Sells exactly the expected lot (500) per symbol and never more, so any copy-trader
    shares in the same ticker are left alone. If the broker holds LESS than 500, or
    already has an open sell order on the symbol, that symbol is skipped.
  - A short CALL on a symbol blocks selling its shares (they would become uncovered and
    Alpaca rejects it). The symbol is skipped unless --close-calls buys the call back.
  - Market orders are only sent while the market is open.
  - Idempotent: re-run it any time; symbols already sold simply show as "nothing to sell".
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent.parent
EXPECTED = {"DKNG": 500, "IONQ": 500, "MARA": 500, "SMCI": 500, "SOFI": 500}
OCC_RE = re.compile(r"^([A-Z.]+)(\d{6})([CP])(\d{8})$")
MAX_CALL_BUYBACK_PRICE = 0.25   # per share; refuse to buy a call back above this

with open(BASE_DIR / "config" / "alpaca_credentials.json") as f:
    _creds = json.load(f)
BASE_URL = _creds["endpoint"].rstrip("/")
DATA_URL = "https://data.alpaca.markets"
HEADERS = {
    "APCA-API-KEY-ID":     _creds["api_key"],
    "APCA-API-SECRET-KEY": _creds["api_secret"],
}

if "paper-api.alpaca.markets" not in BASE_URL:
    sys.exit(f"Refusing to run: endpoint is {BASE_URL}, not the paper account.")


def api(method: str, path: str, **kw):
    r = requests.request(method, BASE_URL + path, headers=HEADERS, timeout=20, **kw)
    r.raise_for_status()
    return r.json() if r.text else None


def short_calls_on(symbol: str, positions: list) -> list:
    out = []
    for p in positions:
        m = OCC_RE.match(p["symbol"])
        if m and m.group(1) == symbol and m.group(3) == "C" and float(p["qty"]) < 0:
            out.append(p)
    return out


def plan_sells(positions: list, open_orders: list, close_calls: bool) -> list:
    """Pure planning: one dict per symbol -> {symbol, qty, action, reason, calls}."""
    by_symbol = {p["symbol"]: p for p in positions if p.get("asset_class") != "us_option"}
    plan = []
    for symbol, want in EXPECTED.items():
        pos = by_symbol.get(symbol)
        held = float(pos["qty"]) if pos else 0.0
        item = {"symbol": symbol, "qty": want, "action": "sell", "reason": "", "calls": []}
        if held <= 0:
            item.update(action="skip", reason="nothing to sell (already gone)")
        elif held < want:
            item.update(action="skip", reason=f"broker holds {held:g}, expected {want} -- check manually")
        elif any(o["symbol"] == symbol and o["side"] == "sell" for o in open_orders):
            item.update(action="skip", reason="an open sell order already reserves shares -- cancel it first")
        else:
            calls = short_calls_on(symbol, positions)
            item["calls"] = calls
            if calls and not close_calls:
                names = ", ".join(c["symbol"] for c in calls)
                item.update(action="skip", reason=f"short call open ({names}); selling would leave it "
                                                  f"uncovered -- rerun with --close-calls or after it expires")
        plan.append(item)
    return plan


def wait_for(order_id: str, seconds: int) -> dict:
    deadline = time.time() + seconds
    order = api("GET", f"/orders/{order_id}")
    while order["status"] not in ("filled", "canceled", "expired", "rejected") and time.time() < deadline:
        time.sleep(3)
        order = api("GET", f"/orders/{order_id}")
    return order


def buy_back_call(call: dict) -> bool:
    occ, qty = call["symbol"], abs(int(float(call["qty"])))
    snap = requests.get(f"{DATA_URL}/v1beta1/options/snapshots", headers=HEADERS, timeout=20,
                        params={"symbols": occ, "feed": "indicative"}).json()
    ask = (snap.get("snapshots", {}).get(occ, {}).get("latestQuote", {}) or {}).get("ap") or 0
    price = round(ask if ask > 0 else 0.05, 2)
    if price > MAX_CALL_BUYBACK_PRICE:
        print(f"    {occ}: ask ${price:.2f} is above the ${MAX_CALL_BUYBACK_PRICE:.2f} safety cap -- not buying back.")
        return False
    order = api("POST", "/orders", json={"symbol": occ, "qty": str(qty), "side": "buy", "type": "limit",
                                         "limit_price": f"{price:.2f}", "time_in_force": "day"})
    print(f"    buy-to-close {qty} x {occ} @ limit ${price:.2f} ...")
    order = wait_for(order["id"], 90)
    if order["status"] == "filled":
        print(f"    filled @ ${order['filled_avg_price']}")
        return True
    print(f"    not filled ({order['status']}) -- canceling.")
    if order["status"] not in ("canceled", "expired", "rejected"):
        api("DELETE", f"/orders/{order['id']}")
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true", help="actually place orders (default is a dry run)")
    ap.add_argument("--close-calls", action="store_true", help="buy back short calls on these symbols first")
    ap.add_argument("--yes", action="store_true", help="skip the typed SELL confirmation")
    args = ap.parse_args()

    clock = api("GET", "/clock")
    positions = api("GET", "/positions")
    open_orders = api("GET", "/orders", params={"status": "open", "limit": 500})
    plan = plan_sells(positions, open_orders, args.close_calls)

    px = {p["symbol"]: float(p["current_price"]) for p in positions}
    print(f"\nMarket open: {clock['is_open']}   Endpoint: {BASE_URL}\n")
    print(f"{'SYMBOL':8s} {'ACTION':6s} {'QTY':>5s} {'~PROCEEDS':>11s}  NOTE")
    total = 0.0
    for it in plan:
        proceeds = it["qty"] * px.get(it["symbol"], 0) if it["action"] == "sell" else 0
        total += proceeds
        note = it["reason"] or ("buys back " + ", ".join(c["symbol"] for c in it["calls"]) + " first" if it["calls"] else "")
        print(f"{it['symbol']:8s} {it['action']:6s} {it['qty']:>5d} {proceeds:>11,.0f}  {note}")
    print(f"{'':8s} {'':6s} {'':>5s} {total:>11,.0f}  (estimate at current prices)\n")

    to_sell = [it for it in plan if it["action"] == "sell"]
    if not args.execute:
        print("DRY RUN -- nothing was sent. Add --execute to place these orders.")
        return 0
    if not to_sell:
        print("Nothing to sell.")
        return 0
    if not clock["is_open"]:
        print("Market is closed -- not sending market orders. Run again during market hours.")
        return 1
    if not args.yes and input("Type SELL to place these orders: ").strip() != "SELL":
        print("Cancelled -- nothing was sent.")
        return 1

    for it in to_sell:
        sym = it["symbol"]
        print(f"\n{sym}:")
        blocked = False
        for call in it["calls"]:
            if not buy_back_call(call):
                blocked = True
        if blocked:
            print(f"  SKIPPED {sym}: short call still open, shares not sold.")
            continue
        try:
            order = api("POST", "/orders", json={"symbol": sym, "qty": str(it["qty"]), "side": "sell",
                                                 "type": "market", "time_in_force": "day"})
        except requests.HTTPError as e:
            print(f"  ORDER REJECTED: {e.response.text[:200]}")
            continue
        order = wait_for(order["id"], 60)
        if order["status"] == "filled":
            print(f"  SOLD {order['filled_qty']} {sym} @ ${float(order['filled_avg_price']):.2f}")
        else:
            print(f"  order status: {order['status']} (id {order['id']}) -- check the dashboard.")

    print("\nDone. Re-run (dry run) to confirm nothing is left.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
