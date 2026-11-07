"""Read-only balance and execute:false quote checks. The binary is a fake."""
import json
import os
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import db
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
  if [ "$mode" = "other" ]; then
    echo '{"items":[{"symbol":"SOL","totalQuantityString":"1","spamStatus":"VERIFIED","logoUri":"https://cdn.example/secret","queriedWalletBalances":[{"address":"GLmLe3gpVw87nSeA8YY93megzW6gDSeavHys3BKQ61fh","quantityString":"1"}]}]}'
    exit 0
  fi
  echo '{"items":[{"symbol":"SOL","totalQuantityString":"1000000000","spamStatus":"VERIFIED","logoUri":"https://cdn.example/logo","queriedWalletBalances":[{"address":"'"$WALLET"'","quantityString":"1000000000"}],"session":"nope"}]}'
  exit 0
fi
if [ "$1" = "buy" ]; then
  echo '{"quoteRequest":{"taker":{"address":"'"$WALLET"'"},"execute":false},"quoteResponse":{"quotes":[{"quoteId":"pq-new","sellAmount":"10000000","buyAmount":"1","transactionData":"TXDATA-DO-NOT-STORE"}]}}'
  exit 0
fi
exit 2
""".replace("$WALLET", WALLET),
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
        db.set_setting("mode", "PAPER", "test")
        db.set_setting("agents_enabled", "0", "test")
        import server
        self.client = TestClient(server.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

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
            "quoteRequest": {"taker": {"address": WALLET}, "walletId": WALLET_ID},
            "quoteResponse": {"quotes": [{"quoteId": "pq-exp", "expiresAt": 1700000000}]},
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

    def _args(self) -> str:
        path = self.home / "args"
        return path.read_text(encoding="utf-8") if path.is_file() else ""
