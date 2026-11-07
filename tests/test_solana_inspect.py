"""Read-only Solana inspection against a fake RPC. Nothing is sent."""
import base64
import json
import unittest
from pathlib import Path

import phantom_transport
import solana_inspect
import solana_jupiter
import solana_tx

def _native_account() -> bytes:
    raw = bytearray(165)
    raw[0:32] = solana_tx.b58decode(solana_tx.NATIVE_MINT)
    raw[32:64] = solana_tx.b58decode(phantom_transport.REQUIRED_WALLET)
    return bytes(raw)


def _jupiter_route(quoted: int, slippage_bps: int) -> bytes:
    disc = solana_jupiter._SHARED
    plan = (1).to_bytes(4, "little") + bytes([7, 100, 0, 1])
    return (
        disc + bytes([1]) + plan
        + (10_000_000).to_bytes(8, "little")
        + quoted.to_bytes(8, "little")
        + slippage_bps.to_bytes(2, "little")
        + bytes([0])
    )


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
            body = self.tables.get(payload["params"][0])
            if body is None:
                return {"result": {"context": {"slot": 9}, "value": None}}
            encoded = base64.b64encode(body).decode("ascii")
            return {"result": {"context": {"slot": 9}, "value": {"data": [encoded, "base64"]}}}
        if method == "getSignatureStatuses":
            return {"result": {"context": {"slot": 12}, "value": [{"confirmationStatus": "confirmed", "err": None}]}}
        raise AssertionError(method)


class TestSolanaInspect(unittest.TestCase):
    def test_bounded_transfer_checks_the_blockhash_and_cannot_send(self):
        tx = solana_tx.encode_legacy_transfer(WALLET, WALLET, 10_000_000, BLOCK)
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
        self.assertFalse(shown["spend_verified"])
        self.assertFalse(shown["intent_verified"])
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
        fake.tables[DEST] = _native_account()
        shown = solana_inspect.inspect(tx, solana_inspect.RpcClient(fake))
        self.assertFalse(shown["spend_verified"])
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
        stale.tables[DEST] = _native_account()
        self.assertEqual(solana_inspect.inspect(tx, solana_inspect.RpcClient(stale))["reason"], "blockhash is not valid")
        over = solana_tx.encode_legacy_transfer(WALLET, WALLET, 10_000_001, BLOCK)
        self.assertEqual(solana_inspect.inspect(over, solana_inspect.RpcClient(FakeRpc()))["reason"], "debit is not bound")
        other = solana_tx.encode_legacy_transfer(DEST, WALLET, 1, BLOCK)
        self.assertEqual(solana_inspect.inspect(other, solana_inspect.RpcClient(FakeRpc()))["error_category"], "wallet_unbound")
        failed = FakeRpc()
        failed.sim_err = {"InstructionError": [0, "Custom"]}
        failed.tables[TABLE] = table
        failed.tables[DEST] = _native_account()
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

    def test_two_lookup_tables_follow_the_web3_writable_then_readonly_order(self):
        # Serialized by @solana/web3.js MessageV0.compile. Static payer and program,
        # then both tables' writable addresses, then both tables' readonly addresses.
        tx = "AQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACAAQABAgEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQEBBAIEAwUBCQIVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFQEAAQEWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFgEAAQE="
        expected = [
            "4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKi",
            "8qbHbw2BbbTHBW1sbeqakYXVKRQM8Ne7pLK7m6CVfeR",
            "gBxS1f6uyyGPuW5MzGBukidSb71jdsCb5fZaoSzULE5",
            "p2Yicb86aZig616Eav2VWG9vuXR5mEqhtzshZYBxzsV",
            "k7FaK87WHGVXzkaoHb7CdVPgkKDQhZ29VLDeBVbDfYn",
            "swqrv48gsrwpBFbftEwnP2vB4jckpvfGJfXkwaniLCC",
        ]
        decoded = solana_tx.decode_transaction(base64.b64decode(tx))
        self.assertEqual(decoded.version, "v0")
        self.assertEqual(len(decoded.signers), 1)
        self.assertEqual(decoded.fee_payer, expected[0])
        self.assertEqual([item.program_index for item in decoded.instructions], [1])
        self.assertEqual(decoded.instructions[0].account_indexes, [2, 4, 3, 5])
        fake = FakeRpc()
        for look, addresses in (
            (decoded.lookups[0], [bytes([10]) * 32, bytes([11]) * 32]),
            (decoded.lookups[1], [bytes([12]) * 32, bytes([13]) * 32]),
        ):
            meta = (1).to_bytes(4, "little") + (b"\xff" * 8) + (b"\x00" * 44)
            fake.tables[look.table] = meta + b"".join(addresses)
        self.assertEqual(decoded.lookups[0].writable, [0])
        self.assertEqual(decoded.lookups[0].readonly, [1])
        self.assertEqual(decoded.lookups[1].writable, [0])
        self.assertEqual(decoded.lookups[1].readonly, [1])
        resolved = solana_inspect._resolve(decoded, solana_inspect.RpcClient(fake), "confirmed")
        self.assertEqual(resolved, expected)
        self.assertEqual(decoded.instructions[0].program_id, expected[1])

    def test_compute_budget_bounds_and_aggregate_findings_skip_simulation(self):
        heap = bytes([1]) + (32 * 1024).to_bytes(4, "little")
        ok = solana_tx.encode_legacy(WALLET, [WALLET, solana_tx.COMPUTE_BUDGET], 1, [
            (1, [], heap),
            (1, [], bytes([2]) + (200_000).to_bytes(4, "little")),
        ], BLOCK)
        # No native debit, so the bound fails after the layout is accepted, and simulation is not run.
        shown = solana_inspect.inspect(ok, solana_inspect.RpcClient(FakeRpc()))
        self.assertEqual(shown["error_category"], "amount_unbound")
        self.assertEqual(shown["simulation_status"], "not_run")
        bad_heap = bytes([1]) + (40 * 1024 + 1).to_bytes(4, "little")
        bincode_heap = (1).to_bytes(4, "little") + (32 * 1024).to_bytes(4, "little")
        self.assertEqual(len(bincode_heap), 8)
        odd = bytes([1, 0, 0])
        okx = "proVF4pMXVaYqmy4NjniPh4pqKNfMmsihgd4wdkCX3u"
        combined = solana_tx.encode_legacy(WALLET, [WALLET, solana_tx.COMPUTE_BUDGET, okx], 2, [
            (1, [], bad_heap),
            (2, [0], bytes([7])),
        ], BLOCK)
        fake = FakeRpc()
        rejected = solana_inspect.inspect(combined, solana_inspect.RpcClient(fake))
        self.assertEqual(rejected["findings"][0]["error_category"], "compute_budget_out_of_bounds")
        self.assertEqual(rejected["findings"][1]["error_category"], "okx_route_unsupported")
        self.assertEqual(rejected["blockhash_status"], "not_run")
        self.assertEqual(rejected["simulation_status"], "not_run")
        self.assertNotIn("simulateTransaction", fake.methods)
        self.assertIn(okx, rejected["program_ids"])
        short = solana_tx.encode_legacy(WALLET, [WALLET, solana_tx.COMPUTE_BUDGET], 1, [
            (1, [], odd),
        ], BLOCK)
        malformed = solana_inspect.inspect(short, solana_inspect.RpcClient(FakeRpc()))
        self.assertEqual(malformed["error_category"], "compute_budget_unverified")
        self.assertIn("length 3", malformed["reason"])
        accepted = solana_tx.encode_legacy(WALLET, [WALLET, solana_tx.COMPUTE_BUDGET], 1, [
            (1, [], bincode_heap),
        ], BLOCK)
        self.assertEqual(
            solana_inspect.inspect(accepted, solana_inspect.RpcClient(FakeRpc()))["error_category"],
            "amount_unbound",
        )

    def test_token_layouts_are_narrow_and_later_programs_are_kept(self):
        token = solana_tx.TOKEN_PROGRAM
        system = solana_tx.SYSTEM_PROGRAM
        native = solana_tx.NATIVE_MINT
        account = solana_tx.b58encode(b"\x66" * 32)
        keys = [WALLET, account, token, system]
        sync_rpc = FakeRpc()
        sync_rpc.tables[account] = _native_account()
        sync = solana_tx.encode_legacy(WALLET, keys, 2, [
            (3, [0, 1], (2).to_bytes(4, "little") + (10_000_000).to_bytes(8, "little")),
            (2, [1], bytes([17])),
        ], BLOCK)
        shown = solana_inspect.inspect(sync, solana_inspect.RpcClient(sync_rpc))
        self.assertEqual(shown["stage"], "chain_checked")
        self.assertIn(token, shown["program_ids"])
        self.assertFalse(shown["slippage_verified"])
        self.assertFalse(shown["spend_verified"])
        self.assertFalse(shown["intent_verified"])
        self.assertFalse(shown["execution_allowed"])
        unread = solana_inspect.inspect(sync, solana_inspect.RpcClient(FakeRpc()))
        self.assertEqual(unread["error_category"], "account_unread")
        usdc = solana_tx.encode_legacy(WALLET, [WALLET, solana_tx.USDC_MINT, account, token], 1, [
            (3, [0, 1, 2, 0], bytes([12]) + (1).to_bytes(8, "little") + bytes([6])),
        ], BLOCK)
        self.assertEqual(solana_inspect.inspect(usdc, solana_inspect.RpcClient(FakeRpc()))["error_category"], "usdc_debit_not_buy")
        third = solana_tx.b58encode(b"\x77" * 32)
        close = solana_tx.encode_legacy(WALLET, [WALLET, account, third, token], 1, [
            (3, [1, 2, 0], bytes([9])),
        ], BLOCK)
        self.assertEqual(solana_inspect.inspect(close, solana_inspect.RpcClient(FakeRpc()))["error_category"], "close_destination_unbound")
        priced = solana_tx.encode_legacy(WALLET, [WALLET, solana_tx.COMPUTE_BUDGET], 1, [
            (1, [], bytes([3]) + (1).to_bytes(8, "little")),
            (1, [], bytes([2]) + (200_000).to_bytes(4, "little")),
        ], BLOCK)
        self.assertEqual(solana_inspect.inspect(priced, solana_inspect.RpcClient(FakeRpc()))["error_category"], "fee_unbound")
        extra = solana_tx.encode_legacy(WALLET, [WALLET, third, system], 1, [
            (2, [0, 0], (2).to_bytes(4, "little") + (1).to_bytes(8, "little")),
        ], BLOCK, required=2)
        self.assertEqual(solana_inspect.inspect(extra, solana_inspect.RpcClient(FakeRpc()))["error_category"], "extra_signer")
        unknown = solana_tx.b58encode(b"\x03" * 32)
        hidden = solana_tx.encode_legacy(WALLET, [WALLET, native, token, unknown], 2, [
            (2, [1], bytes([7]) + (1).to_bytes(8, "little")),
            (3, [0], bytes([153])),
        ], BLOCK)
        rejected = solana_inspect.inspect(hidden, solana_inspect.RpcClient(FakeRpc()))
        self.assertEqual(rejected["error_category"], "token_instruction_unverified")
        self.assertEqual(rejected["instruction_index"], 0)
        self.assertEqual(rejected["instruction_tag"], 7)
        self.assertIn(unknown, rejected["program_ids"])
        self.assertIn(token, rejected["program_ids"])
        self.assertIn("tag 7", rejected["reason"])
        plain = solana_tx.encode_legacy(WALLET, [WALLET, native, token], 1, [
            (2, [0, 1, 0], bytes([3]) + (1).to_bytes(8, "little")),
        ], BLOCK)
        unbound = solana_inspect.inspect(plain, solana_inspect.RpcClient(FakeRpc()))
        self.assertEqual(unbound["error_category"], "token_mint_unbound")
        self.assertEqual(unbound["instruction_tag"], 3)
        other_mint = solana_tx.b58encode(b"\x55" * 32)
        checked = solana_tx.encode_legacy(WALLET, [WALLET, other_mint, native, token], 1, [
            (3, [0, 1, 2, 0], bytes([12]) + (1).to_bytes(8, "little") + bytes([6])),
        ], BLOCK)
        bad_mint = solana_inspect.inspect(checked, solana_inspect.RpcClient(FakeRpc()))
        self.assertEqual(bad_mint["error_category"], "token_mint_unbound")
        self.assertEqual(bad_mint["instruction_tag"], 12)
        self.assertFalse(bad_mint["execution_allowed"])
        other_mint = solana_tx.b58encode(b"\x55" * 32)
        ata = solana_tx.ASSOCIATED_TOKEN
        jupiter = solana_jupiter.PROGRAM_ID
        created = solana_tx.encode_legacy(WALLET, [WALLET, DEST, other_mint, ata, jupiter], 3, [
            (3, [0, 1, 0, 2], bytes([1])),
            (4, [0], b"\x00" * 8),
        ], BLOCK)
        mismatch = solana_inspect.inspect(created, solana_inspect.RpcClient(FakeRpc()))
        self.assertEqual(mismatch["error_category"], "token_mint_unbound")
        self.assertEqual(mismatch["instruction_index"], 0)
        self.assertEqual(mismatch["instruction_tag"], 1)
        self.assertEqual(mismatch["observed_mint"], other_mint)
        self.assertEqual(mismatch["observed_owner"], WALLET)
        self.assertEqual(set(mismatch["expected_mints"]), {solana_tx.NATIVE_MINT, solana_tx.USDC_MINT})
        self.assertIn(jupiter, mismatch["program_ids"])
        self.assertFalse(mismatch["slippage_verified"])
        route = _jupiter_route(898162, 100)
        keys = [
            WALLET, solana_tx.b58encode(b"\x44" * 32), DEST, solana_tx.b58encode(b"\x46" * 32),
            solana_tx.b58encode(b"\x47" * 32), solana_tx.b58encode(b"\x48" * 32),
            solana_tx.NATIVE_MINT, solana_tx.USDC_MINT, solana_tx.TOKEN_PROGRAM,
            solana_tx.SYSTEM_PROGRAM, jupiter,
        ]
        swap = solana_tx.encode_legacy(WALLET, keys, 5, [
            (9, [0, 0], (2).to_bytes(4, "little") + (10_000_000).to_bytes(8, "little")),
            (10, [8, 1, 0, 2, 3, 4, 5, 6, 7], route),
        ], BLOCK)
        checked = solana_inspect.inspect(swap, solana_inspect.RpcClient(FakeRpc()))
        self.assertTrue(checked["chain_ok"])
        self.assertTrue(checked["slippage_field_ok"])
        self.assertFalse(checked["slippage_verified"])
        self.assertEqual(checked["quoted_out_amount"], "898162")
        self.assertEqual(checked["slippage_bps"], 100)
        self.assertFalse(checked["spend_verified"])
        self.assertFalse(checked["intent_verified"])
        self.assertFalse(checked["execution_allowed"])
        bad_route = bytes(route)
        bad_route = route[:8] + bytes([1]) + (1).to_bytes(4, "little") + bytes([250, 100, 0, 1]) + route[8 + 1 + 4 + 4:]
        unsupported = solana_tx.encode_legacy(WALLET, keys, 5, [
            (9, [0, 0], (2).to_bytes(4, "little") + (10_000_000).to_bytes(8, "little")),
            (10, [8, 1, 0, 2, 3, 4, 5, 6, 7], bad_route),
        ], BLOCK)
        self.assertEqual(
            solana_inspect.inspect(unsupported, solana_inspect.RpcClient(FakeRpc()))["error_category"],
            "unsupported_route",
        )
