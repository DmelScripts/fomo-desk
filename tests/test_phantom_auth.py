"""Device login is auth-only. The fake binary never reaches Phantom."""
import json
import logging
import os
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import db
import phantom_auth
import phantom_transport
from tests.support import temp_desk_env

TOKEN = "dummy-app-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SECRET = "STAMPER_SECRET=do-not-leak"


class TestPhantomAuth(unittest.TestCase):
    def setUp(self):
        self.tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        self.home = Path(self.tmp.name) / "phantom-home"
        self.home.mkdir()
        self.bin = Path(self.tmp.name) / "phantom"
        self.bin.write_text(
            """#!/bin/sh
echo "$1" >> "$HOME/calls"
if [ "$1" = "login" ]; then
  echo "Visit: https://connect.phantom.app/device-connect?user_code=ABCD-EFGH&client_id=desk-test&public_key=not-a-session" >&2
  echo "Code: ABCD-EFGH" >&2
  echo "STAMPER_SECRET=do-not-leak" >&2
  mode=$(cat "$HOME/mode" 2>/dev/null || echo approve)
  if [ "$mode" = "badurl" ]; then
    echo "Visit: https://evil.example/device-connect?user_code=ABCD-EFGH" >&2
    echo "Code: ABCD-EFGH" >&2
    echo "STAMPER_SECRET=do-not-leak" >&2
    exit 1
  fi
  if [ "$mode" = "hang" ]; then sleep 30; fi
  if [ "$mode" = "die" ]; then exit 1; fi
  exit 0
fi
if [ "$1" = "wallet" ]; then
  mode=$(cat "$HOME/mode" 2>/dev/null || echo approve)
  if [ "$mode" = "auth401" ]; then
    echo "Failed to get wallet addresses: 401" >&2
    exit 1
  fi
  if [ "$mode" = "mismatch" ]; then
    echo '{"walletId":"11111111-2222-4333-8444-555555555555","organizationId":"org","addresses":[{"addressType":"Solana","address":"4tKiUbkTbbcPqrTDsjmMWgZWZ7mZucMKJqk2nHE2HP4f"}]}'
    exit 0
  fi
  if [ "$mode" = "empty" ]; then
    exit 0
  fi
  if [ "$mode" = "toon" ]; then
    printf '%s\n' 'walletId: d76451f8-288c-4ab8-b6de-1cfd84403873' 'organizationId: org_1' 'addresses[1]{addressType,address}:' '  solana,GLmLe3gpVw87nSeA8YY93megzW6gDSeavHys3BKQ61fh'
    exit 0
  fi
  echo '{"walletId":"d76451f8-288c-4ab8-b6de-1cfd84403873","addresses":[{"addressType":"Solana","address":"GLmLe3gpVw87nSeA8YY93megzW6gDSeavHys3BKQ61fh"}]}'
  exit 0
fi
exit 2
""",
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
        phantom_auth.reset_for_tests()
        self.addCleanup(phantom_auth.reset_for_tests)
        db.init_db()
        db.set_setting("mode", "PAPER", "test")
        db.set_setting("agents_enabled", "0", "test")
        import server
        self.client = TestClient(server.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_login_requires_the_app_token_and_is_not_cached(self):
        self.assertEqual(self.client.post("/api/phantom/login", json={}).status_code, 401)
        self.assertEqual(self.client.get("/api/phantom/login").status_code, 401)
        refused = self.client.post("/api/phantom/login", headers=AUTH, json={"tool": "login", "execute": True})
        self.assertEqual(refused.status_code, 400)
        self.assertNotIn(SECRET, refused.text)
        (self.home / "mode").write_text("hang", encoding="utf-8")
        started = self.client.post("/api/phantom/login", headers=AUTH, json={})
        self.assertEqual(started.status_code, 200, started.text)
        self.assertEqual(started.headers.get("cache-control"), "no-store")
        self.assertIn("/device-connect?", started.json()["approval_url"])
        self.assertEqual(started.json()["user_code"], "ABCD-EFGH")
        self.assertNotIn(SECRET, started.text)
        self.assertFalse(started.json()["live_ready"])
        account = self.client.get("/api/account", headers=AUTH)
        self.assertNotIn("ABCD-EFGH", account.text)
        self.assertNotIn("device-connect", account.text)
        self.assertEqual(self.home.stat().st_mode & 0o777, 0o700)
        self.assertFalse(started.json()["executable"])
        again = self.client.post("/api/phantom/login", headers=AUTH, json={})
        self.assertEqual(again.json()["user_code"], "ABCD-EFGH")
        self.assertEqual(self._calls().count("login"), 1)
        logs = self._capture(lambda: phantom_auth.public_login())
        self.assertNotIn(SECRET, logs)
        self.assertNotIn("do-not-leak", logs)

    def test_bad_url_and_missing_binary_do_not_leak(self):
        (self.home / "mode").write_text("badurl", encoding="utf-8")
        secret_file = self.home / ".phantom-mcp"
        secret_file.mkdir()
        (secret_file / "session.json").write_text('{"stamper":"SESSIONFILESECRET"}', encoding="utf-8")
        started = self.client.post("/api/phantom/login", headers=AUTH, json={})
        self.assertEqual(started.status_code, 200, started.text)
        self._wait("failed")
        body = self.client.get("/api/phantom/login", headers=AUTH)
        self.assertIsNone(body.json().get("approval_url"))
        self.assertNotIn("evil.example", body.text)
        self.assertNotIn("SESSIONFILESECRET", body.text)
        self.assertNotIn(SECRET, body.text)
        self.assertNotIn("ABCD-EFGH", body.text)
        account = self.client.get("/api/account", headers=AUTH)
        self.assertNotIn("ABCD-EFGH", account.text)
        self.assertNotIn("device-connect", account.text)
        self.assertNotIn("SESSIONFILESECRET", account.text)
        os.environ["DESK_PHANTOM_BIN"] = "/tmp/phantom-binary-missing"
        missing = self.client.post("/api/phantom/login", headers=AUTH, json={})
        self.assertEqual(missing.status_code, 409)
        self.assertEqual(missing.json()["detail"], "phantom package is not available")
        self.assertNotIn("SESSIONFILESECRET", missing.text)
        self.assertFalse(phantom_auth._allowed("https://evil.example/device-connect?user_code=ABCD-EFGH"))
        self.assertFalse(phantom_auth._allowed("http://connect.phantom.app/device-connect?user_code=ABCD-EFGH"))
        self.assertTrue(phantom_auth._allowed(
            "https://connect.phantom.app/device-connect?user_code=ABCD-EFGH&client_id=desk&public_key=abc"
        ))

    def test_expired_login_does_not_start_another(self):
        (self.home / "mode").write_text("hang", encoding="utf-8")
        phantom_auth.start(timeout_s=0.4)
        self._wait("expired")
        self.assertEqual(self._calls().count("login"), 1)
        time.sleep(0.3)
        self.assertEqual(self._calls().count("login"), 1)
        self.assertEqual(phantom_auth.account_view()["auth_flow"], "expired")
        self.assertIsNone(phantom_auth.public_login().get("approval_url"))

    def test_approval_records_a_different_wallet_without_changing_the_allowlist(self):
        (self.home / "mode").write_text("mismatch", encoding="utf-8")
        phantom_auth.start()
        self._wait("mismatch")
        view = phantom_auth.public_login()
        self.assertEqual(view["observed_wallet_id"], "11111111-2222-4333-8444-555555555555")
        self.assertFalse(view["matches_allowlist"])
        self.assertEqual(view["required_wallet_id"], phantom_transport.REQUIRED_WALLET_ID)
        self.assertEqual(phantom_transport.REQUIRED_WALLET, "GLmLe3gpVw87nSeA8YY93megzW6gDSeavHys3BKQ61fh")
        self.assertIsNone(phantom_transport.quote({}).get("quote"))
        self.assertFalse(view["live_ready"])
        self.assertEqual(db.get_setting("paper_start_stack"), "200")
        self.assertEqual(db.get_setting("agents_enabled"), "0")
        self.assertTrue(db.query("SELECT value FROM phantom_control WHERE key = 'halt'")[0]["value"] == "1")

    def test_matching_login_cannot_execute_and_a_401_does_not_login_again(self):
        (self.home / "mode").write_text("approve", encoding="utf-8")
        phantom_auth.start()
        self._wait("observed")
        view = self.client.post("/api/phantom/login/check", headers=AUTH, json={}).json()
        self.assertTrue(view["matches_allowlist"])
        self.assertFalse(view["live_ready"])
        self.assertFalse(view["executable"])
        self.assertFalse(view["send"])
        self.assertIsNone(phantom_transport.quote({}).get("quote"))
        self.assertEqual(db.get_setting("mode"), "PAPER")
        self.assertEqual(db.get_setting("agents_enabled"), "0")
        phantom_auth.reset_for_tests()
        (self.home / "calls").write_text("", encoding="utf-8")
        (self.home / "mode").write_text("auth401", encoding="utf-8")
        phantom_auth.start()
        self._wait("auth_error")
        phantom_auth.check()
        self.assertEqual(self._calls().count("login"), 1)
        self.assertNotIn("buy", self._calls())
        self.assertEqual(phantom_auth.account_view()["auth_flow"], "auth_error")

    def test_addresses_shapes_do_not_call_a_missing_identity_a_mismatch(self):
        match = {
            "walletId": "d76451f8-288c-4ab8-b6de-1cfd84403873",
            "organizationId": "org_1",
            "addresses": [
                {"addressType": "solana", "address": "GLmLe3gpVw87nSeA8YY93megzW6gDSeavHys3BKQ61fh"},
                {"addressType": "ethereum", "address": "0xabc"},
            ],
        }
        toon = "\n".join([
            "walletId: d76451f8-288c-4ab8-b6de-1cfd84403873",
            "organizationId: org_1",
            "addresses[2]{addressType,address}:",
            "  solana,GLmLe3gpVw87nSeA8YY93megzW6gDSeavHys3BKQ61fh",
            "  ethereum,0xabc",
        ])
        self.assertEqual(phantom_auth._classify(toon, 0)["state"], "observed")
        self.assertEqual(phantom_auth._classify(json.dumps(match), 0)["state"], "observed")
        self.assertEqual(phantom_auth._classify(json.dumps({"ok": True, "data": match, "meta": {"token": SECRET}}), 0)["state"], "observed")
        different = json.dumps({
            "ok": True,
            "data": {
                "walletId": "11111111-2222-4333-8444-555555555555",
                "addresses": [{"addressType": "solana", "address": "4tKiUbkTbbcPqrTDsjmMWgZWZ7mZucMKJqk2nHE2HP4f"}],
            },
        })
        mismatch = phantom_auth._classify(different, 0)
        self.assertEqual(mismatch["state"], "mismatch")
        self.assertEqual(mismatch["wallet_id"], "11111111-2222-4333-8444-555555555555")
        empty = phantom_auth._classify("", 0)
        self.assertEqual(empty["state"], "unable_to_verify")
        self.assertIsNone(empty["wallet_id"])
        self.assertIsNone(empty["address"])
        partial = phantom_auth._classify(json.dumps({"walletId": "d76451f8-288c-4ab8-b6de-1cfd84403873"}), 0)
        self.assertEqual(partial["state"], "unable_to_verify")
        self.assertIsNone(partial["wallet_id"])
        wrapped = phantom_auth._classify(json.dumps({
            "ok": False,
            "error": {"code": "UNKNOWN", "message": "Failed to get wallet addresses: status code 401", "stamper": SECRET},
        }), 1)
        self.assertEqual(wrapped["state"], "auth_error")
        self.assertNotIn(SECRET, json.dumps(wrapped))
        self.assertEqual(phantom_auth._classify("code: UNKNOWN\nmessage: \"Failed to get wallet addresses: status code 401\"\n", 1)["state"], "auth_error")
        (self.home / "mode").write_text("toon", encoding="utf-8")
        phantom_auth.start()
        self._wait("observed")
        (self.home / "mode").write_text("empty", encoding="utf-8")
        phantom_auth.reset_for_tests()
        (self.home / "calls").write_text("", encoding="utf-8")
        phantom_auth.start()
        self._wait("unable_to_verify")
        view = phantom_auth.public_login()
        self.assertIsNone(view["observed_wallet_id"])
        self.assertIsNone(view["observed_wallet"])
        self.assertNotEqual(view["auth_flow"], "mismatch")
        self.assertEqual(view["verify"]["shape"], "empty")

    def _calls(self) -> str:
        path = self.home / "calls"
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8")

    def _wait(self, state: str) -> None:
        deadline = time.time() + 3
        while time.time() < deadline:
            if phantom_auth.account_view()["auth_flow"] == state:
                return
            time.sleep(0.05)
        self.fail(phantom_auth.account_view()["auth_flow"])

    def _capture(self, work) -> str:
        records = []

        class Grab(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Grab()
        logger = logging.getLogger("phantom_auth")
        logger.addHandler(handler)
        try:
            work()
        finally:
            logger.removeHandler(handler)
        return "\n".join(records)
