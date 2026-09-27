#!/usr/bin/env python3
"""
Unit tests for strategy_ledger.py — per-strategy realized P&L log.

Uses a temp directory for LEDGER_DIR so tests never touch real ledger files.
Run with:
    python -m unittest strategies.tests.test_strategy_ledger   (from repo root)
or  python -m unittest tests.test_strategy_ledger              (from strategies/)
"""

import os
import sys
import shutil
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import strategy_ledger as sl


class TestStrategyLedger(unittest.TestCase):
    def setUp(self):
        self._orig_dir = sl.LEDGER_DIR
        self._tmp = tempfile.mkdtemp()
        sl.LEDGER_DIR = Path(self._tmp)

    def tearDown(self):
        sl.LEDGER_DIR = self._orig_dir
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_summarize_empty_ledger(self):
        summary = sl.summarize("nonexistent_strategy")
        self.assertEqual(summary["realized_pl"], 0.0)
        self.assertEqual(summary["trade_count"], 0)
        self.assertEqual(summary["win_rate"], 0.0)

    def test_record_and_summarize_single_event(self):
        sl.record("wheel", {"type": "CSP", "total": 125.50})
        summary = sl.summarize("wheel")
        self.assertEqual(summary["realized_pl"], 125.50)
        self.assertEqual(summary["trade_count"], 1)
        self.assertEqual(summary["win_rate"], 1.0)

    def test_win_rate_mixed_events(self):
        sl.record("wheel", {"total": 100.0})
        sl.record("wheel", {"total": -50.0})
        sl.record("wheel", {"total": 30.0})
        summary = sl.summarize("wheel")
        self.assertAlmostEqual(summary["realized_pl"], 80.0)
        self.assertEqual(summary["trade_count"], 3)
        self.assertAlmostEqual(summary["win_rate"], 2 / 3, places=3)

    def test_strategies_are_isolated(self):
        sl.record("wheel", {"total": 100.0})
        sl.record("momentum", {"total": -20.0})
        self.assertEqual(sl.summarize("wheel")["realized_pl"], 100.0)
        self.assertEqual(sl.summarize("momentum")["realized_pl"], -20.0)

    def test_malformed_line_is_skipped(self):
        path = sl._ledger_path("wheel")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            f.write('{"total": 10.0}\n')
            f.write("not valid json\n")
            f.write('{"total": 5.0}\n')
        summary = sl.summarize("wheel")
        self.assertEqual(summary["trade_count"], 2)
        self.assertAlmostEqual(summary["realized_pl"], 15.0)


if __name__ == "__main__":
    unittest.main()
