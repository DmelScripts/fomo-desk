"""Authenticated limit configuration and real runner reservation boundaries."""
import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fastapi.testclient import TestClient
from fastapi import HTTPException
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

    def test_readiness_separates_approved_limits_from_execution_evidence(self):
        path = "/api/phantom/readiness"
        self.assertEqual(self.client.get(path).status_code, 401)
        self.client.post("/api/phantom/risk-limits", headers=self.auth, json=self.policy)
        result = self.client.get(path, headers=self.auth)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers["cache-control"], "no-store")
        body = result.json()
        self.assertTrue(body["checks"]["fee_policy_approved"])
        self.assertTrue(body["checks"]["aggregate_loss_policy_approved"])
        for check in ("actual_fees_verified", "live_ledger_verified", "swap_path_verified", "signer_enabled"):
            self.assertIn(check, body["unmet"])
        self.assertFalse(body["ready"])
        self.assertFalse(body["live_ready"])

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

    def _control_change_between_entry_check_and_transaction(self, control, *, reserved):
        self.client.post("/api/phantom/risk-limits", headers=self.auth, json=self.policy)
        db.set_setting("mode", "PAPER", "test")
        db.execute("UPDATE phantom_control SET value='0' WHERE key='halt'")
        intent_id = f"race-{control}-{reserved}"
        body = self.intent(intent_id)
        if reserved:
            spec = phantom_runner._spec(body)
            db.exclusive(lambda conn: phantom_runner._insert(conn, spec, "reserved", spec["reserved"], None))
        observed = threading.Event()
        resume = threading.Event()
        original_halted = phantom_runner.halted

        def pause_after_outer_snapshot():
            value = original_halted()
            observed.set()
            if not resume.wait(timeout=5):
                raise AssertionError("test did not release entry request")
            return value

        class NoQuote:
            name = "paper_simulation"

            def quote(self, _spec):
                raise AssertionError("a changed entry control reached the quote")

        with patch.object(phantom_runner, "halted", side_effect=pause_after_outer_snapshot):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(phantom_runner.run_intent, body, NoQuote(), "entry")
                try:
                    self.assertTrue(observed.wait(timeout=5))
                    # Commit the other request after the old snapshot and before
                    # the entry's reservation transaction is allowed to start.
                    if control == "halt":
                        db.execute("UPDATE phantom_control SET value='1' WHERE key='halt'")
                    else:
                        db.set_setting("mode", "LIVE", "test")
                finally:
                    resume.set()
                if control == "halt":
                    self.assertEqual(pending.result(timeout=5)["reason"], "kill_switch")
                else:
                    with self.assertRaises(HTTPException) as rejected:
                        pending.result(timeout=5)
                    self.assertEqual(rejected.exception.status_code, 409)
        rows = db.query("SELECT status FROM phantom_reservations WHERE intent_id=?", (intent_id,))
        self.assertEqual([row["status"] for row in rows], ["reserved"] if reserved else [])

    def test_concurrent_halt_blocks_new_and_previously_reserved_entries(self):
        for reserved in (False, True):
            with self.subTest(reserved=reserved):
                self._control_change_between_entry_check_and_transaction("halt", reserved=reserved)

    def test_concurrent_mode_change_blocks_new_and_previously_reserved_entries(self):
        for reserved in (False, True):
            with self.subTest(reserved=reserved):
                self._control_change_between_entry_check_and_transaction("mode", reserved=reserved)
