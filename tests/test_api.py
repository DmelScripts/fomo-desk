"""STEP 7 API. Dummy tokens only. Fixtures stay in a temp database."""
import json
import os
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import db
from api import _night_start, _state, api_price_mints, is_paused, write_price_batch
from config import DESK_TZ, report_boundary
from bus import hub, set_risk_price_mints, set_scanner_price_mints
from tests.support import temp_desk_env

TOKEN = "dummy-app-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
MINT = "MintApi111111111111111111111111111111111"


class TestApi(unittest.TestCase):
    def setUp(self):
        self.tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        os.environ["APP_TOKEN"] = TOKEN
        os.environ["TYPESAFE_API_KEY"] = "dummy-typesafe"
        os.environ["TG_TOKEN"] = "dummy-tg"
        os.environ["VAPID_PRIVATE_KEY"] = "dummy-vapid"
        os.environ.pop("DESK_AGENTS", None)
        self.addCleanup(lambda: os.environ.pop("APP_TOKEN", None))
        self.addCleanup(lambda: os.environ.pop("TYPESAFE_API_KEY", None))
        self.addCleanup(lambda: os.environ.pop("TG_TOKEN", None))
        self.addCleanup(lambda: os.environ.pop("VAPID_PRIVATE_KEY", None))
        import server
        self.client = TestClient(server.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        set_scanner_price_mints(set())
        set_risk_price_mints(set())

    def test_auth_and_public_health(self):
        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        body = health.json()
        self.assertEqual(body["step"], 10)
        self.assertFalse(body["agents_enabled"])
        self.assertNotIn("dummy-app-token", health.text)
        self.assertEqual(self.client.get("/api/state").status_code, 401)
        self.assertEqual(self.client.get("/api/state", headers={"Authorization": "Bearer wrong"}).status_code, 401)
        leaked = self.client.get("/api/state", headers=AUTH, params={"token": TOKEN})
        self.assertEqual(leaked.status_code, 401)
        self.assertNotIn(TOKEN, leaked.text)
        os.environ.pop("APP_TOKEN")
        missing = self.client.get("/api/state", headers=AUTH)
        self.assertEqual(missing.status_code, 503)
        self.assertEqual(missing.json()["detail"], "unavailable")
        os.environ["APP_TOKEN"] = TOKEN

    def test_empty_shapes(self):
        state = self.client.get("/api/state", headers=AUTH)
        self.assertEqual(state.status_code, 200)
        payload = state.json()
        self.assertEqual(payload["mode"], "PAPER")
        self.assertEqual(payload["balance"], 200.0)
        self.assertEqual(payload["today_pct"], 0.0)
        self.assertEqual(payload["multiplier"], 1.0)
        self.assertEqual([card["name"] for card in payload["agents"]], ["scanner", "auditor", "analyst", "executor", "risk"])
        self.assertTrue(all(card["running"] is False for card in payload["agents"]))
        self.assertEqual(payload["agents"][0]["line"], "Listening for graduations")
        chart = self.client.get("/api/chart", headers=AUTH, params={"range": "1H"}).json()
        self.assertEqual(chart["points"], [])
        self.assertEqual(chart["marks"], [])
        self.assertEqual(self.client.get("/api/activity", headers=AUTH).json()["rows"], [])
        self.assertEqual(self.client.get("/api/coin/" + MINT, headers=AUTH).status_code, 404)
        audit = self.client.get("/api/audit/" + MINT, headers=AUTH).json()
        self.assertEqual(audit["layers"], [])
        self.assertIsNone(audit["passed"])
        self.assertIn("r=savipww", audit["coin_url"])
        decisions = self.client.get("/api/agents/scanner/decisions", headers=AUTH).json()
        self.assertEqual(decisions["decisions"], [])
        self.assertEqual(self.client.get("/api/agents/nope/decisions", headers=AUTH).status_code, 404)
        account = self.client.get("/api/account", headers=AUTH).json()
        self.assertEqual(account["mode"], "PAPER")
        self.assertEqual(account["stack"], 200.0)
        self.assertEqual(account["signup_url"], "https://fomo.family/r/savipww")
        self.assertIn("r=savipww", account["profile_url"])
        settings = self.client.get("/api/settings", headers=AUTH).json()
        self.assertIn("scanner", settings["groups"])
        self.assertNotIn("dummy-typesafe", json.dumps(settings))
        self.assertNotIn("APP_TOKEN", json.dumps(settings))

    def test_validation_changelog_and_live_lock(self):
        bad = self.client.post("/api/settings", headers=AUTH, json={"key": "stop_pct", "value": "1000"})
        self.assertEqual(bad.status_code, 400)
        self.assertIn("0.1", bad.json()["detail"])
        self.assertIn("100", bad.json()["detail"])
        refused = self.client.post("/api/settings", headers=AUTH, json={"key": "APP_TOKEN", "value": "nope"})
        self.assertEqual(refused.status_code, 400)
        self.assertNotIn("nope", refused.text)
        ok = self.client.post("/api/settings", headers=AUTH, json={"key": "stop_pct", "value": "8"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(db.get_setting("stop_pct"), "8")
        log = db.query('SELECT key, "old", "new", who FROM change_log')
        self.assertEqual(log[-1]["key"], "stop_pct")
        self.assertEqual(log[-1]["new"], "8")
        self.assertEqual(log[-1]["who"], "api")
        self.assertEqual(self.client.get("/api/chart", headers=AUTH, params={"range": "nope"}).status_code, 400)
        self.assertEqual(self.client.get("/api/activity", headers=AUTH, params={"before": "soon"}).status_code, 400)
        live = self.client.post("/api/mode", headers=AUTH, json={"mode": "LIVE"})
        self.assertEqual(live.status_code, 400)
        locked = self.client.post("/api/mode", headers=AUTH, json={"mode": "LIVE", "confirm": "LIVE"})
        self.assertEqual(locked.status_code, 409)
        self.assertEqual(db.get_setting("mode"), "PAPER")
        self.assertEqual(self.client.post("/api/mode", headers=AUTH, json={"mode": "PAPER"}).json()["mode"], "PAPER")
        db.execute(
            "INSERT INTO positions (mint, opened_at, entry_price, size_usd, mode) VALUES (?, ?, ?, ?, 'PAPER')",
            (MINT, time.time(), 1, 10),
        )
        blocked = self.client.post("/api/agents/risk/pause", headers=AUTH)
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(self.client.post("/api/agents/scanner/pause", headers=AUTH).json()["paused"], True)
        self.assertEqual(self.client.post("/api/agents/scanner/resume", headers=AUTH).json()["paused"], False)
        saved = self.client.post("/api/push", headers=AUTH, json={
            "endpoint": "https://push.example/sub", "p256dh": "pub", "auth": "auth",
        })
        self.assertEqual(saved.status_code, 200)
        self.assertNotIn("auth", saved.json())
        self.assertEqual(self.client.post("/api/push", headers=AUTH, json={
            "endpoint": "http://insecure.example", "p256dh": "pub", "auth": "auth",
        }).status_code, 400)

    def test_price_writer_ownership_and_one_batch(self):
        now = time.time()
        db.execute(
            "INSERT INTO coins (mint, name, symbol) VALUES (?, ?, ?)",
            ("AuditedMint", "Aud", "AUD"),
        )
        db.execute(
            "INSERT INTO audits (mint, ts, layer, name, value_text, \"pass\", source) VALUES (?, ?, '1', 'Mint authority', 'Revoked', 1, 'rpc')",
            ("AuditedMint", now),
        )
        db.execute(
            "INSERT INTO coins (mint, name, symbol) VALUES (?, ?, ?)",
            ("OpenMint", "Open", "OPN"),
        )
        db.execute(
            "INSERT INTO positions (mint, opened_at, entry_price, size_usd, mode) VALUES (?, ?, 1, 10, 'PAPER')",
            ("OpenMint", now),
        )
        set_scanner_price_mints({"WaitingMint"})
        mints = api_price_mints(now)
        self.assertIn("AuditedMint", mints)
        self.assertNotIn("OpenMint", mints)
        self.assertNotIn("WaitingMint", mints)
        payload = [
            {"dexId": "pumpfun", "baseToken": {"address": "AuditedMint"}, "priceUsd": "1", "volume": {"m5": 10}, "liquidity": {"usd": 1}},
            {"dexId": "pumpswap", "baseToken": {"address": "AuditedMint"}, "priceUsd": "2", "volume": {"m5": 30}, "liquidity": {"usd": 50000}},
        ]
        self.assertEqual(write_price_batch(payload, now), 1)
        row = db.query("SELECT price FROM prices WHERE mint = ?", ("AuditedMint",))[0]
        self.assertEqual(row["price"], 2.0)

    def test_sse_contract_and_disconnect(self):
        with self.client.stream("GET", "/api/stream", headers=AUTH) as response:
            self.assertEqual(response.status_code, 200)
            self.assertIn("text/event-stream", response.headers["content-type"])
            hub.publish("scan", {"mint": MINT, "verdict": "kill", "reason": "liq $1.0K"})
            chunk = ""
            for piece in response.iter_text():
                chunk += piece
                if "event: scan" in chunk:
                    break
            self.assertIn("event: scan", chunk)
            self.assertIn(MINT, chunk)
            self.assertNotIn("dummy-app-token", chunk)
        self.assertEqual(len(hub.subscribers), 0)
        self.assertEqual(self.client.get("/api/stream").status_code, 401)

    def test_errors_do_not_leak_secrets(self):
        import api
        original = api._state

        def explode():
            raise RuntimeError("TYPESAFE_API_KEY=dummy-typesafe TG_TOKEN=dummy-tg")

        api._state = explode
        try:
            response = self.client.get("/api/state", headers=AUTH)
        finally:
            api._state = original
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": "error"})
        self.assertNotIn("dummy-typesafe", response.text)
        self.assertNotIn("dummy-tg", response.text)
        self.assertNotIn("dummy-vapid", response.text)

    def test_vietnam_day_boundary(self):
        from datetime import datetime, timezone
        at = datetime(2026, 11, 6, 1, 0, tzinfo=timezone.utc)
        self.assertEqual(at.astimezone(DESK_TZ).strftime("%Y-%m-%d %H:%M"), "2026-11-06 08:00")
        exactly = at.timestamp()
        just_before = exactly - 1
        just_after = exactly + 1
        self.assertEqual(_night_start(exactly), exactly)
        self.assertEqual(_night_start(just_after), exactly)
        self.assertEqual(_night_start(just_before), exactly - 86400)
        self.assertEqual(report_boundary(exactly, "08:00", upcoming=True), exactly + 86400)
        self.assertEqual(report_boundary(just_before, "08:00", upcoming=True), exactly)
        db.execute(
            "INSERT INTO signals (mint, ts, price, mode, status) VALUES (?, ?, 1, 'PAPER', 'sent')",
            ("BeforeMint", just_before),
        )
        db.execute(
            "INSERT INTO signals (mint, ts, price, mode, status) VALUES (?, ?, 1, 'PAPER', 'sent')",
            ("AfterMint", exactly),
        )
        owned = api_price_mints(just_after)
        self.assertIn("AfterMint", owned)
        self.assertNotIn("BeforeMint", owned)
        db.execute(
            "INSERT INTO balance (ts, value) VALUES (?, ?)",
            (just_before, 150.0),
        )
        db.execute(
            "INSERT INTO balance (ts, value) VALUES (?, ?)",
            (exactly, 180.0),
        )
        from api import _chart
        live = _chart("LIVE", just_after)
        self.assertEqual([point["ts"] for point in live["points"]], [exactly])
        db.execute(
            """
            INSERT INTO positions (
                mint, opened_at, entry_price, size_usd, closed_at, pnl_usd, mode
            ) VALUES (?, ?, 1, 10, ?, 50, 'PAPER')
            """,
            ("AfterMint", exactly, exactly),
        )
        state = _state(just_after)
        self.assertEqual(state["balance"], 250.0)
        self.assertEqual(state["today_pct"], 25.0)
        self.assertEqual(state["multiplier"], 1.25)

    def test_pause_reaches_the_loops(self):
        import asyncio
        from agents.auditor import Auditor
        from agents.executor import Executor
        from agents.scanner import Scanner
        from bus import Desk
        from notifier import Notifier
        import api

        class Boom:
            async def get_json(self, url):
                raise AssertionError(url)
            async def post_json(self, *args, **kwargs):
                raise AssertionError("paused work called the network")

        desk = Desk(Boom(), Notifier(mode="PAPER"), now=lambda: 1_000.0)
        api._paused.add("scanner")
        self.addCleanup(api._paused.clear)
        scanner = Scanner(desk)
        held = asyncio.run(scanner.ingest({
            "mint": "PausedMint", "signature": "sig", "txType": "migrate", "pool": "pump-amm",
        }))
        self.assertEqual(held["reason"], "paused")
        self.assertIn("PausedMint", scanner.waiting)

        api._paused.add("auditor")
        auditor = Auditor(desk)
        asyncio.run(desk.bus.auditor.put({"mint": "PausedMint"}))
        payload = asyncio.run(desk.bus.auditor.get())

        async def hold():
            return await auditor._hold_if_paused(payload, desk.bus.auditor)

        self.assertTrue(asyncio.run(hold()))
        self.assertEqual(desk.bus.auditor.qsize(), 1)
        self.assertFalse(db.query("SELECT id FROM audits"))

        async def cancel_sleep(_seconds):
            raise asyncio.CancelledError()

        held_desk = Desk(Boom(), Notifier(mode="PAPER"), now=lambda: 1_000.0, sleep=cancel_sleep)
        executor = Executor(held_desk)
        asyncio.run(held_desk.bus.executor.put({"mint": "PausedMint", "price": 1, "liquidity_usd": 50000}))
        api._paused.add("executor")
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(executor.run())
        self.assertEqual(held_desk.bus.executor.qsize(), 1)
        self.assertFalse(db.query("SELECT id FROM signals WHERE mint = ?", ("PausedMint",)))

        from agents.analyst import Analyst
        analyst = Analyst(held_desk)
        asyncio.run(held_desk.bus.analyst.put({"mint": "PausedMint"}))
        api._paused.add("analyst")
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(analyst.run())
        self.assertEqual(held_desk.bus.analyst.qsize(), 1)

        api._paused.add("risk")
        self.assertTrue(is_paused("risk"))
        db.execute(
            "INSERT INTO positions (mint, opened_at, entry_price, size_usd, mode) VALUES (?, 1, 1, 10, 'PAPER')",
            ("OpenMint",),
        )
        self.assertFalse(is_paused("risk"))
        refused = self.client.post("/api/agents/risk/pause", headers=AUTH)
        self.assertEqual(refused.status_code, 409)

    def test_account_hides_secrets_and_search_uses_real_rows(self):
        account = self.client.get("/api/account", headers=AUTH)
        self.assertEqual(account.status_code, 200)
        text = account.text
        self.assertNotIn("dummy-typesafe", text)
        self.assertNotIn("dummy-tg", text)
        self.assertNotIn("dummy-vapid", text)
        self.assertNotIn(TOKEN, text)
        body = account.json()
        self.assertFalse(body["integrations"]["email"]["ready"])
        self.assertFalse(body["integrations"]["telegram"]["ready"])
        self.assertTrue(body["integrations"]["web_push"]["ready"])
        self.assertEqual(body["integrations"]["web_push"]["channel"], "webpush")
        self.assertTrue(body["jev"]["host_locked"])
        self.assertIn("cannot show", body["jev"]["detail"])
        locked = self.client.post("/api/account/key", headers=AUTH, json={
            "name": "TYPESAFE_API_KEY", "value": "super-secret-jev",
        })
        self.assertEqual(locked.status_code, 409)
        self.assertNotIn("super-secret-jev", locked.text)
        self.assertNotIn("dummy-typesafe", locked.text)
        os.environ.pop("TYPESAFE_API_KEY")
        secret_path = Path(self.tmp.name) / "secrets.env"
        os.environ["DESK_SECRETS_PATH"] = str(secret_path)
        self.addCleanup(lambda: os.environ.pop("DESK_SECRETS_PATH", None))
        saved = self.client.post("/api/account/key", headers=AUTH, json={
            "name": "TYPESAFE_API_KEY", "value": "file-only-jev",
        })
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json(), {"ok": True, "configured": True})
        self.assertNotIn("file-only-jev", saved.text)
        self.assertIn("file-only-jev", secret_path.read_text())
        cleared = self.client.post("/api/account/key", headers=AUTH, json={
            "name": "TYPESAFE_API_KEY", "clear": True,
        })
        self.assertEqual(cleared.json()["configured"], False)
        self.assertNotIn("file-only-jev", cleared.text)
        db.execute(
            "INSERT INTO coins (mint, name, symbol) VALUES (?, ?, ?)",
            (MINT, "Searchable", "SRCH"),
        )
        found = self.client.get("/api/search", headers=AUTH, params={"q": "SRCH"}).json()
        self.assertEqual(found["rows"][0]["mint"], MINT)
        self.assertIn("r=savipww", found["rows"][0]["coin_url"])
        self.assertEqual(self.client.get("/api/search", headers=AUTH, params={"q": ""}).json()["rows"], [])

    def test_coin_leaves_missing_market_fields_null(self):
        now = time.time()
        db.execute(
            "INSERT INTO coins (mint, name, symbol, migrated_at) VALUES (?, ?, ?, ?)",
            (MINT, "Null Cap", "NUL", now - 3600),
        )
        db.execute(
            "INSERT INTO prices (mint, ts, price, volume_m5) VALUES (?, ?, ?, NULL)",
            (MINT, now, 1.25),
        )
        coin = self.client.get("/api/coin/" + MINT, headers=AUTH).json()
        self.assertIsNone(coin["stats"]["market_cap"])
        self.assertIsNone(coin["stats"]["volume_h24"])
        self.assertEqual(coin["stats"]["holders"], "n/a")
        self.assertIsNone(coin["stats"]["liquidity_usd"])
        self.assertEqual(coin["price"], 1.25)
        self.assertGreater(coin["age_min"], 50)
        self.assertIsNone(coin["position"])
        self.assertIsNone(coin["shield"])
