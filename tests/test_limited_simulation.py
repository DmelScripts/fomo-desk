"""Manual rehearsal is fixed, idempotent, guarded, and entirely offline."""

import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fastapi import HTTPException

import db
import execution_transport
import limited_simulation
import phantom_runner
import risk_limits
from tests.support import temp_desk_env


class TestLimitedSimulation(unittest.TestCase):
    def setUp(self):
        self.tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        environment = patch.dict(os.environ, {"DESK_AGENTS": "0", "DESK_PHANTOM_PAPER": "0"})
        environment.start()
        self.addCleanup(environment.stop)
        db.init_db()
        self.approve()
        db.execute("UPDATE phantom_control SET value='0' WHERE key='halt'")

    def approve(self):
        risk_limits.approve({"max_total_loss_usd": "10.00", "max_round_trip_fee_usd": "0.20"})

    def test_fixed_round_trip_is_synthetic_and_never_calls_network_or_cli(self):
        with patch("subprocess.run", side_effect=AssertionError("CLI called")), \
             patch("subprocess.Popen", side_effect=AssertionError("process started")), \
             patch("httpx.post", side_effect=AssertionError("network called")), \
             patch.object(execution_transport, "submit", side_effect=AssertionError("signer called")):
            before = limited_simulation.status()
            result = limited_simulation.run("fresh")
            after = limited_simulation.status()
        self.assertTrue(before["ready"])
        self.assertEqual(result["status"], "sim_closed")
        self.assertEqual(result["realized_pnl"], "-0.20")
        self.assertEqual(result["amount_usd"], "8.00")
        self.assertEqual(result["entry_price"], "1.00")
        self.assertEqual(result["exit_price"], "1.00")
        self.assertEqual(result["assumptions"]["sol_price_usd"], "100.00")
        self.assertEqual(result["labels"], ["simulation", "synthetic_prices", "no_real_orders"])
        for key in ("send", "signer_called", "live_ready", "executable", "network_used", "agents_started"):
            self.assertFalse(result[key])
        row = db.query("SELECT * FROM phantom_reservations")[0]
        self.assertEqual(row["intent_id"], "sol-usdc-sim-fresh")
        self.assertEqual(row["reserved_usd"], "0.00")
        self.assertEqual(row["fee_usd"], "0.20")
        self.assertEqual(after["latest_result"], result)
        self.assertEqual(after["risk_limits"]["cumulative_realized_losses_usd"], "0.20")
        self.assertEqual(db.get_setting("agents_enabled"), "0")
        self.assertEqual(db.get_setting("mode"), "PAPER")

    def test_retry_is_idempotent_even_after_new_entry_capacity_is_consumed(self):
        first = limited_simulation.run("same")
        self.assertEqual(limited_simulation.run("same"), first)
        for index in range(9):
            self.assertEqual(limited_simulation.run(f"other-{index}")["status"], "sim_closed")
        self.assertEqual(len(db.query("SELECT intent_id FROM phantom_reservations")), 10)
        self.assertEqual(risk_limits.view()["cumulative_realized_losses_usd"], "2.00")
        self.assertFalse(limited_simulation.status()["ready"])
        self.assertEqual(limited_simulation.status()["reason"], "total_loss_limit")
        self.assertEqual(limited_simulation.run("same"), first)
        blocked = limited_simulation.run("over-limit")
        self.assertEqual(blocked["reason"], "total_loss_limit")
        self.assertEqual(blocked["status"], "rejected")
        self.assertEqual(risk_limits.view()["cumulative_realized_losses_usd"], "2.00")

    def test_pause_and_unapproved_or_changed_policy_never_get_auto_cleared(self):
        db.execute("UPDATE phantom_control SET value='1' WHERE key='halt'")
        self.assertTrue(limited_simulation.status()["halted"])
        with self.assertRaises(HTTPException) as caught:
            limited_simulation.run("paused")
        self.assertEqual(caught.exception.detail, "simulation_paused")
        self.assertTrue(phantom_runner.halted())
        db.execute("UPDATE phantom_control SET value='0' WHERE key='halt'")
        risk_limits.clear()
        with self.assertRaises(HTTPException) as caught:
            limited_simulation.run("revoked")
        self.assertEqual(caught.exception.detail, "approval_revoked")
        risk_limits.approve({"max_total_loss_usd": "20.00", "max_round_trip_fee_usd": "0.20"})
        self.assertEqual(limited_simulation.status()["reason"], "selected_limits_required")
        with self.assertRaises(HTTPException):
            limited_simulation.run("different-limits")
        self.assertEqual(db.query("SELECT intent_id FROM phantom_reservations"), [])

    def test_agents_and_live_mode_block_the_manual_simulation(self):
        db.set_setting("agents_enabled", "1", "test")
        self.assertEqual(limited_simulation.status()["reason"], "agents_must_be_off")
        with self.assertRaises(HTTPException):
            limited_simulation.run("agents")
        db.set_setting("agents_enabled", "0", "test")
        with patch.dict(os.environ, {"DESK_AGENTS": "1"}):
            self.assertEqual(limited_simulation.status()["reason"], "agents_must_be_off")
            with self.assertRaises(HTTPException):
                limited_simulation.run("env-agents")
        db.set_setting("mode", "LIVE", "test")
        self.assertEqual(limited_simulation.status()["reason"], "paper_mode_required")
        with self.assertRaises(HTTPException):
            limited_simulation.run("live")
        self.assertEqual(db.query("SELECT intent_id FROM phantom_reservations"), [])

    def test_inputs_cannot_override_amount_price_pair_or_other_intents(self):
        for value in (None, True, {}, {"intent_id": "a", "fee_usd": "0.01"}, "", "a b", "x" * 68):
            with self.subTest(value=value):
                with self.assertRaises(HTTPException) as caught:
                    limited_simulation.run(value)
                self.assertEqual(caught.exception.status_code, 400)
        limited_simulation.run("collision")
        db.execute("UPDATE phantom_reservations SET fee_usd='0.10' WHERE intent_id='sol-usdc-sim-collision'")
        with self.assertRaises(HTTPException) as caught:
            limited_simulation.run("collision")
        self.assertEqual(caught.exception.detail, "simulation_intent_conflict")
        self.assertIsNone(limited_simulation.status()["latest_result"])

    def test_agent_change_before_transaction_is_observed_and_blocks_quote(self):
        prepared = threading.Event()
        resume = threading.Event()
        original_spec = phantom_runner._spec

        def pause_after_spec(body):
            value = original_spec(body)
            prepared.set()
            if not resume.wait(timeout=5):
                raise AssertionError("test failed to release request")
            return value

        with patch.object(phantom_runner, "_spec", side_effect=pause_after_spec), \
             patch.object(phantom_runner.PaperRuntime, "quote", side_effect=AssertionError("quote called")):
            with ThreadPoolExecutor(max_workers=1) as pool:
                request = pool.submit(limited_simulation.run, "race")
                try:
                    self.assertTrue(prepared.wait(timeout=5))
                    db.set_setting("agents_enabled", "1", "test")
                finally:
                    resume.set()
                with self.assertRaises(HTTPException) as caught:
                    request.result(timeout=5)
                self.assertEqual(caught.exception.detail, "agents_must_be_off")
        self.assertEqual(db.query("SELECT intent_id FROM phantom_reservations"), [])


if __name__ == "__main__":
    unittest.main()
