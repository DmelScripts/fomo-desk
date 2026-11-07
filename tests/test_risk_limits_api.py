"""Authenticated limit configuration and real runner reservation boundaries."""
import os
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fastapi.testclient import TestClient
import db
import phantom_runner
import phantom_transport
import risk_limits
from tests.support import temp_desk_env


class TestRiskLimitsAPI(unittest.TestCase):
    def setUp(self):
        self.tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        env = patch.dict(os.environ, {"APP_TOKEN": "test-risk-token", "DESK_PHANTOM_PAPER": "1"})
        env.start()
        self.addCleanup(env.stop)
        db.init_db()
        import server
        self.client = TestClient(server.app)
        self.auth = {"Authorization": "Bearer test-risk-token"}
        self.policy = {"approved": True, "max_total_loss_usd": "10.00", "max_round_trip_fee_usd": "0.20"}

    def intent(self, key, fee="0.20"):
        return {
            "intent_id": key, "wallet_id": phantom_transport.REQUIRED_WALLET_ID,
            "wallet": phantom_transport.REQUIRED_WALLET, "chain": "solana:mainnet",
            "sell_native": True, "buy_mint": phantom_runner.USDC, "slippage": 1,
            "sol_usd": "150.00", "fx_at": time.time(), "quote_source": "paper", "fee_usd": fee,
        }

    def test_auth_explicit_amounts_and_revocation_do_not_activate(self):
        path = "/api/phantom/risk-limits"
        self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.post(path, json=self.policy).status_code, 401)
        self.assertEqual(self.client.post(path, headers=self.auth, json={"approved": True}).status_code, 400)
        self.assertEqual(self.client.post(path, headers=self.auth, json={"approved": 0}).status_code, 400)
        self.assertEqual(self.client.post(path, headers=self.auth, json={**self.policy, "execute": True}).status_code, 400)
        result = self.client.post(path, headers=self.auth, json=self.policy)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.headers["cache-control"], "no-store")
        self.assertTrue(result.json()["approved"])
        self.assertFalse(result.json()["live_ready"])
        self.assertEqual(db.get_setting("mode"), "PAPER")
        self.assertEqual(db.get_setting("agents_enabled"), "0")
        self.assertTrue(phantom_runner.halted())
        db.init_db()
        self.assertEqual(self.client.get(path, headers=self.auth).json()["limits"]["max_total_loss_usd"], "10.00")
        self.assertFalse(self.client.post(path, headers=self.auth, json={"approved": False}).json()["approved"])

    def test_new_entries_enforce_limits_atomically_and_exits_remain_possible(self):
        self.client.post("/api/phantom/risk-limits", headers=self.auth, json=self.policy)
        phantom_runner.set_halt({"halted": False})
        too_expensive = phantom_runner.run_intent(self.intent("fee", "0.21"), runtime=phantom_runner.PaperRuntime(), cycle="entry")
        self.assertEqual(too_expensive["reason"], "round_trip_fee_limit")
        def enter(key):
            return phantom_runner.run_intent(self.intent(key), runtime=phantom_runner.PaperRuntime(), cycle="entry")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(enter, ["a", "b"]))
        self.assertEqual(sorted(result["status"] for result in results), ["rejected", "sim_open"])
        self.assertEqual(risk_limits.view()["open_committed_usd"], "8.20")
        opened = db.query("SELECT intent_id FROM phantom_reservations WHERE status='sim_open'")[0]["intent_id"]
        closed = phantom_runner.run_intent(self.intent(opened), runtime=phantom_runner.PaperRuntime(), cycle="exit", prices={"entry": "1", "exit": "0.5"})
        self.assertEqual(closed["status"], "sim_closed")
        self.assertEqual(risk_limits.view()["cumulative_realized_losses_usd"], "4.20")
        self.assertEqual(enter("next")["reason"], "total_loss_limit")

    def test_revocation_and_changed_retry_cannot_bypass_the_stored_reservation(self):
        self.client.post("/api/phantom/risk-limits", headers=self.auth, json=self.policy)
        phantom_runner.set_halt({"halted": False})
        body = self.intent("pending")
        spec = phantom_runner._spec(body)
        db.exclusive(lambda conn: phantom_runner._insert(conn, spec, "reserved", spec["reserved"], None))
        class NoQuote:
            name = "paper_simulation"
            def quote(self, spec):
                raise AssertionError("blocked entry called quote")
        changed = phantom_runner.run_intent(self.intent("pending", "7.00"), runtime=NoQuote(), cycle="entry")
        self.assertEqual(changed["reason"], "intent_changed")
        self.assertEqual(risk_limits.view()["open_committed_usd"], "8.20")
        risk_limits.clear()
        revoked = phantom_runner.run_intent(body, runtime=NoQuote(), cycle="entry")
        self.assertEqual(revoked["reason"], "approval_revoked")
        self.assertEqual(phantom_runner.run_intent(self.intent("new"), runtime=NoQuote(), cycle="entry")["reason"], "approval_revoked")
