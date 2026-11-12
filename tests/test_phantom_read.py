"""Read-only balance and execute:false quote checks. The binary is a fake."""
import base64
import json
import os
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import db
import phantom_auth
import phantom_read
import phantom_transport
from tests.support import temp_desk_env

TOKEN = "dummy-app-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
WALLET = phantom_transport.REQUIRED_WALLET
WALLET_ID = phantom_transport.REQUIRED_WALLET_ID
OLD = "GLmLe3gpVw87nSeA8YY93megzW6gDSeavHys3BKQ61fh"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


class TestPhantomRead(unittest.TestCase):
    def setUp(self):
        self.tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        self.home = Path(self.tmp.name) / "phantom-home"
        self.home.mkdir()
        self.bin = Path(self.tmp.name) / "phantom"
        self.bin.write_text(
            """#!/bin/sh
printf '%s\\n' "$@" >> "$HOME/args"
mode=$(cat "$HOME/mode" 2>/dev/null || echo balance)
if [ "$1" = "login" ]; then
  echo login >&2
  exit 2
fi
if [ "$1" = "wallet" ]; then
  if [ "$mode" = "auth" ]; then
    echo "Failed to get wallet addresses: status code 401" >&2
    exit 1
  fi
  if [ "$mode" = "empty" ]; then
    echo '{"items":[]}'
    exit 0
  fi
  if [ "$mode" = "other" ]; then
    echo '{"items":[{"symbol":"SOL","totalQuantityString":"1","spamStatus":"VERIFIED","logoUri":"https://cdn.example/secret","queriedWalletBalances":[{"address":"GLmLe3gpVw87nSeA8YY93megzW6gDSeavHys3BKQ61fh","quantityString":"1"}]}]}'
    exit 0
  fi
  if [ "$mode" = "mixed" ]; then
    echo '{"items":[{"symbol":"SOL","spamStatus":"VERIFIED","totalQuantityString":"1004","queriedWalletBalances":[{"address":"'"$WALLET"'","quantityString":"5"},{"address":"GLmLe3gpVw87nSeA8YY93megzW6gDSeavHys3BKQ61fh","quantityString":"999"}]},{"symbol":"SCAM","spamStatus":"SPAM","totalQuantityString":"1","queriedWalletBalances":[{"address":"'"$WALLET"'","quantityString":"1"}]}]}'
    exit 0
  fi
  echo '{"items":[{"symbol":"SOL","totalQuantityString":"1000000000","spamStatus":"VERIFIED","logoUri":"https://cdn.example/logo","queriedWalletBalances":[{"address":"'"$WALLET"'","quantityString":"1000000000"}],"session":"nope"}]}'
  exit 0
fi
if [ "$1" = "buy" ]; then
  if [ "$mode" = "auth" ]; then
    echo "Request failed with status code 401" >&2
    exit 1
  fi
  if [ "$mode" = "down" ]; then
    echo "upstream unavailable" >&2
    exit 1
  fi
  if [ "$mode" = "prepared" ]; then
    cat "$HOME/prepared.json"
    exit 0
  fi
  echo '{"quoteRequest":{"taker":{"chainId":"solana:101","resourceType":"address","address":"'"$WALLET"'"},"buyToken":{"chainId":"solana:101","resourceType":"address","address":"'"$USDC"'"},"sellToken":{"chainId":"solana:101","resourceType":"nativeToken","slip44":"501"},"sellAmount":"10000000","base64EncodedTx":false,"execute":false},"quoteResponse":{"quotes":[{"quoteId":"pq-new","sellAmount":"10000000","buyAmount":"891179","fees":[{"amount":"7640"}],"transactionData":"TXDATA-DO-NOT-STORE"}]}}'
  exit 0
fi
exit 2
""".replace("$WALLET", WALLET).replace("$USDC", USDC),
            encoding="utf-8",
        )
        self.bin.chmod(0o755)
        os.environ["APP_TOKEN"] = TOKEN
        os.environ["DESK_PHANTOM_BIN"] = str(self.bin)
        os.environ["DESK_PHANTOM_HOME"] = str(self.home)
        os.environ.pop("DESK_PHANTOM_PAPER", None)
        self.addCleanup(lambda: os.environ.pop("APP_TOKEN", None))
        self.addCleanup(lambda: os.environ.pop("DESK_PHANTOM_BIN", None))
        self.addCleanup(lambda: os.environ.pop("DESK_PHANTOM_HOME", None))
        db.init_db()
        phantom_auth.reset_for_tests()
        self.addCleanup(phantom_auth.reset_for_tests)
        db.set_setting("mode", "PAPER", "test")
        db.set_setting("agents_enabled", "0", "test")
        import server
        self.client = TestClient(server.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        phantom_auth.reset_for_tests()
        for name in ("calls", "args"):
            path = self.home / name
            if path.exists():
                path.write_text("", encoding="utf-8")

    def test_balance_is_readonly_and_bound_to_the_allowlist(self):
        self.assertEqual(self.client.post("/api/phantom/balance", json={}).status_code, 401)
        shown = self.client.post("/api/phantom/balance", headers=AUTH, json={})
        self.assertEqual(shown.status_code, 200, shown.text)
        self.assertEqual(shown.headers.get("cache-control"), "no-store")
        body = shown.json()
        self.assertEqual(body["status"], "observed")
        self.assertEqual(body["items"][0]["symbol"], "SOL")
        self.assertEqual(body["items"][0]["total_quantity"], "1000000000")
        self.assertFalse(body["send"])
        self.assertFalse(body["executable"])
        self.assertNotIn("logo", shown.text)
        self.assertNotIn("session", shown.text)
        self.assertNotIn("login", self._args())
        account = self.client.get("/api/account", headers=AUTH)
        self.assertNotIn("1000000000", account.text)
        (self.home / "mode").write_text("other", encoding="utf-8")
        other = self.client.post("/api/phantom/balance", headers=AUTH, json={}).json()
        self.assertEqual(other["status"], "mismatch")
        self.assertNotIn("items", other)
        self.assertEqual(other["wallet"], WALLET)
        self.assertEqual(phantom_transport.REQUIRED_WALLET, WALLET)
        (self.home / "mode").write_text("auth", encoding="utf-8")
        failed = self.client.post("/api/phantom/balance", headers=AUTH, json={}).json()
        self.assertEqual(failed["status"], "auth_error")
        self.assertNotIn("login", self._args())
        os.environ["DESK_PHANTOM_BIN"] = "/tmp/phantom-binary-missing"
        missing = self.client.post("/api/phantom/balance", headers=AUTH, json={})
        self.assertEqual(missing.status_code, 409)
        self.assertEqual(db.get_setting("paper_start_stack"), "200")
        self.assertEqual(db.get_setting("agents_enabled"), "0")
        self.assertEqual(db.query("SELECT value FROM phantom_control WHERE key = 'halt'")[0]["value"], "1")

    def test_quote_is_execute_false_and_stays_unusable_without_expiry(self):
        os.environ["DESK_PHANTOM_BIN"] = str(self.bin)
        shown = self.client.post("/api/phantom/quote", headers=AUTH, json={})
        self.assertEqual(shown.status_code, 200, shown.text)
        self.assertEqual(shown.headers.get("cache-control"), "no-store")
        body = shown.json()
        self.assertEqual(body["status"], "observed")
        self.assertTrue(body["provider_expiry_unknown"])
        self.assertIsNone(body["provider_expiry"])
        self.assertFalse(body["usable"])
        self.assertFalse(body["executable"])
        self.assertFalse(body["send"])
        self.assertFalse(body["live_ready"])
        self.assertEqual(body["quote_id"], "pq-new")
        self.assertEqual(body["chain"], "solana:101")
        self.assertEqual(body["sell"], "native")
        self.assertEqual(body["buy_mint"], USDC)
        self.assertEqual(body["sell_amount"], "10000000")
        self.assertEqual(body["buy_amount"], "891179")
        self.assertIsNone(body["buy_decimals"])
        self.assertNotIn("usd", shown.text.lower())
        self.assertNotIn("7640", shown.text)
        self.assertTrue(body["transaction_material_omitted"])
        self.assertNotIn("TXDATA-DO-NOT-STORE", shown.text)
        args = self._args().splitlines()
        self.assertEqual(args[args.index("--execute") + 1], "false")
        self.assertEqual(args[args.index("--base64-encoded-tx") + 1], "false")
        self.assertEqual(args[args.index("--wallet-id") + 1], WALLET_ID)
        self.assertEqual(args[args.index("--taker") + 1], WALLET)
        self.assertNotIn("--execute true", " ".join(args))
        old = phantom_read._quote(json.dumps({
            "quoteRequest": {"taker": {"address": OLD}},
            "quoteResponse": {"quotes": [{"quoteId": "old-quote"}]},
        }), 0)
        self.assertEqual(old["status"], "mismatch")
        self.assertEqual(old["observed_wallet"], OLD)
        self.assertFalse(old["usable"])
        self.assertEqual(phantom_transport.REQUIRED_WALLET, WALLET)
        self.assertEqual(db.get_setting("mode"), "PAPER")
        self.assertEqual(db.query("SELECT id FROM quotes"), [])
        extra = self.client.post("/api/phantom/quote", headers=AUTH, json={"amount": "1"})
        self.assertEqual(extra.status_code, 400)
        foreign = "11111111-2222-4333-8444-555555555555"
        wrong_wallet = phantom_read._quote(json.dumps({
            "quoteRequest": {"walletId": foreign, "taker": {"address": WALLET}},
            "quoteResponse": {"quotes": [{"quoteId": "foreign"}]},
        }), 0)
        self.assertEqual(wrong_wallet["status"], "mismatch")
        self.assertNotIn(foreign, json.dumps(wrong_wallet))
        self.assertFalse(wrong_wallet["executable"])
        dated = phantom_read._quote(json.dumps({
            "quoteRequest": self._request(),
            "quoteResponse": {"quotes": [{"quoteId": "pq-exp", "sellAmount": "10000000", "expiresAt": 1700000000}]},
        }), 0)
        self.assertEqual(dated["provider_expiry"], 1700000000)
        self.assertFalse(dated["provider_expiry_unknown"])
        self.assertFalse(dated["usable"])
        self.assertFalse(dated["executable"])
        self.assertFalse(dated["send"])
        signed = phantom_read._quote(json.dumps({
            "quoteRequest": {"taker": {"address": WALLET}},
            "quoteResponse": {"quotes": [{"quoteId": "sent"}]},
            "execution": {"signature": "SIGSECRET", "rawTransaction": "RAWTX"},
        }), 0)
        self.assertEqual(signed["status"], "blocked")
        self.assertNotIn("SIGSECRET", json.dumps(signed))
        self.assertNotIn("RAWTX", json.dumps(signed))
        self.assertNotIn("quote_id", signed)
        self.assertFalse(signed["executable"])
        import phantom_runner
        self.assertEqual(phantom_runner.BANKROLL, __import__("decimal").Decimal("100.00"))
        self.assertEqual(phantom_runner.SIZE, __import__("decimal").Decimal("8.00"))

    def test_balance_uses_the_allowlisted_row_and_drops_spam(self):
        (self.home / "mode").write_text("mixed", encoding="utf-8")
        shown = self.client.post("/api/phantom/balance", headers=AUTH, json={}).json()
        self.assertEqual(shown["items"], [{
            "symbol": "SOL",
            "total_quantity": "5",
            "spam_status": "VERIFIED",
        }])
        self.assertNotIn("1004", json.dumps(shown))
        self.assertNotIn("999", json.dumps(shown))
        self.assertNotIn("SCAM", json.dumps(shown))
        self.assertNotIn(OLD, json.dumps(shown))

    def test_quote_rejects_a_different_mint_and_does_not_login_on_401(self):
        self.assertEqual(self.client.post("/api/phantom/quote", json={}).status_code, 401)
        self.assertEqual(self._args(), "")
        other = phantom_read._quote(json.dumps({
            "quoteRequest": self._request(buy="So11111111111111111111111111111111111111112"),
            "quoteResponse": {"quotes": [{"quoteId": "wrong-mint"}]},
        }), 0)
        self.assertEqual(other["status"], "unable_to_verify")
        self.assertFalse(other["usable"])
        self.assertFalse(other["executable"])
        self.assertNotIn("quote_id", other)
        wrong_decimals = phantom_read._quote(json.dumps({
            "quoteRequest": self._request(decimals=9),
            "quoteResponse": {"quotes": [{"quoteId": "wrong-decimals", "sellAmount": "10000000"}]},
        }), 0)
        self.assertEqual(wrong_decimals["status"], "unable_to_verify")
        self.assertIsNone(wrong_decimals.get("buy_decimals"))
        devnet = self._request()
        devnet["sellToken"] = {"chainId": "solana:103", "resourceType": "nativeToken", "slip44": "501"}
        rejected = phantom_read._quote(json.dumps({
            "quoteRequest": devnet,
            "quoteResponse": {"quotes": [{"quoteId": "devnet", "sellAmount": "10000000"}]},
        }), 0)
        self.assertEqual(rejected["status"], "unable_to_verify")
        self.assertFalse(rejected["executable"])
        payload = phantom_read._quote(json.dumps({
            "quoteRequest": self._request(base64=True),
            "quoteResponse": {"quotes": [{"quoteId": "payload"}]},
        }), 0)
        self.assertEqual(payload["status"], "blocked")
        self.assertFalse(payload["executable"])
        with self.assertRaises(RuntimeError):
            phantom_read._run(["buy", "--execute", "true"])
        self.assertNotIn("true", self._args())
        (self.home / "mode").write_text("auth", encoding="utf-8")
        failed = self.client.post("/api/phantom/quote", headers=AUTH, json={}).json()
        self.assertEqual(failed["status"], "auth_error")
        self.assertNotIn("login", self._args())
        (self.home / "mode").write_text("down", encoding="utf-8")
        down = self.client.post("/api/phantom/quote", headers=AUTH, json={}).json()
        self.assertEqual(down["status"], "unable_to_verify")
        self.assertNotIn("upstream", json.dumps(down))
        self.assertNotIn("login", self._args())

    def test_preflight_keeps_the_unsigned_transaction_in_memory(self):
        import solana_inspect
        import solana_tx
        raw = bytearray(base64.b64decode(solana_tx.encode_legacy_transfer(
            WALLET, solana_tx.b58encode(b"\x22" * 32), 10_000_000, solana_tx.b58encode(b"\x11" * 32),
        )))
        program_at = 1 + 64 + 4 + 32 + 32
        raw[program_at:program_at + 32] = b"\x03" * 32
        tx = base64.b64encode(bytes(raw)).decode("ascii")
        (self.home / "prepared.json").write_text(json.dumps({
            "quoteRequest": {
                "taker": {"chainId": "solana:101", "resourceType": "address", "address": WALLET},
                "buyToken": {"chainId": "solana:101", "resourceType": "address", "address": USDC},
                "sellToken": {"chainId": "solana:101", "resourceType": "nativeToken", "slip44": "501"},
                "sellAmount": "10000000",
                "base64EncodedTx": True,
                "execute": False,
            },
            "quoteResponse": {"quotes": [{
                "quoteId": "pq-pre",
                "sellAmount": "10000000",
                "buyAmount": "891179",
                "transactionData": tx,
            }]},
        }), encoding="utf-8")
        (self.home / "mode").write_text("prepared", encoding="utf-8")
        calls = []

        def transport(payload):
            calls.append(payload["method"])
            if payload["method"] == "getGenesisHash":
                return {"result": solana_inspect.MAINNET_GENESIS}
            raise AssertionError(payload["method"])

        original = phantom_read._rpc
        phantom_read._rpc = lambda: solana_inspect.RpcClient(transport)
        self.addCleanup(lambda: setattr(phantom_read, "_rpc", original))
        self.assertEqual(self.client.post("/api/phantom/preflight", json={}).status_code, 401)
        shown = self.client.post("/api/phantom/preflight", headers=AUTH, json={})
        self.assertEqual(shown.status_code, 200, shown.text)
        self.assertEqual(shown.headers.get("cache-control"), "no-store")
        body = shown.json()
        self.assertEqual(body["error_category"], "unverified_program")
        self.assertIn(solana_tx.b58encode(b"\x03" * 32), body["program_ids"])
        self.assertFalse(body["slippage_verified"])
        self.assertFalse(body["execution_allowed"])
        self.assertFalse(body["send"])
        self.assertFalse(body["executable"])
        self.assertFalse(body["live_ready"])
        self.assertIsNone(body["provider_expiry"])
        self.assertFalse(body["freshness_guarantee"])
        self.assertNotIn(tx, shown.text)
        self.assertNotIn("transactionData", shown.text)
        self.assertEqual(calls, ["getGenesisHash"])
        args = self._args().splitlines()
        self.assertEqual(args[args.index("--execute") + 1], "false")
        self.assertEqual(args[args.index("--base64-encoded-tx") + 1], "true")
        self.assertEqual(args[args.index("--amount") + 1], "10000000")
        self.assertEqual(db.query("SELECT id FROM quotes"), [])
        self.assertNotIn("login", self._args())

    def test_restart_keeps_cached_auth_and_failures_stay_closed(self):
        shown = self.client.post("/api/phantom/balance", headers=AUTH, json={}).json()
        self.assertEqual(shown["items"][0]["total_quantity"], "1000000000")
        self.assertIsInstance(shown["items"][0]["total_quantity"], str)
        account = self.client.get("/api/account", headers=AUTH).json()["phantom"]
        self.assertEqual(account["auth"], "observed")
        self.assertEqual(account["auth_evidence"], "fresh")
        self.assertTrue(account["cached_match"] is None)
        self.assertEqual(account["transport"], "BLOCKED")
        self.assertEqual(account["execution_transport"], "unimplemented")
        self.assertFalse(account["readonly_quote_is_execution"])
        self.assertFalse(account["live_ready"])
        self.assertFalse(account["executable"])
        self.assertFalse(account["send"])
        self.assertEqual(account["loss_policy"], "approved")
        self.assertFalse(account["live_consent"])
        stored = self._evidence()
        self.assertEqual(set(stored), {"state", "source", "at"})
        self.assertEqual(stored["state"], "observed")
        self.assertEqual(stored["source"], "balance")
        self.assertNotIn("1000000000", json.dumps(stored))
        (self.home / "mode").write_text("empty", encoding="utf-8")
        empty = self.client.post("/api/phantom/balance", headers=AUTH, json={}).json()
        self.assertEqual(empty["status"], "unable_to_verify")
        self.assertEqual(phantom_auth.account_view()["auth_flow"], "observed")
        phantom_auth.reset_for_tests()
        restarted = self.client.get("/api/account", headers=AUTH).json()["phantom"]
        self.assertEqual(restarted["auth"], "cached_observed")
        self.assertEqual(restarted["auth_evidence"], "cached")
        self.assertTrue(restarted["cached_match"])
        self.assertFalse(restarted["live_ready"])
        self.assertEqual(restarted["transport"], "BLOCKED")
        self.assertEqual(restarted["execution_transport"], "unimplemented")
        self.assertNotEqual(restarted["auth"], "AWAITING_OWNER_AUTH_PROCEDURE")
        (self.home / "mode").write_text("auth", encoding="utf-8")
        failed = self.client.post("/api/phantom/balance", headers=AUTH, json={}).json()
        self.assertEqual(failed["status"], "auth_error")
        self.assertNotIn("login", self._args())
        self.assertEqual(phantom_auth.account_view()["auth_flow"], "auth_error")
        self.assertFalse(phantom_auth.account_view()["matches_allowlist"])
        phantom_auth.reset_for_tests()
        self.assertEqual(phantom_auth.account_view()["auth_flow"], "cached_auth_error")
        self.assertFalse(phantom_auth.account_view()["cached_match"])
        (self.home / "mode").write_text("other", encoding="utf-8")
        other = self.client.post("/api/phantom/balance", headers=AUTH, json={}).json()
        self.assertEqual(other["status"], "mismatch")
        self.assertEqual(phantom_transport.REQUIRED_WALLET, WALLET)
        self.assertNotIn(OLD, self._evidence_text())
        phantom_auth.reset_for_tests()
        self.assertEqual(phantom_auth.account_view()["auth_flow"], "cached_mismatch")
        self.assertIsNone(phantom_auth.account_view()["observed_wallet"])
        missing = phantom_auth._classify(json.dumps({"walletId": WALLET_ID}), 0)
        phantom_auth._apply_classification(missing)
        self.assertEqual(phantom_auth.account_view()["auth_flow"], "unable_to_verify")
        self.assertNotEqual(phantom_auth.account_view()["auth_flow"], "mismatch")
        phantom_auth.reset_for_tests()
        self.assertEqual(phantom_auth.account_view()["auth_flow"], "cached_unable_to_verify")
        floated = phantom_read._balance(json.dumps({
            "items": [{
                "symbol": "SOL",
                "spamStatus": "VERIFIED",
                "totalQuantity": 1.5,
                "queriedWalletBalances": [{"address": WALLET, "quantity": 1.5, "quantityString": "1.50"}],
            }],
        }), 0)
        self.assertEqual(floated["items"][0]["total_quantity"], "1.50")
        dropped = phantom_read._balance(json.dumps({
            "items": [{
                "symbol": "SOL",
                "spamStatus": "VERIFIED",
                "queriedWalletBalances": [{"address": WALLET, "quantity": 1.25}],
            }],
        }), 0)
        self.assertEqual(dropped["status"], "unable_to_verify")
        priced = phantom_read._quote(json.dumps({
            "quoteRequest": self._request(),
            "quoteResponse": {"quotes": [{
                "quoteId": "pq-amt",
                "sellAmount": "10000000",
                "buyAmount": 1.25,
                "fees": [{"amount": "7640"}],
            }]},
        }), 0)
        self.assertEqual(priced["status"], "observed")
        self.assertIsNone(priced["buy_amount"])
        self.assertNotIn("7640", json.dumps(priced))
        self.assertIsNone(priced["buy_decimals"])
        self.assertFalse(priced["executable"])

    def test_balance_and_quote_evidence_leave_the_auth_gate_open(self):
        import desk_loop
        balanced = self.client.post("/api/phantom/balance", headers=AUTH, json={}).json()
        self.assertEqual(balanced["status"], "observed")
        after_balance = phantom_auth.account_view()
        self.assertEqual(after_balance["evidence_source"], "balance")
        self.assertIsNone(after_balance["observed_wallet_id"])
        self.assertIsNone(after_balance["observed_wallet"])
        self.assertIsNone(after_balance["observed_at"])
        self.assertFalse(after_balance["matches_allowlist"])
        self.assertFalse(phantom_auth.server_auth_closed())
        quoted = self.client.post("/api/phantom/quote", headers=AUTH, json={}).json()
        self.assertEqual(quoted["status"], "observed")
        after_quote = phantom_auth.account_view()
        self.assertEqual(after_quote["evidence_source"], "quote")
        self.assertIsNone(after_quote["observed_wallet_id"])
        self.assertIsNone(after_quote["observed_wallet"])
        self.assertFalse(phantom_auth.server_auth_closed())
        gate = next(item for item in desk_loop.remaining_live_gates() if item["gate"] == "phantom_server_auth")
        self.assertFalse(gate["closed"])
        phantom_auth.note_evidence("observed", "balance")
        self.assertIsNone(phantom_auth.account_view()["observed_wallet_id"])
        self.assertNotEqual(phantom_auth.account_view()["observed_wallet"], WALLET)
        self.assertFalse(phantom_auth.server_auth_closed())

    def _evidence(self) -> dict:
        return json.loads(self._evidence_text())

    def _evidence_text(self) -> str:
        rows = db.query("SELECT value FROM phantom_control WHERE key = 'auth_evidence'")
        return rows[0]["value"]

    def _request(self, buy=USDC, decimals=None, base64=False):
        token = {"chainId": "solana:101", "resourceType": "address", "address": buy}
        if decimals is not None:
            token["decimals"] = decimals
        return {
            "taker": {"chainId": "solana:101", "resourceType": "address", "address": WALLET},
            "walletId": WALLET_ID,
            "buyToken": token,
            "sellToken": {"chainId": "solana:101", "resourceType": "nativeToken", "slip44": "501"},
            "sellAmount": "10000000",
            "base64EncodedTx": base64,
            "execute": False,
        }

    def _args(self) -> str:
        path = self.home / "args"
        return path.read_text(encoding="utf-8") if path.is_file() else ""
