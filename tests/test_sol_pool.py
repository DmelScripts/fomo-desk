"""Deepest wSOL pool selection. It does not sign or send."""
import io
import json
import logging
import os
import sys
import unittest

import cross_source
import dexscreener_public
import desk_loop
import memecoin_desk
import solana_tx
from tests.support import arm_holder_chart, clear_filter_fixtures, temp_desk_env

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
            "candidates": 3,
            "screened": [
                {"mint": MINT, "stage": "scout", "reason": "age", "quote": {"attempted": True, "reason": "pool_dex_unsupported"}},
                {"mint": MINT, "stage": "filter", "reason": "lp", "quote": {"attempted": True, "pre_sign": {"simulate": True, "simulation": {"ok": False}}}},
                {"mint": MINT, "stage": "protected", "quote": {"attempted": True, "pre_sign": {"simulate": True, "preflight_ready": True, "simulation": {"ok": True}}}},
            ],
        })
        self.assertEqual(counts["sims_attempted"], 2)
        self.assertEqual(counts["sims_failed"], 1)
        self.assertEqual(counts["sims_ok"], 1)
        self.assertEqual(counts["pool_checks"], 1)
        self.assertEqual(counts["filter_rejects"], {"lp": 1})
        eth = _pair("pool-eth", SOL, 500)
        eth["chainId"] = "ethereum"
        numbered = _pair("pool-num", SOL, 500)
        numbered["chainId"] = 1
        self.assertEqual(dexscreener_public.parse_pairs([eth], MINT, NOW)["reason"], "chain_rejected")
        self.assertEqual(dexscreener_public.parse_pairs([numbered], MINT, NOW)["reason"], "chain_rejected")
        self.assertIsNone(cross_source.select_pair({"pairs": [eth, numbered]}, MINT, "")[0])

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

    def test_later_stages_stay_on_the_pinned_pool(self):
        shallow = "shallow-pool-" + MINT[:8]
        deep = "deep-pool-" + MINT[:8]
        _tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        import db
        db.init_db()
        cross_source.clear_fetcher()
        self.addCleanup(cross_source.clear_fetcher)
        self.addCleanup(clear_filter_fixtures)

        def row(address, liquidity, price, change):
            return {
                "chainId": "solana",
                "pairAddress": address,
                "baseToken": {"address": MINT},
                "quoteToken": {"address": SOL},
                "dexId": "whirlpool",
                "liquidity": {"usd": liquidity},
                "marketCap": 1_000_000,
                "priceUsd": price,
                "priceChange": {"h1": change},
                "pairCreatedAt": int((NOW - 3600) * 1000),
                "volume": {"h1": 1000, "h24": 1000},
                "txns": {"h1": {"buys": 30, "sells": 10}},
            }

        bundle = {
            "mint": MINT,
            "pair_id": shallow,
            "dex": {"pairs": [row(deep, 500, "2", 10), row(shallow, 50, "9", 40)]},
            "rugcheck": {
                "creator": "SameCreator", "creatorBalance": 0, "token": {"supply": 1000},
                "mintAuthority": None, "freezeAuthority": None, "risks": [],
                "creatorTokens": [{"mint": MINT}],
                "markets": [
                    {"pubkey": deep, "lp": {"quoteUSD": "250000", "lpLockedPct": 100}},
                    {"pubkey": shallow, "lp": {"quoteUSD": "250000", "lpLockedPct": 100}},
                ],
            },
            "tracker": {
                "holders": 10,
                "token": {"creation": {"creator": "SameCreator"}},
                "risk": {"dev": {"percentage": 0}, "risks": []},
                "pools": [
                    {"poolId": deep, "lpBurn": 100, "liquidity": {"quote": "250000", "usd": 1}, "marketCap": {"quote": "1000000", "usd": "1000000"}},
                    {"poolId": shallow, "lpBurn": 100, "liquidity": {"quote": "250000", "usd": 1}, "marketCap": {"quote": "1000000", "usd": "1000000"}},
                ],
            },
            "chart": {"holders": [{"holders": 100, "time": 1}, {"holders": 140, "time": 2}]},
            "das": {"result": {"total": 1, "items": [{"id": MINT}]}},
            "deployer": {"total": 1, "data": [{"mint": MINT}]},
        }
        cross_source.note_bundle(MINT, bundle)
        cross_source.note_cap(MINT, "1000000")
        cross_source.arm_test_fetcher()
        opened = memecoin_desk.scout({
            "mint": MINT, "source": "dexscreener", "fetched_at": NOW,
            "market_cap_basis": "marketCap", "pair_address": shallow,
        }, now=NOW)
        self.assertTrue(opened["ok"], opened)
        snap = memecoin_desk.view(opened["case_id"])["snapshot"]
        self.assertEqual(snap["pair_address"], shallow)
        self.assertEqual(snap["price_usd"], "9")
        self.assertEqual(snap["price_change_h1"], "40")
        self.assertEqual(snap["candidate_pools"], 2)
        cross_source.note_bundle(MINT + "x", dict(bundle, mint=MINT + "x", dex={"pairs": [row(deep, 500, "2", 10)]}))
        missing = memecoin_desk.scout({
            "mint": MINT + "x", "source": "dexscreener", "fetched_at": NOW,
            "market_cap_basis": "marketCap", "pair_address": shallow,
        }, now=NOW)
        self.assertEqual(missing["reason"], "pinned_pair_missing", missing)
        self.assertEqual(missing["stage"], "scout")

        db.execute("UPDATE memecoin_cases SET stage = 'filter_passed' WHERE id = ?", (opened["case_id"],))
        arm_holder_chart()
        parts = memecoin_desk._fetched_components(MINT, shallow)
        self.assertTrue(parts["ok"], parts)
        self.assertEqual(parts["values"]["momentum"], __import__("decimal").Decimal("40"))
        memecoin_desk.analyze(opened["case_id"], {"holder_growth": {"source": "solana_tracker_holders_chart"}}, now=NOW)
        scored = memecoin_desk.view(opened["case_id"])["snapshot"]
        self.assertEqual(scored["pair_address"], shallow)
        self.assertNotEqual(scored.get("price_usd"), "2")

        db.execute("UPDATE memecoin_cases SET stage = 'analyst_passed' WHERE id = ?", (opened["case_id"],))
        db.execute("INSERT INTO phantom_control (key, value, updated_at) VALUES ('halt', '0', 0) ON CONFLICT(key) DO UPDATE SET value = '0'")
        from tests.test_memecoin_desk import _protect
        protected = _protect(opened["case_id"], "paper")
        self.assertEqual(protected.get("stage"), "protected", protected)
        priced = memecoin_desk.view(opened["case_id"])["snapshot"]
        self.assertEqual(priced["pair_address"], shallow)
        self.assertEqual(priced["entry_price_usd"], "9")
        self.assertNotEqual(priced["entry_price_usd"], "2")

        exit_id = opened["case_id"] + ":exit"
        db.execute(
            """
            INSERT INTO memecoin_cases (
                id, mint, stage, snapshot_json, created_at, updated_at, ledger, ledger_provenance
            ) VALUES (?, ?, 'filled', ?, ?, ?, 'live', 'server')
            """,
            (exit_id, MINT, json.dumps({"pair_address": shallow, "entry_price_usd": "9"}), NOW, NOW),
        )
        db.execute(
            """
            INSERT INTO memecoin_entry_receipts (
                signature, case_id, message_sha256, slot, qty, cost_basis_lamports,
                network_fee_lamports, dex_fee_lamports, fee_usd, failed, ledger, pool, ledger_provenance
            ) VALUES ('pin-entry', ?, 'pin', 1, 10, 1, 1, 1, '0.01', 0, 'live', ?, 'server')
            """,
            (exit_id, shallow),
        )
        asked = []

        def transport(request):
            asked.append(request.get("method") or request.get("url"))
            if request.get("kind") == "http" or isinstance(request.get("url"), str):
                return {"status": 200, "body": [row(deep, 500, "2", 10), row(shallow, 50, "9", 40)]}
            return {"status": 200, "body": {"result": None}}

        supervised = desk_loop._supervise_open(transport, NOW)
        self.assertTrue(supervised, supervised)
        self.assertEqual(supervised[0].get("pool"), shallow)
        self.assertNotEqual(supervised[0].get("pool"), deep)
        quote = supervised[0].get("quote") if isinstance(supervised[0].get("quote"), dict) else {}
        if quote:
            self.assertEqual(quote.get("pool"), shallow)
        db.execute("UPDATE memecoin_entry_receipts SET pool = ? WHERE case_id = ?", ("missing-pool", exit_id))
        missed = desk_loop._supervise_open(transport, NOW)
        self.assertEqual(missed[0]["reason"], "pinned_pair_missing")
        self.assertEqual(missed[0]["pool"], "missing-pool")
