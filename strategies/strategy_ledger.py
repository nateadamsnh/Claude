#!/usr/bin/env python3
"""
Strategy Ledger — per-strategy realized P&L log, for cross-strategy comparison
================================================================================
Each strategy's own state file tracks its P&L in whatever schema suits it (the
wheel's premium_history, for example). That's fine for the strategy itself,
but a comparison report shouldn't need to understand every strategy's internal
schema. Instead, each strategy appends a small standardized event here at the
moment P&L is realized, and compare_strategies.py only ever reads this file.

One JSONL file per strategy: strategies/ledgers/{strategy}.jsonl. Each line is
a realized-event dict; the only field this module requires is "total" (the
dollar P&L of that event — positive for premium collected, negative for a
buy-to-close cost). Any other keys (date, type, contract, symbol, ...) are
carried through untouched.
"""

import json
from pathlib import Path

LEDGER_DIR = Path(__file__).parent / "ledgers"


def _ledger_path(strategy: str) -> Path:
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    return LEDGER_DIR / f"{strategy}.jsonl"


def record(strategy: str, event: dict):
    """Append one realized-P&L event for `strategy`. Never raises on I/O
    failure — a ledger write failing should never block the trade it's
    recording."""
    try:
        with open(_ledger_path(strategy), "a") as f:
            f.write(json.dumps(event) + "\n")
    except Exception:
        pass


def _read_events(strategy: str) -> list:
    path = _ledger_path(strategy)
    if not path.exists():
        return []
    events = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except Exception:
                continue
    return events


def summarize(strategy: str) -> dict:
    """Total realized P&L, event count, and win rate for `strategy`."""
    events = _read_events(strategy)
    totals = [float(e.get("total", 0) or 0) for e in events]
    count  = len(totals)
    wins   = sum(1 for t in totals if t > 0)
    return {
        "strategy":     strategy,
        "realized_pl":  round(sum(totals), 2),
        "trade_count":  count,
        "win_rate":     round(wins / count, 3) if count else 0.0,
    }
