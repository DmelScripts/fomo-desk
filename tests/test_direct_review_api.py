"""The public review endpoint cannot accept a caller's transaction or RPC URL."""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import db
import solana_direct_review
from tests.support import temp_desk_env


class DirectReviewApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        environment = patch.dict(os.environ, {"APP_TOKEN": "test-review-token"})
        environment.start()
        self.addCleanup(environment.stop)
        db.init_db()
        import server
        self.client = TestClient(server.app)
        self.path = "/api/phantom/direct-review"
        self.auth = {"Authorization": "Bearer test-review-token"}

    def test_auth_and_untrusted_fields_rejected_before_rpc(self):
        with patch("solana_direct_review.prepare") as prepare:
            self.assertEqual(self.client.post(self.path).status_code, 401)
            for body in ({"execute": True}, {"rpc_url": "https://example.com"}, {"transaction": "caller-bytes"}, {"simulate": True}):
                self.assertEqual(self.client.post(self.path, headers=self.auth, json=body).status_code, 400)
            prepare.assert_not_called()

    def test_only_safe_metadata_returned_without_enabling_agents(self):
        report = {**solana_direct_review.status(), "review_valid": True, "transaction_sha256": "a" * 64}
        review = SimpleNamespace(report=lambda: report, transaction_b64="private-to-process")
        with patch("solana_direct_review.prepare", return_value=review) as prepare:
            result = self.client.post(self.path, headers=self.auth, json={})
            prepare.assert_called_once_with()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers["cache-control"], "no-store")
        self.assertNotIn("private-to-process", result.text)
        self.assertFalse(result.json()["send"])
        self.assertFalse(result.json()["live_ready"])
        self.assertEqual(db.get_setting("mode"), "PAPER")
        self.assertEqual(db.get_setting("agents_enabled"), "0")

    def test_failed_review_has_no_partial_authority(self):
        with patch("solana_direct_review.prepare", side_effect=solana_direct_review.ReviewError("snapshot_context_invalid")):
            result = self.client.post(self.path, headers=self.auth)
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json()["reason"], "snapshot_context_invalid")
        self.assertFalse(result.json()["execution_allowed"])
        self.assertFalse(result.json()["send"])

    def test_receipt_review_accepts_only_authenticated_recorded_intent(self):
        path = "/api/phantom/receipt-review"
        self.assertEqual(self.client.post(path, json={"intent_id": "one"}).status_code, 401)
        for body in ({}, {"intent_id": ""}, {"intent_id": "one", "signature": "caller-signature"}, {"intent_id": "one", "rpc_url": "https://example.com"}):
            self.assertEqual(self.client.post(path, headers=self.auth, json=body).status_code, 400)
        # Missing receipts return before any network read; they cannot turn an
        # arbitrary caller-supplied signature into a server reservation.
        with patch("httpx.post", side_effect=AssertionError("unrecorded receipt used RPC")):
            response = self.client.post(path, headers=self.auth, json={"intent_id": "missing"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json()["status"], "not_run")
        self.assertFalse(response.json()["send"])
        self.assertFalse(response.json()["live_ready"])

    def test_limited_simulation_endpoint_rejects_untrusted_trade_inputs(self):
        path = "/api/phantom/sol-usdc-simulation"
        with patch("limited_simulation.run") as run:
            self.assertEqual(self.client.post(path, json={"intent_id": "one"}).status_code, 401)
            for body in ({}, {"intent_id": ""}, {"intent_id": "one", "buy_mint": "other"}, {"intent_id": "one", "execute": True}):
                self.assertEqual(self.client.post(path, headers=self.auth, json=body).status_code, 400)
            run.assert_not_called()
            run.return_value = {"status": "sim_closed", "send": False, "live_ready": False}
            response = self.client.post(path, headers=self.auth, json={"intent_id": "one"})
            self.assertEqual(response.status_code, 200)
            run.assert_called_once_with("one")
