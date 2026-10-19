#!/usr/bin/env python3
"""
Unit tests for heartbeat.py -- log parsing, run freshness, broker checks.

Run with:
    python -m unittest scripts.tests.test_heartbeat   (from repo root)
"""

import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import heartbeat

HEADER = "DYNAMIC STOP MANAGER  |"


def line(ts: str, level: str, msg: str) -> str:
    return f"{ts},123 | {level:<8} | {msg}"


def keys(problems):
    return {k for k, _ in problems}


SESSION = {"open": datetime(2026, 9, 16, 9, 30), "close": datetime(2026, 9, 16, 16, 0)}

# Generic "every 15 min" freshness lambda, used as a test double for check_runs()
# -- not tied to any specific live job.
FRESH = lambda now, s: (min(now, s["close"] + timedelta(minutes=5)) - timedelta(minutes=25)
                        if now >= s["open"] + timedelta(minutes=25) else None)


class TestParseRuns(unittest.TestCase):
    def test_complete_run_with_errors(self):
        runs = heartbeat.parse_runs([
            line("2026-09-16 10:05:01", "INFO", "=" * 20),
            line("2026-09-16 10:05:01", "INFO", f"{HEADER}  2026-09-16 10:05"),
            line("2026-09-16 10:05:02", "ERROR", "  URGENT: XOM could not close the lot"),
            line("2026-09-16 10:05:03", "INFO", "Run complete."),
            "",
        ], HEADER)
        self.assertEqual(len(runs), 1)
        self.assertTrue(runs[0]["complete"])
        self.assertEqual(len(runs[0]["errors"]), 1)

    def test_run_without_completion_marker(self):
        runs = heartbeat.parse_runs([
            line("2026-09-16 10:05:01", "INFO", HEADER),
            line("2026-09-16 10:05:02", "INFO", "  doing work"),
        ], HEADER)
        self.assertFalse(runs[0]["complete"])

    def test_lines_before_first_header_ignored(self):
        runs = heartbeat.parse_runs([
            line("2026-09-16 10:04:59", "ERROR", "cut-off tail of an older run"),
            line("2026-09-16 10:05:01", "INFO", HEADER),
        ], HEADER)
        self.assertEqual(runs[0]["errors"], [])


class TestCheckRuns(unittest.TestCase):
    def run_at(self, start, complete=True, errors=()):
        return {"start": start, "end": start if complete else None, "complete": complete,
                "errors": list(errors)}

    def test_healthy(self):
        now = datetime(2026, 9, 16, 11, 40)
        runs = [self.run_at(datetime(2026, 9, 16, 11, 35))]
        self.assertEqual(heartbeat.check_runs("DSM", runs, now, SESSION, now - timedelta(minutes=15), FRESH), [])

    def test_crashed_run_detected(self):
        now = datetime(2026, 9, 16, 11, 50)
        runs = [self.run_at(datetime(2026, 9, 16, 11, 20)),
                self.run_at(datetime(2026, 9, 16, 11, 35), complete=False)]
        self.assertIn("DSM:unfinished", keys(heartbeat.check_runs("DSM", runs, now, SESSION, now, FRESH)))

    def test_stale_during_market_hours(self):
        now = datetime(2026, 9, 16, 13, 0)
        runs = [self.run_at(datetime(2026, 9, 16, 11, 35))]
        self.assertIn("DSM:stale", keys(heartbeat.check_runs("DSM", runs, now, SESSION, now, FRESH)))

    def test_not_stale_before_first_run_is_due(self):
        now = datetime(2026, 9, 16, 9, 40)
        runs = [self.run_at(datetime(2026, 9, 15, 16, 5))]
        self.assertEqual(heartbeat.check_runs("DSM", runs, now, SESSION, now, FRESH), [])

    def test_not_stale_on_market_holiday(self):
        now = datetime(2026, 9, 16, 13, 0)
        runs = [self.run_at(datetime(2026, 9, 11, 16, 5))]
        self.assertEqual(heartbeat.check_runs("DSM", runs, now, None, now, FRESH), [])

    def test_last_close_run_satisfies_evening_check(self):
        now = datetime(2026, 9, 16, 17, 0)
        runs = [self.run_at(datetime(2026, 9, 16, 16, 5))]
        self.assertEqual(heartbeat.check_runs("DSM", runs, now, SESSION, now, FRESH), [])

    def test_only_new_errors_reported(self):
        now = datetime(2026, 9, 16, 11, 40)
        old = (datetime(2026, 9, 16, 11, 5), "URGENT old")
        new = (datetime(2026, 9, 16, 11, 35), "URGENT new")
        runs = [self.run_at(datetime(2026, 9, 16, 11, 35), errors=[old, new])]
        problems = heartbeat.check_runs("DSM", runs, now, SESSION, datetime(2026, 9, 16, 11, 25), FRESH)
        self.assertEqual(keys(problems), {"DSM:errors"})
        self.assertIn("1 ERROR", problems[0][1])

    def test_disabled_job_skips_freshness_and_unfinished(self):
        now = datetime(2026, 9, 22, 13, 0)
        runs = [self.run_at(datetime(2026, 9, 16, 9, 31), complete=False)]
        self.assertEqual(heartbeat.check_runs("DSM", runs, now, SESSION, now, FRESH, disabled=True), [])

    def test_disabled_job_still_reports_new_errors(self):
        now = datetime(2026, 9, 22, 13, 0)
        runs = [self.run_at(datetime(2026, 9, 22, 12, 50), errors=[(datetime(2026, 9, 22, 12, 51), "boom")])]
        problems = heartbeat.check_runs("DSM", runs, now, SESSION, datetime(2026, 9, 22, 12, 45), FRESH,
                                        disabled=True)
        self.assertEqual(keys(problems), {"DSM:errors"})

    def test_buy_job_needs_a_run_today(self):
        # generic "must have run once since today's open" freshness lambda
        fresh = lambda now, s: (s["open"] if now >= s["open"] + timedelta(minutes=15) else None)
        now = datetime(2026, 9, 16, 10, 0)
        yesterday = [self.run_at(datetime(2026, 9, 15, 9, 31))]
        self.assertIn("B:stale", keys(heartbeat.check_runs("B", yesterday, now, SESSION, now, fresh)))
        today = [self.run_at(datetime(2026, 9, 16, 9, 31))]
        self.assertEqual(heartbeat.check_runs("B", today, now, SESSION, now, fresh), [])


def pos(sym, qty):
    return {"symbol": sym, "qty": str(qty), "asset_class": "us_equity"}


def order(sym, qty, filled=0):
    return {"symbol": sym, "qty": str(qty), "filled_qty": str(filled), "side": "sell", "type": "stop"}


class TestCheckBroker(unittest.TestCase):
    def test_healthy(self):
        self.assertEqual(heartbeat.check_broker([pos("KDP", 15)], [order("KDP", 15)]), [])

    def test_fractional_remainder_is_not_a_problem(self):
        self.assertEqual(heartbeat.check_broker([pos("CVX", 3.437), pos("SPY", 0.187)],
                                                [order("CVX", 2), order("CVX", 1)]), [])

    def test_short_position(self):
        self.assertIn("short:KBR", keys(heartbeat.check_broker([pos("KBR", -13)], [])))

    def test_uncovered_shares(self):
        self.assertIn("uncovered:HRL", keys(heartbeat.check_broker([pos("HRL", 141)], [order("HRL", 100)])))

    def test_sell_orders_exceed_holdings(self):
        # the KBR failure: two stops for 13 shares each against 13 held
        problems = heartbeat.check_broker([pos("KBR", 13)], [order("KBR", 13), order("KBR", 13)])
        self.assertIn("oversell:KBR", keys(problems))

    def test_sell_order_with_no_position(self):
        self.assertIn("oversell:MGM", keys(heartbeat.check_broker([], [order("MGM", 12)])))

    def test_option_sell_orders_are_not_stock_sells(self):
        # a resting covered-call STO must not read as "5 shares sold but 0 held"
        call = {"symbol": "MARA261106C00012000", "qty": "5", "filled_qty": "0", "side": "sell",
                "type": "limit", "asset_class": "us_option"}
        self.assertEqual(heartbeat.check_broker([pos("MARA", 500)], [call], {"MARA": 500}), [])

    def test_wheel_owned_shares_need_no_stop(self):
        # 2026-10-19: assigned MARA shares must stay unreserved for covered calls
        self.assertEqual(heartbeat.check_broker([pos("MARA", 500)], [], {"MARA": 500}), [])

    def test_only_the_non_wheel_part_needs_a_stop(self):
        problems = heartbeat.check_broker([pos("INTC", 112)], [order("INTC", 5)], {"INTC": 100})
        self.assertEqual(keys(problems), {"uncovered:INTC"})  # 12 held outside the wheel, only 5 covered

    def test_wheel_shares_do_not_hide_a_short(self):
        self.assertIn("short:MARA", keys(heartbeat.check_broker([pos("MARA", -5)], [], {"MARA": 500})))

    def test_partially_filled_order_counts_remaining_only(self):
        self.assertEqual(heartbeat.check_broker([pos("XOM", 6)], [order("XOM", 10, filled=4)]), [])


if __name__ == "__main__":
    unittest.main()
