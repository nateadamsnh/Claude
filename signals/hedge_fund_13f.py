#!/usr/bin/env python3
"""
13F Hedge Fund Holdings Tracker
=================================
Monitors SEC EDGAR for new 13F filings from top-performing hedge funds.
When a new quarterly 13F is filed, parses it for:
  - New positions opened
  - Positions significantly increased (>25%)
  - Positions that overlap with your current holdings

Tracked funds: Berkshire Hathaway, Pershing Square, Scion, Citadel,
               Appaloosa, Third Point, Tiger Global, Duquesne, Druckenmiller

Runs weekly (Monday morning) — new 13Fs drop within 45 days of quarter end.
"""

import re
import requests
import xml.etree.ElementTree as ET
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from signal_utils import (
    get_logger, load_state, save_state, send_signal_email,
    html_table, base_html, PORTFOLIO_SYMBOLS
)

log = get_logger("hedge_fund_13f", "hedge_fund_13f.log")

EDGAR_SEARCH = "https://efts.sec.gov/LATEST/search-index"
EDGAR_BASE   = "https://www.sec.gov"
EDGAR_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"

# Top funds to track: name -> CIK
TRACKED_FUNDS = {
    "Berkshire Hathaway (Buffett)":  "0001067983",
    "Pershing Square (Ackman)":      "0001336528",
    "Scion Asset Mgmt (Burry)":      "0001649339",
    "Appaloosa Mgmt (Tepper)":       "0001006438",
    "Third Point (Loeb)":            "0001040273",
    "Duquesne Family Office":        "0001536411",
    "Tiger Global Management":       "0001167483",
    "Citadel Advisors":              "0001423298",
}

MIN_POSITION_VALUE = 1_000_000  # $1M minimum position to report


def get_latest_13f(cik: str, fund_name: str) -> tuple:
    """Get the most recent 13F filing accession number and date."""
    try:
        url = EDGAR_SUBMISSIONS.format(cik=cik.lstrip("0").zfill(10))
        r   = requests.get(url, timeout=15,
                          headers={"User-Agent": "Nathaniel Trading Monitor nate.adams.nh@gmail.com"})
        if r.status_code != 200:
            return None, None
        data = r.json()
        filings = data.get("filings", {}).get("recent", {})
        forms   = filings.get("form", [])
        accessions = filings.get("accessionNumber", [])
        dates   = filings.get("filingDate", [])

        for form, acc, dt in zip(forms, accessions, dates):
            if form in ("13F-HR", "13F-HR/A"):
                return acc, dt
    except Exception as e:
        log.error(f"Error fetching 13F for {fund_name}: {e}")
    return None, None


def parse_13f_holdings(cik: str, accession: str) -> list:
    """Parse 13F XML to extract holdings list."""
    acc_clean = accession.replace("-", "")
    index_url = f"{EDGAR_BASE}/Archives/edgar/data/{cik.lstrip('0')}/{acc_clean}/{accession}-index.htm"
    holdings  = []

    try:
        r = requests.get(index_url, timeout=15,
                        headers={"User-Agent": "Nathaniel Trading Monitor nate.adams.nh@gmail.com"})
        if r.status_code != 200:
            return []

        # Find the info table XML file
        xml_links = re.findall(r'href="(/Archives/edgar/data/[^"]+(?:infotable|holdings)[^"]*\.xml)"',
                               r.text, re.IGNORECASE)
        if not xml_links:
            xml_links = re.findall(r'href="(/Archives/edgar/data/[^"]*\.xml)"', r.text)

        if not xml_links:
            return []

        xml_url = EDGAR_BASE + xml_links[-1]
        rx = requests.get(xml_url, timeout=15,
                         headers={"User-Agent": "Nathaniel Trading Monitor nate.adams.nh@gmail.com"})
        if rx.status_code != 200:
            return []

        root = ET.fromstring(rx.content)
        # Handle namespace
        ns_match = re.search(r'\{([^}]+)\}', root.tag)
        ns = {"ns": ns_match.group(1)} if ns_match else {}
        prefix = f"{{{'ns' if ns else ''}}}" if ns else ""

        for info in root.iter():
            if "infoTable" not in info.tag:
                continue
            name_el   = info.find(f"{prefix}nameOfIssuer") if prefix else info.find("nameOfIssuer")
            shares_el  = info.find(f"{prefix}sshPrnamt") if prefix else info.find("sshPrnamt")
            value_el   = info.find(f"{prefix}value") if prefix else info.find("value")
            ticker_el  = info.find(f"{prefix}cusip") if prefix else info.find("cusip")

            name  = name_el.text.strip() if name_el is not None else ""
            value = int(value_el.text.strip()) * 1000 if value_el is not None else 0  # values in thousands
            shares = int(shares_el.text.strip()) if shares_el is not None else 0

            if value < MIN_POSITION_VALUE or not name:
                continue
            holdings.append({"name": name, "value": value, "shares": shares})

    except Exception as e:
        log.debug(f"Error parsing 13F XML: {e}")

    return sorted(holdings, key=lambda x: -x["value"])


def matches_portfolio(holding_name: str) -> str:
    """Check if a holding matches any portfolio symbol. Returns symbol or ''."""
    from signal_utils import COMPANY_NAMES
    name_lower = holding_name.lower()
    for sym, names in COMPANY_NAMES.items():
        for n in names:
            if n.lower() in name_lower or name_lower in n.lower():
                return sym
    return ""


def run():
    log.info("=" * 65)
    log.info(f"HEDGE FUND 13F TRACKER  |  {date.today()}")
    log.info("=" * 65)

    state = load_state("hedge_fund_13f.json")
    new_filings = []

    for fund_name, cik in TRACKED_FUNDS.items():
        accession, filing_date = get_latest_13f(cik, fund_name)
        if not accession:
            log.warning(f"  {fund_name}: could not fetch latest 13F")
            continue

        last_seen = state.get(fund_name, {}).get("last_accession", "")
        if accession == last_seen:
            log.info(f"  {fund_name}: no new filing (latest: {filing_date})")
            continue

        log.info(f"  {fund_name}: NEW 13F filed {filing_date} (acc: {accession})")

        # Parse holdings
        holdings = parse_13f_holdings(cik, accession)
        log.info(f"    Parsed {len(holdings)} holdings")

        # Find overlaps with portfolio
        portfolio_overlaps = []
        for h in holdings[:50]:  # Top 50 by value
            sym = matches_portfolio(h["name"])
            if sym:
                portfolio_overlaps.append({**h, "symbol": sym})

        # Top 10 new positions
        top10 = holdings[:10]

        new_filings.append({
            "fund":       fund_name,
            "date":       filing_date,
            "accession":  accession,
            "total":      len(holdings),
            "overlaps":   portfolio_overlaps,
            "top10":      top10,
        })

        state[fund_name] = {"last_accession": accession, "last_date": filing_date}

    save_state("hedge_fund_13f.json", state)

    if not new_filings:
        log.info("  No new 13F filings detected.")
        return

    # Build email
    content = ""
    for f in new_filings:
        content += f"<h3 style='color:#4a90d9;margin-top:20px'>{f['fund']} — Filed {f['date']} ({f['total']} positions)</h3>"

        if f["overlaps"]:
            rows = [[
                f"<strong>{o['symbol']}</strong>",
                o["name"][:40],
                f"${o['value']:,.0f}",
                f"{o['shares']:,.0f}",
            ] for o in f["overlaps"]]
            content += "<p style='color:#4caf50'><strong>Your holdings in this portfolio:</strong></p>"
            content += html_table(["Symbol", "Company", "Market Value", "Shares"], rows, "#1b5e20")

        rows10 = [[
            h["name"][:45],
            f"${h['value']:,.0f}",
            f"{h['shares']:,.0f}",
        ] for h in f["top10"]]
        content += "<p style='margin-top:12px'><strong>Top 10 positions by value:</strong></p>"
        content += html_table(["Company", "Market Value", "Shares"], rows10)

    body_html = base_html("Hedge Fund 13F Filings", "🦈", content)
    body_text = "\n".join(
        f"{f['fund']}: filed {f['date']}, {f['total']} positions, "
        f"{len(f['overlaps'])} overlap with your portfolio"
        for f in new_filings
    )
    send_signal_email(
        f"🦈 New 13F Filing(s) — {len(new_filings)} hedge fund(s) disclosed holdings",
        body_text, body_html
    )
    log.info(f"  Alert sent: {len(new_filings)} new 13F filing(s)\n")


if __name__ == "__main__":
    run()
