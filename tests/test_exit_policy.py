"""Exact exit-rule approval. It does not enable LIVE or change the cap."""
import os
import unittest

from fastapi.testclient import TestClient

import db
import phantom_runner
from tests.support import temp_desk_env

TOKEN = "dummy-app-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class TestExitPolicy(unittest.TestCase):
    def setUp(self):
        self.tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        os.environ["APP_TOKEN"] = TOKEN
        self.addCleanup(lambda: os.environ.pop("APP_TOKEN", None))
        db.init_db()
        db.set_setting("mode", "PAPER", "test")
        db.set_setting("agents_enabled", "0", "test")
        import server
        self.client = TestClient(server.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_approval_persists_restarts_and_revokes_without_touching_guards(self):
        self.assertEqual(self.client.get("/api/phantom/exit-policy").status_code, 401)
        before = self.client.get("/api/phantom/exit-policy", headers=AUTH)
        self.assertEqual(before.headers.get("cache-control"), "no-store")
        self.assertFalse(before.json()["approved"])
        self.assertFalse(before.json()["effective"])
        self.assertFalse(before.json()["live_consent"])
        self.assertEqual(before.json()["loss_policy"], "not_approved")
        self.assertFalse(before.json()["aggregate_loss_policy"])
        self.assertFalse(before.json()["guaranteed_loss_limit"])
        extra = self.client.post("/api/phantom/exit-policy", headers=AUTH, json={"approved": True, "mode": "LIVE"})
        self.assertEqual(extra.status_code, 400)
        approved = self.client.post("/api/phantom/exit-policy", headers=AUTH, json={"approved": True})
        self.assertEqual(approved.status_code, 200, approved.text)
        body = approved.json()
        self.assertTrue(body["approved"])
        self.assertTrue(body["effective"])
        self.assertEqual(body["rules"]["stop_pct_from_entry"], "6")
        self.assertEqual(body["rules"]["trail_arm_profit_pct"], "20")
        self.assertEqual(body["rules"]["trail_drop_from_peak_pct"], "12")
        self.assertEqual(body["rules"]["timeout_h"], "3")
        self.assertEqual(body["rules"]["timeout_min_profit_pct"], "5")
        self.assertEqual(body["rules"]["allocation_usd"], "100.00")
        self.assertEqual(body["rules"]["ticket_usd"], "8.00")
        again = self.client.get("/api/phantom/exit-policy", headers=AUTH).json()
        self.assertTrue(again["effective"])
        self.assertEqual(db.get_setting("mode"), "PAPER")
        self.assertEqual(db.get_setting("agents_enabled"), "0")
        self.assertEqual(db.query("SELECT value FROM phantom_control WHERE key = 'halt'")[0]["value"], "1")
        self.assertEqual(db.get_setting("stop_pct"), "6")
        self.assertEqual(phantom_runner.BANKROLL.__str__(), "100.00")
        self.assertEqual(phantom_runner.public_status()["loss_policy"], "not_approved")
        self.assertFalse(phantom_runner.public_status()["live_ready"])
        db.set_setting("stop_pct", "7", "test")
        drifted = self.client.get("/api/phantom/exit-policy", headers=AUTH).json()
        self.assertTrue(drifted["approved"])
        self.assertFalse(drifted["matches_settings"])
        self.assertFalse(drifted["effective"])
        self.assertEqual(drifted["rules"]["stop_pct_from_entry"], "6")
        refused = self.client.post("/api/phantom/exit-policy", headers=AUTH, json={"approved": True})
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(db.get_setting("stop_pct"), "7")
        db.set_setting("stop_pct", "6", "test")
        revoked = self.client.post("/api/phantom/exit-policy", headers=AUTH, json={"approved": False}).json()
        self.assertFalse(revoked["approved"])
        self.assertTrue(revoked["revoked"])
        self.assertFalse(revoked["effective"])
        self.assertIsNone(revoked["rules"])
        self.assertFalse(self.client.get("/api/phantom/exit-policy", headers=AUTH).json()["approved"])
        self.assertEqual(db.get_setting("mode"), "PAPER")
        page = __import__("pathlib").Path("web/app.js").read_text()
        self.assertIn("LIVE stays off", page)

    def test_prepared_transaction_inspection_cannot_send(self):
        import execution_stage
        stage = execution_stage.status()
        self.assertEqual(stage["signature_status_command"], "absent")
        self.assertEqual(stage["signing"], "unimplemented")
        self.assertFalse(stage["held_quote_executable"])
        noted = execution_stage.note_inspection({
            "blockhash": "1" * 32,
            "wallet_bound": True,
            "simulated": "pending_confirmation",
        })
        self.assertEqual(noted["chain_validity"], "blockhash_only")
        self.assertFalse(noted["last_valid_block_height_present"])
        self.assertFalse(noted["send"])
        self.assertFalse(noted["executable"])
        self.assertFalse(noted["live_ready"])
        with self.assertRaises(ValueError):
            execution_stage.note_inspection({"transactionData": "abc", "blockhash": "1" * 32})
