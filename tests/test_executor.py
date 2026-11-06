"""Executor sizing, PAPER simulation, and the LIVE guard.

Nothing in this module is an order. The tests check the guard and that a
LIVE send does not open a position.
"""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
import links
from agents.executor import Executor, allow_live, set_mode, size_ticket, startup_warning
from bus import Desk
from notifier import Notifier
from tests.support import temp_desk_env

MINT = "4FHEBKNYy6BZ1QFMKj1xL8HDTM12rdXLRYw8DgiCUnQn"


class TestExecutor(unittest.TestCase):
    def test_sizing(self):
        size, skip = size_ticket(200, 8, 47.50, "bump", 100000, 2)
        self.assertIsNone(skip)
        self.assertEqual(size, 47.50)
        size, skip = size_ticket(200, 8, 47.50, "skip", 100000, 2)
        self.assertIsNone(size)
        self.assertEqual(skip, "skip")
        self.assertEqual(
            startup_warning(200, 8, 47.50, "skip"),
            "every coin will be skipped",
        )
        self.assertIsNone(startup_warning(200, 8, 47.50, "bump"))
        capped, skip = size_ticket(100000, 8, 47.50, "bump", 40000, 2)
        self.assertIsNone(skip)
        self.assertEqual(capped, 800)

    def test_paper_only_and_live_guard(self):
        tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        db.init_db()
        source = Path(__file__).resolve().parents[1].joinpath("agents", "executor.py").read_text()
        self.assertNotIn("keypair", source)
        self.assertNotIn("sign_transaction", source)
        self.assertNotIn("send_transaction", source)

        self.assertFalse(set_mode("LIVE", typed="LIVE"))
        self.assertFalse(allow_live("LIVE"))
        self.assertEqual(db.get_setting("mode"), "PAPER")

        notifier = Notifier(mode="PAPER")
        executor = Executor(Desk(http=None, notifier=notifier))
        result = asyncio.run(executor.handle({
            "mint": MINT, "symbol": "LABS", "price": 0.00003, "liquidity_usd": 50000,
        }))
        self.assertEqual(result["mode"], "PAPER")
        self.assertEqual(result["size_usd"], 47.50)
        self.assertEqual(result["link"], links.coin_url(MINT, "solana"))
        self.assertLess(result["stop_price"], 0.00003)
        positions = db.query("SELECT mode FROM positions")
        self.assertEqual([row["mode"] for row in positions], ["PAPER"])
        self.assertEqual(notifier.pushes[0]["url"], links.coin_url(MINT, "solana"))
        self.assertEqual(notifier.pushes[0]["buttons"][0]["label"], "Buy on FOMO")

        db.execute(
            "UPDATE positions SET closed_at = 1, exit_reason = 'stop' WHERE mode = 'PAPER'"
        )
        self.assertTrue(set_mode("LIVE", typed="LIVE"))
        self.assertFalse(set_mode("LIVE", typed="live"))
        db.set_setting("mode", "LIVE", "test")
        live_notifier = Notifier(mode="LIVE")
        live = Executor(Desk(http=None, notifier=live_notifier))
        before = db.query("SELECT COUNT(*) AS n FROM positions")[0]["n"]
        live_result = asyncio.run(live.handle({
            "mint": "OtherMint111", "symbol": "NEW", "price": 1, "liquidity_usd": 50000,
        }))
        self.assertEqual(live_result["status"], "waiting")
        self.assertEqual(live_result["mode"], "LIVE")
        after = db.query("SELECT COUNT(*) AS n FROM positions")[0]["n"]
        self.assertEqual(after, before)
        self.assertEqual(live_notifier.pushes[0]["url"], links.coin_url("OtherMint111", "solana"))
        self.assertFalse(live_notifier.pushes[0]["title"].startswith("PAPER"))
