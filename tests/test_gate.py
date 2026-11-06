"""Analyst gate: thresholds, score formula, and the rules-mode stand-in."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
from agents.analyst import Analyst, gate, rules_scores
from bus import Desk
from notifier import Notifier
from tests.support import temp_desk_env

PASSING = {"alive_6h": 0.70, "organic": 0.65, "flush": 0.75, "chase": 0.30}


def _pair(h1_volume=7000, buys=65, sells=35, m5=-15, h1=30):
    return {
        "volume": {"h1": h1_volume, "m5": 1},
        "txns": {"h1": {"buys": buys, "sells": sells}, "m5": {"buys": 1, "sells": 1}},
        "priceChange": {"m5": m5, "h1": h1},
    }


class TestGate(unittest.TestCase):
    def test_each_threshold_rejects_and_the_line_passes(self):
        self.assertFalse(gate({**PASSING, "alive_6h": 0.69})["passed"])
        self.assertFalse(gate({**PASSING, "organic": 0.64})["passed"])
        self.assertFalse(gate({**PASSING, "chase": 0.31})["passed"])
        score_69 = gate({**PASSING, "flush": 0.71})
        self.assertEqual(score_69["score"], 69)
        self.assertFalse(score_69["passed"])
        self.assertEqual(score_69["failed"], ["score 69"])

        passed = gate(PASSING)
        self.assertTrue(passed["passed"])
        self.assertEqual(passed["score"], 70)
        self.assertEqual(passed["failed"], [])
        self.assertIn("alive_6h 0.69", gate({**PASSING, "alive_6h": 0.69})["failed"])
        self.assertIn("organic 0.64", gate({**PASSING, "organic": 0.64})["failed"])
        self.assertIn("chase 0.31", gate({**PASSING, "chase": 0.31})["failed"])

    def test_rules_mode_uses_the_same_gate(self):
        self.assertTrue(gate(rules_scores(_pair()))["passed"])
        self.assertFalse(gate(rules_scores(_pair(h1_volume=6900)))["passed"])
        self.assertFalse(gate(rules_scores(_pair(buys=64, sells=36)))["passed"])
        self.assertFalse(gate(rules_scores(_pair(h1=31)))["passed"])
        scored = gate(rules_scores(_pair(m5=-14.2)))
        self.assertEqual(scored["score"], 69)
        self.assertFalse(scored["passed"])

        tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        db.init_db()
        db.set_setting("analyst_mode", "rules", "test")
        desk = Desk(http=None, notifier=Notifier(mode="PAPER"))
        result = self._run(Analyst(desk).judge({
            "mint": "MintRules111",
            "symbol": "RULE",
            "name": "Rule",
            "pair": _pair(h1_volume=6900),
            "layers": [],
            "age_min": 15,
        }))
        self.assertEqual(result["label"], "Analyst (rules)")
        self.assertFalse(result["passed"])
        rows = db.query("SELECT subtitle FROM activity")
        self.assertTrue(any("Analyst (rules)" in (row["subtitle"] or "") for row in rows))

    def _run(self, coro):
        import asyncio
        return asyncio.run(coro)

    def test_jev_body_and_input_tokens(self):
        tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        db.init_db()
        os.environ["TYPESAFE_API_KEY"] = "test-key-not-real"
        self.addCleanup(lambda: os.environ.pop("TYPESAFE_API_KEY", None))
        seen = {}

        class Http:
            async def post_json(self, url, body, headers=None):
                seen["url"] = url
                seen["body"] = body
                seen["headers"] = headers
                return {
                    "model": "jev-latest",
                    "usage": {"input_tokens": 42},
                    "answers": {name: {"noul": PASSING[name]} for name in PASSING},
                }

            async def get_json(self, url):
                raise AssertionError("rules/jev test must not GET")

        desk = Desk(http=Http(), notifier=Notifier(mode="PAPER"))
        result = self._run(Analyst(desk).judge({
            "mint": "MintJev111",
            "symbol": "JEV",
            "pair": _pair(),
            "layers": [],
            "age_min": 12,
        }))
        self.assertTrue(result["passed"])
        self.assertEqual(result["inputTokens"], 42)
        self.assertEqual(set(seen["body"]["questions"]), {"alive_6h", "organic", "flush", "chase"})
        self.assertNotIn("test-key-not-real", str(seen["body"]))
        stored = db.query("SELECT input_tokens, \"pass\" FROM verdicts")
        self.assertEqual(stored[0]["input_tokens"], 42)
        self.assertEqual(stored[0]["pass"], 1)
