"""Guards around the read-only RPC adapter. No socket and no CLI."""
import base64
import hashlib
import threading
import time
import unittest

import db
import execution_transport
import phantom_runner
import phantom_transport
import solana_direct_swap
import solana_read_rpc
import solana_tx
import spend_policy
from tests.support import temp_desk_env
from tests.whirlpool_fixture import FakeTransport, reader


class ReadRpcTests(unittest.TestCase):
    def setUp(self):
        _tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        db.init_db()
        db.set_setting("mode", "PAPER", "test")
        db.set_setting("agents_enabled", "0", "test")
        solana_read_rpc.TRANSPORT = None
        self.addCleanup(lambda: setattr(solana_read_rpc, "TRANSPORT", None))

    def test_send_is_refused_before_the_transport_and_redirects_stop(self):
        called = []
        rpc = solana_read_rpc.ReadRpc(lambda _payload: called.append(True))
        with self.assertRaises(solana_read_rpc.RpcError) as refused:
            rpc.call("sendTransaction", ["unsigned"])
        self.assertEqual(refused.exception.reason, "method_refused")
        self.assertEqual(called, [])
        self.assertEqual(rpc.methods, [])

        import httpx

        class Redirected:
            status_code = 302
            is_redirect = True
            content = b""

            def json(self):
                return {"result": "no"}

        original = httpx.post
        httpx.post = lambda *args, **kwargs: Redirected()
        try:
            with self.assertRaises(solana_read_rpc.RpcError) as redirected:
                solana_read_rpc.http_transport({"jsonrpc": "2.0", "id": 1, "method": "getGenesisHash", "params": []})
        finally:
            httpx.post = original
        self.assertEqual(redirected.exception.reason, "redirect")
        self.assertFalse(solana_read_rpc.reads_enabled())
        solana_read_rpc.enable_reads()
        db.set_setting("rpc_url", "https://other.example", "test")
        self.assertIsNone(solana_read_rpc.configured_reader())

    def test_slot_is_not_a_ttl_and_a_failed_gate_does_not_simulate(self):
        now = time.time()
        self.assertFalse(solana_direct_swap.quote_is_fresh(now - 120, now, 466547240))
        self.assertTrue(solana_direct_swap.quote_is_fresh(now, now, 1))
        self.assertTrue(solana_direct_swap.quote_is_fresh(now, now, 999999999))
        spend_policy.persist_owner_approval()
        phantom_runner.set_halt({"halted": False})
        expensive = reader(fee=5_000_000)
        blocked = execution_transport.reserve_from_reader("fee-cap", expensive)
        self.assertFalse(blocked["ok"])
        self.assertNotIn("simulateTransaction", expensive.methods)
        expired = reader(height=9000, last_valid=1000)
        closed = execution_transport.reserve_from_reader("expired", expired)
        self.assertEqual(closed["reason"], "blockhash_expired")
        self.assertNotIn("simulateTransaction", expired.methods)
        dropped = reader(drop=solana_direct_swap.SOL_USDC_SNAPSHOT["tick_array_addresses"][0])
        missing = solana_read_rpc.load_bundle(dropped)
        self.assertEqual(missing["reason"], "tick_array_missing")
        self.assertEqual(dropped.methods, ["getGenesisHash", "getMultipleAccounts"])

    def test_restart_keeps_the_hold_and_two_reserves_create_once(self):
        spend_policy.persist_owner_approval()
        phantom_runner.set_halt({"halted": False})
        first = reader()
        saved = execution_transport.reserve_from_reader("stay", first)
        self.assertTrue(saved["created"])
        self.assertEqual(first.methods.count("simulateTransaction"), 1)
        self.assertEqual(first.methods.count("getMultipleAccounts"), 1)
        held = saved["reserved_usd"]
        db.init_db()
        row = db.query("SELECT status, reserved_usd FROM phantom_reservations WHERE intent_id = 'stay'")[0]
        self.assertEqual(row["status"], "sim_open")
        self.assertEqual(row["reserved_usd"], held)
        results = []

        def work(name):
            results.append(execution_transport.reserve_from_reader("race", reader()))

        threads = [threading.Thread(target=work, args=(str(i),)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        created = [item for item in results if item.get("created")]
        self.assertEqual(len(created), 1)
        self.assertEqual(sum(item.get("simulation", {}).get("simulated") is True for item in results), 1)
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM phantom_reservations WHERE intent_id = 'race'")[0]["n"], 1)

    def test_a_finalized_flag_does_not_bind_a_mismatched_fill(self):
        prepared, entry, _exit, _rpc = execution_transport._assemble_read(466547240, reader())
        raw = base64.b64decode(entry)
        signed = bytes([raw[0]]) + (b"\x11" * 64) + raw[1 + 64:]
        encoded = base64.b64encode(signed).decode("ascii")
        signature = solana_tx.first_signature(encoded)
        digest = hashlib.sha256(solana_tx.message_bytes(encoded)).hexdigest()
        acquired = prepared["quote_token_est_out"]
        pre = 188718141
        status = {"value": [{"slot": prepared["context_slot"], "err": None, "confirmationStatus": "finalized"}]}
        def transaction(fee, post):
            return {
                "slot": prepared["context_slot"],
                "meta": {
                    "err": None,
                    "fee": fee,
                    "preTokenBalances": [{"mint": solana_tx.USDC_MINT, "owner": phantom_transport.REQUIRED_WALLET, "uiTokenAmount": {"amount": str(pre)}}],
                    "postTokenBalances": [{"mint": solana_tx.USDC_MINT, "owner": phantom_transport.REQUIRED_WALLET, "uiTokenAmount": {"amount": str(post)}}],
                },
                "transaction": [encoded, "base64"],
            }
        bound = solana_read_rpc._bind_receipt(signature, status, transaction(6000, pre + acquired))
        self.assertEqual(bound["acquired_raw"], acquired)
        self.assertNotEqual(bound["acquired_raw"], pre + acquired)
        self.assertEqual(bound["message_sha256"], digest)
        self.assertEqual(bound["network_fee_lamports"], 6000)
        self.assertEqual(bound["confirmation_status"], "finalized")
        wrong = solana_read_rpc._bind_receipt(signature, status, transaction(1, pre + acquired))
        self.assertEqual(wrong["network_fee_lamports"], 1)
        self.assertNotEqual(wrong["network_fee_lamports"], prepared["network_fee_lamports"])
