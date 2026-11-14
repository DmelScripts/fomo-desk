"""The six-role loop can be seen. Observing it does not sign or send."""
import copy
import io
import json
import logging
import os
import unittest

from fastapi.testclient import TestClient

import db
import desk_loop
import desk_providers
import live_submit
import solana_tx
from tests.support import temp_desk_env

TOKEN = "dummy-app-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
NOW = 1_800_000_000.0
MINT = solana_tx.b58encode(b"\x31" * 32)
OTHER = solana_tx.b58encode(b"\x32" * 32)
SECRET = "helius-observe-secret"


class LoopObserveTests(unittest.TestCase):
    def setUp(self):
        self.tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        self.runtime = copy.deepcopy(desk_loop._runtime)
        self.addCleanup(lambda: desk_loop._runtime.update(copy.deepcopy(self.runtime)))
        os.environ["APP_TOKEN"] = TOKEN
        os.environ["DESK_PRICE_LOOP"] = "0"
        os.environ.pop("HELIUS_API_KEY", None)
        os.environ.pop("SOLANA_TRACKER_API_KEY", None)
        self.addCleanup(lambda: os.environ.pop("APP_TOKEN", None))
        self.addCleanup(lambda: os.environ.pop("DESK_PRICE_LOOP", None))
        self.addCleanup(lambda: os.environ.pop("HELIUS_API_KEY", None))
        self.addCleanup(lambda: os.environ.pop("SOLANA_TRACKER_API_KEY", None))
        db.init_db()
        db.set_setting("mode", "PAPER", "test")
        db.set_setting("agents_enabled", "0", "test")

    def _sample(self):
        return {
            "candidates": 2,
            "screened": [
                {
                    "mint": MINT,
                    "symbol": "AAA",
                    "stage": "rejected",
                    "reason": "lp",
                    "quote": {"attempted": True, "simulate": True, "ok": False, "cause": "simulation_err"},
                },
                {
                    "mint": OTHER,
                    "symbol": "BBB",
                    "stage": "analyst",
                    "reason": None,
                    "score": "61",
                    "score_measured": True,
                    "breakdown": {"momentum": "20", "holder_growth": "10"},
                    "paper_entry": True,
                },
            ],
        }

    def test_a_server_pass_is_visible_and_cases_require_the_token(self):
        self.assertFalse(desk_loop.readiness()["server_loop"])
        self.assertFalse(desk_loop.scheduler_status()["server_loop"])
        desk_loop.begin_server_loop(NOW)
        status = desk_loop.scheduler_status()
        self.assertTrue(status["server_loop"])
        self.assertTrue(status["started"])
        self.assertTrue(desk_loop.readiness()["server_loop"])
        self.assertEqual(status["loop_started_at"], NOW)
        self.assertFalse(status["sign"])
        self.assertFalse(status["send"])
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        loop_log = logging.getLogger("desk.loop")
        provider_log = logging.getLogger("desk.provider")
        loop_log.addHandler(handler)
        provider_log.addHandler(handler)
        loop_log.setLevel(logging.INFO)
        provider_log.setLevel(logging.INFO)
        self.addCleanup(lambda: loop_log.removeHandler(handler))
        self.addCleanup(lambda: provider_log.removeHandler(handler))
        os.environ["HELIUS_API_KEY"] = SECRET
        desk_loop.begin_pass(NOW + 1)
        self.assertTrue(desk_loop.scheduler_status()["running"])
        desk_loop.end_pass(NOW + 2, self._sample(), None)
        desk_loop.note_provider("helius", "helius_status")
        desk_loop.note_provider("helius", "https://mainnet.helius-rpc.com/?api-key=" + SECRET)
        text = stream.getvalue()
        self.assertIn("pass candidates=2", text)
        self.assertIn("sims_attempted=1", text)
        self.assertIn("sims_failed=1", text)
        self.assertIn("paper_entries=1", text)
        self.assertIn("provider helius helius_status", text)
        self.assertIn("provider helius redacted", text)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("api-key=", text)
        self.assertNotIn("mainnet.helius-rpc.com", text)
        done = desk_loop.scheduler_status()
        self.assertFalse(done["running"])
        self.assertEqual(done["passes"], self.runtime["passes"] + 1)
        self.assertEqual(done["last_pass_started_at"], NOW + 1)
        self.assertEqual(done["last_pass_ended_at"], NOW + 2)
        self.assertIsNone(done["last_error"])
        self.assertEqual(done["last_pass"]["candidates"], 2)
        self.assertEqual(done["last_pass"]["filter_rejects"], {"lp": 1})
        self.assertEqual(done["last_pass"]["analyst_scored"], 1)
        self.assertEqual(done["last_pass"]["analyst_passed"], 1)
        self.assertEqual(done["last_pass"]["sims_ok"], 0)
        self.assertEqual(done["last_pass"]["paper_entries"], 1)
        desk_loop.end_pass(NOW + 3, None, "RuntimeError")
        self.assertEqual(desk_loop.scheduler_status()["last_error"], "RuntimeError")
        desk_loop.end_server_loop()
        self.assertFalse(desk_loop.scheduler_status()["server_loop"])
        self.assertFalse(desk_loop.scheduler_status()["started"])
        self.assertFalse(desk_loop.readiness()["server_loop"])
        self.assertFalse(live_submit.armed())

        import server
        client = TestClient(server.app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        missing = client.get("/api/memecoin/cases?limit=10")
        self.assertEqual(missing.status_code, 401)
        body = client.get("/api/memecoin/cases?limit=10", headers=AUTH)
        self.assertEqual(body.status_code, 200, body.text)
        payload = body.json()
        self.assertFalse(payload["send"])
        self.assertFalse(payload["sign"])
        self.assertEqual(len(payload["rows"]), 2)
        first = payload["rows"][0]
        self.assertEqual(first["symbol"], "BBB")
        self.assertTrue(first["passed"])
        self.assertEqual(first["filter"]["lp_burned_or_locked"], "pass")
        self.assertEqual(first["analyst"]["score"], "61")
        self.assertEqual(first["analyst"]["breakdown"]["momentum"], "20")
        self.assertTrue(first["paper_entry"])
        self.assertFalse(first["send"])
        self.assertFalse(first["sign"])
        failed = payload["rows"][1]
        self.assertEqual(failed["reason"], "lp")
        self.assertFalse(failed["passed"])
        self.assertEqual(failed["filter"]["lp_burned_or_locked"], "fail")
        self.assertEqual(failed["filter"]["holder_growth"], "not_evaluated")
        self.assertEqual(failed["filter"]["dev_holding"], "not_evaluated")
        self.assertEqual(failed["filter"]["bundles"], "not_evaluated")
        self.assertEqual(failed["filter"]["liquidity_vs_mcap"], "not_evaluated")
        self.assertEqual(failed["filter"]["dev_rug_history"], "not_evaluated")
        self.assertTrue(failed["simulation"]["attempted"])
        self.assertFalse(failed["simulation"]["ok"])
        self.assertEqual(failed["simulation"]["reason"], "simulation_err")
        self.assertNotIn(SECRET, body.text)
        self.assertEqual(db.get_setting("mode"), "PAPER")
        self.assertEqual(db.get_setting("agents_enabled"), "0")

        previous_keep = desk_loop._EVAL_KEEP
        desk_loop._EVAL_KEEP = 1
        self.addCleanup(lambda: setattr(desk_loop, "_EVAL_KEEP", previous_keep))
        desk_loop.end_pass(NOW + 4, {"candidates": 1, "screened": [{"mint": MINT, "stage": "rejected", "reason": "bundle"}]}, None)
        kept = client.get("/api/memecoin/cases?limit=10", headers=AUTH).json()["rows"]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["reason"], "bundle")
        self.assertEqual(kept[0]["filter"]["bundles"], "fail")

    def test_run_reports_the_armed_state_and_does_not_sign(self):
        import phantom_session
        import server
        os.environ["HELIUS_API_KEY"] = SECRET
        os.environ["SOLANA_TRACKER_API_KEY"] = "tracker-observe-secret"
        desk_providers.shutdown()
        import cross_source
        cross_source.clear_fetcher()
        desk_providers.install()
        self.addCleanup(desk_providers.shutdown)
        client = TestClient(server.app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        quiet = client.post("/api/memecoin/run", headers=AUTH, json={})
        self.assertEqual(quiet.status_code, 409)
        self.assertEqual(quiet.json()["reason"], "scheduler_unarmed")
        self.assertFalse(quiet.json()["sign"])
        self.assertFalse(quiet.json()["send"])
        turned = client.post(
            "/api/settings", headers=AUTH, json={"key": "memecoin_scheduler_authorized", "value": "1"},
        )
        self.assertEqual(turned.status_code, 200, turned.text)
        signs = []
        original = phantom_session.sign
        phantom_session.sign = lambda *args, **kwargs: signs.append("sign")
        self.addCleanup(lambda: setattr(phantom_session, "sign", original))
        idle = client.post("/api/memecoin/run", headers=AUTH, json={"mint": MINT})
        self.assertEqual(idle.status_code, 409, idle.text)
        self.assertEqual(idle.json()["reason"], "armed_idle")
        self.assertTrue(idle.json()["armed"])
        self.assertFalse(idle.json()["sign"])
        self.assertFalse(idle.json()["send"])
        self.assertFalse(idle.json()["request_body_used"])
        desk_loop.begin_server_loop(NOW)
        self.addCleanup(desk_loop.end_server_loop)
        running = client.post("/api/memecoin/run", headers=AUTH, json={})
        self.assertEqual(running.status_code, 409)
        self.assertEqual(running.json()["reason"], "loop_running")
        self.assertTrue(running.json()["server_loop"])
        self.assertFalse(running.json()["sign"])
        self.assertFalse(running.json()["send"])
        self.assertEqual(signs, [])
        self.assertFalse(live_submit.armed())
        self.assertEqual(db.get_setting("mode"), "PAPER")
        route = __import__("pathlib").Path("api.py").read_text(encoding="utf-8").split("def memecoin_run")[1].split("def memecoin_cases")[0]
        self.assertNotIn("run_discovery", route)
        self.assertNotIn("phantom_session", route)
        self.assertNotIn(SECRET, idle.text)
        self.assertNotIn("tracker-observe-secret", json.dumps(running.json()))
