"""STEP 9 commands. Temp database, no network, no push delivery."""
import os
import unittest

from fastapi.testclient import TestClient

import api
import commands
import db
import links
from tests.support import temp_desk_env

TOKEN = "dummy-app-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
MINT = "MintCmd111111111111111111111111111111"
OTHER = "MintCmd222222222222222222222222222222"


def _layers(passed=False):
    names = [
        "Mint authority", "Freeze authority", "Dev wallet", "Top 10 holders",
        "Liquidity", "Pool age", "Bundled wallets", "Delegations",
    ]
    layers = []
    for number, name in enumerate(names, 1):
        bad = number == 3 and not passed
        layers.append({
            "n": number,
            "name": name,
            "pass": not bad,
            "value": "dev holds 31%" if bad else "Revoked",
            "source": "rpc",
        })
    return layers


class TestCommands(unittest.TestCase):
    def setUp(self):
        self.tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        self.previous_private = os.environ.get("VAPID_PRIVATE_KEY")
        self.previous_token = os.environ.get("APP_TOKEN")
        self.previous_tg = os.environ.get("TG_TOKEN")
        os.environ.pop("VAPID_PRIVATE_KEY", None)
        os.environ.pop("TG_TOKEN", None)
        os.environ.pop("DESK_AGENTS", None)
        os.environ["APP_TOKEN"] = TOKEN
        self.addCleanup(self._restore_env)
        db.init_db()
        api._paused.clear()
        self.addCleanup(api._paused.clear)
        self.audited = []
        original = commands.audit_only

        def boom(mint):
            raise AssertionError(mint)

        commands.audit_only = boom
        self.addCleanup(lambda: setattr(commands, "audit_only", original))

    def _restore_env(self):
        self._put("VAPID_PRIVATE_KEY", self.previous_private)
        self._put("APP_TOKEN", self.previous_token)
        self._put("TG_TOKEN", self.previous_tg)

    def _put(self, key, value):
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    def _coin(self, mint, name, symbol):
        db.execute(
            "INSERT INTO coins (mint, name, symbol) VALUES (?, ?, ?)",
            (mint, name, symbol),
        )

    def test_help_invite_report_and_status(self):
        self.assertIn("buy X", commands.run("nope")["text"])
        self.assertEqual(commands.run("/invite")["text"], links.SIGNUP_URL)
        report = commands.run("report")
        self.assertEqual(report["text"], "No morning page has been written.")
        self.assertNotIn("http", report["text"])
        status = commands.run("status")
        self.assertIn("PAPER", status["text"])
        self.assertIn("Balance $200.00", status["text"])
        self.assertIn("No open position.", status["text"])
        self.assertEqual(status["coins"], [])

    def test_unknown_name_and_two_matches(self):
        unknown = commands.run("buy nope")
        self.assertIn("Send the mint.", unknown["text"])
        self.assertEqual(unknown["coins"], [])
        self.assertEqual(self.audited, [])
        self._coin(MINT, "Alpha", "DUP")
        self._coin(OTHER, "Beta", "DUP")
        choice = commands.run("why DUP")
        self.assertIn("Which coin?", choice["text"])
        self.assertEqual({item["mint"] for item in choice["coins"]}, {MINT, OTHER})
        for item in choice["coins"]:
            self.assertEqual(item["coin_url"], links.coin_url(item["mint"], "solana"))
            self.assertIn(item["coin_url"], choice["text"])
        self.assertEqual(db.query("SELECT id FROM signals"), [])

    def test_resend_is_not_a_new_trade(self):
        self._coin(MINT, "Alpha", "ALP")
        url = links.coin_url(MINT, "solana")
        now = __import__("time").time()
        db.execute(
            """
            INSERT INTO signals (mint, ts, price, size_usd, stack_pct, link, mode, status)
            VALUES (?, ?, 1, 47.5, 8, ?, 'PAPER', 'bought')
            """,
            (MINT, now, url),
        )
        before = db.query("SELECT COUNT(*) AS n FROM signals")[0]["n"]
        reply = commands.run("/buy ALP")
        self.assertTrue(reply["text"].startswith("PAPER · Buy ALP"))
        self.assertIn("Original push text was not stored.", reply["text"])
        self.assertNotIn("stop -", reply["text"])
        self.assertIn(url, reply["text"])
        self.assertEqual(reply["buttons"], [{"label": "Buy on FOMO", "url": url}])
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM signals")[0]["n"], before)
        self.assertEqual(db.query("SELECT id FROM positions"), [])
        self.assertEqual(db.query("SELECT id FROM activity"), [])
        self.assertEqual(self.audited, [])

    def test_old_push_is_audited_instead_of_resent(self):
        from api import _night_start
        self._coin(MINT, "Alpha", "ALP")
        db.execute(
            """
            INSERT INTO signals (mint, ts, price, size_usd, stack_pct, link, mode, status)
            VALUES (?, ?, 1, 10, 8, ?, 'PAPER', 'bought')
            """,
            (MINT, _night_start() - 10, links.coin_url(MINT, "solana")),
        )

        def fake(mint):
            self.audited.append(mint)
            return {"passed": True, "layers": _layers(True)}

        commands.audit_only = fake
        reply = commands.run("buy ALP")
        self.assertEqual(self.audited, [MINT])
        self.assertIn("not sized, not on the desk", reply["text"])
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM signals")[0]["n"], 1)
        self.assertEqual(db.query("SELECT id FROM positions"), [])

    def test_never_pushed_audits_without_execution(self):
        def fake(mint):
            self.audited.append(mint)
            return {"passed": False, "reason": "dev holds 31%", "layers": _layers(False)}

        commands.audit_only = fake
        reply = commands.run("buy " + MINT)
        self.assertEqual(self.audited, [MINT])
        self.assertIn("not sized, not on the desk", reply["text"])
        self.assertIn("dev holds 31%", reply["text"])
        self.assertIn(links.coin_url(MINT, "solana"), reply["text"])
        self.assertEqual(reply["buttons"][0]["url"], links.coin_url(MINT, "solana"))
        self.assertEqual(db.query("SELECT id FROM signals"), [])
        self.assertEqual(db.query("SELECT id FROM positions"), [])
        self.assertEqual(len(db.query("SELECT id FROM audits")), 8)
        db.set_setting("enabled", "false", "test")

        def explode(mint):
            raise AssertionError(mint)

        commands.audit_only = explode
        off = commands.run("buy " + MINT)
        self.assertIn("Auditor is off.", off["text"])
        self.assertIn("not sized, not on the desk", off["text"])

    def test_sell_and_why(self):
        self._coin(MINT, "Alpha", "ALP")
        url = links.coin_url(MINT, "solana")
        closed = commands.run("sell ALP")
        self.assertIn("No open position", closed["text"])
        self.assertIn(url, closed["text"])
        self.assertEqual(closed["buttons"], [])
        now = __import__("time").time()
        db.execute(
            "INSERT INTO positions (mint, opened_at, entry_price, size_usd, mode) VALUES (?, ?, 1, 47.5, 'PAPER')",
            (MINT, now),
        )
        db.execute(
            "INSERT INTO scans (mint, ts, verdict, reason) VALUES (?, ?, 'kill', 'liq $1')",
            (MINT, now - 50),
        )
        db.execute(
            """
            INSERT INTO audits (mint, ts, layer, name, value_text, "pass", source)
            VALUES (?, ?, '3', 'Dev wallet', 'dev holds 31%', 0, 'rpc')
            """,
            (MINT, now - 10),
        )
        sell = commands.run("sell ALP")
        self.assertEqual(sell["buttons"], [{"label": "Sell on FOMO", "url": url}])
        self.assertIn(url, sell["text"])
        self.assertEqual(len(db.query("SELECT id FROM positions WHERE closed_at IS NULL")), 1)
        why = commands.run("why ALP")
        self.assertIn("Auditor stopped ALP", why["text"])
        self.assertIn("dev holds 31%", why["text"])
        self.assertIn(url, why["text"])

    def test_pause_settings_stack_and_live_lock(self):
        self._coin(MINT, "Alpha", "ALP")
        now = __import__("time").time()
        db.execute(
            "INSERT INTO positions (mint, opened_at, entry_price, size_usd, mode) VALUES (?, ?, 1, 10, 'PAPER')",
            (MINT, now),
        )
        blocked = commands.run("pause Risk")
        self.assertTrue(blocked["refused"])
        self.assertNotIn("risk", api._paused)
        paused = commands.run("/pause Scanner")
        self.assertEqual(paused["text"], "scanner paused")
        self.assertIn("scanner", api._paused)
        self.assertEqual(commands.run("resume scanner")["text"], "scanner resumed")
        self.assertNotIn("scanner", api._paused)
        changed = commands.run("set stop to -8%")
        self.assertEqual(changed["text"], "stop -6% -> -8%")
        self.assertEqual(db.get_setting("stop_pct"), "8")
        log = db.query('SELECT key, "old", "new", who FROM change_log')[-1]
        self.assertEqual(log["key"], "stop_pct")
        self.assertEqual(log["new"], "8")
        self.assertEqual(log["who"], "command")
        refused = commands.run("set stop to 1000")
        self.assertTrue(refused["refused"])
        self.assertIn("0.1", refused["text"])
        self.assertEqual(db.get_setting("stop_pct"), "8")
        self.assertEqual(commands.run("set mode to rules")["text"], "analyst mode jev -> rules")
        self.assertEqual(db.get_setting("analyst_mode"), "rules")
        stacked = commands.run("stack 1240")
        self.assertIn("1240", stacked["text"])
        self.assertEqual(db.get_setting("stack"), "1240")
        self.assertEqual(db.get_setting("mode"), "PAPER")
        self.assertTrue(commands.run("live")["refused"])
        locked = commands.run("live LIVE")
        self.assertIn("LIVE locked", locked["text"])
        self.assertEqual(db.get_setting("mode"), "PAPER")
        self.assertEqual(commands.run("paper")["text"], "mode PAPER")
        self.assertEqual(db.get_setting("agents_enabled"), "0")

    def test_api_auth_and_safe_replies(self):
        import server
        client = TestClient(server.app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        self.assertEqual(client.post("/api/command", json={"text": "status"}).status_code, 401)
        leaked = client.post("/api/command", headers=AUTH, params={"token": TOKEN}, json={"text": "status"})
        self.assertEqual(leaked.status_code, 401)
        self.assertNotIn(TOKEN, leaked.text)
        ok = client.post("/api/command", headers=AUTH, json={"text": "help"})
        self.assertEqual(ok.status_code, 200)
        self.assertIn("buy X", ok.json()["text"])
        self.assertNotIn(TOKEN, ok.text)
        self.assertEqual(client.post("/api/command", headers=AUTH, json={}).status_code, 400)
        health = client.get("/health")
        self.assertEqual(health.json()["step"], 10)
        self.assertFalse(health.json()["agents_enabled"])
