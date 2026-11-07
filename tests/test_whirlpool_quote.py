"""Official rust-sdk quote vectors. No network and no signing."""
import unittest
from copy import deepcopy
import base64
import json
from pathlib import Path

import solana_whirlpool_quote as quote
import solana_whirlpool


class WhirlpoolQuoteTests(unittest.TestCase):
    def fixture(self):
        pool = {"sqrt_price": 1 << 64, "tick_current_index": 0, "fee_rate": 3000, "liquidity": 10**18, "tick_spacing": 8, "adaptive_fee": False}
        arrays = [{"start_tick_index": start, "ticks": [{"initialized": False, "liquidity_net": 0} for _ in range(88)]} for start in (-704, 0, 704)]
        return pool, arrays

    def test_sdk_exact_in_vectors(self):
        expected = (
            (True, True, 996, 896, 3),
            (True, False, 920, 828, 38),
            (False, True, 996, 896, 3),
            (False, False, 918, 826, 39),
        )
        for specified_a, liquid, est_out, minimum, fee in expected:
            got = quote.sdk_fixture(1000, specified_a, 1000, liquid, True)
            self.assertEqual(got["token_est_out"], est_out)
            self.assertEqual(got["token_min_out"], minimum)
            self.assertEqual(got["trade_fee"], fee)
            self.assertEqual(got["token_in"], 1000)
            self.assertNotIn("blockhash", got)
            self.assertEqual(got["context_slot"], 1)

    def test_sdk_exact_out_vectors(self):
        expected = (
            (False, True, 1005, 1106, 4),
            (False, False, 1088, 1197, 42),
            (True, True, 1005, 1106, 4),
            (True, False, 1088, 1197, 42),
        )
        for specified_a, liquid, est_in, maximum, fee in expected:
            got = quote.sdk_fixture(1000, specified_a, 1000, liquid, False)
            self.assertEqual(got["token_est_in"], est_in)
            self.assertEqual(got["token_max_in"], maximum)
            self.assertEqual(got["trade_fee"], fee)

    def test_slippage_floor_matches_the_published_996_case(self):
        self.assertEqual(quote.min_amount(996, 1000), 896)
        self.assertEqual(quote.sqrt_price_from_tick(0), 1 << 64)
        self.assertEqual(quote.sqrt_price_to_tick_index(1 << 64), 0)
        with self.assertRaises(quote.QuoteError):
            quote.quote_exact_in({}, [], 1, True, 1, 0, "blockhash")

    def test_adaptive_pool_is_rejected_without_an_oracle_fee_model(self):
        pool, arrays = self.fixture()
        pool["adaptive_fee"] = True
        with self.assertRaises(quote.QuoteError) as caught:
            quote.quote_exact_in(pool, arrays, 1000, True, 100, 0, 1)
        self.assertEqual(caught.exception.category, "adaptive_fee_unsupported")

    def test_empty_or_malformed_tick_sequences_fail_with_quote_error(self):
        pool, arrays = self.fixture()
        malformed = [[], arrays + arrays, [{"start_tick_index": 0, "ticks": []}]]
        invalid_flag = deepcopy(arrays)
        invalid_flag[0]["ticks"][0]["initialized"] = 2
        malformed.append(invalid_flag)
        for value in malformed:
            with self.subTest(value=value), self.assertRaises(quote.QuoteError):
                quote.quote_exact_in(pool, value, 1000, True, 100, 0, 1)
        # A single array is valid when the entire swap fits inside it.
        got = quote.quote_exact_in(pool, [arrays[1]], 1000, False, 100, 0, 1)
        self.assertEqual(got["token_in"], 1000)

    def test_u64_amounts_and_pool_fields_are_strict(self):
        pool, arrays = self.fixture()
        for amount in (True, -1, 0, 1.5, 1 << 64):
            with self.subTest(amount=amount), self.assertRaises(quote.QuoteError):
                quote.quote_exact_in(pool, arrays, amount, True, 100, 0, 1)
        for field, value in (("liquidity", 1 << 128), ("fee_rate", -1), ("tick_spacing", 0), ("sqrt_price", 0)):
            bad = dict(pool, **{field: value})
            with self.subTest(field=field), self.assertRaises(quote.QuoteError):
                quote.quote_exact_in(bad, arrays, 1000, True, 100, 0, 1)
        with self.assertRaises(quote.QuoteError):
            quote.min_amount(1 << 64, 9999)

    def test_exhausted_tick_sequence_and_liquidity_overflow_fail_closed(self):
        pool, arrays = self.fixture()
        with self.assertRaises(quote.QuoteError) as caught:
            quote.quote_exact_in(pool, [arrays[1]], 1000, True, 100, 0, 1)
        self.assertEqual(caught.exception.category, "tick_array_unbound")
        with self.assertRaises(quote.QuoteError):
            quote._next_liquidity((1 << 128) - 1, {"liquidity_net": -1}, True)

    def test_public_pool_snapshot_matches_independently_run_official_sdk(self):
        # Recorded with @orca-so/whirlpools-core@3.1.1 over these same raw public
        # account bytes, using an independent JS decoder. No RPC or signing.
        fixture = json.loads((Path(__file__).parent / "fixtures/corrected_whirlpool_snapshot.json").read_text())
        records = fixture["accounts"]
        self.assertTrue(all(record["owner"] == solana_whirlpool.PROGRAM_ID for record in records))
        pool = solana_whirlpool.pool_state(base64.b64decode(records[0]["data_base64"], validate=True))
        arrays = [solana_whirlpool.tick_array_state(base64.b64decode(record["data_base64"], validate=True)) for record in records[1:]]
        for record, array in zip(records[1:], arrays):
            self.assertEqual(array["pool"], fixture["pool_address"])
            self.assertEqual(record["address"], solana_whirlpool.tick_array_address(fixture["pool_address"], array["start_tick_index"]))
        args = fixture["request"]
        got = quote.quote_exact_in(pool, arrays, args["amount"], args["a_to_b"], args["slippage_bps"], args["sqrt_price_limit"], fixture["source"]["context_slot"])
        for field in ("token_in", "token_est_out", "token_min_out", "trade_fee"):
            self.assertEqual(got[field], fixture["official_sdk"]["expected"][field])
