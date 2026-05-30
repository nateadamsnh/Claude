#!/usr/bin/env python3
"""
ETF Unusual Volume / Flow Monitor
===================================
Detects unusual volume spikes in sector ETFs that are relevant to
Nathaniel's holdings. A volume spike significantly above the 20-day
average signals institutional accumulation or distribution.

Monitored ETFs:
  SOXX — Semiconductors (IONQ, INTC)
  XLE  — Energy (XOM, CVX)
  XLK  — Tech (MSFT, NVDA, GOOGL, ORCL, IBM)
  VNQ  — REITs (O, VNQ)
  XLV  — Healthcare (BSX, SYK)
  XLI  — Industrials (EMR, CARR)
  IYT  — Transportation
  GLD/IAU — Gold

Alert threshold: volume > 2x 20-day average

Runs daily at market open (9:35 AM ET).
"""

import json
import requests
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from signal_utils import (
    get_logger, load_state, save_state, send_signal_email,
    html_table, base_html
)

BASE_DIR = Path(__file__).parent.parent
with open(BASE_DIR / "config" / "alpaca_credentials.json") as f:
    creds = json.load(f)
DATA_URL = "https://data.alpaca.markets/v2"
HEADERS  = {
    "APCA-API-KEY-ID":     creds["api_key"],
    "APCA-API-SECRET-KEY": creds["api_secret"],
}

log = get_logger("etf_flows", "etf_flows.log")

MONITORED_ETFS = {
    "SOXX": "Semiconductors — IONQ, INTC",
    "XLE":  "Energy — XOM, CVX",
    "XLK":  "Technology — MSFT, NVDA, GOOGL, IBM, ORCL",
    "VNQ":  "Real Estate — O, VNQ",
    "XLV":  "Healthcare — SYK",
    "XLI":  "Industrials — EMR, CARR",
    "IAU":  "Gold — IAU",
    "XLF":  "Financials — BAC, MA, V",
    "ITA":  "Defense — LMT, BWXT",
    "ARKK": "Innovation ETF — PLTR, IONQ, RKLB",
}

VOLUME_SPIKE_THRESHOLD = 2.0  # Alert if volume > 2x 20-day average
LOOKBACK_DAYS = 30


def get_bars(symbol: str, days: int) -> list:
    """Fetch daily bars for a symbol."""
    start = (date.today() - timedelta(days=days + 10)).isoformat()
    r = requests.get(
        f"{DATA_URL}/stocks/{symbol}/bars",
        headers=HEADERS,
        params={"timeframe": "1Day", "start": start, "limit": days + 10, "feed": "sip"},
        timeout=15,
    )
    if r.status_code == 200:
        return r.json().get("bars", [])
    return []


def run():
    log.info("=" * 65)
    log.info(f"ETF FLOW MONITOR  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    log.info("=" * 65)

    state    = load_state("etf_flows.json")
    today_str = date.today().isoformat()

    if state.get("last_run") == today_str:
        log.info("  Already ran today — skipping.")
        return

    alerts = []

    for etf, description in MONITORED_ETFS.items():
        try:
            bars = get_bars(etf, LOOKBACK_DAYS)
            if len(bars) < 5:
                log.warning(f"  {etf}: insufficient data ({len(bars)} bars)")
                continue

            today_bar = bars[-1]
            prev_bars = bars[:-1]

            today_vol  = float(today_bar.get("v", 0))
            today_close = float(today_bar.get("c", 0))
            today_open  = float(today_bar.get("o", 0))

            avg_vol = sum(float(b.get("v", 0)) for b in prev_bars[-20:]) / min(20, len(prev_bars))
            vol_ratio = today_vol / avg_vol if avg_vol > 0 else 0

            direction = "INFLOW ↑" if today_close >= today_open else "OUTFLOW ↓"
            color_flag = "🟢" if today_close >= today_open else "🔴"

            log.info(
                f"  {etf:6s} | vol={today_vol:>12,.0f} | avg={avg_vol:>12,.0f} | "
                f"ratio={vol_ratio:.2f}x | {direction}"
            )

            if vol_ratio >= VOLUME_SPIKE_THRESHOLD:
                alerts.append({
                    "etf":        etf,
                    "desc":       description,
                    "today_vol":  today_vol,
                    "avg_vol":    avg_vol,
                    "ratio":      vol_ratio,
                    "direction":  direction,
                    "flag":       color_flag,
                    "close":      today_close,
                    "pct_change": (today_close - today_open) / today_open * 100,
                })

        except Exception as e:
            log.error(f"  {etf}: error — {e}")

    state["last_run"] = today_str
    save_state("etf_flows.json", state)

    if not alerts:
        log.info("  No unusual ETF volume detected today.")
        return

    log.info(f"  {len(alerts)} ETF(s) showing unusual volume — sending alert")

    rows = [[
        f"{a['flag']} <strong>{a['etf']}</strong>",
        a["desc"],
        f"${a['close']:.2f} ({a['pct_change']:+.1f}%)",
        f"{a['today_vol']:,.0f}",
        f"{a['avg_vol']:,.0f}",
        f"<strong>{a['ratio']:.1f}x</strong>",
        f"<strong>{a['direction']}</strong>",
    ] for a in sorted(alerts, key=lambda x: -x["ratio"])]

    table = html_table(
        ["ETF", "Sector / Holdings", "Price", "Today Vol", "20d Avg Vol", "Spike", "Direction"],
        rows
    )
    content = (
        f"<p><strong>{len(alerts)}</strong> ETF(s) showing volume >2x their 20-day average today. "
        f"This signals institutional accumulation or distribution in sectors you hold.</p>"
        f"{table}"
        f"<p style='color:#888;font-size:12px;margin-top:12px'>"
        f"Green = price closed above open (buying). Red = price closed below open (selling).</p>"
    )
    body_html = base_html("ETF Unusual Volume Alert", "📊", content)
    body_text = "\n".join(
        f"{a['etf']}: {a['ratio']:.1f}x normal volume — {a['direction']}" for a in alerts
    )
    send_signal_email(
        f"📊 ETF Volume Spike — {len(alerts)} sector(s) showing unusual institutional activity",
        body_text, body_html
    )
    log.info("Run complete.\n")


if __name__ == "__main__":
    run()
