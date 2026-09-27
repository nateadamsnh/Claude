#!/usr/bin/env python3
"""
Backtest: Consistent-History Losers
====================================
Answers one question: does the live strategy (signals/consistent_losers.py
picks + scripts/dynamic_stop_manager.py staged stops) beat doing something
dumber with the same money and the same exits?

The signal thresholds, fund-name filter and earnings-headline regex are
IMPORTED from the live signal module, and the stop logic is imported from
the live stop manager (compute_stage), so the backtest can't quietly drift
from what actually trades. Only the data plumbing is re-implemented here.

Simulation (daily bars, one decision per session):
  - Signal at session t's close -> buy at session t+1's open. Whole shares,
    max(1, $500 // open) -- same sizing rule as buy_consistent_losers.py.
  - Staged stop evaluated once per day. The stop in force at the start of a
    day is checked against that day's low BEFORE the day's high is allowed
    to ratchet it. A gap below the stop fills at the open. If the ratcheted
    target ends the day at/above the close, the lot is closed at the close
    (the live manager's "target already passed" market sell).
  - Slippage applied on both sides (--slippage-bps).

Baselines, all using the identical exits and trade count per day:
  1. RANDOM QUIET: random names from the same price+volume-stable pool,
     ignoring the "biggest loser today" ranking. Tests whether the ranking
     adds anything.
  2. RANDOM ANY: random names from every non-fund symbol with a bar that
     day. Tests whether the whole screen adds anything.
  3. SPY over each trade's exact holding window (per-trade excess return).
  4. Same picks, NO stops, fixed 5- and 20-day holds. Tests whether the
     staged stop helps or hurts the signal.

Honest limits:
  - Daily bars can't reproduce the 15-minute intraday ratchet exactly.
  - IEX feed (matches live) has gappy bars for thin names; a missing bar
    anywhere in the 22-day window disqualifies a symbol, same as live.
  - Prices are split-adjusted. The live signal uses raw prices, so a split
    day could look like a crash to the live screen but not here.
  - Delisted symbols are included, but Alpaca's inactive list is not a
    complete survivorship-free database.
  - Earnings filter uses only news published by the signal day's close, so
    it has no look-ahead -- which also means it is WEAKER than the live
    +5-day window looks on paper (live can't see the future either).

Usage:
  python scripts/backtest_consistent_losers.py --fetch-only     # download + cache data
  python scripts/backtest_consistent_losers.py                  # full run
  python scripts/backtest_consistent_losers.py --no-earnings    # skip news calls
"""

import argparse
import json
import math
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR / "signals"))
sys.path.insert(0, str(Path(__file__).parent))

import consistent_losers as live_signal          # thresholds, fund regex, earnings regex, creds
from dynamic_stop_manager import compute_stage   # the exact live staged-stop logic

HEADERS  = live_signal.HEADERS
BASE_URL = live_signal.BASE_URL
DATA_URL = live_signal.DATA_URL

CACHE_DIR   = BASE_DIR / "backtests" / "cache"
RESULTS_DIR = BASE_DIR / "backtests" / "results"

NOTIONAL_PER_BUY = 500
BATCH_SIZE       = 150
WARMUP_CAL_DAYS  = 45      # history needed before --start for the 20-day window
RANDOM_SEEDS     = 20
ANY_POOL_MIN_PRICE = 5.0


# ── Data ─────────────────────────────────────────────────────────────────────

def http_get(url: str, params: dict, timeout: int = 60) -> dict:
    for attempt in range(6):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=timeout)
        except requests.RequestException:
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
    raise RuntimeError(f"GET {url} failed after retries")


SYMBOL_RE = re.compile(r"^[A-Z][A-Z.]{0,6}$")   # inactive list includes CUSIP-style junk the bars API rejects


def get_universe() -> dict:
    """{symbol: name}: live-tradable non-OTC names plus delisted non-OTC names."""
    out = {}
    for status in ("active", "inactive"):
        assets = http_get(f"{BASE_URL}/assets", {"status": status, "asset_class": "us_equity"})
        for a in assets:
            if a["exchange"] == "OTC" or not SYMBOL_RE.match(a["symbol"]):
                continue
            if status == "active" and not a.get("tradable"):
                continue
            out.setdefault(a["symbol"], a.get("name") or "")
    return out


def get_calendar(start: str, end: str) -> list:
    return [d["date"] for d in http_get(f"{BASE_URL}/calendar", {"start": start, "end": end})]


def _fetch_batch(batch: list, start: str, end: str, feed: str) -> list:
    """On a 400 (a symbol the bars API won't accept), bisect so one bad
    symbol costs itself, not its 149 neighbors."""
    rows = []
    page_token = None
    try:
        while True:
            params = {"symbols": ",".join(batch), "timeframe": "1Day", "start": start, "end": end,
                      "limit": 10000, "feed": feed, "adjustment": "split"}
            if page_token:
                params["page_token"] = page_token
            d = http_get(f"{DATA_URL}/v2/stocks/bars", params)
            for sym, bars in (d.get("bars") or {}).items():
                for x in bars:
                    rows.append((sym, x["t"][:10], x["o"], x["h"], x["l"], x["c"], x["v"]))
            page_token = d.get("next_page_token")
            if not page_token:
                return rows
    except requests.HTTPError as e:
        if e.response is None or e.response.status_code != 400:
            raise
        if len(batch) == 1:
            print(f"  skipping symbol rejected by bars API: {batch[0]}", flush=True)
            return []
        mid = len(batch) // 2
        return (_fetch_batch(batch[:mid], start, end, feed)
                + _fetch_batch(batch[mid:], start, end, feed))


def fetch_bars(symbols: list, start: str, end: str, feed: str, cache: Path) -> pd.DataFrame:
    """Daily bars in long form. One cache file per batch so an interrupted
    download resumes where it stopped."""
    cache.mkdir(parents=True, exist_ok=True)
    frames = []
    n_batches = math.ceil(len(symbols) / BATCH_SIZE)
    for b in range(n_batches):
        path = cache / f"batch_{b:04d}.pkl"
        if path.exists():
            frames.append(pd.read_pickle(path))
            continue
        rows = _fetch_batch(symbols[b * BATCH_SIZE:(b + 1) * BATCH_SIZE], start, end, feed)
        df = pd.DataFrame(rows, columns=["sym", "date", "o", "h", "l", "c", "v"])
        df.to_pickle(path)
        frames.append(df)
        print(f"  bars batch {b + 1}/{n_batches}: {len(df):,} rows", flush=True)
    return pd.concat(frames, ignore_index=True)


def load_panels(start: str, end: str, feed: str) -> tuple:
    """Wide (dates x symbols) OHLCV panels aligned to the trading calendar."""
    fetch_start = (date.fromisoformat(start) - timedelta(days=WARMUP_CAL_DAYS)).isoformat()
    key = f"{feed}_{fetch_start}_{end}"
    panel_path = CACHE_DIR / f"panels_{key}.pkl"
    if panel_path.exists():
        return pd.read_pickle(panel_path)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    universe_path = CACHE_DIR / f"universe_{key}.json"
    if universe_path.exists():
        universe = json.loads(universe_path.read_text())
    else:
        universe = get_universe()
        universe_path.write_text(json.dumps(universe))
    print(f"  Universe: {len(universe):,} non-OTC symbols (active + delisted)")

    calendar = get_calendar(fetch_start, end)
    long = fetch_bars(sorted(universe), fetch_start, end, feed, CACHE_DIR / f"bars_{key}")
    long = long.drop_duplicates(["date", "sym"])
    panels = {f: long.pivot(index="date", columns="sym", values=f).reindex(calendar)
              for f in ("o", "h", "l", "c", "v")}
    result = (panels, universe, calendar)
    pd.to_pickle(result, panel_path)
    return result


# ── Signal ───────────────────────────────────────────────────────────────────

def compute_masks(panels: dict, universe: dict) -> tuple:
    """Vectorized replica of consistent_losers.check_stability(). For session
    t the window is t-21..t-1 (21 bars, 20 day-over-day changes), every bar
    present with positive volume, no close move > MAX_DAILY_PRICE_MOVE and --
    for the losers list -- no volume move > MAX_DAILY_VOLUME_MOVE."""
    C, V = panels["c"], panels["v"]
    lb = live_signal.LOOKBACK_DAYS
    ret  = C / C.shift(1) - 1
    vchg = V / V.shift(1) - 1

    price_ok = ret.abs().rolling(lb, min_periods=lb).max().shift(1) <= live_signal.MAX_DAILY_PRICE_MOVE / 100
    vol_pos  = (V > 0).astype(float).where(V.notna()).rolling(lb + 1, min_periods=lb + 1).min().shift(1) == 1
    vol_ok   = vchg.abs().rolling(lb, min_periods=lb).max().shift(1) <= live_signal.MAX_DAILY_VOLUME_MOVE / 100
    close_ok = (C > 0) & ret.notna()

    fund_cols = [s for s in C.columns if live_signal.is_fund(universe.get(s, ""))]

    quiet = price_ok & vol_pos & vol_ok & close_ok
    # The "any stock" pool still needs clean history and a price floor: without
    # them a handful of sub-$1 names with bad prints (+1,000% "returns") swamp
    # the average. 95% of the strategy's own entries are above $10.
    any_valid = close_ok & vol_pos & (C >= ANY_POOL_MIN_PRICE)
    quiet.loc[:, fund_cols] = False
    any_valid.loc[:, fund_cols] = False
    losers = quiet & (ret < 0)
    return ret, losers, quiet, any_valid


class EarningsChecker:
    """News-headline earnings filter, restricted to articles published by the
    signal day's close so the backtest can't peek at the future."""

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.path = CACHE_DIR / "earnings_news_cache.json"
        self.cache = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.calls = 0

    def has_earnings(self, symbol: str, day: str) -> bool:
        if not self.enabled:
            return False
        key = f"{symbol}|{day}"
        if key not in self.cache:
            d = date.fromisoformat(day)
            try:
                news = http_get(f"{DATA_URL}/v1beta1/news", {
                    "symbols": symbol,
                    "start": (d - timedelta(days=live_signal.EARNINGS_DAYS_BEFORE)).isoformat(),
                    "end":   f"{day}T20:00:00Z",
                    "limit": 50,
                }, timeout=15).get("news", [])
                self.cache[key] = any(live_signal.EARNINGS_HEADLINE_RE.search(n.get("headline", ""))
                                      for n in news)
            except Exception:
                self.cache[key] = False   # fail open, same as live
            self.calls += 1
            time.sleep(0.3)
            if self.calls % 100 == 0:
                self.save()
        return self.cache[key]

    def save(self):
        self.path.write_text(json.dumps(self.cache))


class NewsChecker:
    """How many news articles a symbol had around its signal day.

    Tests the news/no-news split: a large drop WITHOUT news is the
    overreaction this strategy is premised on, while a large drop WITH news
    is the market repricing something real, which tends to keep drifting.
    The live screen doesn't distinguish them, so if the two buckets have
    opposite signs they cancel -- a candidate explanation for why ranking by
    drop size added nothing over random quiet names.

    Same no-look-ahead rule as EarningsChecker: only articles published by
    the signal day's close count, so this sees exactly what a trader placing
    the next morning's order could have seen."""

    def __init__(self, enabled: bool, days_before: int = 1):
        self.enabled = enabled
        self.days_before = days_before
        self.path = CACHE_DIR / "news_count_cache.json"
        self.cache = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.calls = 0

    def count(self, symbol: str, day: str) -> int:
        if not self.enabled:
            return 0
        key = f"{symbol}|{day}|{self.days_before}"
        if key not in self.cache:
            d = date.fromisoformat(day)
            try:
                news = http_get(f"{DATA_URL}/v1beta1/news", {
                    "symbols": symbol,
                    "start": (d - timedelta(days=self.days_before)).isoformat(),
                    "end":   f"{day}T20:00:00Z",
                    "limit": 50,
                }, timeout=15).get("news", [])
                self.cache[key] = len(news)
            except Exception:
                self.cache[key] = -1        # unknown; excluded from both buckets
            self.calls += 1
            time.sleep(0.3)
            if self.calls % 100 == 0:
                self.save()
        return self.cache[key]

    def save(self):
        self.path.write_text(json.dumps(self.cache))


def split_by_news(trades: list, counts: dict) -> tuple:
    """(no_news, news, unknown) -- trades keyed by (signal_t, col)."""
    quiet_side, loud, unknown = [], [], []
    for t in trades:
        c = counts.get((t["signal_t"], t["col"]))
        (unknown if c is None or c < 0 else quiet_side if c == 0 else loud).append(t)
    return quiet_side, loud, unknown


def welch_t(a: list, b: list) -> float:
    """Two-sample t (unequal variances) on per-trade returns: is the
    difference between the buckets bigger than sampling noise?"""
    x, y = np.array([t["ret"] for t in a]), np.array([t["ret"] for t in b])
    if len(x) < 2 or len(y) < 2:
        return 0.0
    se = math.sqrt(x.var(ddof=1) / len(x) + y.var(ddof=1) / len(y))
    return float((x.mean() - y.mean()) / se) if se > 0 else 0.0


def pick_signals(ret, losers, dates_idx: list, earnings: EarningsChecker) -> dict:
    """{signal row index: [column indices]} -- top RESULT_COUNT losers by % drop."""
    R = ret.to_numpy()
    L = losers.to_numpy()
    cols = ret.columns
    picks = {}
    for n, t in enumerate(dates_idx):
        cand = np.flatnonzero(L[t])
        if len(cand) == 0:
            continue
        order = cand[np.argsort(R[t, cand], kind="stable")]
        chosen = []
        for j in order:
            if len(chosen) >= live_signal.RESULT_COUNT:
                break
            if earnings.has_earnings(cols[j], ret.index[t]):
                continue
            chosen.append(j)
        if chosen:
            picks[t] = chosen
        if (n + 1) % 50 == 0:
            print(f"  signals: {n + 1}/{len(dates_idx)} sessions", flush=True)
    earnings.save()
    return picks


# ── Trade simulation ─────────────────────────────────────────────────────────

def advance_stage(entry: float, highest: float, stage: str) -> tuple:
    """The live manager runs every 15 min, so a big up day can carry a lot
    through more than one stage. Iterate compute_stage to a fixed point."""
    for _ in range(3):
        new_stage, target = compute_stage(entry, highest, stage)
        if new_stage == stage:
            return stage, target
        stage = new_stage
    return stage, target


def simulate(O, H, L, C, j: int, entry_t: int, slip: float, hold_days: int = None):
    """One lot. Returns dict or None if there's no entry bar."""
    last = O.shape[0] - 1
    raw_open = O[entry_t, j]
    if not (raw_open > 0):
        return None
    qty = max(1, int(NOTIONAL_PER_BUY // raw_open))
    entry = raw_open * (1 + slip)

    def result(exit_t, exit_px, reason):
        return {"entry_t": entry_t, "exit_t": exit_t, "qty": qty, "entry": entry,
                "exit": exit_px, "reason": reason,
                "ret": exit_px / entry - 1, "pnl": (exit_px - entry) * qty}

    if hold_days is not None:
        t_exit = min(entry_t + hold_days - 1, last)
        for t in range(t_exit, entry_t - 1, -1):     # last available close on/before the exit day
            if C[t, j] > 0:
                return result(t, C[t, j] * (1 - slip), "hold" if t_exit < last else "open")
        return None

    stage, stop = advance_stage(entry, entry, "tight")
    highest = entry
    last_seen = entry_t
    for t in range(entry_t, last + 1):
        o, h, l, c = O[t, j], H[t, j], L[t, j], C[t, j]
        if not (c > 0):
            continue                          # no bar that day (IEX gap / delisted)
        last_seen = t
        if t > entry_t and o <= stop:
            return result(t, o * (1 - slip), "gap")
        if l <= stop:
            return result(t, stop * (1 - slip), "stop")
        highest = max(highest, h)
        stage, stop = advance_stage(entry, highest, stage)
        if stop >= c:
            return result(t, c * (1 - slip), "target_passed")
    return result(last_seen, C[last_seen, j] * (1 - slip), "open")


def run_trades(picks: dict, arrays: tuple, slip: float, hold_days: int = None) -> list:
    O, H, L, C = arrays
    out = []
    for t, cols in picks.items():
        if t + 1 >= O.shape[0]:
            continue
        for j in cols:
            r = simulate(O, H, L, C, j, t + 1, slip, hold_days)
            if r:
                r["signal_t"], r["col"] = t, j
                out.append(r)
    return out


# ── Metrics ──────────────────────────────────────────────────────────────────

def spy_window_return(trade: dict, spy: tuple) -> float:
    so, sc = spy
    a, b = so[trade["entry_t"]], sc[trade["exit_t"]]
    return b / a - 1 if a > 0 and b > 0 else np.nan


def summarize(trades: list, spy: tuple = None) -> dict:
    if not trades:
        return {"trades": 0}
    rets = np.array([t["ret"] for t in trades])
    pnls = np.array([t["pnl"] for t in trades])
    wins, losses = pnls[pnls > 0].sum(), -pnls[pnls < 0].sum()
    by_exit = sorted(trades, key=lambda t: t["exit_t"])
    cum = np.cumsum([t["pnl"] for t in by_exit])
    dd = (np.maximum.accumulate(np.concatenate([[0], cum]))[1:] - cum).max()
    s = {
        "trades":        len(trades),
        "win_rate":      float((rets > 0).mean()),
        "mean_ret":      float(rets.mean()),
        "median_ret":    float(np.median(rets)),
        "t_stat":        float(rets.mean() / (rets.std(ddof=1) / math.sqrt(len(rets)))) if len(rets) > 1 else 0.0,
        "total_pnl":     float(pnls.sum()),
        "profit_factor": float(wins / losses) if losses > 0 else float("inf"),
        "max_drawdown":  float(dd),
        "avg_hold_days": float(np.mean([t["exit_t"] - t["entry_t"] + 1 for t in trades])),
    }
    if spy is not None:
        ex = np.array([t["ret"] - spy_window_return(t, spy) for t in trades])
        ex = ex[~np.isnan(ex)]
        s["mean_excess_vs_spy"] = float(ex.mean())
        s["excess_t_stat"] = float(ex.mean() / (ex.std(ddof=1) / math.sqrt(len(ex)))) if len(ex) > 1 else 0.0
    return s


def random_baseline(picks: dict, pool_mask, arrays: tuple, slip: float, seeds: int) -> list:
    P = pool_mask.to_numpy()
    runs = []
    for seed in range(seeds):
        rng = np.random.default_rng(seed)
        rand_picks = {}
        for t, cols in picks.items():
            pool = np.flatnonzero(P[t])
            if len(pool) == 0:
                continue
            rand_picks[t] = list(rng.choice(pool, size=min(len(cols), len(pool)), replace=False))
        runs.append(summarize(run_trades(rand_picks, arrays, slip)))
    return runs


def live_parity(picks: dict, dates: list, cols) -> list:
    """Compare backtest picks to what the live buy script actually tried to
    buy, parsed from logs/buy_consistent_losers.log."""
    log_path = BASE_DIR / "logs" / "buy_consistent_losers.log"
    if not log_path.exists():
        return []
    live, current = {}, None
    sig_re = re.compile(r"Signal from (\d{4}-\d{2}-\d{2}): (\d+) symbol")
    sym_re = re.compile(r"\|\s{3}([A-Z][A-Z.]*): (?:submitting|price)")
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = sig_re.search(line)
        if m:
            current = m.group(1)
            live.setdefault(current, [])
            continue
        m = sym_re.search(line)
        if m and current and m.group(1) not in live[current]:
            live[current].append(m.group(1))
    idx = {d: i for i, d in enumerate(dates)}
    rows = []
    for d, syms in sorted(live.items()):
        if d not in idx or idx[d] + 1 >= len(dates):   # no next session in the data to trade on
            continue
        bt = [cols[j] for j in picks.get(idx[d], [])]
        rows.append({"date": d, "live": syms, "backtest": bt,
                     "overlap": len(set(syms) & set(bt))})
    return rows


# ── Report ───────────────────────────────────────────────────────────────────

def fmt(s: dict) -> str:
    if not s.get("trades"):
        return "no trades"
    line = (f"{s['trades']:>5} trades | win {s['win_rate']:.0%} | mean {s['mean_ret']:+.2%} "
            f"(t={s['t_stat']:+.1f}) | median {s['median_ret']:+.2%} | P&L ${s['total_pnl']:+,.0f} | PF {s['profit_factor']:.2f} | "
            f"maxDD ${s['max_drawdown']:,.0f} | hold {s['avg_hold_days']:.1f}d")
    if "mean_excess_vs_spy" in s:
        line += f" | vs SPY {s['mean_excess_vs_spy']:+.2%} (t={s['excess_t_stat']:+.1f})"
    return line


def fmt_random(runs: list, strat: dict) -> str:
    means = np.array([r["mean_ret"] for r in runs if r.get("trades")])
    medians = np.array([r["median_ret"] for r in runs if r.get("trades")])
    wins = np.array([r["win_rate"] for r in runs if r.get("trades")])
    pnls = np.array([r["total_pnl"] for r in runs if r.get("trades")])
    if len(means) == 0:
        return "no trades"
    pct = (means < strat["mean_ret"]).mean()
    return (f"mean/trade {means.mean():+.2%} (5-95%: {np.percentile(means, 5):+.2%} .. "
            f"{np.percentile(means, 95):+.2%}) | median {medians.mean():+.2%} | win {wins.mean():.0%} | "
            f"P&L ${pnls.mean():+,.0f} | strategy beats {pct:.0%} of {len(means)} random runs")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2024-09-03", help="first signal session")
    ap.add_argument("--end", default=(date.today() - timedelta(days=1)).isoformat())
    ap.add_argument("--feed", default="iex", choices=["iex", "sip"], help="iex matches the live signal")
    ap.add_argument("--slippage-bps", type=float, default=10.0)
    ap.add_argument("--no-earnings", action="store_true", help="skip the news-based earnings filter")
    ap.add_argument("--no-news-split", action="store_true",
                    help="skip the no-news vs news breakdown (one cached news lookup per traded pick)")
    ap.add_argument("--news-days-before", type=int, default=1,
                    help="days before the signal day counted as 'around the drop' (default 1)")
    ap.add_argument("--seeds", type=int, default=RANDOM_SEEDS)
    ap.add_argument("--fetch-only", action="store_true")
    args = ap.parse_args()

    print(f"Loading data ({args.feed}, {args.start} .. {args.end})...", flush=True)
    panels, universe, calendar = load_panels(args.start, args.end, args.feed)
    if args.fetch_only:
        print(f"Cached {panels['c'].shape[1]:,} symbols x {panels['c'].shape[0]} sessions.")
        return

    dates = list(panels["c"].index)
    ret, losers, quiet, any_valid = compute_masks(panels, universe)
    signal_rows = [i for i, d in enumerate(dates) if d >= args.start and i + 1 < len(dates)]

    print(f"Picking signals over {len(signal_rows)} sessions"
          f"{'' if args.no_earnings else ' (with earnings news lookups -- slow first time, cached after)'}...",
          flush=True)
    picks = pick_signals(ret, losers, signal_rows, EarningsChecker(not args.no_earnings))

    arrays = tuple(panels[f].to_numpy() for f in ("o", "h", "l", "c"))
    slip = args.slippage_bps / 10000
    if "SPY" not in panels["c"].columns:
        raise SystemExit("SPY bars missing -- can't compute benchmark")
    spy = (panels["o"]["SPY"].to_numpy(), panels["c"]["SPY"].to_numpy())

    strat = run_trades(picks, arrays, slip)
    s_strat = summarize(strat, spy)
    s_h5 = summarize(run_trades(picks, arrays, slip, hold_days=5), spy)
    s_h20 = summarize(run_trades(picks, arrays, slip, hold_days=20), spy)
    print(f"Running {args.seeds} random baselines x 2 pools...", flush=True)
    rand_quiet = random_baseline(picks, quiet, arrays, slip, args.seeds)
    rand_any = random_baseline(picks, any_valid, arrays, slip, args.seeds)

    # News split: was the drop accompanied by news, or did it come out of nowhere?
    news = NewsChecker(not args.no_news_split, args.news_days_before)
    counts = {}
    if news.enabled:
        pairs = sorted({(t["signal_t"], t["col"]) for t in strat})
        print(f"Fetching news counts for {len(pairs)} traded picks (cached after first run)...", flush=True)
        for n, (t, j) in enumerate(pairs):
            counts[(t, j)] = news.count(panels["c"].columns[j], panels["c"].index[t])
            if (n + 1) % 100 == 0:
                print(f"  news: {n + 1}/{len(pairs)}", flush=True)
        news.save()
    no_news, with_news, unknown_news = split_by_news(strat, counts)
    s_no_news, s_with_news = summarize(no_news, spy), summarize(with_news, spy)

    mid = signal_rows[len(signal_rows) // 2]
    first = [t for t in strat if t["signal_t"] < mid]
    second = [t for t in strat if t["signal_t"] >= mid]
    spy_bh = spy[1][signal_rows[-1] + 1] / spy[0][signal_rows[0] + 1] - 1

    cols = panels["c"].columns
    parity = live_parity(picks, dates, cols)

    lines = [
        "=" * 100,
        f"CONSISTENT-HISTORY LOSERS BACKTEST  |  signals {dates[signal_rows[0]]} .. {dates[signal_rows[-1]]}  |  "
        f"feed={args.feed}  slippage={args.slippage_bps:.0f}bps/side  earnings_filter={'off' if args.no_earnings else 'on'}",
        "=" * 100,
        f"Strategy (staged stops) : {fmt(s_strat)}",
        f"Same picks, hold 5d     : {fmt(s_h5)}",
        f"Same picks, hold 20d    : {fmt(s_h20)}",
        f"Random QUIET names      : {fmt_random(rand_quiet, s_strat)}",
        f"Random ANY names >=${ANY_POOL_MIN_PRICE:.0f}   : {fmt_random(rand_any, s_strat)}",
        f"SPY buy & hold (period) : {spy_bh:+.2%}",
    ]
    if news.enabled:
        lines += [
            "",
            f"-- News split (articles in the {args.news_days_before}d before the drop, through that day's close) --",
            f"NO news at the drop     : {fmt(s_no_news)}",
            f"NEWS at the drop        : {fmt(s_with_news)}",
            f"Difference (no-news minus news): "
            f"{(s_no_news.get('mean_ret', 0) - s_with_news.get('mean_ret', 0)):+.2%}/trade "
            f"(Welch t={welch_t(no_news, with_news):+.1f}); {len(unknown_news)} trade(s) unclassified",
        ]
    lines += [
        "",
        f"First half  : {fmt(summarize(first, spy))}",
        f"Second half : {fmt(summarize(second, spy))}",
        "",
        "Exit reasons: " + ", ".join(f"{k}={v}" for k, v in
                                     pd.Series([t['reason'] for t in strat]).value_counts().items()),
    ]
    if parity:
        tot_live = sum(len(p["live"]) for p in parity)
        tot_overlap = sum(p["overlap"] for p in parity)
        lines += ["", f"Live parity: backtest reproduced {tot_overlap}/{tot_live} live picks across "
                      f"{len(parity)} sessions (live rules changed mid-August, so early sessions differ)"]
        for p in parity[-8:]:
            lines.append(f"  {p['date']}  live={','.join(p['live']) or '-':32} backtest={','.join(p['backtest']) or '-'}")
    report = "\n".join(lines)
    print("\n" + report)

    out_dir = RESULTS_DIR / datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.txt").write_text(report, encoding="utf-8")
    pd.DataFrame([{**t, "symbol": cols[t["col"]], "signal_date": dates[t["signal_t"]],
                   "entry_date": dates[t["entry_t"]], "exit_date": dates[t["exit_t"]],
                   "news_count": counts.get((t["signal_t"], t["col"]))}
                  for t in strat]).drop(columns=["col", "signal_t", "entry_t", "exit_t"]) \
        .to_csv(out_dir / "trades.csv", index=False)
    (out_dir / "summary.json").write_text(json.dumps({
        "args": vars(args), "strategy": s_strat, "hold_5d": s_h5, "hold_20d": s_h20,
        "no_news": s_no_news, "with_news": s_with_news,
        "news_diff_welch_t": welch_t(no_news, with_news) if news.enabled else None,
        "random_quiet": rand_quiet, "random_any": rand_any, "spy_buy_hold": spy_bh,
        "parity": parity}, indent=2, default=str))
    print(f"\nSaved to {out_dir}")


if __name__ == "__main__":
    main()
