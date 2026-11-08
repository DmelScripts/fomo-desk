"""Halt and policy are claimed after identity, before send. Fake runner only."""
import json
import subprocess
import threading
import time
import unittest

import db
import execution_transport
import exit_policy
import phantom_runner
import phantom_transport
import solana_read_rpc
import spend_policy
import usdc_valuation
from tests.support import temp_desk_env
from tests.whirlpool_fixture import FakeTransport, reader

WALLET = phantom_transport.REQUIRED_WALLET
WALLET_ID = phantom_transport.REQUIRED_WALLET_ID


def _harness():
    return {
        "price": 99_990_000, "conf": 10_000, "expo": -8,
        "publish_time": int(time.time()), "feed_id": usdc_valuation.FEED_ID,
    }


def _identity():
    return json.dumps({
        "walletId": WALLET_ID,
        "organizationId": "org",
        "addresses": [{"addressType": "solana", "address": WALLET}],
    })


class SendWindowTests(unittest.TestCase):
    def setUp(self):
        _tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        db.init_db()
        db.set_setting("mode", "PAPER", "test")
        db.set_setting("agents_enabled", "0", "test")
        spend_policy.persist_owner_approval()
        exit_policy.set_approval(True)
        phantom_runner.set_halt({"halted": False})
        usdc_valuation.enable()
        usdc_valuation.install_harness(_harness())
        self.addCleanup(lambda: usdc_valuation.install_harness(None))
        solana_read_rpc.enable_reads()
        self.transport = FakeTransport()
        solana_read_rpc.TRANSPORT = self.transport
        self.addCleanup(lambda: setattr(solana_read_rpc, "TRANSPORT", None))

    def test_halt_flip_during_addresses_does_not_send(self):
        tx = self._ready("halt-flip")

        def runner(argv, _env, _timeout):
            self.assertEqual(argv[1:3], ["wallet", "addresses"])
            phantom_runner.set_halt({"halted": True})
            return 0, _identity()

        refused = execution_transport.send_after_identity("halt-flip", tx, runner, binary="phantom")
        self.assertEqual(refused["subprocess"], ["addresses"])
        self.assertEqual(refused["reason"], "kill_switch")
        self.assertFalse(refused["send"])
        self.assertFalse(refused["post_claim_toctou"])
        self.assertEqual(db.query("SELECT status FROM phantom_reservations WHERE intent_id = 'halt-flip'")[0]["status"], "sim_open")
        phantom_runner.set_halt({"halted": False})
        stale = execution_transport.send_after_identity("halt-flip", tx, self._ok, binary="phantom")
        self.assertEqual(stale["subprocess"], ["addresses"])
        self.assertEqual(stale["reason"], "policy_changed")
        self.assertFalse(stale["send"])

    def test_policy_version_changes_during_addresses_do_not_send(self):
        self._flip("fee-rewrite", lambda: spend_policy.persist_owner_approval())
        self._flip("fee-revoke", lambda: spend_policy.clear_owner_approval())
        self._flip("exit-revoke", lambda: exit_policy.set_approval(False))

    def test_unchanged_controls_send_once(self):
        tx = self._ready("steady")
        sent = execution_transport.send_after_identity("steady", tx, self._ok, binary="phantom")
        self.assertEqual(sent["subprocess"], ["addresses", "send"])
        self.assertTrue(sent["send"])
        self.assertTrue(sent["post_claim_toctou"])
        again = execution_transport.send_after_identity("steady", tx, self._ok, binary="phantom")
        self.assertEqual(again["subprocess"], [])
        self.assertFalse(again["send"])

    def test_an_unrelated_control_write_still_sends_once(self):
        tx = self._ready("unrelated")

        def runner(argv, _env, _timeout):
            if argv[1:3] == ["wallet", "addresses"]:
                db.execute(
                    "INSERT INTO phantom_control (key, value, updated_at) VALUES ('unrelated_generation', '1', ?)",
                    (time.time(),),
                )
                return 0, _identity()
            return 0, json.dumps({"signature": "3" * 64, "networkId": "solana:mainnet", "account": WALLET})

        sent = execution_transport.send_after_identity("unrelated", tx, runner, binary="phantom")
        self.assertEqual(sent["subprocess"], ["addresses", "send"])
        self.assertTrue(sent["post_claim_toctou"])
        second = execution_transport.send_after_identity("unrelated", tx, runner, binary="phantom")
        self.assertEqual(second["subprocess"], [])
        self.assertEqual(db.query("SELECT status FROM phantom_reservations WHERE intent_id = 'unrelated'")[0]["status"], "submit_unknown")

    def test_two_claims_send_once(self):
        tx = self._ready("race")
        body = json.dumps({"signature": "4" * 64, "networkId": "solana:mainnet", "account": WALLET})
        calls = []
        lock = threading.Lock()

        def runner(argv, _env, _timeout):
            with lock:
                calls.append(argv[1:3])
            if argv[1:3] == ["wallet", "addresses"]:
                return 0, _identity()
            return 0, body

        results = []

        def work():
            results.append(execution_transport.send_after_identity("race", tx, runner, binary="phantom"))

        threads = [threading.Thread(target=work) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        sends = [call for call in calls if call == ["solana", "send"]]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sum(item.get("send") is True for item in results), 1)
        self.assertEqual(db.query("SELECT status FROM phantom_reservations WHERE intent_id = 'race'")[0]["status"], "submit_unknown")

    def test_a_timed_out_send_is_not_replayed_after_restart(self):
        tx = self._ready("timeout")

        def runner(argv, _env, _timeout):
            if argv[1:3] == ["wallet", "addresses"]:
                return 0, _identity()
            raise subprocess.TimeoutExpired(argv, _timeout)

        first = execution_transport.send_after_identity("timeout", tx, runner, binary="phantom")
        self.assertEqual(first["status"], "submit_unknown")
        self.assertEqual(first["subprocess"], ["addresses", "send"])
        self.assertFalse(first["resend"])
        db.init_db()
        second = execution_transport.send_after_identity("timeout", tx, runner, binary="phantom")
        self.assertEqual(second["subprocess"], [])
        self.assertFalse(second["send"])
        self.assertEqual(db.query("SELECT status FROM phantom_reservations WHERE intent_id = 'timeout'")[0]["status"], "submit_unknown")

    def test_blockhash_expiry_after_identity_does_not_send(self):
        tx = self._ready("expired")

        def runner(argv, _env, _timeout):
            self.transport.height = 6000
            return 0, _identity()

        refused = execution_transport.send_after_identity("expired", tx, runner, binary="phantom")
        self.assertEqual(refused["subprocess"], ["addresses"])
        self.assertEqual(refused["reason"], "blockhash_expired")
        self.assertEqual(db.query("SELECT status FROM phantom_reservations WHERE intent_id = 'expired'")[0]["status"], "sim_open")

    def _flip(self, intent, change):
        tx = self._ready(intent)

        def runner(argv, _env, _timeout):
            change()
            return 0, _identity()

        refused = execution_transport.send_after_identity(intent, tx, runner, binary="phantom")
        self.assertEqual(refused["subprocess"], ["addresses"], intent)
        self.assertEqual(refused["reason"], "policy_changed", intent)
        self.assertFalse(refused["send"])
        self.assertEqual(db.query("SELECT status FROM phantom_reservations WHERE intent_id = ?", (intent,))[0]["status"], "sim_open")

    def _ok(self, argv, _env, _timeout):
        if argv[1:3] == ["wallet", "addresses"]:
            return 0, _identity()
        return 0, json.dumps({"signature": "2" * 64, "networkId": "solana:mainnet", "account": WALLET})

    def _ready(self, intent):
        spend_policy.persist_owner_approval()
        exit_policy.set_approval(True)
        phantom_runner.set_halt({"halted": False})
        saved = execution_transport.reserve_from_reader(intent, reader(), keep_tx=True)
        self.assertTrue(saved.get("created"), saved)
        tx = saved.pop("_server_tx")
        execution_transport.remember_review(intent, tx)
        return tx
