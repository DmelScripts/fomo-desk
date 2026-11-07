"""Adversarial checks for the release blockers. Fake RPC and temp databases only."""
import hashlib
import json
import time
import unittest

import db
import execution_transport
import exit_policy
import phantom_runner
import phantom_transport
import solana_direct_swap
import solana_read_rpc
import solana_tx
import spend_policy
import usdc_valuation
from tests.support import temp_desk_env
from tests.whirlpool_fixture import reader

WALLET = phantom_transport.REQUIRED_WALLET
WALLET_ID = phantom_transport.REQUIRED_WALLET_ID


class ExploitRejectionTests(unittest.TestCase):
    def setUp(self):
        _tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        db.init_db()
        db.set_setting("mode", "PAPER", "test")
        db.set_setting("agents_enabled", "0", "test")
        solana_read_rpc.TRANSPORT = None
        self.addCleanup(lambda: setattr(solana_read_rpc, "TRANSPORT", None))
        self.addCleanup(lambda: usdc_valuation.set_quote(None))

    def test_caller_bytes_and_a_cheap_pool_do_not_reach_send(self):
        cheap = dict(solana_direct_swap.SOL_USDC_SNAPSHOT)
        cheap["sqrt_price"] = 1165280000000000000
        proved = solana_direct_swap.prove("AAAA", {solana_direct_swap.SOL_USDC_POOL: ("owner", b"")}, 1, False, network_fee_lamports=1, rent_lamports=1)
        self.assertFalse(proved["slippage_verified"])
        self.assertFalse(proved["fee_cap_ok"])
        self.assertNotIn(solana_direct_swap.INPUT_LAMPORTS, solana_direct_swap._allowed_amounts({"sqrt_price": cheap["sqrt_price"]}))
        phantom_runner.set_halt({"halted": False})
        spend_policy.persist_owner_approval()
        exit_policy.set_approval(True)
        calls = []
        result = execution_transport.submit(
            "cheap", "AAAA", runner=lambda *args: calls.append(args),
            direct_state={"accounts": {solana_direct_swap.SOL_USDC_POOL: ("x", b"y")}, "slippage_verified": True, "network_fee_lamports": 1, "rent_lamports": 1, "context_slot": 1, "wsol_exists": False},
            blockhash_source=lambda: {"blockhash": "echo", "slot": 1, "last_valid_block_height": 9, "simulated": True, "simulation": {"err": None}},
        )
        self.assertEqual(calls, [])
        self.assertIn("caller_state_refused", result["unmet"])
        self.assertIn("usd_source_approved", result["unmet"])
        self.assertFalse(result["checks"]["slippage_verified"])

    def test_owner_release_does_not_refill_and_loss_does_not_add_the_fee_twice(self):
        now = time.time()
        db.execute(
            """
            INSERT INTO phantom_reservations (
                intent_id, status, wallet_id, wallet, chain, pair_json, amount_usd, fee_usd,
                reserved_usd, slippage, entry_quote_id, exit_quote_id, fx_sol_usd, fx_at,
                result_json, created_at, updated_at, realized_pnl
            ) VALUES ('held-real', 'submit_unknown', ?, ?, 'solana:mainnet', ?, '8.00', '0.01',
                      '8.14', 1, NULL, NULL, '89.00', ?, ?, ?, ?, NULL)
            """,
            (WALLET_ID, WALLET, json.dumps({"origin": "direct_whirlpool", "chain_submit": True}), now, json.dumps({"origin": "direct_whirlpool", "chain_submit": True}), now, now),
        )
        released = phantom_runner.owner_release({"intent_id": "held-real", "release": True})
        self.assertEqual(released["reason"], "release_disabled")
        self.assertEqual(db.query("SELECT status, reserved_usd FROM phantom_reservations WHERE intent_id = 'held-real'")[0]["reserved_usd"], "8.14")
        self.assertEqual(phantom_runner.allocation()["open_hold_usd"], "8.14")
        db.execute(
            """
            INSERT INTO phantom_reservations (
                intent_id, status, wallet_id, wallet, chain, pair_json, amount_usd, fee_usd,
                reserved_usd, slippage, entry_quote_id, exit_quote_id, fx_sol_usd, fx_at,
                result_json, created_at, updated_at, realized_pnl
            ) VALUES ('net-loss', 'sim_closed', ?, ?, 'solana:mainnet', '{}', '8.00', '5.00',
                      '0.00', 1, NULL, NULL, '89.00', ?, NULL, ?, ?, '-19.99')
            """,
            (WALLET_ID, WALLET, now, now, now),
        )
        conn = db.connect()
        try:
            self.assertEqual(phantom_runner.cumulative_realized_loss(conn), __import__("decimal").Decimal("19.99"))
        finally:
            conn.close()

    def test_exit_uses_only_a_bounded_confirmed_delta(self):
        self.assertEqual(solana_direct_swap.exit_amount(188718142, 188718141, confirmed=True, minimum=1, estimated=188718142)["reason"], "above_balance")
        self.assertFalse(solana_direct_swap.exit_amount(7995630, 188718141, confirmed=False, minimum=7915673, estimated=7995630)["executable"])
        allowed = solana_direct_swap.exit_amount(7950000, 188718141, confirmed=True, minimum=7915673, estimated=7995630, ata_balance=188718141 + 7950000)
        self.assertTrue(allowed["allowed"])
        self.assertEqual(allowed["exit_raw"], 7950000)
        with self.assertRaises(ValueError) as refused:
            solana_direct_swap.build_exit(
                solana_direct_swap.SOL_USDC_POOL, b"", {}, 100, "hash", 1, 188718141, 188718141,
                minimum=7915673, estimated=7995630, ata_balance=188718141,
            )
        self.assertEqual(str(refused.exception), "outside_fill")

    def test_halt_on_legacy_transfer_never_starts_the_cli(self):
        now = time.time()
        tx = solana_tx.encode_legacy_transfer(WALLET, WALLET, 10_000_000, solana_tx.b58encode(b"\x11" * 32))
        db.execute(
            """
            INSERT INTO phantom_reservations (
                intent_id, status, wallet_id, wallet, chain, pair_json, amount_usd, fee_usd,
                reserved_usd, slippage, entry_quote_id, exit_quote_id, fx_sol_usd, fx_at,
                result_json, created_at, updated_at
            ) VALUES ('legacy', 'sim_open', ?, ?, 'solana:mainnet', '{}', '8.00', '0.00',
                      '8.00', 1, NULL, NULL, '89', ?, NULL, ?, ?)
            """,
            (WALLET_ID, WALLET, now, now, now),
        )
        execution_transport.remember_review("legacy", tx)
        calls = []
        refused = execution_transport.send_after_identity("legacy", tx, lambda *args: calls.append(args), binary="phantom")
        self.assertEqual(calls, [])
        self.assertEqual(refused["subprocess"], [])
        self.assertEqual(refused["reason"], "kill_switch")
        phantom_runner.set_halt({"halted": False})
        spend_policy.persist_owner_approval()
        still = execution_transport.send_after_identity("legacy", tx, lambda *args: calls.append(args), binary="phantom")
        self.assertEqual(calls, [])
        self.assertEqual(still["reason"], "usd_source_unapproved")
        self.assertEqual(still["cli_transport"], "argv_only")
        self.assertFalse(still["stdin_transaction"])

    def test_activation_stays_unready_and_consent_dies_when_policy_changes(self):
        now = time.time()
        db.execute(
            "INSERT INTO phantom_control (key, value, updated_at) VALUES (?, ?, ?)",
            (execution_transport._CONSENT, json.dumps({
                "live_trading_requested": True, "agents_requested": list(db.AGENT_NAMES),
                "applied": False, "fee_policy": "not_approved", "aggregate_loss_policy": False,
                "statement": execution_transport._STATEMENT,
            }), now),
        )
        self.assertIsNotNone(execution_transport._consent())
        spend_policy.persist_owner_approval()
        self.assertIsNone(execution_transport._consent())
        status = execution_transport.activation_status()
        self.assertFalse(status["ready"])
        self.assertIn("usd_source_approved", status["unmet"])
        self.assertIn("swap_path_verified", status["unmet"])
        self.assertIn("kill_switch_open", status["unmet"])
        self.assertFalse(status["live_ready"])
        self.assertFalse(status["checks"]["signer_enabled"])
        self.assertFalse(solana_direct_swap.route_guard()["swap_path_verified"])
        phantom_runner.set_halt({"halted": False})
        skipped = phantom_runner.on_qualified_signal({"qualified": True, "mint": "Mint", "price": 1, "sol_usd": "89", "fx_at": time.time()})
        self.assertEqual(skipped["reason"], "fee_policy")
        self.assertEqual(db.query("SELECT intent_id FROM phantom_reservations WHERE intent_id = 'sig-Mint'"), [])

    def test_slot_lag_and_simulation_failure_do_not_insert(self):
        lagged = reader(tip=466547240 + 200)
        bundle = solana_read_rpc.load_bundle(lagged)
        self.assertEqual(bundle["reason"], "slot_lag")
        echo = reader(tip=466547240)
        echoed = solana_read_rpc.load_bundle(echo)
        self.assertEqual(echoed["reason"], "slot_echo")
        self.assertFalse(echoed["independent_slot_time_proven"])
        self.assertNotIn("getFeeForMessage", lagged.methods)
        self.assertNotIn("simulateTransaction", lagged.methods)
        spend_policy.persist_owner_approval()
        phantom_runner.set_halt({"halted": False})
        failed = reader(sim_err={"InstructionError": [0, {"Custom": 1}]})
        saved = execution_transport.reserve_from_reader("sim-fail", failed)
        self.assertEqual(saved["reason"], "simulation_failed")
        self.assertEqual(db.query("SELECT intent_id FROM phantom_reservations WHERE intent_id = 'sim-fail'"), [])
        self.assertIsNone(spend_policy.usd_source()["accepted_band"])
        self.assertFalse(spend_policy.usd_source()["approved"])

    def test_a_policy_change_during_the_read_does_not_insert(self):
        spend_policy.persist_owner_approval()
        phantom_runner.set_halt({"halted": False})
        usdc_valuation.enable()
        usdc_valuation.set_quote({
            "price": 99_990_000, "conf": 10_000, "expo": -8,
            "publish_time": int(time.time()), "feed_id": usdc_valuation.FEED_ID,
        })
        stamp = spend_policy.policy_stamp()
        prepared, _entry, _exit, _reader = execution_transport._assemble_read(466547240, reader())
        spend_policy.persist_owner_approval()
        saved = execution_transport.reserve_direct("stale-policy", prepared=prepared, stamp=stamp, simulation_clean=True)
        self.assertEqual(saved["reason"], "policy_changed")
        self.assertFalse(saved["created"])
        self.assertEqual(db.query("SELECT intent_id FROM phantom_reservations WHERE intent_id = 'stale-policy'"), [])

    def test_a_transfer_to_the_pool_does_not_spawn_when_usd_is_on(self):
        spend_policy.persist_owner_approval()
        phantom_runner.set_halt({"halted": False})
        usdc_valuation.enable()
        usdc_valuation.set_quote({
            "price": 99_990_000, "conf": 10_000, "expo": -8,
            "publish_time": int(time.time()), "feed_id": usdc_valuation.FEED_ID,
        })
        pool = solana_direct_swap.SOL_USDC_POOL
        tx = solana_tx.encode_legacy_transfer(WALLET, pool, 2_000_000_000, solana_tx.b58encode(b"\x44" * 32))
        message = hashlib.sha256(solana_tx.message_bytes(tx)).hexdigest()
        now = time.time()
        db.execute(
            """
            INSERT INTO phantom_reservations (
                intent_id, status, wallet_id, wallet, chain, pair_json, amount_usd, fee_usd,
                reserved_usd, slippage, entry_quote_id, exit_quote_id, fx_sol_usd, fx_at,
                result_json, created_at, updated_at
            ) VALUES ('transfer', 'sim_open', ?, ?, 'solana:mainnet', '{}', '8.00', '0.01',
                      '8.14', 1, NULL, NULL, '89', ?, ?, ?, ?)
            """,
            (WALLET_ID, WALLET, now, json.dumps({
                "origin": "direct_whirlpool", "message_sha256": message,
                "entry_lamports": 2_000_000_000, "entry_token_min_out": 1,
            }), now, now),
        )
        execution_transport.remember_review("transfer", tx)
        calls = []
        refused = execution_transport.send_after_identity("transfer", tx, lambda *args: calls.append(args), binary="phantom")
        self.assertEqual(calls, [])
        self.assertEqual(refused["reason"], "swap_missing")
        self.assertEqual(db.query("SELECT status FROM phantom_reservations WHERE intent_id = 'transfer'")[0]["status"], "sim_open")

    def test_missing_stamp_and_a_false_cap_do_not_insert(self):
        prepared = {
            "technical_ok": True, "fee_cap_ok": False, "exposure_cap_ok": False, "usd_cap_ok": False,
            "reason": "cap", "exposure_micro_usdc": 8_000_000, "fee_micro_usdc": 1000,
            "marginal_micro_usdc": 8_000_000, "context_slot": 1, "quote_token_est_out": 1,
            "quote_token_min_out": 1, "wallet_usdc_raw": 1, "message_sha256": "abc", "entry_lamports": 1,
        }
        spend_policy.persist_owner_approval()
        phantom_runner.set_halt({"halted": False})
        missing = execution_transport.reserve_direct("no-stamp", prepared=dict(prepared), stamp=None, simulation_clean=True)
        self.assertEqual(missing["reason"], "stamp_missing")
        capped = execution_transport.reserve_direct("false-cap", prepared=dict(prepared), stamp=spend_policy.policy_stamp(), simulation_clean=True)
        self.assertEqual(capped["reason"], "cap")
        self.assertEqual(db.query("SELECT intent_id FROM phantom_reservations"), [])

    def test_nineteen_ninety_nine_still_allows_a_new_entry_and_does_not_guarantee_the_stop(self):
        self.assertFalse(spend_policy.target()["guaranteed_loss_limit"])
        self.assertTrue(spend_policy.target()["an_open_trade_can_realize_past_the_stop"])
        self.assertTrue(spend_policy.target()["open_exposure_is_not_realized_loss"])
        spend_policy.persist_owner_approval()
        phantom_runner.set_halt({"halted": False})
        usdc_valuation.enable()
        usdc_valuation.set_quote({
            "price": 99_990_000, "conf": 10_000, "expo": -8,
            "publish_time": int(time.time()), "feed_id": usdc_valuation.FEED_ID,
        })
        now = time.time()
        db.execute(
            """
            INSERT INTO phantom_reservations (
                intent_id, status, wallet_id, wallet, chain, pair_json, amount_usd, fee_usd,
                reserved_usd, slippage, entry_quote_id, exit_quote_id, fx_sol_usd, fx_at,
                result_json, created_at, updated_at, realized_pnl
            ) VALUES ('almost', 'sim_closed', ?, ?, 'solana:mainnet', '{}', '8.00', '5.00',
                      '0.00', 1, NULL, NULL, '89', ?, NULL, ?, ?, '-19.99')
            """,
            (WALLET_ID, WALLET, now, now, now),
        )
        saved = execution_transport.reserve_from_reader("next", reader())
        self.assertTrue(saved["created"], saved)
        self.assertNotEqual(saved["reason"], "stop_new_entries")

    def test_an_echoed_estimate_is_not_a_receipt_and_a_real_delta_is_persisted(self):
        phantom_runner.CHAIN_STATUS = lambda _signature: {
            "err": None, "confirmation_status": "finalized", "acquired_raw": 7995630,
            "signature": "1" * 64, "network": "solana:mainnet", "wallet": WALLET,
        }
        echo = phantom_runner.apply_chain_proof("no-row", "1" * 64, 10)
        self.assertFalse(echo.get("confirmed", False))
        self.assertNotEqual(echo.get("reason"), "signature_status")
