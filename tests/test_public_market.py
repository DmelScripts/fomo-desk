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
import solana_whirlpool
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
        from tests.test_memecoin_desk import _arm_recorded_exit
        original = memecoin_desk.reserve_exit

        def reserve_exit(case_id, *args, **kwargs):
            _arm_recorded_exit(case_id)
            return original(case_id, *args, **kwargs)

        memecoin_desk.reserve_exit = reserve_exit
        self.addCleanup(lambda: setattr(memecoin_desk, "reserve_exit", original))

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
        self.assertEqual(result["jupiter_order"]["reason"], "estimate_unavailable")
        self.assertFalse(result["jupiter_order"]["transaction_present"])
        self.assertFalse(result["jupiter_order"]["min_out_proven"])
        self.assertFalse(result["jupiter_order"]["execution_allowed"])
        self.assertEqual(result["jupiter_build"]["reason"], "estimate_unavailable")
        self.assertFalse(result["jupiter_build"]["min_out_proven"])
        self.assertFalse(result["jupiter_build"]["buy_proven"])
        self.assertFalse(result["jupiter_build"]["exit_proven"])
        self.assertFalse(result["jupiter_build"]["execution_allowed"])
        self.assertFalse(result["send"])
        self.assertEqual(seen.count("http"), 2)
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
        pool = solana_whirlpool.reference_pool(
            solana_tx.NATIVE_MINT, solana_tx.b58encode(b"\x11" * 32),
            solana_tx.USDC_MINT, solana_tx.b58encode(b"\x12" * 32),
            sqrt_price=5531210526019943285,
        )
        quote = {"usable": True, "upper": "1", "lower": "1", "origin": "test_harness"}
        self.assertEqual(memecoin_desk.protect(opened["case_id"], pool, quote)["stage"], "protected")
        blocked = memecoin_desk.record_jupiter_block(opened["case_id"], shown)
        self.assertEqual(blocked["reason"], "jupiter_instruction_unproven")
        self.assertEqual(memecoin_desk.view(opened["case_id"])["stage"], "protected")
        self.assertFalse(memecoin_desk.view(opened["case_id"])["snapshot"]["send"])
        closed = token_execution.supported_buy(MINT)
        self.assertEqual(closed["jupiter_route_v2"], "message_required")
        self.assertEqual(closed["decoder"], "jupiter_route_v2_whirlpool")
        self.assertEqual(closed["jupiter_swap_v2"], "refused")
        self.assertFalse(closed["mobile_phantom_is_server_auth"])
        exited = token_execution.supported_exit(MINT)
        self.assertEqual(exited["jupiter_swap_v2"], "refused")
        self.assertFalse(exited["loss_stop_blocks_exit"])

    def test_missing_policy_and_caller_slippage_make_no_http_call(self):
        seen = []

        def transport(request):
            seen.append(request)
            return {"status": 200, "body": {}}

        values, accounts = __import__("tests.test_jupiter_route_v2", fromlist=["_pool"])._pool(solana_tx.NATIVE_MINT, MINT)
        db.execute("DELETE FROM settings WHERE key = ?", ( __import__("owner_slippage").KEY,))
        db.init_db()
        self.assertIsNone(db.get_setting(__import__("owner_slippage").KEY, None))
        missing = public_market.run_bounded(
            transport, NOW, pool_accounts=accounts, pool=values["pool"], amount=1_000_000,
            input_mint=solana_tx.NATIVE_MINT, output_mint=MINT,
        )
        self.assertEqual(missing["reason"], "owner_slippage_unapproved")
        self.assertEqual(missing["calls"], [])
        self.assertEqual(seen, [])
        self.assertFalse(missing["instruction_matches_request"])
        db.init_db()
        for override in ({"slippage_bps": 51}, {"slippage_bps": 100}, {"slippage_bps": 50}, {"slippage_allowance_bps": 50}):
            seen.clear()
            refused = public_market.run_bounded(
                transport, NOW, pool_accounts=accounts, pool=values["pool"], amount=1_000_000,
                input_mint=solana_tx.NATIVE_MINT, output_mint=MINT, **override,
            )
            self.assertEqual(refused["reason"], "slippage_override", override)
            self.assertEqual(seen, [])
            self.assertEqual(refused["calls"], [])
        with self.assertRaises(TypeError):
            jupiter_swap_v2.build_url(solana_tx.NATIVE_MINT, MINT, "1000000")
        with self.assertRaises(ValueError):
            jupiter_swap_v2.build_url(solana_tx.NATIVE_MINT, MINT, "1000000", slippage_bps=51)
        with self.assertRaises(ValueError):
            jupiter_swap_v2.build_url(solana_tx.NATIVE_MINT, MINT, "1000000", slippage_bps=100)

    def test_the_service_requests_the_measured_bps_and_keeps_the_provider_bytes(self):
        import base64
        import json as _json
        import urllib.request
        from urllib.parse import parse_qs, urlparse
        from tests.test_jupiter_route_v2 import PRICE_SLOT, WHEN, _metas, _pool, _prices, _route, _snap, _tx
        import desk_loop
        import jupiter_route_v2
        import phantom_transport
        wallet = phantom_transport.REQUIRED_WALLET
        swap_mint = solana_tx.b58encode(bytes([92]) * 32)
        rent = solana_tx.TOKEN_ACCOUNT_RENT
        buy_values, buy_accounts = _pool(solana_tx.NATIVE_MINT, swap_mint)
        exit_values, exit_accounts = _pool(swap_mint, solana_tx.NATIVE_MINT)
        seen = []
        provided = []
        queue = []

        def transport(request):
            if request.get("kind") == "rpc":
                seen.append(request["method"])
                return {"status": 200, "body": {"result": {"value": _mint_account(0)}}}
            url = request["url"]
            seen.append(url)
            if url.endswith("/token-boosts/latest/v1"):
                return {"status": 200, "body": [{"chainId": "solana", "tokenAddress": MINT}]}
            if "/token-pairs/" in url:
                return {"status": 200, "body": _pair()}
            if "/swap/v2/order" in url:
                return {"status": 200, "body": {"router": "metis", "inAmount": "1", "outAmount": "1", "feeBps": 0, "transaction": None}}
            query = parse_qs(urlparse(url).query)
            amount = int(query["amount"][0])
            declared = int(query["slippageBps"][0])
            incoming = query["inputMint"][0]
            pool_values, pool_accounts = (buy_values, buy_accounts) if incoming == solana_tx.NATIVE_MINT else (exit_values, exit_accounts)
            measured = jupiter_route_v2.measure_pool(pool_accounts, pool_values["pool"], amount, incoming)
            data = _route(measured["est_out"], declared, True, amount=amount)
            provided.append(data)
            return {"status": 200, "body": {
                "otherAmountThreshold": str(measured["est_out"]),
                "routePlan": [],
                "swapInstruction": {
                    "programId": solana_jupiter.PROGRAM_ID,
                    "accounts": [],
                    "data": base64.b64encode(data).decode("ascii"),
                },
            }}

        def ask(accounts, pool, amount, input_mint, output_mint):
            return public_market.run_bounded(
                transport, NOW, pool_accounts=accounts, pool=pool, amount=amount,
                input_mint=input_mint, output_mint=output_mint,
            )

        tiny = ask(buy_accounts, buy_values["pool"], 3, solana_tx.NATIVE_MINT, swap_mint)
        divisible = ask(buy_accounts, buy_values["pool"], 202, solana_tx.NATIVE_MINT, swap_mint)
        bought = ask(buy_accounts, buy_values["pool"], 1_000_000, solana_tx.NATIVE_MINT, swap_mint)
        builds = [url for url in seen if isinstance(url, str) and "/swap/v2/build?" in url]
        self.assertTrue(any("amount=3&" in url and "slippageBps=0" in url for url in builds), builds)
        self.assertTrue(any("amount=202&" in url and "slippageBps=50" in url for url in builds), builds)
        self.assertTrue(any("amount=1000000&" in url and "slippageBps=49" in url for url in builds), builds)
        self.assertFalse(any("amount=1000000&" in url and "slippageBps=50" in url for url in builds))
        self.assertFalse(any("slippageBps=51" in url or "slippageBps=100" in url for url in builds))
        self.assertEqual(bought["measured_est_out"], 999_899)
        self.assertEqual(bought["requested_slippage_bps"], 49)
        self.assertTrue(bought["instruction_matches_request"], bought["jupiter_build"])
        self.assertFalse(bought["rewritten"])
        self.assertFalse(tiny["send"])
        self.assertFalse(divisible["live_ready"])
        raw = base64.b64decode(bought["instruction_data_b64"])
        self.assertEqual(raw, provided[-1])
        self.assertEqual(__import__("hashlib").sha256(raw).hexdigest(), bought["instruction_sha256"])
        self.assertIn("lp_fields_unpublished", desk_loop.scheduler_status()["ready_because"])
        self.assertFalse(desk_loop.scheduler_status()["armed"])

        opened = memecoin_desk.scout({
            "mint": swap_mint, "source": "dexscreener", "fetched_at": NOW,
            "pair_created_at": int((NOW - 3600) * 1000), "market_cap_usd": "1000000",
            "liquidity_usd": "250000", "price_usd": "1", "price_change_h1": "10",
        }, now=NOW)
        onchain = {
            "source": "stalkchain_token_onchain", "fetched_at": NOW,
            "mint_authority_revoked": True, "freeze_authority_revoked": True, "lp_burned_or_locked": True,
            "dev_holding_pct": "1",
            "dev_rugs": {"status": "checked", "source": "stalkchain_token_deployer", "rugs": 0, "tokens_seen": 1},
            "bundled_launch_pct": "1",
        }
        self.assertTrue(memecoin_desk.filter_case(opened["case_id"], onchain, now=NOW)["ok"])
        components = {name: {"value": "70", "source": "holder_reader", "fetched_at": NOW} for name in memecoin_desk.COMPONENTS}
        self.assertTrue(memecoin_desk.analyze(opened["case_id"], components, now=NOW)["ok"])
        pool = solana_whirlpool.reference_pool(
            solana_tx.NATIVE_MINT, solana_tx.b58encode(b"\x11" * 32),
            solana_tx.USDC_MINT, solana_tx.b58encode(b"\x12" * 32),
            sqrt_price=5531210526019943285,
        )
        case_id = opened["case_id"]
        self.assertEqual(memecoin_desk.protect(case_id, pool, {"usable": True, "upper": "1", "lower": "1", "origin": "test_harness"})["stage"], "protected")

        def urlopen(req, timeout=8):
            payload = _json.loads(req.data.decode())
            body = {"jsonrpc": "2.0", "result": __import__("solana_inspect").MAINNET_GENESIS if payload["method"] == "getGenesisHash" else queue.pop(0)}

            class _Response:
                status = 200
                def read(self):
                    return _json.dumps(body).encode()
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    return False
            return _Response()

        original = urllib.request.urlopen
        urllib.request.urlopen = urlopen
        self.addCleanup(lambda: setattr(urllib.request, "urlopen", original))
        ata = solana_tx.associated_token_address(wallet, swap_mint, solana_tx.TOKEN_PROGRAM)
        owned = buy_accounts.pop(buy_values["dest"])
        buy_values["dest"] = ata
        buy_accounts[ata] = owned
        signed_buy = _tx(_metas(solana_tx.NATIVE_MINT, swap_mint, buy_values), raw, side="buy", fund_mint=swap_mint)
        buy_raw = bytearray(base64.b64decode(signed_buy))
        buy_raw[1:65] = b"\x21" + b"\x61" * 63
        signed_buy = base64.b64encode(bytes(buy_raw)).decode()
        reserved = memecoin_desk.reserve_entry(case_id, signed_buy, _snap(buy_accounts, WHEN), _prices(), WHEN)
        self.assertTrue(reserved["ok"], reserved)
        self.assertEqual(reserved["instruction_slippage_bps"], 49)
        self.assertEqual(reserved["quoted_out_amount"], 999_899)
        self.assertFalse(reserved["effectual_entry"])
        self.assertIn("quote_expiry_unproven", reserved["effectual_blocked_by"])
        self.assertFalse(reserved["send"])
        decoded = solana_tx.decode_transaction(bytes(buy_raw))
        pre = [0] * len(decoded.static_keys)
        post = [0] * len(decoded.static_keys)
        pre[0] = 200_000_000
        post[0] = 200_000_000 - (1_000_000 + 5_000 + rent)
        post[decoded.static_keys.index(ata)] = rent
        signature = solana_tx.first_signature(signed_buy)
        queue.append({
            "slot": PRICE_SLOT, "blockTime": int(WHEN), "transaction": [signed_buy, "base64"],
            "meta": {"err": None, "fee": 5000, "preBalances": pre, "postBalances": post, "preTokenBalances": [],
                     "postTokenBalances": [{"mint": swap_mint, "owner": wallet, "uiTokenAmount": {"amount": "999899"}}]},
        })
        booked = memecoin_desk.confirm_entry(case_id, signature, WHEN)
        self.assertTrue(booked["ok"], booked)
        self.assertEqual(booked["qty"], 999_899)
        self.assertFalse(booked["quote_expiry_proven"])

        def exit_message(qty, route_bytes, nonce):
            values = dict(exit_values)
            accounts = dict(exit_accounts)
            held = accounts.pop(values["source"])
            values["source"] = ata
            accounts[ata] = held
            unsigned = _tx(_metas(swap_mint, solana_tx.NATIVE_MINT, values), route_bytes, side="exit")
            raw_tx = bytearray(base64.b64decode(unsigned))
            raw_tx[1:65] = bytes([nonce]) + b"\x71" * 63
            signed = base64.b64encode(bytes(raw_tx)).decode()
            return signed, accounts

        loose_est = jupiter_route_v2.measure_pool(exit_accounts, exit_values["pool"], 400_000, swap_mint)["est_out"]
        loose_signed, loose_accounts = exit_message(400_000, _route(loose_est, 50, True, amount=400_000), 31)
        self.assertEqual(memecoin_desk.reserve_exit(case_id, loose_signed, 400_000, _snap(loose_accounts, WHEN), _prices(), WHEN)["reason"], "min_below_amm")
        first = ask(exit_accounts, exit_values["pool"], 400_000, swap_mint, solana_tx.NATIVE_MINT)
        self.assertTrue(any("amount=400000&" in url and "slippageBps=49" in url for url in seen if isinstance(url, str)), seen)
        self.assertTrue(first["instruction_matches_request"], first["jupiter_build"])
        first_bytes = base64.b64decode(first["instruction_data_b64"])
        self.assertEqual(first_bytes, provided[-1])
        first_signed, first_accounts = exit_message(400_000, first_bytes, 32)
        first_reserved = memecoin_desk.reserve_exit(case_id, first_signed, 400_000, _snap(first_accounts, WHEN), _prices(), WHEN)
        self.assertTrue(first_reserved["ok"], first_reserved)
        self.assertEqual(first_reserved["instruction_slippage_bps"], 49)
        self.assertFalse(first_reserved["send"])
        exit_decoded = solana_tx.decode_transaction(base64.b64decode(first_signed))
        pre = [0] * len(exit_decoded.static_keys)
        post = [0] * len(exit_decoded.static_keys)
        pre[0] = 5_000_000
        post[0] = 5_000_000 + first["measured_est_out"]
        pre[exit_decoded.static_keys.index(ata)] = rent
        post[exit_decoded.static_keys.index(ata)] = rent
        queue.append({
            "slot": PRICE_SLOT, "transaction": [first_signed, "base64"],
            "meta": {"err": None, "fee": 5000, "preBalances": pre, "postBalances": post,
                     "preTokenBalances": [{"mint": swap_mint, "owner": wallet, "uiTokenAmount": {"amount": "999899"}}],
                     "postTokenBalances": [{"mint": swap_mint, "owner": wallet, "uiTokenAmount": {"amount": "599899"}}]},
        })
        closed = memecoin_desk.confirm_exit(case_id, solana_tx.first_signature(first_signed), WHEN)
        self.assertTrue(closed["ok"], closed)
        retry = ask(exit_accounts, exit_values["pool"], 599_899, swap_mint, solana_tx.NATIVE_MINT)
        self.assertTrue(any("amount=599899&" in url and "slippageBps=49" in url for url in seen if isinstance(url, str)))
        self.assertTrue(retry["instruction_matches_request"], retry)
        retry_bytes = base64.b64decode(retry["instruction_data_b64"])
        self.assertEqual(retry_bytes, provided[-1])
        retry_signed, retry_accounts = exit_message(599_899, retry_bytes, 33)
        retry_reserved = memecoin_desk.reserve_exit(case_id, retry_signed, 599_899, _snap(retry_accounts, WHEN), _prices(), WHEN)
        self.assertTrue(retry_reserved["ok"], retry_reserved)
        self.assertEqual(retry_reserved["instruction_slippage_bps"], 49)
        retry_decoded = solana_tx.decode_transaction(base64.b64decode(retry_signed))
        pre = [0] * len(retry_decoded.static_keys)
        post = [0] * len(retry_decoded.static_keys)
        pre[0] = 5_000_000
        post[0] = 5_000_000 + retry["measured_est_out"]
        pre[retry_decoded.static_keys.index(ata)] = rent
        post[retry_decoded.static_keys.index(ata)] = rent
        queue.append({
            "slot": PRICE_SLOT, "transaction": [retry_signed, "base64"],
            "meta": {"err": None, "fee": 5000, "preBalances": pre, "postBalances": post,
                     "preTokenBalances": [{"mint": swap_mint, "owner": wallet, "uiTokenAmount": {"amount": "599899"}}],
                     "postTokenBalances": [{"mint": swap_mint, "owner": wallet, "uiTokenAmount": {"amount": "0"}}]},
        })
        finished = memecoin_desk.confirm_exit(case_id, solana_tx.first_signature(retry_signed), WHEN)
        self.assertTrue(finished["ok"], finished)
        self.assertFalse(desk_loop.readiness()["live_ready"])
        self.assertFalse(desk_loop.readiness()["send"])
        self.assertIn("stalkchain_response_fields_for_lp_rug_growth_are_unpublished", desk_loop.readiness()["blocked_because"])
