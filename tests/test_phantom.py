"""PAPER Phantom runner. No login, no send, no LIVE unlock."""
import asyncio
import json
import os
import threading
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import db
import phantom_auth
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
        phantom_auth.reset_for_tests()
        self.addCleanup(phantom_auth.reset_for_tests)
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
        self.assertTrue(account["phantom"]["signals_connected"])
        self.assertTrue(account["phantom"]["scheduled_exits_connected"])
        self.assertEqual(account["phantom"]["intake"], "qualified_signal")
        self.assertFalse(account["phantom"]["unrealized_spendable"])
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
        self.assertEqual(reconciled.json()["reason"], "chain_reconciliation_required")
        self.assertEqual(db.query("SELECT status FROM phantom_reservations WHERE intent_id = 'unknown'")[0]["status"], "submit_unknown")
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

    def test_kill_switch_blocks_new_entries_and_still_exits(self):
        opened = phantom_runner.run_intent(self._intent("held-open", fee="0.20"), runtime=ExitBlocked())
        self.assertEqual(opened["status"], "sim_open")
        phantom_runner.set_halt({"halted": True})
        fresh = phantom_runner.run_intent(self._intent("after-halt"))
        self.assertEqual(fresh["reason"], "kill_switch")
        self.assertEqual(db.query("SELECT intent_id FROM phantom_reservations WHERE intent_id = 'after-halt'"), [])
        closed = phantom_runner.run_intent(self._intent("held-open", fee="0.20"))
        self.assertEqual(closed["status"], "sim_closed")
        self.assertTrue(closed["reconciled"])
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM phantom_reservations WHERE intent_id = 'held-open'")[0]["n"], 1)
        now = time.time()
        db.execute(
            """
            INSERT INTO phantom_reservations (
                intent_id, status, wallet_id, wallet, chain, pair_json, amount_usd, fee_usd,
                reserved_usd, slippage, entry_quote_id, exit_quote_id, fx_sol_usd, fx_at,
                result_json, created_at, updated_at
            ) VALUES ('halt-unknown', 'submit_unknown', ?, ?, 'solana:mainnet', '{}', '8.00', '0.20',
                      '8.20', 1, 'entry-old', NULL, '150.00', ?, NULL, ?, ?)
            """,
            (WALLET_ID, WALLET, now, now, now),
        )
        reconciled = phantom_runner.reconcile_unknown({"intent_id": "halt-unknown"})
        self.assertEqual(reconciled["reason"], "chain_reconciliation_required")
        self.assertFalse(reconciled["reconciled"])
        self.assertEqual(db.query("SELECT status, reserved_usd FROM phantom_reservations WHERE intent_id = 'halt-unknown'")[0]["status"], "submit_unknown")
        self.assertEqual(db.query("SELECT reserved_usd FROM phantom_reservations WHERE intent_id = 'halt-unknown'")[0]["reserved_usd"], "8.20")
        self.assertTrue(phantom_runner.halted())

    def test_cap_survives_restart_and_is_not_daily_turnover(self):
        opened = phantom_runner.run_intent(self._intent("stay", fee="50.00"), runtime=ExitBlocked())
        self.assertEqual(opened["status"], "sim_open")
        held = db.query("SELECT reserved_usd FROM phantom_reservations WHERE intent_id = 'stay'")[0]["reserved_usd"]
        self.assertEqual(held, "58.00")
        db.init_db()
        self.assertEqual(db.query("SELECT reserved_usd FROM phantom_reservations WHERE intent_id = 'stay'")[0]["reserved_usd"], "58.00")
        self.assertEqual(db.get_setting("paper_start_stack"), "200")
        phantom_runner.set_halt({"halted": False})
        blocked = phantom_runner.run_intent(self._intent("after-restart", fee="50.00"))
        self.assertEqual(blocked["reason"], "bankroll")
        closed = phantom_runner.run_intent(self._intent("turn", fee="0.20"))
        self.assertEqual(closed["status"], "sim_closed")
        nxt = phantom_runner.run_intent(self._intent("turn-2", fee="0.20"))
        self.assertEqual(nxt["status"], "sim_closed")
        self.assertEqual(nxt["amount_usd"], "8.00")
        spent = db.query(
            "SELECT fee_usd FROM phantom_reservations WHERE status = 'sim_closed' AND intent_id = 'turn'"
        )[0]["fee_usd"]
        self.assertEqual(spent, "0.20")

    def test_concurrent_reservations_do_not_exceed_the_cap(self):
        results = []

        def once(name):
            results.append(phantom_runner.run_intent(self._intent(name, fee="50.00"), runtime=ExitBlocked()))

        threads = [threading.Thread(target=once, args=(name,)) for name in ("cap-a", "cap-b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        statuses = sorted(row["status"] for row in results)
        self.assertEqual(statuses, ["rejected", "sim_open"])
        open_rows = db.query("SELECT reserved_usd FROM phantom_reservations WHERE status = 'sim_open'")
        self.assertEqual(len(open_rows), 1)
        self.assertEqual(open_rows[0]["reserved_usd"], "58.00")

    def test_stale_quote_and_wallet_mismatch_fail_closed(self):
        stale = phantom_runner.run_intent(self._intent("stale-quote"), runtime=StalePaper())
        self.assertEqual(stale["reason"], "paper_validity")
        self.assertEqual(db.query("SELECT status FROM phantom_reservations WHERE intent_id = 'stale-quote'")[0]["status"], "released")
        invented = phantom_runner.run_intent(self._intent("invented"), runtime=InventedExpiry())
        self.assertEqual(invented["reason"], "invented_provider_expiry")
        self.assertNotEqual(db.query("SELECT status FROM phantom_reservations WHERE intent_id = 'invented'")[0]["status"], "sim_closed")
        wrong = self._intent("wrong-wallet")
        wrong["wallet"] = "4tKiUbkTbbcPqrTDsjmMWgZWZ7mZucMKJqk2nHE2HP4f"
        self.assertEqual(self.client.post("/api/phantom/run", headers=AUTH, json=wrong).status_code, 400)
        wrong_id = self._intent("wrong-id")
        wrong_id["wallet_id"] = "00000000-0000-0000-0000-000000000000"
        self.assertEqual(self.client.post("/api/phantom/run", headers=AUTH, json=wrong_id).status_code, 400)
        self.assertEqual(db.query("SELECT intent_id FROM phantom_reservations WHERE intent_id IN ('wrong-wallet', 'wrong-id')"), [])
        opened = phantom_runner.run_intent(self._intent("auth-open"), runtime=ExitBlocked())
        self.assertEqual(opened["status"], "sim_open")
        os.environ["DESK_PHANTOM_PAPER"] = "0"

        class Explode:
            name = "blocked"

            def quote(self, _spec):
                raise AssertionError("quoted during auth failure")

        blocked = phantom_runner.run_intent(self._intent("auth-open"), runtime=Explode())
        self.assertEqual(blocked["status"], "sim_open")
        self.assertEqual(blocked["reason"], "exit_blocked_auth")
        self.assertEqual(db.query("SELECT status FROM phantom_reservations WHERE intent_id = 'auth-open'")[0]["status"], "sim_open")

    def test_agents_do_not_feed_the_phantom_runner(self):
        root = Path(__file__).resolve().parents[1]
        for name in (
            "agents/scanner.py", "agents/auditor.py", "agents/analyst.py",
            "main.py", "server.py", "report.py", "health.py", "notifier.py", "replay.py",
        ):
            text = (root / name).read_text(encoding="utf-8")
            self.assertNotIn("phantom_runner", text, name)
        self.assertIn("on_qualified_signal", (root / "agents" / "executor.py").read_text(encoding="utf-8"))
        self.assertIn("on_scheduled_exit", (root / "agents" / "risk.py").read_text(encoding="utf-8"))
        self.assertEqual(db.get_setting("agents_enabled"), "0")
        self.assertTrue(phantom_runner.public_status()["signals_connected"])
        skipped = self._intent("skip-me")
        skipped["price"] = None
        from agents.executor import Executor
        from bus import Desk
        from notifier import Notifier
        executor = Executor(Desk(http=None, notifier=Notifier(mode="PAPER")))
        result = asyncio.run(executor.handle({
            "mint": "4FHEBKNYy6BZ1QFMKj1xL8HDTM12rdXLRYw8DgiCUnQn",
            "symbol": "LABS",
            "liquidity_usd": 50000,
        }))
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(db.query("SELECT intent_id FROM phantom_reservations"), [])

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


    def test_qualified_signal_and_risk_exit_use_the_separate_allocation(self):
        from agents.executor import Executor
        from agents.risk import Risk
        from bus import Desk
        from notifier import Notifier
        mint = "4FHEBKNYy6BZ1QFMKj1xL8HDTM12rdXLRYw8DgiCUnQn"
        desk = Desk(http=None, notifier=Notifier(mode="PAPER"))
        entered = asyncio.run(Executor(desk).handle({
            "mint": mint,
            "symbol": "LABS",
            "price": 1,
            "liquidity_usd": 100000,
            "sol_usd": "150.00",
            "fx_at": time.time(),
            "phantom_fee_usd": "0.20",
        }))
        self.assertEqual(entered["sizeUsd"], 47.50)
        self.assertEqual(entered["status"], "bought")
        phantom = db.query("SELECT * FROM phantom_reservations WHERE intent_id = ?", ("sig-" + mint,))[0]
        self.assertEqual(phantom["status"], "sim_open")
        self.assertEqual(phantom["amount_usd"], "8.00")
        self.assertEqual(db.get_setting("paper_start_stack"), "200")
        self.assertEqual(db.get_setting("agents_enabled"), "0")
        self.assertEqual(db.get_setting("min_ticket_usd"), "47.50")
        books = phantom_runner.allocation()
        self.assertEqual(books["open_hold_usd"], "8.20")
        self.assertEqual(books["available_usd"], "91.80")
        self.assertEqual(books["unrealized_pnl"], "0.00")
        marked = phantom_runner.mark_open("sig-" + mint, "2")
        self.assertEqual(marked["unrealized_pnl"], "8.00")
        self.assertEqual(marked["available_usd"], "91.80")
        row = db.query("SELECT * FROM positions WHERE mint = ?", (mint,))[0]
        held = Risk(desk).poll_position(dict(row), time.time(), price=1)
        self.assertEqual(held["action"], "hold")
        self.assertEqual(db.query("SELECT status FROM phantom_reservations WHERE intent_id = ?", ("sig-" + mint,))[0]["status"], "sim_open")
        stopped = Risk(desk).poll_position(dict(db.query("SELECT * FROM positions WHERE mint = ?", (mint,))[0]), time.time(), price=0.5)
        self.assertEqual(stopped["action"], "stop")
        closed = db.query("SELECT * FROM phantom_reservations WHERE intent_id = ?", ("sig-" + mint,))[0]
        self.assertEqual(closed["status"], "sim_closed")
        self.assertEqual(closed["realized_pnl"], "-4.20")
        saved = json.loads(closed["result_json"])
        self.assertEqual(saved["entry_fee_usd"], "0.10")
        self.assertEqual(saved["exit_fee_usd"], "0.10")
        self.assertEqual(saved["provider"], "synthetic_paper")
        self.assertFalse(saved["live_capable"])
        self.assertEqual(saved["fill"], "simulated")
        after = phantom_runner.allocation()
        self.assertEqual(after["available_usd"], "95.80")
        self.assertEqual(after["realized_pnl"], "-4.20")
        self.assertEqual(db.get_setting("paper_start_stack"), "200")
        fomo = db.query("SELECT size_usd, pnl_usd FROM positions WHERE mint = ?", (mint,))[0]
        self.assertEqual(fomo["size_usd"], 47.50)
        self.assertNotEqual(fomo["pnl_usd"], -4.20)
        db.init_db()
        self.assertEqual(phantom_runner.allocation()["available_usd"], "95.80")
        again = phantom_runner.on_qualified_signal({
            "qualified": True, "mint": mint, "price": 1, "sol_usd": "150.00", "fx_at": time.time(),
        })
        self.assertEqual(again["status"], "sim_closed")
        self.assertEqual(phantom_runner.allocation()["available_usd"], "95.80")

    def test_losing_round_trip_is_not_refilled_from_the_paper_wallet(self):
        lost = phantom_runner.run_intent(self._intent("loss"), prices={"entry": "1", "exit": "0.5"})
        self.assertEqual(lost["realized_pnl"], "-4.20")
        self.assertEqual(lost["entry_fee_usd"], "0.10")
        self.assertEqual(lost["exit_fee_usd"], "0.10")
        self.assertEqual(phantom_runner.allocation()["available_usd"], "95.80")
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM positions")[0]["n"], 0)
        self.assertEqual(db.get_setting("paper_start_stack"), "200")
        db.init_db()
        self.assertEqual(phantom_runner.allocation()["available_usd"], "95.80")
        phantom_runner.run_intent(self._intent("loss"), prices={"entry": "1", "exit": "2"})
        self.assertEqual(phantom_runner.allocation()["available_usd"], "95.80")
        now = time.time()
        db.execute(
            """
            INSERT INTO phantom_reservations (
                intent_id, status, wallet_id, wallet, chain, pair_json, amount_usd, fee_usd,
                reserved_usd, slippage, entry_quote_id, exit_quote_id, fx_sol_usd, fx_at,
                result_json, created_at, updated_at
            ) VALUES ('held-unknown', 'submit_unknown', ?, ?, 'solana:mainnet', '{}', '8.00', '0.20',
                      '40.00', 1, NULL, NULL, '150.00', ?, NULL, ?, ?)
            """,
            (WALLET_ID, WALLET, now, now, now),
        )
        self.assertEqual(phantom_runner.allocation()["available_usd"], "55.80")
        blocked = phantom_runner.run_intent(self._intent("needs-room", fee="50.00"))
        self.assertEqual(blocked["reason"], "bankroll")
        phantom_runner.reconcile_unknown({"intent_id": "held-unknown"})
        self.assertEqual(phantom_runner.allocation()["available_usd"], "55.80")
        self.assertEqual(db.query("SELECT status FROM phantom_reservations WHERE intent_id = 'held-unknown'")[0]["status"], "submit_unknown")
        db.init_db()
        repeated = self.client.post("/api/phantom/reconcile", headers=AUTH, json={"intent_id": "held-unknown"})
        self.assertFalse(repeated.json()["reconciled"])
        self.assertEqual(phantom_runner.allocation()["available_usd"], "55.80")

class ExitBlocked(phantom_runner.PaperRuntime):
    def quote(self, spec):
        if spec["side"] == "exit":
            quote = super().quote(spec)
            quote["sim_fill_allowed"] = False
            return quote
        return super().quote(spec)


class StalePaper(phantom_runner.PaperRuntime):
    def quote(self, spec):
        quote = super().quote(spec)
        quote["paper_valid_until"] = time.time() - 30
        return quote


class InventedExpiry(phantom_runner.PaperRuntime):
    def quote(self, spec):
        quote = super().quote(spec)
        quote["provider_expiry"] = time.time() + 60
        return quote
