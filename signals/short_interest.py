#!/usr/bin/env python3
"""
Short Interest Monitor
=======================
Downloads FINRA bi-weekly short interest data and tracks changes
in short interest for portfolio holdings.

Alert conditions:
  SHORT SQUEEZE SETUP: Short interest drops >20% in one period
    (shorts covering = expect price to rise)
  BEAR WARNING: Short interest rises >30% in one period
    (smart money betting on decline)

FINRA releases short interest data twice monthly:
  Settlement dates around the 15th and last day of each month.
  Data published ~1 week later.

Runs bi-weekly (1st and 15th of each month).
"""

import csv
import io
import requests
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from signal_utils import (
    get_logger, load_state, save_state, send_signal_email,
    html_table, base_html, PORTFOLIO_SYMBOLS
)

log = get_logger("short_interest", "short_interest.log")

# FINRA short interest download — most recent consolidated file
FINRA_URL = "https://cdn.finra.org/equity/regsho/biweekly/FNRAshvol{date}.txt"
# Fallback: FINRA short data search
FINRA_SEARCH = "https://api.finra.org/data/group/otcMarket/name/weeklyDownloadFile"

SQUEEZE_THRESHOLD = -0.20   # Short interest dropped 20%+ = squeeze setup
BEAR_THRESHOLD    =  0.30   # Short interest rose 30%+ = bear warning
MIN_SHORT_SHARES  = 100_000  # Minimum shares short to be meaningful


def try_download_finra(attempt_dates: list) -> list:
    """Try to download FINRA short data for the most recent available date."""
    for dt in attempt_dates:
        url = FINRA_URL.format(date=dt.strftime("%Y%m%d"))
        try:
            r = requests.get(url, timeout=20,
                            headers={"User-Agent": "Nathaniel Trading Monitor nate.adams.nh@gmail.com"})
            if r.status_code == 200 and len(r.content) > 1000:
                log.info(f"  Downloaded FINRA data for {dt}")
                return parse_finra_file(r.text, str(dt))
        except Exception:
            pass
    return []


def parse_finra_file(content: str, data_date: str) -> list:
    """Parse FINRA short interest pipe-delimited file."""
    records = []
    reader = csv.reader(io.StringIO(content), delimiter="|")
    headers = None
    for row in reader:
        if not headers:
            headers = [h.strip().upper() for h in row]
            continue
        if len(row) < 4:
            continue
        try:
            rec = dict(zip(headers, [r.strip() for r in row]))
            symbol     = rec.get("SYMBOL", rec.get("SYMBOLCODE", "")).upper()
            short_vol  = int(rec.get("SHORTVOLUME", rec.get("SHORTEXEMPTVOLUME", "0")) or 0)
            total_vol  = int(rec.get("TOTALVOLUME", "0") or 0)
            if symbol and total_vol > 0:
                records.append({
                    "symbol":    symbol,
                    "short_vol": short_vol,
                    "total_vol": total_vol,
                    "short_pct": short_vol / total_vol if total_vol else 0,
                    "date":      data_date,
                })
        except (ValueError, KeyError):
            pass
    return records


def run():
    log.info("=" * 65)
    log.info(f"SHORT INTEREST MONITOR  |  {date.today()}")
    log.info("=" * 65)

    state = load_state("short_interest.json")

    # Try recent FINRA release dates (they release ~1 week after settlement)
    today = date.today()
    from datetime import timedelta
    candidate_dates = [today - timedelta(days=i) for i in range(1, 15)]

    records = try_download_finra(candidate_dates)
    if not records:
        log.warning("  Could not download FINRA short interest data — will retry next run.")
        return

    log.info(f"  Loaded {len(records)} records")

    # Filter to portfolio symbols
    portfolio_data = {r["symbol"]: r for r in records if r["symbol"] in PORTFOLIO_SYMBOLS}
    log.info(f"  Portfolio matches: {len(portfolio_data)} symbols")

    # Compare to previous
    prev_data  = state.get("last_data", {})
    alerts     = []
    updated    = {}

    for sym, current in portfolio_data.items():
        updated[sym] = current
        if sym not in prev_data:
            log.info(f"  {sym}: first reading — short vol {current['short_vol']:,} ({current['short_pct']*100:.1f}%)")
            continue

        prev    = prev_data[sym]
        prev_sv = prev["short_vol"]
        curr_sv = current["short_vol"]

        if prev_sv < MIN_SHORT_SHARES:
            continue

        change_pct = (curr_sv - prev_sv) / prev_sv if prev_sv else 0
        log.info(
            f"  {sym:6s} | short_vol {prev_sv:>10,} -> {curr_sv:>10,} | "
            f"change {change_pct:+.1f}% | short_pct {current['short_pct']*100:.1f}%"
        )

        if change_pct <= SQUEEZE_THRESHOLD:
            alerts.append({
                "symbol":      sym,
                "alert_type":  "SQUEEZE SETUP",
                "icon":        "🟢",
                "prev":        prev_sv,
                "curr":        curr_sv,
                "change_pct":  change_pct,
                "short_pct":   current["short_pct"],
                "note":        "Shorts covering — price may rise",
            })
            log.info(f"    >> SQUEEZE SETUP: {sym} shorts dropped {change_pct*100:.1f}%")

        elif change_pct >= BEAR_THRESHOLD:
            alerts.append({
                "symbol":      sym,
                "alert_type":  "BEAR WARNING",
                "icon":        "🔴",
                "prev":        prev_sv,
                "curr":        curr_sv,
                "change_pct":  change_pct,
                "short_pct":   current["short_pct"],
                "note":        "Smart money betting on decline — monitor closely",
            })
            log.info(f"    >> BEAR WARNING: {sym} shorts rose {change_pct*100:.1f}%")

    state["last_data"] = updated
    state["last_run"]  = date.today().isoformat()
    save_state("short_interest.json", state)

    if not alerts:
        log.info("  No significant short interest changes detected.")
        return

    rows = [[
        f"{a['icon']} <strong>{a['symbol']}</strong>",
        f"<strong>{a['alert_type']}</strong>",
        f"{a['prev']:,}",
        f"{a['curr']:,}",
        f"<strong>{a['change_pct']*100:+.1f}%</strong>",
        f"{a['short_pct']*100:.1f}%",
        a["note"],
    ] for a in sorted(alerts, key=lambda x: abs(x["change_pct"]), reverse=True)]

    table = html_table(
        ["Symbol", "Signal", "Prev Short Vol", "Curr Short Vol", "Change", "Short %", "Interpretation"],
        rows
    )
    content = f"<p><strong>{len(alerts)}</strong> significant short interest change(s) in your holdings:</p>{table}"
    body_html = base_html("Short Interest Alert", "📉", content)
    body_text = "\n".join(
        f"{a['icon']} {a['symbol']} {a['alert_type']}: {a['change_pct']*100:+.1f}% change"
        for a in alerts
    )
    send_signal_email(
        f"📉 Short Interest Alert — {len(alerts)} signal(s) in your holdings",
        body_text, body_html
    )
    log.info("Run complete.\n")


if __name__ == "__main__":
    run()
