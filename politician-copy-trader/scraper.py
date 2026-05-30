#!/usr/bin/env python3
"""
Capitol Trades scraper.
Fetches the most recent trades for a given politician ID.
Tries __NEXT_DATA__ JSON first (Next.js SSR), falls back to HTML table parsing.
"""

import json
import re
import logging
import requests
from bs4 import BeautifulSoup
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

log = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.capitoltrades.com/",
}


@dataclass
class PoliticianTrade:
    ticker: str       # "AAPL:US" or "N/A"
    company: str      # "Apple Inc"
    tx_date: str      # "YYYY-MM-DD"
    filed_date: str   # "YYYY-MM-DD"
    tx_type: str      # "buy" or "sell"
    size_range: str   # "1K-15K"
    price: Optional[float]

    @property
    def unique_key(self) -> str:
        return f"{self.ticker}|{self.tx_date}|{self.tx_type}|{self.size_range}"

    @property
    def alpaca_symbol(self) -> str:
        # "BRK/B:US" -> "BRK.B",  "AAPL:US" -> "AAPL"
        symbol = self.ticker.replace(":US", "").replace("/", ".")
        return symbol.upper().strip(".")

    def is_tradeable(self, skip_keywords: list) -> bool:
        if not self.ticker or self.ticker in ("N/A", ""):
            return False
        combined = (self.ticker + " " + self.company).lower()
        if any(kw in combined for kw in skip_keywords):
            return False
        if ":US" not in self.ticker:
            return False
        return True


def _parse_date(raw: str) -> str:
    raw = raw.strip()
    for fmt in ("%d %b %Y", "%Y-%m-%d", "%b %d, %Y", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


def fetch_trades(politician_id: str, page_size: int = 20) -> List[PoliticianTrade]:
    url = (
        f"https://www.capitoltrades.com/trades"
        f"?politician={politician_id}&sortBy=-txDate&pageSize={page_size}"
    )
    log.info(f"Fetching: {url}")
    resp = requests.get(url, headers=_HEADERS, timeout=30)
    resp.raise_for_status()

    trades = _try_next_data(resp.text)
    if trades is not None:
        log.info(f"Parsed {len(trades)} trades via __NEXT_DATA__")
        return trades

    trades = _try_html_table(resp.text)
    log.info(f"Parsed {len(trades)} trades via HTML table")
    return trades


# ── Next.js __NEXT_DATA__ path ─────────────────────────────────────────────────

def _try_next_data(html: str) -> Optional[List[PoliticianTrade]]:
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    if not script:
        return None
    try:
        data = json.loads(script.string)
    except (json.JSONDecodeError, AttributeError):
        return None

    page_props = data.get("props", {}).get("pageProps", {})
    trades_raw = _find_trades_list(page_props)
    if not trades_raw:
        log.debug("__NEXT_DATA__ found but no trades array located in pageProps")
        return None

    results = []
    for t in trades_raw:
        try:
            trade = _parse_json_trade(t)
            if trade:
                results.append(trade)
        except Exception as e:
            log.debug(f"JSON trade parse error: {e}")
    return results if results else None


def _find_trades_list(obj, depth=0):
    """Recursively search for a list that looks like trades data."""
    if depth > 5:
        return None
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        first = obj[0]
        trade_keys = {"txDate", "filedDate", "type", "txType", "issuer", "ticker"}
        if trade_keys & set(first.keys()):
            return obj
    if isinstance(obj, dict):
        for key in ("trades", "data", "items", "results", "initialData"):
            if key in obj:
                result = _find_trades_list(obj[key], depth + 1)
                if result:
                    return result
        for v in obj.values():
            result = _find_trades_list(v, depth + 1)
            if result:
                return result
    return None


def _parse_json_trade(t: dict) -> Optional[PoliticianTrade]:
    issuer = t.get("issuer") or {}
    ticker = issuer.get("ticker") or t.get("ticker") or "N/A"
    company = issuer.get("name") or t.get("issuerName") or t.get("company") or "Unknown"

    tx_date_raw = t.get("txDate") or t.get("transactionDate") or ""
    filed_date_raw = t.get("filedDate") or t.get("publishDate") or t.get("filedAt") or ""
    tx_type_raw = (t.get("txType") or t.get("transactionType") or t.get("type") or "").lower()
    size_range = str(t.get("size") or t.get("txSize") or t.get("amount") or "")
    price_raw = t.get("price")
    price = float(price_raw) if price_raw else None

    if not tx_date_raw:
        return None
    if tx_type_raw not in ("buy", "sell", "exchange", "sale"):
        return None

    return PoliticianTrade(
        ticker=ticker,
        company=company,
        tx_date=_parse_date(tx_date_raw),
        filed_date=_parse_date(filed_date_raw),
        tx_type="sell" if tx_type_raw in ("sell", "exchange", "sale") else "buy",
        size_range=size_range,
        price=price,
    )


# ── HTML table fallback ────────────────────────────────────────────────────────

def _try_html_table(html: str) -> List[PoliticianTrade]:
    soup = BeautifulSoup(html, "html.parser")
    rows = (
        soup.select("table tbody tr")
        or soup.select("tr[class*='trade']")
        or soup.select("[class*='trades-table'] tr")
    )

    results = []
    for row in rows:
        try:
            trade = _parse_html_row(row)
            if trade:
                results.append(trade)
        except Exception as e:
            log.debug(f"HTML row parse error: {e}")
    return results


_DATE_RE = re.compile(r"\d{1,2}\s+[A-Za-z]{3}\s+\d{4}")
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# Matches tickers like AAPL:US, BRK/B:US, BRK.B:US
_TICKER_RE = re.compile(r"([A-Z]{1,6}(?:[./][A-Z]{1,2})?):US")
_SIZE_RE = re.compile(r"\$?(\d+(?:\.\d+)?[KMB])\s*[-–]\s*\$?(\d+(?:\.\d+)?[KMB])", re.I)
_PRICE_RE = re.compile(r"\$(\d[\d,]*\.\d{2})")


def _parse_html_row(row) -> Optional[PoliticianTrade]:
    text = row.get_text(" | ", strip=True)
    cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
    if len(cells) < 4:
        return None

    # Ticker — prefer the full match including slashes (BRK/B:US)
    ticker_match = _TICKER_RE.search(text)
    if ticker_match:
        # group(0) is full match e.g. "BRK/B:US", group(1) is just the symbol part
        ticker = ticker_match.group(0)  # e.g. "BRK/B:US"
    else:
        ticker = "N/A"

    # Company name — strip ticker suffix from the cell
    company = cells[1] if len(cells) > 1 else "Unknown"
    company = _TICKER_RE.sub("", company).strip(" :|")
    if not company:
        company = "Unknown"

    # Dates — try "28 Apr 2026" format first, then ISO "2026-04-28"
    dates = _DATE_RE.findall(text)
    if not dates:
        dates = _ISO_DATE_RE.findall(text)
    filed_date = _parse_date(dates[0]) if dates else ""
    tx_date = _parse_date(dates[1]) if len(dates) > 1 else filed_date

    # Transaction type
    text_lower = text.lower()
    if "sell" in text_lower or "exchange" in text_lower:
        tx_type = "sell"
    elif "buy" in text_lower or "purchase" in text_lower:
        tx_type = "buy"
    else:
        return None

    # Size range
    size_match = _SIZE_RE.search(text)
    size_range = f"{size_match.group(1)}-{size_match.group(2)}" if size_match else ""

    # Price
    price_match = _PRICE_RE.search(text)
    price = float(price_match.group(1).replace(",", "")) if price_match else None

    return PoliticianTrade(
        ticker=ticker,
        company=company,
        tx_date=tx_date,
        filed_date=filed_date,
        tx_type=tx_type,
        size_range=size_range,
        price=price,
    )
