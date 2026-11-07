"""PAPER quote records. No Phantom call, no execution, no LIVE unlock."""
import os
import unittest

from fastapi.testclient import TestClient

import db
from tests.support import temp_desk_env

TOKEN = "dummy-app-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
WALLET = "4tKiUbkTbbcPqrTDsjmMWgZWZ7mZucMKJqk2nHE2HP4f"


class TestQuotes(unittest.TestCase):
    def setUp(self):
        self.tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        os.environ["APP_TOKEN"] = TOKEN
        os.environ.pop("DESK_AGENTS", None)
        self.addCleanup(lambda: os.environ.pop("APP_TOKEN", None))
        db.init_db()
        db.set_setting("mode", "PAPER", "test")
        db.set_setting("agents_enabled", "0", "test")
        import server
        self.client = TestClient(server.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_request_is_quote_only_and_idempotent(self):
        self.assertEqual(self.client.post("/api/quote/request", json={"id": "q1", "amount": "0.01", "native": True}).status_code, 401)
        refused = self.client.post("/api/quote/request", headers=AUTH, json={
            "id": "q1", "amount": "0.01", "native": True, "execute": True,
        })
        self.assertEqual(refused.status_code, 400)
        malicious = self.client.post("/api/quote/request", headers=AUTH, json={
            "id": "q1", "amount": "0.01", "native": True, "signature": "signed",
        })
        self.assertEqual(malicious.status_code, 400)
        self.assertEqual(db.query("SELECT id FROM quotes"), [])
        first = self.client.post("/api/quote/request", headers=AUTH, json={
            "id": "q1", "amount": "0.01", "native": True, "chain": "solana:mainnet", "slippage": 1,
        })
        self.assertEqual(first.status_code, 200)
        body = first.json()
        self.assertEqual(body["label"], "PAPER · quote only")
        self.assertFalse(body["execute"])
        self.assertEqual(body["bridge"], "not_connected")
        self.assertEqual(body["status"], "pending")
        again = self.client.post("/api/quote/request", headers=AUTH, json={
            "id": "q1", "amount": "0.01", "native": True,
        })
        self.assertEqual(again.json()["id"], "q1")
        self.assertEqual(len(db.query("SELECT id FROM quotes")), 1)
        self.assertEqual(db.get_setting("mode"), "PAPER")
        self.assertEqual(db.get_setting("agents_enabled"), "0")

    def test_result_rejects_execution_and_records_a_quote(self):
        self.client.post("/api/quote/request", headers=AUTH, json={"id": "q2", "amount": "1", "mint": WALLET})
        executed = self.client.post("/api/quote/result", headers=AUTH, json={
            "id": "q2", "execute": "true", "signature": "sig", "rawTransaction": "raw",
        })
        self.assertEqual(executed.status_code, 400)
        self.assertNotIn("sig", executed.text)
        self.assertEqual(db.query("SELECT status FROM quotes WHERE id = 'q2'")[0]["status"], "pending")
        hidden = self.client.post("/api/quote/result", headers=AUTH, json={
            "id": "q2", "wallet": WALLET, "out_amount": "2", "expiry": 9_999_999_999,
            "execution": {"signature": "sig"},
        })
        self.assertEqual(hidden.status_code, 400)
        self.assertIsNone(db.query("SELECT wallet FROM quotes WHERE id = 'q2'")[0]["wallet"])
        import time
        ok = self.client.post("/api/quote/result", headers=AUTH, json={
            "id": "q2", "wallet": WALLET, "out_amount": "2", "expiry": time.time() + 600, "slippage": 0.5,
        })
        self.assertEqual(ok.status_code, 200)
        saved = ok.json()
        self.assertEqual(saved["status"], "quoted")
        self.assertEqual(saved["label"], "PAPER · quote only")
        self.assertFalse(saved["execute"])
        self.assertNotIn("signature", saved)
        account = self.client.get("/api/account", headers=AUTH).json()
        self.assertEqual(account["quotes"][0]["status"], "quoted")
        self.assertEqual(account["mode"], "PAPER")
        self.assertEqual(db.get_setting("stop_pct"), "6")
        self.assertEqual(db.get_setting("min_score"), "70")
