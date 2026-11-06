"""Web push delivery. pywebpush is mocked. No network and no real VAPID use."""
import json
import logging
import os
import shutil
import subprocess
import unittest
from pathlib import Path

import pywebpush
from fastapi.testclient import TestClient

import db
import links
import webpush
from notifier import Notifier
from tests.support import temp_desk_env

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "dummy-app-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PRIVATE = "dummy-vapid"
MINT = "MintPush11111111111111111111111111111111"
GONE = "https://push.example/gone"
OK = "https://push.example/ok"
FAIL = "https://push.example/fail"


class _Response:
    def __init__(self, status, text):
        self.status_code = status
        self.text = text
        self.reason = "no"


class TestWebPush(unittest.TestCase):
    def setUp(self):
        self.tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        self.previous_private = os.environ.get("VAPID_PRIVATE_KEY")
        self.previous_tg = os.environ.get("TG_TOKEN")
        os.environ["VAPID_PRIVATE_KEY"] = PRIVATE
        os.environ["APP_TOKEN"] = TOKEN
        os.environ.pop("TG_TOKEN", None)
        os.environ.pop("DESK_AGENTS", None)
        self.addCleanup(self._restore_env)
        db.init_db()
        db.set_setting("notifier", "webpush", "test")
        db.set_setting("mode", "PAPER", "test")
        self.calls = []
        original = pywebpush.webpush

        def fake_webpush(**kwargs):
            self.calls.append(kwargs)
            endpoint = kwargs["subscription_info"]["endpoint"]
            if endpoint == OK:
                return None
            if endpoint == GONE:
                raise pywebpush.WebPushException("gone", response=_Response(410, "dummy-vapid"))
            raise pywebpush.WebPushException("fail", response=_Response(500, "push-response-secret"))

        pywebpush.webpush = fake_webpush
        self.addCleanup(lambda: setattr(pywebpush, "webpush", original))

    def _restore_env(self):
        self._putback("VAPID_PRIVATE_KEY", self.previous_private)
        self._putback("TG_TOKEN", self.previous_tg)
        os.environ.pop("APP_TOKEN", None)

    def _putback(self, key, value):
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    def _sub(self, endpoint):
        db.execute(
            "INSERT INTO push_subs (endpoint, p256dh, auth, created_at) VALUES (?, ?, ?, ?)",
            (endpoint, "dummy-p256dh", "dummy-auth", 1),
        )

    def test_success_410_and_other_failure(self):
        self._sub(OK)
        self._sub(GONE)
        self._sub(FAIL)
        url = links.coin_url(MINT, "solana")
        with self.assertLogs("webpush", level="WARNING") as caught:
            result = webpush.deliver("Sold FIX · +$5", url)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["removed"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertNotIn(PRIVATE, json.dumps(result))
        self.assertNotIn("push-response-secret", json.dumps(result))
        logged = "\n".join(caught.output)
        self.assertNotIn(PRIVATE, logged)
        self.assertNotIn("push-response-secret", logged)
        self.assertNotIn("dummy-auth", logged)
        endpoints = {row["endpoint"] for row in db.query("SELECT endpoint FROM push_subs")}
        self.assertEqual(endpoints, {OK, FAIL})
        self.assertEqual(len(self.calls), 3)
        for call in self.calls:
            self.assertEqual(call["vapid_private_key"], PRIVATE)
            self.assertFalse(call["verbose"])
            payload = json.loads(call["data"])
            self.assertEqual(payload["title"], "Grok Bot · now")
            self.assertEqual(payload["body"], "Sold FIX · +$5")
            self.assertEqual(payload["url"], url)
            self.assertIn("r=savipww", payload["url"])

    def test_unconfigured_does_not_call_pywebpush(self):
        os.environ.pop("VAPID_PRIVATE_KEY")
        self._sub(OK)
        result = webpush.deliver("Sold FIX", links.coin_url(MINT, "solana"))
        self.assertFalse(result["configured"])
        self.assertEqual(self.calls, [])
        self.assertEqual(len(db.query("SELECT endpoint FROM push_subs")), 1)
        state = webpush.status()
        self.assertFalse(state["ready"])
        self.assertNotIn(PRIVATE, json.dumps(state))
        self.assertIn("not set", state["detail"])

    def test_telegram_with_a_token_is_not_web_push(self):
        os.environ["TG_TOKEN"] = "dummy-tg"
        db.set_setting("notifier", "telegram", "test")
        self.assertEqual(webpush.selected_channel(), "telegram")
        self.assertFalse(webpush.status()["ready"])
        self._sub(OK)
        webpush.deliver("Sold FIX", links.SIGNUP_URL)
        self.assertEqual(self.calls, [])

    def test_skipped_telegram_uses_web_push(self):
        db.set_setting("notifier", "telegram", "test")
        self.assertEqual(webpush.selected_channel(), "webpush")
        self.assertTrue(webpush.status()["ready"])

    def test_notifier_forwards_the_exact_coin_url(self):
        self._sub(OK)
        url = links.coin_url(MINT, "solana")
        Notifier(mode="PAPER").push(
            "Sold FIX", "+$5", url, [{"label": "Sell on FOMO", "url": url}],
        )
        payload = json.loads(self.calls[0]["data"])
        self.assertEqual(payload["url"], url)
        self.assertTrue(payload["body"].startswith("PAPER · "))
        self.assertEqual(db.get_setting("agents_enabled"), "0")
        self.assertEqual(db.get_setting("mode"), "PAPER")

    def test_api_test_push_is_authenticated_and_mock_only(self):
        import server
        client = TestClient(server.app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        self.assertEqual(client.post("/api/push/test").status_code, 401)
        os.environ.pop("VAPID_PRIVATE_KEY")
        refused = client.post("/api/push/test", headers=AUTH)
        self.assertEqual(refused.status_code, 409)
        self.assertNotIn(PRIVATE, refused.text)
        self.assertEqual(self.calls, [])
        os.environ["VAPID_PRIVATE_KEY"] = PRIVATE
        self._sub(OK)
        sent = client.post("/api/push/test", headers=AUTH)
        self.assertEqual(sent.status_code, 200)
        self.assertEqual(sent.json()["sent"], 1)
        self.assertNotIn(PRIVATE, sent.text)
        self.assertNotIn("dummy-auth", sent.text)
        payload = json.loads(self.calls[0]["data"])
        self.assertEqual(payload["url"], links.SIGNUP_URL)
        self.assertEqual(payload["body"], "Test push")
        account = client.get("/api/account", headers=AUTH)
        self.assertTrue(account.json()["integrations"]["web_push"]["ready"])
        self.assertNotIn(PRIVATE, account.text)
        self.assertFalse(account.json()["integrations"]["email"]["ready"])
        self.assertFalse(account.json()["integrations"]["telegram"]["ready"])

    def test_service_worker_opens_the_push_url(self):
        binary = os.environ.get("NODE") or shutil.which("node") or "/exec-daemon/node"
        if not Path(binary).exists():
            binary = shutil.which("node")
        if not binary:
            self.fail("node is required")
        url = links.coin_url(MINT, "solana")
        script = (
            "const fs=require('fs'); const vm=require('vm'); const opened=[]; const handlers={};\n"
            "const sandbox={URL, console, fetch(){return Promise.resolve({ok:true, clone(){return this;}});}, caches:{open(){return Promise.resolve({put(){}});}, match(){return Promise.resolve();}}};\n"
            "sandbox.self={location:{origin:'https://desk.test'}, addEventListener(name, fn){handlers[name]=fn;}, skipWaiting(){}, "
            "clients:{claim(){return Promise.resolve();}, openWindow(url){opened.push(url); return Promise.resolve();}}, "
            "registration:{showNotification(){return Promise.resolve();}}};\n"
            "sandbox.globalThis=sandbox;\n"
            "vm.runInNewContext(fs.readFileSync('web/sw.js','utf8'), sandbox, {filename:'sw.js'});\n"
            "const url=process.argv[1];\n"
            "if (sandbox.self.deskNotificationUrl({url}) !== url) process.exit(2);\n"
            "let waited; handlers.notificationclick({notification:{data:{url}, close(){}}, waitUntil(p){waited=p;}});\n"
            "Promise.resolve(waited).then(() => { if (opened[0] !== url) process.exit(3); process.exit(0); });\n"
        )
        result = subprocess.run(
            [binary, "-e", script, url],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    unittest.main()
