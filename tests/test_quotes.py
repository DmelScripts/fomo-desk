"""PAPER quote contract. No Phantom call, no execution, no LIVE unlock."""
import os
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import db
from tests.support import temp_desk_env

TOKEN = "dummy-app-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
WALLET = "4tKiUbkTbbcPqrTDsjmMWgZWZ7mZucMKJqk2nHE2HP4f"
MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
OTHER = "So11111111111111111111111111111111111111112"


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

    def test_request_binds_the_pair_and_rejects_bad_amounts(self):
        self.assertEqual(self.client.post("/api/quote/request", json=self._request("q1")).status_code, 401)
        for payload in (
            {"execute": True},
            {"signature": "signed"},
            {"session": "cookie"},
            {"amount": "0.00"},
            {"amount": "0"},
            {"amount": "0.0"},
            {"amount": "NaN"},
            {"amount": "Infinity"},
            {"amount": "1e2"},
            {"amount": "-1"},
            {"amountUnit": "lamports"},
            {"slippageTolerance": True},
        ):
            body = self._request("q1")
            body.update(payload)
            refused = self.client.post("/api/quote/request", headers=AUTH, json=body)
            self.assertEqual(refused.status_code, 400, payload)
            self.assertNotIn("signed", refused.text)
        self.assertEqual(db.query("SELECT id FROM quotes"), [])
        missing_decimals = self._request("q1", sell_native=False, sell_mint=MINT, buy_mint=OTHER)
        missing_decimals.pop("sellTokenDecimals", None)
        self.assertEqual(self.client.post("/api/quote/request", headers=AUTH, json=missing_decimals).status_code, 400)
        first = self.client.post("/api/quote/request", headers=AUTH, json=self._request("q1"))
        self.assertEqual(first.status_code, 200)
        body = first.json()
        self.assertEqual(body["label"], "PAPER · manually imported Phantom quote")
        self.assertEqual(body["provenance"], "manual_import")
        self.assertEqual(body["bridge"], "not_connected")
        self.assertEqual(body["provider"], "phantom")
        self.assertFalse(body["execute"])
        self.assertFalse(body["usable"])
        self.assertEqual(body["status"], "pending")
        self.assertEqual(body["tool"], "buy")
        self.assertEqual(body["wallet_tool"], "wallet_addresses")
        self.assertFalse(body["tool_args"]["execute"])
        self.assertFalse(body["tool_args"]["autoSlippage"])
        self.assertFalse(body["tool_args"]["base64EncodedTx"])
        self.assertEqual(body["tool_args"]["amount"], "0.01")
        self.assertEqual(body["tool_args"]["amountUnit"], "ui")
        self.assertEqual(body["tool_args"]["sellTokenDecimals"], 9)
        self.assertTrue(body["tool_args"]["sellTokenIsNative"])
        self.assertEqual(body["tool_args"]["buyTokenMint"], MINT)
        self.assertEqual(body["tool_args"]["slippageTolerance"], 1)
        again = self.client.post("/api/quote/request", headers=AUTH, json=self._request("q1"))
        self.assertEqual(again.json()["tool_args"], body["tool_args"])
        changed = self._request("q1")
        changed["amount"] = "0.02"
        self.assertEqual(self.client.post("/api/quote/request", headers=AUTH, json=changed).status_code, 409)
        self.assertEqual(len(db.query("SELECT id FROM quotes")), 1)
        self.assertEqual(db.get_setting("mode"), "PAPER")
        self.assertEqual(db.get_setting("agents_enabled"), "0")

    def test_result_binds_wallet_quote_and_expires_on_read(self):
        created = self.client.post("/api/quote/request", headers=AUTH, json=self._request("q2")).json()
        executed = self.client.post("/api/quote/result", headers=AUTH, json={
            "id": "q2",
            "execute": True,
            "wallet_addresses": self._wallet(),
            "quote": {"quoteRequest": created["tool_args"], "quoteResponse": {}},
        })
        self.assertEqual(executed.status_code, 400)
        self.assertEqual(db.query("SELECT status, wallet FROM quotes WHERE id = 'q2'")[0]["status"], "pending")
        nested = self._result(created, route={"signature": "sig"})
        hidden = self.client.post("/api/quote/result", headers=AUTH, json=nested)
        self.assertEqual(hidden.status_code, 400)
        self.assertNotIn("sig", hidden.text)
        self.assertIsNone(db.query("SELECT wallet FROM quotes WHERE id = 'q2'")[0]["wallet"])
        session = self._result(created)
        session["wallet_addresses"]["session"] = "cookie"
        self.assertEqual(self.client.post("/api/quote/result", headers=AUTH, json=session).status_code, 400)
        mismatched = self._result(created)
        mismatched["quote"]["quoteRequest"] = dict(created["tool_args"])
        mismatched["quote"]["quoteRequest"]["amount"] = "9"
        self.assertEqual(self.client.post("/api/quote/result", headers=AUTH, json=mismatched).status_code, 400)
        other_wallet = self._result(created)
        other_wallet["quote"]["quoteRequest"] = dict(created["tool_args"], taker=OTHER)
        self.assertEqual(self.client.post("/api/quote/result", headers=AUTH, json=other_wallet).status_code, 400)
        self.assertIsNone(db.query("SELECT wallet FROM quotes WHERE id = 'q2'")[0]["wallet"])
        timeless = self._result(created)
        del timeless["quote"]["quoteResponse"]["expiry"]
        self.assertEqual(self.client.post("/api/quote/result", headers=AUTH, json=timeless).status_code, 400)
        ok = self.client.post("/api/quote/result", headers=AUTH, json=self._result(created))
        self.assertEqual(ok.status_code, 200, ok.text)
        saved = ok.json()
        self.assertEqual(saved["status"], "quoted")
        self.assertTrue(saved["usable"])
        self.assertEqual(saved["wallet"], WALLET)
        self.assertEqual(saved["provider_quote_id"], "pq-2")
        self.assertEqual(saved["provenance"], "manual_import")
        self.assertEqual(saved["bridge"], "not_connected")
        self.assertFalse(saved["execute"])
        self.assertGreater(saved["expiry"], time.time())
        account = self.client.get("/api/account", headers=AUTH).json()
        shown = account["quotes"][0]
        self.assertEqual(shown["provider"], "phantom")
        self.assertEqual(shown["wallet"], WALLET)
        self.assertEqual(shown["slippage"], 1)
        self.assertIsNotNone(shown["expiry"])
        self.assertEqual(account["mode"], "PAPER")
        db.execute("UPDATE quotes SET expiry = ? WHERE id = 'q2'", (time.time() - 5,))
        expired = self.client.get("/api/account", headers=AUTH).json()["quotes"][0]
        self.assertEqual(expired["status"], "expired")
        self.assertFalse(expired["usable"])
        self.assertEqual(db.query("SELECT status FROM quotes WHERE id = 'q2'")[0]["status"], "expired")
        self.assertEqual(db.get_setting("stop_pct"), "6")
        self.assertEqual(db.get_setting("min_score"), "70")
        self.assertEqual(db.get_setting("agents_enabled"), "0")
        source = Path("web/app.js").read_text()
        render = source.split("function renderQuotes")[1].split("function renderCommand")[0]
        for field in ("row.provider", "row.wallet", "row.slippage", "row.expiry"):
            self.assertIn(field, render)
        adapter = Path("quotes.py").read_text()
        for banned in ("httpx", "urllib", "web3", "solders", "broadcast_transaction"):
            self.assertNotIn(banned, adapter)

    def test_base_units_and_provider_expiry_shapes(self):
        base = self._request("q3")
        base["amountUnit"] = "base"
        base["amount"] = "1000"
        created = self.client.post("/api/quote/request", headers=AUTH, json=base)
        self.assertEqual(created.status_code, 200, created.text)
        args = created.json()["tool_args"]
        self.assertEqual(args["amountUnit"], "base")
        self.assertNotIn("sellTokenDecimals", args)
        self.assertFalse(args["execute"])
        millis = self._result(created.json())
        millis["quote"]["quoteResponse"] = {
            "quoteId": "pq-ms",
            "buyAmount": "4",
            "expiry": int((time.time() + 900) * 1000),
        }
        saved = self.client.post("/api/quote/result", headers=AUTH, json=millis)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["provider_quote_id"], "pq-ms")
        self.assertTrue(saved.json()["usable"])
        self.assertLess(saved.json()["expiry"], time.time() + 1000)
        iso = self._request("q4")
        iso["amount"] = "0.02"
        made = self.client.post("/api/quote/request", headers=AUTH, json=iso).json()
        stamped = self._result(made)
        when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 1200))
        stamped["quote"]["quoteResponse"] = {"id": "pq-iso", "details": {"expiresAt": when, "outAmount": "8"}}
        recorded = self.client.post("/api/quote/result", headers=AUTH, json=stamped)
        self.assertEqual(recorded.status_code, 200, recorded.text)
        self.assertEqual(recorded.json()["provider_quote_id"], "pq-iso")
        self.assertEqual(recorded.json()["out_amount"], "8")
        self.assertEqual(db.get_setting("mode"), "PAPER")
        self.assertEqual(db.get_setting("agents_enabled"), "0")

    def _request(self, quote_id, sell_native=True, sell_mint=None, buy_mint=MINT):
        body = {
            "id": quote_id,
            "sellChainId": "solana:mainnet",
            "buyChainId": "solana:mainnet",
            "amount": "0.01",
            "amountUnit": "ui",
            "slippageTolerance": 1,
            "buyTokenMint": buy_mint,
        }
        if sell_native:
            body["sellTokenIsNative"] = True
        else:
            body["sellTokenMint"] = sell_mint
            body["sellTokenDecimals"] = 6
        return body

    def _wallet(self):
        return {"addresses": [{"addressType": "solana", "address": WALLET}]}

    def _result(self, created, **response):
        payload = {
            "quoteId": "pq-2",
            "outAmount": "2",
            "expiry": time.time() + 600,
        }
        payload.update(response)
        return {
            "id": created["id"],
            "wallet_addresses": self._wallet(),
            "quote": {
                "quoteRequest": created["tool_args"],
                "quoteResponse": payload,
            },
        }
