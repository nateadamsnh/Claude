#!/usr/bin/env python3
"""
Activist Investor Monitor — 13D/13G Filings
=============================================
Monitors SEC EDGAR for 13D and 13G filings — when a hedge fund
acquires more than 5% of a public company.

13D = Activist intent (wants to influence management) — very bullish
13G = Passive 5%+ stake — moderately bullish

Alerts when any of your portfolio holdings attract an activist.
Also alerts on NEW 13D filings in any company (potential trade idea).

Runs every 4 hours during market hours.
"""

import requests
import xml.etree.ElementTree as ET
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from signal_utils import (
    get_logger, load_state, save_state, send_signal_email,
    html_table, base_html, PORTFOLIO_SYMBOLS, COMPANY_NAMES
)

log = get_logger("activist_monitor", "activist_monitor.log")

EDGAR_RSS_13D = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SC+13D&dateb=&owner=include&count=40&output=atom"
EDGAR_RSS_13G = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=SC+13G&dateb=&owner=include&count=40&output=atom"


def fetch_filings(url: str, filing_type: str) -> list:
    """Fetch recent 13D or 13G filings from EDGAR RSS."""
    try:
        r = requests.get(url, timeout=15,
                        headers={"User-Agent": "Nathaniel Trading Monitor nate.adams.nh@gmail.com"})
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.content)
        ns   = {"atom": "http://www.w3.org/2005/Atom"}
        results = []
        for entry in root.findall("atom:entry", ns):
            title   = entry.findtext("atom:title", "", ns)
            link_el = entry.find("atom:link", ns)
            fid     = entry.findtext("atom:id", "", ns)
            updated = entry.findtext("atom:updated", "", ns)
            results.append({
                "title":        title,
                "link":         link_el.attrib.get("href", "") if link_el is not None else "",
                "filing_id":    fid,
                "updated":      updated[:10] if updated else "",
                "filing_type":  filing_type,
            })
        return results
    except Exception as e:
        log.error(f"Error fetching {filing_type} RSS: {e}")
        return []


def matches_portfolio(title: str) -> str:
    """Return portfolio symbol if the filing mentions one of our companies."""
    title_lower = title.lower()
    for sym in PORTFOLIO_SYMBOLS:
        for name in COMPANY_NAMES.get(sym, [sym]):
            if name.lower() in title_lower:
                return sym
    return ""


def run():
    log.info("=" * 65)
    log.info(f"ACTIVIST MONITOR (13D/13G)  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    log.info("=" * 65)

    state    = load_state("activist_13dg.json")
    seen_ids = set(state.get("seen_filing_ids", []))

    filings_13d = fetch_filings(EDGAR_RSS_13D, "13D")
    filings_13g = fetch_filings(EDGAR_RSS_13G, "13G")
    all_filings = filings_13d + filings_13g

    log.info(f"  Fetched {len(filings_13d)} 13D and {len(filings_13g)} 13G filings")

    portfolio_hits = []
    all_new_13d    = []

    for f in all_filings:
        fid = f["filing_id"]
        if fid in seen_ids:
            continue
        seen_ids.add(fid)

        sym = matches_portfolio(f["title"])
        if sym:
            portfolio_hits.append({**f, "symbol": sym})
            log.info(
                f"  [PORTFOLIO HIT] {f['filing_type']} on {sym}: {f['title'][:80]}"
            )

        if f["filing_type"] == "13D":
            all_new_13d.append(f)
            log.info(f"  [NEW 13D] {f['title'][:80]}")

    state["seen_filing_ids"] = list(seen_ids)[-1000:]
    state["last_run"] = datetime.now().isoformat()
    save_state("activist_13dg.json", state)

    if not portfolio_hits and not all_new_13d:
        log.info("  No new activist filings detected.")
        return

    content = ""

    if portfolio_hits:
        rows = [[
            f"<strong style='color:#f44336'>{h['symbol']}</strong>",
            f"<strong>{h['filing_type']}</strong>",
            h["title"][:60],
            h["updated"],
            f'<a href="{h["link"]}" style="color:#4a90d9">View →</a>',
        ] for h in portfolio_hits]
        content += "<h3 style='color:#f44336'>🚨 Activist Filing on YOUR Holdings</h3>"
        content += "<p>An investor has taken a 5%+ stake in a company you own:</p>"
        content += html_table(
            ["Symbol", "Type", "Description", "Date", "Filing"],
            rows, "#b71c1c"
        )

    if all_new_13d:
        rows13d = [[
            h["title"][:65],
            h["updated"],
            f'<a href="{h["link"]}" style="color:#4a90d9">View →</a>',
        ] for h in all_new_13d[:15]]
        content += "<h3 style='color:#ff9800;margin-top:20px'>📋 All New 13D Filings (Potential Trade Ideas)</h3>"
        content += "<p>Activist investors disclosed new 5%+ stakes. These often precede buyouts or restructuring:</p>"
        content += html_table(["Description", "Date", "Filing"], rows13d, "#e65100")

    body_html = base_html("Activist Investor Alert — 13D/13G Filings", "🏴", content)
    body_text = "\n".join(f"{f['filing_type']}: {f['title']}" for f in portfolio_hits + all_new_13d[:5])
    subject = (
        f"🚨 ACTIVIST on your holdings: {', '.join(h['symbol'] for h in portfolio_hits)}"
        if portfolio_hits else
        f"📋 {len(all_new_13d)} new 13D activist filing(s) — potential trade ideas"
    )
    send_signal_email(subject, body_text, body_html)
    log.info("Run complete.\n")


if __name__ == "__main__":
    run()
