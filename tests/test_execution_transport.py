"""The signer reads desk state. A caller-supplied all-clear is not accepted."""
import json
import time
import unittest

import db
import execution_transport
import exit_policy
import phantom_runner
import phantom_transport
import solana_tx
from tests.support import temp_desk_env
from tests.test_solana_inspect import BLOCK, FakeRpc

WALLET = phantom_transport.REQUIRED_WALLET
WALLET_ID = phantom_transport.REQUIRED_WALLET_ID


class RecordingSigner:
    def __init__(self):
        self.calls = []

    def sign_and_send_transaction(self, **kwargs):
        self.calls.append(kwargs)
        return {"signature": "not-a-real-signature"}


class ExecutionTransportTest(unittest.TestCase):
    def setUp(self):
        self.tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        db.init_db()
        db.set_setting("mode", "PAPER", "test")
        db.set_setting("agents_enabled", "0", "test")
        self.tx = solana_tx.encode_legacy_transfer(WALLET, WALLET, 10_000_000, BLOCK)
        self.signer = RecordingSigner()
        self.rpc = solana_inspect_rpc()

    def test_activation_is_not_ready_and_fee_policy_cannot_be_marked_approved(self):
        status = execution_transport.activation_status()
        self.assertFalse(status["ready"])
        self.assertEqual(status["agents"], ["scanner", "auditor", "analyst", "executor", "risk"])
        self.assertIn("fee_policy_approved", status["unmet"])
        self.assertIn("aggregate_loss_policy_approved", status["unmet"])
        self.assertIn("swap_path_verified", status["unmet"])
        self.assertIn("signer_enabled", status["unmet"])
        self.assertFalse(status["live_ready"])
        self.assertEqual(len(status["swap_blockers"]), 2)
        self.assertEqual(db.get_setting("mode"), "PAPER")
        self.assertEqual(db.get_setting("agents_enabled"), "0")

    def test_stuffed_flags_are_not_an_argument_and_do_not_call_the_signer(self):
        with self.assertRaises(TypeError):
            execution_transport.submit({"live_consent": True, "slippage_verified": True, "fee_policy": "approved"})
        result = execution_transport.submit("intent-1", self.tx, signer=self.signer, rpc=self.rpc)
        self.assertEqual(self.signer.calls, [])
        self.assertFalse(result["signer_called"])
        self.assertIsNone(result["signature"])
        self.assertFalse(result["send"])
        self.assertIn("fee_policy_approved", result["unmet"])
        self.assertIn("slippage_verified", result["unmet"])
        self.assertIn("aggregate_loss_policy_approved", result["unmet"])

    def test_exit_approval_and_live_request_do_not_approve_fees_or_send(self):
        exit_policy.set_approval(True)
        self.assertTrue(exit_policy.view()["effective"])
        self.assertFalse(exit_policy.view()["live_consent"])
        now = time.time()
        db.execute(
            """
            INSERT INTO phantom_control (key, value, updated_at) VALUES (?, ?, ?)
            """,
            (
                execution_transport._CONSENT,
                json.dumps({
                    "live_trading_requested": True,
                    "agents_requested": ["scanner", "auditor", "analyst", "executor", "risk"],
                    "applied": False,
                    "fee_policy": "not_approved",
                    "aggregate_loss_policy": False,
                    "statement": execution_transport._STATEMENT,
                }),
                now,
            ),
        )
        phantom_runner.set_halt({"halted": False})
        self._open_reservation("intent-2")
        execution_transport.remember_review("intent-2", self.tx)
        result = execution_transport.submit("intent-2", self.tx, signer=self.signer, rpc=self.rpc)
        self.assertEqual(self.signer.calls, [])
        self.assertNotIn("exit_tuple_effective", result["unmet"])
        self.assertNotIn("live_request_recorded", result["unmet"])
        self.assertIn("fee_policy_approved", result["unmet"])
        self.assertIn("aggregate_loss_policy_approved", result["unmet"])
        self.assertIn("slippage_verified", result["unmet"])
        self.assertEqual(db.get_setting("mode"), "PAPER")
        self.assertEqual(db.get_setting("agents_enabled"), "0")
        self.assertFalse(execution_transport.activation_status()["ready"])

    def test_kill_switch_wallet_and_duplicate_block_before_a_signer_call(self):
        phantom_runner.set_halt({"halted": True})
        self._open_reservation("intent-3")
        execution_transport.remember_review("intent-3", self.tx)
        halted = execution_transport.submit("intent-3", self.tx, signer=self.signer, rpc=self.rpc)
        self.assertIn("kill_switch_open", halted["unmet"])
        self.assertEqual(self.signer.calls, [])
        phantom_runner.set_halt({"halted": False})
        foreign = solana_tx.encode_legacy_transfer(solana_tx.b58encode(b"\x22" * 32), WALLET, 10_000_000, BLOCK)
        execution_transport.remember_review("intent-4", foreign)
        self._open_reservation("intent-4")
        mismatched = execution_transport.submit("intent-4", foreign, signer=self.signer, rpc=self.rpc)
        self.assertIn("wallet_network", mismatched["unmet"])
        self.assertEqual(self.signer.calls, [])
        execution_transport._write(execution_transport._LEDGER, {"intent-3": {"sha256": "abc", "status": "submit_unknown"}})
        db.init_db()
        phantom_runner.set_halt({"halted": False})
        again = execution_transport.submit("intent-3", self.tx, signer=self.signer, rpc=self.rpc)
        self.assertIn("idempotent", again["unmet"])
        self.assertEqual(self.signer.calls, [])
        self.assertEqual(execution_transport.reconcile("intent-3")["status"], "refused")
        self.assertEqual(execution_transport.reconcile("missing")["status"], "not_run")

    def test_unknown_submit_is_not_resent(self):
        now = time.time()
        db.execute(
            """
            INSERT INTO phantom_reservations (
                intent_id, status, wallet_id, wallet, chain, pair_json, amount_usd, fee_usd,
                reserved_usd, slippage, entry_quote_id, exit_quote_id, fx_sol_usd, fx_at,
                result_json, created_at, updated_at
            ) VALUES ('intent-5', 'submit_unknown', ?, ?, 'solana:mainnet', '{}', '8.00', '0.20',
                      '8.20', 1, 'entry', NULL, '150.00', ?, NULL, ?, ?)
            """,
            (WALLET_ID, WALLET, now, now, now),
        )
        execution_transport.remember_review("intent-5", self.tx)
        phantom_runner.set_halt({"halted": False})
        result = execution_transport.submit("intent-5", self.tx, signer=self.signer, rpc=self.rpc)
        self.assertIn("unknown_submit_clear", result["unmet"])
        self.assertEqual(self.signer.calls, [])

    def _open_reservation(self, intent_id):
        now = time.time()
        db.execute(
            """
            INSERT INTO phantom_reservations (
                intent_id, status, wallet_id, wallet, chain, pair_json, amount_usd, fee_usd,
                reserved_usd, slippage, entry_quote_id, exit_quote_id, fx_sol_usd, fx_at,
                result_json, created_at, updated_at
            ) VALUES (?, 'sim_open', ?, ?, 'solana:mainnet', '{}', '8.00', '0.20',
                      '8.20', 1, 'entry', NULL, '150.00', ?, NULL, ?, ?)
            """,
            (intent_id, WALLET_ID, WALLET, now, now, now),
        )


def solana_inspect_rpc():
    import solana_inspect
    return solana_inspect.RpcClient(FakeRpc())
