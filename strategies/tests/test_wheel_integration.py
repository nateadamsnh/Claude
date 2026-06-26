#!/usr/bin/env python3
"""
Integration tests for options_wheel.py — exercise the real run_csp/run_cc and
order functions with the network monkeypatched out. These lock in the actual
bug fixes (false expiry, BTC fill-confirmation) and the new safety wiring
(dry-run, hard limits, kill-switch).

Run with:
    python -m unittest tests.test_wheel_integration      (from strategies/)

No network and no state files are touched: every Alpaca-touching function is
replaced, and save_state is stubbed.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import options_wheel as ow

# A contract expiring in 2099 so the "has it expired?" guards are deterministic
# regardless of the date the test runs.
FUTURE_PUT = "SOFI991231P00015500"


def fresh_state(**kw):
    s = {
        "stage": "CSP", "active_contract": None, "active_order_id": None,
        "closing_order_id": None, "active_qty": 0, "order_filled": False,
        "premium_sold": 0.0, "cost_basis": None, "shares_held": 0,
        "total_premium": 0.0, "cycle_count": 0, "premium_history": [],
        "last_summary_date": None, "untracked_open_positions": [],
    }
    s.update(kw)
    return s


class WheelTestBase(unittest.TestCase):
    def patch(self, name, value):
        """Set ow.<name> = value and restore the original after the test."""
        orig = getattr(ow, name)
        setattr(ow, name, value)
        self.addCleanup(setattr, ow, name, orig)

    def setUp(self):
        # Safe defaults shared by all tests.
        self.patch("save_state", lambda s: None)        # never write state files
        self.patch("SYMBOL", "SOFI")
        self.patch("BLOCK_NEW_ENTRIES", False)
        self.patch("DRY_RUN", False)


class TestDryRun(WheelTestBase):
    def _block_network(self):
        def boom(*a, **k):
            raise AssertionError("network call attempted during DRY_RUN")
        orig = ow.requests.post
        ow.requests.post = boom
        self.addCleanup(lambda: setattr(ow.requests, "post", orig))

    def test_sell_to_open_places_nothing(self):
        self.patch("DRY_RUN", True)
        self._block_network()
        out = ow.sell_to_open(
            {"symbol": FUTURE_PUT, "strike": 15.5, "exp": "2099-12-31", "mid": 0.30},
            "CSP-STO", qty=3)
        self.assertTrue(out.get("dry_run"))
        self.assertNotIn("id", out)

    def test_buy_to_close_places_nothing(self):
        self.patch("DRY_RUN", True)
        self._block_network()
        out = ow.buy_to_close(FUTURE_PUT, 0.10, "CSP-BTC", qty=3)
        self.assertTrue(out.get("dry_run"))
        self.assertNotIn("id", out)


class TestFalseExpiryGuard(WheelTestBase):
    def test_missing_contract_before_expiry_preserves_state(self):
        self.patch("get_latest_stock_price", lambda sym: 17.50)
        state = fresh_state(active_contract=FUTURE_PUT, order_filled=True,
                            active_qty=3, premium_sold=0.23)
        # Empty positions = the exact stale/empty snapshot that used to trigger a
        # phantom "expired worthless" + duplicate sale.
        ow.run_csp(state, {}, {"cash": 50000}, cash_budget=10000)
        self.assertEqual(state["active_contract"], FUTURE_PUT)  # NOT cleared
        self.assertEqual(state["cycle_count"], 0)               # NO phantom cycle


class TestBtcFillConfirmation(WheelTestBase):
    def test_unfilled_btc_keeps_position(self):
        self.patch("get_latest_stock_price", lambda sym: 17.50)
        self.patch("get_order", lambda oid: {"status": "expired"})
        state = fresh_state(active_contract=FUTURE_PUT, order_filled=True,
                            active_qty=3, premium_sold=0.23, closing_order_id="oid-1")
        positions = {FUTURE_PUT: {"symbol": FUTURE_PUT, "qty": "-3"}}  # still open
        ow.run_csp(state, positions, {"cash": 50000}, cash_budget=10000)
        self.assertEqual(state["active_contract"], FUTURE_PUT)  # still managed
        self.assertIsNone(state["closing_order_id"])            # flag cleared, will retry
        self.assertEqual(state["cycle_count"], 0)

    def test_filled_btc_closes_and_debits_premium(self):
        self.patch("get_latest_stock_price", lambda sym: 17.50)
        self.patch("get_order", lambda oid: {"status": "filled", "filled_avg_price": "0.10"})
        state = fresh_state(active_contract=FUTURE_PUT, order_filled=True,
                            active_qty=3, premium_sold=0.23, closing_order_id="oid-1",
                            total_premium=69.0)
        positions = {FUTURE_PUT: {"symbol": FUTURE_PUT, "qty": "-3"}}
        ow.run_csp(state, positions, {"cash": 50000}, cash_budget=10000)
        self.assertIsNone(state["active_contract"])             # closed
        self.assertIsNone(state["closing_order_id"])
        self.assertEqual(state["cycle_count"], 1)
        self.assertAlmostEqual(state["total_premium"], 69.0 - 0.10 * 100 * 3)  # 39.0
        self.assertEqual(state["premium_history"][-1]["type"], "CSP-BTC")


class TestHardLimits(WheelTestBase):
    def _drive_new_csp(self, contract, account_cash=1e9, budget=1e9):
        """Set up the new-CSP path so it reaches the limit check, then run."""
        self.patch("get_latest_stock_price", lambda sym: 53.0)
        self.patch("get_regime", lambda verbose=False: {
            "regime": "BULL", "spy_price": 700.0, "sma200": 650.0, "pct_vs_200": 7.7})
        self.patch("regime_contracts_multiplier", lambda n, r: n)
        self.patch("symbol_drawdown", lambda sym, px: None)
        self.patch("find_put_contract", lambda px: contract)
        sold = []
        self.patch("sell_to_open", lambda *a, **k: sold.append((a, k)) or {})
        state = fresh_state()
        ow.run_csp(state, {}, {"cash": account_cash}, cash_budget=budget)
        return sold, state

    def test_oversize_notional_blocked(self):
        self.patch("SYMBOL", "IONQ")
        # 5 x $53 x 100 = $26,500 > $15,000 default cap
        contract = {"symbol": "IONQ991231P00053000", "strike": 53.0,
                    "exp": "2099-12-31", "mid": 2.0}
        sold, state = self._drive_new_csp(contract)
        self.assertEqual(sold, [])                       # limit blocked the sale
        self.assertIsNone(state["active_contract"])

    def test_within_limits_allows_sale(self):
        self.patch("SYMBOL", "DKNG")
        # 5 x $25 x 100 = $12,500 < $15,000 cap -> allowed
        contract = {"symbol": "DKNG991231P00025000", "strike": 25.0,
                    "exp": "2099-12-31", "mid": 0.80}
        sold, state = self._drive_new_csp(contract)
        self.assertEqual(len(sold), 1)                   # sell_to_open was called


class TestKillSwitch(WheelTestBase):
    def test_blocks_new_csp_when_engaged(self):
        self.patch("BLOCK_NEW_ENTRIES", True)
        self.patch("get_latest_stock_price", lambda sym: 17.50)
        sold = []
        self.patch("sell_to_open", lambda *a, **k: sold.append(1) or {})
        # If the kill-switch didn't short-circuit, these would be needed; provide
        # them so a regression surfaces as "sold something" not an AttributeError.
        self.patch("get_regime", lambda verbose=False: {
            "regime": "BULL", "spy_price": 700.0, "sma200": 650.0, "pct_vs_200": 7.7})
        self.patch("regime_contracts_multiplier", lambda n, r: n)
        self.patch("symbol_drawdown", lambda sym, px: None)
        self.patch("find_put_contract", lambda px: {
            "symbol": FUTURE_PUT, "strike": 15.5, "exp": "2099-12-31", "mid": 0.30})
        state = fresh_state()
        ow.run_csp(state, {}, {"cash": 50000}, cash_budget=10000)
        self.assertEqual(sold, [])                       # no new entry while engaged
        self.assertIsNone(state["active_contract"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
