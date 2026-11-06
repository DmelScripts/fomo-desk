"""Jev 429 holds the coin for up to 60s. Virtual clock, no network."""
import asyncio
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
from agents.analyst import JEV_QUEUE_S, Analyst
from bus import Desk
from notifier import Notifier
from tests.support import temp_desk_env

PASSING = {
    "alive_6h": {"noul": 0.80},
    "organic": {"noul": 0.80},
    "flush": {"noul": 0.80},
    "chase": {"noul": 0.10},
}


class TestJev429(unittest.TestCase):
    def setUp(self):
        tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        db.init_db()
        os.environ["TYPESAFE_API_KEY"] = "test-key-not-real"
        self.addCleanup(lambda: os.environ.pop("TYPESAFE_API_KEY", None))
        self.clock = {"t": 1_000.0}
        self.calls = 0

        async def sleep(seconds):
            self.clock["t"] += seconds

        self.sleep = sleep

    def _desk(self, http):
        return Desk(
            http,
            Notifier(mode="PAPER"),
            now=lambda: self.clock["t"],
            sleep=self.sleep,
        )

    def _payload(self):
        return {
            "mint": "Mint429",
            "symbol": "WAIT",
            "name": "Wait",
            "pair": {"volume": {"h1": 1, "m5": 1}, "txns": {"h1": {"buys": 1, "sells": 1}, "m5": {"buys": 1, "sells": 1}}, "priceChange": {"m5": 0, "h1": 0}},
            "layers": [],
            "age_min": 15,
            "price": 1,
        }

    def test_429_is_held_until_60s_then_rejected(self):
        class Http:
            def __init__(self, outer):
                self.outer = outer

            async def post_json(self, url, body, headers=None, timeout=None):
                self.outer.calls += 1
                self.assertNotIn = None
                return {"error": {"code": 429}}

        http = Http(self)
        result = asyncio.run(Analyst(self._desk(http)).judge(self._payload()))
        self.assertFalse(result["passed"])
        self.assertEqual(result["failed"], ["no answer"])
        self.assertGreaterEqual(self.clock["t"] - 1_000.0, JEV_QUEUE_S)
        self.assertGreater(self.calls, 1)
        self.assertNotIn("test-key-not-real", str(result))

    def test_429_that_clears_before_60s_is_scored(self):
        class Http:
            def __init__(self, outer):
                self.outer = outer

            async def post_json(self, url, body, headers=None, timeout=None):
                self.outer.calls += 1
                if self.outer.calls < 3:
                    return {"error": {"code": 429}}
                return {"model": "jev-latest", "usage": {"input_tokens": 3}, "answers": PASSING}

        result = asyncio.run(Analyst(self._desk(Http(self))).judge(self._payload()))
        self.assertTrue(result["passed"], result)
        self.assertLess(self.clock["t"] - 1_000.0, JEV_QUEUE_S)
        self.assertEqual(self.calls, 3)
        rows = db.query("SELECT subtitle FROM activity")
        self.assertFalse(any("no answer" in (row["subtitle"] or "") for row in rows))
