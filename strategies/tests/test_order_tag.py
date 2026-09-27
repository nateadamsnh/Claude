#!/usr/bin/env python3
"""
Unit tests for order_tag.py — client_order_id generation.

Run with:
    python -m unittest strategies.tests.test_order_tag   (from repo root)
or  python -m unittest tests.test_order_tag              (from strategies/)
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import order_tag


class TestClientOrderId(unittest.TestCase):
    def test_contains_strategy_and_symbol(self):
        cid = order_tag.client_order_id("wheel", "MARA")
        self.assertTrue(cid.startswith("wheel-MARA-"))

    def test_unique_across_calls(self):
        ids = {order_tag.client_order_id("wheel", "MARA") for _ in range(50)}
        self.assertEqual(len(ids), 50)

    def test_strips_unsafe_characters(self):
        cid = order_tag.client_order_id("wheel test", "BRK.B/X")
        # BRK.B/X: '.' and '-' are allowed, '/' and space are stripped
        self.assertNotIn(" ", cid)
        self.assertNotIn("/", cid)
        self.assertTrue(cid.startswith("wheeltest-BRK.BX-"))


if __name__ == "__main__":
    unittest.main()
