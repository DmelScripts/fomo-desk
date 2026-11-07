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
REAL_WALLET = "GLmLe3gpVw87nSeA8YY93megzW6gDSeavHys3BKQ61fh"
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
        fractional = self._request("q-base")
        fractional["amountUnit"] = "base"
        fractional["amount"] = "1.5"
        self.assertEqual(self.client.post("/api/quote/request", headers=AUTH, json=fractional).status_code, 400)

    def test_real_provider_quote_is_observed_and_not_executable(self):
        created = self.client.post("/api/quote/request", headers=AUTH, json=self._request("hist")).json()
        self.assertEqual(created["tool_args"]["amount"], "0.01")
        self.assertEqual(created["tool_args"]["amountUnit"], "ui")
        self.assertFalse(created["tool_args"]["execute"])
        payload = "TXDATA-DO-NOT-STORE"
        dirty = self._real_result(created)
        dirty["quote"]["quoteResponse"]["quotes"][0]["transactionData"] = payload
        saved = self.client.post("/api/quote/result", headers=AUTH, json=dirty)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertNotIn(payload, saved.text)
        self.assertNotIn("transactionData", saved.text)
        self.assertNotIn("rawTransaction", saved.text)
        body = saved.json()
        self.assertEqual(body["status"], "observed")
        self.assertFalse(body["usable"])
        self.assertFalse(body["executable"])
        self.assertFalse(body["execute"])
        self.assertTrue(body["provider_expiry_unknown"])
        self.assertIsNone(body["expiry"])
        self.assertIsNone(body["local_ttl"])
        self.assertEqual(body["provenance"], "manual_import")
        self.assertEqual(body["bridge"], "not_connected")
        self.assertEqual(body["provider"], "phantom")
        self.assertEqual(body["wallet"], REAL_WALLET)
        self.assertEqual(body["provider_quote_id"], "e09cf162-9f00-40fa-9c36-0714a7c5fa94")
        self.assertEqual(body["sell_amount_base"], "10000000")
        self.assertEqual(body["out_amount"], "891179")
        self.assertEqual(body["route_provider"], "OKX")
        self.assertEqual(body["provider_chain"], "solana:101")
        self.assertEqual(body["quote_session_id"], "67346b2e-fac9-49d5-8d9c-7a949d1864c7")
        self.assertTrue(body["transaction_material_omitted"])
        self.assertEqual(
            [(fee["name"], fee["amount"]) for fee in body["fees"]],
            [("Phantom fee", "7640"), ("Tip payment", "6000"), ("Priority fee", "82880")],
        )
        self.assertAlmostEqual(body["proposed_slippage"], 0.75)
        self.assertIsNotNone(body["observed_at"])
        stored = db.query("SELECT status, expiry, import_json FROM quotes WHERE id = 'hist'")[0]
        self.assertEqual(stored["status"], "observed")
        self.assertIsNone(stored["expiry"])
        self.assertNotIn(payload, stored["import_json"])
        account = self.client.get("/api/account", headers=AUTH).json()["quotes"][0]
        self.assertTrue(account["provider_expiry_unknown"])
        self.assertFalse(account["usable"])
        self.assertFalse(account["executable"])
        render = Path("web/app.js").read_text().split("function renderQuotes")[1].split("function renderCommand")[0]
        for label in ("provider expiry unknown", "not executable", "row.fees", "row.wallet", "row.slippage"):
            self.assertIn(label, render)
        mismatch = self._real_result(created)
        mismatch["quote"]["quoteRequest"]["sellAmount"] = "999"
        mismatch["quote"]["quoteResponse"]["quotes"][0]["sellAmount"] = "999"
        self.assertEqual(self.client.post("/api/quote/result", headers=AUTH, json=mismatch).status_code, 400)
        self.assertEqual(db.query("SELECT provider_quote_id FROM quotes WHERE id = 'hist'")[0]["provider_quote_id"], "e09cf162-9f00-40fa-9c36-0714a7c5fa94")
        other = self.client.post("/api/quote/request", headers=AUTH, json=self._request("hist2")).json()
        bad_taker = self._real_result(other)
        bad_taker["quote"]["quoteRequest"]["taker"]["address"] = WALLET
        bad_taker["quote"]["quoteResponse"]["taker"]["address"] = WALLET
        refused = self.client.post("/api/quote/result", headers=AUTH, json=bad_taker)
        self.assertEqual(refused.status_code, 400)
        self.assertNotIn(WALLET, refused.text)
        self.assertEqual(db.query("SELECT status FROM quotes WHERE id = 'hist2'")[0]["status"], "pending")
        raw = self._real_result(other)
        raw["quote"]["quoteResponse"]["quotes"][0]["rawTransaction"] = payload
        omitted = self.client.post("/api/quote/result", headers=AUTH, json=raw)
        self.assertEqual(omitted.status_code, 200, omitted.text)
        self.assertNotIn(payload, omitted.text)
        self.assertTrue(omitted.json()["transaction_material_omitted"])
        self.assertFalse(omitted.json()["executable"])
        third = self.client.post("/api/quote/request", headers=AUTH, json=self._request("hist3")).json()
        signed = self._real_result(third)
        signed["quote"]["quoteResponse"]["quotes"][0]["signature"] = payload
        blocked = self.client.post("/api/quote/result", headers=AUTH, json=signed)
        self.assertEqual(blocked.status_code, 400)
        self.assertNotIn(payload, blocked.text)
        self.assertIsNone(db.query("SELECT wallet FROM quotes WHERE id = 'hist3'")[0]["wallet"])
        zero = self._real_result(other)
        zero["quote"]["quoteResponse"]["quotes"][0]["buyAmount"] = "0"
        self.assertEqual(self.client.post("/api/quote/result", headers=AUTH, json=zero).status_code, 400)
        self.assertEqual(db.get_setting("mode"), "PAPER")
        self.assertEqual(db.get_setting("agents_enabled"), "0")
        self.assertEqual(db.get_setting("stop_pct"), "6")
        self.assertEqual(db.get_setting("min_score"), "70")

    def test_version_8_quote_row_keeps_its_pair(self):
        self.client.post("/api/quote/request", headers=AUTH, json=self._request("keep"))
        conn = db.connect()
        try:
            conn.execute("ALTER TABLE quotes RENAME TO quotes_v9")
            conn.execute(
                """
                CREATE TABLE quotes (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    label TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    wallet TEXT,
                    amount TEXT NOT NULL,
                    amount_unit TEXT NOT NULL,
                    sell_chain TEXT NOT NULL,
                    buy_chain TEXT NOT NULL,
                    sell_native INTEGER NOT NULL,
                    buy_native INTEGER NOT NULL,
                    sell_mint TEXT,
                    buy_mint TEXT,
                    sell_decimals INTEGER,
                    buy_decimals INTEGER,
                    exact_out INTEGER NOT NULL,
                    slippage REAL NOT NULL,
                    expiry REAL,
                    out_amount TEXT,
                    provider_quote_id TEXT,
                    failure TEXT,
                    request_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO quotes (
                    id, status, label, provider, provenance, wallet, amount, amount_unit,
                    sell_chain, buy_chain, sell_native, buy_native, sell_mint, buy_mint,
                    sell_decimals, buy_decimals, exact_out, slippage, expiry, out_amount,
                    provider_quote_id, failure, request_json, created_at, updated_at
                )
                SELECT id, status, label, provider, provenance, wallet, amount, amount_unit,
                    sell_chain, buy_chain, sell_native, buy_native, sell_mint, buy_mint,
                    sell_decimals, buy_decimals, exact_out, slippage, expiry, out_amount,
                    provider_quote_id, failure, request_json, created_at, updated_at
                FROM quotes_v9
                """
            )
            conn.execute("DROP TABLE quotes_v9")
            conn.execute("PRAGMA user_version=8")
            conn.commit()
        finally:
            conn.close()
        db.init_db()
        row = db.query("SELECT id, status, amount FROM quotes WHERE id = 'keep'")[0]
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["amount"], "0.01")
        conn = db.connect()
        try:
            columns = [info[1] for info in conn.execute("PRAGMA table_info(quotes)")]
        finally:
            conn.close()
        self.assertIn("observed_at", columns)
        self.assertIn("import_json", columns)
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

    def _real_result(self, created):
        usdc = {
            "chainId": "solana:101",
            "resourceType": "address",
            "address": MINT,
        }
        native = {"chainId": "solana:101", "resourceType": "nativeToken", "slip44": "501"}
        wrapped = {
            "chainId": "solana:101",
            "resourceType": "address",
            "address": "So11111111111111111111111111111111111111112",
        }
        taker = {"chainId": "solana:101", "resourceType": "address", "address": REAL_WALLET}
        return {
            "id": created["id"],
            "wallet_addresses": {"addresses": [{"addressType": "Solana", "address": REAL_WALLET}]},
            "quote": {
                "quoteRequest": {
                    "taker": dict(taker),
                    "buyToken": dict(usdc),
                    "sellToken": dict(native),
                    "sellAmount": "10000000",
                    "slippageTolerance": 1,
                    "exactOut": False,
                    "autoSlippage": False,
                    "base64EncodedTx": False,
                },
                "quoteResponse": {
                    "type": "solana",
                    "quotes": [{
                        "quoteId": "e09cf162-9f00-40fa-9c36-0714a7c5fa94",
                        "sellAmount": "10000000",
                        "buyAmount": "891179",
                        "slippageTolerance": 1,
                        "priceImpact": 0,
                        "simulationFailed": False,
                        "baseProvider": {"id": "okx", "name": "OKX"},
                        "fees": [
                            {"name": "Phantom fee", "amount": 7640, "type": "phantom", "percentage": 0.0085, "token": dict(usdc)},
                            {"name": "Tip payment", "amount": 6000, "type": "tips", "percentage": 0, "token": dict(native)},
                            {"name": "Priority fee", "amount": 82880, "type": "priority_fee", "percentage": 0, "token": dict(native)},
                        ],
                        "balanceSufficiency": "sufficient",
                    }],
                    "taker": dict(taker),
                    "buyToken": dict(usdc),
                    "sellToken": dict(wrapped),
                    "slippageTolerance": 1,
                    "proposedSlippage": 0.75,
                    "quoteSessionId": "67346b2e-fac9-49d5-8d9c-7a949d1864c7",
                    "requestId": "11b95f6f-068f-4932-813b-24409797745a",
                },
            },
        }

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
