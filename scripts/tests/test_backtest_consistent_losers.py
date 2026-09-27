#!/usr/bin/env python3
"""
Tests for backtest_consistent_losers.py. The important one is parity: the
vectorized screen must agree with the live check_stability() on every
symbol/session, or the backtest is testing a different strategy.

Run with:
    python -m unittest scripts.tests.test_backtest_consistent_losers   (from repo root)
"""

import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import backtest_consistent_losers as bt

live = bt.live_signal


def synthetic_panels(n_days=60, n_syms=80, seed=7):
    rng = np.random.default_rng(seed)
    dates = [f"2026-01-{d:02d}" if d <= 31 else f"2026-02-{d - 31:02d}" for d in range(1, n_days + 1)]
    syms = [f"S{i}" for i in range(n_syms)]
    # mix of quiet and noisy names so both sides of every threshold get exercised
    vol_scale = rng.choice([0.01, 0.02, 0.04], size=n_syms)
    rets = rng.normal(0, 1, (n_days, n_syms)) * vol_scale
    close = 50 * np.cumprod(1 + rets, axis=0)
    volume = np.exp(rng.normal(12, rng.choice([0.1, 0.25, 0.5], size=n_syms), (n_days, n_syms)))
    close_df = pd.DataFrame(close, index=dates, columns=syms)
    vol_df = pd.DataFrame(volume, index=dates, columns=syms)
    # holes and zero-volume days, which must fail closed
    close_df.iloc[30, 3] = np.nan
    vol_df.iloc[40, 5] = 0
    panels = {"o": close_df, "h": close_df * 1.01, "l": close_df * 0.99, "c": close_df, "v": vol_df}
    return panels, {s: "Some Company Inc" for s in syms}


class TestSignalParity(unittest.TestCase):
    def test_vectorized_screen_matches_live_check_stability(self):
        panels, universe = synthetic_panels()
        ret, losers, quiet, _ = bt.compute_masks(panels, universe)
        dates = list(panels["c"].index)
        lb = live.LOOKBACK_DAYS
        checked = agreed_losers = 0
        for t in range(lb + 1, len(dates)):
            window = dates[t - (lb + 1): t + 1]
            for sym in panels["c"].columns:
                bars = {d: {"c": panels["c"].at[d, sym], "v": panels["v"].at[d, sym]}
                        for d in window if pd.notna(panels["c"].at[d, sym]) and pd.notna(panels["v"].at[d, sym])}
                res = live.check_stability(window, bars)
                live_quiet = bool(res and res["volume_stable"])
                live_loser = bool(live_quiet and res["pct_change"] < 0)
                self.assertEqual(bool(quiet.iat[t, panels["c"].columns.get_loc(sym)]), live_quiet,
                                 f"quiet mismatch {sym} {dates[t]}")
                self.assertEqual(bool(losers.iat[t, panels["c"].columns.get_loc(sym)]), live_loser,
                                 f"loser mismatch {sym} {dates[t]}")
                checked += 1
                agreed_losers += live_loser
        self.assertGreater(agreed_losers, 0, "synthetic data never produced a loser -- test is vacuous")
        self.assertGreater(checked, 1000)

    def test_funds_excluded(self):
        panels, universe = synthetic_panels()
        universe["S0"] = "SPDR S&P 500 ETF Trust"
        _, losers, quiet, any_valid = bt.compute_masks(panels, universe)
        self.assertFalse(quiet["S0"].any())
        self.assertFalse(any_valid["S0"].any())


def arrays(opens, highs, lows, closes):
    col = lambda xs: np.array(xs, dtype=float).reshape(-1, 1)
    return col(opens), col(highs), col(lows), col(closes)


class TestSimulate(unittest.TestCase):
    def test_tight_stop_hit_on_entry_day(self):
        O, H, L, C = arrays([100, 99], [100.5, 99], [96, 98], [97, 98])
        r = bt.simulate(O, H, L, C, 0, 0, slip=0)
        self.assertEqual(r["reason"], "stop")
        self.assertAlmostEqual(r["exit"], 97.0)
        self.assertEqual(r["qty"], 5)

    def test_gap_below_stop_fills_at_open(self):
        O, H, L, C = arrays([100, 90], [100, 91], [99, 88], [99.5, 89])
        r = bt.simulate(O, H, L, C, 0, 0, slip=0)
        self.assertEqual((r["reason"], r["exit"]), ("gap", 90.0))

    def test_breakeven_then_wide_trail(self):
        # day 0 high 104 -> 3% trail (100.88) clears entry -> breakeven hold at 100
        # day 1 high 106 >= 105 -> wide trail at 100.7; day 2 low 100.5 hits it
        O, H, L, C = arrays([100, 103, 102], [104, 106, 102], [100.5, 102, 100.5], [103, 105, 101])
        r = bt.simulate(O, H, L, C, 0, 0, slip=0)
        self.assertEqual(r["reason"], "stop")
        self.assertAlmostEqual(r["exit"], 106 * 0.95)

    def test_same_day_high_does_not_raise_that_days_stop(self):
        # high 110 would move the stop above 97, but the low is checked first against the old stop
        O, H, L, C = arrays([100, 108], [110, 109], [97.5, 107], [108, 108])
        r = bt.simulate(O, H, L, C, 0, 0, slip=0)
        self.assertEqual(r["exit_t"], 1)

    def test_open_position_marked_at_last_close(self):
        O, H, L, C = arrays([100, 100.5], [101, 101], [99.5, 100], [100.5, 100.8])
        r = bt.simulate(O, H, L, C, 0, 0, slip=0)
        self.assertEqual((r["reason"], r["exit"]), ("open", 100.8))

    def test_slippage_applied_both_sides(self):
        O, H, L, C = arrays([100, 100], [100, 100], [100, 100], [100, 100])
        r = bt.simulate(O, H, L, C, 0, 0, slip=0.001, hold_days=2)
        self.assertAlmostEqual(r["ret"], 0.999 / 1.001 - 1)

    def test_expensive_stock_buys_one_share(self):
        O, H, L, C = arrays([800], [801], [799], [800])
        self.assertEqual(bt.simulate(O, H, L, C, 0, 0, slip=0)["qty"], 1)


if __name__ == "__main__":
    unittest.main()


class TestNewsSplit(unittest.TestCase):
    """The no-news / news breakdown: which trades land in which bucket."""

    def trade(self, t, j, ret):
        return {"signal_t": t, "col": j, "ret": ret, "pnl": ret * 100,
                "entry_t": t + 1, "exit_t": t + 3}

    def test_buckets_by_count(self):
        trades = [self.trade(0, 1, 0.05), self.trade(0, 2, -0.02), self.trade(1, 1, 0.01)]
        counts = {(0, 1): 0, (0, 2): 7, (1, 1): 0}
        quiet, loud, unknown = bt.split_by_news(trades, counts)
        self.assertEqual([t["col"] for t in quiet], [1, 1])
        self.assertEqual([t["col"] for t in loud], [2])
        self.assertEqual(unknown, [])

    def test_failed_lookup_is_unclassified(self):
        # -1 is the cache's "the API call failed" marker: excluded, never
        # silently counted as "no news" (which would bias the headline result)
        trades = [self.trade(0, 1, 0.05), self.trade(0, 2, 0.01)]
        quiet, loud, unknown = bt.split_by_news(trades, {(0, 1): -1})
        self.assertEqual((quiet, loud), ([], []))
        self.assertEqual(len(unknown), 2)   # (0,2) missing from counts entirely

    def test_welch_t_sign_and_zero_cases(self):
        a = [self.trade(0, i, r) for i, r in enumerate([0.05, 0.04, 0.06, 0.05])]
        b = [self.trade(1, i, r) for i, r in enumerate([-0.05, -0.04, -0.06, -0.05])]
        self.assertGreater(bt.welch_t(a, b), 5)
        self.assertLess(bt.welch_t(b, a), -5)
        self.assertEqual(bt.welch_t(a, []), 0.0)

    def test_disabled_checker_returns_zero_without_network(self):
        self.assertEqual(bt.NewsChecker(False).count("AAPL", "2026-09-21"), 0)
