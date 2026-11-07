"""Owner notification delivery. pywebpush is mocked. No network."""
import asyncio
import os
import threading
import unittest

import pywebpush
from fastapi.testclient import TestClient

import api
import db
import webpush
from health import Supervisor
from report import save_page, window
from tests.support import temp_desk_env

TOKEN = "dummy-app-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PRIVATE = "dummy-vapid"
PHONE = "https://push.example/phone"
OTHER = "https://push.example/other"


class _Response:
    def __init__(self, status):
        self.status_code = status
        self.text = ""
        self.reason = ""


class TestNotify(unittest.TestCase):
    def setUp(self):
        self.tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        self.previous_private = os.environ.get("VAPID_PRIVATE_KEY")
        self.previous_token = os.environ.get("APP_TOKEN")
        os.environ["VAPID_PRIVATE_KEY"] = PRIVATE
        os.environ["APP_TOKEN"] = TOKEN
        os.environ.pop("DESK_AGENTS", None)
        self.addCleanup(self._restore)
        db.init_db()
        db.set_setting("notifier", "webpush", "test")
        api._paused.clear()
        self.addCleanup(api._paused.clear)
        self.calls = []
        self.payloads = []
        self.status = {}
        original = pywebpush.webpush

        def fake_webpush(**kwargs):
            endpoint = kwargs["subscription_info"]["endpoint"]
            self.calls.append(endpoint)
            self.payloads.append(kwargs["data"])
            code = self.status.get(endpoint, 201)
            if code > 202:
                raise pywebpush.WebPushException("fail", response=_Response(code))
            return None

        pywebpush.webpush = fake_webpush
        self.addCleanup(lambda: setattr(pywebpush, "webpush", original))

    def _restore(self):
        if self.previous_private is None:
            os.environ.pop("VAPID_PRIVATE_KEY", None)
        else:
            os.environ["VAPID_PRIVATE_KEY"] = self.previous_private
        if self.previous_token is None:
            os.environ.pop("APP_TOKEN", None)
        else:
            os.environ["APP_TOKEN"] = self.previous_token

    def _phone(self, endpoint=PHONE):
        db.execute(
            "INSERT INTO push_subs (endpoint, p256dh, auth, created_at) VALUES (?, ?, ?, ?)",
            (endpoint, "dummy-p256dh", "dummy-auth", 1),
        )

    def test_auth_bad_input_and_no_subscriptions(self):
        import server
        client = TestClient(server.app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        self.assertEqual(client.post("/api/notify", json={"kind": "verify", "event": "once"}).status_code, 401)
        leaked = client.post(
            "/api/notify", headers=AUTH, params={"token": TOKEN},
            json={"kind": "verify", "event": "once"},
        )
        self.assertEqual(leaked.status_code, 401)
        self.assertNotIn(TOKEN, leaked.text)
        self.assertNotIn(PRIVATE, leaked.text)
        bad = client.post("/api/notify", headers=AUTH, json={"kind": "other"})
        self.assertEqual(bad.status_code, 400)
        missing = client.post("/api/notify", headers=AUTH, json={"kind": "report", "window_id": "nope"})
        self.assertEqual(missing.status_code, 409)
        self.assertEqual(self.calls, [])
        none = client.post("/api/notify", headers=AUTH, json={"kind": "verify", "event": "deploy-check"})
        self.assertEqual(none.status_code, 200)
        self.assertEqual(none.json()["sent"], 0)
        self.assertEqual(none.json()["reason"], "no subscriptions")
        self.assertNotIn(PRIVATE, none.text)
        self.assertEqual(db.get_setting("agents_enabled"), "0")
        self.assertEqual(db.get_setting("mode"), "PAPER")

    def test_report_uses_the_saved_page_and_retry_skips(self):
        self._phone()
        end = 1_700_000_000.0
        # A real window id from the clock helper, then point the rows at it.
        span = window(end)
        db.execute(
            'INSERT INTO skips (mint, "until") VALUES (?, ?)',
            ("kept", span["end"] + 10),
        )
        save_page(span["id"], "https://pages.example/night-one", now=end)
        import server
        client = TestClient(server.app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        forged = client.post("/api/notify", headers=AUTH, json={
            "kind": "report", "window_id": span["id"], "url": "https://evil.example/fake",
        })
        self.assertEqual(forged.status_code, 400)
        self.assertEqual(self.calls, [])
        first = client.post("/api/notify", headers=AUTH, json={"kind": "report", "window_id": span["id"]})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["sent"], 1)
        self.assertEqual(self.calls, [PHONE])
        self.assertIn("https://pages.example/night-one", self.payloads[0])
        self.assertNotIn("evil.example", self.payloads[0])
        second = client.post("/api/notify", headers=AUTH, json={"kind": "report", "window_id": span["id"]})
        self.assertEqual(second.json()["skipped"], 1)
        self.assertEqual(second.json()["sent"], 0)
        self.assertEqual(len(self.payloads), 1)
        self.assertEqual([row["mint"] for row in db.query("SELECT mint FROM skips")], ["kept"])

    def test_retry_after_failure_and_410(self):
        self._phone(PHONE)
        self._phone(OTHER)
        self.status[PHONE] = 500
        self.status[OTHER] = 410
        first = webpush.deliver_once("verify:once", "Verification only. Not a restart and not a morning report.", "")
        self.assertEqual(first["failed"], 1)
        self.assertEqual(first["removed"], 1)
        self.assertEqual([row["endpoint"] for row in db.query("SELECT endpoint FROM push_subs")], [PHONE])
        self.status[PHONE] = 201
        second = webpush.deliver_once("verify:once", "Verification only. Not a restart and not a morning report.", "")
        self.assertEqual(second["sent"], 1)
        self.assertEqual(second["failed"], 0)
        third = webpush.deliver_once("verify:once", "Verification only. Not a restart and not a morning report.", "")
        self.assertEqual(third["skipped"], 1)
        self.assertEqual(third["sent"], 0)
        self.assertEqual(self.calls.count(PHONE), 2)
        self.assertEqual(self.calls.count(OTHER), 1)

    def test_concurrent_claim_sends_once(self):
        self._phone()
        barrier = threading.Barrier(2)
        results = []

        def go():
            barrier.wait()
            results.append(webpush.deliver_once("verify:race", "Verification only. Not a restart and not a morning report.", ""))

        threads = [threading.Thread(target=go) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(self.calls.count(PHONE), 1)
        self.assertEqual(sum(item["sent"] for item in results), 1)
        self.assertEqual(sum(item["skipped"] for item in results), 1)

    def test_restart_alert_requires_a_real_restart(self):
        self._phone()
        import server
        client = TestClient(server.app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        missing = client.post("/api/notify", headers=AUTH, json={"kind": "restart", "event_id": "restart-scanner-1-abcd"})
        self.assertEqual(missing.status_code, 409)
        self.assertEqual(self.calls, [])

        async def factory():
            await asyncio.Event().wait()

        async def once():
            supervisor = Supervisor()
            supervisor.register("process", factory)
            db.beat("process", 5_000.0)
            return await supervisor.recover("process", now=5_000.0 + 300)

        outcome = asyncio.run(once())
        self.assertTrue(outcome["restarted"])
        sent = client.post("/api/notify", headers=AUTH, json={"kind": "restart", "event_id": outcome["event_id"]})
        self.assertEqual(sent.status_code, 200)
        self.assertEqual(sent.json()["sent"], 1)
        self.assertIn(outcome["push_text"], self.payloads[0])
        self.assertNotIn("https://", self.payloads[0].split('"url":', 1)[1])
        self.assertNotIn(PRIVATE, sent.text)
        again = client.post("/api/notify", headers=AUTH, json={"kind": "restart", "event_id": outcome["event_id"]})
        self.assertEqual(again.json()["skipped"], 1)
        self.assertEqual(self.calls.count(PHONE), 1)
        self.assertEqual(db.get_setting("agents_enabled"), "0")

    def test_verify_rejects_a_spoofed_note_and_a_token_url(self):
        import server
        client = TestClient(server.app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        spoof = client.post("/api/notify", headers=AUTH, json={
            "kind": "verify", "event": "once", "note": "Restarted scanner, down 5 min",
        })
        self.assertEqual(spoof.status_code, 400)
        token_url = client.post("/api/notify", headers=AUTH, json={
            "kind": "verify", "event": "once", "url": "https://example.com/path?token=abc",
        })
        self.assertEqual(token_url.status_code, 400)
        insecure = client.post("/api/notify", headers=AUTH, json={
            "kind": "verify", "event": "once", "url": "http://example.com/path",
        })
        self.assertEqual(insecure.status_code, 400)
        self.assertEqual(self.calls, [])
        self.assertEqual(db.get_setting("mode"), "PAPER")


if __name__ == "__main__":
    unittest.main()
