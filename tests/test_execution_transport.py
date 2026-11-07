"""The signer reads desk state. A caller-supplied all-clear is not accepted."""
import json
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

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
        phantom_runner.set_halt({"halted": False})
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
        body = json.dumps({"signature": SIGNATURE, "networkId": execution_transport.CLI_MAINNET_NETWORK, "account": WALLET})
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
        body = json.dumps({"signature": SIGNATURE, "networkId": execution_transport.CLI_MAINNET_NETWORK, "account": WALLET})
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

    def test_production_helper_cannot_bypass_closed_signer_gate(self):
        self._ready("intent-direct-production")
        with patch.object(execution_transport, "_spawn") as spawn:
            result = execution_transport.send_after_identity(
                "intent-direct-production", self.tx, execution_transport._production_runner
            )
            self.assertEqual(result["reason"], "signer_disabled")
            self.assertEqual(result["subprocess"], [])
            with self.assertRaises(PermissionError):
                execution_transport._production_runner(["phantom"], {}, 1)
            spawn.assert_not_called()
        decision = execution_transport.submit("intent-direct-production", self.tx, runner=self.runner)
        self.assertIn("signer_enabled", decision["unmet"])
        self.assertEqual(self.runner.calls, [])

    def test_expired_future_and_malformed_reviews_make_zero_calls(self):
        values = [time.time() - execution_transport.REVIEW_MAX_AGE_S - 1,
                  time.time() + 60, None, True, "now", float("nan")]
        for index, reviewed_at in enumerate(values):
            with self.subTest(reviewed_at=reviewed_at):
                intent = "intent-expired-review-" + str(index)
                self._ready(intent)
                review = execution_transport._read(execution_transport._REVIEW + intent)
                review["reviewed_at"] = reviewed_at
                execution_transport._write(execution_transport._REVIEW + intent, review)
                runner = RecordingRunner(identity=(0, self._identity()))
                result = execution_transport.send_after_identity(intent, self.tx, runner)
                self.assertEqual(result["subprocess"], [])
                self.assertEqual(runner.calls, [])

    def test_review_or_controls_changed_during_identity_prevent_send(self):
        def replace_review(intent):
            review = execution_transport._read(execution_transport._REVIEW + intent)
            review["sha256"] = "changed"
            execution_transport._write(execution_transport._REVIEW + intent, review)

        def change_reservation(intent):
            db.execute("UPDATE phantom_reservations SET slippage = 99 WHERE intent_id = ?", (intent,))

        mutations = [replace_review, change_reservation,
                     lambda _intent: phantom_runner.set_halt({"halted": True}),
                     lambda _intent: db.set_setting("mode", "LIVE", "test")]
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                db.set_setting("mode", "PAPER", "test")
                intent = "intent-changed-during-identity-" + str(index)
                self._ready(intent)
                calls = []

                def runner(argv, _env, _timeout):
                    calls.append(argv)
                    self.assertEqual(argv[1:3], ["wallet", "addresses"])
                    mutate(intent)
                    return 0, self._identity()

                result = execution_transport.send_after_identity(intent, self.tx, runner)
                self.assertEqual(result["reason"], "reservation_lost")
                self.assertEqual(result["subprocess"], ["addresses"])
                self.assertEqual(len(calls), 1)
                self.assertEqual(execution_transport._reservation(intent)["status"], "sim_open")
                self.assertNotIn(intent, execution_transport._ledger())

    def test_review_expiring_during_identity_prevents_send(self):
        intent = "intent-expires-during-identity"
        self._ready(intent)
        reviewed_at = execution_transport._read(execution_transport._REVIEW + intent)["reviewed_at"]
        with patch.object(execution_transport.time, "time", side_effect=[
            reviewed_at + 1, reviewed_at + execution_transport.REVIEW_MAX_AGE_S + 1
        ]):
            runner = RecordingRunner(identity=(0, self._identity()))
            result = execution_transport.send_after_identity(intent, self.tx, runner)
        self.assertEqual(result["reason"], "reservation_lost")
        self.assertEqual(result["subprocess"], ["addresses"])
        self.assertEqual(len(runner.calls), 1)

    def test_invalid_reservation_money_and_chain_refuse_before_identity(self):
        cases = [("reserved_usd", "-1.00"), ("reserved_usd", "NaN"),
                 ("reserved_usd", "Infinity"), ("reserved_usd", "7.00"),
                 ("reserved_usd", "101.00"), ("fee_usd", "-0.20"),
                 ("chain", "solana:devnet")]
        for index, (column, value) in enumerate(cases):
            with self.subTest(column=column, value=value):
                intent = "intent-invalid-reservation-" + str(index)
                self._ready(intent)
                db.execute("UPDATE phantom_reservations SET " + column + " = ? WHERE intent_id = ?", (value, intent))
                execution_transport.remember_review(intent, self.tx)
                runner = RecordingRunner(identity=(0, self._identity()))
                result = execution_transport.send_after_identity(intent, self.tx, runner)
                self.assertEqual(result["subprocess"], [])
                self.assertEqual(runner.calls, [])
                self.assertIn("reservation_open", execution_transport.decide(intent, self.tx)["unmet"])

    def test_submit_passes_resolved_binary_to_production_bridge(self):
        # Exercise only dispatch, with both the decision and bridge substituted.
        # The actual production runner remains disabled and is never invoked.
        with patch.object(execution_transport, "decide", return_value={"unmet": []}), \
             patch.object(phantom_auth, "_binary", return_value="/pinned/bin/phantom"), \
             patch.object(execution_transport, "send_after_identity", return_value={"subprocess": []}) as bridge, \
             patch.object(execution_transport, "_spawn") as spawn:
            execution_transport.submit("intent-path", self.tx)
            bridge.assert_called_once_with(
                "intent-path", self.tx, execution_transport._production_runner,
                binary="/pinned/bin/phantom"
            )
            spawn.assert_not_called()

    def test_pinned_cli_canonical_network_output_and_other_networks(self):
        # Actual 3.0.0 output uses normalizeNetworkId(), not the input alias.
        output = {"signature": SIGNATURE,
                  "networkId": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
                  "account": WALLET}
        self.assertEqual(execution_transport._signature(json.dumps(output)), SIGNATURE)
        for network in ["solana:mainnet", "solana:101", "solana:devnet",
                        "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1", "eip155:1"]:
            with self.subTest(network=network):
                self.assertIsNone(execution_transport._signature(json.dumps({**output, "networkId": network})))
        self.assertIsNone(execution_transport._signature(json.dumps({**output, "account": "wrong-wallet"})))

    def test_oversized_base58_signature_is_not_treated_as_submitted(self):
        self._ready("intent-invalid-signature")
        body = json.dumps({"signature": "z" * 88, "networkId": execution_transport.CLI_MAINNET_NETWORK, "account": WALLET})
        runner = RecordingRunner(identity=(0, self._identity()), send=(0, body))
        result = execution_transport.send_after_identity("intent-invalid-signature", self.tx, runner)
        self.assertEqual(result["status"], "submit_unknown")
        self.assertNotIn("signature", result)

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
