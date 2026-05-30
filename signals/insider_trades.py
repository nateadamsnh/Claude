#!/usr/bin/env python3
"""
SEC Form 4 — Corporate Insider Trade Monitor
=============================================
Monitors SEC EDGAR for Form 4 filings (insider trades) by executives
and directors at companies in Nathaniel's portfolio.

Alerts ONLY on insider BUYING — executives sell for many reasons
(taxes, diversification), but buying with their own money is a
strong conviction signal.

Runs every 2 hours during market hours.
"""

import re
import requests
import xml.etree.ElementTree as ET
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from signal_utils import (
    get_logger, load_state, save_state, send_signal_email,
    html_table, base_html, PORTFOLIO_SYMBOLS, COMPANY_NAMES
)

log = get_logger("insider_trades", "insider_trades.log")

EDGAR_RSS = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&dateb=&owner=include&count=100&output=atom"
EDGAR_BASE = "https://www.sec.gov"
MIN_PURCHASE_VALUE = 10_000  # Only alert on purchases >= $10K


def fetch_recent_form4_filings() -> list:
    """Fetch the latest Form 4 filings from EDGAR RSS feed."""
    try:
        r = requests.get(EDGAR_RSS, timeout=15,
                        headers={"User-Agent": "Nathaniel Trading Monitor nate.adams.nh@gmail.com"})
        if r.status_code != 200:
            log.error(f"EDGAR RSS returned {r.status_code}")
            return []
        root = ET.fromstring(r.content)
        ns   = {"atom": "http://www.w3.org/2005/Atom"}
        entries = []
        for entry in root.findall("atom:entry", ns):
            title     = entry.findtext("atom:title", "", ns)
            link_el   = entry.find("atom:link", ns)
            filing_id = entry.findtext("atom:id", "", ns)
            updated   = entry.findtext("atom:updated", "", ns)
            entries.append({
                "title":      title,
                "link":       link_el.attrib.get("href", "") if link_el is not None else "",
                "filing_id":  filing_id,
                "updated":    updated,
            })
        return entries
    except Exception as e:
        log.error(f"Error fetching EDGAR RSS: {e}")
        return []


def parse_form4_detail(filing_url: str) -> dict:
    """
    Parse a Form 4 filing index page to extract transaction details.
    Returns dict with: issuer, insider_name, transaction_type, shares, price_per_share, total_value
    """
    result = {"issuer": "", "insider": "", "tx_type": "", "shares": 0, "price": 0.0, "total": 0.0, "ticker": ""}
    try:
        r = requests.get(filing_url, timeout=10,
                        headers={"User-Agent": "Nathaniel Trading Monitor nate.adams.nh@gmail.com"})
        if r.status_code != 200:
            return result

        text = r.text
        # Find links to actual XML filing
        xml_links = re.findall(r'href="(/Archives/edgar/data/[^"]+\.xml)"', text)
        if not xml_links:
            return result

        # Fetch the first XML (primary document)
        xml_url = EDGAR_BASE + xml_links[0]
        r2 = requests.get(xml_url, timeout=10,
                         headers={"User-Agent": "Nathaniel Trading Monitor nate.adams.nh@gmail.com"})
        if r2.status_code != 200:
            return result

        xml_text = r2.text
        # Extract issuer name and ticker
        issuer_match = re.search(r'<issuerName>(.*?)</issuerName>', xml_text)
        ticker_match = re.search(r'<issuerTradingSymbol>(.*?)</issuerTradingSymbol>', xml_text)
        insider_match = re.search(r'<rptOwnerName>(.*?)</rptOwnerName>', xml_text)
        title_match = re.search(r'<officerTitle>(.*?)</officerTitle>', xml_text)

        result["issuer"]  = issuer_match.group(1).strip() if issuer_match else ""
        result["ticker"]  = ticker_match.group(1).strip().upper() if ticker_match else ""
        result["insider"] = insider_match.group(1).strip() if insider_match else ""
        result["title"]   = title_match.group(1).strip() if title_match else ""

        # Look for non-derivative transactions (direct stock purchases/sales)
        tx_codes   = re.findall(r'<transactionCode>(.*?)</transactionCode>', xml_text)
        shares_list = re.findall(r'<transactionShares>.*?<value>([\d.]+)</value>', xml_text, re.DOTALL)
        price_list  = re.findall(r'<transactionPricePerShare>.*?<value>([\d.]+)</value>', xml_text, re.DOTALL)

        purchases = [(c, s, p) for c, s, p in zip(
            tx_codes,
            shares_list[:len(tx_codes)],
            price_list[:len(tx_codes)]
        ) if c == "P"]  # P = purchase in open market

        if purchases:
            code, shares_str, price_str = purchases[0]
            shares = float(shares_str)
            price  = float(price_str)
            result["tx_type"] = "BUY"
            result["shares"]  = shares
            result["price"]   = price
            result["total"]   = shares * price

    except Exception as e:
        log.debug(f"Error parsing Form 4 detail: {e}")

    return result


def run():
    log.info("=" * 65)
    log.info(f"INSIDER TRADE MONITOR  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    log.info("=" * 65)

    state     = load_state("insider_trades.json")
    seen_ids  = set(state.get("seen_filing_ids", []))

    # Build lookup set of portfolio company names
    portfolio_names = set()
    for sym in PORTFOLIO_SYMBOLS:
        for name in COMPANY_NAMES.get(sym, []):
            portfolio_names.add(name.lower())

    filings    = fetch_recent_form4_filings()
    log.info(f"  Fetched {len(filings)} recent Form 4 filings from EDGAR")

    new_buys = []

    for filing in filings:
        fid   = filing["filing_id"]
        title = filing["title"].lower()

        if fid in seen_ids:
            continue
        seen_ids.add(fid)

        # Quick filter: does the title mention any of our companies?
        if not any(name in title for name in portfolio_names):
            continue

        # Fetch and parse the actual filing
        detail = parse_form4_detail(filing["link"])

        if detail["tx_type"] != "BUY":
            continue
        if detail["total"] < MIN_PURCHASE_VALUE:
            continue

        # Match ticker to portfolio
        ticker = detail["ticker"]
        if ticker not in PORTFOLIO_SYMBOLS:
            # Try name match
            matched = False
            for sym in PORTFOLIO_SYMBOLS:
                for name in COMPANY_NAMES.get(sym, []):
                    if name.lower() in detail["issuer"].lower():
                        ticker = sym
                        matched = True
                        break
                if matched:
                    break
            if not matched:
                continue

        new_buys.append({
            "symbol":  ticker,
            "issuer":  detail["issuer"],
            "insider": detail["insider"],
            "title":   detail.get("title", ""),
            "shares":  detail["shares"],
            "price":   detail["price"],
            "total":   detail["total"],
            "link":    filing["link"],
        })
        log.info(
            f"  [INSIDER BUY] {ticker} — {detail['insider']} ({detail.get('title','')}) "
            f"bought {detail['shares']:,.0f} shares @ ${detail['price']:.2f} "
            f"(${detail['total']:,.0f} total)"
        )

    # Save state
    state["seen_filing_ids"] = list(seen_ids)[-2000:]
    state["last_run"] = datetime.now().isoformat()
    save_state("insider_trades.json", state)

    if not new_buys:
        log.info("  No new insider buying detected in portfolio holdings.")
        return

    log.info(f"  Sending alert: {len(new_buys)} insider buy(s)")
    rows = [
        [
            f"<strong>{b['symbol']}</strong>",
            b["insider"],
            b["title"][:30],
            f"{b['shares']:,.0f}",
            f"${b['price']:.2f}",
            f"<strong>${b['total']:,.0f}</strong>",
        ]
        for b in sorted(new_buys, key=lambda x: -x["total"])
    ]
    table = html_table(
        ["Symbol", "Insider", "Title", "Shares", "Price", "Total Value"], rows,
        color="#1b5e20"
    )
    content = (
        f"<p style='color:#4caf50;font-weight:bold'>🟢 {len(new_buys)} insider purchase(s) detected in your holdings.</p>"
        f"<p>Executives buying their own stock with personal money is one of the strongest available signals.</p>"
        f"{table}"
    )
    body_html = base_html("Insider Buying Alert", "🔍", content)
    body_text = "\n".join(
        f"{b['symbol']}: {b['insider']} bought {b['shares']:,.0f} shares @ ${b['price']:.2f} (${b['total']:,.0f})"
        for b in new_buys
    )
    send_signal_email(
        f"🔍 Insider BUY Alert — {len(new_buys)} executive purchase(s) in your holdings",
        body_text, body_html
    )
    log.info("Run complete.\n")


if __name__ == "__main__":
    run()
