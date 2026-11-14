"""Deepest wSOL pool selection. It does not sign or send."""
import io
import logging
import os
import sys
import unittest

import cross_source
import dexscreener_public
import desk_loop
import solana_tx

NOW = 1_800_000_000.0
MINT = solana_tx.b58encode(b"\x41" * 32)
SOL = solana_tx.NATIVE_MINT
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _pair(address, quote, liquidity, **extra):
    body = {
        "chainId": "solana",
        "pairAddress": address,
        "baseToken": {"address": MINT},
        "quoteToken": {"address": quote, "symbol": "SOL" if quote == SOL else "USDC"},
        "liquidity": {"usd": liquidity},
        "marketCap": extra.pop("marketCap", 1_000_000),
        "priceUsd": extra.pop("priceUsd", "1"),
        "priceChange": extra.pop("priceChange", {"h1": 10}),
        "pairCreatedAt": extra.pop("pairCreatedAt", int((NOW - 3600) * 1000)),
        "volume": extra.pop("volume", {"h24": 1}),
        "fdv": extra.pop("fdv", 9_000_000),
    }
    body.update(extra)
    return body


class SolPoolTests(unittest.TestCase):
    def test_the_deepest_sol_pool_wins_and_gaps_do_not_fall_back(self):
        deep = _pair("pool-deep", SOL, 500, volume={"h24": 1})
        shallow = _pair("pool-shallow", SOL, 50, volume={"h24": 9_000})
        foreign = _pair("pool-usdc", USDC, 5_000_000)
        parsed = dexscreener_public.parse_pairs([shallow, foreign, deep], MINT, NOW)
        self.assertTrue(parsed["ok"], parsed)
        self.assertFalse(parsed["fdv_used_as_market_cap"])
        self.assertEqual(parsed["scout"]["pair_address"], "pool-deep")
        self.assertEqual(parsed["scout"]["liquidity_usd"], "500")
        self.assertEqual(parsed["scout"]["candidate_pools"], 2)
        self.assertEqual(parsed["scout"]["market_cap_basis"], "marketCap")

        same = _pair("pool-b", SOL, 80, volume={"h24": 4})
        richer = _pair("pool-a", SOL, 80, volume={"h24": 9})
        tied = dexscreener_public.parse_pairs([same, richer], MINT, NOW)
        self.assertEqual(tied["scout"]["pair_address"], "pool-a")
        address_low = _pair("pool-a", SOL, 80, volume={"h24": 4})
        address_high = _pair("pool-b", SOL, 80, volume={"h24": 4})
        by_address = dexscreener_public.parse_pairs([address_low, address_high], MINT, NOW)
        self.assertEqual(by_address["scout"]["pair_address"], "pool-b")

        only_foreign = dexscreener_public.parse_pairs([foreign], MINT, NOW)
        self.assertEqual(only_foreign["reason"], "quote_not_sol")
        self.assertEqual(only_foreign["stage"], "scout")
        self.assertFalse(only_foreign["fdv_used_as_market_cap"])

        incomplete = _pair("pool-deep", SOL, 500, priceChange={"m5": 1})
        complete = _pair("pool-shallow", SOL, 10)
        refused = dexscreener_public.parse_pairs([complete, incomplete], MINT, NOW)
        self.assertEqual(refused["reason"], "price_change_h1_missing")
        self.assertEqual(refused["pair_address"], "pool-deep")
        self.assertEqual(refused["stage"], "scout")
        self.assertFalse(refused["fdv_used_as_market_cap"])

        no_cap = _pair("pool-deep", SOL, 500)
        no_cap.pop("marketCap")
        no_cap["fdv"] = 8_000_000
        kept = _pair("pool-shallow", SOL, 10, marketCap=1_000_000, fdv=1)
        missing_cap = dexscreener_public.parse_pairs([kept, no_cap], MINT, NOW)
        self.assertEqual(missing_cap["reason"], "market_cap_missing")
        self.assertEqual(missing_cap["pair_address"], "pool-deep")
        self.assertTrue(missing_cap["fdv_present"])
        self.assertFalse(missing_cap["fdv_used_as_market_cap"])
        self.assertNotEqual(missing_cap.get("market_cap_usd"), "8000000")

        bare = [
            {"chainId": "solana", "pairAddress": "pool-a", "baseToken": {"address": MINT}, "quoteToken": {"address": SOL}},
            {"chainId": "solana", "pairAddress": "pool-b", "baseToken": {"address": MINT}, "quoteToken": {"address": SOL}, "fdv": 1},
        ]
        ambiguous = dexscreener_public.parse_pairs(bare, MINT, NOW)
        self.assertEqual(ambiguous["reason"], "pool_ambiguous")
        self.assertEqual(ambiguous["candidate_pools"], 2)

    def test_only_a_simulate_transaction_counts_as_a_sim(self):
        counts = desk_loop._pass_counts({
            "candidates": 2,
            "screened": [
                {"mint": MINT, "stage": "scout", "reason": "age", "quote": {"attempted": True, "reason": "pool_dex_unsupported"}},
                {"mint": MINT, "stage": "filter", "reason": "lp", "quote": {"simulate": True, "ok": False, "attempted": True}},
            ],
        })
        self.assertEqual(counts["sims_attempted"], 1)
        self.assertEqual(counts["sims_failed"], 1)
        self.assertEqual(counts["sims_ok"], 0)
        self.assertEqual(counts["pool_checks"], 1)
        self.assertEqual(counts["filter_rejects"], {"lp": 1})

    def test_observe_logs_reach_stdout_without_the_key(self):
        from config import install_secret_redaction
        secret = "helius-pool-log-secret"
        os.environ["HELIUS_API_KEY"] = secret
        self.addCleanup(lambda: os.environ.pop("HELIUS_API_KEY", None))
        install_secret_redaction()
        desk_loop.configure_observe_logs()
        captured = io.StringIO()
        logger = logging.getLogger("desk.loop")
        provider = logging.getLogger("desk.provider")
        self.assertEqual(logger.level, logging.INFO)
        self.assertEqual(provider.level, logging.INFO)
        found = [
            handler
            for item in (logger, provider)
            for handler in item.handlers
            if getattr(handler, "_desk_stdout", False)
        ]
        self.assertGreaterEqual(len(found), 2)
        self.assertTrue(all(handler.stream is sys.stdout for handler in found))
        previous = [(handler, handler.stream) for handler in found]
        for handler, _stream in previous:
            handler.stream = captured
        self.addCleanup(lambda: [setattr(handler, "stream", stream) for handler, stream in previous])
        logger.info("pass candidates=4 key=%s", secret)
        desk_loop.note_provider("helius", "https://mainnet.helius-rpc.com/?api-key=" + secret)
        text = captured.getvalue()
        self.assertIn("pass candidates=4", text)
        self.assertIn("provider helius redacted", text)
        self.assertNotIn(secret, text)
        self.assertNotIn("mainnet.helius-rpc.com", text)
        self.assertNotIn("api-key=", text)
