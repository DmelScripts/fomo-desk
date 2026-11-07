"""STEP 11 paper cash. Temp database, no network and no push delivery."""
import asyncio
import os
import threading
import unittest

from fastapi.testclient import TestClient

import db
import fees
from agents.executor import Executor, empty_night_reason, paper_books, paper_night_done, paper_night_status
from agents.risk import Risk
from bus import Desk
from notifier import Notifier
from tests.support import temp_desk_env

MINT = "MintPaper11111111111111111111111111111"
OTHER = "MintPaper22222222222222222222222222222"
TOKEN = "dummy-app-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class TestPaper(unittest.TestCase):
    def setUp(self):
        self.tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        os.environ["APP_TOKEN"] = TOKEN
        os.environ.pop("DESK_AGENTS", None)
        os.environ.pop("VAPID_PRIVATE_KEY", None)
        self.addCleanup(lambda: os.environ.pop("APP_TOKEN", None))
        db.init_db()

    def _desk(self):
        return Executor(Desk(http=None, notifier=Notifier(mode="PAPER"), now=lambda: 1_000.0))

    def _fill(self, executor, mint, price=1.0, liquidity=100000):
        return asyncio.run(executor.handle({
            "mint": mint, "symbol": "PAP", "price": price, "liquidity_usd": liquidity, "score": 80,
        }))

    def test_offline_round_trip_uses_both_notionals(self):
        # OFFLINE example: $47.50 in, price doubles, exit notional $95.
        entry_fee = fees.fee_usd(47.50, 0.10)
        exit_fee = fees.fee_usd(95, 0.10)
        net, total = fees.pnl_usd(47.50, 1.0, 2.0, 0.10)
        self.assertAlmostEqual(entry_fee, 0.855)
        self.assertAlmostEqual(exit_fee, 0.855)
        self.assertAlmostEqual(total, 1.71)
        self.assertAlmostEqual(net, 47.50 - 1.71)

    def test_fill_reserves_cash_and_a_retry_does_not_fill_again(self):
        first = self._fill(self._desk(), MINT)
        self.assertEqual(first["status"], "bought")
        books = paper_books()
        entry_fee = fees.fee_usd(47.50)
        self.assertAlmostEqual(books["cash"], 200 - 47.50 - entry_fee)
        self.assertIsNone(books["unrealized"])
        again = self._fill(self._desk(), MINT)
        self.assertEqual(again["reason"], "already sent")
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM positions")[0]["n"], 1)
        restart = self._fill(Executor(Desk(http=None, notifier=Notifier(mode="PAPER"), now=lambda: 2_000.0)), MINT)
        self.assertEqual(restart["reason"], "already sent")
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM positions")[0]["n"], 1)

    def test_concurrent_fills_cannot_overspend(self):
        db.set_setting("stack_pct", "80", "test")
        results = []

        def run(mint):
            executor = Executor(Desk(http=None, notifier=Notifier(mode="PAPER"), now=lambda: 1_000.0))
            results.append(asyncio.run(executor.handle({
                "mint": mint, "symbol": mint[-3:], "price": 1, "liquidity_usd": 100000,
            })))

        threads = [threading.Thread(target=run, args=(mint,)) for mint in (MINT, OTHER)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        opened = [row for row in results if row.get("status") == "bought"]
        refused = [row for row in results if row.get("reason") == "no free stack"]
        self.assertEqual(len(opened), 1)
        self.assertEqual(len(refused), 1)
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM positions")[0]["n"], 1)
        self.assertGreater(paper_books()["cash"], -1e-6)

    def test_skip_configuration_does_not_open_a_trade(self):
        db.set_setting("below_min", "skip", "test")
        reason = empty_night_reason("PAPER")
        self.assertIn("every coin will be skipped, stack under $593.75", reason)
        result = self._fill(self._desk(), MINT)
        self.assertEqual(result["status"], "skipped")
        self.assertIn("every coin will be skipped", result["reason"])
        self.assertEqual(db.query("SELECT id FROM positions"), [])
        self.assertEqual(db.query("SELECT id FROM signals"), [])

    def test_exit_uses_the_polled_price_and_both_fees(self):
        self._fill(self._desk(), MINT, price=1)
        db.execute(
            "INSERT INTO prices (mint, ts, price, volume_m5) VALUES (?, 1000, 2, NULL)",
            (MINT,),
        )
        marked = paper_books()
        self.assertAlmostEqual(marked["unrealized"], fees.pnl_usd(47.50, 1, 2)[0])
        self.assertIsNotNone(marked["unrealized"])
        risk = Risk(Desk(http=None, notifier=Notifier(mode="PAPER"), now=lambda: 2_000.0))
        row = dict(db.query("SELECT * FROM positions")[0])
        exited = risk.poll_position(row, now=2_000, price=0.90, liquidity=50_000)
        self.assertEqual(exited["action"], "stop")
        self.assertEqual(exited["exit_price"], 0.90)
        stored = db.query("SELECT pnl_usd, fees_usd, exit_price FROM positions")[0]
        net, total = fees.pnl_usd(47.50, 1, 0.90)
        entry_fee = fees.fee_usd(fees.exit_notional(47.50, 1, 1))
        exit_fee = fees.fee_usd(fees.exit_notional(47.50, 1, 0.90))
        self.assertAlmostEqual(total, entry_fee + exit_fee)
        self.assertAlmostEqual(stored["pnl_usd"], net)
        self.assertAlmostEqual(stored["fees_usd"], total)
        self.assertAlmostEqual(paper_books()["cash"], 200 + net)
        self.assertAlmostEqual(paper_books()["realized"], net)

    def test_live_rows_do_not_change_paper_cash_and_night_stays_shut(self):
        db.execute(
            """
            INSERT INTO positions (
                mint, opened_at, entry_price, size_usd, mode, entry_fee_usd
            ) VALUES (?, 1, 1, 80, 'LIVE', 0.72)
            """,
            (OTHER,),
        )
        self.assertAlmostEqual(paper_books()["cash"], 200)
        self.assertFalse(paper_night_done())
        db.execute(
            """
            UPDATE positions SET closed_at = 2, exit_price = 1, exit_reason = 'stop', pnl_usd = -1
            WHERE mint = ?
            """,
            (OTHER,),
        )
        self.assertAlmostEqual(paper_books()["realized"], 0)
        self.assertFalse(paper_night_status()["done"])
        import server
        client = TestClient(server.app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        state = client.get("/api/state", headers=AUTH).json()
        self.assertFalse(state["paper_night"]["done"])
        self.assertIn("not defined", state["paper_night"]["reason"])
        self.assertEqual(state["mode"], "PAPER")
        self.assertFalse(client.get("/health").json()["agents_enabled"])
        self.assertEqual(client.get("/health").json()["step"], 12)
