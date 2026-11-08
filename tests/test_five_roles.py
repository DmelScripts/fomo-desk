"""Scanner, auditor, analyst, executor, and risk on one mint. The buy is not sent."""
import asyncio
import copy
import os
import unittest

import db
import token_execution
from agents.analyst import Analyst
from agents.auditor import Auditor
from agents.executor import Executor
from agents.risk import Risk
from agents.scanner import Scanner
from bus import Desk
from fastapi.testclient import TestClient
from notifier import Notifier
from tests.support import load, temp_desk_env
from tests.test_replay import POOL, FakeHttp, _largest

MINT = "4FHEBKNYy6BZ1QFMKj1xL8HDTM12rdXLRYw8DgiCUnQn"
TOKEN = "dummy-app-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FiveRoleTests(unittest.TestCase):
    def test_the_bus_reaches_every_role_and_the_buy_fails_closed(self):
        _tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        os.environ["APP_TOKEN"] = TOKEN
        os.environ["TYPESAFE_API_KEY"] = "replay-key-not-real"
        self.addCleanup(lambda: os.environ.pop("APP_TOKEN", None))
        self.addCleanup(lambda: os.environ.pop("TYPESAFE_API_KEY", None))
        db.init_db()
        db.set_setting("mode", "PAPER", "test")
        db.set_setting("agents_enabled", "0", "test")
        dex = copy.deepcopy(load("dexscreener_pair.json"))
        dex[0]["liquidity"]["usd"] = 50000
        clock = {"t": 2_000_000.0}
        desk = Desk(FakeHttp(dex, _largest()), Notifier(mode="PAPER"), now=lambda: clock["t"])
        scanner, auditor, analyst = Scanner(desk), Auditor(desk), Analyst(desk)
        executor, risk = Executor(desk), Risk(desk)

        async def run():
            first = await scanner.ingest(load("pumpportal_migration.json"))
            self.assertEqual(first["verdict"], "wait", first)
            clock["t"] += 15 * 60
            scanned = await scanner.ingest(load("pumpportal_migration.json"))
            self.assertEqual(scanned["verdict"], "pass", scanned)
            self.assertEqual(scanned["pool"], POOL)
            audited = await auditor.audit_payload(await desk.bus.auditor.get())
            self.assertTrue(audited["passed"], audited.get("reason"))
            verdict = await analyst.judge(await desk.bus.analyst.get())
            self.assertTrue(verdict["passed"], verdict)
            filled = await executor.handle(await desk.bus.executor.get())
            return filled

        filled = asyncio.run(run())
        self.assertEqual(filled["status"], "bought")
        self.assertEqual(filled["chain"]["reason"], "unsupported_buy_route")
        self.assertFalse(filled["chain_send"])
        self.assertIsNone(filled["chain"]["decoder"])
        self.assertEqual(filled["chain"]["jupiter_route_v2"], "not_enabled")
        self.assertTrue(filled["chain"]["diagnostic_sol_usdc_is_not_this_buy"])
        self.assertEqual(filled["chain"]["wallet"], "7AFJbqsX2UeK7ZW5M1gbXWjiKCai2CJWHpCzXe7woC7t")
        self.assertEqual(db.query("SELECT intent_id FROM phantom_reservations"), [])
        row = db.query("SELECT * FROM positions WHERE mint = ?", (MINT,))[0]
        held = risk.poll_position(dict(row), clock["t"], price=float(dex[0]["priceUsd"]), liquidity=50000)
        self.assertEqual(held["action"], "hold")
        stopped = risk.poll_position(dict(db.query("SELECT * FROM positions WHERE mint = ?", (MINT,))[0]), clock["t"], price=float(dex[0]["priceUsd"]) * 0.5, liquidity=50000)
        self.assertEqual(stopped["action"], "stop")
        self.assertEqual(stopped["chain"]["reason"], "unsupported_exit_route")
        self.assertFalse(stopped["loss_stop_blocks_exit"])
        self.assertEqual(db.query("SELECT intent_id FROM phantom_reservations"), [])
        self.assertIn("risk", {row["agent"] for row in db.query("SELECT agent FROM activity")})
        self.assertTrue(db.query("SELECT mint FROM scans WHERE mint = ?", (MINT,)))
        self.assertTrue(db.query("SELECT mint FROM audits WHERE mint = ?", (MINT,)))
        self.assertEqual(db.query('SELECT "pass" FROM verdicts WHERE mint = ?', (MINT,))[0]["pass"], 1)
        self.assertTrue(db.query("SELECT mint FROM signals WHERE mint = ?", (MINT,)))
        self.assertEqual(db.get_setting("agents_enabled"), "0")
        import server
        client = TestClient(server.app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        routed = client.post("/api/agents/token-buy", headers=AUTH, json={"mint": MINT})
        self.assertEqual(routed.status_code, 409)
        self.assertEqual(routed.json()["reason"], "unsupported_buy_route")
        self.assertFalse(routed.json()["send"])
        hedge = client.post("/api/agents/token-buy", headers=AUTH, json={"mint": token_execution.SOL_USDC_POOL})
        self.assertEqual(hedge.json()["reason"], "diagnostic_is_not_a_token_buy")
        leaked = client.post("/api/agents/token-buy", headers=AUTH, json={"mint": MINT, "transaction": "aaaa"})
        self.assertEqual(leaked.status_code, 400)
        self.assertEqual(db.get_setting("agents_enabled"), "0")
