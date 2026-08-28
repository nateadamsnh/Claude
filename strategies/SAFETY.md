# Wheel Safety Layer

Guardrails that sit between the wheel strategy and the broker. The goal: the
strategy can never act on state that disagrees with reality, can never exceed
money-proportional limits, and can be run in a no-trade "alert-only" mode. These
are the prerequisites for ever trusting the wheel with a live cash account.

All the decision logic lives in **`wheel_safety.py`** as pure functions (no
network, no I/O), so it is fully unit-tested. `options_wheel.py` calls them with
live data.

## 1. Reconciliation (`reconcile_symbol`)
Before acting on any symbol, local state is compared to the broker's actual
positions:
- **Violation → HALT the symbol** (skip, alert, require manual fix): state
  tracks a filled short option the broker shows nowhere (and it hasn't expired),
  or a covered-call share count that disagrees with the broker.
- **Untracked → alert only**: the broker holds a short option the state doesn't
  manage (e.g. a stray duplicate). Listing it under `untracked_open_positions`
  in the state file acknowledges it and silences the alert.

This directly targets the root cause of every bug found to date: acting on
"fire-and-forget" state that drifted from reality.

## 2. Hard limits (`check_entry_limits`, `daily_loss_halt`)
Enforced in code before every new sale, independent of strategy sizing. Defaults
live in `wheel_safety.DEFAULT_LIMITS`; override per-machine via
`config/wheel_limits.json`:

| Limit | Default | Meaning |
|-------|---------|---------|
| `max_contracts_per_symbol` | 5 | never sell more than this many at once |
| `max_notional_per_symbol` | $15,000 | strike × 100 × qty collateral cap |
| `min_premium_per_contract` | $0.05 | don't sell near-worthless premium |
| `max_daily_loss_pct` | 0.10 | kill-switch: block NEW entries when the account is down >10% on the day |

The daily-loss kill-switch **blocks new entries only** — it never liquidates, so
it can't dump short premium at a loss. Existing positions are still managed/closed.

## 3. Dry-run / alert-only mode
Set the environment variable `WHEEL_DRY_RUN=1` (or `true`/`yes`/`on`). Order
placement then logs `WOULD ...` and places nothing; state is left untouched, so
the run just reports its intentions. Default off preserves live behavior.

```sh
# see what the wheel would do, without trading:
WHEEL_DRY_RUN=1 python options_wheel.py
```

## Running the tests
```sh
cd strategies
python -m unittest discover -s tests -p "test_*.py"
```
- `tests/test_wheel_safety.py` — pure logic: parsing, limits, kill-switch, reconciliation.
- `tests/test_wheel_integration.py` — real `run_csp`/`run_cc` with the network
  mocked: false-expiry guard, buy-to-close fill confirmation, dry-run, limits,
  kill-switch. These lock in the bug fixes from PRs #4 and #6.

## What this does NOT yet cover (before going live)
- A sustained clean paper track record (weeks with zero reconcile violations).
- Partial-fill handling on multi-contract orders.
- Alerting out of band (email/push) when the kill-switch or a reconcile halt fires.
See `memory/live-money-readiness.md` for the full bar.
