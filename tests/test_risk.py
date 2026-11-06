"""Risk: stop, trail, timeout, no-price, pulled liquidity, gap exit."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
import links
from agents.risk import Risk, hard_stop, trail_line
from bus import Desk
from notifier import Notifier
from tests.support import temp_desk_env

MINT = "MintRisk111"


class TestRisk(unittest.TestCase):
    def setUp(self):
        tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        db.init_db()
        db.execute(
            "INSERT INTO coins (mint, symbol, name) VALUES (?, ?, ?)",
            (MINT, "RISK", "RiskCoin"),
        )
        self.notifier = Notifier(mode="PAPER")
        self.risk = Risk(Desk(http=None, notifier=self.notifier))

    def _open(self, entry=1.0, stop_pct=6, opened_at=1_000.0):
        stop = hard_stop(entry, stop_pct)
        db.execute(
            """
            INSERT INTO positions (
                mint, opened_at, entry_price, size_usd, peak_price, stop_price, trail_active, mode
            ) VALUES (?, ?, ?, ?, ?, ?, 0, 'PAPER')
            """,
            (MINT, opened_at, entry, 100, entry, stop),
        )
        return db.query("SELECT * FROM positions WHERE mint = ?", (MINT,))[0]

    def test_stop_is_below_entry_and_gap_uses_the_polled_price(self):
        self.assertLess(hard_stop(1, 6), 1)
        row = dict(self._open())
        result = self.risk.poll_position(row, now=1_100, price=0.70, liquidity=50_000)
        self.assertEqual(result["action"], "stop")
        self.assertEqual(result["exit_price"], 0.70)
        self.assertNotEqual(result["exit_price"], hard_stop(1, 6))
        self.assertIn("hit the -6% stop", self.notifier.pushes[-1]["title"])
        self.assertEqual(self.notifier.pushes[-1]["url"], links.coin_url(MINT, "solana"))

    def test_trail_arms_only_moves_up_and_never_below_entry(self):
        self.assertIsNone(trail_line(1, 1.10, 25, 13, None))
        armed = trail_line(1, 1.25, 25, 13, None)
        self.assertAlmostEqual(armed, 1.25 * 0.87)
        self.assertGreaterEqual(armed, 1)
        higher = trail_line(1, 1.40, 25, 13, armed)
        self.assertGreater(higher, armed)
        loosened = trail_line(1, 1.40, 25, 90, higher)
        self.assertEqual(loosened, higher)
        clamped = trail_line(1, 1.25, 25, 90, None)
        self.assertGreaterEqual(clamped, 1)

        row = dict(self._open())
        hold = self.risk.poll_position(row, now=1_100, price=1.25, liquidity=50_000)
        self.assertEqual(hold["action"], "hold")
        row = dict(db.query("SELECT * FROM positions WHERE mint = ?", (MINT,))[0])
        exited = self.risk.poll_position(row, now=1_200, price=1.25 * 0.87, liquidity=50_000)
        self.assertEqual(exited["action"], "trail")
        self.assertEqual(exited["exit_price"], 1.25 * 0.87)

    def test_looser_stop_does_not_move_an_open_stop(self):
        row = dict(self._open())
        db.set_setting("stop_pct", "8", "test")
        held = self.risk.poll_position(row, now=1_100, price=0.95, liquidity=50_000)
        self.assertEqual(held["action"], "hold")
        self.assertAlmostEqual(held["stop_pct"], 6, places=4)
        fresh = Risk(Desk(http=None, notifier=Notifier(mode="PAPER")))
        db.set_setting("stop_pct", "4", "test")
        row = dict(db.query("SELECT * FROM positions WHERE mint = ?", (MINT,))[0])
        # The position was opened at -6%. Tightening the setting to -4% may lift it.
        # A second coin opened after the change carries the new stop; this one
        # already locked 6% and 0.95 is still above both 0.94 and a tightened 0.96? 
        # 0.95 is below a 4% stop (0.96), so the tighter setting exits it.
        tightened = fresh.poll_position(row, now=1_300, price=0.95, liquidity=50_000)
        self.assertEqual(tightened["action"], "stop")
        self.assertAlmostEqual(tightened["stop_pct"], 4, places=4)

    def test_timeout_no_price_and_pulled_liquidity(self):
        row = dict(self._open(opened_at=0))
        timeout = self.risk.poll_position(row, now=3 * 3600, price=1.04, liquidity=50_000)
        self.assertEqual(timeout["action"], "timeout")
        self.assertEqual(timeout["exit_price"], 1.04)
        self.assertIn("dead money", self.notifier.pushes[-1]["title"])

        db.execute("DELETE FROM positions")
        self.risk.state.clear()
        self.notifier.pushes.clear()
        row = dict(self._open())
        for _ in range(3):
            missed = self.risk.poll_position(row, now=2_000, price=None, liquidity=50_000)
            self.assertEqual(missed["action"], "no-price")
        self.assertEqual(self.notifier.pushes, [])
        fourth = self.risk.poll_position(row, now=2_100, price=None, liquidity=50_000)
        self.assertEqual(fourth["miss"], 4)
        self.assertIn("no price for 1 min", self.notifier.pushes[-1]["title"])
        self.assertEqual(self.notifier.pushes[-1]["buttons"][0]["url"], links.coin_url(MINT, "solana"))

        db.execute("DELETE FROM positions")
        self.risk.state.clear()
        self.notifier.pushes.clear()
        row = dict(self._open())
        self.risk.poll_position(row, now=3_000, price=1.1, liquidity=50_000)
        row = dict(db.query("SELECT * FROM positions WHERE mint = ?", (MINT,))[0])
        pulled = self.risk.poll_position(row, now=3_100, price=0.2, liquidity=0)
        self.assertEqual(pulled["action"], "pulled")
        self.assertEqual(pulled["exit_price"], 1.1)
