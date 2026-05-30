#!/usr/bin/env python3
"""
Market Regime Detector
=======================
Uses SPY's 200-day and 50-day moving averages to classify the current
market regime. Used by wheel strategies to decide whether to sell new puts.

Regime States:
  BULL    — SPY above 200-day SMA → sell puts normally
  CAUTION — SPY within 2% below 200-day SMA → sell at half contracts
  BEAR    — SPY more than 2% below 200-day SMA → pause, do not sell puts

Logic:
  A bear market (SPY below 200-day SMA) historically produces the worst
  outcomes for cash-secured puts. Selling puts into a falling market risks
  assignment at elevated prices that keep falling. Pausing in BEAR regime
  preserves cash and avoids getting stuck holding depreciating shares.

  CAUTION zone gives a buffer — market may be recovering or just dipping.
  Half contracts reduce exposure without fully stopping income.
"""

import json
import sys
import requests
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR    = Path(__file__).parent.parent
CONFIG_FILE = BASE_DIR / "config" / "alpaca_credentials.json"
STATE_FILE  = BASE_DIR / "strategies" / "regime_state.json"

with open(CONFIG_FILE) as f:
    creds = json.load(f)

DATA_URL = "https://data.alpaca.markets/v2"
HEADERS  = {
    "APCA-API-KEY-ID":     creds["api_key"],
    "APCA-API-SECRET-KEY": creds["api_secret"],
}

# Regime thresholds
BEAR_THRESHOLD    = -0.02   # >2% below 200-day SMA → BEAR
CAUTION_THRESHOLD =  0.00   # 0–2% below 200-day SMA → CAUTION
# Above 200-day SMA → BULL


def get_regime(verbose: bool = True) -> dict:
    """
    Fetches SPY daily bars, calculates 200/50-day SMAs,
    returns a regime dict.
    """
    # Fetch 220 days of SPY daily bars (need 200+ for SMA200)
    from datetime import date, timedelta
    start = (date.today() - timedelta(days=320)).isoformat()  # extra buffer for weekends/holidays
    r = requests.get(
        f"{DATA_URL}/stocks/SPY/bars",
        headers=HEADERS,
        params={"timeframe": "1Day", "limit": 220, "start": start, "feed": "sip"},
        timeout=15,
    )
    r.raise_for_status()
    bars = r.json().get("bars", [])

    if len(bars) < 200:
        return {
            "regime":         "UNKNOWN",
            "spy_price":      None,
            "sma200":         None,
            "sma50":          None,
            "pct_vs_200":     None,
            "message":        f"Insufficient data ({len(bars)} bars, need 200+)",
            "timestamp":      datetime.now().isoformat(),
        }

    closes = [float(b["c"]) for b in bars]
    spy_price = closes[-1]
    sma200    = round(sum(closes[-200:]) / 200, 2)
    sma50     = round(sum(closes[-50:])  / 50,  2)
    pct_vs_200 = (spy_price - sma200) / sma200  # positive = above, negative = below

    if pct_vs_200 >= CAUTION_THRESHOLD:
        regime  = "BULL"
        message = (f"SPY ${spy_price:.2f} is {pct_vs_200*100:+.1f}% above 200-day SMA "
                   f"(${sma200:.2f}) — proceed normally")
    elif pct_vs_200 >= BEAR_THRESHOLD:
        regime  = "CAUTION"
        message = (f"SPY ${spy_price:.2f} is {pct_vs_200*100:+.1f}% below 200-day SMA "
                   f"(${sma200:.2f}) — sell at half contracts")
    else:
        regime  = "BEAR"
        message = (f"SPY ${spy_price:.2f} is {pct_vs_200*100:+.1f}% below 200-day SMA "
                   f"(${sma200:.2f}) — pause wheel, do not sell new puts")

    result = {
        "regime":     regime,
        "spy_price":  spy_price,
        "sma200":     sma200,
        "sma50":      sma50,
        "pct_vs_200": round(pct_vs_200 * 100, 2),
        "message":    message,
        "timestamp":  datetime.now().isoformat(),
    }

    # Save state
    with open(STATE_FILE, "w") as f:
        json.dump(result, f, indent=2)

    if verbose:
        icon = {"BULL": "🟢", "CAUTION": "🟡", "BEAR": "🔴"}.get(regime, "⚪")
        print(f"{icon}  MARKET REGIME: {regime}")
        print(f"   SPY:     ${spy_price:.2f}")
        print(f"   SMA 200: ${sma200:.2f}  ({pct_vs_200*100:+.2f}%)")
        print(f"   SMA  50: ${sma50:.2f}")
        print(f"   → {message}")

    return result


def regime_contracts_multiplier(base_contracts: int, regime: str) -> int:
    """
    Adjusts number of contracts to sell based on regime.
      BULL    → full contracts
      CAUTION → half (minimum 1)
      BEAR    → 0 (pause)
    """
    if regime == "BULL":
        return base_contracts
    elif regime == "CAUTION":
        return max(1, base_contracts // 2)
    else:  # BEAR
        return 0


if __name__ == "__main__":
    get_regime(verbose=True)
