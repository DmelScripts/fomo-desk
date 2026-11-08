"""The six-role handoff fetches. Caller labels are not a source. Nothing is armed."""
import os
import unittest
from pathlib import Path

import desk_loop
import memecoin_desk
import solana_tx
import usdc_valuation
from tests.support import temp_desk_env

NOW = 1_800_000_000.0
MINT = solana_tx.b58encode(b"\x55" * 32)


def _pair():
    return [{
        "chainId": "solana",
        "dexId": "raydium",
        "pairAddress": solana_tx.b58encode(b"\x66" * 32),
        "baseToken": {"address": MINT},
        "priceUsd": "1.25",
        "marketCap": 1_000_000,
        "fdv": 5_000_000,
        "liquidity": {"usd": 250_000},
        "pairCreatedAt": int((NOW - 3600) * 1000),
        "priceChange": {"h1": 10},
    }]


class DeskLoopTests(unittest.TestCase):
    def setUp(self):
        _tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        os.environ.pop("STALKCHAIN_API_KEY", None)
        import db
        db.init_db()
        db.execute("UPDATE phantom_control SET value = '0' WHERE key = 'halt'")

    def test_a_missing_stalkchain_key_stops_the_handoff(self):
        def transport(request):
            if request["kind"] == "http":
                return {"status": 200, "body": _pair()}
            raw = bytearray(82)
            raw[44] = 6
            raw[45] = 1
            import base64
            return {"status": 200, "body": {"result": {"value": {"owner": solana_tx.TOKEN_PROGRAM, "data": [base64.b64encode(bytes(raw)).decode("ascii"), "base64"]}}}}

        result = desk_loop.run_once(MINT, transport, NOW)
        self.assertEqual(result["reason"], "lp")
        self.assertEqual(result["stage"], "rejected")
        self.assertFalse(result["stalkchain"]["called"])
        self.assertIn("dev_rugs", result["stalkchain"]["unproven"])
        self.assertIn("holder_growth", result["stalkchain"]["unproven"])
        self.assertIn("lp_lock_expiry", result["stalkchain"]["unproven"])
        self.assertFalse(result["score_measured"])
        self.assertFalse(result["armed"])
        self.assertFalse(result["live_ready"])
        self.assertFalse(result["usd_live"])
        self.assertEqual(usdc_valuation.keyless_catalog_is_not_a_price()["reason"], "metadata_not_a_price")
        self.assertTrue(memecoin_desk.log_for(result["case_id"]))

    def test_without_a_transport_nothing_is_fetched(self):
        result = desk_loop.run_once(MINT, None, NOW)
        self.assertEqual(result["reason"], "network_disabled")
        self.assertFalse(result["armed"])
        self.assertFalse(result["scheduled"] if "scheduled" in result else desk_loop.readiness()["run_once_scheduled"])

    def test_a_present_key_is_sent_on_the_call_and_absent_from_the_result(self):
        os.environ["STALKCHAIN_API_KEY"] = "sc_test_not_a_real_key"
        self.addCleanup(lambda: os.environ.pop("STALKCHAIN_API_KEY", None))
        seen = []

        def transport(request):
            seen.append(request["url"])
            self.assertTrue(request["headers"]["authorization"].endswith("sc_test_not_a_real_key"))
            self.assertEqual(request["json"], {"token": MINT})
            return {"status": 200, "body": {"data": {"marketCapBasis": "market_cap", "fdvUsd": 9}, "meta": {}}}

        result = __import__("stalkchain_adapter").read_mint(MINT, transport)
        self.assertEqual(len(seen), 4)
        self.assertNotIn("sc_test_not_a_real_key", __import__("json").dumps(result))
        self.assertEqual(result["reason"], "response_fields_unpublished")
        self.assertEqual(result["named"]["marketCapBasis"], "market_cap")
        self.assertFalse(result["fdv_used_as_market_cap"])
        self.assertIn("lp_lock_expiry", result["unproven"])
        self.assertIn("holder_growth", result["unproven"])
        self.assertFalse(result["score_measured"])
        self.assertEqual(result["render_presence"], "unknown")
        import pyth_sponsored
        pyth_sponsored._LAST = None
        view = desk_loop.readiness()
        self.assertTrue(view["stalkchain_env_present"])
        self.assertNotIn("sc_test_not_a_real_key", __import__("json").dumps(view))
        self.assertFalse(view["run_once_scheduled"])
        self.assertFalse(view["deployed"])
        self.assertEqual(view["render_presence"], "unknown")
        self.assertEqual(view["onchain_pyth"], "not_read")
        self.assertTrue(view["onchain_pyth_configured"])
        self.assertFalse(view["onchain_pyth_valid"])
        self.assertFalse(view["onchain_pyth_fresh"])
        self.assertFalse(view["onchain_pyth_live"])
        self.assertIsNone(view["onchain_pyth_age_s"])
        self.assertEqual(view["onchain_pyth_account"], "7UVimffxr9ow1uXYxsr4LHAcV58mLzhmwaeKvJ1pjLiE")
        self.assertEqual(view["onchain_pyth_valuation"], "sol_usd")
        self.assertFalse(view["onchain_pyth_usdc_used"])
        self.assertFalse(view["onchain_pyth_usable"])
        source = Path("server.py").read_text(encoding="utf-8")
        self.assertIn("desk_loop.scheduler_status()", source)
        self.assertIn('if gate["armed"]', source)
        self.assertIn("def _memecoin_screen", source)
        self.assertFalse(desk_loop.scheduler_status()["armed"])
        self.assertFalse(desk_loop.scheduler_status()["started"])

    def test_discovery_names_the_mint_and_records_only_its_own_series(self):
        _tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        import db
        db.init_db()

        def transport(request):
            if request.get("url", "").endswith("/token-boosts/latest/v1"):
                return {"status": 200, "body": [{"chainId": "solana", "tokenAddress": MINT}]}
            if request.get("kind") == "rpc":
                raw = bytearray(82)
                raw[44] = 6
                raw[45] = 1
                return {"status": 200, "body": {"result": {"value": {"owner": solana_tx.TOKEN_PROGRAM, "data": [__import__("base64").b64encode(bytes(raw)).decode(), "base64"]}}}}
            return {"status": 200, "body": _pair()}

        found = desk_loop.run_discovery(transport, NOW)
        self.assertEqual(found["candidates"], 1)
        self.assertEqual(found["kills"][0]["mint"], MINT)
        self.assertEqual(found["kills"][0]["reason"], "lp")
        self.assertEqual(found["kills"][0]["source"], "dexscreener")
        self.assertFalse(found["caller_named_mint"])
        self.assertFalse(found["armed"])
        self.assertFalse(found["score_measured"])
        self.assertTrue(found["holder_growth_unproven"])
        refused = desk_loop.collect_holder_count(MINT, {"source": "stalkchain", "holder_count": 12, "fetched_at": NOW})
        self.assertFalse(refused["written"])
        self.assertEqual(refused["reason"], "holder_count_source_unpublished")
        self.assertEqual(desk_loop.holder_growth(MINT)["reason"], "holder_growth_unproven")
        bare = [{key: value for key, value in _pair()[0].items() if key != "marketCap"}]
        missing = desk_loop.run_once(MINT, lambda request: {"status": 200, "body": bare}, NOW)
        self.assertEqual(missing["reason"], "market_cap_missing")
        self.assertEqual(missing["source"], "dexscreener")

    def test_every_solana_boost_is_a_candidate_and_the_loop_can_be_stopped(self):
        other = solana_tx.b58encode(b"\x44" * 32)

        def transport(request):
            if str(request.get("url", "")).endswith("/token-boosts/latest/v1"):
                return {"status": 200, "body": [
                    {"chainId": "ethereum", "tokenAddress": "0xabc"},
                    {"chainId": "solana", "tokenAddress": MINT},
                    {"chainId": "solana", "tokenAddress": other},
                ]}
            if request.get("kind") == "rpc":
                raw = bytearray(82)
                raw[44] = 6
                raw[45] = 1
                return {"status": 200, "body": {"result": {"value": {"owner": solana_tx.TOKEN_PROGRAM, "data": [__import__("base64").b64encode(bytes(raw)).decode(), "base64"]}}}}
            mint = str(request.get("url", "")).rsplit("/", 1)[-1]
            body = _pair()
            body[0]["baseToken"] = {"address": mint}
            return {"status": 200, "body": body}

        found = desk_loop.run_discovery(transport, NOW)
        self.assertEqual(found["candidates"], 2)
        self.assertEqual({item.get("reason") for item in found["kills"] if item.get("mint")}, {"lp"})
        self.assertIn("not_solana", {item.get("reason") for item in found["kills"]})
        status = desk_loop.scheduler_status()
        self.assertFalse(status["authorized"])
        self.assertFalse(status["ready"])
        self.assertFalse(status["armed"])
        self.assertNotIn("owner_slippage_unapproved", status["ready_because"])
        self.assertIn("lp_fields_unpublished", status["ready_because"])
        self.assertIn("holder_growth_unproven", status["ready_because"])
        once = desk_loop.run_supervised(transport, NOW, rounds=1)
        self.assertTrue(once["ok"], once)
        self.assertFalse(desk_loop.scheduler_status()["started"])
        again = desk_loop.run_supervised(transport, NOW, rounds=1)
        self.assertTrue(again["ok"], again)

        original = desk_loop.run_discovery

        def explode(*args, **kwargs):
            raise RuntimeError("reader down")

        desk_loop.run_discovery = explode
        try:
            fault = desk_loop.run_supervised(transport, NOW, rounds=1)
        finally:
            desk_loop.run_discovery = original
        self.assertEqual(fault["reason"], "discovery_fault")
        self.assertFalse(desk_loop.scheduler_status()["started"])
        self.assertFalse(desk_loop.scheduler_status()["running"])

        def reenter(request):
            reenter.nested = desk_loop.run_supervised(transport, NOW, rounds=1)
            raise RuntimeError("stop after nested")

        nested = desk_loop.run_supervised(reenter, NOW, rounds=1)
        self.assertEqual(reenter.nested["reason"], "already_running")
        self.assertFalse(desk_loop.scheduler_status()["started"])
        self.assertFalse(desk_loop.scheduler_status()["running"])
        self.assertTrue(nested["ok"], nested)

    def test_supervised_round_quotes_through_the_official_reader(self):
        import asyncio
        import base64
        import json
        import os
        import urllib.request
        from urllib.parse import parse_qs, urlparse
        from tests.test_jupiter_route_v2 import WHEN, _pool, _prices, _route
        import db
        import jupiter_route_v2
        import memecoin_desk
        import solana_whirlpool_quote
        import stalkchain_adapter
        import solana_whirlpool
        from server import app, lifespan
        swap_mint = solana_tx.b58encode(bytes([92]) * 32)
        other = solana_tx.b58encode(bytes([93]) * 32)
        values, accounts = _pool(solana_tx.NATIVE_MINT, swap_mint)
        old_pool = values["pool"]
        raw_pool = bytearray(accounts[old_pool][1])
        config = b"\x44" * 32
        raw_pool[8:40] = config
        fields = solana_whirlpool.pool_fields(bytes(raw_pool))
        seed = bytes(raw_pool[solana_whirlpool._TICK_SPACING + 2:solana_whirlpool._TICK_SPACING + 4])
        derived, bump = solana_tx.find_program_address(
            [b"whirlpool", config, solana_tx.b58decode(fields[0]), solana_tx.b58decode(fields[2]), seed],
            solana_tx.b58decode(solana_whirlpool.PROGRAM_ID),
        )
        raw_pool[40] = bump
        new_pool = solana_tx.b58encode(derived)
        accounts.pop(old_pool)
        accounts[new_pool] = (solana_whirlpool.PROGRAM_ID, bytes(raw_pool))
        for key in list(values["ticks"]):
            accounts.pop(key, None)
        state = solana_whirlpool.pool_state(bytes(raw_pool))
        base = solana_whirlpool.start_tick_index(state["tick_current_index"], state["tick_spacing"])
        step = solana_whirlpool.TICK_ARRAY_SIZE * state["tick_spacing"]
        ticks = []
        for start in [base - i * step for i in range(3)] + [base + i * step for i in range(3)]:
            address = solana_whirlpool.tick_array_address(new_pool, start)
            accounts[address] = (solana_whirlpool.PROGRAM_ID, solana_whirlpool.reference_tick_array(new_pool, start))
            ticks.append(address)
        values["ticks"] = ticks[:3]
        values["pool"] = new_pool
        for vault, mint in ((fields[1], fields[0]), (fields[3], fields[2])):
            token = bytearray(solana_whirlpool.reference_token_account(mint, new_pool))
            token[108] = 1
            accounts[vault] = (solana_tx.TOKEN_PROGRAM, bytes(token))
        accounts[solana_whirlpool.oracle_address(new_pool)] = (
            solana_whirlpool.PROGRAM_ID, solana_whirlpool.oracle_account(new_pool),
        )
        accounts.pop(solana_tx.associated_token_address(
            __import__("phantom_transport").REQUIRED_WALLET, solana_tx.NATIVE_MINT, solana_tx.TOKEN_PROGRAM,
        ), None)
        slot = {"context": 2_000_000, "tip": 2_000_028, "owner": None, "price_scale": 1, "pyth": True}
        provided = {}
        import pyth_sponsored
        from pathlib import Path

        def urlopen(req, timeout=8):
            url = req.full_url
            seen.append(url)
            if url.endswith("/token-boosts/latest/v1"):
                body = [
                    {"chainId": "solana", "tokenAddress": swap_mint},
                    {"chainId": "solana", "tokenAddress": other},
                ]
            elif "/token-pairs/" in url:
                mint = url.rsplit("/", 1)[-1]
                pair = _pair()
                pair[0]["baseToken"] = {"address": mint}
                pair[0]["pairCreatedAt"] = int((moment - 3600) * 1000)
                if mint == swap_mint:
                    pair[0]["dexId"] = "whirlpool"
                    pair[0]["pairAddress"] = values["pool"]
                    if slot.get("mark"):
                        pair[0]["priceUsd"] = slot["mark"]
                body = pair
            elif url.startswith("https://api.jup.ag/swap/v2/build"):
                query = parse_qs(urlparse(url).query)
                amount = int(query["amount"][0])
                declared = int(query["slippageBps"][0])
                output = query["outputMint"][0]
                a_to_b = output == swap_mint
                starts = [base - i * step for i in range(3)] if a_to_b else [base + i * step for i in range(3)]
                arrays = []
                for start in starts:
                    parsed_tick = solana_whirlpool.tick_array_state(accounts[solana_whirlpool.tick_array_address(values["pool"], start)][1])
                    arrays.append({"start_tick_index": parsed_tick["start_tick_index"], "ticks": parsed_tick["ticks"]})
                estimate = solana_whirlpool_quote.quote_exact_in(state, arrays, amount, a_to_b, 0, 0, slot["context"])["token_est_out"]
                data = _route(estimate, declared, True, amount=amount)
                provided["data"] = data
                body = {"otherAmountThreshold": str(estimate), "routePlan": [], "swapInstruction": {
                    "programId": "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
                    "accounts": [],
                    "data": base64.b64encode(data).decode("ascii"),
                }}
            elif url.startswith("https://api.jup.ag/swap/v2/order"):
                body = {"router": "metis", "inAmount": "1000000", "outAmount": "999899", "transaction": None}
            else:
                payload = json.loads(req.data.decode())
                method = payload["method"]
                seen.append(method)
                if method == "getGenesisHash":
                    body = {"jsonrpc": "2.0", "result": "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"}
                elif method == "getMinimumBalanceForRentExemption":
                    body = {"jsonrpc": "2.0", "result": solana_tx.TOKEN_ACCOUNT_RENT}
                elif method == "getSlot":
                    body = {"jsonrpc": "2.0", "result": slot["tip"]}
                elif method == "getMultipleAccounts":
                    keys = payload["params"][0]
                    if keys == [pyth_sponsored.SOL_ACCOUNT, pyth_sponsored.USDC_ACCOUNT]:
                        if slot["pyth"] is not True:
                            body = {"jsonrpc": "2.0", "error": {"code": -1, "message": "pyth"}}
                        else:
                            sol_raw = bytearray(Path("tests/fixtures/pyth_sol_usd_price_update_v2.bin").read_bytes())
                            price = int.from_bytes(sol_raw[73:81], "little", signed=True) * slot["price_scale"]
                            sol_raw[73:81] = int(price).to_bytes(8, "little", signed=True)
                            usdc_raw = Path("tests/fixtures/pyth_usdc_usd_price_update_v2.bin").read_bytes()
                            body = {"jsonrpc": "2.0", "result": {"context": {"slot": 466835746}, "value": [
                                {"owner": pyth_sponsored.RECEIVER_PROGRAM_ID, "data": [base64.b64encode(bytes(sol_raw)).decode(), "base64"]},
                                {"owner": pyth_sponsored.RECEIVER_PROGRAM_ID, "data": [base64.b64encode(usdc_raw).decode(), "base64"]},
                            ]}}
                    else:
                        value = []
                        for key in keys:
                            if key not in accounts:
                                value.append(None)
                                continue
                            owner, raw = accounts[key]
                            if key == values["pool"] and slot["owner"] is not None:
                                owner = slot["owner"]
                            value.append({"owner": owner, "data": [base64.b64encode(raw).decode("ascii"), "base64"]})
                        body = {"jsonrpc": "2.0", "result": {"context": {"slot": slot["context"]}, "value": value}}
                elif method == "getAccountInfo":
                    raw = bytearray(82)
                    raw[44] = 6
                    raw[45] = 1
                    body = {"jsonrpc": "2.0", "result": {"context": {"slot": slot["context"]}, "value": {"owner": solana_tx.TOKEN_PROGRAM, "data": [base64.b64encode(bytes(raw)).decode("ascii"), "base64"]}}}
                else:
                    raise AssertionError(method)
            class _Response:
                status = 200
                def read(self):
                    return json.dumps(body).encode()
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    return False
            return _Response()

        seen = []
        original = urllib.request.urlopen
        urllib.request.urlopen = urlopen
        self.addCleanup(lambda: setattr(urllib.request, "urlopen", original))
        os.environ["DESK_PRICE_LOOP"] = "0"
        self.addCleanup(lambda: os.environ.pop("DESK_PRICE_LOOP", None))
        moment = WHEN + 1

        def features():
            return {
                "mint": swap_mint,
                "source": "synthetic_test_adapter",
                "fetched_at": moment,
                "onchain": {
                    "source": "synthetic_test_adapter", "fetched_at": moment,
                    "mint_authority_revoked": True, "freeze_authority_revoked": True, "lp_burned_or_locked": True,
                    "dev_holding_pct": "1",
                    "dev_rugs": {"status": "checked", "source": "synthetic_test_adapter", "rugs": 0, "tokens_seen": 1},
                    "bundled_launch_pct": "1",
                },
                "components": {name: {"value": "70", "source": "synthetic_test_adapter", "fetched_at": moment} for name in memecoin_desk.COMPONENTS},
            }

        db.set_setting("memecoin_scheduler_authorized", "1", "synthetic_test_consent")

        async def boot():
            async with lifespan(app):
                status = desk_loop.scheduler_status()
                self.assertTrue(status["authorized"])
                self.assertFalse(status["ready"])
                self.assertFalse(status["armed"])
                self.assertIn("lp_fields_unpublished", status["ready_because"])
                self.assertIn("holder_growth_unproven", status["ready_because"])
                screened = desk_loop.synthetic_screen(swap_mint, features(), desk_loop.public_reader, moment)
                real = desk_loop.run_supervised(desk_loop.public_reader, moment, rounds=1)
                return screened, real

        screened, found = asyncio.run(boot())
        self.assertFalse(desk_loop.scheduler_status()["armed"])
        self.assertFalse(found["live_ready"])
        self.assertFalse(screened["live_capital"], screened)
        self.assertEqual(screened.get("reason"), None, screened)
        self.assertEqual(memecoin_desk.view(swap_mint)["snapshot"]["capital_ledger"], "synthetic")
        found = found["discovery"]
        builds = [url for url in seen if "/swap/v2/build?" in url]
        self.assertEqual(len(builds), 1, seen)
        quoted = screened["quote"]
        raw_amount = memecoin_desk.view(swap_mint)["snapshot"]["entry_amount_raw"]
        chosen = jupiter_route_v2.encoded_slippage(quoted["measured_est_out"])
        self.assertIn("amount=%s" % raw_amount, builds[0])
        self.assertIn("slippageBps=%s" % chosen["slippage_bps"], builds[0])
        self.assertLessEqual(chosen["slippage_bps"], 50)
        self.assertNotIn(other, "".join(builds))
        self.assertEqual(quoted["requested_slippage_bps"], chosen["slippage_bps"])
        self.assertLessEqual(__import__("decimal").Decimal(memecoin_desk.view(swap_mint)["snapshot"]["entry_total_usd"]), __import__("decimal").Decimal("8.00"))
        self.assertFalse(memecoin_desk.view(swap_mint)["snapshot"]["sources_are_live_mcp"])
        self.assertEqual(memecoin_desk.set_entry_amount(swap_mint, raw_amount + 1)["reason"], "amount_locked")
        self.assertTrue(quoted["instruction_matches_request"], quoted)
        self.assertFalse(quoted["rewritten"])
        self.assertFalse(quoted["quote_expiry_proven"])
        self.assertTrue(quoted["block_height_missing"])
        self.assertEqual(quoted["context_slot"], slot["context"])
        self.assertNotEqual(quoted["context_slot"], 1)
        self.assertEqual(__import__("hashlib").sha256(provided["data"]).hexdigest(), quoted["instruction_sha256"])
        unknown = next(item for item in found["kills"] if item.get("mint") == other)
        self.assertEqual(unknown["reason"], "lp")
        self.assertFalse(unknown["jupiter_called"])
        self.assertFalse(unknown["quote"]["score_measured"])
        self.assertEqual(memecoin_desk.view(swap_mint)["stage"], "protected")
        self.assertEqual(memecoin_desk.view(swap_mint)["snapshot"]["entry_amount_raw"], raw_amount)
        self.assertNotEqual(db.get_setting("memecoin_entry_halt", ""), "1")
        self.assertIn("stalkchain_response_fields_for_lp_rug_growth_are_unpublished", desk_loop.readiness()["blocked_because"])
        self.assertIn("lp_fields_unpublished", desk_loop.scheduler_status()["ready_because"])
        self.assertFalse(quoted["oracle_price_proven"])
        slot["price_scale"] = 2
        seen.clear()
        doubled = desk_loop.synthetic_screen(swap_mint, features(), desk_loop.public_reader, moment)
        doubled_urls = [url for url in seen if "/swap/v2/build?" in url]
        self.assertFalse(any(("amount=%s" % raw_amount) in url for url in doubled_urls), doubled_urls)
        smaller = memecoin_desk.view(swap_mint)["snapshot"]["entry_amount_raw"]
        self.assertLess(smaller, raw_amount)
        self.assertTrue(doubled_urls)
        self.assertIn("amount=%s" % smaller, doubled_urls[0])
        self.assertGreater(memecoin_desk.view(swap_mint)["snapshot"]["entry_intent_version"], 1)
        slot["price_scale"] = 1
        slot["pyth"] = False
        seen.clear()
        dark = desk_loop.synthetic_screen(swap_mint, features(), desk_loop.public_reader, moment)
        self.assertFalse(any("/swap/v2/" in url for url in seen), seen)
        self.assertEqual(memecoin_desk.view(swap_mint)["snapshot"]["entry_amount_raw"], smaller)
        self.assertIsNotNone(dark["quote"]["reason"])
        slot["pyth"] = True

        hostile = bytearray(accounts[fields[1]][1])
        hostile[32:64] = b"\x99" * 32
        accounts[fields[1]] = (solana_tx.TOKEN_PROGRAM, bytes(hostile))
        seen.clear()
        hostile_run = desk_loop.synthetic_screen(swap_mint, features(), desk_loop.public_reader, moment)
        self.assertFalse(any("/swap/v2/" in url for url in seen), seen)
        self.assertEqual(hostile_run["quote"]["reason"], "whirlpool_vault_unbound")
        restored = bytearray(solana_whirlpool.reference_token_account(fields[0], new_pool))
        restored[108] = 1
        accounts[fields[1]] = (solana_tx.TOKEN_PROGRAM, bytes(restored))

        seen.clear()
        slot["owner"] = solana_tx.TOKEN_PROGRAM
        wrong = desk_loop.synthetic_screen(swap_mint, features(), desk_loop.public_reader, moment)
        self.assertFalse(any("/swap/v2/" in url for url in seen), seen)
        wrong_quote = wrong["quote"]
        self.assertEqual(wrong_quote["reason"], "whirlpool_pool_unbound")
        self.assertFalse(wrong_quote["jupiter_called"])
        self.assertEqual(memecoin_desk.view(swap_mint)["stage"], "protected")

        seen.clear()
        slot["owner"] = None
        slot["tip"] = slot["context"] + 400
        stale = desk_loop.synthetic_screen(swap_mint, features(), desk_loop.public_reader, moment)
        self.assertFalse(any("/swap/v2/" in url for url in seen), seen)
        self.assertEqual(stale["quote"]["reason"], "rent_slot_stale")
        slot["tip"] = slot["context"] - 10
        seen.clear()
        future = desk_loop.synthetic_screen(swap_mint, features(), desk_loop.public_reader, moment)
        self.assertFalse(any("/swap/v2/" in url for url in seen))
        self.assertEqual(future["quote"]["reason"], "rent_slot_stale")
        slot["tip"] = slot["context"] + 28
        up = []
        for start in [base + i * step for i in range(3)]:
            parsed = solana_whirlpool.tick_array_state(accounts[solana_whirlpool.tick_array_address(new_pool, start)][1])
            up.append({"start_tick_index": parsed["start_tick_index"], "ticks": parsed["ticks"]})
        exit_qty = 999_899
        exit_est = solana_whirlpool_quote.quote_exact_in(state, up, exit_qty, False, 0, 0, slot["context"])["token_est_out"]
        held = memecoin_desk.view(swap_mint)["snapshot"]
        held["remaining_raw"] = exit_qty
        held["live_receipts"] = []
        held["capital_ledger"] = "live"
        held["live_capital"] = True
        held["fill_basis_proven"] = True
        held["entry_price_source"] = "entry_receipt"
        held["basis_cost_lamports"] = 1
        held["basis_decimals"] = 6
        held["entry_price_usd"] = "0.00000001"
        held["stop_price_usd"] = "0.000000001"
        held["trail_arm_price_usd"] = "0.000000012"
        held["pool"] = values["pool"]
        held["entry_funding"] = {"source": "entry_receipt", "proven": True, "signature": "receipt-supervised-exit"}
        db.execute(
            "INSERT INTO memecoin_entry_receipts (signature, case_id, message_sha256, slot, qty, cost_basis_lamports, network_fee_lamports, dex_fee_lamports, fee_usd, failed, ledger, ledger_provenance, pool) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'live', 'server', ?)",
            ("receipt-supervised-exit", swap_mint, "supervised", slot["context"], exit_qty, 1, 5000, 1, "0.01", values["pool"]),
        )
        db.execute(
            "INSERT INTO memecoin_exit_receipts (signature, case_id, message_sha256, slot, fee_usd, qty, ledger, ledger_provenance) VALUES (?, ?, ?, ?, ?, ?, 'live', 'server')",
            ("exit-row-400k", swap_mint, "supervised-exit", slot["context"], "0.01", 400_000),
        )
        db.execute(
            "INSERT INTO memecoin_exit_receipts (signature, case_id, message_sha256, slot, fee_usd, qty, ledger, ledger_provenance) VALUES (?, ?, ?, ?, ?, ?, 'live', 'server')",
            ("exit-row-other", "other-case", "other", slot["context"], "0.01", 400_000),
        )
        db.execute("DELETE FROM memecoin_cases WHERE id = ?", (swap_mint,))
        db.execute(
            "INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, created_at, updated_at, ledger, ledger_provenance) VALUES (?, ?, 'filled', ?, ?, ?, 'live', 'server')",
            (swap_mint, swap_mint, json.dumps(held, sort_keys=True), NOW, NOW),
        )
        db.set_setting("memecoin_entry_halt", "1", "test")
        slot["mark"] = "1.20"
        seen.clear()
        held_quiet = desk_loop.run_supervised(desk_loop.public_reader, moment, rounds=1)["discovery"]
        self.assertFalse(any("/swap/v2/" in url for url in seen), seen)
        self.assertEqual(held_quiet["positions"][0]["reason"], "exit_hold")
        self.assertEqual(memecoin_desk.view(swap_mint)["stage"], "filled")
        fired = memecoin_desk.view(swap_mint)["snapshot"]
        fired["entry_price_usd"] = "1000"
        fired["stop_price_usd"] = "940"
        fired["peak_price_usd"] = "1000"
        db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = ?", (json.dumps(fired, sort_keys=True), swap_mint))
        slot["mark"] = "0.90"
        seen.clear()
        exited = desk_loop.run_supervised(desk_loop.public_reader, moment, rounds=1)["discovery"]
        exit_url = next(url for url in seen if "/swap/v2/build?" in url)
        exit_left = 999_899 - 400_000
        exit_est = solana_whirlpool_quote.quote_exact_in(state, up, exit_left, False, 0, 0, slot["context"])["token_est_out"]
        exit_bps = jupiter_route_v2.encoded_slippage(exit_est)["slippage_bps"]
        self.assertIn("amount=%s" % exit_left, exit_url)
        self.assertNotIn("amount=%s" % exit_qty, exit_url)
        self.assertIn("slippageBps=%s" % exit_bps, exit_url)
        self.assertLessEqual(exit_bps, 50)
        self.assertEqual(memecoin_desk.view(swap_mint)["stage"], "exit_intent")
        self.assertEqual(db.get_setting("memecoin_entry_halt"), "1")
        self.assertEqual(exited["positions"][0]["quote"]["side"], "exit")
        self.assertNotIn(swap_mint, [item.get("mint") for item in exited["kills"]])

        db.execute("DELETE FROM settings WHERE key = ?", ("memecoin_max_slippage_bps",))
        db.init_db()
        seen.clear()
        missing = desk_loop.run_supervised(desk_loop.public_reader, moment, rounds=1)["discovery"]
        self.assertFalse(any("/swap/v2/" in url or "getMultipleAccounts" in url for url in seen), seen)
        self.assertEqual(missing["positions"][0]["quote"]["reason"], "owner_slippage_unapproved")
        self.assertFalse(jupiter_route_v2.quote_request(999_899)["ok"])
        self.assertIsNone(db.get_setting("memecoin_max_slippage_bps", None))

    def test_open_receipt_is_supervised_off_the_boost_list(self):
        other = solana_tx.b58encode(b"\x77" * 32)
        opened = memecoin_desk.scout({
            "mint": MINT, "source": "dexscreener", "fetched_at": NOW,
            "pair_created_at": int((NOW - 3600) * 1000), "market_cap_usd": "1000000",
            "liquidity_usd": "250000", "price_usd": "1", "price_change_h1": "1",
        }, now=NOW)
        snap = memecoin_desk.view(opened["case_id"])["snapshot"]
        snap["entry_price_usd"] = "1"
        snap["stop_price_usd"] = "0.94"
        snap["trail_drop_pct"] = "12"
        import db
        import json
        db.execute(
            "UPDATE memecoin_cases SET stage = 'filled', ledger = 'live', ledger_provenance = 'server', snapshot_json = ? WHERE id = ?",
            (json.dumps(snap, sort_keys=True), opened["case_id"]),
        )
        db.execute(
            "INSERT INTO memecoin_entry_receipts (signature, case_id, message_sha256, slot, qty, cost_basis_lamports, network_fee_lamports, dex_fee_lamports, fee_usd, failed, ledger, ledger_provenance) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'live', 'server')",
            ("entry-open", opened["case_id"], "open", 1, 100, 1, 1, 1, "0.01"),
        )
        price = {"usd": "1.00"}

        def transport(request):
            url = request.get("url") or ""
            if url.endswith("/token-boosts/latest/v1"):
                if price["usd"] == "fault":
                    raise RuntimeError("boosts down")
                return {"status": 200, "body": [{"chainId": "solana", "tokenAddress": other}]}
            if "/token-pairs/" in url:
                pair = _pair()
                pair[0]["baseToken"] = {"address": url.rsplit("/", 1)[-1]}
                pair[0]["priceUsd"] = "1.00" if price["usd"] != "fault" else "0.50"
                return {"status": 200, "body": pair}
            raw = bytearray(82)
            raw[45] = 1
            return {"status": 200, "body": {"result": {"context": {"slot": 10}, "value": {"owner": solana_tx.TOKEN_PROGRAM, "data": [__import__("base64").b64encode(bytes(raw)).decode(), "base64"]}}}}

        held = desk_loop.run_discovery(transport, NOW)
        self.assertEqual(memecoin_desk.view(MINT)["stage"], "filled")
        self.assertFalse(held["positions"][0]["jupiter_called"])
        self.assertNotIn(MINT, [item.get("mint") for item in held["kills"]])
        refused = memecoin_desk.mark(MINT, "0.10", source="caller_fixture", fetched_at=NOW, now=NOW)
        self.assertEqual(refused["reason"], "mark_source")
        self.assertEqual(memecoin_desk.view(MINT)["stage"], "filled")

        def marked(request):
            url = request.get("url") or ""
            if url.endswith("/token-boosts/latest/v1"):
                raise RuntimeError("boosts down")
            if "/token-pairs/" in url:
                body = _pair()
                body[0]["baseToken"] = {"address": url.rsplit("/", 1)[-1]}
                body[0]["priceUsd"] = "0.10"
                return {"status": 200, "body": body}
            raise AssertionError("rpc")

        fired = desk_loop.run_discovery(marked, NOW)
        self.assertTrue(fired["entry_halt"])
        self.assertEqual(fired["reason"], "discovery_fault")
        self.assertEqual(memecoin_desk.view(MINT)["stage"], "filled")
        snap = memecoin_desk.view(MINT)["snapshot"]
        snap["exit_intent"] = {"source": "server_pool_pyth", "live": True, "action": "stop"}
        snap["inflight"] = {"message_sha256": "already"}
        db.execute("UPDATE memecoin_cases SET stage = 'exit_intent', snapshot_json = ? WHERE id = ?", (json.dumps(snap, sort_keys=True), MINT))
        again = desk_loop.run_discovery(marked, NOW)
        self.assertEqual(again["positions"][0]["reason"], "exit_inflight")
        self.assertFalse(again["positions"][0]["jupiter_called"])
        self.assertEqual(memecoin_desk.view(MINT)["snapshot"]["inflight"]["message_sha256"], "already")

    def test_remaining_qty_is_the_exit_table(self):
        import db
        import json
        import threading
        opened = memecoin_desk.scout({
            "mint": MINT, "source": "dexscreener", "fetched_at": NOW,
            "pair_created_at": int((NOW - 3600) * 1000), "market_cap_usd": "1000000",
            "liquidity_usd": "250000", "price_usd": "1", "price_change_h1": "1",
        }, now=NOW)
        case_id = opened["case_id"]
        db.execute(
            "INSERT INTO memecoin_entry_receipts (signature, case_id, message_sha256, slot, qty, cost_basis_lamports, network_fee_lamports, dex_fee_lamports, fee_usd, failed, ledger, ledger_provenance) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'live', 'server')",
            ("entry-qty", case_id, "qty", 1, 999_899, 1, 1, 1, "0.01"),
        )
        db.execute(
            "INSERT INTO memecoin_exit_receipts (signature, case_id, message_sha256, slot, fee_usd, qty, ledger, ledger_provenance) VALUES (?, ?, ?, ?, ?, ?, 'live', 'server')",
            ("sold-400", case_id, "sold", 2, "0.01", 400_000),
        )
        db.execute(
            "INSERT INTO memecoin_exit_receipts (signature, case_id, message_sha256, slot, fee_usd, qty, ledger, ledger_provenance) VALUES (?, ?, ?, ?, ?, ?, 'live', 'server')",
            ("sold-else", "another-case", "else", 2, "0.01", 400_000),
        )
        snap = memecoin_desk.view(case_id)["snapshot"]
        snap["remaining_raw"] = 999_899
        snap["live_receipts"] = []
        snap["exit_intent"] = {"source": "server_pool_pyth", "live": True, "action": "stop"}
        db.execute(
            "UPDATE memecoin_cases SET stage = 'exit_intent', ledger = 'live', ledger_provenance = 'server', snapshot_json = ? WHERE id = ?",
            (json.dumps(snap, sort_keys=True), case_id),
        )
        self.assertEqual(memecoin_desk.receipt_remaining(case_id)["remaining"], 599_899)
        forged = desk_loop._quote_intent(MINT, solana_tx.b58encode(b"\x11" * 32))
        self.assertEqual(forged["reason"], "exit_unproven")
        pool = solana_tx.b58encode(b"\x21" * 32)
        db.execute("UPDATE memecoin_entry_receipts SET pool = ? WHERE signature = ?", (pool, "entry-qty"))
        snap["entry_price_usd"] = "1"
        snap["basis_cost_lamports"] = 1
        db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = ?", (json.dumps(snap, sort_keys=True), case_id))
        db.execute(
            """
            INSERT INTO memecoin_risk_intents (
                id, case_id, ledger, ledger_provenance, entry_signature, action, pool, slot, mark_price_usd,
                entry_price_usd, stop_price_usd, qty, basis_cost_lamports, basis_decimals,
                evidence_json, created_at
            ) VALUES ('intent-qty', ?, 'live', 'server', 'entry-qty', 'stop', ?, 1, '0.90', '1', '0.94', 999899, 1, 6, '{}', ?)
            """,
            (case_id, pool, NOW),
        )
        snap.pop("entry_price_usd", None)
        snap.pop("basis_cost_lamports", None)
        db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = ?", (json.dumps(snap, sort_keys=True), case_id))
        other_pool = solana_tx.b58encode(b"\x22" * 32)
        mismatched = desk_loop._quote_intent(MINT, other_pool)
        self.assertEqual(mismatched["reason"], "pool_mismatch")
        self.assertNotIn("amount", mismatched)
        intent = desk_loop._quote_intent(MINT, pool)
        self.assertEqual(intent["amount"], 599_899)
        self.assertEqual(intent["pool"], pool)
        reserved_before = json.loads(db.query("SELECT snapshot_json FROM memecoin_cases WHERE id = ?", (case_id,))[0]["snapshot_json"]).get("reserved_usd")
        for label in ("synthetic", "paper", "live", None):
            body = json.loads(db.query("SELECT snapshot_json FROM memecoin_cases WHERE id = ?", (case_id,))[0]["snapshot_json"])
            if label is None:
                body.pop("capital_ledger", None)
            else:
                body["capital_ledger"] = label
            db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = ?", (json.dumps(body), case_id))
            again = desk_loop._quote_intent(MINT, pool)
            self.assertEqual(again["amount"], 599_899, label)
            self.assertEqual(again["pool"], pool)
            self.assertEqual(again.get("snapshot_mode_conflict"), label not in {None, "live"})
        self.assertEqual(json.loads(db.query("SELECT snapshot_json FROM memecoin_cases WHERE id = ?", (case_id,))[0]["snapshot_json"]).get("reserved_usd"), reserved_before)
        synthetic_mint = solana_tx.b58encode(b"\x53" * 32)
        db.execute(
            "INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, created_at, updated_at, ledger, ledger_provenance) VALUES (?, ?, 'exit_intent', ?, ?, ?, 'synthetic', 'server')",
            (synthetic_mint, synthetic_mint, json.dumps({"capital_ledger": "live", "reserved_usd": "8.00"}), NOW, NOW),
        )
        blocked = desk_loop._quote_intent(synthetic_mint, pool)
        self.assertEqual(blocked["reason"], "synthetic_not_live")
        self.assertNotIn("amount", blocked)
        self.assertEqual(json.loads(db.query("SELECT snapshot_json FROM memecoin_cases WHERE id = ?", (case_id,))[0]["snapshot_json"]).get("reserved_usd"), reserved_before)
        seen = []

        def read():
            seen.append(memecoin_desk.receipt_remaining(case_id)["remaining"])

        threads = [threading.Thread(target=read) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(set(seen), {599_899})
        import db as again
        self.assertEqual(again.query("SELECT qty FROM memecoin_exit_receipts WHERE case_id = ?", (case_id,))[0]["qty"], 400_000)
        self.assertEqual(memecoin_desk.receipt_remaining(case_id)["remaining"], 599_899)
        db.execute("UPDATE memecoin_exit_receipts SET qty = ? WHERE signature = ?", (999_899, "sold-400"))
        self.assertEqual(memecoin_desk.receipt_remaining(case_id)["remaining"], 0)
        flat = desk_loop._quote_intent(MINT, pool)
        self.assertEqual(flat["reason"], "flat")
        self.assertNotIn("amount", flat)

    def test_rent_uses_the_classic_account_and_refuses_the_rest(self):
        import base64
        import phantom_transport
        import solana_whirlpool
        wallet = phantom_transport.REQUIRED_WALLET
        tip = {"slot": 1_000}
        wsol = solana_tx.associated_token_address(wallet, solana_tx.NATIVE_MINT, solana_tx.TOKEN_PROGRAM)
        ata = solana_tx.associated_token_address(wallet, MINT, solana_tx.TOKEN_PROGRAM)

        def mint_account(owner):
            raw = bytearray(82)
            raw[45] = 1
            return {"owner": owner, "data": [base64.b64encode(bytes(raw)).decode(), "base64"]}

        def classic(mint):
            raw = bytearray(solana_whirlpool.reference_token_account(mint, wallet))
            raw[108] = 1
            return {"owner": solana_tx.TOKEN_PROGRAM, "data": [base64.b64encode(bytes(raw)).decode(), "base64"]}

        def transport(accounts, owner=solana_tx.TOKEN_PROGRAM, context=1_000, genesis="5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"):
            def send(request):
                method = request.get("method")
                if method == "getGenesisHash":
                    return {"status": 200, "body": {"result": genesis}}
                if method == "getSlot":
                    return {"status": 200, "body": {"result": tip["slot"]}}
                if method == "getAccountInfo":
                    return {"status": 200, "body": {"result": {"context": {"slot": context}, "value": mint_account(owner)}}}
                if method == "getMinimumBalanceForRentExemption":
                    return {"status": 200, "body": {"result": solana_tx.TOKEN_ACCOUNT_RENT}}
                if method == "getMultipleAccounts":
                    value = []
                    for key in request["accounts"]:
                        value.append(accounts.get(key))
                    return {"status": 200, "body": {"result": {"context": {"slot": context}, "value": value}}}
                raise AssertionError(method)
            return send

        both = desk_loop._account_rent(transport({}), MINT)
        self.assertTrue(both["ok"], both)
        self.assertEqual(both["count"], 2)
        self.assertEqual(both["lamports"], solana_tx.TOKEN_ACCOUNT_RENT * 2)
        one = desk_loop._account_rent(transport({wsol: classic(solana_tx.NATIVE_MINT)}), MINT)
        self.assertEqual(one["count"], 1)
        self.assertEqual(one["lamports"], solana_tx.TOKEN_ACCOUNT_RENT)
        present = desk_loop._account_rent(transport({wsol: classic(solana_tx.NATIVE_MINT), ata: classic(MINT)}), MINT)
        self.assertEqual(present["count"], 0)
        self.assertEqual(present["lamports"], 0)
        short = {"owner": solana_tx.TOKEN_PROGRAM, "data": [base64.b64encode(b"\x01").decode(), "base64"]}
        refused = desk_loop._account_rent(transport({wsol: short}), MINT)
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["reason"], "rent_account_unbound")
        self.assertNotIn("lamports", refused)
        token2022 = desk_loop._account_rent(transport({}, owner=solana_tx.TOKEN_2022), MINT)
        self.assertEqual(token2022["reason"], "token_2022_unsupported")
        self.assertIsNone(token2022["lamports"])
        stale = desk_loop._account_rent(transport({}, context=1), MINT)
        tip["slot"] = 1_000
        self.assertEqual(stale["reason"], "rent_slot_stale")
        future = desk_loop._account_rent(transport({}, context=1_100), MINT)
        self.assertEqual(future["reason"], "rent_slot_stale")
        bad = desk_loop._account_rent(transport({}, genesis="not-mainnet"), MINT)
        self.assertEqual(bad["reason"], "genesis")
        wrong = {"mint": "other", "source": "synthetic_test_adapter", "fetched_at": NOW, "onchain": {}, "components": {}}
        self.assertEqual(desk_loop.synthetic_screen(MINT, wrong, transport({}), NOW)["reason"], "synthetic_mint")
        self.assertIsNone(memecoin_desk.view("other"))

    def test_synthetic_ledger_cannot_reach_live_reserve_or_loss(self):
        import db
        import json
        from decimal import Decimal
        opened = memecoin_desk.scout({
            "mint": MINT, "source": "dexscreener", "fetched_at": NOW,
            "pair_created_at": int((NOW - 3600) * 1000), "market_cap_usd": "1000000",
            "liquidity_usd": "250000", "price_usd": "1", "price_change_h1": "1",
        }, now=NOW)
        case_id = opened["case_id"]
        from tests.test_jupiter_route_v2 import WHEN, _prices
        import pyth_sponsored
        pyth_sponsored.read_accounts(_prices(), WHEN)
        db.execute("UPDATE memecoin_cases SET stage = 'analyst_passed' WHERE id = ?", (case_id,))
        protected = memecoin_desk.protect(case_id, now=WHEN, ledger="synthetic", rent_proof={"ok": True, "lamports": 1_488_440, "count": 1})
        if protected.get("ok") is not True:
            snap = memecoin_desk.view(case_id)["snapshot"]
            snap["price_usd"] = "1"
            db.execute("UPDATE memecoin_cases SET snapshot_json = ?, stage = 'analyst_passed' WHERE id = ?", (json.dumps(snap), case_id))
            protected = memecoin_desk.protect(case_id, now=NOW, ledger="synthetic", rent_proof={"ok": True, "lamports": 1_488_440, "count": 1})
        self.assertEqual(db.query("SELECT ledger FROM memecoin_cases WHERE id = ?", (case_id,))[0]["ledger"], "synthetic", protected)
        called = {"n": 0}

        def prices(_now):
            called["n"] += 1
            raise AssertionError("network")

        reserved = memecoin_desk.reserve_entry(case_id, "not-a-message", None, prices, NOW)
        self.assertEqual(reserved["reason"], "synthetic_not_live")
        self.assertEqual(called["n"], 0)
        self.assertNotIn("entry_inflight", memecoin_desk.view(case_id)["snapshot"])
        forged = memecoin_desk.view(case_id)["snapshot"]
        forged["capital_ledger"] = "live"
        forged["live_ledger"] = True
        forged["entry_inflight"] = {"message_sha256": "forged"}
        db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = ?", (json.dumps(forged, sort_keys=True), case_id))

        def chain(_signature):
            raise AssertionError("chain")

        original = desk_loop.read_transaction
        desk_loop.read_transaction = chain
        try:
            confirmed = memecoin_desk.confirm_entry(case_id, "sig-synthetic", NOW)
        finally:
            desk_loop.read_transaction = original
        self.assertEqual(confirmed["reason"], "ledger_lineage")
        self.assertTrue(confirmed["quarantine"])
        self.assertEqual(db.query("SELECT signature FROM memecoin_entry_receipts"), [])
        self.assertEqual(db.get_setting("memecoin_integrity_halt"), "1")
        forged["realized_pnl"] = "-5"
        forged["pnl_live"] = True
        forged["entry_funding"] = {"source": "entry_receipt", "proven": True, "signature": "sig-synthetic", "tokens_out": 10}
        db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = ?", (json.dumps(forged, sort_keys=True), case_id))
        db.execute(
            "INSERT INTO memecoin_entry_receipts (signature, case_id, message_sha256, slot, qty, cost_basis_lamports, network_fee_lamports, dex_fee_lamports, fee_usd, failed, ledger, ledger_provenance) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'synthetic', 'server')",
            ("sig-synthetic", case_id, "forged", 1, 10, 1, 1, 1, "0.01"),
        )
        db.execute(
            "INSERT INTO memecoin_refunds (signature, case_id, rent_lamports, network_fee_lamports, fee_usd, released, ledger, ledger_provenance) VALUES ('refund-synthetic', ?, 1, 1, '5.00', 1, 'synthetic', 'server')",
            (case_id,),
        )
        self.assertEqual(memecoin_desk.loss_totals()["live_loss_usd"], "0.00")
        live = memecoin_desk.scout({
            "mint": solana_tx.b58encode(b"\x42" * 32), "source": "dexscreener", "fetched_at": NOW,
            "pair_created_at": int((NOW - 3600) * 1000), "market_cap_usd": "1000000",
            "liquidity_usd": "250000", "price_usd": "1", "price_change_h1": "1",
        }, now=NOW)
        db.execute("UPDATE memecoin_cases SET stage = 'protected', ledger = 'live', ledger_provenance = 'server' WHERE id = ?", (live["case_id"],))
        replay = memecoin_desk.confirm_entry(live["case_id"], "sig-synthetic", NOW)
        self.assertEqual(replay["reason"], "cross_ledger_replay")
        self.assertEqual(memecoin_desk.view(live["case_id"])["stage"], "protected")
        for number in range(12):
            mint = solana_tx.b58encode(bytes([number + 1]) * 32)
            db.execute(
                "INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, created_at, updated_at, ledger) VALUES (?, ?, 'protected', ?, ?, ?, 'live')",
                ("live-%s" % number, mint, json.dumps({"reserved_usd": "8.00"}), NOW, NOW),
            )
        db.execute(
            "INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, created_at, updated_at, ledger) VALUES ('live-fee', ?, 'protected', ?, ?, ?, 'live')",
            (solana_tx.b58encode(b"\x63" * 32), json.dumps({"reserved_usd": "4.00"}), NOW, NOW),
        )
        self.assertFalse(db.exclusive(lambda conn: memecoin_desk._room_for(conn, "new-ticket", Decimal("8"))))
        db.init_db()
        self.assertFalse(db.exclusive(lambda conn: memecoin_desk._room_for(conn, "new-ticket", Decimal("8"))))

    def test_live_mark_uses_pool_bytes_not_a_caller_price(self):
        import db
        import json
        from decimal import Decimal
        from tests.test_jupiter_route_v2 import WHEN, _prices
        import pyth_sponsored
        import solana_whirlpool
        opened = memecoin_desk.scout({
            "mint": MINT, "source": "dexscreener", "fetched_at": WHEN,
            "pair_created_at": int((WHEN - 3600) * 1000), "market_cap_usd": "1000000",
            "liquidity_usd": "250000", "price_usd": "9", "price_change_h1": "1",
        }, now=WHEN)
        case_id = opened["case_id"]
        import base64
        raw = bytearray(solana_whirlpool.reference_pool(
            solana_tx.NATIVE_MINT, solana_tx.b58encode(b"\x31" * 32),
            MINT, solana_tx.b58encode(b"\x32" * 32),
            sqrt_price=1 << 64,
        ))
        raw[8:40] = b"\x44" * 32
        fields = solana_whirlpool.pool_fields(bytes(raw))
        seed = bytes(raw[solana_whirlpool._TICK_SPACING + 2:solana_whirlpool._TICK_SPACING + 4])
        derived, bump = solana_tx.find_program_address(
            [b"whirlpool", bytes(raw[8:40]), solana_tx.b58decode(fields[0]), solana_tx.b58decode(fields[2]), seed],
            solana_tx.b58decode(solana_whirlpool.PROGRAM_ID),
        )
        raw[40] = bump
        pool = solana_tx.b58encode(derived)
        accounts = {pool: (solana_whirlpool.PROGRAM_ID, bytes(raw))}
        for vault, mint in ((fields[1], fields[0]), (fields[3], fields[2])):
            token = bytearray(solana_whirlpool.reference_token_account(mint, pool))
            token[108] = 1
            accounts[vault] = (solana_tx.TOKEN_PROGRAM, bytes(token))
        accounts[solana_whirlpool.oracle_address(pool)] = (solana_whirlpool.PROGRAM_ID, solana_whirlpool.oracle_account(pool))
        state = {"genesis": "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d", "owner": None, "slot": 2_000_000, "tip": 2_000_028, "pyth": True}

        def transport(request):
            method = request.get("method")
            if method == "getGenesisHash":
                return {"status": 200, "body": {"result": state["genesis"]}}
            if method == "getSlot":
                return {"status": 200, "body": {"result": state["tip"]}}
            if method == "getAccountInfo":
                mint_raw = bytearray(82)
                mint_raw[44] = 6
                mint_raw[45] = 1
                return {"status": 200, "body": {"result": {"context": {"slot": state["slot"]}, "value": {"owner": solana_tx.TOKEN_PROGRAM, "data": [base64.b64encode(bytes(mint_raw)).decode(), "base64"]}}}}
            keys = request.get("accounts") or []
            if keys == [pyth_sponsored.SOL_ACCOUNT, pyth_sponsored.USDC_ACCOUNT]:
                if state["pyth"] is not True:
                    return {"status": 200, "body": {"error": {"code": -1}}}
                return _prices()(request)
            value = []
            for key in keys:
                owner, blob = accounts[key]
                if key == pool and state["owner"] is not None:
                    owner = state["owner"]
                value.append({"owner": owner, "data": [base64.b64encode(blob).decode(), "base64"]})
            return {"status": 200, "body": {"result": {"context": {"slot": state["slot"]}, "value": value}}}

        leg = pyth_sponsored.read_accounts(_prices(), WHEN)["sol"]
        untrusted = memecoin_desk.read_bound_price(case_id, bytes(raw), pool, leg, 6, state["slot"], state["tip"], WHEN)
        self.assertFalse(untrusted["trusted"])
        self.assertFalse(hasattr(memecoin_desk, "_seal_mark"))
        self.assertEqual(memecoin_desk.mark_open_position(case_id, untrusted, WHEN)["reason"], "mark_source")
        db.execute(
            "INSERT INTO memecoin_entry_receipts (signature, case_id, message_sha256, slot, qty, cost_basis_lamports, network_fee_lamports, dex_fee_lamports, fee_usd, failed, ledger, pool, ledger_provenance) VALUES ('mark-entry', ?, 'mark', 1, 1000, 1, 1, 1, '0.01', 0, 'live', ?, 'server')",
            (case_id, pool),
        )
        snap = memecoin_desk.view(case_id)["snapshot"]
        snap.update({
            "fill_basis_proven": True, "entry_price_source": "entry_receipt",
            "entry_price_usd": "0.0000001", "stop_price_usd": "0.00000001",
            "trail_arm_price_usd": "0.00000012", "trail_drop_pct": "12",
            "discovery_price_usd": "9", "basis_cost_lamports": 1, "basis_decimals": 6,
        })
        db.execute("UPDATE memecoin_cases SET stage = 'filled', ledger = 'live', ledger_provenance = 'server', snapshot_json = ? WHERE id = ?", (json.dumps(snap, sort_keys=True), case_id))
        state["genesis"] = "not-mainnet"
        self.assertEqual(memecoin_desk.mark_open_position(case_id, transport, WHEN)["reason"], "genesis")
        state["genesis"] = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
        state["owner"] = solana_tx.TOKEN_PROGRAM
        self.assertEqual(memecoin_desk.mark_open_position(case_id, transport, WHEN)["reason"], "whirlpool_pool_unbound")
        state["owner"] = None
        state["tip"] = state["slot"] + 400
        self.assertEqual(memecoin_desk.mark_open_position(case_id, transport, WHEN)["reason"], "slot_lag")
        state["tip"] = state["slot"] - 10
        self.assertEqual(memecoin_desk.mark_open_position(case_id, transport, WHEN)["reason"], "slot_lag")
        state["tip"] = state["slot"] + 28
        state["pyth"] = False
        self.assertNotEqual(memecoin_desk.mark_open_position(case_id, transport, WHEN).get("action"), "stop")
        state["pyth"] = True
        self.assertEqual(db.query("SELECT id FROM memecoin_risk_intents WHERE case_id = ?", (case_id,)), [])
        self.assertEqual(memecoin_desk.mark(case_id, "0.10", source="caller_fixture", fetched_at=WHEN, now=WHEN)["reason"], "mark_source")
        held = memecoin_desk.mark_open_position(case_id, transport, WHEN)
        self.assertEqual(held["action"], "hold", held)
        self.assertNotEqual(Decimal(held["mark_price"]), Decimal("9"))
        snap = memecoin_desk.view(case_id)["snapshot"]
        snap["stop_price_usd"] = held["mark_price"]
        snap["entry_price_usd"] = format(Decimal(held["mark_price"]) / Decimal("0.94"), "f")
        db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = ?", (json.dumps(snap, sort_keys=True), case_id))
        fired = memecoin_desk.mark_open_position(case_id, transport, WHEN)
        self.assertEqual(fired["action"], "stop", fired)
        self.assertEqual(memecoin_desk.view(case_id)["stage"], "exit_intent")
        row = db.query("SELECT id, pool, ledger FROM memecoin_risk_intents WHERE case_id = ?", (case_id,))[0]
        self.assertEqual(row["pool"], pool)
        self.assertEqual(row["ledger"], "live")
        db.init_db()
        self.assertEqual(db.query("SELECT id FROM memecoin_risk_intents WHERE case_id = ?", (case_id,))[0]["id"], row["id"])

    def test_unknown_ledger_stays_a_hold_and_omission_does_not_become_live(self):
        import db
        import json
        import sqlite3
        import threading
        from decimal import Decimal
        memecoin_desk.loss_totals()
        db.execute(
            "INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, created_at, updated_at, ledger) VALUES ('odd', ?, 'protected', ?, ?, ?, 'bogus')",
            (MINT, json.dumps({"reserved_usd": "8.00"}), NOW, NOW),
        )
        self.assertGreaterEqual(db.exclusive(lambda conn: memecoin_desk._held_conn(conn)), Decimal("8"))
        called = {"n": 0}

        def prices(_now):
            called["n"] += 1
            raise AssertionError("network")

        refused = memecoin_desk.reserve_entry("odd", "nope", None, prices, NOW)
        self.assertEqual(refused["reason"], "ledger_unknown")
        self.assertEqual(called["n"], 0)
        self.assertEqual(db.get_setting("memecoin_integrity_halt"), "1")
        self.assertEqual(json.loads(db.query("SELECT snapshot_json FROM memecoin_cases WHERE id = 'odd'")[0]["snapshot_json"])["reserved_usd"], "8.00")
        self.assertEqual(memecoin_desk.loss_totals()["live_loss_usd"], "0.00")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO memecoin_entry_receipts (signature, case_id, message_sha256, slot, qty, cost_basis_lamports, network_fee_lamports, dex_fee_lamports, fee_usd, failed) VALUES ('omit', 'odd', 'omit', 1, 1, 1, 1, 1, '0.01', 0)"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO memecoin_entry_receipts (signature, case_id, message_sha256, slot, qty, cost_basis_lamports, network_fee_lamports, dex_fee_lamports, fee_usd, failed, ledger) VALUES ('no-prov', 'odd', 'no-prov', 1, 1, 1, 1, 1, '0.01', 0, 'live')"
            )
        winners = []

        def insert(signature):
            try:
                db.execute(
                    "INSERT INTO memecoin_entry_receipts (signature, case_id, message_sha256, slot, qty, cost_basis_lamports, network_fee_lamports, dex_fee_lamports, fee_usd, failed, ledger, ledger_provenance) VALUES (?, 'odd', 'race', 1, 1, 1, 1, 1, '0.01', 0, 'live', 'server')",
                    (signature,),
                )
                winners.append(signature)
            except sqlite3.IntegrityError:
                winners.append("lost")

        threads = [threading.Thread(target=insert, args=("same-sig",)) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(winners.count("same-sig"), 1)
        self.assertEqual(len(db.query("SELECT signature FROM memecoin_entry_receipts WHERE signature = 'same-sig'")), 1)
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE memecoin_entry_receipts SET ledger_provenance = 'caller' WHERE signature = 'same-sig'")
        self.assertEqual(db.query("SELECT ledger_provenance, cost_basis_lamports FROM memecoin_entry_receipts WHERE signature = 'same-sig'")[0]["cost_basis_lamports"], 1)
        self.assertEqual(db.query("SELECT ledger_provenance FROM memecoin_entry_receipts WHERE signature = 'same-sig'")[0]["ledger_provenance"], "server")

    def test_set_lineage_cannot_be_rewritten(self):
        import db
        import sqlite3
        conn = db.connect()
        conn.execute(
            """
            CREATE TABLE memecoin_cases (
                id TEXT PRIMARY KEY, mint TEXT NOT NULL, stage TEXT NOT NULL,
                snapshot_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                ledger TEXT, ledger_provenance TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE memecoin_entry_receipts (
                signature TEXT PRIMARY KEY, case_id TEXT NOT NULL, message_sha256 TEXT NOT NULL,
                slot INTEGER NOT NULL, qty INTEGER NOT NULL, cost_basis_lamports INTEGER NOT NULL,
                network_fee_lamports INTEGER NOT NULL, dex_fee_lamports INTEGER NOT NULL,
                fee_usd TEXT NOT NULL, failed INTEGER NOT NULL,
                ledger TEXT, ledger_provenance TEXT
            )
            """
        )
        rows = (
            ("live-row", "live", "server", 11),
            ("paper-row", "paper", "server", 12),
            ("synthetic-row", "synthetic", "server", 13),
            ("unknown-row", "live", "mystery", 14),
            ("legacy-row", "live", "legacy_default", 15),
            ("omitted-row", "live", None, 16),
        )
        for signature, ledger, provenance, cost in rows:
            conn.execute(
                "INSERT INTO memecoin_entry_receipts (signature, case_id, message_sha256, slot, qty, cost_basis_lamports, network_fee_lamports, dex_fee_lamports, fee_usd, failed, ledger, ledger_provenance) VALUES (?, 'case', 'old', 1, 1, ?, 1, 1, '0.01', 0, ?, ?)",
                (signature, cost, ledger, provenance),
            )
        conn.execute(
            "INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, created_at, updated_at, ledger, ledger_provenance) VALUES ('scout-row', ?, 'scouted', '{}', ?, ?, NULL, NULL)",
            (MINT, NOW, NOW),
        )
        conn.commit()
        conn.close()
        memecoin_desk.loss_totals()
        omitted = db.query("SELECT ledger_provenance, cost_basis_lamports FROM memecoin_entry_receipts WHERE signature = 'omitted-row'")[0]
        self.assertEqual(omitted["ledger_provenance"], "legacy_default")
        self.assertEqual(omitted["cost_basis_lamports"], 16)
        self.assertIsNone(db.query("SELECT ledger_provenance FROM memecoin_cases WHERE id = 'scout-row'")[0]["ledger_provenance"])
        for signature, ledger, provenance, cost in rows:
            target = "paper" if ledger != "paper" else "live"
            next_provenance = "legacy_default" if provenance == "server" else "server"
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE memecoin_entry_receipts SET ledger = ? WHERE signature = ?", (target, signature))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE memecoin_entry_receipts SET ledger_provenance = ? WHERE signature = ?", (next_provenance, signature))
            self.assertEqual(db.query("SELECT cost_basis_lamports, ledger, ledger_provenance FROM memecoin_entry_receipts WHERE signature = ?", (signature,))[0]["cost_basis_lamports"], cost)
        memecoin_desk.loss_totals()
        self.assertEqual(db.query("SELECT SUM(cost_basis_lamports) AS total FROM memecoin_entry_receipts")[0]["total"], 11 + 12 + 13 + 14 + 15 + 16)
        self.assertIsNone(memecoin_desk._live_entry_receipt("case"))

    def test_legacy_default_live_is_quarantined_and_the_hold_stays(self):
        import db
        import json
        import sqlite3
        import threading
        from decimal import Decimal
        conn = db.connect()
        conn.execute(
            """
            CREATE TABLE memecoin_cases (
                id TEXT PRIMARY KEY, mint TEXT NOT NULL, stage TEXT NOT NULL,
                snapshot_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                ledger TEXT NOT NULL DEFAULT 'live'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE memecoin_entry_receipts (
                signature TEXT PRIMARY KEY, case_id TEXT NOT NULL, message_sha256 TEXT NOT NULL,
                slot INTEGER NOT NULL, qty INTEGER NOT NULL, cost_basis_lamports INTEGER NOT NULL,
                network_fee_lamports INTEGER NOT NULL, dex_fee_lamports INTEGER NOT NULL,
                fee_usd TEXT NOT NULL, failed INTEGER NOT NULL,
                ledger TEXT NOT NULL DEFAULT 'live'
            )
            """
        )
        conn.execute(
            "INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, created_at, updated_at) VALUES ('old', ?, 'protected', ?, ?, ?)",
            (MINT, json.dumps({"reserved_usd": "8.00"}), NOW, NOW),
        )
        conn.execute(
            "INSERT INTO memecoin_entry_receipts (signature, case_id, message_sha256, slot, qty, cost_basis_lamports, network_fee_lamports, dex_fee_lamports, fee_usd, failed) VALUES ('old-fill', 'old', 'old', 1, 10, 5, 1, 1, '0.01', 0)"
        )
        conn.commit()
        conn.close()
        threads = [threading.Thread(target=memecoin_desk.loss_totals) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        legacy = db.query("SELECT * FROM memecoin_cases WHERE id = 'old'")[0]
        self.assertEqual(memecoin_desk._ledger_of(legacy), "unknown")
        self.assertEqual(json.loads(legacy["snapshot_json"])["reserved_usd"], "8.00")
        self.assertGreaterEqual(db.exclusive(lambda item: memecoin_desk._held_conn(item)), Decimal("8"))
        self.assertIsNone(memecoin_desk._live_entry_receipt("old"))
        self.assertEqual(db.query("SELECT cost_basis_lamports FROM memecoin_entry_receipts WHERE signature = 'old-fill'")[0]["cost_basis_lamports"], 5)
        memecoin_desk.loss_totals()
        self.assertEqual(memecoin_desk._ledger_of(db.query("SELECT * FROM memecoin_cases WHERE id = 'old'")[0]), "unknown")
        self.assertEqual(json.loads(db.query("SELECT snapshot_json FROM memecoin_cases WHERE id = 'old'")[0]["snapshot_json"])["reserved_usd"], "8.00")
        db.execute(
            "INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, created_at, updated_at, ledger, ledger_provenance) VALUES ('new', ?, 'protected', ?, ?, ?, 'live', 'server')",
            (solana_tx.b58encode(b"\x41" * 32), json.dumps({"reserved_usd": "4.00"}), NOW, NOW),
        )
        self.assertEqual(memecoin_desk._ledger_of(db.query("SELECT * FROM memecoin_cases WHERE id = 'new'")[0]), "live")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE memecoin_entry_receipts SET ledger_provenance = 'server' WHERE signature = 'old-fill'")
        self.assertEqual(db.query("SELECT cost_basis_lamports, ledger_provenance FROM memecoin_entry_receipts WHERE signature = 'old-fill'")[0]["cost_basis_lamports"], 5)
        self.assertEqual(db.query("SELECT ledger_provenance FROM memecoin_entry_receipts WHERE signature = 'old-fill'")[0]["ledger_provenance"], "legacy_default")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE memecoin_entry_receipts SET ledger = 'paper' WHERE signature = 'old-fill'")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE memecoin_cases SET ledger = 'paper', ledger_provenance = 'server' WHERE id = 'old'")
        self.assertEqual(db.query("SELECT cost_basis_lamports FROM memecoin_entry_receipts WHERE signature = 'old-fill'")[0]["cost_basis_lamports"], 5)
        self.assertEqual(json.loads(db.query("SELECT snapshot_json FROM memecoin_cases WHERE id = 'old'")[0]["snapshot_json"])["reserved_usd"], "8.00")
