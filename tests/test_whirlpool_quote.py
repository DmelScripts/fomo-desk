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

    def test_compute_swap_matches_the_program_max_step(self):
        """programs/whirlpool/src/math/swap_math.rs swap_a_to_b_input_max at 2509ad9."""
        step = quote._swap_step(1000, 20_000, 1296, 9 << 64, 4 << 64, True, True)
        self.assertEqual(step["amount_in"], 180)
        self.assertEqual(step["amount_out"], 6480)
        self.assertEqual(step["fee_amount"], 4)
        self.assertEqual(step["next_sqrt_price"], 4 << 64)

    def test_two_crossed_ticks_charge_more_than_the_one_shot_ceil(self):
        arrays = []
        for start in (0, -88, -176):
            ticks = [{"initialized": False, "liquidity_net": 0} for _ in range(88)]
            arrays.append({"start_tick_index": start, "ticks": ticks})
        arrays[1]["ticks"][87] = {"initialized": True, "liquidity_net": 0}
        arrays[1]["ticks"][86] = {"initialized": True, "liquidity_net": 0}
        pool = {
            "sqrt_price": quote.sqrt_price_from_tick(0),
            "tick_current_index": 0,
            "fee_rate": 3000,
            "liquidity": 1,
            "tick_spacing": 1,
        }
        got = quote._compute_swap(4, 0, pool, arrays, True, True)
        self.assertEqual(got["trade_fee"], 2)
        self.assertEqual(got["steps"], 2)
        self.assertEqual(got["amount_remaining"], 0)
        self.assertEqual((4 * 3000 + 1_000_000 - 1) // 1_000_000, 1)
        self.assertLess(got["trade_fee"], quote.exact_in_input_ceiling(4))

    def test_exact_in_fee_cannot_exceed_the_input(self):
        """Every mix of crossing steps and one final partial step stays within the input."""
        for rate in (0, 1, 3000, quote.MAX_STATIC_FEE_RATE):
            for amount in range(1, 25):
                self.assertEqual(quote.exact_in_input_ceiling(amount), amount)
                best = [0] * (amount + 1)
                for left in range(1, amount + 1):
                    best[left] = left
                    for amount_in in range(0, left + 1):
                        step_fee = quote.crossing_step_fee(amount_in, rate)
                        consumed = amount_in + step_fee
                        if consumed == 0 or consumed > left:
                            continue
                        best[left] = max(best[left], step_fee + best[left - consumed])
                self.assertEqual(best[amount], amount)

    def test_a_partial_step_can_charge_the_whole_input(self):
        price = quote.sqrt_price_from_tick(0)
        target = quote.sqrt_price_from_tick(-100)
        step = quote._swap_step(4, 3000, 10**24, price, target, True, True)
        self.assertEqual(step["amount_in"], 0)
        self.assertEqual(step["fee_amount"], 4)
        self.assertEqual(step["fee_amount"], quote.exact_in_input_ceiling(4))

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
