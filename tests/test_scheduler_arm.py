"""The six-role loop can arm in PAPER. Arming does not sign or send."""
import json
import os
import unittest

from fastapi.testclient import TestClient

import commands
import cross_source
import db
import desk_loop
import desk_providers
import live_submit
import memecoin_desk
import solana_rpc
import solana_tx
from tests.support import temp_desk_env

TOKEN = "dummy-app-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
NOW = 1_800_000_000.0
MINT = solana_tx.b58encode(b"\x21" * 32)
HELIUS = "test-helius-not-a-secret"
TRACKER = "test-tracker-not-a-secret"


class SchedulerArmTests(unittest.TestCase):
    def setUp(self):
        self.tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        os.environ["APP_TOKEN"] = TOKEN
        os.environ["HELIUS_API_KEY"] = HELIUS
        os.environ["SOLANA_TRACKER_API_KEY"] = TRACKER
        os.environ["DESK_PRICE_LOOP"] = "0"
        os.environ.pop("DESK_SIM_DRY_RUN", None)
        self.addCleanup(lambda: os.environ.pop("APP_TOKEN", None))
        self.addCleanup(lambda: os.environ.pop("HELIUS_API_KEY", None))
        self.addCleanup(lambda: os.environ.pop("SOLANA_TRACKER_API_KEY", None))
        self.addCleanup(lambda: os.environ.pop("DESK_SIM_DRY_RUN", None))
        self.addCleanup(lambda: os.environ.pop("DESK_PRICE_LOOP", None))
        db.init_db()
        db.set_setting("mode", "PAPER", "test")
        db.set_setting("agents_enabled", "0", "test")
        desk_providers.shutdown()
        cross_source.clear_fetcher()
        desk_providers.install()
        self.addCleanup(desk_providers.shutdown)
        self.addCleanup(lambda: desk_providers.set_transport(None))
        self.addCleanup(lambda: desk_providers.set_gap(None))
        previous = solana_rpc.TRANSPORT
        self.addCleanup(lambda: setattr(solana_rpc, "TRANSPORT", previous))
        import server
        self.client = TestClient(server.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_the_switch_is_authenticated_and_paper_arming_does_not_sign(self):
        import phantom_session
        self.assertEqual(db.get_setting("memecoin_scheduler_authorized"), "0")
        quiet = desk_loop.scheduler_status()
        self.assertTrue(quiet["ready"])
        self.assertFalse(quiet["authorized"])
        self.assertFalse(quiet["armed"])
        self.assertFalse(quiet["sign"])
        self.assertFalse(quiet["send"])
        self.assertFalse(live_submit.armed())

        missing = self.client.post("/api/settings", json={"key": "memecoin_scheduler_authorized", "value": "1"})
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(db.get_setting("memecoin_scheduler_authorized"), "0")
        refused = commands.run("set memecoin_scheduler_authorized to 1")
        self.assertTrue(refused["refused"])
        self.assertEqual(db.get_setting("memecoin_scheduler_authorized"), "0")

        os.environ.pop("SOLANA_TRACKER_API_KEY")
        blocked = self.client.post(
            "/api/settings", headers=AUTH, json={"key": "memecoin_scheduler_authorized", "value": "1"},
        )
        self.assertEqual(blocked.status_code, 200, blocked.text)
        self.assertEqual(blocked.json()["value"], "1")
        unready = desk_loop.scheduler_status()
        self.assertTrue(unready["authorized"])
        self.assertFalse(unready["ready"])
        self.assertFalse(unready["armed"])
        self.assertIn("solana_tracker_api_key_missing", unready["ready_because"])
        self.assertIn("holder_growth_provider_unready", unready["ready_because"])
        self.assertNotIn("holder_growth_unproven", unready["ready_because"])
        self.assertNotIn("lp_fields_unpublished", unready["ready_because"])
        self.assertNotIn(TRACKER, blocked.text)
        self.assertNotIn(HELIUS, json.dumps(unready))

        os.environ["SOLANA_TRACKER_API_KEY"] = TRACKER
        armed = desk_loop.scheduler_status()
        self.assertTrue(armed["authorized"])
        self.assertTrue(armed["ready"])
        self.assertTrue(armed["armed"])
        self.assertFalse(armed["sign"])
        self.assertFalse(armed["send"])
        self.assertFalse(armed["live_ready"])
        self.assertEqual(armed["ready_because"], [])
        self.assertEqual(db.get_setting("mode"), "PAPER")
        self.assertEqual(db.get_setting("agents_enabled"), "0")
        self.assertFalse(live_submit.armed())
        log = db.query(
            'SELECT who, "old", "new" FROM change_log WHERE key = ? ORDER BY ts',
            ("memecoin_scheduler_authorized",),
        )
        self.assertEqual(log[-1]["who"], "api")
        self.assertEqual(log[-1]["old"], "0")
        self.assertEqual(log[-1]["new"], "1")
        gates = {item["gate"]: item["closed"] for item in desk_loop.remaining_live_gates()}
        for name in ("lp_lock_live", "rug_history_live", "holder_growth_live", "real_submit"):
            self.assertIn(name, gates)
            self.assertFalse(gates[name])

        growth = desk_loop.holder_growth(MINT)
        self.assertFalse(growth["ok"])
        self.assertEqual(growth["reason"], "holder_growth_unproven")
        desk_providers.set_gap(0)
        desk_providers.set_transport(lambda spec: {"status": 404, "body": {}})
        scouted = memecoin_desk.scout({
            "mint": MINT,
            "source": "dexscreener",
            "market_cap_basis": "marketCap",
            "fetched_at": NOW,
        }, now=NOW)
        self.assertNotEqual(scouted.get("stage"), "scout_passed")
        self.assertTrue(desk_loop.scheduler_status()["armed"])

        signs = []
        original = phantom_session.sign
        phantom_session.sign = lambda *args, **kwargs: signs.append("sign")
        self.addCleanup(lambda: setattr(phantom_session, "sign", original))
        seen = []

        def transport(request):
            seen.append(request.get("method") or request.get("url"))
            if request.get("method") in {"sendTransaction", "signTransaction"}:
                signs.append(request.get("method"))
            return {"status": 404, "body": {}}

        found = desk_loop.run_discovery(transport, NOW)
        self.assertNotIn("sendTransaction", seen)
        self.assertNotIn("signTransaction", seen)
        self.assertEqual(signs, [])
        self.assertFalse(found["send"])
        self.assertFalse(found["sign"])
        submitted = live_submit.submit("case", "hash", NOW, transport, lambda message: signs.append(message))
        self.assertEqual(submitted["reason"], "live_unreachable")
        self.assertEqual(signs, [])
        self.assertFalse(submitted["sign"])
        self.assertFalse(submitted["send"])

        os.environ["DESK_SIM_DRY_RUN"] = "1"
        self.assertTrue(desk_loop.simulation_allowed())
        self.assertEqual(db.get_setting("mode"), "PAPER")
        posted = []

        def rpc(payload):
            posted.append(payload["method"])
            return {"status": 200, "body": {"result": {"value": {"err": None}}}}

        solana_rpc.TRANSPORT = rpc
        simulated = desk_loop.public_reader({"method": "simulateTransaction", "transaction": "AQID"})
        self.assertEqual(posted, ["simulateTransaction"])
        self.assertEqual(simulated["status"], 200)
        self.assertFalse(simulated.get("sign") is True)
        with self.assertRaises(RuntimeError) as refused_send:
            desk_loop.public_reader({"method": "sendTransaction", "transaction": "AQID"})
        self.assertEqual(str(refused_send.exception), "method_refused")
        self.assertEqual(posted, ["simulateTransaction"])
        self.assertEqual(signs, [])
        os.environ.pop("DESK_SIM_DRY_RUN")
        with self.assertRaises(RuntimeError) as refused_sim:
            desk_loop.public_reader({"method": "simulateTransaction", "transaction": "AQID"})
        self.assertEqual(str(refused_sim.exception), "method_refused")

        os.environ.pop("HELIUS_API_KEY")
        without_helius = desk_loop.scheduler_status()
        self.assertFalse(without_helius["armed"])
        self.assertIn("helius_api_key_missing", without_helius["ready_because"])
        self.assertIn("rug_history_provider_unready", without_helius["ready_because"])
        os.environ["HELIUS_API_KEY"] = HELIUS
        off = self.client.post(
            "/api/settings", headers=AUTH, json={"key": "memecoin_scheduler_authorized", "value": "0"},
        )
        self.assertEqual(off.status_code, 200, off.text)
        stopped = desk_loop.scheduler_status()
        self.assertTrue(stopped["ready"])
        self.assertFalse(stopped["authorized"])
        self.assertFalse(stopped["armed"])
        self.assertFalse(live_submit.armed())
        self.assertEqual(db.get_setting("mode"), "PAPER")
        self.assertEqual(db.get_setting("agents_enabled"), "0")
