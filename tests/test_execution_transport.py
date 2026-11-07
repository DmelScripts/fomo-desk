"""The signer reads desk state. A caller-supplied all-clear is not accepted."""
import json
import subprocess
import threading
import time
import unittest

import db
import execution_transport
import exit_policy
import phantom_auth
import phantom_runner
import phantom_transport
import solana_tx
from tests.support import temp_desk_env
from tests.test_solana_inspect import BLOCK, FakeRpc

WALLET = phantom_transport.REQUIRED_WALLET
WALLET_ID = phantom_transport.REQUIRED_WALLET_ID
SIGNATURE = "1" * 64


class RecordingRunner:
    def __init__(self, identity=None, send=None, fail=None):
        self.calls = []
        self.identity = identity
        self.send = send
        self.fail = fail

    def __call__(self, argv, env, timeout):
        self.calls.append((list(argv), dict(env), timeout))
        if self.fail == argv[1:3]:
            raise subprocess.TimeoutExpired(argv, timeout)
        if argv[1:3] == ["wallet", "addresses"]:
            return self.identity
        if argv[1:3] == ["solana", "send"]:
            return self.send
        raise AssertionError(argv[1:3])


class ExecutionTransportTest(unittest.TestCase):
    def setUp(self):
        self.tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        db.init_db()
        db.set_setting("mode", "PAPER", "test")
        db.set_setting("agents_enabled", "0", "test")
        self.tx = solana_tx.encode_legacy_transfer(WALLET, WALLET, 10_000_000, BLOCK)
        self.runner = RecordingRunner()
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
        self.assertEqual(status["chat_conditional_live_approval"], "not_a_control")
        self.assertFalse(status["recorded_live_applied"])
        self.assertFalse(status["live_applicable"])
        db.execute(
            "INSERT INTO phantom_control (key, value, updated_at) VALUES (?, ?, ?)",
            (execution_transport._CONSENT, json.dumps({"applied": True, "statement": "chat"}), time.time()),
        )
        forced = execution_transport.activation_status()
        self.assertTrue(forced["recorded_live_applied"])
        self.assertFalse(forced["live_applicable"])
        self.assertFalse(forced["ready"])
        self.assertIn("fee_policy_approved", forced["unmet"])
        self.assertEqual(forced["chat_conditional_live_approval"], "not_a_control")
        self.assertEqual(len(status["swap_blockers"]), 2)
        self.assertEqual(db.get_setting("mode"), "PAPER")
        self.assertEqual(db.get_setting("agents_enabled"), "0")

    def test_stuffed_flags_are_not_an_argument_and_do_not_call_the_signer(self):
        with self.assertRaises(TypeError):
            execution_transport.submit({"live_consent": True, "slippage_verified": True, "fee_policy": "approved"})
        result = execution_transport.submit("intent-1", self.tx, runner=self.runner, rpc=self.rpc)
        self.assertEqual(self.runner.calls, [])
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
        result = execution_transport.submit("intent-2", self.tx, runner=self.runner, rpc=self.rpc)
        self.assertEqual(self.runner.calls, [])
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
        halted = execution_transport.submit("intent-3", self.tx, runner=self.runner, rpc=self.rpc)
        self.assertIn("kill_switch_open", halted["unmet"])
        self.assertEqual(self.runner.calls, [])
        phantom_runner.set_halt({"halted": False})
        foreign = solana_tx.encode_legacy_transfer(solana_tx.b58encode(b"\x22" * 32), WALLET, 10_000_000, BLOCK)
        execution_transport.remember_review("intent-4", foreign)
        self._open_reservation("intent-4")
        mismatched = execution_transport.submit("intent-4", foreign, runner=self.runner, rpc=self.rpc)
        self.assertIn("wallet_network", mismatched["unmet"])
        self.assertEqual(self.runner.calls, [])
        execution_transport._write(execution_transport._LEDGER, {"intent-3": {"sha256": "abc", "status": "submit_unknown"}})
        db.init_db()
        phantom_runner.set_halt({"halted": False})
        again = execution_transport.submit("intent-3", self.tx, runner=self.runner, rpc=self.rpc)
        self.assertIn("idempotent", again["unmet"])
        self.assertEqual(self.runner.calls, [])
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
        result = execution_transport.submit("intent-5", self.tx, runner=self.runner, rpc=self.rpc)
        self.assertIn("unknown_submit_clear", result["unmet"])
        self.assertEqual(self.runner.calls, [])

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


class CliBridgeTest(ExecutionTransportTest):
    def _identity(self, wallet=WALLET, wallet_id=WALLET_ID):
        return json.dumps({
            "walletId": wallet_id,
            "organizationId": "org",
            "addresses": [{"addressType": "solana", "address": wallet}],
        })

    def _ready(self, intent_id):
        self._open_reservation(intent_id)
        execution_transport.remember_review(intent_id, self.tx)

    def test_guard_failure_does_not_start_a_subprocess(self):
        self._ready("intent-guard")
        phantom_runner.set_halt({"halted": False})
        runner = RecordingRunner(identity=(0, self._identity()), send=(0, "{}"))
        result = execution_transport.submit("intent-guard", self.tx, runner=runner, rpc=self.rpc)
        self.assertEqual(runner.calls, [])
        self.assertEqual(result["subprocess"], [])
        self.assertIn("fee_policy_approved", result["unmet"])
        self.assertIn("aggregate_loss_policy_approved", result["unmet"])
        self.assertIn("slippage_verified", result["unmet"])
        self.assertFalse(execution_transport.activation_status()["checks"]["swap_path_verified"])

    def test_matching_identity_uses_explicit_network_before_send(self):
        self._ready("intent-cli")
        body = json.dumps({"signature": SIGNATURE, "networkId": "solana:mainnet", "account": WALLET})
        runner = RecordingRunner(identity=(0, self._identity()), send=(0, body))
        result = execution_transport.send_after_identity("intent-cli", self.tx, runner, binary="/usr/bin/phantom")
        self.assertEqual(result["subprocess"], ["addresses", "send"])
        self.assertEqual(result["signature"], SIGNATURE)
        addresses, send = runner.calls
        self.assertEqual(addresses[0], ["/usr/bin/phantom", "wallet", "addresses", "--format", "json"])
        self.assertEqual(send[0][:4], ["/usr/bin/phantom", "solana", "send", "--transaction"])
        self.assertEqual(send[0][4], self.tx)
        self.assertEqual(send[0][5:], ["--network-id", "solana:mainnet", "--wallet-id", WALLET_ID, "--confirmed", "true", "--format", "json"])
        self.assertTrue(addresses[1]["HOME"].endswith("phantom-mcp"))
        self.assertEqual(addresses[1]["HOME"], send[1]["HOME"])
        self.assertNotIn(self.tx, json.dumps(execution_transport._ledger()))
        self.assertEqual(execution_transport._reservation("intent-cli")["status"], "submit_unknown")

    def test_wallet_mismatch_and_expired_auth_stop_before_send(self):
        self._ready("intent-mismatch")
        other = solana_tx.b58encode(b"\x33" * 32)
        mismatch = RecordingRunner(identity=(0, self._identity(wallet=other)))
        refused = execution_transport.send_after_identity("intent-mismatch", self.tx, mismatch, binary="phantom")
        self.assertEqual(refused["reason"], "wallet_mismatch")
        self.assertEqual(refused["subprocess"], ["addresses"])
        self.assertEqual(execution_transport._reservation("intent-mismatch")["status"], "sim_open")
        self._ready("intent-expired")
        expired = RecordingRunner(identity=(1, "Failed to get wallet addresses: status code 401"))
        closed = execution_transport.send_after_identity("intent-expired", self.tx, expired, binary="phantom")
        self.assertEqual(closed["reason"], "auth_expired")
        self.assertEqual(closed["subprocess"], ["addresses"])
        self.assertEqual(len(expired.calls), 1)
        with self.assertRaises(ValueError):
            execution_transport.send_argv("phantom", "not a transaction")

    def test_timeout_stays_unknown_and_is_not_replayed(self):
        self._ready("intent-timeout")
        runner = RecordingRunner(identity=(0, self._identity()), fail=["solana", "send"])
        first = execution_transport.send_after_identity("intent-timeout", self.tx, runner, binary="phantom")
        self.assertEqual(first["status"], "submit_unknown")
        self.assertFalse(first["resend"])
        self.assertEqual(first["subprocess"], ["addresses", "send"])
        second = execution_transport.send_after_identity("intent-timeout", self.tx, runner, binary="phantom")
        self.assertEqual(second["subprocess"], [])
        self.assertFalse(second["resend"])
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(execution_transport._reservation("intent-timeout")["status"], "submit_unknown")

    def test_ambiguous_output_is_not_resent(self):
        self._ready("intent-ambiguous")
        runner = RecordingRunner(identity=(0, self._identity()), send=(0, json.dumps({"signature": None, "networkId": "solana:devnet", "account": WALLET})))
        result = execution_transport.send_after_identity("intent-ambiguous", self.tx, runner, binary="phantom")
        self.assertEqual(result["status"], "submit_unknown")
        self.assertNotIn("signature", result)
        again = execution_transport.send_after_identity("intent-ambiguous", self.tx, runner, binary="phantom")
        self.assertEqual(again["subprocess"], [])
        self.assertEqual(len(runner.calls), 2)

    def test_concurrent_claim_sends_once(self):
        self._ready("intent-race")
        body = json.dumps({"signature": SIGNATURE, "networkId": "solana:mainnet", "account": WALLET})
        runner = RecordingRunner(identity=(0, self._identity()), send=(0, body))
        results = []

        def work():
            results.append(execution_transport.send_after_identity("intent-race", self.tx, runner, binary="phantom"))

        threads = [threading.Thread(target=work) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        sends = [call for call in runner.calls if call[0][1:3] == ["solana", "send"]]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sum(item.get("send") is True for item in results), 1)

    def test_spawn_closes_stdin_and_does_not_use_a_shell(self):
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs

            class Done:
                returncode = 0
                stdout = b"{}"
                stderr = b""

            return Done()

        original = subprocess.run
        subprocess.run = fake_run
        try:
            code, _text = execution_transport._spawn(["phantom", "wallet", "addresses", "--format", "json"], {"HOME": self.tmp.name}, 5)
        finally:
            subprocess.run = original
        self.assertEqual(code, 0)
        self.assertIs(captured["kwargs"]["stdin"], subprocess.DEVNULL)
        self.assertIs(captured["kwargs"]["shell"], False)
        self.assertEqual(captured["argv"], ["phantom", "wallet", "addresses", "--format", "json"])
        self.assertTrue(callable(phantom_auth._binary))


def solana_inspect_rpc():
    import solana_inspect
    return solana_inspect.RpcClient(FakeRpc())
