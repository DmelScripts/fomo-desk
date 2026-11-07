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


def _raw_private() -> str:
    from py_vapid import Vapid
    from py_vapid.utils import b64urlencode

    vapid = Vapid()
    vapid.generate_keys()
    return b64urlencode(vapid.private_key.private_numbers().private_value.to_bytes(32, "big"))
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
        self.signing_key = _raw_private()
        os.environ["VAPID_PRIVATE_KEY"] = self.signing_key
        os.environ["APP_TOKEN"] = TOKEN
        os.environ.pop("TG_TOKEN", None)
        os.environ.pop("DESK_AGENTS", None)
        self.addCleanup(self._restore_env)
        db.init_db()
        db.set_setting("notifier", "webpush", "test")
        db.set_setting("mode", "PAPER", "test")
        self.calls = []
        self.real_webpush = pywebpush.webpush
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
        self.assertNotIn(self.signing_key, json.dumps(result))
        self.assertNotIn("push-response-secret", json.dumps(result))
        logged = "\n".join(caught.output)
        self.assertNotIn(self.signing_key, logged)
        self.assertNotIn("push-response-secret", logged)
        self.assertNotIn("dummy-auth", logged)
        endpoints = {row["endpoint"] for row in db.query("SELECT endpoint FROM push_subs")}
        self.assertEqual(endpoints, {OK, FAIL})
        self.assertEqual(len(self.calls), 3)
        for call in self.calls:
            self.assertTrue(hasattr(call["vapid_private_key"], "sign"))
            self.assertNotIn(self.signing_key, str(call["vapid_claims"]))
            self.assertFalse(call["verbose"])
            self.assertGreater(call["ttl"], 0)
            self.assertEqual(call["vapid_claims"]["sub"], "mailto:desk@localhost")
            payload = json.loads(call["data"])
            self.assertEqual(payload["title"], "Grok Bot · now")
            self.assertEqual(payload["body"], "Sold FIX · +$5")
            self.assertEqual(payload["url"], url)
            self.assertIn("r=savipww", payload["url"])

    def test_provider_failure_keeps_status_and_drops_the_body(self):
        class Response:
            def __init__(self, status, text):
                self.status_code = status
                self.text = text
                self.reason = "Bad Request"
                self.headers = {}

        def fail(**kwargs):
            self.calls.append(kwargs)
            raise pywebpush.WebPushException(
                "Push failed: 400 Bad Request\nResponse body:" + Response(400, "").text,
                response=Response(400, '{"reason":"BadTtl","leak":"endpoint-secret"}'),
            )

        pywebpush.webpush = fail
        self._sub(OK)
        with self.assertLogs("webpush", level="WARNING") as caught:
            result = webpush.deliver("Test push", "https://example.test/page")
        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["errors"], [{"status": 400, "reason": "BadTtl", "category": "provider"}])
        blob = json.dumps(result)
        self.assertNotIn("endpoint-secret", blob)
        self.assertNotIn(OK, blob)
        self.assertNotIn(PRIVATE, blob)
        logged = "\n".join(caught.output)
        self.assertIn("status=400", logged)
        self.assertIn("reason=BadTtl", logged)
        self.assertNotIn("endpoint-secret", logged)
        self.assertNotIn(OK, logged)
        library = pywebpush.WebPushException(
            "hidden",
            response=Response(403, '{"reason":"VapidPkHashMismatch","endpoint":"https://push.example/secret"}'),
        )
        self.assertEqual(library.status_code, 403)
        self.assertIn("https://push.example/secret", str(library))
        prepared = pywebpush.WebPusher({"endpoint": "https://push.example/phone"})._prepare_send_data(
            data=None, ttl=0,
        )
        self.assertEqual(prepared["headers"]["ttl"], "0")
        positive = pywebpush.WebPusher({"endpoint": "https://push.example/phone"})._prepare_send_data(
            data=None, ttl=webpush._TTL_S,
        )
        self.assertEqual(positive["headers"]["ttl"], str(webpush._TTL_S))
        import py_vapid
        self.assertTrue(py_vapid._check_sub("mailto:desk@localhost"))

    def test_key_match_is_boolean_and_does_not_echo_material(self):
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        from py_vapid import Vapid
        from py_vapid.utils import b64urlencode

        vapid = Vapid()
        vapid.generate_keys()
        private = b64urlencode(vapid.private_key.private_numbers().private_value.to_bytes(32, "big"))
        public = b64urlencode(vapid.public_key.public_bytes(
            Encoding.X962, PublicFormat.UncompressedPoint,
        ))
        os.environ["VAPID_PRIVATE_KEY"] = private
        original = webpush._public
        webpush._public = lambda: public
        self.addCleanup(lambda: setattr(webpush, "_public", original))
        state = webpush.status()
        self.assertTrue(state["keys_match"])
        self.assertTrue(state["contact_is_mailto"])
        self.assertFalse(state["contact_host_public"])
        blob = json.dumps(state)
        self.assertNotIn(private, blob)
        self.assertNotIn(public, blob)
        self.assertNotIn("desk@localhost", blob)
        webpush._public = lambda: b64urlencode(b"\x04" + (b"\x01" * 64))
        self.assertFalse(webpush.status()["keys_match"])
        self.assertTrue(webpush.status()["private_key_parseable"])

    def test_pem_signs_locally_and_parse_failure_is_not_a_mismatch(self):
        import requests
        from py_vapid import Vapid
        from py_vapid.utils import b64urlencode
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        vapid = Vapid()
        vapid.generate_keys()
        raw = b64urlencode(vapid.private_key.private_numbers().private_value.to_bytes(32, "big"))
        public = b64urlencode(vapid.public_key.public_bytes(
            Encoding.X962, PublicFormat.UncompressedPoint,
        ))
        pem = vapid.private_pem().decode()
        self.assertRaises(Exception, Vapid.from_string, pem)
        loaded = webpush._load_private(pem)
        signed = loaded.sign({"sub": "mailto:desk@localhost", "aud": "https://web.push.apple.com"})
        self.assertTrue(signed["Authorization"].startswith("vapid "))
        self.assertNotIn(raw, signed["Authorization"])
        self.assertNotIn("BEGIN", signed["Authorization"])
        curled = self.real_webpush(
            subscription_info={
                "endpoint": "https://push.example/phone",
                "keys": {"p256dh": public, "auth": b64urlencode(b"0123456789abcdef")},
            },
            data="hello",
            vapid_private_key=loaded,
            vapid_claims={"sub": "mailto:desk@localhost"},
            ttl=webpush._TTL_S,
            curl=True,
        )
        self.assertIsInstance(curled, str)
        self.assertIn(str(webpush._TTL_S), curled)
        self.assertNotIn(raw, curled)
        self.assertNotIn("BEGIN", curled)
        escaped = pem.replace("\n", "\\n")
        self.assertTrue(webpush._load_private(escaped).public_key)
        os.environ["VAPID_PRIVATE_KEY"] = pem
        original = webpush._public
        webpush._public = lambda: public
        self.addCleanup(lambda: setattr(webpush, "_public", original))
        state = webpush.status()
        self.assertTrue(state["private_key_parseable"])
        self.assertTrue(state["public_key_parseable"])
        self.assertTrue(state["keys_match"])
        blob = json.dumps(state)
        self.assertNotIn(raw, blob)
        self.assertNotIn("BEGIN", blob)
        webpush._public = lambda: b64urlencode(b"\x04" + (b"\x03" * 64))
        mismatched = webpush.status()
        self.assertTrue(mismatched["private_key_parseable"])
        self.assertTrue(mismatched["public_key_parseable"])
        self.assertFalse(mismatched["keys_match"])
        os.environ["VAPID_PRIVATE_KEY"] = "not-a-key"
        broken = webpush.status()
        self.assertFalse(broken["private_key_parseable"])
        self.assertIsNone(broken["keys_match"])
        self.assertNotIn("not-a-key", json.dumps(broken))
        os.environ["VAPID_PRIVATE_KEY"] = pem
        webpush._public = lambda: public

        def offline(**_kwargs):
            raise requests.ConnectionError("hidden-host")

        pywebpush.webpush = offline
        self._sub("https://push.example/offline")
        result = webpush.deliver("Test push", "https://example.test/page")
        self.assertEqual(result["errors"], [{"status": None, "reason": None, "category": "network"}])
        self.assertNotIn("hidden-host", json.dumps(result))
        self.assertNotIn(raw, json.dumps(result))
        os.environ["VAPID_PRIVATE_KEY"] = "not-a-key"
        def refuse_network(**_kwargs):
            raise AssertionError("network was called")

        pywebpush.webpush = refuse_network
        refused = webpush.deliver("Test push", "https://example.test/page")
        self.assertEqual(refused["errors"][0]["category"], "private_key")
        self.assertIsNone(refused["errors"][0]["status"])
        self.assertNotIn("not-a-key", json.dumps(refused))

    def test_test_push_reports_a_provider_failure(self):
        import server

        class Response:
            status_code = 400
            text = '{"reason":"BadTtl","leak":"do-not-show"}'
            reason = "Bad Request"
            headers = {}

        def fail(**_kwargs):
            raise pywebpush.WebPushException("Push failed", response=Response())

        pywebpush.webpush = fail
        self._sub(OK)
        client = TestClient(server.app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        sent = client.post("/api/push/test", headers=AUTH)
        self.assertEqual(sent.status_code, 200)
        body = sent.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["sent"], 0)
        self.assertEqual(body["failed"], 1)
        self.assertEqual(body["errors"], [{"status": 400, "reason": "BadTtl", "category": "provider"}])
        self.assertNotIn("do-not-show", sent.text)
        self.assertNotIn(OK, sent.text)
        self.assertNotIn(PRIVATE, sent.text)

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

    def test_push_save_accepts_the_phone_json_shape(self):
        import server
        client = TestClient(server.app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        payload = json.dumps({
            "endpoint": "https://push.example/phone",
            "p256dh": "pub-key",
            "auth": "auth-key",
        })
        plain = client.post(
            "/api/push",
            content=payload,
            headers={**AUTH, "content-type": "text/plain;charset=UTF-8"},
        )
        self.assertEqual(plain.status_code, 200)
        self.assertEqual(plain.json(), {"ok": True})
        self.assertNotIn("pub-key", plain.text)
        self.assertNotIn("auth-key", plain.text)
        self.assertNotIn("https://push.example/phone", plain.text)
        labeled = client.post(
            "/api/push",
            content=payload,
            headers={**AUTH, "content-type": "application/json"},
        )
        self.assertEqual(labeled.status_code, 200)
        rows = db.query("SELECT endpoint, p256dh, auth FROM push_subs")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["endpoint"], "https://push.example/phone")
        self.assertEqual(rows[0]["p256dh"], "pub-key")
        self.assertEqual(rows[0]["auth"], "auth-key")
        self.assertEqual(client.post("/api/push", content=payload).status_code, 401)
        refused = client.post("/api/push", content="not-json", headers={**AUTH, "content-type": "text/plain"})
        self.assertEqual(refused.status_code, 400)
        self.assertNotIn("not-json", refused.text)
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
        os.environ["VAPID_PRIVATE_KEY"] = self.signing_key
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
