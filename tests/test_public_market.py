"""Public adapters stay fixtures here. A JSON Jupiter minimum does not unlock a send."""
import base64
import os
import unittest

import db
import dexscreener_public
import jupiter_swap_v2
import memecoin_desk
import public_market
import solana_jupiter
import solana_mint_read
import solana_tx
import token_execution
from tests.support import temp_desk_env

NOW = 1_800_000_000.0
MINT = solana_tx.b58encode(b"\x55" * 32)


def _mint_account(option: int) -> dict:
    raw = bytearray(82)
    raw[0:4] = option.to_bytes(4, "little")
    raw[44] = 6
    raw[45] = 1
    raw[46:50] = (0).to_bytes(4, "little")
    return {"owner": solana_tx.TOKEN_PROGRAM, "data": [base64.b64encode(bytes(raw)).decode("ascii"), "base64"]}


def _pair(**over):
    body = {
        "chainId": "solana",
        "dexId": "raydium",
        "pairAddress": solana_tx.b58encode(b"\x66" * 32),
        "baseToken": {"address": MINT},
        "priceUsd": "1",
        "marketCap": 1_000_000,
        "fdv": 9_000_000,
        "liquidity": {"usd": 250_000},
        "pairCreatedAt": int((NOW - 3600) * 1000),
        "priceChange": {"h1": 10},
        "txns": {"h1": {"buys": 40, "sells": 20}},
        "boosts": {"active": 3},
    }
    body.update(over)
    return [body]


class PublicAdapterTests(unittest.TestCase):
    def setUp(self):
        _tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        os.environ["APP_TOKEN"] = "dummy-app-token"
        self.addCleanup(lambda: os.environ.pop("APP_TOKEN", None))
        db.init_db()
        db.execute("UPDATE phantom_control SET value = '0' WHERE key = 'halt'")

    def test_parser_keeps_market_cap_and_does_not_prove_the_rest(self):
        parsed = dexscreener_public.parse_pairs(_pair(), MINT, NOW)
        self.assertTrue(parsed["ok"], parsed)
        self.assertFalse(parsed["fdv_used_as_market_cap"])
        self.assertEqual(parsed["scout"]["market_cap_usd"], "1000000")
        self.assertNotIn("unique_buyers", parsed["scout"])
        self.assertIn("unique_buyers", parsed["not_proven"])
        self.assertTrue(parsed["transaction_counts_are_not_unique_buyers"])
        missing = _pair()
        del missing[0]["marketCap"]
        self.assertEqual(dexscreener_public.parse_pairs(missing, MINT, NOW)["reason"], "market_cap_missing")
        missing[0]["liquidity"] = None
        missing[0]["marketCap"] = 1_000_000
        self.assertEqual(dexscreener_public.parse_pairs(missing, MINT, NOW)["reason"], "liquidity_missing")
        self.assertFalse(dexscreener_public.parse_pairs(missing, MINT, NOW)["fdv_used_as_market_cap"])
        self.assertFalse(dexscreener_public.parse_boost_mint([{"chainId": "solana", "tokenAddress": MINT, "amount": 9}])["smart_wallet_proof"])

    def test_mint_account_proves_only_the_two_authorities(self):
        revoked = solana_mint_read.parse_account(_mint_account(0), MINT)
        self.assertTrue(revoked["mint_authority_revoked"])
        self.assertTrue(revoked["freeze_authority_revoked"])
        self.assertIn("dev_rugs", revoked["not_proven"])
        self.assertIn("bundled_launch_pct", revoked["not_proven"])
        live = solana_mint_read.parse_account(_mint_account(1), MINT)
        self.assertFalse(live["mint_authority_revoked"])

    def test_public_facts_stop_before_an_unproven_lp_and_jupiter_cannot_enter_or_exit(self):
        seen = []

        def transport(request):
            seen.append(request["kind"])
            if request["kind"] == "rpc":
                return {"status": 200, "body": {"result": {"value": _mint_account(0)}}}
            url = request["url"]
            if url.endswith("/token-boosts/latest/v1"):
                return {"status": 200, "body": [{"chainId": "solana", "tokenAddress": MINT}]}
            if "/token-pairs/" in url:
                return {"status": 200, "body": _pair()}
            if "/swap/v2/order" in url:
                return {"status": 200, "body": {"router": "metis", "inAmount": "10000000", "outAmount": "11680143", "feeBps": 2, "transaction": None}}
            data = solana_jupiter.ROUTE_V2 + (1000).to_bytes(8, "little") + (900).to_bytes(8, "little")
            return {"status": 200, "body": {
                "otherAmountThreshold": "900",
                "routePlan": [{"swapInfo": {"label": "Meteora", "ammKey": "x", "inputMint": "a", "outputMint": "b", "inAmount": "1", "outAmount": "1"}, "percent": 100, "bps": 10000}],
                "swapInstruction": {"programId": solana_jupiter.PROGRAM_ID, "accounts": [{}] * 13, "data": base64.b64encode(data).decode("ascii")},
            }}

        result = public_market.run_bounded(transport, NOW)
        self.assertEqual(result["reason"], "lp")
        self.assertEqual(result["stage"], "rejected")
        self.assertFalse(result["fdv_used_as_market_cap"])
        self.assertFalse(result["jupiter_order"]["transaction_present"])
        self.assertFalse(result["jupiter_order"]["min_out_proven"])
        self.assertFalse(result["jupiter_order"]["execution_allowed"])
        self.assertTrue(result["jupiter_build"]["matches_route_v2"])
        self.assertFalse(result["jupiter_build"]["min_out_proven"])
        self.assertFalse(result["jupiter_build"]["dex_fee_proven"])
        self.assertFalse(result["jupiter_build"]["network_fee_proven"])
        self.assertFalse(result["jupiter_build"]["combined_fee_usd_proven"])
        self.assertFalse(result["jupiter_build"]["buy_proven"])
        self.assertFalse(result["jupiter_build"]["exit_proven"])
        self.assertFalse(result["jupiter_build"]["execution_allowed"])
        self.assertFalse(result["send"])
        self.assertEqual(seen.count("http"), 4)
        self.assertEqual(seen.count("rpc"), 1)
        with self.assertRaises(public_market.PublicStop):
            public_market.run_bounded(None, NOW)

    def test_a_matching_json_minimum_is_still_not_a_buy_or_an_exit(self):
        data = solana_jupiter.ROUTE_V2 + (1000).to_bytes(8, "little") + (900).to_bytes(8, "little")
        shown = jupiter_swap_v2.assess_build({
            "otherAmountThreshold": "900",
            "routePlan": [],
            "swapInstruction": {
                "programId": solana_jupiter.PROGRAM_ID,
                "accounts": [],
                "data": base64.b64encode(data).decode("ascii"),
            },
        })
        self.assertFalse(shown["min_out_proven"])
        self.assertFalse(shown["buy_proven"])
        self.assertFalse(shown["exit_proven"])
        self.assertEqual(shown["reason"], "route_v2_bytes")
        opened = memecoin_desk.scout({
            "mint": MINT, "source": "dexscreener", "fetched_at": NOW,
            "pair_created_at": int((NOW - 3600) * 1000), "market_cap_usd": "1000000",
            "liquidity_usd": "250000", "price_usd": "1", "price_change_h1": "10",
        }, now=NOW)
        self.assertTrue(opened["ok"])
        onchain = {
            "source": "stalkchain_token_onchain", "fetched_at": NOW,
            "mint_authority_revoked": True, "freeze_authority_revoked": True, "lp_burned_or_locked": True,
            "dev_holding_pct": "1",
            "dev_rugs": {"status": "checked", "source": "stalkchain_token_deployer", "rugs": 0, "tokens_seen": 1},
            "bundled_launch_pct": "1",
        }
        self.assertTrue(memecoin_desk.filter_case(opened["case_id"], onchain, now=NOW)["ok"])
        components = {
            name: {"value": "70", "source": "holder_reader", "fetched_at": NOW}
            for name in memecoin_desk.COMPONENTS
        }
        self.assertTrue(memecoin_desk.analyze(opened["case_id"], components, now=NOW)["ok"])
        self.assertEqual(memecoin_desk.protect(opened["case_id"])["stage"], "protected")
        blocked = memecoin_desk.record_jupiter_block(opened["case_id"], shown)
        self.assertEqual(blocked["reason"], "jupiter_instruction_unproven")
        self.assertEqual(memecoin_desk.view(opened["case_id"])["stage"], "protected")
        self.assertFalse(memecoin_desk.view(opened["case_id"])["snapshot"]["send"])
        closed = token_execution.supported_buy(MINT)
        self.assertEqual(closed["jupiter_route_v2"], "not_enabled")
        self.assertEqual(closed["jupiter_swap_v2"], "refused")
        self.assertFalse(closed["mobile_phantom_is_server_auth"])
        exited = token_execution.supported_exit(MINT)
        self.assertEqual(exited["jupiter_swap_v2"], "refused")
        self.assertFalse(exited["loss_stop_blocks_exit"])
