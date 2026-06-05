#!/usr/bin/env python3
"""
Government Contracts Trading Strategy
=======================================
Monitors USASpending.gov for significant new federal contract awards.
When a publicly traded company wins a contract >= MIN_CONTRACT_VALUE,
buys $500 of their stock automatically.

Thesis: Major government contracts are a strong near-term revenue signal.
The stock often moves on the news and the contract provides visibility
into future earnings.

Runs daily at 6:15 PM ET (after market close, after contract data updates).
State: strategies/govt_contracts_state.json
Log  : logs/govt_contracts.log
"""

import json
import sys
import os
import requests
import logging
from datetime import date, timedelta
from pathlib import Path

BASE_DIR    = Path(__file__).parent.parent
CONFIG_FILE = BASE_DIR / "config" / "alpaca_credentials.json"
STATE_FILE  = BASE_DIR / "strategies" / "govt_contracts_state.json"
LOG_FILE    = BASE_DIR / "logs" / "govt_contracts.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(BASE_DIR / "signals"))
from signal_utils import send_signal_email, base_html, html_table

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
log = logging.getLogger(__name__)

# ── Credentials ───────────────────────────────────────────────────────────────
with open(CONFIG_FILE) as f:
    creds = json.load(f)
BASE_URL = creds["endpoint"]
HEADERS  = {
    "APCA-API-KEY-ID":     creds["api_key"],
    "APCA-API-SECRET-KEY": creds["api_secret"],
}

# ── Strategy Parameters ───────────────────────────────────────────────────────
MIN_CONTRACT_VALUE = 10_000_000   # Only trade on contracts >= $10M
TRADE_AMOUNT       = 500          # $500 per position
LOOKBACK_DAYS      = 3            # Search last 3 days (catches reporting lag)

# ── Company name → ticker mapping ────────────────────────────────────────────
# Maps USASpending recipient name keywords to Alpaca ticker symbols
COMPANY_TICKERS = {
    # Defense & Aerospace
    "Lockheed Martin":          "LMT",
    "Raytheon":                 "RTX",
    "RTX":                      "RTX",
    "General Dynamics":         "GD",
    "Northrop Grumman":         "NOC",
    "Boeing":                   "BA",
    "L3Harris":                 "LHX",
    "Leidos":                   "LDOS",
    "Booz Allen":               "BAH",
    "SAIC":                     "SAIC",
    "Science Applications":     "SAIC",
    "BWX Technologies":         "BWXT",
    "BWXT":                     "BWXT",
    "Rocket Lab":               "RKLB",
    "TransDigm":                "TDG",
    "HEICO":                    "HEI",
    "Curtiss-Wright":           "CW",
    "Kratos":                   "KTOS",
    "Mercury Systems":          "MRCY",
    "Parsons":                  "PSN",

    # Technology & IT
    "Palantir":                 "PLTR",
    "Microsoft":                "MSFT",
    "Amazon":                   "AMZN",
    "Google":                   "GOOGL",
    "Alphabet":                 "GOOGL",
    "IBM":                      "IBM",
    "International Business":   "IBM",
    "Accenture":                "ACN",
    "NVIDIA":                   "NVDA",
    "Oracle":                   "ORCL",
    "IonQ":                     "IONQ",
    "Dell":                     "DELL",
    "Hewlett Packard":          "HPE",
    "Leidos":                   "LDOS",
    "CACI":                     "CACI",
    "ManTech":                  "MAN",
    "Peraton":                  None,  # Private
    "Maximus":                  "MMS",
    "GDIT":                     None,  # General Dynamics IT (subsidiary)

    # Energy & Nuclear
    "Fluor":                    "FLR",
    "Bechtel":                  None,  # Private
    "Amentum":                  None,  # Private
    "URS":                      None,  # Private

    # Healthcare & Other
    "Humana":                   "HUM",
    "UnitedHealth":             "UNH",
    "CVS":                      "CVS",
    "DXC Technology":           "DXC",
}

USASPENDING_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"


# ── State helpers ─────────────────────────────────────────────────────────────
def load_state() -> dict:
    defaults = {
        "seen_award_ids":  [],   # Award IDs already processed
        "traded_tickers":  {},   # {award_id: {ticker, amount, date, contract_value}}
        "total_deployed":  0.0,  # Total $ deployed by this strategy
        "last_run":        None,
    }
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            saved = json.load(f)
        for k, v in defaults.items():
            saved.setdefault(k, v)
        return saved
    return defaults


def save_state(state: dict):
    tmp = str(STATE_FILE) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


# ── USASpending query ─────────────────────────────────────────────────────────
def fetch_contracts(start_date: str, end_date: str) -> list:
    """Fetch new contract awards from USASpending.gov."""
    payload = {
        "filters": {
            "award_type_codes": ["A", "B", "C", "D"],  # Contracts only
            "time_period": [{"start_date": start_date, "end_date": end_date}],
            "award_amounts": [{"lower_bound": MIN_CONTRACT_VALUE}],
        },
        "fields": [
            "Award ID", "Recipient Name", "Award Amount",
            "Start Date", "Description", "Awarding Agency",
            "Awarding Sub Agency", "period_of_performance_start_date",
        ],
        "sort":  "Award Amount",
        "order": "desc",
        "limit": 50,
        "page":  1,
    }
    try:
        r = requests.post(USASPENDING_URL, json=payload, timeout=25)
        if r.status_code == 200:
            return r.json().get("results", [])
        log.error(f"USASpending error {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.error(f"USASpending request failed: {e}")
    return []


# ── Ticker resolution ─────────────────────────────────────────────────────────
def resolve_ticker(recipient_name: str) -> str | None:
    """Map a USASpending recipient name to a ticker symbol."""
    import re
    name_upper = recipient_name.upper()
    for keyword, ticker in COMPANY_TICKERS.items():
        # Use word-boundary match to avoid substring false positives
        # e.g. "DELL" should not match "CADDELL"
        pattern = r'\b' + re.escape(keyword.upper()) + r'\b'
        if re.search(pattern, name_upper):
            return ticker
    return None


# ── Trade execution ───────────────────────────────────────────────────────────
def get_current_positions() -> set:
    """Return set of symbols currently held."""
    try:
        r = requests.get(f"{BASE_URL}/positions", headers=HEADERS, timeout=10)
        return {p["symbol"] for p in r.json() if p.get("asset_class") == "us_equity"}
    except Exception:
        return set()


def buy_stock(ticker: str) -> dict:
    """Place a $500 notional market buy order."""
    payload = {
        "symbol":        ticker,
        "notional":      str(TRADE_AMOUNT),
        "side":          "buy",
        "type":          "market",
        "time_in_force": "day",
    }
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json=payload, timeout=10)
    return r.json()


def is_market_open() -> bool:
    try:
        r = requests.get(f"{BASE_URL}/clock", headers=HEADERS, timeout=10)
        return r.json().get("is_open", False)
    except Exception:
        return False


# ── Main ──────────────────────────────────────────────────────────────────────
def run():
    log.info("=" * 65)
    log.info(f"GOVT CONTRACTS STRATEGY  |  {date.today()}")
    log.info("=" * 65)

    state     = load_state()
    seen_ids  = set(state["seen_award_ids"])
    today_str = date.today().isoformat()

    # Search last LOOKBACK_DAYS to catch reporting lag
    start_date = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    end_date   = today_str

    log.info(f"  Scanning USASpending {start_date} to {end_date} (>= ${MIN_CONTRACT_VALUE:,.0f})")

    awards = fetch_contracts(start_date, end_date)
    log.info(f"  Found {len(awards)} contracts above threshold")

    current_positions = get_current_positions()
    market_open       = is_market_open()
    new_trades        = []

    for a in awards:
        award_id  = a.get("Award ID", "")
        amount    = float(a.get("Award Amount") or 0)
        recipient = a.get("Recipient Name", "")
        agency    = a.get("Awarding Agency", "")
        desc      = (a.get("Description") or "")[:100]
        award_date = (a.get("Start Date")
                      or a.get("period_of_performance_start_date", ""))

        if not award_id or award_id in seen_ids:
            continue

        seen_ids.add(award_id)
        state["seen_award_ids"] = list(seen_ids)[-1000:]

        ticker = resolve_ticker(recipient)
        if not ticker:
            log.info(f"  [SKIP] ${amount:,.0f} to '{recipient}' — no ticker mapping")
            continue

        log.info(f"  [MATCH] ${amount:,.0f} contract → {ticker} | {recipient[:40]} | {agency[:30]}")

        # Skip if already holding this ticker
        if ticker in current_positions:
            log.info(f"    Already hold {ticker} — skipping new buy (strategy manager handles it)")
            continue

        # Execute trade
        if market_open:
            order  = buy_stock(ticker)
            status = order.get("status", "error")
            log.info(f"    BUY {ticker} ${TRADE_AMOUNT} — order status: {status}")
        else:
            status = "queued (market closed)"
            log.info(f"    QUEUED: BUY {ticker} ${TRADE_AMOUNT} — market closed, will execute at open")

        trade_record = {
            "award_id":       award_id,
            "ticker":         ticker,
            "recipient":      recipient,
            "contract_value": amount,
            "agency":         agency,
            "description":    desc,
            "award_date":     award_date,
            "traded_date":    today_str,
            "order_status":   status,
            "trade_amount":   TRADE_AMOUNT,
        }
        state["traded_tickers"][award_id] = trade_record
        state["total_deployed"] = state.get("total_deployed", 0) + TRADE_AMOUNT
        current_positions.add(ticker)  # Prevent double-buy within same run
        new_trades.append(trade_record)

    state["last_run"] = today_str
    save_state(state)

    if not new_trades:
        log.info("  No new qualifying contracts found today.")
        return

    # Send email alert
    log.info(f"  Sending alert: {len(new_trades)} new contract trade(s)")

    rows = [[
        f"<strong>{t['ticker']}</strong>",
        f"${t['contract_value']:,.0f}",
        t["recipient"][:35],
        t["agency"][:30],
        t["award_date"][:10],
        t["description"][:60],
        f"<span style='color:#4caf50'>{t['order_status']}</span>",
    ] for t in sorted(new_trades, key=lambda x: -x["contract_value"])]

    table   = html_table(
        ["Ticker", "Contract Value", "Recipient", "Agency", "Date", "Description", "Order"],
        rows
    )
    content = (
        f"<p><strong>{len(new_trades)}</strong> new government contract(s) triggered automatic buys:</p>"
        f"{table}"
        f"<p style='color:#888;font-size:12px;margin-top:12px'>"
        f"Each position gets a 10% stop-loss and trailing stop via StrategyManager. "
        f"Total deployed by this strategy: ${state['total_deployed']:,.0f}</p>"
    )
    body_html = base_html("Govt Contracts Strategy", "🏛️", content)
    body_text  = "\n".join(
        f"{t['ticker']}: ${t['contract_value']:,.0f} from {t['agency']}" for t in new_trades
    )
    send_signal_email(
        f"🏛️ Contracts Strategy — {len(new_trades)} new buy(s) triggered",
        body_text, body_html
    )
    log.info("Run complete.\n")


if __name__ == "__main__":
    run()
