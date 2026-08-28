#!/usr/bin/env python3
"""
Wheel Safety Layer — pure, testable guards for the wheel strategy
=================================================================
Everything here takes plain data IN and returns a decision OUT — no network,
no module globals, no file or log I/O. That means the whole module is exercised
by unit tests without touching Alpaca, and the live strategy (options_wheel.py)
just calls these with real data.

Three responsibilities:
  1. Reconciliation  — compare local state to the broker's actual positions and
                       refuse to act when they disagree (the root cause of every
                       bug found so far: acting on state that doesn't match reality).
  2. Hard limits     — money-proportional caps enforced independently of strategy
                       logic: contracts per symbol, notional per symbol, minimum
                       premium.
  3. Daily-loss kill — halt NEW entries (not liquidation) when the account is down
                       more than a threshold on the day.

None of these place orders or close positions. They only ever say "no" to an
action the strategy is about to take, or surface a mismatch for a human.
"""

import re
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path


# ══════════════════════════════════════════════════════════════════════════════
# Limits config
# ══════════════════════════════════════════════════════════════════════════════

DEFAULT_LIMITS = {
    "max_contracts_per_symbol": 5,        # never sell more than this many at once
    "max_notional_per_symbol":  15000.0,  # strike * 100 * qty cap (collateral guard)
    "min_premium_per_contract": 0.05,     # don't sell near-worthless premium
    "max_daily_loss_pct":       0.10,     # halt NEW entries if equity down > this on the day
}


def load_limits(path=None) -> dict:
    """Return DEFAULT_LIMITS overlaid with any values from a JSON file at `path`.
    Unknown keys in the file are ignored; a missing/broken file falls back to
    defaults (fail safe — the limits are always present)."""
    limits = dict(DEFAULT_LIMITS)
    if path and Path(path).exists():
        try:
            with open(path) as f:
                user = json.load(f)
            for k in DEFAULT_LIMITS:
                if k in user:
                    limits[k] = user[k]
        except Exception:
            pass  # keep defaults on any parse error
    return limits


# ══════════════════════════════════════════════════════════════════════════════
# OCC option-symbol parsing
# ══════════════════════════════════════════════════════════════════════════════

_OCC_RE = re.compile(r"^([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


def parse_occ(symbol: str) -> dict | None:
    """Parse an OCC option symbol like 'SOFI260710P00015500' into its parts, or
    None if it isn't an option symbol. strike = last 8 digits / 1000."""
    m = _OCC_RE.match(symbol or "")
    if not m:
        return None
    underlying, yy, mm, dd, cp, strike = m.groups()
    try:
        exp = date(2000 + int(yy), int(mm), int(dd))
    except ValueError:
        return None
    return {
        "underlying": underlying,
        "expiration": exp,
        "type":       "call" if cp == "C" else "put",
        "strike":     int(strike) / 1000.0,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Reconciliation
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ReconResult:
    ok: bool                              # False => a violation; caller should HALT the symbol
    violations: list = field(default_factory=list)   # serious state/broker disagreements
    untracked:  list = field(default_factory=list)   # broker short options not in state (alert only)


def _short_option_positions(symbol: str, positions: dict) -> dict:
    """{occ: position} for SHORT option positions (qty < 0) on `symbol`."""
    out = {}
    for psym, p in positions.items():
        info = parse_occ(psym)
        if not info or info["underlying"] != symbol:
            continue
        try:
            qty = float(p.get("qty", 0))
        except (TypeError, ValueError):
            qty = 0.0
        if qty < 0:
            out[psym] = p
    return out


def reconcile_symbol(symbol: str, state: dict, positions: dict, today: date = None) -> ReconResult:
    """Compare local `state` to the broker's actual `positions` for one symbol.

    `positions` is {symbol_or_occ: position_dict} exactly as the broker returns.

    Flags as VIOLATIONS (halt the symbol — do not act on bad state):
      - state tracks a filled short option the broker shows NEITHER as the
        contract NOR as assigned shares, AND the contract hasn't expired yet
        (it cannot have legitimately vanished before expiration).
      - CC stage expects N shares but the broker shows a different count.

    Flags as UNTRACKED (alert only — the tracked position is still consistent):
      - any short option the broker holds on this underlying that the state
        neither tracks as active nor lists under `untracked_open_positions`.
    """
    today = today or date.today()
    violations: list = []
    untracked:  list = []

    broker_shorts = _short_option_positions(symbol, positions)
    active        = state.get("active_contract")
    filled        = state.get("order_filled")
    acknowledged  = {u.get("contract") for u in state.get("untracked_open_positions", [])}

    # ── Violation: state believes it holds a filled short option that's gone ──
    has_shares = symbol in positions
    if active and filled and active not in broker_shorts and not has_shares:
        info = parse_occ(active)
        if info and today < info["expiration"]:
            violations.append(
                f"state tracks {active} as filled, but the broker shows neither that "
                f"contract nor {symbol} shares, and it doesn't expire until "
                f"{info['expiration']} — state is out of sync with reality"
            )
        # else: on/after expiration with no shares is a legitimate expiry —
        # leave it for the strategy's expiry handling, not a reconcile violation.

    # ── Violation: covered-call share count disagreement ─────────────────────
    if state.get("stage") == "CC":
        try:
            broker_shares = int(float(positions.get(symbol, {}).get("qty", 0) or 0))
        except (TypeError, ValueError):
            broker_shares = 0
        expected = int(state.get("shares_held", 0) or 0)
        if broker_shares != expected:
            violations.append(
                f"CC stage expects {expected} {symbol} shares but the broker shows "
                f"{broker_shares}"
            )

    # ── Untracked (alert only): broker shorts the state doesn't know about ────
    for psym in broker_shorts:
        if psym != active and psym not in acknowledged:
            untracked.append(psym)

    return ReconResult(ok=not violations, violations=violations, untracked=untracked)


# ══════════════════════════════════════════════════════════════════════════════
# Hard limits
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class LimitCheck:
    allowed: bool
    reason:  str = "ok"


def check_entry_limits(symbol: str, strike: float, qty: int,
                       premium_per_share: float, limits: dict) -> LimitCheck:
    """Money-proportional backstop applied before selling any new contract,
    independent of the strategy's own sizing. Returns allowed=False with a
    reason on the first cap breached."""
    if qty is None or qty < 1:
        return LimitCheck(False, f"qty {qty} is below 1")

    max_qty = limits["max_contracts_per_symbol"]
    if qty > max_qty:
        return LimitCheck(False, f"qty {qty} exceeds max_contracts_per_symbol {max_qty}")

    notional = strike * 100 * qty
    max_notional = limits["max_notional_per_symbol"]
    if notional > max_notional:
        return LimitCheck(
            False,
            f"notional ${notional:,.0f} exceeds max_notional_per_symbol ${max_notional:,.0f}"
        )

    min_prem = limits["min_premium_per_contract"]
    if premium_per_share < min_prem:
        return LimitCheck(
            False,
            f"premium ${premium_per_share:.2f}/sh below min_premium_per_contract ${min_prem:.2f}"
        )

    return LimitCheck(True, "ok")


def daily_loss_halt(equity: float, last_equity: float, limits: dict) -> LimitCheck:
    """Kill-switch for NEW entries (does NOT liquidate). allowed=False when the
    day's equity change is worse than -max_daily_loss_pct. Fails OPEN if
    last_equity is unusable, so a bad data point can't wedge the strategy."""
    try:
        last = float(last_equity)
        eq   = float(equity)
    except (TypeError, ValueError):
        return LimitCheck(True, "ok (equity unreadable — failing open)")
    if last <= 0:
        return LimitCheck(True, "ok (no prior equity — failing open)")

    day_pl_pct = (eq - last) / last
    threshold  = -abs(limits["max_daily_loss_pct"])
    if day_pl_pct <= threshold:
        return LimitCheck(
            False,
            f"daily P&L {day_pl_pct*100:+.1f}% breached kill-switch "
            f"({threshold*100:.0f}%) — no new entries this run"
        )
    return LimitCheck(True, "ok")
