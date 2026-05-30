#!/usr/bin/env python3
"""
Government Contract Awards Monitor
====================================
Monitors USASpending.gov for federal contract awards to companies
in Nathaniel's portfolio. Alerts when a significant contract is awarded
to LMT, BWXT, IONQ, RKLB, PLTR, IBM, MSFT, NVDA.

Runs daily at 6:00 PM ET (after market close).
"""

import requests
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from signal_utils import (
    get_logger, load_state, save_state, send_signal_email,
    html_table, base_html, GOVT_CONTRACT_SYMBOLS, COMPANY_NAMES
)

log = get_logger("contract_awards", "contract_awards.log")

USASPENDING_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
MIN_AWARD_AMOUNT = 1_000_000  # Only alert on awards >= $1M

# Map symbols to search terms for USASpending
SEARCH_TERMS = {
    "LMT":  ["Lockheed Martin"],
    "BWXT": ["BWX Technologies", "BWXT Government"],
    "IONQ": ["IonQ"],
    "RKLB": ["Rocket Lab"],
    "PLTR": ["Palantir"],
    "IBM":  ["International Business Machines", "IBM Corporation"],
    "MSFT": ["Microsoft Corporation"],
    "NVDA": ["NVIDIA Corporation"],
}


def search_awards(recipient: str, start_date: str, end_date: str) -> list:
    """Search USASpending for contract awards to a recipient."""
    payload = {
        "filters": {
            "award_type_codes": ["A", "B", "C", "D"],  # Contracts only
            "recipient_search_text": [recipient],
            "time_period": [{"start_date": start_date, "end_date": end_date}],
        },
        "fields": [
            "Award ID", "Recipient Name", "Award Amount",
            "Start Date", "Description", "Awarding Agency",
            "Awarding Sub Agency",
        ],
        "sort": "Award Amount",
        "order": "desc",
        "limit": 10,
        "page": 1,
    }
    try:
        r = requests.post(USASPENDING_URL, json=payload, timeout=20)
        if r.status_code == 200:
            return r.json().get("results", [])
    except Exception as e:
        log.error(f"USASpending API error for '{recipient}': {e}")
    return []


def run():
    log.info("=" * 65)
    log.info(f"CONTRACT AWARDS MONITOR  |  {date.today()}")
    log.info("=" * 65)

    state     = load_state("contract_awards.json")
    # Use a list (not set) to preserve insertion order for deterministic [-500:] trim
    seen_ids_list = state.get("seen_award_ids", [])
    seen_ids      = set(seen_ids_list)  # set for O(1) lookup; list preserved for ordered save

    # Search past 2 days to catch any delays in reporting
    end_date   = date.today().isoformat()
    start_date = (date.today() - timedelta(days=2)).isoformat()

    new_awards = []

    for symbol, search_terms in SEARCH_TERMS.items():
        for term in search_terms:
            awards = search_awards(term, start_date, end_date)
            for a in awards:
                award_id = a.get("Award ID", "")
                amount   = float(a.get("Award Amount") or 0)

                if award_id in seen_ids:
                    continue
                if amount < MIN_AWARD_AMOUNT:
                    continue

                seen_ids.add(award_id)
                seen_ids_list.append(award_id)
                # "Start Date" is a USASpending display alias; fall back to the canonical field
                award_date = (a.get("Start Date")
                              or a.get("period_of_performance_start_date")
                              or a.get("Award Date", ""))
                new_awards.append({
                    "symbol":    symbol,
                    "award_id":  award_id,
                    "recipient": a.get("Recipient Name", term),
                    "amount":    amount,
                    "date":      award_date,
                    "agency":    a.get("Awarding Agency", ""),
                    "desc":      (a.get("Description") or "")[:120],
                })
                log.info(
                    f"  [{symbol}] NEW CONTRACT: ${amount:,.0f} from "
                    f"{a.get('Awarding Agency') or '?'} | {(a.get('Description') or '')[:60]}"
                )

    # Save state — keep the 500 most recently added IDs (list preserves insertion order)
    state["seen_award_ids"] = seen_ids_list[-500:]
    state["last_run"] = end_date
    save_state("contract_awards.json", state)

    if not new_awards:
        log.info("  No new significant contracts found.")
        return

    # Send email
    log.info(f"  Sending alert: {len(new_awards)} new contract(s)")
    rows = [
        [
            f"<strong>{a['symbol']}</strong>",
            f"${a['amount']:,.0f}",
            a["agency"][:40],
            a["date"],
            a["desc"][:80],
        ]
        for a in sorted(new_awards, key=lambda x: -x["amount"])
    ]
    table = html_table(
        ["Symbol", "Award Amount", "Agency", "Date", "Description"], rows
    )
    content = f"<p>Found <strong>{len(new_awards)}</strong> new federal contract award(s) for your holdings:</p>{table}"
    body_html = base_html("Government Contract Awards", "🏛️", content)
    body_text = "\n".join(
        f"{a['symbol']}: ${a['amount']:,.0f} from {a['agency']}" for a in new_awards
    )
    send_signal_email(
        f"🏛️ Contract Alert — {len(new_awards)} new award(s) for your holdings",
        body_text, body_html
    )
    log.info("Run complete.\n")


if __name__ == "__main__":
    run()
