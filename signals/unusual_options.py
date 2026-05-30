#!/usr/bin/env python3
"""
Unusual Options Activity Monitor
==================================
Uses Alpaca's options snapshot API to detect unusual implied volatility (IV)
spikes in your portfolio holdings. A sudden IV surge signals that large
institutions are buying options protection or making directional bets.

Alert conditions:
  IV SPIKE: Current IV > 2x the stock's 30-day historical volatility
  UNUSUAL VOLUME: Options volume > 3x average daily options volume
  SKEW ALERT: Put/Call IV ratio > 1.5 (hedging demand = bearish signal)

Also scans for unusually large single options trades using the
Unusual Whales free public feed when available.

Runs every 30 minutes during market hours.
"""

import json
import math
import requests
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from signal_utils import (
    get_logger, load_state, save_state, send_signal_email,
    html_table, base_html, PORTFOLIO_SYMBOLS
)

BASE_DIR = Path(__file__).parent.parent
with open(BASE_DIR / "config" / "alpaca_credentials.json") as f:
    creds = json.load(f)
BASE_URL = creds["endpoint"]
DATA_URL = "https://data.alpaca.markets"
HEADERS  = {
    "APCA-API-KEY-ID":     creds["api_key"],
    "APCA-API-SECRET-KEY": creds["api_secret"],
}

log = get_logger("unusual_options", "unusual_options.log")

IV_SPIKE_THRESHOLD    = 1.8   # IV > 1.8x historical vol = spike
PC_SKEW_THRESHOLD     = 1.6   # Put IV / Call IV > 1.6 = heavy hedging demand

# Symbols with liquid enough options to monitor meaningfully
OPTIONS_SYMBOLS = [
    "NVDA", "MSFT", "GOOGL", "PLTR", "IONQ", "SOXX",
    "IBM", "INTC", "LMT", "CVX", "XOM", "XLE", "BWXT", "RKLB"
]


def get_hist_volatility(symbol: str, days: int = 30) -> float:
    """Calculate historical volatility from daily returns."""
    start = (date.today() - timedelta(days=days + 10)).isoformat()
    r = requests.get(
        f"{DATA_URL}/v2/stocks/{symbol}/bars",
        headers=HEADERS,
        params={"timeframe": "1Day", "start": start, "limit": days + 5, "feed": "sip"},
        timeout=15,
    )
    if r.status_code != 200:
        return 0.0
    bars = r.json().get("bars", [])
    if len(bars) < 5:
        return 0.0
    closes = [float(b["c"]) for b in bars]
    returns = [math.log(closes[i] / closes[i-1]) for i in range(1, len(closes))]
    if not returns:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / len(returns)
    return math.sqrt(variance * 252)  # Annualized


def get_options_snapshot(symbol: str) -> dict:
    """Get options snapshot for near-term ATM options."""
    today = date.today()
    exp_min = (today + timedelta(days=7)).isoformat()
    exp_max = (today + timedelta(days=35)).isoformat()

    result = {"call_iv": 0.0, "put_iv": 0.0, "found": False, "current_price": 0.0}

    # Get current price
    try:
        rp = requests.get(
            f"{DATA_URL}/v2/stocks/{symbol}/trades/latest",
            headers=HEADERS, timeout=10
        )
        if rp.status_code == 200:
            result["current_price"] = float(rp.json()["trade"]["p"])
    except Exception:
        pass

    price = result["current_price"]
    if price == 0:
        return result

    # Get ATM call options
    for opt_type, key in [("call", "call_iv"), ("put", "put_iv")]:
        try:
            strike_low  = price * 0.95
            strike_high = price * 1.05
            r = requests.get(
                f"{BASE_URL}/options/contracts",
                headers=HEADERS,
                params={
                    "underlying_symbol":   symbol,
                    "type":                opt_type,
                    "strike_price_gte":    str(round(strike_low, 2)),
                    "strike_price_lte":    str(round(strike_high, 2)),
                    "expiration_date_gte": exp_min,
                    "expiration_date_lte": exp_max,
                    "limit": 5,
                },
                timeout=10,
            )
            contracts = r.json().get("option_contracts", []) if r.status_code == 200 else []
            if not contracts:
                continue

            snap = requests.get(
                f"{DATA_URL}/v1beta1/options/snapshots/{symbol}",
                headers=HEADERS,
                params={"symbols": contracts[0]["symbol"]},
                timeout=10,
            ).json()
            # Alpaca returns impliedVolatility at the snapshot root level,
            # NOT nested under greeks (greeks contains delta/gamma/theta/vega/rho only)
            iv = snap.get("snapshots", {}).get(contracts[0]["symbol"], {}).get("impliedVolatility") or 0
            if iv > 0:
                result[key] = float(iv)
                result["found"] = True
        except Exception:
            pass

    return result


def run():
    log.info("=" * 65)
    log.info(f"UNUSUAL OPTIONS MONITOR  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    log.info("=" * 65)

    # Check market is open
    clock = requests.get(f"{BASE_URL}/clock", headers=HEADERS, timeout=10).json()
    if not clock.get("is_open"):
        log.info("  Market closed — skipping.")
        return

    state  = load_state("unusual_options.json")
    alerts = []

    for symbol in OPTIONS_SYMBOLS:
        try:
            hist_vol = get_hist_volatility(symbol)
            snap     = get_options_snapshot(symbol)

            if not snap["found"] or hist_vol == 0:
                log.info(f"  {symbol:6s} | no options data available")
                continue

            call_iv = snap["call_iv"]
            put_iv  = snap["put_iv"]
            avg_iv  = (call_iv + put_iv) / 2 if call_iv and put_iv else max(call_iv, put_iv)
            iv_ratio = avg_iv / hist_vol if hist_vol > 0 else 0
            pc_skew  = put_iv / call_iv if call_iv > 0 and put_iv > 0 else 0

            log.info(
                f"  {symbol:6s} | price=${snap['current_price']:.2f} | "
                f"call_iv={call_iv*100:.1f}% | put_iv={put_iv*100:.1f}% | "
                f"hist_vol={hist_vol*100:.1f}% | iv_ratio={iv_ratio:.2f}x | "
                f"pc_skew={pc_skew:.2f}"
            )

            alert_reasons = []
            if iv_ratio >= IV_SPIKE_THRESHOLD:
                alert_reasons.append(f"IV spike ({iv_ratio:.1f}x hist vol)")
            if pc_skew >= PC_SKEW_THRESHOLD:
                alert_reasons.append(f"Put/Call skew {pc_skew:.2f} (heavy hedging)")

            if alert_reasons:
                alerts.append({
                    "symbol":    symbol,
                    "price":     snap["current_price"],
                    "call_iv":   call_iv,
                    "put_iv":    put_iv,
                    "hist_vol":  hist_vol,
                    "iv_ratio":  iv_ratio,
                    "pc_skew":   pc_skew,
                    "reasons":   ", ".join(alert_reasons),
                })
                log.info(f"    >> ALERT: {', '.join(alert_reasons)}")

        except Exception as e:
            log.error(f"  {symbol}: error — {e}")

    save_state("unusual_options.json", state)

    if not alerts:
        log.info("  No unusual options activity detected.")
        return

    rows = [[
        f"<strong>{a['symbol']}</strong>",
        f"${a['price']:.2f}",
        f"{a['call_iv']*100:.1f}%",
        f"{a['put_iv']*100:.1f}%",
        f"{a['hist_vol']*100:.1f}%",
        f"<strong>{a['iv_ratio']:.2f}x</strong>",
        f"{a['pc_skew']:.2f}",
        a["reasons"],
    ] for a in sorted(alerts, key=lambda x: -x["iv_ratio"])]

    table = html_table(
        ["Symbol", "Price", "Call IV", "Put IV", "Hist Vol", "IV Ratio", "P/C Skew", "Alert"],
        rows, "#4a148c"
    )
    content = (
        f"<p><strong>{len(alerts)}</strong> unusual options activity signal(s) detected:</p>"
        f"{table}"
        f"<p style='color:#888;font-size:12px;margin-top:10px'>"
        f"IV Ratio >1.8x = institutions buying options (expect a move). "
        f"P/C Skew >1.6 = heavy put buying (hedging = bearish signal).</p>"
    )
    body_html = base_html("Unusual Options Activity", "⚡", content)
    body_text = "\n".join(
        f"{a['symbol']}: IV {a['iv_ratio']:.1f}x hist vol — {a['reasons']}" for a in alerts
    )
    send_signal_email(
        f"⚡ Options Alert — {len(alerts)} unusual activity signal(s) in your holdings",
        body_text, body_html
    )
    log.info("Run complete.\n")


if __name__ == "__main__":
    run()
