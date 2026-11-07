"""STEP 10 settings. Temp databases only. No network and no live desk."""
import math
import os
import unittest

from fastapi.testclient import TestClient

import commands
import db
from agents.risk import Risk
from bus import Desk
from notifier import Notifier
from tests.support import temp_desk_env

TOKEN = "dummy-app-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
MINT = "MintSet111111111111111111111111111111"


class TestSettings(unittest.TestCase):
    def setUp(self):
        self.tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        os.environ["APP_TOKEN"] = TOKEN
        os.environ.pop("DESK_AGENTS", None)
        self.addCleanup(lambda: os.environ.pop("APP_TOKEN", None))
        db.init_db()

    def test_defaults_bounds_and_labels(self):
        from config import SETTING_COPY, SETTING_GROUPS, SETTING_RULES, SETTINGS_DEFAULTS
        self.assertEqual(db.get_setting("enabled"), "true")
        self.assertEqual(db.get_setting("analyst_mode"), "jev")
        self.assertEqual(db.get_setting("notifier"), "webpush")
        self.assertEqual(db.get_setting("agents_enabled"), "0")
        self.assertEqual(db.get_setting("paper_start_stack"), "200")
        self.assertIsNone(db.get_setting("stack"))
        for key, value in SETTINGS_DEFAULTS.items():
            if key == "agents_enabled":
                continue
            self.assertEqual(db.get_setting(key), value, key)
        import server
        client = TestClient(server.app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        body = client.get("/api/settings", headers=AUTH).json()
        self.assertNotIn("agents_enabled", str(body["groups"].keys()))
        flat = [item["key"] for group in body["groups"].values() for item in group]
        self.assertNotIn("agents_enabled", flat)
        self.assertIn("max_top10_pct", [item["key"] for item in body["groups"]["auditor"]])
        shared = next(item for item in body["groups"]["scanner"] if item["key"] == "max_top10_pct")
        self.assertIn("Shared", shared["note"])
        self.assertEqual(shared["min"], SETTING_RULES["max_top10_pct"][1])
        self.assertEqual(shared["max"], SETTING_RULES["max_top10_pct"][2])
        risk_poll = next(item for item in body["groups"]["risk"] if item["key"] == "risk.poll_s")
        self.assertEqual(risk_poll["label"], "Risk poll")
        self.assertIn("ticket", body)
        self.assertEqual(body["ticket"], "tickets bumped to $47.50 = 23.75% of stack")
        self.assertTrue(all(key in SETTING_COPY for keys in SETTING_GROUPS.values() for key in keys))
        refused = client.post("/api/settings", headers=AUTH, json={"key": "agents_enabled", "value": "1"})
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(db.get_setting("agents_enabled"), "0")
        mode = client.post("/api/settings", headers=AUTH, json={"key": "mode", "value": "LIVE"})
        self.assertEqual(mode.status_code, 400)
        self.assertEqual(db.get_setting("mode"), "PAPER")

    def test_rejects_non_finite_numbers_on_both_paths(self):
        import server
        client = TestClient(server.app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        for value in ("nan", "inf", "-inf", True):
            response = client.post("/api/settings", headers=AUTH, json={"key": "stop_pct", "value": value})
            self.assertEqual(response.status_code, 400, value)
            self.assertIn("0.1", response.json()["detail"])
            self.assertIn("100", response.json()["detail"])
            self.assertNotIn(TOKEN, response.text)
        self.assertEqual(db.get_setting("stop_pct"), "6")
        for text in ("set stop to nan", "set stop to inf", "set stop to -inf"):
            reply = commands.run(text)
            self.assertTrue(reply["refused"])
            self.assertIn("0.1", reply["text"])
        self.assertEqual(db.get_setting("stop_pct"), "6")
        self.assertTrue(math.isfinite(float(db.get_setting("stop_pct"))))

    def test_ticket_skip_names_the_choice_and_live_stack_is_separate(self):
        from agents.executor import ticket_warning
        self.assertEqual(
            ticket_warning(200, 8, 47.50, "bump"),
            "tickets bumped to $47.50 = 23.75% of stack",
        )
        skip = ticket_warning(200, 8, 47.50, "skip")
        self.assertIn("every coin will be skipped, stack under $593.75", skip)
        self.assertIn("bump", skip)
        self.assertIn("raise the stack", skip)
        reply = commands.run("set below min to skip")
        self.assertIn("every coin will be skipped, stack under $593.75", reply["text"])
        self.assertEqual(db.get_setting("below_min"), "skip")
        log = db.query('SELECT who, "new" FROM change_log WHERE key = ?', ("below_min",))[-1]
        self.assertEqual(log["who"], "command")
        self.assertEqual(log["new"], "skip")
        live = commands.run("stack 200")
        self.assertIn("LIVE stack:", live["text"])
        self.assertIn("every coin will be skipped", live["text"])
        self.assertEqual(db.get_setting("paper_start_stack"), "200")
        self.assertEqual(db.get_setting("stack"), "200")
        self.assertEqual(db.get_setting("mode"), "PAPER")

    def test_migration_preserves_owner_rows(self):
        db.set_setting("stop_pct", "9", "owner")
        db.set_setting("notifier", "telegram", "owner")
        db.execute(
            "INSERT INTO push_subs (endpoint, p256dh, auth, created_at) VALUES (?, ?, ?, ?)",
            ("https://push.example/kept", "pub", "auth", 1),
        )
        conn = db.connect()
        try:
            conn.execute("PRAGMA user_version=2")
            conn.commit()
        finally:
            conn.close()
        db.init_db()
        self.assertEqual(db.get_setting("stop_pct"), "9")
        self.assertEqual(db.get_setting("notifier"), "telegram")
        self.assertEqual(db.get_setting("analyst_mode"), "jev")
        self.assertEqual(db.get_setting("agents_enabled"), "0")
        self.assertEqual(db.query("SELECT endpoint FROM push_subs")[0]["endpoint"], "https://push.example/kept")
        conn = db.connect()
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)
            columns = [row[1] for row in conn.execute("PRAGMA table_info(positions)")]
            self.assertIn("trail_giveback_pct", columns)
        finally:
            conn.close()

    def test_untouched_telegram_default_becomes_webpush(self):
        conn = db.connect()
        try:
            conn.execute("UPDATE settings SET value = 'telegram' WHERE key = 'notifier'")
            conn.execute("DELETE FROM change_log WHERE key = 'notifier'")
            conn.execute("PRAGMA user_version=2")
            conn.commit()
        finally:
            conn.close()
        db.init_db()
        self.assertEqual(db.get_setting("notifier"), "webpush")
        self.assertEqual(db.get_setting("agents_enabled"), "0")

    def test_armed_trail_does_not_loosen_and_resend_keeps_stored_text(self):
        db.execute(
            """
            INSERT INTO positions (
                mint, opened_at, entry_price, size_usd, peak_price, stop_price, trail_active, mode
            ) VALUES (?, 0, 1, 10, 1, 0.94, 0, 'PAPER')
            """,
            (MINT,),
        )
        risk = Risk(Desk(http=None, notifier=Notifier(mode="PAPER")))
        row = dict(db.query("SELECT * FROM positions")[0])
        risk.poll_position(row, now=10, price=1.25, liquidity=50_000)
        stored = db.query("SELECT trail_active, trail_giveback_pct, stop_price FROM positions")[0]
        self.assertEqual(stored["trail_active"], 1)
        self.assertAlmostEqual(stored["trail_giveback_pct"], 12)
        db.set_setting("trail_pct", "20", "owner")
        again = Risk(Desk(http=None, notifier=Notifier(mode="PAPER")))
        row = dict(db.query("SELECT * FROM positions")[0])
        again.poll_position(row, now=20, price=1.20, liquidity=50_000)
        kept = db.query("SELECT trail_giveback_pct, trail_active, stop_price FROM positions")[0]
        self.assertAlmostEqual(kept["trail_giveback_pct"], 12)
        self.assertEqual(kept["trail_active"], 1)
        self.assertAlmostEqual(kept["stop_price"], 0.94)
        db.set_setting("trail_pct", "8", "owner")
        tight = Risk(Desk(http=None, notifier=Notifier(mode="PAPER")))
        row = dict(db.query("SELECT * FROM positions")[0])
        tight.poll_position(row, now=30, price=1.20, liquidity=50_000)
        self.assertAlmostEqual(db.query("SELECT trail_giveback_pct FROM positions")[0]["trail_giveback_pct"], 8)
        import links
        url = links.coin_url(MINT, "solana")
        db.execute(
            """
            INSERT INTO coins (mint, name, symbol) VALUES (?, 'Alpha', 'ALP')
            """,
            (MINT,),
        )
        db.execute(
            """
            INSERT INTO signals (
                mint, ts, price, size_usd, stack_pct, link, mode, status, push_title, push_body
            ) VALUES (?, ?, 1, 47.5, 8, ?, 'PAPER', 'bought', ?, ?)
            """,
            (MINT, __import__("time").time(), url, "Buy ALP · $47.50 · 8.00% of stack", "Auditor 8/8 · stop -6% · trail 12%"),
        )
        db.set_setting("stop_pct", "9", "owner")
        reply = commands.run("buy ALP")
        self.assertIn("stop -6%", reply["text"])
        self.assertNotIn("stop -9%", reply["text"])
        self.assertEqual(reply["buttons"][0]["url"], url)
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM signals")[0]["n"], 1)


if __name__ == "__main__":
    unittest.main()
