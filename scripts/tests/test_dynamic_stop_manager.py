"""Tests for scripts/dynamic_stop_manager.py's never-sell-what-you-don't-hold guards.

Run: python -m unittest scripts.tests.test_dynamic_stop_manager

Replays the two ways the manager shorted stocks before 2026-09-22:
  * MGM 2026-09-14: the lot's stop had already filled, but the lot stayed in
    state and a market sell went out for the same 12 shares.
  * KBR 2026-09-09 / ghost lots: state tracked shares the account no longer
    held, and every stop replacement tried to sell them.
No network: every broker call is patched.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))
import dynamic_stop_manager as dsm

dsm.log.disabled = True   # keep test runs out of logs/dynamic_stop_manager.log (the heartbeat reads it)


def lot(sym, qty, stop_id=None, stop_price=10.0, entry=10.0, high=10.0, stage="tight"):
    return {"buy_order_id": f"b-{sym}-{qty}-{stop_id}", "symbol": sym, "qty": qty, "entry_price": entry,
            "highest_price": high, "stage": stage, "stop_order_id": stop_id, "stop_price": stop_price}


def pos(qty):
    return {"qty": str(qty)}


def order(oid, sym, qty, side="sell", typ="stop", filled=0):
    return {"id": oid, "symbol": sym, "qty": str(qty), "filled_qty": str(filled), "side": side, "type": typ}


class TestAllocateLots(unittest.TestCase):
    def test_ghost_lots_dropped(self):
        lots = [lot("FHB", 75), lot("M", 42)]
        kept, dropped, resized = dsm.allocate_lots_to_holdings(lots, {}, set())
        self.assertEqual(kept, [])
        self.assertEqual(len(dropped), 2)

    def test_lot_with_live_stop_kept_over_stale_one(self):
        live, stale = lot("CVX", 2, stop_id="s1"), lot("CVX", 6)
        kept, dropped, _ = dsm.allocate_lots_to_holdings([stale, live], {"CVX": 7}, {"s1"})
        # live 2 kept whole, stale shrunk to the remaining 5
        self.assertEqual([(l["qty"], l["stop_order_id"]) for l in kept], [(5, None), (2, "s1")])
        self.assertEqual(dropped, [])

    def test_partial_lot_resized_and_flagged_for_replacement(self):
        kept, _, resized = dsm.allocate_lots_to_holdings([lot("HTGC", 118, stop_id="s1")], {"HTGC": 59}, {"s1"})
        self.assertEqual(kept[0]["qty"], 59)
        self.assertIsNone(kept[0]["stop_price"])
        self.assertEqual(resized, [("HTGC", 118, 59)])

    def test_matching_state_untouched(self):
        lots = [lot("LQD", 4, stop_id="s1")]
        kept, dropped, resized = dsm.allocate_lots_to_holdings(lots, {"LQD": 4}, {"s1"})
        self.assertEqual((kept, dropped, resized), (lots, [], []))
        self.assertEqual(kept[0]["stop_price"], 10.0)


class TestSellableQty(unittest.TestCase):
    def check(self, positions, orders, sym, ignore=None):
        with mock.patch.object(dsm, "get_positions", return_value=positions), \
             mock.patch.object(dsm, "get_open_orders", return_value=orders):
            return dsm.sellable_qty(sym, ignore_order_id=ignore)

    def test_not_held(self):
        self.assertEqual(self.check({}, [], "MGM"), 0)

    def test_short(self):
        self.assertEqual(self.check({"KBR": pos(-13)}, [], "KBR"), 0)

    def test_reserved_by_open_sell(self):
        self.assertEqual(self.check({"X": pos(10)}, [order("o1", "X", 4)], "X"), 6)

    def test_ignored_order_not_reserved(self):
        self.assertEqual(self.check({"X": pos(10)}, [order("o1", "X", 10)], "X", ignore="o1"), 10)

    def test_fraction_floored(self):
        self.assertEqual(self.check({"X": pos(7.437)}, [], "X"), 7)


class TestRunInnerNeverShorts(unittest.TestCase):
    """Drive _run_inner against a fake broker and record every sell it sends."""

    def run_with(self, lots, positions, open_orders, price):
        state = {"lots": lots}
        sells = []
        patches = [
            mock.patch.object(dsm, "get_open_orders", return_value=open_orders),
            mock.patch.object(dsm, "get_positions", return_value=positions),
            mock.patch.object(dsm, "get_latest_price", return_value=price),
            mock.patch.object(dsm, "absorb_new_lots"),
            mock.patch.object(dsm, "cancel_order"),
            mock.patch.object(dsm, "submit_stop", side_effect=lambda s, q, p: sells.append(("stop", s, q)) or "new"),
            mock.patch.object(dsm, "submit_market_sell", side_effect=lambda s, q: sells.append(("mkt", s, q)) or "m"),
            mock.patch.object(dsm.time, "sleep"),
            mock.patch.object(dsm.requests, "get"),
        ]
        for p in patches:
            p.start()
        try:
            dsm._run_inner(state)
        finally:
            for p in patches:
                p.stop()
        return state, sells

    def test_mgm_replay_no_second_sell(self):
        # Stop already filled (no position, no open order); the lot's order id
        # was lost by a failed replacement, so step 1 can't retire it. Its
        # target is above price, which used to trigger a market sell.
        state, sells = self.run_with([lot("MGM", 12, stop_id=None, stop_price=None, entry=40.37, high=41.1)],
                                     positions={}, open_orders=[], price=39.79)
        self.assertEqual(sells, [])
        self.assertEqual(state["lots"], [])

    def test_ghost_lots_never_sell(self):
        lots = [lot("FHB", 75, stop_id=None, stop_price=None), lot("M", 42, stop_id=None, stop_price=None)]
        state, sells = self.run_with(lots, positions={"FHB": pos(0.371), "M": pos(0.964)}, open_orders=[], price=9.0)
        self.assertEqual(sells, [])
        self.assertEqual(state["lots"], [])

    def test_short_position_never_sold(self):
        state, sells = self.run_with([lot("KBR", 13, stop_id=None, stop_price=None)],
                                     positions={"KBR": pos(-13)}, open_orders=[], price=35.0)
        self.assertEqual(sells, [])

    def test_oversized_lot_replaced_at_held_size(self):
        state, sells = self.run_with([lot("HTGC", 118, stop_id="s1", entry=16.6, high=17.9)],
                                     positions={"HTGC": pos(59.955)},
                                     open_orders=[order("s1", "HTGC", 59)], price=17.5)
        self.assertEqual(sells, [("stop", "HTGC", 59)])
        self.assertEqual(state["lots"][0]["qty"], 59)

    def test_healthy_lot_unchanged(self):
        # entry 106.14, high 106.95 -> tight target 103.74 == resting stop, so nothing to do
        state, sells = self.run_with([lot("LQD", 4, stop_id="s1", stop_price=103.74, entry=106.14, high=106.95)],
                                     positions={"LQD": pos(4.711)},
                                     open_orders=[order("s1", "LQD", 4)], price=105.0)
        self.assertEqual(sells, [])
        self.assertEqual(len(state["lots"]), 1)


if __name__ == "__main__":
    unittest.main()
