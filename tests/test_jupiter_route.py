"""Jupiter shared_accounts_route cases. None of them enable sending."""
import unittest

import phantom_read
import solana_inspect
import solana_jupiter
import solana_tx
from tests.test_solana_inspect import BLOCK, FakeRpc, WALLET, _jupiter_route

JUP = solana_jupiter.PROGRAM_ID


def _keys():
    return [
        WALLET,
        solana_tx.b58encode(b"\x44" * 32),
        solana_tx.b58encode(b"\x45" * 32),
        solana_tx.b58encode(b"\x46" * 32),
        solana_tx.b58encode(b"\x47" * 32),
        solana_tx.b58encode(b"\x48" * 32),
        solana_tx.NATIVE_MINT,
        solana_tx.USDC_MINT,
        solana_tx.TOKEN_PROGRAM,
        solana_tx.SYSTEM_PROGRAM,
        JUP,
    ]


def _swap(data: bytes):
    return solana_tx.encode_legacy(WALLET, _keys(), 5, [
        (9, [0, 0], (2).to_bytes(4, "little") + (10_000_000).to_bytes(8, "little")),
        (10, [8, 1, 0, 2, 3, 4, 5, 6, 7], data),
    ], BLOCK)


def _show(data: bytes):
    return solana_inspect.inspect(_swap(data), solana_inspect.RpcClient(FakeRpc()))


class TestJupiterRoute(unittest.TestCase):
    def test_valid_route_is_not_execution_and_slippage_is_only_a_field(self):
        shown = _show(_jupiter_route(898162, 100))
        self.assertTrue(shown["chain_ok"])
        self.assertTrue(shown["slippage_field_ok"])
        self.assertFalse(shown["slippage_verified"])
        self.assertFalse(shown["spend_verified"])
        self.assertFalse(shown["intent_verified"])
        self.assertFalse(shown["execution_allowed"])
        self.assertFalse(shown["send"])
        self.assertEqual(shown["quoted_out_amount"], "898162")
        self.assertEqual(shown["slippage_bps"], 100)

    def test_amount_above_the_cap_is_rejected(self):
        data = bytearray(_jupiter_route(898162, 100))
        data[8 + 1 + 4 + 4:8 + 1 + 4 + 4 + 8] = (10_000_001).to_bytes(8, "little")
        shown = _show(bytes(data))
        self.assertEqual(shown["error_category"], "amount_unbound")
        self.assertFalse(shown["execution_allowed"])

    def test_route_amount_must_equal_the_sol_debit(self):
        data = bytearray(_jupiter_route(898162, 100))
        data[8 + 1 + 4 + 4:8 + 1 + 4 + 4 + 8] = (9_000_000).to_bytes(8, "little")
        shown = _show(bytes(data))
        self.assertEqual(shown["error_category"], "amount_unbound")

    def test_slippage_above_one_percent_is_rejected(self):
        shown = _show(_jupiter_route(898162, 101))
        self.assertEqual(shown["error_category"], "slippage_unbound")
        self.assertFalse(shown["slippage_verified"])
        self.assertFalse(shown["slippage_field_ok"])

    def test_platform_fee_is_rejected(self):
        data = bytearray(_jupiter_route(898162, 100))
        data[-1] = 1
        shown = _show(bytes(data))
        self.assertEqual(shown["error_category"], "slippage_unbound")

    def test_authority_must_be_the_allowlisted_wallet(self):
        other = solana_tx.b58encode(b"\x44" * 32)
        tx = solana_tx.encode_legacy(WALLET, _keys(), 5, [
            (9, [0, 0], (2).to_bytes(4, "little") + (10_000_000).to_bytes(8, "little")),
            (10, [8, 1, 1, 2, 3, 4, 5, 6, 7], _jupiter_route(898162, 100)),
        ], BLOCK)
        shown = solana_inspect.inspect(tx, solana_inspect.RpcClient(FakeRpc()))
        self.assertEqual(shown["error_category"], "wallet_unbound")
        self.assertNotEqual(other, WALLET)

    def test_mints_must_be_native_sol_to_usdc(self):
        tx = solana_tx.encode_legacy(WALLET, _keys(), 5, [
            (9, [0, 0], (2).to_bytes(4, "little") + (10_000_000).to_bytes(8, "little")),
            (10, [8, 1, 0, 2, 3, 4, 5, 7, 6], _jupiter_route(898162, 100)),
        ], BLOCK)
        shown = solana_inspect.inspect(tx, solana_inspect.RpcClient(FakeRpc()))
        self.assertEqual(shown["error_category"], "token_mint_unbound")
        self.assertEqual(shown["observed_mint"], solana_tx.NATIVE_MINT)

    def test_buy_amount_equality_is_not_intent_verification(self):
        body = {
            "quoteRequest": {
                "taker": {"address": WALLET},
                "buyToken": {"chainId": "solana:101", "address": solana_tx.USDC_MINT},
                "sellToken": {"chainId": "solana:101", "resourceType": "nativeToken", "slip44": "501"},
                "sellAmount": "10000000",
                "execute": False,
            },
            "quoteResponse": {"quotes": [{"quoteId": "pq", "sellAmount": "10000000", "buyAmount": "898162", "transactionData": _swap(_jupiter_route(898162, 100))}]},
        }
        import json
        phantom_read._rpc = lambda: solana_inspect.RpcClient(FakeRpc())
        shown = phantom_read._preflight(json.dumps(body), 0)
        self.assertTrue(shown["quoted_out_equals_buy_amount"])
        self.assertFalse(shown["intent_verified"])
        self.assertFalse(shown["slippage_verified"])
        self.assertTrue(shown["slippage_field_ok"])
        self.assertNotIn(body["quoteResponse"]["quotes"][0]["transactionData"], json.dumps(shown))
        body["quoteResponse"]["quotes"][0]["buyAmount"] = "1"
        body["quoteResponse"]["quotes"][0]["transactionData"] = _swap(_jupiter_route(898162, 100))
        mismatched = phantom_read._preflight(json.dumps(body), 0)
        self.assertFalse(mismatched["quoted_out_equals_buy_amount"])
        self.assertFalse(mismatched["intent_verified"])

    def test_unsupported_discriminator_and_enum_fail_closed(self):
        shown = _show(b"\x11" * 8 + _jupiter_route(898162, 100)[8:])
        self.assertEqual(shown["error_category"], "jupiter_instruction_unsupported")
        bad = bytearray(_jupiter_route(898162, 100))
        bad[13] = 250
        shown = _show(bytes(bad))
        self.assertEqual(shown["error_category"], "unsupported_route")

    def test_truncation_trailing_bytes_and_route_length_fail_closed(self):
        route = _jupiter_route(898162, 100)
        self.assertEqual(_show(route[:-1])["error_category"], "unsupported_route")
        self.assertEqual(_show(route + b"\x00")["error_category"], "unsupported_route")
        empty = route[:8] + bytes([1]) + (0).to_bytes(4, "little") + route[8 + 1 + 4 + 4:]
        self.assertEqual(_show(empty)["error_category"], "unsupported_route")
        long = route[:8] + bytes([1]) + (17).to_bytes(4, "little") + route[13:]
        self.assertEqual(_show(long)["error_category"], "unsupported_route")


def _swap(data: bytes) -> str:
    return solana_tx.encode_legacy(WALLET, [
        WALLET, solana_tx.b58encode(b"\x44" * 32), solana_tx.b58encode(b"\x45" * 32),
        solana_tx.b58encode(b"\x46" * 32), solana_tx.b58encode(b"\x47" * 32), solana_tx.b58encode(b"\x48" * 32),
        solana_tx.NATIVE_MINT, solana_tx.USDC_MINT, solana_tx.TOKEN_PROGRAM, solana_tx.SYSTEM_PROGRAM,
        solana_jupiter.PROGRAM_ID,
    ], 5, [
        (9, [0, 0], (2).to_bytes(4, "little") + (10_000_000).to_bytes(8, "little")),
        (10, [8, 1, 0, 2, 3, 4, 5, 6, 7], data),
    ], BLOCK)
