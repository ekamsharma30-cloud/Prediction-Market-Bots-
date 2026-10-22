"""Offline tests for the mention-market paper bot: fill engine, sizing caps, settlement.
No network is used. Run:  python3 -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "mentions_bot"))
import kalshi_mentions_bot as B  # noqa: E402

T0 = 1_760_000_000.0


def iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def trade(ts, yes_price, taker, count, tid=None):
    return {"created_time": iso(ts), "yes_price_dollars": "%.4f" % yes_price, "taker_side": taker,
            "count_fp": "%.2f" % count, "trade_id": tid or "t%s%s" % (ts, count)}


def order(side="no", L=0.90, qty=10, ahead=20):
    """In-memory order state as load_state() would build it."""
    return {"side": side, "limit_px": L, "qty": qty, "placed_ts": T0, "last_trade_ts": T0, "last_ids": [],
            "strict_ts": None, "touch_ts": None, "vol_at": 0.0, "book_checks": 0,
            "P": {"ahead": ahead, "behind": 0.0, "fill": 0.0, "ts": None, "full": None},
            "C": {"ahead": ahead, "behind": 0.0, "fill": 0.0, "ts": None, "full": None}}


def book_no(levels):
    """Order book with NO bids {price: size}."""
    return {"orderbook_fp": {"no_dollars": [["%.4f" % p, "%.2f" % s] for p, s in levels.items()], "yes_dollars": []}}


class FillEngine(unittest.TestCase):
    # we bid NO at 0.90 == offer YES at 0.10; YES buyers (taker 'yes') at 0.10 hit our level

    def test_queue_ahead_is_eaten_first(self):
        o = order(ahead=20)
        B.apply_trades(o, [trade(T0 + 10, 0.10, "yes", 15)])
        self.assertEqual(o["P"]["fill"], 0)
        self.assertEqual(o["P"]["ahead"], 5)
        self.assertIsNone(o["strict_ts"])
        self.assertIsNotNone(o["touch_ts"])          # TOUCH model counts it, QUEUE does not

    def test_partial_then_full_fill(self):
        o = order(ahead=5, qty=10)
        B.apply_trades(o, [trade(T0 + 10, 0.10, "yes", 9)])
        self.assertEqual(o["P"]["fill"], 4)          # 5 ahead eaten, 4 left over fill us
        self.assertIsNone(o["P"]["full"])
        B.apply_trades(o, [trade(T0 + 20, 0.10, "yes", 50)])
        self.assertEqual(o["P"]["fill"], 10)
        self.assertIsNotNone(o["P"]["full"])

    def test_trade_through_fills_everything(self):
        o = order(ahead=1000)
        B.apply_trades(o, [trade(T0 + 10, 0.12, "yes", 1)])   # printed above our 0.10 offer
        self.assertEqual(o["P"]["fill"], 10)
        self.assertEqual(o["C"]["fill"], 10)
        self.assertIsNotNone(o["strict_ts"])

    def test_wrong_side_and_old_trades_ignored(self):
        o = order(ahead=0)
        B.apply_trades(o, [trade(T0 - 10, 0.12, "yes", 50),   # before we were placed
                           trade(T0 + 10, 0.08, "yes", 50)])  # inside our price, not through
        self.assertEqual(o["P"]["fill"], 0)

    def test_cross_at_our_price_means_level_was_empty(self):
        # a YES seller printing at 0.10 means a YES bid sat at 0.10 while we offered 0.10:
        # that can only happen if our level had already cleared -> treated as a full fill
        o = order(ahead=50)
        B.apply_trades(o, [trade(T0 + 10, 0.10, "no", 1)])
        self.assertEqual(o["P"]["fill"], 10)
        self.assertIsNone(o["strict_ts"])

    def test_duplicate_trades_not_double_counted(self):
        o = order(ahead=0, qty=10)
        t = trade(T0 + 10, 0.10, "yes", 3, tid="same")
        B.apply_trades(o, [t])
        B.apply_trades(o, [t])
        self.assertEqual(o["P"]["fill"], 3)

    def test_upto_defers_later_trades(self):
        o = order(ahead=0, qty=10)
        B.apply_trades(o, [trade(T0 + 10, 0.10, "yes", 2), trade(T0 + 100, 0.10, "yes", 2)], upto=T0 + 50)
        self.assertEqual(o["P"]["fill"], 2)

    def test_book_growth_goes_behind_us(self):
        o = order(ahead=20)
        B.apply_book(o, book_no({0.90: 50}))
        self.assertEqual(o["P"]["ahead"], 20)
        self.assertEqual(o["P"]["behind"], 30)

    def test_cancels_split_pro_rata_vs_safe(self):
        o = order(ahead=20)
        o["P"]["behind"] = 20
        B.apply_book(o, book_no({0.90: 20}))          # level shrank 40 -> 20 with no trades
        self.assertAlmostEqual(o["P"]["ahead"], 10)   # realistic: half the cancels were ahead
        self.assertAlmostEqual(o["P"]["behind"], 10)
        self.assertEqual(o["C"]["ahead"], 20)         # safe model: still 20 ahead


class Money(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        B.DB_PATH = self.path
        B.log = lambda msg: None
        self.conn = B.db()

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    def add(self, ticker, event="EV1", L=0.90, **kw):
        base = dict(strategy="mentions", grp="other", series="KXTEST", event=event, ticker=ticker, label="w",
                    side="no", limit_px=L, qty=0, fee_rate=0.0, placed_ts=T0, close_ts=T0 + 3600,
                    yes_bid=0.08, yes_ask=round(1 - L, 4), entry_note="", q_ahead0=0)
        base.update(kw)
        return B.place(self.conn, **base)

    def test_quarter_kelly_size(self):
        qty, _ = B.size_order(self.conn, "mentions", 0.90)
        frac = min(B.MAX_FRAC, B.KELLY_MULT * B.EDGE["mentions"] / (1 - 0.90))
        self.assertEqual(qty, int(B.START_BANK * frac / 0.90))

    def test_event_cap(self):
        placed = sum(self.add("W%d" % i) for i in range(20))
        reserved = B.event_reserved(self.conn, "mentions", "EV1")
        self.assertLessEqual(reserved, B.START_BANK * B.MAX_EVENT_FRAC + 1e-6)
        self.assertLess(placed, 20)
        self.assertTrue(self.add("OTHER", event="EV2"))   # a different event still has room

    def test_settlement_pnl_and_bank(self):
        self.assertTrue(self.add("WIN"))
        self.assertTrue(self.add("LOSE", event="EV2"))
        q = self.conn.execute("SELECT qty FROM orders WHERE ticker='WIN'").fetchone()[0]
        self.conn.execute("UPDATE orders SET fill_q=qty, fill_qc=qty, strict_ts=?, touch_ts=?, status='done'", (T0, T0))
        self.conn.commit()
        for t, res in (("WIN", "no"), ("LOSE", "yes")):
            row = self.conn.execute("SELECT * FROM orders WHERE ticker=?", (t,)).fetchone()
            B.settle_order(self.conn, row, {"result": res, "status": "settled"}, T0 + 7200)
        r = dict(self.conn.execute("SELECT ticker, pnl_queue FROM orders").fetchall())
        self.assertAlmostEqual(r["WIN"], 0.10 * q, places=4)
        self.assertAlmostEqual(r["LOSE"], -0.90 * q, places=4)
        b = B.bank_state(self.conn)
        self.assertAlmostEqual(b["equity"], B.START_BANK + r["WIN"] + r["LOSE"], places=4)
        self.assertAlmostEqual(b["reserved"], 0.0)

    def test_unfilled_order_pays_nothing(self):
        self.assertTrue(self.add("NOFILL"))
        self.conn.execute("UPDATE orders SET status='done'")
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM orders").fetchone()
        B.settle_order(self.conn, row, {"result": "yes", "status": "settled"}, T0 + 7200)
        self.assertEqual(self.conn.execute("SELECT pnl_queue FROM orders").fetchone()[0], 0.0)

    def test_fee_rounds_up_to_cent(self):
        self.assertEqual(B.fee_dollars(0.0175, 10, 0.90), 0.02)
        self.assertEqual(B.fee_dollars(0.0, 10, 0.90), 0.0)

    def test_does_not_qualify_markets_skipped(self):
        m = {"ticker": "X", "status": "active", "yes_ask_dollars": "0.05", "yes_sub_title": "Event does not qualify"}
        ok = dict(m, ticker="Y", yes_sub_title="Tariff")
        with mock.patch.object(B, "get_book", lambda t: book_no({0.95: 10})), \
                mock.patch.object(B, "fee_rate", lambda s: 0.0):
            B.snapshot_event(self.conn, T0, "KXTEST", "EV9", [m, ok], 0)
        self.assertFalse(B.exists(self.conn, "mentions", "X"))
        self.assertTrue(B.exists(self.conn, "mentions", "Y"))

    def test_dashboard_state_is_json(self):
        import json
        json.dumps(B.mentions_state(self.conn))


if __name__ == "__main__":
    unittest.main()
