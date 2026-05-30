#!/usr/bin/env python3
"""
Wheel Candidate Scanner
=======================
Scans live options quotes for potential wheel strategy candidates.
Runs once at market open, saves results to logs/wheel_candidate_scan.log
"""

import json
import sys
import requests
import logging
from datetime import date, timedelta
from pathlib import Path

BASE_DIR    = Path(__file__).parent.parent
CONFIG_FILE = BASE_DIR / "config" / "alpaca_credentials.json"
LOG_FILE    = BASE_DIR / "logs" / "wheel_candidate_scan.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
log = logging.getLogger(__name__)

with open(CONFIG_FILE) as f:
    creds = json.load(f)
BASE_URL = creds["endpoint"]
DATA_URL = "https://data.alpaca.markets"
HEADERS  = {
    "APCA-API-KEY-ID":     creds["api_key"],
    "APCA-API-SECRET-KEY": creds["api_secret"],
}

CANDIDATES = ["UBER", "F", "SMCI", "DKNG", "SOFI", "RIVN", "MARA"]
DTE_MIN    = 14
DTE_MAX    = 35

def market_is_open():
    r = requests.get(f"{BASE_URL}/clock", headers=HEADERS, timeout=10)
    return r.json().get("is_open", False)

def scan():
    if not market_is_open():
        log.info("Market not open yet — exiting.")
        return

    today   = date.today()
    exp_min = (today + timedelta(days=DTE_MIN)).strftime("%Y-%m-%d")
    exp_max = (today + timedelta(days=DTE_MAX)).strftime("%Y-%m-%d")

    log.info("=" * 65)
    log.info(f"WHEEL CANDIDATE SCAN  |  {today}")
    log.info("=" * 65)

    results = []
    for sym in CANDIDATES:
        try:
            r = requests.get(f"{DATA_URL}/v2/stocks/{sym}/trades/latest",
                             headers=HEADERS, timeout=10)
            price = float(r.json()["trade"]["p"])
            target = round(price * 0.90, 2)

            r2 = requests.get(f"{BASE_URL}/options/contracts", headers=HEADERS, params={
                "underlying_symbol": sym, "type": "put",
                "strike_price_gte":  str(round(target * 0.95, 2)),
                "strike_price_lte":  str(round(target * 1.05, 2)),
                "expiration_date_gte": exp_min,
                "expiration_date_lte": exp_max,
                "limit": 20,
            }, timeout=10)
            contracts = r2.json().get("option_contracts", [])

            best = None
            best_dist = 999
            for c in contracts:
                strike = float(c["strike_price"])
                snap   = requests.get(
                    f"{DATA_URL}/v1beta1/options/snapshots/{sym}",
                    headers=HEADERS, params={"symbols": c["symbol"]}, timeout=10
                ).json()
                quote = snap.get("snapshots", {}).get(c["symbol"], {}).get("latestQuote", {})
                bid = quote.get("bp", 0)
                ask = quote.get("ap", 0)
                if bid <= 0:
                    continue
                mid  = round((bid + ask) / 2, 2)
                dte  = (date.fromisoformat(c["expiration_date"]) - today).days
                dist = abs(strike - target)
                if dist < best_dist:
                    best_dist = dist
                    best = {
                        "symbol":       sym,
                        "stock_price":  price,
                        "strike":       strike,
                        "otm_pct":      round((strike - price) / price * 100, 1),
                        "expiration":   c["expiration_date"],
                        "dte":          dte,
                        "bid":          bid,
                        "ask":          ask,
                        "mid":          mid,
                        "spread_pct":   round((ask - bid) / mid * 100, 1) if mid > 0 else 999,
                        "collateral":   round(strike * 100, 2),
                        "annual_yield": round((mid / strike) * (365 / dte) * 100, 1),
                        "contract":     c["symbol"],
                    }

            if best:
                results.append(best)
                log.info(
                    f"  {best['symbol']:6s}  stock=${best['stock_price']:>7.2f}  "
                    f"strike=${best['strike']:>6.2f} ({best['otm_pct']:+.1f}%)  "
                    f"exp={best['expiration']} ({best['dte']}d)  "
                    f"mid=${best['mid']:.2f}  spread={best['spread_pct']:.0f}%  "
                    f"collateral=${best['collateral']:,.0f}  "
                    f"annualized={best['annual_yield']}%"
                )
            else:
                log.info(f"  {sym:6s}  stock=${price:.2f}  -- no liquid puts found --")

        except Exception as e:
            log.error(f"  {sym}: ERROR {e}")

    if results:
        log.info("")
        log.info("=== RANKED BY ANNUALIZED YIELD ===")
        for r in sorted(results, key=lambda x: -x["annual_yield"]):
            log.info(
                f"  {r['symbol']:6s}  {r['annual_yield']:>5.1f}% ann  "
                f"${r['mid']:.2f}/sh credit  "
                f"${r['collateral']:,.0f} collateral  "
                f"{r['spread_pct']:.0f}% spread  "
                f"{r['dte']}d to exp"
            )

    # Save JSON for easy reading later
    out = BASE_DIR / "logs" / "wheel_scan_latest.json"
    with open(out, "w") as f:
        json.dump({"date": str(today), "results": results}, f, indent=2)
    log.info(f"\nResults saved to {out}")
    log.info("=" * 65)

if __name__ == "__main__":
    scan()
