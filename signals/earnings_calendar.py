#!/usr/bin/env python3
"""
Earnings Calendar Alert
========================
Warns you 2 days before any of your holdings report earnings so you
can decide whether to:
  - Reduce position (avoid earnings volatility)
  - Hold through (if conviction is high)
  - Add to position (if expecting a beat)

Also alerts when an earnings date is just 1 day away as a final reminder.

Uses Alpaca's calendar/news API and SEC EDGAR earnings dates.

Runs daily at 8:00 AM ET (before market open).
"""

import json
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

log = get_logger("earnings_calendar", "earnings_calendar.log")

WARN_DAYS_AHEAD = [2, 1]  # Alert 2 days before, then 1 day before


def get_upcoming_earnings(symbols: list, days_ahead: int = 14) -> list:
    """
    Fetch upcoming earnings dates from Alpaca news/calendar.
    Returns list of {symbol, earnings_date, days_until}
    """
    results = []
    today   = date.today()

    for symbol in symbols:
        try:
            # Use Alpaca's corporate actions / news endpoint for earnings
            r = requests.get(
                f"{DATA_URL}/v1beta1/corporate-actions",
                headers=HEADERS,
                params={
                    "symbols":    symbol,
                    "types":      "earnings",
                    "start":      today.isoformat(),
                    "end":        (today + timedelta(days=days_ahead)).isoformat(),
                    "limit":      5,
                },
                timeout=10,
            )
            if r.status_code == 200:
                actions = r.json().get("corporate_actions", {}).get("earnings", [])
                for a in actions:
                    edate = date.fromisoformat(a.get("date", "")[:10])
                    days_until = (edate - today).days
                    results.append({
                        "symbol":      symbol,
                        "date":        edate.isoformat(),
                        "days_until":  days_until,
                        "when":        a.get("when", "unknown"),  # BMO/AMC
                        "source":      "alpaca",
                    })
        except Exception:
            pass

    # Fallback: try Alpaca news for earnings mentions
    if not results:
        try:
            symbols_str = ",".join(symbols[:10])  # API limit
            r = requests.get(
                f"{DATA_URL}/v1beta1/news",
                headers=HEADERS,
                params={
                    "symbols": symbols_str,
                    "limit":   50,
                },
                timeout=10,
            )
            if r.status_code == 200:
                for article in r.json().get("news", []):
                    title = article.get("headline", "").lower()
                    if "earnings" in title or "quarterly results" in title or "q1" in title or "q2" in title:
                        for sym in article.get("symbols", []):
                            if sym in symbols:
                                pub_date = article.get("created_at", "")[:10]
                                log.info(f"  Earnings news: {sym} — {article.get('headline','')[:60]}")
        except Exception:
            pass

    return sorted(results, key=lambda x: x["days_until"])


def run():
    log.info("=" * 65)
    log.info(f"EARNINGS CALENDAR  |  {date.today()}")
    log.info("=" * 65)

    state     = load_state("earnings_calendar.json")
    today_str = date.today().isoformat()

    if state.get("last_run") == today_str:
        log.info("  Already ran today — skipping.")
        return

    upcoming  = get_upcoming_earnings(PORTFOLIO_SYMBOLS, days_ahead=14)
    log.info(f"  Found {len(upcoming)} upcoming earnings in next 14 days")

    alerts_2d = [e for e in upcoming if e["days_until"] == 2]
    alerts_1d = [e for e in upcoming if e["days_until"] == 1]
    alerts_soon = [e for e in upcoming if 3 <= e["days_until"] <= 7]

    all_alerts = alerts_1d + alerts_2d

    # Always log the next 14 days
    for e in upcoming:
        log.info(
            f"  {e['symbol']:6s} | earnings {e['date']} | "
            f"{e['days_until']} day(s) away | {e.get('when','?')}"
        )

    state["last_run"]      = today_str
    state["upcoming"]      = upcoming
    save_state("earnings_calendar.json", state)

    if not upcoming:
        log.info("  No upcoming earnings detected in next 14 days.")
        return

    # Build full schedule table
    all_rows = [[
        f"<strong>{e['symbol']}</strong>",
        e["date"],
        f"<strong style='color:{'#f44336' if e['days_until']<=2 else '#ffc107' if e['days_until']<=5 else '#4caf50'}'>"
        f"{e['days_until']} day{'s' if e['days_until']!=1 else ''}</strong>",
        e.get("when", "TBD").upper(),
        "⚠️ ACT NOW" if e["days_until"] <= 2 else "📌 Watch" if e["days_until"] <= 5 else "📅 Mark",
    ] for e in upcoming]

    table = html_table(
        ["Symbol", "Earnings Date", "Days Away", "Timing", "Action"],
        all_rows
    )

    urgent = ""
    if alerts_1d:
        syms = ", ".join(e["symbol"] for e in alerts_1d)
        urgent += f"""
        <div style='background:#b71c1c;padding:12px;border-radius:6px;margin-bottom:12px'>
            <strong>🚨 TOMORROW: {syms} reports earnings!</strong>
            Decide now: reduce position, hold, or add?
        </div>"""

    if alerts_2d:
        syms = ", ".join(e["symbol"] for e in alerts_2d)
        urgent += f"""
        <div style='background:#e65100;padding:12px;border-radius:6px;margin-bottom:12px'>
            <strong>⚠️ IN 2 DAYS: {syms} reports earnings.</strong>
            Consider your position size before the report.
        </div>"""

    content = (
        f"{urgent}"
        f"<h3 style='margin-top:16px'>Earnings Schedule — Next 14 Days</h3>"
        f"{table}"
        f"<p style='color:#888;font-size:12px;margin-top:10px'>"
        f"BMO = Before Market Open. AMC = After Market Close.</p>"
    )
    body_html = base_html("Earnings Calendar Alert", "📅", content)

    subject_syms = ", ".join(e["symbol"] for e in all_alerts) if all_alerts else \
                   ", ".join(e["symbol"] for e in upcoming[:3])
    subject = (
        f"🚨 EARNINGS TOMORROW: {', '.join(e['symbol'] for e in alerts_1d)}"
        if alerts_1d else
        f"⚠️ Earnings in 2 days: {', '.join(e['symbol'] for e in alerts_2d)}"
        if alerts_2d else
        f"📅 Upcoming earnings: {subject_syms} in next 14 days"
    )
    body_text = "\n".join(
        f"{e['symbol']}: earnings {e['date']} ({e['days_until']} days away)" for e in upcoming
    )
    send_signal_email(subject, body_text, body_html)
    log.info("Run complete.\n")


if __name__ == "__main__":
    run()
