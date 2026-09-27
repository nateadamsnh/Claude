#!/usr/bin/env python3
"""
Unit tests for capital_allocator.py — the shared strategy weight-split.

No network, no files (registries are passed in-memory). Run with:
    python -m unittest strategies.tests.test_capital_allocator   (from repo root)
or  python -m unittest tests.test_capital_allocator              (from strategies/)
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import capital_allocator as ca


def registry(*entries):
    return {"strategies": [
        {"name": n, "weight": w, "enabled": e} for (n, w, e) in entries
    ]}


class TestWeightFraction(unittest.TestCase):
    def test_single_strategy_gets_full_share(self):
        reg = registry(("wheel", 1.0, True))
        self.assertEqual(ca.weight_fraction("wheel", reg), 1.0)

    def test_two_equal_strategies_split_evenly(self):
        reg = registry(("wheel", 1.0, True), ("momentum", 1.0, True))
        self.assertAlmostEqual(ca.weight_fraction("wheel", reg), 0.5)
        self.assertAlmostEqual(ca.weight_fraction("momentum", reg), 0.5)

    def test_uneven_weights(self):
        reg = registry(("wheel", 3.0, True), ("momentum", 1.0, True))
        self.assertAlmostEqual(ca.weight_fraction("wheel", reg), 0.75)
        self.assertAlmostEqual(ca.weight_fraction("momentum", reg), 0.25)

    def test_disabled_strategy_excluded_from_denominator(self):
        reg = registry(("wheel", 1.0, True), ("momentum", 1.0, False))
        self.assertEqual(ca.weight_fraction("wheel", reg), 1.0)

    def test_unregistered_name_fails_open_to_full_share(self):
        reg = registry(("wheel", 1.0, True))
        self.assertEqual(ca.weight_fraction("unknown_strategy", reg), 1.0)

    def test_empty_registry_fails_open(self):
        reg = registry()
        self.assertEqual(ca.weight_fraction("wheel", reg), 1.0)

    def test_zero_total_weight_fails_open(self):
        reg = registry(("wheel", 0.0, True))
        self.assertEqual(ca.weight_fraction("wheel", reg), 1.0)


class TestAllocatedAmounts(unittest.TestCase):
    def test_allocated_buying_power_uses_options_buying_power(self):
        reg = registry(("wheel", 1.0, True), ("momentum", 1.0, True))
        account = {"options_buying_power": "10000", "cash": "999999"}
        self.assertAlmostEqual(ca.allocated_buying_power("wheel", account, reg), 5000.0)

    def test_allocated_buying_power_falls_back_to_cash(self):
        reg = registry(("wheel", 1.0, True))
        account = {"cash": "8000"}
        self.assertAlmostEqual(ca.allocated_buying_power("wheel", account, reg), 8000.0)

    def test_allocated_buying_power_missing_fields_is_zero(self):
        reg = registry(("wheel", 1.0, True))
        self.assertEqual(ca.allocated_buying_power("wheel", {}, reg), 0.0)

    def test_allocated_equity_split(self):
        reg = registry(("wheel", 1.0, True), ("momentum", 3.0, True))
        account = {"equity": "40000"}
        self.assertAlmostEqual(ca.allocated_equity("wheel", account, reg), 10000.0)
        self.assertAlmostEqual(ca.allocated_equity("momentum", account, reg), 30000.0)


class TestLoadRegistry(unittest.TestCase):
    def test_missing_file_falls_back_to_default(self):
        reg = ca.load_registry("/nonexistent/path/strategy_framework.json")
        self.assertEqual(reg, ca.DEFAULT_REGISTRY)


if __name__ == "__main__":
    unittest.main()
