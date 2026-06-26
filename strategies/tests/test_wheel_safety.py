#!/usr/bin/env python3
"""
Unit tests for wheel_safety.py — the pure safety layer.

No network, no files (beyond an optional limits JSON in a temp dir). Run with:
    python -m unittest strategies.tests.test_wheel_safety      (from repo root)
or  python -m unittest tests.test_wheel_safety                 (from strategies/)
"""

import os
import sys
import json
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wheel_safety as ws


def short_put(occ, qty=-5, **extra):
    """Minimal Alpaca position dict for a short option."""
    p = {"symbol": occ, "qty": str(qty)}
    p.update(extra)
    return p


class TestParseOcc(unittest.TestCase):
    def test_valid_put(self):
        info = ws.parse_occ("SOFI260710P00015500")
        self.assertEqual(info["underlying"], "SOFI")
        self.assertEqual(info["type"], "put")
        self.assertEqual(info["strike"], 15.5)
        self.assertEqual(info["expiration"], date(2026, 7, 10))

    def test_valid_call(self):
        info = ws.parse_occ("IONQ260717C00060000")
        self.assertEqual(info["type"], "call")
        self.assertEqual(info["strike"], 60.0)

    def test_non_option(self):
        self.assertIsNone(ws.parse_occ("SOFI"))
        self.assertIsNone(ws.parse_occ(""))
        self.assertIsNone(ws.parse_occ(None))

    def test_bad_date(self):
        self.assertIsNone(ws.parse_occ("SOFI269910P00015500"))  # month 99


class TestLoadLimits(unittest.TestCase):
    def test_defaults_when_no_file(self):
        self.assertEqual(ws.load_limits(None), ws.DEFAULT_LIMITS)
        self.assertEqual(ws.load_limits("/no/such/file.json"), ws.DEFAULT_LIMITS)

    def test_overlay_and_ignore_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "limits.json")
            with open(p, "w") as f:
                json.dump({"max_contracts_per_symbol": 2, "bogus": 99}, f)
            lim = ws.load_limits(p)
            self.assertEqual(lim["max_contracts_per_symbol"], 2)         # overridden
            self.assertEqual(lim["max_notional_per_symbol"],
                             ws.DEFAULT_LIMITS["max_notional_per_symbol"])  # default kept
            self.assertNotIn("bogus", lim)                               # unknown ignored

    def test_broken_file_falls_back(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "limits.json")
            with open(p, "w") as f:
                f.write("{ not json")
            self.assertEqual(ws.load_limits(p), ws.DEFAULT_LIMITS)


class TestEntryLimits(unittest.TestCase):
    def setUp(self):
        self.lim = dict(ws.DEFAULT_LIMITS)

    def test_ok(self):
        r = ws.check_entry_limits("DKNG", 25.0, 5, 0.88, self.lim)
        self.assertTrue(r.allowed)

    def test_too_many_contracts(self):
        r = ws.check_entry_limits("DKNG", 25.0, 6, 0.88, self.lim)
        self.assertFalse(r.allowed)
        self.assertIn("max_contracts_per_symbol", r.reason)

    def test_notional_cap(self):
        # 5 x 53 x 100 = 26,500 > 15,000 default
        r = ws.check_entry_limits("IONQ", 53.0, 5, 2.31, self.lim)
        self.assertFalse(r.allowed)
        self.assertIn("notional", r.reason)

    def test_min_premium(self):
        r = ws.check_entry_limits("SOFI", 15.5, 3, 0.02, self.lim)
        self.assertFalse(r.allowed)
        self.assertIn("min_premium", r.reason)

    def test_zero_qty(self):
        r = ws.check_entry_limits("SOFI", 15.5, 0, 0.30, self.lim)
        self.assertFalse(r.allowed)


class TestDailyLossHalt(unittest.TestCase):
    def setUp(self):
        self.lim = dict(ws.DEFAULT_LIMITS)  # 10%

    def test_normal_day_allowed(self):
        self.assertTrue(ws.daily_loss_halt(49000, 50000, self.lim).allowed)  # -2%

    def test_breach_blocks(self):
        r = ws.daily_loss_halt(44000, 50000, self.lim)  # -12%
        self.assertFalse(r.allowed)
        self.assertIn("kill-switch", r.reason)

    def test_exact_threshold_blocks(self):
        self.assertFalse(ws.daily_loss_halt(45000, 50000, self.lim).allowed)  # -10%

    def test_gain_allowed(self):
        self.assertTrue(ws.daily_loss_halt(52000, 50000, self.lim).allowed)

    def test_fails_open_on_bad_data(self):
        self.assertTrue(ws.daily_loss_halt(0, 0, self.lim).allowed)
        self.assertTrue(ws.daily_loss_halt(100, "x", self.lim).allowed)


class TestReconcile(unittest.TestCase):
    def _state(self, **kw):
        base = {
            "stage": "CSP", "active_contract": None, "order_filled": False,
            "shares_held": 0, "untracked_open_positions": [],
        }
        base.update(kw)
        return base

    def test_clean_match(self):
        occ = "SOFI260710P00015500"
        state = self._state(active_contract=occ, order_filled=True)
        positions = {occ: short_put(occ, -3)}
        r = ws.reconcile_symbol("SOFI", state, positions)
        self.assertTrue(r.ok)
        self.assertEqual(r.untracked, [])

    def test_untracked_short_is_alert_not_halt(self):
        active = "SOFI260710P00015500"
        dup    = "SOFI260717P00016000"
        state = self._state(active_contract=active, order_filled=True)
        positions = {active: short_put(active, -3), dup: short_put(dup, -1)}
        r = ws.reconcile_symbol("SOFI", state, positions)
        self.assertTrue(r.ok)                  # untracked alone does NOT halt
        self.assertIn(dup, r.untracked)

    def test_acknowledged_untracked_is_silent(self):
        active = "SOFI260710P00015500"
        dup    = "SOFI260717P00016000"
        state = self._state(active_contract=active, order_filled=True,
                            untracked_open_positions=[{"contract": dup}])
        positions = {active: short_put(active, -3), dup: short_put(dup, -1)}
        r = ws.reconcile_symbol("SOFI", state, positions)
        self.assertTrue(r.ok)
        self.assertEqual(r.untracked, [])      # acknowledged => not re-flagged

    def test_phantom_filled_contract_halts(self):
        # State thinks it holds a filled put that the broker shows nowhere, and
        # it hasn't expired yet -> divergence -> halt.
        occ = "SOFI260710P00015500"
        state = self._state(active_contract=occ, order_filled=True)
        r = ws.reconcile_symbol("SOFI", state, {}, today=date(2026, 7, 1))
        self.assertFalse(r.ok)
        self.assertTrue(any("out of sync" in v for v in r.violations))

    def test_legit_expiry_after_exp_does_not_halt(self):
        occ = "SOFI260710P00015500"
        state = self._state(active_contract=occ, order_filled=True)
        r = ws.reconcile_symbol("SOFI", state, {}, today=date(2026, 7, 11))  # past exp
        self.assertTrue(r.ok)

    def test_cc_share_mismatch_halts(self):
        state = self._state(stage="CC", active_contract=None, order_filled=False,
                            shares_held=200)
        positions = {"IONQ": {"symbol": "IONQ", "qty": "100"}}  # only 100, expected 200
        r = ws.reconcile_symbol("IONQ", state, positions)
        self.assertFalse(r.ok)
        self.assertTrue(any("shares" in v for v in r.violations))

    def test_cc_share_match_ok(self):
        state = self._state(stage="CC", active_contract=None, order_filled=False,
                            shares_held=100)
        positions = {"IONQ": {"symbol": "IONQ", "qty": "100"}}
        r = ws.reconcile_symbol("IONQ", state, positions)
        self.assertTrue(r.ok)

    def test_assignment_not_flagged(self):
        # Filled put gone from options but shares now present = assignment, not a
        # divergence. Should NOT halt.
        occ = "DKNG260710P00025000"
        state = self._state(active_contract=occ, order_filled=True)
        positions = {"DKNG": {"symbol": "DKNG", "qty": "500"}}
        r = ws.reconcile_symbol("DKNG", state, positions)
        self.assertTrue(r.ok)

    def test_ignores_other_underlyings(self):
        state = self._state(active_contract="SOFI260710P00015500", order_filled=True)
        positions = {
            "SOFI260710P00015500": short_put("SOFI260710P00015500", -3),
            "MARA260710P00013500": short_put("MARA260710P00013500", -5),  # different symbol
        }
        r = ws.reconcile_symbol("SOFI", state, positions)
        self.assertTrue(r.ok)
        self.assertEqual(r.untracked, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
