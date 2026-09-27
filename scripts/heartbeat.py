#!/usr/bin/env python3
"""
Heartbeat -- is the live Consistent-Losers system actually working?
=====================================================================
Every failure so far has been silent: 70 buys logged as "Bought 0/N", a
three-day stop-manager crash, and (found 2026-09-16) two weeks of ~450
"URGENT ... leaving unprotected" errors a day, with every run still ending
in "Run complete." while stray sells opened SHORT positions in KBR and MGM.
Each script's own log had the evidence; nothing was reading it.

This script reads it, and checks the broker directly, every 15 minutes:

  1. Task Scheduler -- each live task is enabled and its last exit code is 0.
  2. Runs -- each job has a completed run as recent as its schedule
     requires, and its latest run didn't start and then never finish
     (a crash under pythonw.exe leaves no traceback anywhere).
  3. Errors -- any ERROR/CRITICAL log line written since the last check.
  4. Broker -- no short positions; every whole share is covered by a
     resting sell order; no symbol has more shares queued to sell than it
     holds (that sell would open a short); the stop manager's tracked lots
     match actual holdings.

Alerts by email only when the SET of problems changes, plus a reminder
every REMIND_HOURS while problems persist -- so a broken system nags
without sending 30 identical emails a day. After the close it also sends
one daily status email even when everything is healthy: if that email
ever stops arriving, the heartbeat itself is dead.

Runs via Task Scheduler \\Alpaca\\Heartbeat, every 15 min 9:40 AM - 5:10 PM
ET Mon-Fri (offset 5 min from the stop manager so it reads finished runs).
"""

import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from math import floor
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent / "signals"))
from signal_utils import get_logger, load_state, save_state, send_signal_email, base_html, STATES_DIR

BASE_DIR  = Path(__file__).parent.parent
LOGS_DIR  = BASE_DIR / "logs"
DATA_URL  = "https://data.alpaca.markets"
STATE_FILE = "heartbeat.json"

REMIND_HOURS       = 3
DIGEST_AFTER       = (16, 45)     # send the daily status email on the first run after this time
STALE_RUN_MINUTES  = 10           # a run that started this long ago with no "Run complete." crashed/hung
LOG_TAIL_BYTES     = 3_000_000
ERROR_SAMPLES      = 3

# Live jobs. `header` is the banner each script logs at the start of a run.
# `fresh` returns the latest time by which a completed run must exist, given
# now and the session's open/close, or None if nothing is due yet.
JOBS = {
    "DynamicStopManager": {
        "task": "\\Alpaca\\DynamicStopManager",
        "log": "dynamic_stop_manager.log",
        "header": "DYNAMIC STOP MANAGER  |",
        # every 15 min 9:35-16:05: once 9:55 has passed, a run must be <25 min old
        "fresh": lambda now, s: (min(now, s["close"] + timedelta(minutes=5)) - timedelta(minutes=25)
                                 if now >= s["open"] + timedelta(minutes=25) else None),
    },
    "BuyConsistentLosers": {
        "task": "\\Alpaca\\BuyConsistentLosers",
        "log": "buy_consistent_losers.log",
        "header": "BUY CONSISTENT-HISTORY LOSERS  |",
        "fresh": lambda now, s: (s["open"] if now >= s["open"] + timedelta(minutes=15) else None),
    },
    "ConsistentLosers": {
        "task": "\\Alpaca\\Signals\\ConsistentLosers",
        "log": "consistent_losers.log",
        "header": "CONSISTENT-HISTORY LOSERS MONITOR  |",
        "fresh": lambda now, s: (s["close"] if now >= s["close"] + timedelta(minutes=25) else None),
    },
}

TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \| (\w+)\s*\| (.*)$")

log = get_logger("heartbeat", "heartbeat.log")


def load_creds() -> tuple:
    with open(BASE_DIR / "config" / "alpaca_credentials.json") as f:
        creds = json.load(f)
    return creds["endpoint"], {"APCA-API-KEY-ID": creds["api_key"],
                               "APCA-API-SECRET-KEY": creds["api_secret"]}


# ── Log parsing (pure) ───────────────────────────────────────────────────────

def parse_runs(lines: list, header: str) -> list:
    """Split log lines into runs: [{start, end, complete, errors: [(ts, msg)]}].
    Lines without a timestamp (blank lines from "Run complete.\\n") are ignored."""
    runs = []
    for line in lines:
        m = TS_RE.match(line)
        if not m:
            continue
        ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        level, msg = m.group(2), m.group(3)
        if header in msg:
            runs.append({"start": ts, "end": None, "complete": False, "errors": []})
            continue
        if not runs:
            continue
        run = runs[-1]
        if msg.strip() == "Run complete.":
            run["complete"], run["end"] = True, ts
        elif level in ("ERROR", "CRITICAL"):
            run["errors"].append((ts, msg.strip()))
    return runs


def check_runs(job: str, runs: list, now: datetime, session, since: datetime, fresh,
               disabled: bool = False) -> list:
    """Problems as (key, message). `key` is stable across runs so the same
    ongoing problem doesn't count as a new one just because a number changed.

    `disabled`: the job's task is switched off (already reported as `{job}:state`),
    so skip the freshness and unfinished-run checks -- a paused job is expected
    to have no recent runs, and its last run may predate the "Run complete."
    marker. New ERROR lines are still reported."""
    problems = []

    if runs and not disabled:
        latest = runs[-1]
        if not latest["complete"] and now - latest["start"] > timedelta(minutes=STALE_RUN_MINUTES):
            problems.append((f"{job}:unfinished",
                             f"{job}: run started {latest['start']:%m-%d %H:%M} never logged "
                             f"'Run complete.' -- it crashed or hung"))

    if session is not None and not disabled:
        due = fresh(now, session)
        if due is not None:
            done = [r for r in runs if r["complete"] and r["start"] >= due - timedelta(minutes=1)]
            if not done:
                last_ok = next((r["start"] for r in reversed(runs) if r["complete"]), None)
                problems.append((f"{job}:stale",
                                 f"{job}: no completed run since {due:%m-%d %H:%M} "
                                 f"(last completed: {last_ok:%m-%d %H:%M})" if last_ok else
                                 f"{job}: no completed run found in its log"))

    errors = [e for r in runs for e in r["errors"] if e[0] > since]
    if errors:
        samples = "; ".join(msg[:160] for _, msg in errors[:ERROR_SAMPLES])
        problems.append((f"{job}:errors",
                         f"{job}: {len(errors)} ERROR line(s) since {since:%m-%d %H:%M} -- e.g. {samples}"))
    return problems


# ── Broker checks (pure) ─────────────────────────────────────────────────────

def check_broker(positions: list, open_orders: list, lots: list) -> list:
    problems = []
    held = {p["symbol"]: float(p["qty"]) for p in positions if p.get("asset_class", "us_equity") == "us_equity"}

    selling, covering = {}, {}
    for o in open_orders:
        if o.get("side") != "sell":
            continue
        remaining = float(o["qty"]) - float(o.get("filled_qty") or 0)
        selling[o["symbol"]] = selling.get(o["symbol"], 0) + remaining
        covering[o["symbol"]] = covering.get(o["symbol"], 0) + remaining

    tracked = {}
    for lot in lots:
        tracked[lot["symbol"]] = tracked.get(lot["symbol"], 0) + int(lot["qty"])

    for sym in sorted(set(held) | set(selling) | set(tracked)):
        qty = held.get(sym, 0.0)
        whole = floor(qty) if qty > 0 else 0
        if qty < 0:
            problems.append((f"short:{sym}",
                             f"SHORT position {sym}: {qty:g} shares -- this strategy never shorts; "
                             f"a sell fired on shares that weren't held. Unlimited-loss exposure."))
        if selling.get(sym, 0) > max(qty, 0) + 1e-9:
            problems.append((f"oversell:{sym}",
                             f"{sym}: {selling[sym]:g} shares in open sell orders but only {max(qty, 0):g} held "
                             f"-- if these fill, they open a short"))
        elif whole >= 1 and covering.get(sym, 0) < whole:
            problems.append((f"uncovered:{sym}",
                             f"UNPROTECTED {sym}: {whole} whole shares held, only "
                             f"{covering.get(sym, 0):g} covered by resting sell orders"))
        if tracked.get(sym, 0) != whole:
            problems.append((f"drift:{sym}",
                             f"State drift {sym}: stop manager tracks {tracked.get(sym, 0)} shares, "
                             f"broker holds {whole} whole shares"))
    return problems


# ── Live inputs ──────────────────────────────────────────────────────────────

def read_log_tail(name: str) -> list:
    path = LOGS_DIR / name
    if not path.exists():
        return []
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - LOG_TAIL_BYTES))
        data = f.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if size > LOG_TAIL_BYTES else lines   # first line may be cut mid-way


def get_session(base_url: str, headers: dict, now: datetime):
    """Today's session open/close as naive local datetimes, or None if the
    market is closed today. Assumes this machine runs on US Eastern time,
    which every Task Scheduler time in this repo already assumes."""
    day = now.date().isoformat()
    r = requests.get(f"{base_url}/calendar", headers=headers, params={"start": day, "end": day}, timeout=15)
    r.raise_for_status()
    cal = r.json()
    if not cal or cal[0]["date"] != day:
        return None
    return {"open":  datetime.strptime(f"{day} {cal[0]['open']}", "%Y-%m-%d %H:%M"),
            "close": datetime.strptime(f"{day} {cal[0]['close']}", "%Y-%m-%d %H:%M")}


def check_tasks() -> list:
    ps = ("Get-ScheduledTask -TaskPath '\\Alpaca\\*' | ForEach-Object { "
          "$i = Get-ScheduledTaskInfo -TaskPath $_.TaskPath -TaskName $_.TaskName -ErrorAction SilentlyContinue; "
          "[PSCustomObject]@{ Full = \"$($_.TaskPath)$($_.TaskName)\"; State = [string]$_.State; "
          "Result = $i.LastTaskResult } } | ConvertTo-Json")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=60).stdout
        tasks = json.loads(out)
        tasks = {t["Full"]: t for t in ([tasks] if isinstance(tasks, dict) else tasks)}
    except Exception as e:
        return [("tasks:query", f"Could not query Task Scheduler: {e}")]

    problems = []
    for job, spec in JOBS.items():
        t = tasks.get(spec["task"])
        if t is None:
            problems.append((f"{job}:missing", f"Task {spec['task']} does not exist"))
            continue
        if t["State"] not in ("Ready", "Running"):
            problems.append((f"{job}:state", f"Task {spec['task']} is {t['State']} -- it will not run"))
        # 267009 = currently running, 267011 = has not run yet
        if (t.get("Result") or 0) not in (0, 267009, 267011):
            problems.append((f"{job}:exitcode", f"Task {spec['task']} last exit code {t['Result']} (nonzero = crashed)"))
    return problems


# ── Main ─────────────────────────────────────────────────────────────────────

def run():
    now = datetime.now()
    log.info("=" * 65)
    log.info(f"HEARTBEAT  |  {now:%Y-%m-%d %H:%M}")
    log.info("=" * 65)

    state = load_state(STATE_FILE)
    since = datetime.fromisoformat(state["last_checked"]) if state.get("last_checked") else now - timedelta(days=1)

    problems = check_tasks()
    base_url, headers = load_creds()
    try:
        session = get_session(base_url, headers, now)
    except Exception as e:
        session = None
        problems.append(("calendar", f"Could not fetch market calendar: {e}"))

    disabled = {k.split(":")[0] for k, _ in problems if k.endswith(":state")}
    for job, spec in JOBS.items():
        runs = parse_runs(read_log_tail(spec["log"]), spec["header"])
        problems += check_runs(job, runs, now, session, since, spec["fresh"], disabled=job in disabled)

    try:
        positions = requests.get(f"{base_url}/positions", headers=headers, timeout=15)
        orders = requests.get(f"{base_url}/orders", headers=headers, params={"status": "open", "limit": 500}, timeout=15)
        positions.raise_for_status()
        orders.raise_for_status()
        lots = (json.loads((STATES_DIR / "dynamic_stops.json").read_text()).get("lots", [])
                if (STATES_DIR / "dynamic_stops.json").exists() else [])
        problems += check_broker(positions.json(), orders.json(), lots)
    except Exception as e:
        problems.append(("broker", f"Could not check positions/orders at Alpaca: {e}"))

    for _, msg in problems:
        log.warning(f"  - {msg}")
    if not problems:
        log.info("  All checks passed.")

    keys = sorted(k for k, _ in problems)
    last_alert = datetime.fromisoformat(state["last_alert_at"]) if state.get("last_alert_at") else None
    changed = keys != state.get("last_alert_keys", [])
    remind = bool(problems) and (last_alert is None or now - last_alert >= timedelta(hours=REMIND_HOURS))

    if problems and (changed or remind):
        send_alert(problems, now, new_keys=set(keys) - set(state.get("last_alert_keys", [])))
        state["last_alert_at"] = now.isoformat(timespec="seconds")
        state["last_alert_keys"] = keys
    elif not problems and state.get("last_alert_keys"):
        send_signal_email("✅ Heartbeat — all clear", "All previously reported problems are resolved.",
                          base_html("Heartbeat", "✅", "<p>All previously reported problems are resolved.</p>"))
        state["last_alert_keys"] = []
        log.info("  Recovery email sent.")

    if (session is not None and (now.hour, now.minute) >= DIGEST_AFTER
            and state.get("last_digest") != now.date().isoformat()):
        send_digest(problems, now)
        state["last_digest"] = now.date().isoformat()

    state["last_checked"] = now.isoformat(timespec="seconds")
    save_state(STATE_FILE, state)
    log.info("Run complete.\n")


def send_alert(problems: list, now: datetime, new_keys: set):
    items = "".join(f"<li>{'<strong>NEW:</strong> ' if k in new_keys else ''}{m}</li>" for k, m in problems)
    content = (f"<p><strong>{len(problems)}</strong> problem(s) in the live trading system:</p><ul>{items}</ul>"
               f"<p style='color:#888;font-size:12px'>You'll get this again only if the set of problems "
               f"changes, or every {REMIND_HOURS}h while they persist.</p>")
    send_signal_email(f"🚨 Heartbeat — {len(problems)} problem(s)", "\n".join(m for _, m in problems),
                      base_html("Heartbeat Alert", "🚨", content))
    log.warning(f"  Alert email sent ({len(problems)} problems, {len(new_keys)} new).")


def send_digest(problems: list, now: datetime):
    status = (f"<p style='color:#ff6b6b'><strong>{len(problems)} open problem(s)</strong> -- see alert emails.</p>"
              if problems else "<p style='color:#4caf50'><strong>All checks passed.</strong></p>")
    content = (status + "<p>Daily heartbeat: if this email stops arriving on a trading day, the heartbeat "
               "monitor itself isn't running.</p>")
    send_signal_email(f"💓 Daily heartbeat — {'OK' if not problems else f'{len(problems)} problem(s)'} — {now:%Y-%m-%d}",
                      "OK" if not problems else "\n".join(m for _, m in problems),
                      base_html("Daily Heartbeat", "💓", content))
    log.info("  Daily digest sent.")


if __name__ == "__main__":
    run()
