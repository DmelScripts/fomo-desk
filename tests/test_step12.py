"""STEP 12 health and night facts. Temp database. No network and no push."""
import asyncio
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

import api
import commands
import db
import links
from config import DESK_TZ
from health import STALE_S, Supervisor, diagnose
from report import gather, save_page, window
from tests.support import temp_desk_env

TOKEN = "dummy-app-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
RED = "MintRed1111111111111111111111111111111"
GREEN = "MintGreen111111111111111111111111111111"


def _at_vn_morning() -> float:
    return datetime(2026, 11, 7, 1, 0, tzinfo=timezone.utc).timestamp()


class TestStep12(unittest.TestCase):
    def setUp(self):
        self.tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        os.environ["APP_TOKEN"] = TOKEN
        os.environ.pop("DESK_AGENTS", None)
        os.environ.pop("VAPID_PRIVATE_KEY", None)
        self.addCleanup(lambda: os.environ.pop("APP_TOKEN", None))
        self.addCleanup(api._paused.clear)
        db.init_db()
        api._paused.clear()

    def test_off_and_paused_are_not_stale_targets(self):
        now = 1_000_000.0
        body = diagnose(now)
        scanner = next(item for item in body["agents"] if item["name"] == "scanner")
        self.assertEqual(scanner["intended"], "off")
        self.assertEqual(scanner["observed"], "missing")
        self.assertFalse(scanner["recoverable"])
        self.assertFalse(body["continuous"])
        self.assertIn("hourly", body["note"])
        db.set_setting("agents_enabled", "1", "test")
        api._paused.add("scanner")
        api._paused.add("risk")
        paused = diagnose(now)
        scanner = next(item for item in paused["agents"] if item["name"] == "scanner")
        self.assertEqual(scanner["intended"], "paused")
        self.assertFalse(scanner["recoverable"])
        db.beat("risk", now - STALE_S - 30)
        risk = next(item for item in diagnose(now)["agents"] if item["name"] == "risk")
        self.assertEqual(risk["intended"], "paused")
        self.assertEqual(risk["observed"], "stale")
        self.assertFalse(risk["recoverable"])

    def test_recovery_replaces_one_stale_task_and_refuses_to_enable(self):
        started = []

        async def factory():
            started.append(time_mark())
            await asyncio.Event().wait()

        def time_mark():
            return len(started) + 1

        async def scenario():
            supervisor = Supervisor()
            refused = await supervisor.recover("scanner", now=1_000_000.0)
            self.assertFalse(refused["restarted"])
            self.assertEqual(db.get_setting("agents_enabled"), "0")
            db.set_setting("agents_enabled", "1", "test")
            api._paused.add("auditor")
            supervisor.register("auditor", factory)
            held = await supervisor.recover("auditor", now=1_000_000.0)
            self.assertFalse(held["restarted"])
            self.assertIn("auditor", api._paused)
            self.assertEqual(started, [])
            supervisor.register("scanner", factory)
            db.beat("scanner", 1_000_000.0 - 300)
            first, second = await asyncio.gather(
                supervisor.recover("scanner", now=1_000_000.0),
                supervisor.recover("scanner", now=1_000_000.0),
            )
            restarted = [item for item in (first, second) if item["restarted"]]
            self.assertEqual(len(restarted), 1)
            self.assertEqual(len(started), 1)
            self.assertEqual(restarted[0]["push_text"], "Restarted scanner, down 5 min")
            self.assertFalse(restarted[0]["sent"])
            self.assertNotIn("scanner", api._paused)

        asyncio.run(scenario())
        self.assertEqual(db.get_setting("agents_enabled"), "1")
        db.set_setting("agents_enabled", "0", "test")

    def test_quiet_socket_is_not_a_reconnect(self):
        import asyncio
        from agents.scanner import migration_silence

        class Quiet:
            def __init__(self, ok):
                self.ok = ok
                self.pings = 0

            async def ping(self):
                self.pings += 1
                loop = asyncio.get_running_loop()
                future = loop.create_future()
                if self.ok:
                    future.set_result(0.01)
                else:
                    future.set_exception(ConnectionError("dead"))
                return future

        now = 3_000_000.0

        async def run():
            quiet = Quiet(True)
            self.assertEqual(await migration_silence(quiet), "stay")
            self.assertEqual(quiet.pings, 1)
            dead = Quiet(False)
            self.assertEqual(await migration_silence(dead), "reconnect")

        asyncio.run(run())
        db.execute("UPDATE ws_events SET ts = ? WHERE kind = 'silent'", (now,))
        db.execute("UPDATE ws_events SET ts = ? WHERE kind = 'reconnect'", (now,))
        body = diagnose(now)
        self.assertEqual(body["websocket"]["reconnects_trailing_hour"], 1)
        self.assertFalse(body["websocket"]["alert"])
        for _ in range(11):
            db.note_ws_reconnect(now, kind="silent")
        self.assertFalse(diagnose(now)["websocket"]["alert"])
        source = (Path(__file__).resolve().parents[1] / "agents" / "scanner.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("subscribeMigration"), 1)
        self.assertNotIn("subscribeTokenTrade", source)
        self.assertNotIn("subscribeAccountTrade", source)

    def test_reconnects_count_only_the_trailing_hour(self):
        now = 2_000_000.0
        db.note_ws_reconnect(now - 3700)
        db.note_ws_reconnect(now - 10)
        db.note_ws_reconnect(now - 20)
        body = diagnose(now)
        self.assertEqual(body["websocket"]["reconnects_trailing_hour"], 2)
        self.assertFalse(body["websocket"]["alert"])

    def test_report_window_orders_red_and_leaves_cost_unknown(self):
        end = _at_vn_morning()
        start = end - 86400
        self.assertEqual(datetime.fromtimestamp(end, DESK_TZ).strftime("%H:%M"), "08:00")
        span = window(end)
        self.assertEqual(span["start"], start)
        self.assertEqual(span["end"], end)
        db.execute("INSERT INTO coins (mint, symbol) VALUES (?, 'RED')", (RED,))
        db.execute("INSERT INTO coins (mint, symbol) VALUES (?, 'GRN')", (GREEN,))
        db.execute(
            """
            INSERT INTO positions (
                mint, opened_at, entry_price, exit_price, size_usd, closed_at, pnl_usd, fees_usd, exit_reason, mode
            ) VALUES (?, ?, 1, 0.9, 10, ?, -2, 0.2, 'stop', 'PAPER')
            """,
            (RED, start + 10, end - 10),
        )
        db.execute(
            """
            INSERT INTO positions (
                mint, opened_at, entry_price, exit_price, size_usd, closed_at, pnl_usd, fees_usd, exit_reason, mode
            ) VALUES (?, ?, 1, 1.2, 10, ?, 1, 0.2, 'trail', 'PAPER')
            """,
            (GREEN, start + 20, start + 30),
        )
        db.execute(
            """
            INSERT INTO positions (
                mint, opened_at, entry_price, exit_price, size_usd, closed_at, pnl_usd, fees_usd, exit_reason, mode
            ) VALUES (?, ?, 1, 1, 10, ?, 9, 0.1, 'stop', 'PAPER')
            """,
            (RED, end, end),
        )
        db.execute(
            "INSERT INTO scans (mint, ts, verdict, reason) VALUES (?, ?, 'kill', 'liq $1')",
            (RED, start + 5),
        )
        db.execute(
            "INSERT INTO scans (mint, ts, verdict, reason) VALUES (?, ?, 'kill', 'liq $1')",
            (GREEN, start + 6),
        )
        db.execute(
            """
            INSERT INTO audits (mint, ts, layer, name, value_text, "pass", source)
            VALUES (?, ?, '3', 'Dev wallet', 'dev holds 31%', 0, 'rpc')
            """,
            (RED, start + 7),
        )
        db.execute(
            """
            INSERT INTO verdicts (mint, ts, score, "pass", reason, input_tokens)
            VALUES (?, ?, 40, 0, 'chase 0.31', NULL)
            """,
            (GREEN, start + 8),
        )
        db.set_setting("rpc_url", "https://secret-rpc.example/hidden", "test")
        body = gather(end + 60)
        self.assertEqual(body["window"]["id"], span["id"])
        self.assertEqual(body["trades"]["red"][0]["symbol"], "RED")
        self.assertEqual(body["trades"]["green"][0]["symbol"], "GRN")
        self.assertEqual(body["trades"]["red"][0]["coin_url"], links.coin_url(RED, "solana"))
        self.assertNotIn("secret-rpc", str(body))
        self.assertEqual(body["scanner_kills"], [{"reason": "liq $1", "count": 2}])
        self.assertEqual(body["auditor_refusals"][0]["layer"], "Dev wallet")
        self.assertEqual(body["analyst_rejects"], [{"rule": "chase 0.31", "count": 1}])
        self.assertIsNone(body["jev"]["input_tokens"])
        self.assertIsNone(body["jev"]["cost_usd"])
        self.assertEqual(body["jev"]["cost"], "unknown")
        self.assertEqual(body["footer"], "New to FOMO? " + links.SIGNUP_URL)
        self.assertFalse(body["schedule"]["server_reschedules_routines"])
        self.assertEqual(len(body["trades"]["red"]) + len(body["trades"]["green"]), 2)
        empty = gather(start)
        self.assertEqual(empty["status"], "not_run")
        self.assertEqual(empty["trades"]["red"], [])

    def test_ack_clears_only_that_windows_skips_once(self):
        end = _at_vn_morning()
        span = window(end)
        db.execute('INSERT INTO skips (mint, "until") VALUES (?, ?)', ("old", span["end"]))
        db.execute('INSERT INTO skips (mint, "until") VALUES (?, ?)', ("new", span["end"] + 86400))
        before = gather(end + 60)
        self.assertEqual(
            [row["mint"] for row in db.query("SELECT mint FROM skips ORDER BY mint")],
            ["new", "old"],
        )
        self.assertEqual(before["status"], "not_run")
        bad = save_page(span["id"], "http://insecure.example/page")
        self.assertFalse(bad["ok"])
        self.assertEqual(len(db.query("SELECT mint FROM skips")), 2)
        saved = save_page(span["id"], "https://pages.example/night", now=end + 60)
        self.assertTrue(saved["saved"])
        self.assertEqual(saved["skips_cleared"], 1)
        self.assertEqual([row["mint"] for row in db.query("SELECT mint FROM skips")], ["new"])
        db.execute('INSERT INTO skips (mint, "until") VALUES (?, ?)', ("due-again", span["end"]))
        again = save_page(span["id"], "https://pages.example/night", now=end + 120)
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["skips_cleared"], 0)
        self.assertEqual(
            sorted(row["mint"] for row in db.query("SELECT mint FROM skips")),
            ["due-again", "new"],
        )
        self.assertEqual(commands.run("report")["text"], "https://pages.example/night")

    def test_routes_do_not_notify_or_enable_agents(self):
        import server
        client = TestClient(server.app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        self.assertEqual(client.get("/api/health").status_code, 401)
        health = client.get("/api/health", headers=AUTH)
        self.assertEqual(health.status_code, 200)
        self.assertFalse(health.json()["agents_enabled"])
        self.assertNotIn("secret-rpc", health.text)
        report = client.get("/api/report", headers=AUTH)
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.json()["status"], "not_run")
        self.assertEqual(db.query("SELECT mint FROM skips"), [])
        recover = client.post("/api/health/recover", headers=AUTH, json={"agent": "scanner"})
        self.assertFalse(recover.json()["restarted"])
        self.assertEqual(db.get_setting("agents_enabled"), "0")
        self.assertEqual(client.get("/health").json()["step"], 12)


if __name__ == "__main__":
    unittest.main()
