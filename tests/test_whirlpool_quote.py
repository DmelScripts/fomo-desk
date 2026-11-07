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
