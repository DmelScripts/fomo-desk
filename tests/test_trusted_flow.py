"""Configured valuation lets the trusted path reach each later guard. No real signer."""
import base64
import hashlib
import json
import time
import unittest

import db
import execution_transport
import phantom_auth
import phantom_runner
import phantom_transport
import solana_direct_swap
import solana_read_rpc
import solana_tx
import spend_policy
import usdc_valuation
from tests.support import temp_desk_env
from tests.whirlpool_fixture import FakeTransport, reader

WALLET = phantom_transport.REQUIRED_WALLET
WALLET_ID = phantom_transport.REQUIRED_WALLET_ID
SIGNATURE = "1" * 64


def _identity():
    return json.dumps({
        "walletId": WALLET_ID,
        "organizationId": "org",
        "addresses": [{"addressType": "solana", "address": WALLET}],
    })


class TrustedFlowTests(unittest.TestCase):
    def setUp(self):
        _tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        db.init_db()
        db.set_setting("mode", "PAPER", "test")
        db.set_setting("agents_enabled", "0", "test")
        phantom_auth.reset_for_tests()
        self.addCleanup(phantom_auth.reset_for_tests)
        usdc_valuation.set_quote(None)
        self.addCleanup(lambda: usdc_valuation.set_quote(None))
        solana_read_rpc.TRANSPORT = None
        self.addCleanup(lambda: setattr(solana_read_rpc, "TRANSPORT", None))

    def test_fixture_valuation_reaches_identity_hold_receipt_and_exit(self):
        spend_policy.persist_owner_approval()
        phantom_runner.set_halt({"halted": False})
        usdc_valuation.enable()
        usdc_valuation.set_quote({
            "price": 99_990_000, "conf": 10_000, "expo": -8,
            "publish_time": int(time.time()), "feed_id": usdc_valuation.FEED_ID,
        })
        self.assertTrue(spend_policy.usd_source()["approved"])
        self.assertIsNone(spend_policy.usd_source()["accepted_band"])
        built, entry, _exit, _rpc = execution_transport._assemble_read(466547240, reader())
        self.assertTrue(built["ok"])
        self.assertTrue(built["fee_cap_ok"])
        self.assertIsNone(built["exit_tx_sha256"])
        saved = execution_transport.reserve_from_reader("flow", reader())
        self.assertTrue(saved["created"], saved)
        self.assertEqual(saved["simulation"]["err"], None)
        self.assertFalse(saved["live_ready"])
        row = db.query("SELECT status, reserved_usd, result_json FROM phantom_reservations WHERE intent_id = 'flow'")[0]
        stored = json.loads(row["result_json"])
        self.assertEqual(stored["message_sha256"], built["message_sha256"])
        self.assertEqual(row["status"], "sim_open")
        execution_transport.remember_review("flow", entry)
        calls = []

        def runner(argv, _env, _timeout):
            calls.append(list(argv))
            if argv[1:3] == ["wallet", "addresses"]:
                return 0, _identity()
            body = json.dumps({"signature": SIGNATURE, "networkId": "solana:mainnet", "account": WALLET})
            return 0, body

        sent = execution_transport.send_after_identity("flow", entry, runner, binary="phantom")
        self.assertEqual(sent["subprocess"], ["addresses", "send"])
        self.assertEqual(sent["cli_transport"], "argv_only")
        self.assertFalse(sent["stdin_transaction"])
        self.assertEqual(calls[1][1:4], ["solana", "send", "--transaction"])
        self.assertEqual(calls[1][4], entry)
        self.assertNotIn(entry, json.dumps(execution_transport._ledger()))
        held = db.query("SELECT status, reserved_usd, result_json FROM phantom_reservations WHERE intent_id = 'flow'")[0]
        self.assertEqual(held["status"], "submit_unknown")
        self.assertEqual(held["reserved_usd"], row["reserved_usd"])
        again = execution_transport.send_after_identity("flow", entry, runner, binary="phantom")
        self.assertEqual(again["subprocess"], [])
        self.assertEqual(len(calls), 2)
        raw = base64.b64decode(entry)
        signed = bytes([raw[0]]) + (b"\x11" * 64) + raw[1 + 64:]
        encoded = base64.b64encode(signed).decode("ascii")
        decoded = solana_tx.decode_transaction(signed)
        ata = solana_tx.associated_token_address(WALLET, solana_tx.USDC_MINT, solana_tx.TOKEN_PROGRAM)
        index = decoded.static_keys.index(ata)
        pre = stored["wallet_usdc_raw"]
        delta = stored["entry_token_min_out"] + 100
        self.assertLess(delta, stored["entry_token_est_out"])
        transport = FakeTransport()
        transport.statuses = {"context": {"slot": stored["context_slot"]}, "value": [{
            "slot": stored["context_slot"], "err": None, "confirmationStatus": "finalized",
        }]}
        transport.transaction = {
            "slot": stored["context_slot"],
            "meta": {
                "err": None,
                "fee": stored["network_fee_lamports"],
                "preTokenBalances": [{"accountIndex": index, "mint": solana_tx.USDC_MINT, "owner": WALLET, "uiTokenAmount": {"amount": str(pre)}}],
                "postTokenBalances": [{"accountIndex": index, "mint": solana_tx.USDC_MINT, "owner": WALLET, "uiTokenAmount": {"amount": str(pre + delta)}}],
            },
            "transaction": [encoded, "base64"],
        }
        solana_read_rpc.enable_reads()
        solana_read_rpc.TRANSPORT = transport
        confirmed = phantom_runner.apply_chain_proof("flow", solana_tx.first_signature(encoded), 20)
        self.assertTrue(confirmed.get("confirmed"), confirmed)
        self.assertEqual(confirmed["acquired_raw"], delta)
        self.assertNotEqual(confirmed["acquired_raw"], stored["entry_token_est_out"])
        self.assertEqual(db.query("SELECT reserved_usd FROM phantom_reservations WHERE intent_id = 'flow'")[0]["reserved_usd"], held["reserved_usd"])
        planned = execution_transport.plan_acquired_exit("flow", reader())
        self.assertTrue(planned["ok"], planned)
        self.assertEqual(planned["exit_raw"], delta)
        self.assertFalse(planned["send"])
        self.assertIsNone(planned["realized_pnl"])
        self.assertEqual(db.query("SELECT status, reserved_usd, realized_pnl FROM phantom_reservations WHERE intent_id = 'flow'")[0]["realized_pnl"], None)
        after = json.loads(db.query("SELECT result_json FROM phantom_reservations WHERE intent_id = 'flow'")[0]["result_json"])
        bundle = solana_read_rpc.load_bundle(reader())
        exit_tx, _quote = solana_direct_swap.build_exit(
            bundle["pool"], bundle["pool_data"], bundle["accounts"], solana_direct_swap.SOL_USDC_SNAPSHOT["specified_slippage_bps"],
            bundle["blockhash"], bundle["context_slot"], delta, after["wallet_usdc_raw"],
            minimum=after["entry_token_min_out"], estimated=after["entry_token_est_out"], ata_balance=after["ata_balance"],
        )
        self.assertEqual(hashlib.sha256(solana_tx.message_bytes(exit_tx)).hexdigest(), after["exit_message_sha256"])
        raw_exit = base64.b64decode(exit_tx)
        signed_exit = bytes([raw_exit[0]]) + (b"\x22" * 64) + raw_exit[1 + 64:]
        encoded_exit = base64.b64encode(signed_exit).decode("ascii")
        exit_decoded = solana_tx.decode_transaction(signed_exit)
        exit_index = exit_decoded.static_keys.index(ata)
        exit_pre = pre + delta
        missing_pre = FakeTransport()
        missing_pre.statuses = {"context": {"slot": bundle["context_slot"]}, "value": [{
            "slot": bundle["context_slot"], "err": None, "confirmationStatus": "finalized",
        }]}
        missing_pre.transaction = {
            "slot": bundle["context_slot"],
            "meta": {
                "err": None, "fee": stored["network_fee_lamports"],
                "preTokenBalances": [],
                "postTokenBalances": [{"accountIndex": exit_index, "mint": solana_tx.USDC_MINT, "owner": WALLET, "uiTokenAmount": {"amount": str(pre)}}],
            },
            "transaction": [encoded_exit, "base64"],
        }
        solana_read_rpc.TRANSPORT = missing_pre
        unbooked = execution_transport.book_exit_close("flow", solana_tx.first_signature(encoded_exit), 21)
        self.assertEqual(unbooked["reason"], "pre_missing")
        self.assertIsNone(db.query("SELECT realized_pnl FROM phantom_reservations WHERE intent_id = 'flow'")[0]["realized_pnl"])
        close = FakeTransport()
        close.statuses = missing_pre.statuses
        close.transaction = {
            "slot": bundle["context_slot"],
            "meta": {
                "err": None, "fee": stored["network_fee_lamports"],
                "preTokenBalances": [{"accountIndex": exit_index, "mint": solana_tx.USDC_MINT, "owner": WALLET, "uiTokenAmount": {"amount": str(exit_pre)}}],
                "postTokenBalances": [{"accountIndex": exit_index, "mint": solana_tx.USDC_MINT, "owner": WALLET, "uiTokenAmount": {"amount": str(pre)}}],
            },
            "transaction": [encoded_exit, "base64"],
        }
        solana_read_rpc.TRANSPORT = close
        booked = execution_transport.book_exit_close("flow", solana_tx.first_signature(encoded_exit), 21)
        self.assertTrue(booked["booked"], booked)
        self.assertEqual(booked["reserved_usd"], "0.00")
        self.assertTrue(booked["realized_pnl"].startswith("-"))
        closed = db.query("SELECT status, reserved_usd, realized_pnl FROM phantom_reservations WHERE intent_id = 'flow'")[0]
        self.assertEqual(closed["status"], "sim_closed")
        self.assertEqual(closed["realized_pnl"], booked["realized_pnl"])
        again_booked = execution_transport.book_exit_close("flow", solana_tx.first_signature(encoded_exit), 21)
        self.assertFalse(again_booked["booked"])
        self.assertEqual(db.query("SELECT realized_pnl FROM phantom_reservations WHERE intent_id = 'flow'")[0]["realized_pnl"], booked["realized_pnl"])
        conn = db.connect()
        try:
            self.assertGreater(phantom_runner.cumulative_realized_loss(conn), 0)
        finally:
            conn.close()
        self.assertFalse(execution_transport.activation_status()["checks"]["signer_enabled"])
        self.assertEqual(set(execution_transport.activation_status()["pipeline_gaps"]), {"scanner", "auditor", "analyst", "executor", "risk"})
