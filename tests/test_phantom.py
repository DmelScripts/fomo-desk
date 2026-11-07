"""PAPER Phantom runner. No login, no send, no LIVE unlock."""
import os
import threading
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import db
import phantom_runner
import phantom_transport
from tests.support import temp_desk_env

TOKEN = "dummy-app-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
WALLET_ID = phantom_transport.REQUIRED_WALLET_ID
WALLET = phantom_transport.REQUIRED_WALLET
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


class TestPhantomTransport(unittest.TestCase):
    def test_transport_stays_blocked_and_probe_sends_nothing(self):
        body = phantom_transport.status()
        self.assertEqual(body["transport"], "BLOCKED")
        self.assertEqual(body["auth"], "AWAITING_OWNER_AUTH_PROCEDURE")
        self.assertFalse(body["send"])
        self.assertFalse(body["live_ready"])
        self.assertEqual(body["package"], "@phantom/mcp-server@1.2.9")
        self.assertEqual(body["cli"], "@phantom/cli@3.0.0")
        self.assertIsNone(phantom_transport.quote({"tool": "buy"}).get("quote"))
        home = "/tmp/phantom-probe-home"
        script = Path("/tmp/phantom-probe.sh")
        script.write_text("#!/bin/sh\necho up > \"$HOME/started\"\nsleep 30\n", encoding="utf-8")
        script.chmod(0o755)
        probed = phantom_transport.probe_stdio([str(script)], home)
        self.assertEqual(probed["transport"], "BLOCKED")
        self.assertFalse(probed["probe"]["stdin_written"])
        self.assertFalse(probed["probe"]["tool_call"])
        self.assertTrue(probed["probe"]["started"])
        self.assertTrue((Path(home) / "started").is_file())
        self.assertFalse(phantom_transport.probe_stdio(["phantom-mcp", "login"], home)["probe"]["started"])
        self.assertEqual(phantom_transport.probe_stdio([str(script)], "/workspace")["probe"].get("reason"), "refused_home")


class TestPhantomRunner(unittest.TestCase):
    def setUp(self):
        self.tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        os.environ["APP_TOKEN"] = TOKEN
        os.environ["DESK_PHANTOM_PAPER"] = "1"
        os.environ.pop("DESK_AGENTS", None)
        self.addCleanup(lambda: os.environ.pop("APP_TOKEN", None))
        self.addCleanup(lambda: os.environ.pop("DESK_PHANTOM_PAPER", None))
        db.init_db()
        db.set_setting("mode", "PAPER", "test")
        db.set_setting("agents_enabled", "0", "test")
        phantom_runner.set_halt({"halted": False})
        import server
        self.client = TestClient(server.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_paper_round_trip_is_simulated_and_the_observed_quote_is_not(self):
        self.assertEqual(self.client.post("/api/phantom/run", json=self._intent("a")).status_code, 401)
        secret = self._intent("a")
        secret["session"] = {"stamper": "nope", "transactionData": "TX"}
        refused = self.client.post("/api/phantom/run", headers=AUTH, json=secret)
        self.assertEqual(refused.status_code, 400)
        self.assertNotIn("TX", refused.text)
        self.assertEqual(db.query("SELECT intent_id FROM phantom_reservations"), [])
        live = self._intent("a")
        live["execute"] = True
        self.assertEqual(self.client.post("/api/phantom/run", headers=AUTH, json=live).status_code, 400)
        self.assertEqual(phantom_runner.run_intent(self._intent("a", quote_source="observed"))["reason"], "observed_nonexecutable")
        self.assertEqual(db.query("SELECT status FROM phantom_reservations WHERE intent_id = 'a'")[0]["status"], "released")
        done = phantom_runner.run_intent(self._intent("b"))
        self.assertEqual(done["status"], "sim_closed")
        self.assertEqual(done["fill"], "simulated")
        self.assertFalse(done["send"])
        self.assertFalse(done["executable"])
        self.assertFalse(done["fomo_bump_applies"])
        self.assertEqual(done["amount_usd"], "8.00")
        self.assertNotEqual(done["entry_quote_id"], done["exit_quote_id"])
        self.assertNotEqual(done["entry_quote_id"], phantom_runner.OBSERVED_QUOTE_ID)
        again = phantom_runner.run_intent(self._intent("b"))
        self.assertEqual(again["exit_quote_id"], done["exit_quote_id"])
        self.assertEqual(len(db.query("SELECT intent_id FROM phantom_reservations WHERE intent_id = 'b'")), 1)
        account = self.client.get("/api/account", headers=AUTH).json()
        self.assertEqual(account["stack"], 200)
        self.assertEqual(account["phantom"]["bankroll_usd"], "100.00")
        self.assertEqual(account["phantom"]["size_usd"], "8.00")
        self.assertFalse(account["phantom"]["fomo_bump_applies"])
        self.assertFalse(account["phantom"]["live_ready"])
        self.assertEqual(account["phantom"]["transport"], "BLOCKED")
        self.assertEqual(account["mode"], "PAPER")
        self.assertEqual(db.get_setting("paper_start_stack"), "200")
        self.assertEqual(db.get_setting("min_ticket_usd"), "47.50")
        self.assertEqual(db.get_setting("below_min"), "bump")
        self.assertEqual(db.get_setting("agents_enabled"), "0")
        self.assertEqual(db.get_setting("stop_pct"), "6")
        page = Path("web/app.js").read_text()
        render = page.split("function renderPhantom")[1].split("function renderQuotes")[0]
        self.assertIn("Not a wallet policy", render)
        self.assertIn("47.50", render)
        self.assertIn("not executable", render)
        self.assertNotIn("size_ticket", Path("phantom_runner.py").read_text())

    def test_cap_fx_halt_unknown_and_concurrency(self):
        stale = self._intent("stale")
        stale["fx_at"] = time.time() - 1000
        self.assertEqual(self.client.post("/api/phantom/run", headers=AUTH, json=stale).status_code, 400)
        missing = self._intent("missing")
        missing["sol_usd"] = "0"
        self.assertEqual(self.client.post("/api/phantom/run", headers=AUTH, json=missing).status_code, 400)
        phantom_runner.set_halt({"halted": True})
        self.assertEqual(phantom_runner.run_intent(self._intent("held"))["reason"], "kill_switch")
        self.assertEqual(db.query("SELECT intent_id FROM phantom_reservations WHERE intent_id = 'held'"), [])
        phantom_runner.set_halt({"halted": False})
        opened = phantom_runner.run_intent(self._intent("open", fee="50.00"), runtime=ExitBlocked())
        self.assertEqual(opened["status"], "sim_open")
        blocked = phantom_runner.run_intent(self._intent("next", fee="50.00"))
        self.assertEqual(blocked["reason"], "bankroll")
        self.assertEqual(db.query("SELECT status FROM phantom_reservations WHERE intent_id = 'next'")[0]["status"], "rejected")
        now = time.time()
        db.execute(
            """
            INSERT INTO phantom_reservations (
                intent_id, status, wallet_id, wallet, chain, pair_json, amount_usd, fee_usd,
                reserved_usd, slippage, entry_quote_id, exit_quote_id, fx_sol_usd, fx_at,
                result_json, created_at, updated_at
            ) VALUES ('unknown', 'submit_unknown', ?, ?, 'solana:mainnet', '{}', '8.00', '0.20',
                      '8.20', 1, NULL, NULL, '150.00', ?, NULL, ?, ?)
            """,
            (WALLET_ID, WALLET, now, now, now),
        )

        class Explode:
            name = "paper_simulation"

            def quote(self, _spec):
                raise AssertionError("resubmit")

        unknown = phantom_runner.run_intent(self._intent("unknown"), runtime=Explode())
        self.assertEqual(unknown["reason"], "reconcile_before_resubmit")
        self.assertFalse(unknown["resubmit"])
        reconciled = self.client.post("/api/phantom/reconcile", headers=AUTH, json={"intent_id": "unknown"})
        self.assertEqual(reconciled.status_code, 200)
        self.assertEqual(reconciled.json()["reason"], "reconciled_without_resubmit")
        self.assertEqual(db.query("SELECT status FROM phantom_reservations WHERE intent_id = 'unknown'")[0]["status"], "released")
        os.environ["DESK_PHANTOM_PAPER"] = "0"
        phantom_runner.set_halt({"halted": False})
        auth = phantom_runner.run_intent(self._intent("auth"))
        self.assertEqual(auth["reason"], "AWAITING_OWNER_AUTH_PROCEDURE")
        self.assertTrue(phantom_runner.halted())
        self.assertEqual(db.query("SELECT kind FROM phantom_alerts")[0]["kind"], "session_halt")
        self.assertEqual(db.query("SELECT intent_id FROM phantom_reservations WHERE intent_id = 'auth'"), [])
        db.set_setting("mode", "LIVE", "test")
        phantom_runner.set_halt({"halted": False})
        os.environ["DESK_PHANTOM_PAPER"] = "1"
        self.assertEqual(self.client.post("/api/phantom/run", headers=AUTH, json=self._intent("live")).status_code, 409)
        self.assertEqual(db.get_setting("paper_start_stack"), "200")
        self.assertEqual(db.get_setting("agents_enabled"), "0")

    def test_same_intent_is_single_flight(self):
        results = []

        def once():
            results.append(phantom_runner.run_intent(self._intent("same")))

        threads = [threading.Thread(target=once) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(db.query("SELECT intent_id FROM phantom_reservations WHERE intent_id = 'same'")), 1)
        self.assertEqual(results[0]["status"], "sim_closed")
        self.assertEqual(results[1]["exit_quote_id"], results[0]["exit_quote_id"])

    def _intent(self, intent_id, quote_source="paper", fee="0.20"):
        return {
            "intent_id": intent_id,
            "wallet_id": WALLET_ID,
            "wallet": WALLET,
            "chain": "solana:mainnet",
            "sell_native": True,
            "buy_mint": USDC,
            "slippage": 1,
            "sol_usd": "150.00",
            "fx_at": time.time(),
            "quote_source": quote_source,
            "fee_usd": fee,
        }


class ExitBlocked(phantom_runner.PaperRuntime):
    def quote(self, spec):
        if spec["side"] == "exit":
            quote = super().quote(spec)
            quote["quote_id"] = spec.get("entry") or quote["quote_id"]
            quote["sim_fill_allowed"] = False
            return quote
        return super().quote(spec)
