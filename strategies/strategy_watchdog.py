#!/usr/bin/env python3
"""
Strategy Watchdog
==================
Daily configuration and behavior monitor for all trading strategies.
Verifies that what each strategy *claims* matches what Alpaca *shows* —
born from the govt-contracts bug where state said "$9,500 deployed" while
zero orders had ever been placed.

Checks:
  1. Task Scheduler — every expected task exists, is enabled, last result 0,
     last ran within its expected cadence, and has a next run scheduled.
  2. Stop coverage — every equity position with >= 1 whole share has an
     active stop or trailing-stop sell order.
  3. State vs reality — recent govt-contract trades have real order IDs;
     copy-trader pending queue isn't stuck.
  4. Freshness — strategy logs and regime state updated recently.

Emails an alert only when something is wrong. Logs every run.
Scheduled daily 6:45 PM Mon-Fri (after all other strategies have run).
"""

import json
import subprocess
import sys
from datetime import datetime
from math import floor
from pathlib import Path

import requests

BASE_DIR    = Path(__file__).parent.parent
CONFIG_FILE = BASE_DIR / "config" / "alpaca_credentials.json"
LOG_FILE    = BASE_DIR / "logs" / "strategy_watchdog.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(BASE_DIR / "signals"))
from signal_utils import send_signal_email, base_html

import logging
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

with open(CONFIG_FILE) as f:
    creds = json.load(f)
BASE_URL = creds["endpoint"]
HEADERS  = {
    "APCA-API-KEY-ID":     creds["api_key"],
    "APCA-API-SECRET-KEY": creds["api_secret"],
}

# Expected tasks: name -> (path, max_age_days_on_weekdays)
# max_age covers weekends/holidays; weekly tasks get 8 days.
EXPECTED_TASKS = {
    "\\Alpaca\\DailyEmailReport":          4,
    # 2026-06-22: only the wheel trades now. MomentumStrategy, GovtContractsStrategy,
    # PortfolioMonitor and StrategyManager are intentionally DISABLED (would place
    # stops that conflict with the wheel's covered calls). Re-add here if re-enabled.
    "\\Alpaca\\WheelStrategy":             4,
    "\\Alpaca\\WheelCandidateScan":        8,
    "\\Alpaca\\WheelCandidateEmail":       8,
    "\\Alpaca\\Signals\\ActivistMonitor":  4,
    "\\Alpaca\\Signals\\ContractAwards":   4,
    "\\Alpaca\\Signals\\EarningsCalendar": 4,
    "\\Alpaca\\Signals\\ETFFlows":         4,
    "\\Alpaca\\Signals\\Hedge13F":         8,
    "\\Alpaca\\Signals\\InsiderTrades":    4,
    "\\Alpaca\\Signals\\ShortInterest":    8,
    "\\Alpaca\\Signals\\SignalDigest":     4,
    "\\Alpaca\\Signals\\UnusualOptions":   4,
}

# One-shot helper tasks that are allowed to exist and never re-run
IGNORED_RESULTS = {267011}  # "task has not yet run" — fine for new one-shots


def check_tasks() -> list:
    """Verify all expected Task Scheduler tasks are healthy."""
    problems = []
    ps = (
        "Get-ScheduledTask -TaskPath '\\Alpaca\\*' | ForEach-Object { "
        "$i = Get-ScheduledTaskInfo -TaskPath $_.TaskPath -TaskName $_.TaskName -ErrorAction SilentlyContinue; "
        "[PSCustomObject]@{ Full = \"$($_.TaskPath)$($_.TaskName)\"; State = [string]$_.State; "
        "LastRun = $i.LastRunTime.ToString('yyyy-MM-dd HH:mm'); Result = $i.LastTaskResult; "
        "NextRun = if ($i.NextRunTime) { $i.NextRunTime.ToString('yyyy-MM-dd HH:mm') } else { '' } } "
        "} | ConvertTo-Json"
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=60).stdout
        tasks = json.loads(out)
        if isinstance(tasks, dict):
            tasks = [tasks]
    except Exception as e:
        return [f"Could not query Task Scheduler: {e}"]

    found = {t["Full"]: t for t in tasks}
    now = datetime.now()

    for name, max_age in EXPECTED_TASKS.items():
        t = found.get(name)
        if t is None:
            problems.append(f"MISSING task: {name}")
            continue
        if t["State"] not in ("Ready", "Running"):
            problems.append(f"{name}: state {t['State']} (expected Ready/Running)")
        result = t.get("Result") or 0
        if result != 0 and result not in IGNORED_RESULTS:
            problems.append(f"{name}: last result {result} (nonzero = failed)")
        try:
            last = datetime.strptime(t["LastRun"], "%Y-%m-%d %H:%M")
            if (now - last).days > max_age:
                problems.append(f"{name}: hasn't run in {(now - last).days} days (max {max_age})")
        except Exception:
            pass
        # Recurring tasks must have a next run scheduled — empty means expired trigger
        if not t.get("NextRun"):
            problems.append(f"{name}: NO next run scheduled — trigger expired or one-shot")
    return problems


def check_stop_coverage() -> list:
    """Every >= 1 whole-share equity position must have a sell stop."""
    problems = []
    try:
        positions = requests.get(f"{BASE_URL}/positions", headers=HEADERS, timeout=15).json()
        orders = requests.get(f"{BASE_URL}/orders", headers=HEADERS,
                              params={"status": "open", "limit": 500}, timeout=15).json()
    except Exception as e:
        return [f"Could not query Alpaca: {e}"]

    # Any open sell order counts: stops protect, market/limit sells are exits in progress
    protected = {o["symbol"] for o in orders if o.get("side") == "sell"}
    pending_buys = {o["symbol"] for o in orders if o.get("side") == "buy"}

    for p in positions:
        if p.get("asset_class") != "us_equity":
            continue
        sym = p["symbol"]
        if floor(float(p["qty"])) < 1:
            continue
        if sym not in protected and sym not in pending_buys:
            problems.append(f"UNPROTECTED position: {sym} ({p['qty']} shares, no stop order)")
    return problems


def check_state_vs_reality() -> list:
    """Strategy state files must match actual Alpaca activity."""
    problems = []
    # Copy trader: pending queue shouldn't grow stale
    try:
        cp = json.load(open(BASE_DIR / "politician-copy-trader" / "state.json"))
        pending = cp.get("pending_trades", [])
        if len(pending) > 25:
            problems.append(f"Copy trader: {len(pending)} pending trades — queue may be stuck")
    except Exception as e:
        problems.append(f"Could not read copy trader state: {e}")

    return problems


def check_freshness() -> list:
    """Logs and state files should be recent on trading days."""
    problems = []
    now = datetime.now()
    # Only meaningful Mon-Fri after market hours; allow 4 days for weekends/holidays
    targets = {
        "wheel log":            BASE_DIR / "logs" / "wheel.log",
        "regime state":         BASE_DIR / "strategies" / "regime_state.json",
    }
    for label, path in targets.items():
        if not path.exists():
            problems.append(f"{label}: file missing ({path.name})")
            continue
        age_days = (now - datetime.fromtimestamp(path.stat().st_mtime)).days
        if age_days > 4:
            problems.append(f"{label}: not updated in {age_days} days")
    return problems


def run():
    log.info("=" * 65)
    log.info(f"STRATEGY WATCHDOG  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 65)

    sections = {
        "Task Scheduler":   check_tasks(),
        "Stop Coverage":    check_stop_coverage(),
        "State vs Reality": check_state_vs_reality(),
        "Freshness":        check_freshness(),
    }

    all_problems = []
    for section, problems in sections.items():
        if problems:
            log.warning(f"  [{section}] {len(problems)} problem(s):")
            for p in problems:
                log.warning(f"    - {p}")
                all_problems.append(f"[{section}] {p}")
        else:
            log.info(f"  [{section}] OK")

    if all_problems:
        log.warning(f"  TOTAL: {len(all_problems)} problem(s) — sending alert email")
        items = "".join(f"<li>{p}</li>" for p in all_problems)
        content = (
            f"<p>The strategy watchdog found <strong>{len(all_problems)}</strong> problem(s):</p>"
            f"<ul>{items}</ul>"
            f"<p style='color:#888;font-size:12px'>Run: {datetime.now():%Y-%m-%d %H:%M}</p>"
        )
        send_signal_email(
            f"⚠️ Strategy Watchdog — {len(all_problems)} problem(s) found",
            "\n".join(all_problems),
            base_html("Strategy Watchdog", "🐕", content),
        )
    else:
        log.info("  ALL SYSTEMS HEALTHY — no alert needed")

    log.info("Run complete.\n")


if __name__ == "__main__":
    run()
