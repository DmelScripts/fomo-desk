"""Official rust-sdk quote vectors. No network and no signing."""
import unittest

import solana_whirlpool_quote as quote


class WhirlpoolQuoteTests(unittest.TestCase):
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

    def test_negative_tick_rounding_and_zero_liquidity_match_the_sdk(self):
        arrays = []
        for start in (-176, 0, 176):
            net = 1000 if start < 0 else -1000
            arrays.append({
                "start_tick_index": start,
                "ticks": [{"initialized": True, "liquidity_net": net} for _ in range(88)],
            })
        pool = {
            "sqrt_price": 1 << 64,
            "tick_current_index": -16,
            "fee_rate": 3000,
            "liquidity": 265_000,
            "tick_spacing": 2,
        }
        got = quote.quote_exact_in(pool, arrays, 1000, True, 50, 0, 1)
        self.assertEqual((got["token_est_out"], got["token_min_out"], got["trade_fee"]), (934, 929, 31))
        self.assertLess(got["sqrt_price_after"], got["sqrt_price_before"])
        exit_quote = quote.quote_exact_in(pool, arrays, 1000, False, 1000, 0, 1)
        self.assertGreater(exit_quote["token_est_out"], 0)
        self.assertEqual(quote.sdk_fixture(1000, False, 1000, False, True)["token_est_out"], 918)
        pool["liquidity"] = 0
        zero = quote.quote_exact_in(pool, arrays, 1000, True, 1000, 0, 1)
        self.assertEqual((zero["token_est_out"], zero["token_min_out"], zero["trade_fee"]), (0, 0, 1000))
        pool["fee_rate"] = 1_000_001
        with self.assertRaises(quote.QuoteError) as caught:
            quote.quote_exact_in(pool, arrays, 1000, True, 50, 0, 1)
        self.assertEqual(caught.exception.category, "fee_unbound")
