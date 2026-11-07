"""Read-only Solana inspection against a fake RPC. Nothing is sent."""
import base64
import json
import unittest
from pathlib import Path

import phantom_transport
import solana_inspect
import solana_tx

WALLET = phantom_transport.REQUIRED_WALLET
BLOCK = solana_tx.b58encode(b"\x11" * 32)
DEST = solana_tx.b58encode(b"\x22" * 32)
TABLE = solana_tx.b58encode(b"\x33" * 32)


class FakeRpc:
    def __init__(self):
        self.methods = []
        self.block_ok = True
        self.sim_err = None
        self.tables = {}

    def __call__(self, payload):
        self.methods.append(payload["method"])
        method = payload["method"]
        if method == "getGenesisHash":
            return {"result": solana_inspect.MAINNET_GENESIS}
        if method == "isBlockhashValid":
            self.commitment = payload["params"][1]["commitment"]
            return {"result": {"context": {"slot": 10}, "value": self.block_ok}}
        if method == "simulateTransaction":
            options = payload["params"][1]
            if options.get("replaceRecentBlockhash") is not False or options.get("sigVerify") is not False:
                raise AssertionError("simulation replaced the blockhash or asked for a signature")
            return {"result": {"context": {"slot": 11}, "value": {"err": self.sim_err}}}
        if method == "getAccountInfo":
            body = self.tables[payload["params"][0]]
            encoded = base64.b64encode(body).decode("ascii")
            return {"result": {"context": {"slot": 9}, "value": {"data": [encoded, "base64"]}}}
        if method == "getSignatureStatuses":
            return {"result": {"context": {"slot": 12}, "value": [{"confirmationStatus": "confirmed", "err": None}]}}
        raise AssertionError(method)


class TestSolanaInspect(unittest.TestCase):
    def test_bounded_transfer_checks_the_blockhash_and_cannot_send(self):
        tx = solana_tx.encode_legacy_transfer(WALLET, DEST, 10_000_000, BLOCK)
        fake = FakeRpc()
        shown = solana_inspect.inspect(tx, solana_inspect.RpcClient(fake))
        self.assertEqual(shown["stage"], "chain_checked")
        self.assertTrue(shown["chain_ok"])
        self.assertEqual(shown["fee_payer"], WALLET)
        self.assertEqual(shown["blockhash"], BLOCK)
        self.assertEqual(shown["commitment"], "confirmed")
        self.assertEqual(shown["blockhash_slot"], 10)
        self.assertEqual(shown["debited_lamports"], 10_000_000)
        self.assertFalse(shown["slippage_verified"])
        self.assertIsNone(shown["provider_expiry"])
        self.assertFalse(shown["freshness_guarantee"])
        self.assertFalse(shown["execution_allowed"])
        self.assertFalse(shown["send"])
        self.assertFalse(shown["executable"])
        self.assertNotIn(tx, json.dumps(shown))
        self.assertEqual(fake.methods, ["isBlockhashValid", "simulateTransaction"])
        prepared = solana_inspect.preflight_transaction(tx, solana_inspect.RpcClient(FakeRpc()))
        self.assertTrue(prepared["genesis_ok"])
        self.assertTrue(prepared["mainnet"])
        self.assertTrue(prepared["chain_ok"])
        self.assertFalse(prepared["freshness_guarantee"])
        self.assertFalse(prepared["slippage_verified"])
        self.assertFalse(prepared["execution_allowed"])
        self.assertIsNone(prepared["provider_expiry"])
        self.assertIsNone(phantom_transport.quote({}).get("quote"))

    def test_lookup_unknown_program_stale_hash_and_status_read(self):
        table = b"\x01\x00\x00\x00" + (b"\xff" * 8) + (b"\x00" * 44) + solana_tx.b58decode(DEST)
        tx = solana_tx.encode_v0_transfer_lookup(WALLET, TABLE, 10_000_000, BLOCK)
        fake = FakeRpc()
        fake.tables[TABLE] = table
        shown = solana_inspect.inspect(tx, solana_inspect.RpcClient(fake))
        self.assertEqual(shown["stage"], "chain_checked")
        self.assertEqual(fake.methods[0], "getAccountInfo")
        raw = base64.b64decode(solana_tx.encode_legacy_transfer(WALLET, DEST, 10_000_000, BLOCK))
        program_at = 1 + 64 + 4 + 32 + 32
        patched = raw[:program_at] + (b"\x03" * 32) + raw[program_at + 32:]
        unknown = solana_inspect.inspect(base64.b64encode(patched).decode("ascii"), solana_inspect.RpcClient(FakeRpc()))
        self.assertEqual(unknown["reason"], "program is not verified")
        self.assertFalse(unknown["execution_allowed"])
        stale = FakeRpc()
        stale.block_ok = False
        stale.tables[TABLE] = table
        self.assertEqual(solana_inspect.inspect(tx, solana_inspect.RpcClient(stale))["reason"], "blockhash is not valid")
        over = solana_tx.encode_legacy_transfer(WALLET, DEST, 10_000_001, BLOCK)
        self.assertEqual(solana_inspect.inspect(over, solana_inspect.RpcClient(FakeRpc()))["reason"], "debit is not bound")
        other = solana_tx.encode_legacy_transfer(DEST, WALLET, 1, BLOCK)
        self.assertEqual(solana_inspect.inspect(other, solana_inspect.RpcClient(FakeRpc()))["reason"], "fee payer is not the allowlisted wallet")
        failed = FakeRpc()
        failed.sim_err = {"InstructionError": [0, "Custom"]}
        failed.tables[TABLE] = table
        self.assertEqual(solana_inspect.inspect(tx, solana_inspect.RpcClient(failed))["reason"], "simulation failed")
        status_rpc = FakeRpc()
        signature = solana_tx.b58encode(b"\x44" * 64)
        status = solana_inspect.signature_status(solana_inspect.RpcClient(status_rpc), signature)
        self.assertEqual(status["confirmation_status"], "confirmed")
        self.assertFalse(status["send"])
        self.assertEqual(status_rpc.methods, ["getSignatureStatuses"])
        self.assertNotIn("solana_inspect", Path("api.py").read_text())
        self.assertNotIn("solana_inspect", Path("server.py").read_text())
        wrong = FakeRpc()

        def genesis_only(payload):
            wrong.methods.append(payload["method"])
            if payload["method"] == "getGenesisHash":
                return {"result": "not-mainnet"}
            raise AssertionError(payload["method"])

        rejected = solana_inspect.preflight_transaction(tx, solana_inspect.RpcClient(genesis_only))
        self.assertEqual(rejected["error_category"], "wrong_network")
        self.assertFalse(rejected["freshness_guarantee"])
        self.assertEqual(wrong.methods, ["getGenesisHash"])
