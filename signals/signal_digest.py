#!/usr/bin/env python3
"""
Daily Signal Digest
=====================
Aggregates all signal monitors into a single morning email summary.
Shows what each monitor found overnight and any pending alerts.

Runs daily at 8:30 AM ET (before market open).
"""

import json
import requests
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from signal_utils import (
    get_logger, load_state, send_signal_email, base_html, PORTFOLIO_SYMBOLS
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

log = get_logger("signal_digest", "signal_digest.log")


def get_portfolio_snapshot() -> dict:
    """Get current portfolio value and day P&L."""
    try:
        acct = requests.get(f"{BASE_URL}/account", headers=HEADERS, timeout=10).json()
        positions = requests.get(f"{BASE_URL}/positions", headers=HEADERS, timeout=10).json()
        stocks = [p for p in positions if p.get("asset_class", "us_equity") == "us_equity"]
        orders = requests.get(f"{BASE_URL}/orders", headers=HEADERS,
                              params={"status": "open", "limit": 200}, timeout=10).json()
        stops = {o["symbol"] for o in orders if o.get("type") == "stop" and o.get("side") == "sell"}
        return {
            "port_val":   float(acct["portfolio_value"]),
            "cash":       float(acct["cash"]),
            "day_pl":     float(acct["equity"]) - float(acct["last_equity"]),
            "positions":  len(stocks),
            "stops":      len(stops),
            "missing_stops": [s["symbol"] for s in stocks if s["symbol"] not in stops],
        }
    except Exception as e:
        log.error(f"Portfolio snapshot error: {e}")
        return {}


def get_regime_status() -> dict:
    """Read latest regime state."""
    rf = BASE_DIR / "strategies" / "regime_state.json"
    if rf.exists():
        return json.load(open(rf))
    return {"regime": "UNKNOWN", "spy_price": 0, "sma200": 0, "pct_vs_200": 0}


def get_qbts_wheel_status() -> dict:
    """Read QBTS wheel state."""
    wf = BASE_DIR / "strategies" / "qbts_wheel_state.json"
    if wf.exists():
        return json.load(open(wf))
    return {}


def get_earnings_upcoming() -> list:
    """Get upcoming earnings from calendar state."""
    ef = BASE_DIR / "signals" / "states" / "earnings_calendar.json"
    if ef.exists():
        state = json.load(open(ef))
        return [e for e in state.get("upcoming", []) if e.get("days_until", 99) <= 5]
    return []


def signal_card(title: str, icon: str, content: str, color: str = "#1a1d26") -> str:
    return f"""
    <div style="background:{color};border:1px solid #2a2d3a;border-radius:8px;padding:16px;margin-bottom:12px">
        <h3 style="color:#fff;margin:0 0 8px">{icon} {title}</h3>
        {content}
    </div>"""


def run():
    log.info("=" * 65)
    log.info(f"SIGNAL DIGEST  |  {date.today()}")
    log.info("=" * 65)

    today_str = date.today().isoformat()

    # ── Portfolio snapshot ────────────────────────────────────────────────────
    port = get_portfolio_snapshot()
    regime = get_regime_status()
    wheel  = get_qbts_wheel_status()
    earnings = get_earnings_upcoming()

    regime_icon = {"BULL": "🟢", "CAUTION": "🟡", "BEAR": "🔴"}.get(regime.get("regime", ""), "⚪")

    # ── Portfolio card ────────────────────────────────────────────────────────
    day_pl   = port.get("day_pl", 0)
    port_val = port.get("port_val", 0)
    missing  = port.get("missing_stops", [])
    stops_ok = len(missing) == 0

    port_content = f"""
        <table style="width:100%;font-size:13px;color:#ccc">
            <tr><td>Portfolio Value</td><td style="text-align:right;color:#fff"><strong>${port_val:,.2f}</strong></td></tr>
            <tr><td>Yesterday P&L</td>
                <td style="text-align:right;color:{'#4caf50' if day_pl>=0 else '#f44336'}">
                    <strong>${day_pl:+,.2f}</strong></td></tr>
            <tr><td>Cash</td><td style="text-align:right">${port.get('cash', 0):,.2f}</td></tr>
            <tr><td>Positions</td><td style="text-align:right">{port.get('positions', 0)}</td></tr>
            <tr><td>Stop Coverage</td>
                <td style="text-align:right;color:{'#4caf50' if stops_ok else '#f44336'}">
                    {'✅ All protected' if stops_ok else f'⚠️ MISSING: {", ".join(missing)}'}</td></tr>
        </table>"""

    # ── Market regime card ────────────────────────────────────────────────────
    regime_name = regime.get("regime", "UNKNOWN")
    regime_action = {
        "BULL":    "✅ Full wheel contracts — sell puts normally",
        "CAUTION": "⚠️ Half contracts — market near 200-day SMA",
        "BEAR":    "🛑 Wheel PAUSED — do not sell new puts",
    }.get(regime_name, "⚪ Unknown")

    regime_content = f"""
        <table style="width:100%;font-size:13px;color:#ccc">
            <tr><td>Regime</td><td style="text-align:right"><strong style="color:#fff">{regime_icon} {regime_name}</strong></td></tr>
            <tr><td>SPY Price</td><td style="text-align:right">${regime.get('spy_price', 0):.2f}</td></tr>
            <tr><td>200-day SMA</td><td style="text-align:right">${regime.get('sma200', 0):.2f}</td></tr>
            <tr><td>SPY vs SMA200</td><td style="text-align:right">{regime.get('pct_vs_200', 0):+.2f}%</td></tr>
            <tr><td>Wheel Action</td><td style="text-align:right;font-size:12px">{regime_action}</td></tr>
        </table>"""

    # ── QBTS Wheel card ───────────────────────────────────────────────────────
    wheel_stage    = wheel.get("stage", "?")
    wheel_contract = wheel.get("active_contract") or "None — hunting for CSP"
    wheel_premium  = wheel.get("total_premium", 0)
    wheel_cycles   = wheel.get("cycle_count", 0)

    wheel_content = f"""
        <table style="width:100%;font-size:13px;color:#ccc">
            <tr><td>Stage</td><td style="text-align:right"><strong style="color:#fff">{wheel_stage}</strong></td></tr>
            <tr><td>Active Contract</td><td style="text-align:right;font-size:11px">{wheel_contract}</td></tr>
            <tr><td>Total Premium Collected</td><td style="text-align:right;color:#4caf50"><strong>${wheel_premium:,.2f}</strong></td></tr>
            <tr><td>Completed Cycles</td><td style="text-align:right">{wheel_cycles}</td></tr>
        </table>"""

    # ── Earnings card ─────────────────────────────────────────────────────────
    if earnings:
        earn_rows = "".join(
            f'<tr><td><strong>{e["symbol"]}</strong></td>'
            f'<td>{e["date"]}</td>'
            f'<td style="color:{"#f44336" if e["days_until"]<=2 else "#ffc107"}">'
            f'{e["days_until"]}d away</td>'
            f'<td>{e.get("when","TBD")}</td></tr>'
            for e in earnings
        )
        earnings_content = f"""
            <p style='color:#ffc107;font-size:13px'>⚠️ Upcoming earnings in your holdings — manage position sizes:</p>
            <table style="width:100%;font-size:13px;color:#ccc">
                <tr style="color:#888"><td>Symbol</td><td>Date</td><td>When</td><td>Timing</td></tr>
                {earn_rows}
            </table>"""
    else:
        earnings_content = "<p style='color:#4caf50;font-size:13px'>✅ No earnings in the next 5 days for your holdings.</p>"

    # ── Signal monitors status ────────────────────────────────────────────────
    monitors = [
        ("contract_awards.json",   "🏛️ Contract Awards",      "USASpending.gov"),
        ("insider_trades.json",    "🔍 Insider Trades",        "SEC Form 4"),
        ("hedge_fund_13f.json",    "🦈 Hedge Fund 13F",        "SEC EDGAR"),
        ("activist_13dg.json",     "🏴 Activist 13D/13G",      "SEC EDGAR"),
        ("etf_flows.json",         "📊 ETF Flows",             "Alpaca Data"),
        ("short_interest.json",    "📉 Short Interest",        "FINRA"),
        ("unusual_options.json",   "⚡ Unusual Options",       "Alpaca Options"),
        ("senate_disclosures.json","🏛️ Senate Disclosures",   "Capitol Trades"),
    ]
    monitor_rows = ""
    for fname, label, source in monitors:
        sf = BASE_DIR / "signals" / "states" / fname
        if sf.exists():
            s = json.load(open(sf))
            last = s.get("last_run", "never")[:10]
            status = "🟢 Active"
        else:
            last = "never"
            status = "⚪ No data yet"
        monitor_rows += (
            f'<tr><td>{label}</td><td>{source}</td>'
            f'<td>{last}</td><td>{status}</td></tr>'
        )

    monitors_content = f"""
        <table style="width:100%;font-size:12px;color:#ccc">
            <tr style="color:#888"><td>Monitor</td><td>Source</td><td>Last Run</td><td>Status</td></tr>
            {monitor_rows}
        </table>"""

    # ── Assemble full email ───────────────────────────────────────────────────
    content = (
        signal_card("Portfolio Overview",   "💼", port_content)
        + signal_card("Market Regime (Markov)", "🧠", regime_content)
        + signal_card("QBTS Wheel Strategy",    "🎡", wheel_content)
        + signal_card("Upcoming Earnings",       "📅", earnings_content,
                      "#1a1a00" if earnings else "#1a1d26")
        + signal_card("Signal Monitor Status",   "📡", monitors_content)
    )

    body_html = base_html(f"Morning Signal Digest — {today_str}", "📡", content)
    body_text = (
        f"Portfolio: ${port_val:,.2f} | Day P&L: ${day_pl:+,.2f}\n"
        f"Regime: {regime_name} ({regime.get('pct_vs_200',0):+.1f}% vs SMA200)\n"
        f"QBTS Wheel: {wheel_stage} | Premium: ${wheel_premium:,.2f}\n"
        f"Earnings soon: {', '.join(e['symbol'] for e in earnings) or 'None'}"
    )
    send_signal_email(
        f"📡 Morning Digest — {today_str} | {regime_icon} {regime_name} | ${port_val:,.0f}",
        body_text, body_html
    )
    log.info("Digest sent.\n")


if __name__ == "__main__":
    run()
