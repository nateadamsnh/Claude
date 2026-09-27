#!/usr/bin/env python3
"""
Capital Allocator — shared, pure weight-split across strategies
=================================================================
Multiple strategies can trade the same Alpaca paper account. Without this,
each strategy would treat the account's full buying power as its own and
they'd silently compete for capital. This module reads a shared weight
registry (config/strategy_framework.json) and hands each strategy its
proportional share of live account numbers.

Pure functions only — no network, no file writes. A missing/broken registry
file fails open to "the caller owns 100%", so a strategy that isn't (yet)
registered, or the file itself being absent, never blocks trading.
"""

import json
from pathlib import Path

DEFAULT_REGISTRY = {"strategies": [{"name": "wheel", "weight": 1.0, "enabled": True}]}

BASE_DIR      = Path(__file__).parent.parent
REGISTRY_FILE = BASE_DIR / "config" / "strategy_framework.json"


def load_registry(path=None) -> dict:
    """Return the parsed registry, falling back to DEFAULT_REGISTRY if the
    file is missing or unparseable (fail safe — allocation always works)."""
    path = path or REGISTRY_FILE
    if Path(path).exists():
        try:
            with open(path) as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("strategies"), list):
                return data
        except Exception:
            pass
    return DEFAULT_REGISTRY


def weight_fraction(name: str, registry: dict = None) -> float:
    """This strategy's weight divided by the sum of all ENABLED weights.
    Returns 1.0 if `name` isn't found in the registry (fail-open: an
    unregistered strategy still gets to run standalone)."""
    registry = registry if registry is not None else load_registry()
    entries  = registry.get("strategies", [])
    enabled  = [e for e in entries if e.get("enabled", True)]

    mine = next((e for e in enabled if e.get("name") == name), None)
    if mine is None:
        return 1.0

    total = sum(float(e.get("weight", 0)) for e in enabled)
    if total <= 0:
        return 1.0

    return float(mine.get("weight", 0)) / total


def allocated_buying_power(name: str, account: dict, registry: dict = None) -> float:
    """This strategy's share of the account's options buying power (falls
    back to cash if options_buying_power is absent/zero)."""
    obp = float(account.get("options_buying_power") or account.get("cash") or 0)
    return weight_fraction(name, registry) * obp


def allocated_equity(name: str, account: dict, registry: dict = None) -> float:
    """This strategy's share of total account equity — for reporting and
    future per-strategy kill-switch use (not consumed by the wheel today)."""
    equity = float(account.get("equity") or 0)
    return weight_fraction(name, registry) * equity
