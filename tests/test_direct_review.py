"""Fresh sample review against fake RPC only. No wallet, network or signing."""
import base64
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import unittest
from unittest import mock

import phantom_transport
import solana_direct_review as review
import solana_direct_swap as direct
import solana_inspect
import solana_tx
import solana_whirlpool as whirlpool
import solana_whirlpool_quote as quote

BLOCK = solana_tx.b58encode(b"\x11" * 32)


def _account(owner, data):
    return {"owner": owner, "data": [base64.b64encode(data).decode(), "base64"], "executable": False}


class FakeRpc:
    def __init__(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/corrected_whirlpool_snapshot.json").read_text())
        self.slot = fixture["source"]["context_slot"]
        self.records = {item["address"]: _account(item["owner"], base64.b64decode(item["data_base64"])) for item in fixture["accounts"]}
        self.pool = self.records[direct.SOL_USDC_POOL]
        self.calls = []
        self.overrides = {}
        self.moves = 0
        data = base64.b64decode(self.pool["data"][0])
        mint_a, vault_a, mint_b, vault_b = whirlpool.pool_fields(data)
        self.records[vault_a] = _account(solana_tx.TOKEN_PROGRAM, whirlpool.reference_token_account(mint_a, direct.SOL_USDC_POOL))
        self.records[vault_b] = _account(solana_tx.TOKEN_PROGRAM, whirlpool.reference_token_account(mint_b, direct.SOL_USDC_POOL))
        self.wallet_accounts = []
        for mint in (solana_tx.NATIVE_MINT, solana_tx.USDC_MINT):
            ata = solana_tx.associated_token_address(phantom_transport.REQUIRED_WALLET, mint, solana_tx.TOKEN_PROGRAM)
            self.wallet_accounts.append(ata)
            self.records[ata] = _account(solana_tx.TOKEN_PROGRAM, whirlpool.reference_token_account(mint, phantom_transport.REQUIRED_WALLET))
        # An absent classic-pool oracle is explicitly represented by RPC null.
        self.records[whirlpool.oracle_address(direct.SOL_USDC_POOL)] = None

    def __call__(self, request):
        self.calls.append(deepcopy(request))
        method = request["method"]
        if method in self.overrides:
            value = self.overrides[method]
            return {"result": value(request) if callable(value) else deepcopy(value)}
        if method == "getGenesisHash":
            result = solana_inspect.MAINNET_GENESIS
        elif method == "getAccountInfo":
            result = {"context": {"slot": self.slot - 1}, "value": deepcopy(self.pool)}
        elif method == "getMultipleAccounts":
            if self.moves:
                self.moves -= 1
                self.slot += 1
                data = bytearray(base64.b64decode(self.pool["data"][0]))
                current = int.from_bytes(data[81:85], "little", signed=True) - 704
                data[81:85] = current.to_bytes(4, "little", signed=True)
                data[65:81] = quote.sqrt_price_from_tick(current).to_bytes(16, "little")
                self.pool = _account(whirlpool.PROGRAM_ID, bytes(data))
                self.records[direct.SOL_USDC_POOL] = self.pool
                start = whirlpool.start_tick_index(current, 8)
                for index in range(3):
                    point = start - index * 704
                    address = whirlpool.tick_array_address(direct.SOL_USDC_POOL, point)
                    self.records.setdefault(address, _account(whirlpool.PROGRAM_ID, whirlpool.reference_tick_array(direct.SOL_USDC_POOL, point)))
            result = {"context": {"slot": self.slot}, "value": [deepcopy(self.records.get(address)) for address in request["params"][0]]}
        elif method == "getLatestBlockhash":
            result = {"context": {"slot": self.slot + 1}, "value": {"blockhash": BLOCK, "lastValidBlockHeight": 900000000}}
        elif method == "getFeeForMessage":
            result = {"context": {"slot": self.slot + 2}, "value": 5000}
        elif method == "getMinimumBalanceForRentExemption":
            result = 2039280
        elif method == "isBlockhashValid":
            result = {"context": {"slot": self.slot + 3}, "value": True}
        elif method == "simulateTransaction":
            result = {"context": {"slot": self.slot + 4}, "value": {"err": None}}
        else:
            raise AssertionError("unexpected RPC method " + method)
        return {"result": result}


class DirectReviewTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeRpc()
        self.rpc = review.ReadOnlyRpc(self.fake)
        self.clock = mock.patch.object(review.time, "monotonic", return_value=100.0)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def test_fresh_review_binds_bytes_quote_fee_and_rpc_context_without_signing(self):
        with mock.patch("subprocess.run", side_effect=AssertionError("no signer process")):
            prepared = review.prepare(self.rpc)
        result = prepared.report()
        self.assertTrue(result["snapshot_bound"])
        self.assertTrue(result["transaction_bound"])
        self.assertTrue(result["minimum_output_bound"])
        self.assertEqual(result["snapshot_source"], "injected_test_rpc")
        self.assertIsNone(result["rpc_endpoint"])
        self.assertEqual(result["commitment"], "confirmed")
        self.assertEqual(result["estimated_output_base_units"], 898336)
        self.assertEqual(result["minimum_output_base_units"], 889352)
        self.assertEqual(result["network_fee_estimate_lamports"], 5000)
        self.assertEqual(result["simulation_status"], "not_run")
        self.assertFalse(result["reservation_bound"])
        self.assertFalse(result["trading_intent_bound"])
        self.assertFalse(result["network_fees_verified"])
        self.assertFalse(result["account_creation_execution_verified"])
        self.assertEqual(result["missing_wallet_token_account_count"], 0)
        self.assertEqual(result["token_account_rent_estimate_lamports"], 0)
        self.assertNotIn("getMinimumBalanceForRentExemption", [call["method"] for call in self.fake.calls])
        self.assertFalse(result["future_market_state_guaranteed"])
        self.assertFalse(result["live_ready"])
        self.assertFalse(result["send"])
        self.assertNotIn("transaction_b64", result)
        self.assertNotIn("accounts", result)
        self.assertNotIn("simulateTransaction", [call["method"] for call in self.fake.calls])
        fee_call = next(call for call in self.fake.calls if call["method"] == "getFeeForMessage")
        self.assertEqual(base64.b64decode(fee_call["params"][0]), review._message(prepared.transaction_b64))
        batch_call = next(call for call in self.fake.calls if call["method"] == "getMultipleAccounts")
        self.assertEqual(batch_call["params"][1]["minContextSlot"], self.fake.slot - 1)
        self.assertFalse(prepared.accounts[6].exists)

    def test_default_network_client_labels_exact_pinned_rpc_without_live_calls(self):
        with mock.patch.object(review.ReadOnlyRpc, "_network", side_effect=self.fake):
            result = review.prepare().report()
        self.assertEqual(result["snapshot_source"], "pinned_mainnet_rpc")
        self.assertEqual(result["rpc_endpoint"], solana_inspect.MAINNET_RPC)

    def test_changed_bytes_and_modified_review_are_rejected_before_rpc(self):
        prepared = review.prepare(self.rpc)
        before = len(self.fake.calls)
        altered = bytearray(base64.b64decode(prepared.transaction_b64))
        altered[-1] ^= 1
        with self.assertRaises(review.ReviewError) as caught:
            review.validate(prepared, base64.b64encode(altered).decode(), self.rpc)
        self.assertEqual(caught.exception.category, "review_bytes_changed")
        mutated_account = replace(prepared.accounts[0], data=prepared.accounts[0].data[:-1] + b"x")
        for changed in (replace(prepared, snapshot_slot=1), replace(prepared, network_fee_lamports=0), replace(prepared, accounts=(mutated_account, *prepared.accounts[1:]))):
            with self.assertRaises(review.ReviewError) as caught:
                review.validate(changed, rpc=self.rpc)
            self.assertEqual(caught.exception.category, "review_invalid")
        self.assertEqual(len(self.fake.calls), before)

    def test_expired_or_clock_reversed_reviews_are_rejected_before_rpc(self):
        prepared = review.prepare(self.rpc)
        before = len(self.fake.calls)
        for now in (99, 100 + review.MAX_REVIEW_AGE_S + 0.01):
            with mock.patch.object(review.time, "monotonic", return_value=now), self.assertRaises(review.ReviewError) as caught:
                review.validate(prepared, rpc=self.rpc)
            self.assertEqual(caught.exception.category, "review_expired")
        self.assertEqual(len(self.fake.calls), before)

    def test_snapshot_slot_regression_or_wrong_network_fails_closed(self):
        self.fake.overrides["getGenesisHash"] = "devnet"
        with self.assertRaises(review.ReviewError) as caught:
            review.prepare(self.rpc)
        self.assertEqual(caught.exception.category, "wrong_network")
        self.assertEqual(len(self.fake.calls), 1)
        del self.fake.overrides["getGenesisHash"]
        self.fake.overrides["getMultipleAccounts"] = {"context": {"slot": 1}, "value": []}
        with self.assertRaises(review.ReviewError) as caught:
            review.prepare(self.rpc)
        self.assertEqual(caught.exception.category, "snapshot_context_invalid")

    def test_missing_wallet_atas_remain_null_and_have_a_verified_creation_plan(self):
        for count in (1, 2):
            fake = FakeRpc()
            for address in fake.wallet_accounts[:count]:
                fake.records[address] = None
            prepared = review.prepare(review.ReadOnlyRpc(fake))
            result = prepared.report()
            self.assertTrue(result["token_account_creation_plan_verified"])
            self.assertEqual(result["missing_wallet_token_account_count"], count)
            self.assertEqual(result["existing_wallet_token_account_count"], 2 - count)
            self.assertEqual(result["token_account_rent_estimate_lamports"], 2039280 * count)
            self.assertFalse(result["rent_estimate_context_bound"])
            self.assertFalse(result["account_creation_execution_verified"])
            self.assertFalse(result["live_ready"])
            for account in prepared.accounts[-2:][:count]:
                self.assertFalse(account.exists)
                self.assertIsNone(account.owner)
                self.assertIsNone(account.data)
            rent_call = next(call for call in fake.calls if call["method"] == "getMinimumBalanceForRentExemption")
            self.assertEqual(rent_call["params"], [165, {"commitment": "confirmed"}])
            with self.assertRaises(review.ReviewError) as caught:
                review.validate(replace(prepared, token_account_rent_lamports=1), rpc=review.ReadOnlyRpc(fake))
            self.assertEqual(caught.exception.category, "review_invalid")

    def test_invalid_rent_estimate_fails_closed_for_missing_atas(self):
        for rent in (None, True, -1, 0, 0.1, 1 << 63, 1 << 64):
            fake = FakeRpc()
            for address in fake.wallet_accounts:
                fake.records[address] = None
            fake.overrides["getMinimumBalanceForRentExemption"] = rent
            with self.subTest(rent=rent), self.assertRaises(review.ReviewError) as caught:
                review.prepare(review.ReadOnlyRpc(fake))
            self.assertEqual(caught.exception.category, "rent_estimate_unavailable")

    def test_invalid_account_owner_and_encoding_are_rejected(self):
        source = self.fake.wallet_accounts[0]
        for field, value, expected in (("owner", solana_tx.SYSTEM_PROGRAM, "whirlpool_owner_unbound"), ("data", ["not base64", "base64"], "account_invalid"), ("executable", True, "account_invalid")):
            fake = FakeRpc()
            fake.records[source][field] = value
            with self.subTest(field=field), self.assertRaises(review.ReviewError) as caught:
                review.prepare(review.ReadOnlyRpc(fake))
            self.assertEqual(caught.exception.category, expected)

    def test_pool_movement_retries_once_with_new_addresses_then_stops(self):
        self.fake.moves = 1
        prepared = review.prepare(self.rpc)
        batches = [call for call in self.fake.calls if call["method"] == "getMultipleAccounts"]
        self.assertEqual(len(batches), 2)
        self.assertNotEqual(batches[0]["params"][0], batches[1]["params"][0])
        self.assertEqual(prepared.snapshot_slot, self.fake.slot)
        fake = FakeRpc()
        fake.moves = 2
        with self.assertRaises(review.ReviewError) as caught:
            review.prepare(review.ReadOnlyRpc(fake))
        self.assertEqual(caught.exception.category, "pool_moved_during_snapshot")

    def test_unknown_fee_invalid_blockhash_or_stale_context_cannot_pass(self):
        for fee in (None, True, -1, 0.1, 1 << 64):
            fake = FakeRpc()
            fake.overrides["getFeeForMessage"] = {"context": {"slot": fake.slot + 2}, "value": fee}
            with self.subTest(fee=fee), self.assertRaises(review.ReviewError) as caught:
                review.prepare(review.ReadOnlyRpc(fake))
            self.assertEqual(caught.exception.category, "network_fee_unavailable")
        self.fake.overrides["getFeeForMessage"] = {"context": {"slot": self.fake.slot}, "value": 5000}
        with self.assertRaises(review.ReviewError) as caught:
            review.prepare(self.rpc)
        self.assertEqual(caught.exception.category, "network_fee_unavailable")
        del self.fake.overrides["getFeeForMessage"]
        self.fake.overrides["isBlockhashValid"] = {"context": {"slot": self.fake.slot + 3}, "value": False}
        with self.assertRaises(review.ReviewError) as caught:
            review.prepare(self.rpc)
        self.assertEqual(caught.exception.category, "blockhash_invalid")
        self.fake.overrides["getLatestBlockhash"] = {"context": {"slot": 1}, "value": {"blockhash": BLOCK, "lastValidBlockHeight": 10}}
        with self.assertRaises(review.ReviewError) as caught:
            review.prepare(self.rpc)
        self.assertEqual(caught.exception.category, "blockhash_context_invalid")

    def test_optional_simulation_preserves_exact_bytes_and_never_requests_signatures(self):
        prepared = review.prepare(self.rpc, simulate=True)
        call = next(call for call in self.fake.calls if call["method"] == "simulateTransaction")
        self.assertEqual(call["params"][0], prepared.transaction_b64)
        self.assertIs(call["params"][1]["sigVerify"], False)
        self.assertIs(call["params"][1]["replaceRecentBlockhash"], False)
        self.assertEqual(prepared.report()["simulation_status"], "checked_unsigned")
        for result in ({}, {"unitsConsumed": 1}, {"err": {"InstructionError": [0, "bad"]}}, {"err": None, "replacementBlockhash": {"blockhash": BLOCK}}):
            fake = FakeRpc()
            fake.overrides["simulateTransaction"] = {"context": {"slot": fake.slot + 4}, "value": result}
            with self.assertRaises(review.ReviewError) as caught:
                review.prepare(review.ReadOnlyRpc(fake), simulate=True)
            self.assertEqual(caught.exception.category, "simulation_failed")

    def test_client_cannot_call_send_or_sign_methods(self):
        for method in ("sendTransaction", "requestAirdrop", "signTransaction"):
            with self.assertRaises(review.ReviewError) as caught:
                self.rpc.call(method, [])
            self.assertEqual(caught.exception.category, "rpc_method_forbidden")
        self.assertEqual(self.fake.calls, [])
