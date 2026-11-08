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
        self.assertEqual(result["reason"], "stalkchain_credentials_missing")
        self.assertEqual(result["stage"], "scout_passed")
        self.assertEqual(result["capability"]["transport"], "unconfigured")
        self.assertEqual(result["capability"]["lp"], "unproven")
        self.assertEqual(result["capability"]["score"], "unapproved")
        self.assertEqual(result["capability"]["analyst_formula"], "UNAPPROVED")
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
        self.assertEqual(found["kills"][0]["reason"], "stalkchain_credentials_missing")
        self.assertEqual(found["kills"][0]["capability"]["transport"], "unconfigured")
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

    def test_a_documented_stalkchain_body_does_not_open_the_handoff(self):
        os.environ["STALKCHAIN_API_KEY"] = "sc_fixture_not_a_real_key"
        self.addCleanup(lambda: os.environ.pop("STALKCHAIN_API_KEY", None))
        calls = []

        def envelope(mint, extra=None):
            data = {"mint": mint, "marketCapBasis": "market_cap", "marketCapSource": "fixture", "fdvUsd": 99}
            if extra:
                data.update(extra)
            return {"data": data, "notes": [], "credits": 0, "meta": {}}

        def transport(request):
            if request.get("method") == "POST":
                calls.append((request["url"], request["json"]))
                self.assertTrue(request["headers"]["authorization"].endswith("sc_fixture_not_a_real_key"))
                return {"status": 200, "body": envelope(MINT)}
            if str(request.get("url", "")).endswith("/token-boosts/latest/v1"):
                return {"status": 200, "body": [{"chainId": "solana", "tokenAddress": MINT}]}
            if request.get("kind") == "rpc":
                raw = bytearray(82)
                raw[44] = 6
                raw[45] = 1
                return {"status": 200, "body": {"result": {"value": {"owner": solana_tx.TOKEN_PROGRAM, "data": [__import__("base64").b64encode(bytes(raw)).decode(), "base64"]}}}}
            return {"status": 200, "body": _pair()}

        found = desk_loop.run_discovery(transport, NOW)
        self.assertEqual(len(calls), 4)
        self.assertEqual({item[1]["token"] for item in calls}, {MINT})
        self.assertTrue(all(item[0].startswith("https://data.stalkchain.com/api/v1/tools/") for item in calls))
        kill = next(item for item in found["kills"] if item.get("mint") == MINT)
        self.assertEqual(kill["reason"], "response_fields_unpublished")
        self.assertEqual(kill["stage"], "scout_passed")
        self.assertEqual(kill["capability"]["transport"], "ready")
        self.assertEqual(kill["capability"]["request_schema"], "documented")
        self.assertFalse(kill["capability"]["response_verified"])
        self.assertEqual(kill["capability"]["freshness"], "unobserved")
        self.assertEqual(kill["capability"]["lp"], "unproven")
        self.assertEqual(kill["capability"]["rugs"], "unproven")
        self.assertEqual(kill["capability"]["growth"], "unproven")
        self.assertEqual(kill["capability"]["score"], "unapproved")
        self.assertFalse(kill["score_measured"])
        self.assertFalse(kill["fixture_is_mainnet_proof"])
        self.assertFalse(found["armed"])
        self.assertFalse(found["score_measured"])
        fields = {item["field"]: item for item in kill["source_fields"]}
        for name in ("lp_burned_or_locked", "dev_rugs", "bundled_launch_pct", "holder_growth", "analyst_score"):
            self.assertFalse(fields[name]["proven"], name)
            self.assertIsNone(fields[name]["json_path"], name)
            self.assertEqual(fields[name]["status"], "observation_required")
            self.assertIn("nov8-observed-mcp-field-mapping.json", fields[name]["observation_files"])
        self.assertNotIn("sc_fixture_not_a_real_key", __import__("json").dumps(found))
        formula = memecoin_desk.analyst_decision()
        self.assertFalse(formula["owner_approved"])
        self.assertEqual(formula["approved_numbers"]["stop_pct"], "6")
        self.assertEqual(formula["approved_numbers"]["liq_of_mcap"], "0.25")
        self.assertIn("analyst_weights_unapproved", desk_loop.scheduler_status()["ready_because"])
        self.assertFalse(desk_loop.scheduler_status()["armed"])
        import stalkchain_adapter
        malformed = stalkchain_adapter.normalize({"lp_burned_or_locked": True}, MINT)
        self.assertEqual(malformed["reason"], "envelope")
        self.assertEqual(malformed["proven"], {})
        crossed = stalkchain_adapter.normalize(envelope(solana_tx.b58encode(b"\x44" * 32)), MINT)
        self.assertEqual(crossed["reason"], "mint_mismatch")
        self.assertEqual(crossed["proven"], {})
        guessed = envelope(MINT, {"lp_burned_or_locked": True, "holder_count": 12, "fetched_at": 1, "score": 90, "rugs": 0})
        held = stalkchain_adapter.normalize(guessed, MINT)
        self.assertEqual(held["reason"], "response_fields_unpublished")
        self.assertEqual(held["freshness"], "unobserved")
        self.assertEqual(held["proven"], {})
        self.assertIn("lp_burned_or_locked", held["unmapped_keys"])
        self.assertIn("fetched_at", held["unmapped_keys"])
        self.assertFalse(held["fixture_is_mainnet_proof"])
        self.assertFalse(held["fdv_used_as_market_cap"])
        self.assertFalse(desk_loop.collect_holder_count(MINT, {"source": "stalkchain", "holder_count": 12, "fetched_at": NOW})["written"])

    def test_the_public_reader_posts_only_the_documented_stalkchain_tool(self):
        import io
        import json
        import urllib.error
        import urllib.request
        os.environ.pop("STALKCHAIN_API_KEY", None)
        self.addCleanup(lambda: os.environ.pop("STALKCHAIN_API_KEY", None))
        posts = []

        class _Response:
            def __init__(self, status, body):
                self.status = status
                self._body = body
            def read(self):
                return self._body
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        def urlopen(req, timeout=8):
            url = req.full_url
            if url.endswith("/token-boosts/latest/v1"):
                return _Response(200, json.dumps([{"chainId": "solana", "tokenAddress": MINT}]).encode())
            if "/token-pairs/" in url:
                return _Response(200, json.dumps(_pair()).encode())
            if "api.mainnet-beta.solana.com" in url:
                raw = bytearray(82)
                raw[44] = 6
                raw[45] = 1
                import base64
                body = {"jsonrpc": "2.0", "result": {"value": {"owner": solana_tx.TOKEN_PROGRAM, "data": [base64.b64encode(bytes(raw)).decode(), "base64"]}}}
                return _Response(200, json.dumps(body).encode())
            raise AssertionError(url)

        def build_opener(*handlers):
            self.assertTrue(any(item is stalkchain_adapter._NoRedirect or isinstance(item, stalkchain_adapter._NoRedirect) for item in handlers))
            class _Opener:
                def open(self, req, timeout=8):
                    posts.append(req)
                    if req.full_url.endswith("stalkchain_token_onchain") and credit["block"]:
                        raise urllib.error.HTTPError(req.full_url, 402, "payment", {}, io.BytesIO(b""))
                    if redirect["block"]:
                        raise urllib.error.HTTPError(req.full_url, 302, "moved", {"Location": "https://evil.example/"}, io.BytesIO(b""))
                    asked = json.loads(req.data.decode())["token"]
                    body = {"data": {"mint": asked, "marketCapBasis": "market_cap", "fdvUsd": 9, "lpBurnedPercent": 91}, "meta": {}}
                    return _Response(200, json.dumps(body).encode())
            return _Opener()

        credit = {"block": False}
        redirect = {"block": False}
        original_open = urllib.request.urlopen
        original_build = urllib.request.build_opener
        urllib.request.urlopen = urlopen
        urllib.request.build_opener = build_opener
        self.addCleanup(lambda: setattr(urllib.request, "urlopen", original_open))
        self.addCleanup(lambda: setattr(urllib.request, "build_opener", original_build))
        import stalkchain_adapter
        with self.assertRaises(RuntimeError):
            desk_loop.public_reader({"method": "POST", "url": stalkchain_adapter.URL + "stalkchain_token_onchain", "json": {"token": MINT}})
        quiet = desk_loop.run_discovery(desk_loop.public_reader, NOW)
        self.assertEqual(posts, [])
        self.assertEqual(quiet["kills"][0]["reason"], "stalkchain_credentials_missing")
        self.assertNotEqual(quiet["kills"][0]["reason"], "lp")
        os.environ["STALKCHAIN_API_KEY"] = "sc_dummy_not_real"
        other = solana_tx.b58encode(b"\x47" * 32)
        global_pair = _pair()
        def urlopen(req, timeout=8):
            url = req.full_url
            if url.endswith("/token-boosts/latest/v1"):
                return _Response(200, json.dumps([{"chainId": "solana", "tokenAddress": other}]).encode())
            if "/token-pairs/" in url:
                body = json.loads(json.dumps(global_pair))
                body[0]["baseToken"] = {"address": url.rsplit("/", 1)[-1]}
                return _Response(200, json.dumps(body).encode())
            if "api.mainnet-beta.solana.com" in url:
                raw = bytearray(82)
                raw[44] = 6
                raw[45] = 1
                import base64
                body = {"jsonrpc": "2.0", "result": {"value": {"owner": solana_tx.TOKEN_PROGRAM, "data": [base64.b64encode(bytes(raw)).decode(), "base64"]}}}
                return _Response(200, json.dumps(body).encode())
            raise AssertionError(url)
        urllib.request.urlopen = urlopen
        found = desk_loop.run_discovery(desk_loop.public_reader, NOW)
        self.assertEqual(len(posts), 4)
        self.assertEqual([item.full_url for item in posts], [stalkchain_adapter.URL + name for name in stalkchain_adapter.TOOLS])
        self.assertTrue(all(item.get_method() == "POST" for item in posts))
        self.assertTrue(all(json.loads(item.data.decode()) == {"token": other} for item in posts))
        self.assertTrue(all(item.get_header("Authorization") == "Bearer sc_dummy_not_real" for item in posts))
        self.assertNotIn("sc_dummy_not_real", json.dumps(found))
        kill = found["kills"][0]
        self.assertEqual(kill["reason"], "response_fields_unpublished")
        self.assertNotEqual(kill["reason"], "lp")
        self.assertFalse(kill["capability"]["response_verified"])
        self.assertEqual(kill["capability"]["analyst_formula"], "UNAPPROVED")
        self.assertIn("lpBurnedPercent", kill["stalkchain"]["unmapped_keys"])
        self.assertFalse(kill["stalkchain"]["fdv_used_as_market_cap"])
        posts.clear()
        credit["block"] = True
        limited = stalkchain_adapter.read_mint(other, desk_loop.public_reader)
        credit["block"] = False
        self.assertEqual(len(posts), 1)
        self.assertEqual(limited["reason"], "stalkchain_credit_limit")
        self.assertEqual(limited["capability"]["transport"], "credit_limited")
        self.assertNotIn("sc_dummy_not_real", json.dumps(limited))
        posts.clear()
        redirect["block"] = True
        moved = stalkchain_adapter.read_mint(other, desk_loop.public_reader)
        redirect["block"] = False
        self.assertEqual(len(posts), 1)
        self.assertEqual(moved["reason"], "redirect_refused")
        self.assertNotIn("evil.example", json.dumps(moved))
        self.assertNotIn("sc_dummy_not_real", json.dumps(moved))
        for bad in (
            {"method": "GET", "url": stalkchain_adapter.URL + "stalkchain_token_onchain", "json": {"token": MINT}},
            {"method": "POST", "url": "https://evil.example/api/v1/tools/stalkchain_token_onchain", "json": {"token": MINT}},
            {"method": "POST", "url": stalkchain_adapter.URL + "stalkchain_wallet_decode", "json": {"token": MINT}},
            {"method": "POST", "url": "https://data.stalkchain.com/api/v1/account", "json": {"token": MINT}},
            {"method": "POST", "url": stalkchain_adapter.URL + "stalkchain_token_onchain", "json": {"token": MINT, "extra": 1}},
            {"method": "POST", "url": "https://api.jup.ag/swap/v2/execute", "json": {"token": MINT}},
            {"method": "simulateTransaction", "transaction": "abc"},
            {"method": "getFeeForMessage", "message": "abc"},
        ):
            posted = len(posts)
            with self.assertRaises(RuntimeError) as refused:
                desk_loop.public_reader(bad)
            if bad.get("method") in {"simulateTransaction", "getFeeForMessage"}:
                self.assertEqual(str(refused.exception), "method_refused")
                self.assertEqual(len(posts), posted)
        observed = "CdmzERuZnYQwqwa2rxtRjsMjDU6y6qrM5ez6n2mxpump"
        generated = solana_tx.b58encode(b"\x11" * 32)
        self.assertTrue(stalkchain_adapter.canonical_mint(observed))
        self.assertTrue(stalkchain_adapter.canonical_mint(generated))
        posts.clear()
        for token in (observed, generated):
            desk_loop.public_reader({"method": "POST", "url": stalkchain_adapter.URL + "stalkchain_token_onchain", "json": {"token": token}})
        self.assertEqual([json.loads(item.data.decode())["token"] for item in posts], [observed, generated])
        rejected = [
            True,
            32,
            " " * 44,
            observed + " ",
            " " + observed,
            "0OIl" * 11,
            observed[:-1] + "\uFF21",
            solana_tx.b58encode(b"\x02" * 31),
            solana_tx.b58encode(b"\x02" * 33),
        ]
        for token in rejected:
            with self.assertRaises(RuntimeError) as caught:
                desk_loop.public_reader({"method": "POST", "url": stalkchain_adapter.URL + "stalkchain_token_onchain", "json": {"token": token}})
            self.assertEqual(str(caught.exception), "body_refused", token)
        self.assertEqual(len(posts), 2)
        skipped = stalkchain_adapter.read_mint(solana_tx.b58encode(b"\x02" * 31), desk_loop.public_reader)
        self.assertEqual(skipped["reason"], "body_refused")
        self.assertFalse(skipped["called"])
        self.assertEqual(skipped["capability"]["transport"], "request_rejected")
        self.assertEqual(skipped["capability"]["request_schema"], "rejected")
        self.assertEqual(len(posts), 2)
        self.assertFalse(desk_loop.scheduler_status()["armed"])

    def test_observed_mcp_measurements_do_not_become_a_pass(self):
        import copy
        import json
        from pathlib import Path as FixturePath
        import stalkchain_observed
        payload = json.loads((FixturePath(__file__).resolve().parent / "fixtures" / "nov8_observed_mcp_fixtures.json").read_text(encoding="utf-8"))
        mint = payload["mint"]
        bundle = {"kind": stalkchain_observed.KIND, "responses": payload["responses"]}
        measured = stalkchain_observed.map_fixture(bundle, mint)
        self.assertTrue(measured["ok"], measured)
        self.assertFalse(measured["live_ready"])
        self.assertFalse(measured["score_measured"])
        self.assertFalse(measured["fixture_is_mainnet_proof"])
        self.assertFalse(measured["rest_envelope_verified"])
        self.assertFalse(measured["creation_time_used_as_freshness"])
        self.assertEqual(measured["freshness"], "unknown")
        self.assertEqual(measured["dev_holding"]["value"], "0.38")
        self.assertEqual(measured["dev_holding"]["numeric_rule_result"], "within")
        self.assertFalse(measured["dev_holding"]["admitted"])
        self.assertEqual(measured["lp"]["lp_burned_percent"], "91")
        self.assertFalse(measured["lp"]["lp_burned_or_locked"])
        self.assertEqual(measured["lp"]["reason"], "lp_burn_incomplete")
        self.assertIsNone(measured["lp"]["lock_expiry"])
        self.assertFalse(measured["rugs"]["clean"])
        self.assertEqual(measured["rugs"]["tokens_launched"], 1)
        self.assertEqual(measured["rugs"]["reason"], "launch_count_is_not_rug_history")
        self.assertEqual(measured["bundle"]["dev_launch_percent"], "9.87")
        self.assertEqual(measured["bundle"]["nondev_launch_percent"], "0.45")
        self.assertEqual(measured["bundle"]["inclusive_percent"], "10.32")
        self.assertEqual(measured["bundle"]["numeric_rule_result"], "reject")
        self.assertFalse(measured["bundle"]["double_counted"])
        self.assertEqual(measured["holder_growth"]["snapshot_total_holders"], 1377)
        self.assertEqual(measured["holder_growth"]["onchain_holders_total"], 1588)
        self.assertEqual(measured["holder_growth"]["reason"], "t0_only")
        self.assertFalse(measured["holder_growth"]["cross_source_is_growth"])
        self.assertEqual(measured["uniques"]["unique_buyers_1h"], 1095)
        self.assertEqual(measured["uniques"]["unique_sellers_1h"], 731)
        self.assertFalse(measured["uniques"]["is_analyst_component"])
        self.assertEqual(measured["price_change_1h"]["value"], "401.13")
        self.assertEqual(measured["price_change_1h"]["numeric_rule_result"], "reject")
        self.assertFalse(measured["holder_quality"]["is_analyst_score"])
        self.assertIsNone(measured["analyst_score"])
        self.assertFalse(measured["analyst_formula"]["owner_approved"])
        self.assertEqual([item["name"] for item in measured["analyst_formula"]["required_inputs"]], list(memecoin_desk.COMPONENTS))
        self.assertFalse(measured["fdv_used_as_market_cap"])
        self.assertEqual(measured["market_cap_basis"], "fully diluted")
        series = stalkchain_observed.holder_growth_series([
            {"source": "snapshot", "mint": mint, "metric": "totalHolders", "captured_at": "2026-11-08T06:56:01.771Z", "value": 1377},
            {"source": "onchain", "mint": mint, "metric": "holdersTotal", "captured_at": "2026-11-08T07:56:01.771Z", "value": 1588},
        ])
        self.assertEqual(series["reason"], "series_mismatch")
        self.assertFalse(series["score_measured"])
        later = stalkchain_observed.holder_growth_series([
            {"source": "snapshot", "mint": mint, "metric": "totalHolders", "captured_at": "2026-11-08T06:56:01.771Z", "value": 1377},
            {"source": "snapshot", "mint": mint, "metric": "totalHolders", "captured_at": "2026-11-08T07:56:01.771Z", "value": 1400},
        ])
        self.assertEqual(later["delta"], 23)
        self.assertFalse(later["score_measured"])
        self.assertFalse(later["live_ready"])
        self.assertFalse(later["is_analyst_component"])
        shifted = copy.deepcopy(bundle)
        shifted["responses"]["onchain"]["data"]["mint"] = "other-mint"
        self.assertEqual(stalkchain_observed.map_fixture(shifted, mint)["reason"], "mint_mismatch")
        typed = copy.deepcopy(bundle)
        typed["responses"]["onchain"]["data"]["lpBurnedPercent"] = "91"
        typed_map = stalkchain_observed.map_fixture(typed, mint)
        self.assertTrue(typed_map["ok"], typed_map)
        self.assertIsNone(typed_map["lp"]["lp_burned_percent"])
        self.assertFalse(typed_map["lp"]["lp_burned_or_locked"])
        doubled = copy.deepcopy(bundle)
        doubled["responses"]["early_buyers"]["data"]["launch"]["buys"][1]["supplyPercent"] = 9.87
        self.assertEqual(stalkchain_observed.map_fixture(doubled, mint)["bundle"]["reason"], "bundle_unjoined")
        self.assertIsNone(stalkchain_observed.map_fixture(doubled, mint)["bundle"]["inclusive_percent"])

        def transport(request):
            if request.get("kind") == "observed_mcp":
                self.assertEqual(request["mint"], mint)
                return {"status": 200, "body": bundle}
            if request.get("method") == "POST":
                return {"status": 200, "body": {"data": {"mint": mint, "lp_burned_or_locked": True, "marketCapBasis": "fully diluted", "fdvUsd": 1}, "meta": {}}}
            if str(request.get("url", "")).endswith("/token-boosts/latest/v1"):
                return {"status": 200, "body": [{"chainId": "solana", "tokenAddress": mint}]}
            if request.get("kind") == "rpc":
                raw = bytearray(82)
                raw[45] = 1
                return {"status": 200, "body": {"result": {"value": {"owner": solana_tx.TOKEN_PROGRAM, "data": [__import__("base64").b64encode(bytes(raw)).decode(), "base64"]}}}}
            body = _pair()
            body[0]["baseToken"] = {"address": mint}
            return {"status": 200, "body": body}

        found = desk_loop.run_discovery(transport, NOW)
        kill = next(item for item in found["kills"] if item.get("mint") == mint)
        self.assertEqual(kill["reason"], "stalkchain_credentials_missing")
        self.assertEqual(kill["stage"], "scout_passed")
        self.assertEqual(kill["capability"]["transport"], "unconfigured")
        self.assertFalse(kill["score_measured"])
        self.assertFalse(kill["fixture_is_mainnet_proof"])
        self.assertFalse(found["armed"])
        self.assertFalse(found["live_ready"])
        observed = kill["observed"]
        self.assertEqual(observed["bundle"]["inclusive_percent"], "10.32")
        self.assertEqual(observed["price_change_1h"]["numeric_rule_result"], "reject")
        self.assertFalse(observed["live_ready"])
        self.assertIsNone(observed["analyst_score"])
        self.assertFalse(observed["admits_filter"])
        self.assertFalse(observed["admits_analyst"])

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
        self.assertEqual({item.get("reason") for item in found["kills"] if item.get("mint")}, {"stalkchain_credentials_missing"})
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
        self.assertEqual(unknown["reason"], "stalkchain_credentials_missing")
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

    def test_predecessor_database_replaces_the_old_lineage_triggers(self):
        import db
        import json
        import shutil
        import sqlite3
        import subprocess
        import sys
        import tempfile
        import pyth_sponsored
        root = Path(__file__).resolve().parents[1]
        predecessor = "60932e4ce163b3a12df1f92b3224546a947af7e2"
        source = tempfile.mkdtemp()
        built = tempfile.mkdtemp()
        failed = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(source, ignore_errors=True))
        self.addCleanup(lambda: shutil.rmtree(built, ignore_errors=True))
        self.addCleanup(lambda: shutil.rmtree(failed, ignore_errors=True))
        archive = subprocess.run(["git", "archive", predecessor], cwd=root, check=True, capture_output=True)
        subprocess.run(["tar", "-x"], cwd=source, check=True, input=archive.stdout)
        script = r"""
import json, os, sqlite3
import db
import memecoin_desk
db.init_db()
memecoin_desk.loss_totals()
path = os.path.join(os.environ["DESK_DATA_DIR"], "desk.db")
conn = sqlite3.connect(path)
conn.row_factory = sqlite3.Row
now = 1800000000
kinds = (
    ("legacy", "live", "legacy_default", "8.00", 11),
    ("server", "live", "server", "4.00", 12),
    ("paper", "paper", "server", "3.00", 13),
    ("synthetic", "synthetic", "server", "2.00", 14),
)
for name, ledger, provenance, reserved, cost in kinds:
    conn.execute(
        "INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, created_at, updated_at, ledger, ledger_provenance) VALUES (?,?,?,?,?,?,?,?)",
        ("case-" + name, "mint-" + name, "filled", json.dumps({"reserved_usd": reserved}), now, now, ledger, provenance),
    )
    conn.execute(
        "INSERT INTO memecoin_entry_receipts (signature, case_id, message_sha256, slot, qty, cost_basis_lamports, network_fee_lamports, dex_fee_lamports, fee_usd, failed, ledger, ledger_provenance) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("entry-" + name, "case-" + name, "m", 1, 10, cost, 1, 1, "0.01", 0, ledger, provenance),
    )
    conn.execute(
        "INSERT INTO memecoin_exit_receipts (signature, case_id, message_sha256, slot, fee_usd, qty, ledger, ledger_provenance) VALUES (?,?,?,?,?,?,?,?)",
        ("exit-" + name, "case-" + name, "m", 1, "0.01", cost, ledger, provenance),
    )
    conn.execute(
        "INSERT INTO memecoin_refunds (signature, case_id, rent_lamports, network_fee_lamports, fee_usd, released, ledger, ledger_provenance) VALUES (?,?,?,?,?,?,?,?)",
        ("refund-" + name, "case-" + name, cost, 1, "0.01", 0, ledger, provenance),
    )
    conn.execute(
        "INSERT INTO memecoin_risk_intents (id, case_id, ledger, ledger_provenance, entry_signature, action, pool, slot, mark_price_usd, entry_price_usd, stop_price_usd, qty, basis_cost_lamports, basis_decimals, evidence_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("intent-" + name, "case-" + name, ledger, provenance, "entry-" + name, "stop", "pool", 1, "1", "1", "0.94", 10, cost, 6, "{}", now),
    )
conn.execute(
    "INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, created_at, updated_at) VALUES ('case-fresh', 'mint-fresh', 'analyst_passed', ?, ?, ?)",
    (json.dumps({"price_usd": "1.00"}), now, now),
)
conn.execute(
    "INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, created_at, updated_at) VALUES ('case-paper-fresh', 'mint-paper-fresh', 'analyst_passed', ?, ?, ?)",
    (json.dumps({"price_usd": "1.00"}), now, now),
)
conn.commit()
saved = [(row["name"], row["sql"]) for row in conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'memecoin%' ORDER BY name")]
body = dict(saved)["memecoin_entry_receipts_provenance_update"]
assert "OLD.ledger_provenance = 'server'" in body
assert "OLD.ledger_provenance IS NOT NULL" not in body
assert "memecoin_cases_lineage" not in dict(saved)
conn.execute("BEGIN IMMEDIATE")
for name, _sql in saved:
    conn.execute("DROP TRIGGER " + name)
conn.execute(
    "INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, created_at, updated_at, ledger, ledger_provenance) VALUES ('case-unknown', 'mint-unknown', 'filled', ?, ?, ?, 'unknown', 'unknown')",
    (json.dumps({"reserved_usd": "1.50"}), now, now),
)
conn.execute(
    "INSERT INTO memecoin_entry_receipts (signature, case_id, message_sha256, slot, qty, cost_basis_lamports, network_fee_lamports, dex_fee_lamports, fee_usd, failed, ledger, ledger_provenance) VALUES ('entry-unknown', 'case-unknown', 'm', 1, 10, 15, 1, 1, '0.01', 0, 'unknown', 'unknown')"
)
conn.execute(
    "INSERT INTO memecoin_exit_receipts (signature, case_id, message_sha256, slot, fee_usd, qty, ledger, ledger_provenance) VALUES ('exit-unknown', 'case-unknown', 'm', 1, '0.01', 15, 'unknown', 'unknown')"
)
conn.execute(
    "INSERT INTO memecoin_refunds (signature, case_id, rent_lamports, network_fee_lamports, fee_usd, released, ledger, ledger_provenance) VALUES ('refund-unknown', 'case-unknown', 15, 1, '0.01', 0, 'unknown', 'unknown')"
)
conn.execute(
    "INSERT INTO memecoin_risk_intents (id, case_id, ledger, ledger_provenance, entry_signature, action, pool, slot, mark_price_usd, entry_price_usd, stop_price_usd, qty, basis_cost_lamports, basis_decimals, evidence_json, created_at) VALUES ('intent-unknown', 'case-unknown', 'unknown', 'unknown', 'entry-unknown', 'stop', 'pool', 1, '1', '1', '0.94', 10, 15, 6, '{}', ?)",
    (now,),
)
for name, sql in saved:
    conn.execute(sql)
conn.commit()
restored = [(row["name"], row["sql"]) for row in conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'memecoin%' ORDER BY name")]
assert restored == saved
conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
conn.commit()
conn.close()
"""
        env = os.environ.copy()
        env["DESK_DATA_DIR"] = built
        env["PYTHONPATH"] = source + os.pathsep + env.get("PYTHONPATH", "")
        env.pop("STALKCHAIN_API_KEY", None)
        ran = subprocess.run([sys.executable, "-c", script], cwd=source, env=env, capture_output=True, text=True)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        original_dir = os.environ["DESK_DATA_DIR"]
        shutil.copy(Path(built) / "desk.db", Path(failed) / "desk.db")

        def trigger_sql(directory):
            conn = sqlite3.connect(Path(directory) / "desk.db")
            try:
                return {row[0]: row[1] for row in conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'memecoin%'")}
            finally:
                conn.close()

        before = trigger_sql(built)
        self.assertIn("OLD.ledger_provenance = 'server'", before["memecoin_entry_receipts_provenance_update"])
        real_replace = memecoin_desk._replace_lineage_triggers

        def explode(conn):
            real_replace(conn)
            raise RuntimeError("lineage upgrade failed")

        memecoin_desk._replace_lineage_triggers = explode
        os.environ["DESK_DATA_DIR"] = failed
        try:
            with self.assertRaises(RuntimeError):
                memecoin_desk.loss_totals()
        finally:
            memecoin_desk._replace_lineage_triggers = real_replace
            os.environ["DESK_DATA_DIR"] = original_dir
        self.assertEqual(trigger_sql(failed), before)
        rolled = sqlite3.connect(Path(failed) / "desk.db")
        try:
            rolled.execute("BEGIN")
            rolled.execute("UPDATE memecoin_entry_receipts SET ledger_provenance = 'server' WHERE signature = 'entry-legacy'")
            rolled.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                rolled.execute("UPDATE memecoin_entry_receipts SET ledger_provenance = 'legacy_default' WHERE signature = 'entry-server'")
            rolled.rollback()
        finally:
            rolled.close()

        desk = Path(original_dir) / "desk.db"
        for suffix in ("", "-wal", "-shm"):
            extra = Path(str(desk) + suffix)
            if extra.exists():
                extra.unlink()
        shutil.copy(Path(built) / "desk.db", desk)
        import threading
        errors = []

        def open_latest():
            try:
                memecoin_desk.loss_totals()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=open_latest) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        upgraded = trigger_sql(original_dir)
        for name in (
            "memecoin_entry_receipts_provenance_update",
            "memecoin_exit_receipts_provenance_update",
            "memecoin_refunds_provenance_update",
            "memecoin_risk_intents_provenance_update",
            "memecoin_cases_lineage",
        ):
            self.assertIn("OLD.ledger_provenance IS NOT NULL", upgraded[name], name)
            self.assertNotIn("OLD.ledger_provenance = 'server'", upgraded[name], name)
        self.assertEqual(db.get_setting("memecoin_lineage_trigger"), "14")
        self.assertEqual(db.exclusive(lambda conn: conn.execute("PRAGMA user_version").fetchone()[0]), 12)
        costs = {row["signature"]: row["cost_basis_lamports"] for row in db.query("SELECT signature, cost_basis_lamports FROM memecoin_entry_receipts")}
        self.assertEqual(costs, {"entry-legacy": 11, "entry-server": 12, "entry-paper": 13, "entry-synthetic": 14, "entry-unknown": 15})
        reserves = {row["id"]: json.loads(row["snapshot_json"])["reserved_usd"] for row in db.query("SELECT id, snapshot_json FROM memecoin_cases WHERE id LIKE 'case-%' AND id NOT LIKE 'case-%fresh'")}
        self.assertEqual(reserves, {"case-legacy": "8.00", "case-server": "4.00", "case-paper": "3.00", "case-synthetic": "2.00", "case-unknown": "1.50"})
        intents = {row["id"]: row["basis_cost_lamports"] for row in db.query("SELECT id, basis_cost_lamports FROM memecoin_risk_intents")}
        self.assertEqual(intents, {"intent-legacy": 11, "intent-server": 12, "intent-paper": 13, "intent-synthetic": 14, "intent-unknown": 15})
        edits = (
            "UPDATE memecoin_entry_receipts SET ledger_provenance = 'server' WHERE signature = 'entry-legacy'",
            "UPDATE memecoin_entry_receipts SET ledger = 'paper' WHERE signature = 'entry-legacy'",
            "UPDATE memecoin_entry_receipts SET ledger = 'live' WHERE signature = 'entry-paper'",
            "UPDATE memecoin_entry_receipts SET ledger = 'live' WHERE signature = 'entry-synthetic'",
            "UPDATE memecoin_entry_receipts SET ledger_provenance = 'server' WHERE signature = 'entry-unknown'",
            "UPDATE memecoin_entry_receipts SET ledger_provenance = 'legacy_default' WHERE signature = 'entry-server'",
            "UPDATE memecoin_exit_receipts SET ledger_provenance = 'server' WHERE signature = 'exit-legacy'",
            "UPDATE memecoin_exit_receipts SET ledger = 'paper' WHERE signature = 'exit-server'",
            "UPDATE memecoin_refunds SET ledger_provenance = 'server' WHERE signature = 'refund-legacy'",
            "UPDATE memecoin_refunds SET ledger = 'live' WHERE signature = 'refund-synthetic'",
            "UPDATE memecoin_risk_intents SET ledger_provenance = 'server' WHERE id = 'intent-legacy'",
            "UPDATE memecoin_risk_intents SET case_id = 'other' WHERE id = 'intent-server'",
            "UPDATE memecoin_cases SET ledger_provenance = 'server' WHERE id = 'case-legacy'",
            "UPDATE memecoin_cases SET ledger = 'paper' WHERE id = 'case-server'",
            "UPDATE memecoin_cases SET ledger = 'live', ledger_provenance = 'server' WHERE id = 'case-unknown'",
        )
        for statement in edits:
            with self.assertRaises(sqlite3.IntegrityError, msg=statement):
                db.execute(statement)
        self.assertEqual({row["signature"]: row["cost_basis_lamports"] for row in db.query("SELECT signature, cost_basis_lamports FROM memecoin_entry_receipts")}, costs)
        db.execute("UPDATE phantom_control SET value = '0' WHERE key = 'halt'")
        previous = pyth_sponsored.sol_leg
        pyth_sponsored.sol_leg = lambda now: {"fresh": True, "used": True, "price": 15_000_000_000, "conf": 1, "expo": -8}
        rent = {"ok": True, "lamports": 1_488_440, "count": 1, "slot": 1, "tip_slot": 2, "genesis": "g", "mint_program": "spl"}
        try:
            live = memecoin_desk.protect("case-fresh", now=NOW, rent_proof=rent)
            paper = memecoin_desk.protect("case-paper-fresh", now=NOW, rent_proof=rent, ledger="paper")
        finally:
            pyth_sponsored.sol_leg = previous
        self.assertTrue(live["ok"], live)
        self.assertTrue(paper["ok"], paper)
        self.assertEqual(db.query("SELECT ledger, ledger_provenance FROM memecoin_cases WHERE id = 'case-fresh'")[0]["ledger"], "live")
        self.assertEqual(db.query("SELECT ledger_provenance FROM memecoin_cases WHERE id = 'case-fresh'")[0]["ledger_provenance"], "server")
        self.assertEqual(db.query("SELECT ledger, ledger_provenance FROM memecoin_cases WHERE id = 'case-paper-fresh'")[0]["ledger"], "paper")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE memecoin_cases SET ledger = 'live' WHERE id = 'case-paper-fresh'")
        db.execute(
            "INSERT INTO memecoin_entry_receipts (signature, case_id, message_sha256, slot, qty, cost_basis_lamports, network_fee_lamports, dex_fee_lamports, fee_usd, failed, ledger, ledger_provenance) VALUES ('entry-new', 'case-fresh', 'new', 2, 10, 21, 1, 1, '0.01', 0, 'live', 'server')"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO memecoin_entry_receipts (signature, case_id, message_sha256, slot, qty, cost_basis_lamports, network_fee_lamports, dex_fee_lamports, fee_usd, failed, ledger) VALUES ('entry-omitted', 'case-fresh', 'new', 2, 10, 22, 1, 1, '0.01', 0, 'live')"
            )
        memecoin_desk.loss_totals()
        db.init_db()
        memecoin_desk.loss_totals()
        again = trigger_sql(original_dir)
        self.assertEqual(again["memecoin_entry_receipts_provenance_update"], upgraded["memecoin_entry_receipts_provenance_update"])
        self.assertEqual(db.get_setting("memecoin_lineage_trigger"), "14")
        self.assertEqual(db.query("SELECT cost_basis_lamports FROM memecoin_entry_receipts WHERE signature = 'entry-legacy'")[0]["cost_basis_lamports"], 11)
        self.assertEqual(db.query("SELECT cost_basis_lamports FROM memecoin_entry_receipts WHERE signature = 'entry-new'")[0]["cost_basis_lamports"], 21)
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE memecoin_entry_receipts SET ledger_provenance = 'server' WHERE signature = 'entry-legacy'")

    def test_the_server_loop_binds_quote_expiry_to_the_reserved_message(self):
        import base64
        import json
        import sqlite3
        import threading
        from tests.test_jupiter_route_v2 import WHEN, _metas, _pool, _prices, _route, _tx
        import db
        import jupiter_route_v2
        import pyth_sponsored
        import solana_inspect
        import solana_jupiter
        import solana_whirlpool
        import solana_whirlpool_quote
        mint = solana_tx.b58encode(bytes([96]) * 32)
        values, accounts = _pool(solana_tx.NATIVE_MINT, mint)
        raw_pool = bytearray(accounts[values["pool"]][1])
        config = b"\x45" * 32
        raw_pool[8:40] = config
        fields = solana_whirlpool.pool_fields(bytes(raw_pool))
        seed = bytes(raw_pool[solana_whirlpool._TICK_SPACING + 2:solana_whirlpool._TICK_SPACING + 4])
        derived, bump = solana_tx.find_program_address(
            [b"whirlpool", config, solana_tx.b58decode(fields[0]), solana_tx.b58decode(fields[2]), seed],
            solana_tx.b58decode(solana_whirlpool.PROGRAM_ID),
        )
        raw_pool[40] = bump
        pool = solana_tx.b58encode(derived)
        accounts.pop(values["pool"])
        accounts[pool] = (solana_whirlpool.PROGRAM_ID, bytes(raw_pool))
        for key in list(values["ticks"]):
            accounts.pop(key, None)
        state = solana_whirlpool.pool_state(bytes(raw_pool))
        base = solana_whirlpool.start_tick_index(state["tick_current_index"], state["tick_spacing"])
        step = solana_whirlpool.TICK_ARRAY_SIZE * state["tick_spacing"]
        tick_addresses = []
        for start in [base - i * step for i in range(3)] + [base + i * step for i in range(3)]:
            address = solana_whirlpool.tick_array_address(pool, start)
            accounts[address] = (solana_whirlpool.PROGRAM_ID, solana_whirlpool.reference_tick_array(pool, start))
            tick_addresses.append(address)
        values["ticks"] = tick_addresses[:3]
        values["pool"] = pool
        values["oracle"] = solana_whirlpool.oracle_address(pool)
        for vault, token_mint in ((fields[1], fields[0]), (fields[3], fields[2])):
            token = bytearray(solana_whirlpool.reference_token_account(token_mint, pool))
            token[108] = 1
            accounts[vault] = (solana_tx.TOKEN_PROGRAM, bytes(token))
        accounts[solana_whirlpool.oracle_address(pool)] = (solana_whirlpool.PROGRAM_ID, solana_whirlpool.oracle_account(pool))
        wallet = __import__("phantom_transport").REQUIRED_WALLET
        accounts.pop(solana_tx.associated_token_address(wallet, solana_tx.NATIVE_MINT, solana_tx.TOKEN_PROGRAM), None)
        accounts.pop(solana_tx.associated_token_address(wallet, mint, solana_tx.TOKEN_PROGRAM), None)
        chain = {
            "genesis": solana_inspect.MAINNET_GENESIS,
            "blockhash": solana_tx.b58encode(b"\x46" * 32),
            "latest_blockhash": solana_tx.b58encode(b"\x48" * 32),
            "last_valid": 1_000_000,
            "latest_last": 1_000_200,
            "height": 999_000,
            "malformed": False,
            "valid": True,
            "publish_expiry": True,
            "expire_at": None,
            "router": "metis",
            "side": "buy",
        }
        opened = memecoin_desk.scout({
            "mint": mint, "source": "dexscreener", "fetched_at": WHEN,
            "pair_created_at": int((WHEN - 3600) * 1000), "market_cap_usd": "1000000",
            "liquidity_usd": "250000", "price_usd": "1", "price_change_h1": "1",
        }, now=WHEN)
        case_id = opened["case_id"]
        db.execute("UPDATE memecoin_cases SET stage = 'analyst_passed' WHERE id = ?", (case_id,))
        previous = pyth_sponsored.sol_leg
        pyth_sponsored.sol_leg = lambda now: {"fresh": True, "used": True, "price": 15_000_000_000, "conf": 1, "expo": -8}
        try:
            protected = memecoin_desk.protect(case_id, now=WHEN, rent_proof={"ok": True, "lamports": solana_tx.TOKEN_ACCOUNT_RENT, "count": 1, "slot": 2_000_000, "tip_slot": 2_000_028, "genesis": solana_inspect.MAINNET_GENESIS, "mint_program": solana_tx.TOKEN_PROGRAM})
        finally:
            pyth_sponsored.sol_leg = previous
        self.assertEqual(protected["stage"], "protected", protected)
        db.set_setting("memecoin_scheduler_authorized", "1", "test")
        db.execute("UPDATE phantom_control SET value = '0' WHERE key = 'halt'")

        def message(side: str, amount: int, blockhash: str):
            a_to_b = side == "buy"
            starts = [base - i * step for i in range(3)] if a_to_b else [base + i * step for i in range(3)]
            arrays = []
            for start in starts:
                parsed = solana_whirlpool.tick_array_state(accounts[solana_whirlpool.tick_array_address(pool, start)][1])
                arrays.append({"start_tick_index": parsed["start_tick_index"], "ticks": parsed["ticks"]})
            estimate = solana_whirlpool_quote.quote_exact_in(state, arrays, amount, a_to_b, 0, 0, 2_000_000)["token_est_out"]
            declared = jupiter_route_v2.encoded_slippage(estimate)["slippage_bps"]
            source = solana_tx.NATIVE_MINT if a_to_b else mint
            dest = mint if a_to_b else solana_tx.NATIVE_MINT
            saved_ticks = values["ticks"]
            values["ticks"] = tick_addresses[:3] if a_to_b else tick_addresses[3:]
            try:
                built = _tx(_metas(source, dest, values), _route(estimate, declared, a_to_b, amount=amount), side=side, blockhash=blockhash)
            finally:
                values["ticks"] = saved_ticks
            return built, declared, estimate

        def transport(request):
            method = request.get("method")
            if request.get("kind") == "http":
                url = request.get("url") or ""
                if url.endswith("/token-boosts/latest/v1"):
                    if chain.get("boosts_fail"):
                        raise RuntimeError("boosts down")
                    return {"status": 200, "body": [{"chainId": "solana", "tokenAddress": mint}]}
                if "/token-pairs/" in url:
                    return {"status": 200, "body": [{
                        "chainId": "solana", "dexId": "whirlpool", "pairAddress": pool,
                        "baseToken": {"address": mint}, "priceUsd": "1", "marketCap": 1_000_000,
                        "liquidity": {"usd": 250_000}, "pairCreatedAt": int((WHEN - 3600) * 1000),
                        "priceChange": {"h1": 1},
                    }]}
                if "/swap/v2/order" in url:
                    amount = int(url.split("amount=")[1].split("&")[0])
                    side = "buy" if "outputMint=%s" % mint in url or url.split("outputMint=")[1].split("&")[0] == mint else "exit"
                    if chain["side"] == "exit":
                        side = "exit"
                    tx, _declared, _estimate = message(side, amount if side == "exit" else 1_000_000, chain.get("message_blockhash") or chain["blockhash"])
                    body = {"router": chain["router"], "inAmount": str(amount), "outAmount": "1", "transaction": None if chain.get("omit_tx") else tx}
                    if chain["publish_expiry"] and not chain["malformed"]:
                        body["lastValidBlockHeight"] = str(chain["last_valid"])
                    if chain["malformed"]:
                        body["lastValidBlockHeight"] = "soon"
                    if chain["expire_at"] is not None:
                        body["expireAt"] = str(chain["expire_at"])
                    return {"status": 200, "body": body}
                if "/swap/v2/build" in url:
                    amount = int(url.split("amount=")[1].split("&")[0])
                    declared = int(url.split("slippageBps=")[1].split("&")[0])
                    output = url.split("outputMint=")[1].split("&")[0]
                    a_to_b = output == mint
                    starts = [base - i * step for i in range(3)] if a_to_b else [base + i * step for i in range(3)]
                    arrays = []
                    for start in starts:
                        parsed = solana_whirlpool.tick_array_state(accounts[solana_whirlpool.tick_array_address(pool, start)][1])
                        arrays.append({"start_tick_index": parsed["start_tick_index"], "ticks": parsed["ticks"]})
                    estimate = solana_whirlpool_quote.quote_exact_in(state, arrays, amount, a_to_b, 0, 0, 2_000_000)["token_est_out"]
                    data = _route(estimate, declared, a_to_b, amount=amount)
                    return {"status": 200, "body": {"otherAmountThreshold": str(estimate), "routePlan": [], "swapInstruction": {
                        "programId": "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
                        "accounts": [],
                        "data": base64.b64encode(data).decode("ascii"),
                    }}}
                return {"status": 200, "body": []}
            if method == "getGenesisHash":
                return {"status": 200, "body": {"result": chain["genesis"]}}
            if method == "getLatestBlockhash":
                return {"status": 200, "body": {"result": {"context": {"slot": chain.get("latest_slot", 2_000_028)}, "value": {"blockhash": chain["latest_blockhash"], "lastValidBlockHeight": chain["latest_last"]}}}}
            if method == "isBlockhashValid":
                if chain.get("validity_context") == "missing":
                    return {"status": 200, "body": {"result": {"value": True}}}
                slot = chain.get("validity_slot", 2_000_028)
                if chain["valid"] == "missing":
                    return {"status": 200, "body": {"result": {}}}
                if chain["valid"] == "malformed":
                    return {"status": 200, "body": {"result": {"context": {"slot": slot}, "value": "yes"}}}
                return {"status": 200, "body": {"result": {"context": {"slot": slot}, "value": request.get("blockhash") == chain["blockhash"] and chain["valid"] is True}}}
            if method == "getBlockHeight":
                result = "soon" if chain["malformed"] else chain["height"]
                return {"status": 200, "body": {"result": result}}
            if method == "getSlot":
                return {"status": 200, "body": {"result": 2_000_028}}
            if method == "getFeeForMessage":
                chain.setdefault("fee_messages", []).append(request.get("message"))
                mode = chain.get("fee_mode") or "clear"
                echoed = request.get("message")
                if mode == "null":
                    value = None
                elif mode == "value":
                    value = chain.get("fee_value")
                elif mode == "malformed":
                    return {"status": 200, "body": {"result": {"context": {"slot": "soon"}, "value": "5000"}, "message": echoed}}
                elif mode == "stale":
                    return {"status": 200, "body": {"result": {"context": {"slot": 1}, "value": 5000}, "message": echoed}}
                elif mode == "mismatch":
                    return {"status": 200, "body": {"result": {"context": {"slot": 2_000_028}, "value": 5000}, "message": "other-message"}}
                elif mode == "missing":
                    return {"status": 200, "body": {"result": {"value": 5000}}}
                else:
                    value = 5000
                return {"status": 200, "body": {"result": {"context": {"slot": 2_000_028}, "value": value}, "message": echoed}}
            if method == "simulateTransaction":
                mode = chain.get("preflight_mode")
                if not mode:
                    raise AssertionError(method)
                chain["sim_calls"] = chain.get("sim_calls", 0) + 1
                chain["last_sim"] = {key: request.get(key) for key in ("transaction", "sigVerify", "replaceRecentBlockhash", "encoding", "commitment", "minContextSlot", "accounts")}
                if mode == "context_missing":
                    return {"status": 200, "body": {"result": {"value": {"err": None, "unitsConsumed": 1}}}}
                if mode == "context_malformed":
                    return {"status": 200, "body": {"result": {"context": {"slot": "soon"}, "value": {"err": None, "unitsConsumed": 1}}}}
                if mode == "stale":
                    return {"status": 200, "body": {"result": {"context": {"slot": 1}, "value": {"err": None, "unitsConsumed": 1, "accounts": [None]}}}}
                if mode == "malformed":
                    return {"status": 200, "body": {"result": {"context": {"slot": 2_000_028}, "value": {"unitsConsumed": 1}}}}
                if mode == "err":
                    return {"status": 200, "body": {"result": {"context": {"slot": 2_000_028}, "value": {"err": {"InstructionError": [0, {"Custom": 1}]}, "unitsConsumed": 1, "accounts": [None]}}}}
                decoded_sim = solana_tx.decode_transaction(base64.b64decode(request["transaction"]))
                compute = [bytes(item.data) for item in decoded_sim.instructions if decoded_sim.static_keys[item.program_index] == solana_tx.COMPUTE_BUDGET]
                fee = jupiter_route_v2.network_fee(compute, decoded_sim.required_signatures)["network_lamports"]
                post = 50_000_000 - fee
                account = {"lamports": post, "owner": solana_tx.SYSTEM_PROGRAM, "data": ["", "base64"], "executable": False, "rentEpoch": 0}
                data_overrides = {
                    "bad_alphabet": ["!!!!", "base64"],
                    "bad_padding": ["YQ", "base64"],
                    "bad_type": [True, "base64"],
                    "bad_encoding": ["", True],
                    "nonempty_system": ["AA==", "base64"],
                }
                if mode == "fee_only":
                    account = {"lamports": post}
                elif mode == "rent_constant":
                    account["lamports"] = post - solana_tx.TOKEN_ACCOUNT_RENT
                elif mode == "malformed_owner":
                    account = {"lamports": post, "owner": True, "data": ["", "base64"], "executable": False, "rentEpoch": 0}
                elif mode == "malformed_data":
                    account = {"lamports": post, "owner": solana_tx.SYSTEM_PROGRAM, "data": "raw", "executable": False, "rentEpoch": 0}
                elif mode == "executable_system":
                    account["executable"] = True
                elif mode in data_overrides:
                    account["data"] = data_overrides[mode]
                return {"status": 200, "body": {"result": {"context": {"slot": 2_000_028}, "value": {"err": None, "unitsConsumed": 1000, "accounts": [account]}}}}
            if method == "getAccountInfo" and request.get("purpose") == "fee_payer":
                shape = chain.get("fee_payer_shape")
                payer_data = {
                    "bad_alphabet": ["!!!!", "base64"],
                    "bad_padding": ["YQ=", "base64"],
                    "bad_type": ["", "base64"],
                    "nonempty_system": ["AQ==", "base64"],
                }
                if shape == "lamports_only":
                    payer = {"lamports": 50_000_000}
                elif shape == "bad_type":
                    payer = {"lamports": 50_000_000, "owner": solana_tx.SYSTEM_PROGRAM, "data": True, "executable": False, "rentEpoch": 0}
                else:
                    payer = {"lamports": 50_000_000, "owner": solana_tx.SYSTEM_PROGRAM, "data": payer_data.get(shape, ["", "base64"]), "executable": False, "rentEpoch": 0}
                return {"status": 200, "body": {"result": {"context": {"slot": 2_000_028}, "value": payer}}}
            if method == "getMinimumBalanceForRentExemption":
                return {"status": 200, "body": {"result": solana_tx.TOKEN_ACCOUNT_RENT}}
            if method == "getAccountInfo":
                raw = bytearray(82)
                raw[44] = 6
                raw[45] = 1
                return {"status": 200, "body": {"result": {"context": {"slot": 2_000_000}, "value": {"owner": solana_tx.TOKEN_PROGRAM, "data": [base64.b64encode(bytes(raw)).decode(), "base64"]}}}}
            if method == "getMultipleAccounts":
                keys = request.get("accounts") or []
                if keys == [pyth_sponsored.SOL_ACCOUNT, pyth_sponsored.USDC_ACCOUNT]:
                    return _prices()(request)
                value = []
                for key in keys:
                    if key not in accounts:
                        value.append(None)
                        continue
                    owner, blob = accounts[key]
                    value.append({"owner": owner, "data": [base64.b64encode(blob).decode(), "base64"]})
                return {"status": 200, "body": {"result": {"context": {"slot": 2_000_000}, "value": value}}}
            raise AssertionError(method)

        evidence = {"dex_id": "whirlpool", "pair_address": pool}
        chain["omit_tx"] = True
        missing = desk_loop._screen_quote(transport, WHEN, mint, evidence)
        self.assertEqual(missing["reason"], None, missing)
        self.assertFalse(missing["quote_expiry_proven"])
        self.assertFalse(missing["reserved"])
        self.assertEqual(missing["reserve_reason"], "provider_transaction_unpublished")
        self.assertIsNone(missing["internal_time_limit"])
        self.assertIsNone(memecoin_desk.view(case_id)["snapshot"].get("entry_inflight"))
        chain["omit_tx"] = False
        chain["publish_expiry"] = False
        unpublished = desk_loop._screen_quote(transport, WHEN, mint, evidence)
        self.assertEqual(unpublished["reserve_reason"], "route_expiry_missing")
        self.assertIsNone(memecoin_desk.view(case_id)["snapshot"].get("entry_inflight"))
        chain["publish_expiry"] = True
        chain["malformed"] = True
        malformed = desk_loop._screen_quote(transport, WHEN, mint, evidence)
        self.assertEqual(malformed["reserve_reason"], "block_height_malformed")
        chain["malformed"] = False
        chain["genesis"] = "not-mainnet"
        wrong_genesis = desk_loop._screen_quote(transport, WHEN, mint, evidence)
        self.assertEqual(wrong_genesis["reason"], "genesis")
        self.assertFalse(wrong_genesis.get("reserved") is True)
        chain["genesis"] = solana_inspect.MAINNET_GENESIS
        chain["height"] = 1_000_001
        self.assertEqual(desk_loop._screen_quote(transport, WHEN, mint, evidence)["reserve_reason"], "blockhash_expired")
        chain["height"] = 999_000
        saved_hash = chain["blockhash"]
        chain["message_blockhash"] = solana_tx.b58encode(b"\x47" * 32)
        self.assertEqual(desk_loop._screen_quote(transport, WHEN, mint, evidence)["reserve_reason"], "blockhash_invalid")
        chain.pop("message_blockhash")
        chain["valid"] = False
        self.assertEqual(desk_loop._screen_quote(transport, WHEN, mint, evidence)["reserve_reason"], "blockhash_invalid")
        chain["valid"] = "malformed"
        self.assertEqual(desk_loop._screen_quote(transport, WHEN, mint, evidence)["reserve_reason"], "blockhash_validity_malformed")
        chain["valid"] = "missing"
        self.assertEqual(desk_loop._screen_quote(transport, WHEN, mint, evidence)["reserve_reason"], "blockhash_validity_missing")
        chain["valid"] = True
        chain["router"] = "jupiterz"
        chain["publish_expiry"] = False
        self.assertEqual(desk_loop._screen_quote(transport, WHEN, mint, evidence)["reserve_reason"], "route_expiry_missing")
        chain["router"] = "metis"
        chain["publish_expiry"] = True
        for label, slot in (("missing", None), ("soon", "soon"), ("boolean", True), ("negative", -5), ("stale", 2_000_000), ("future", 2_000_028 + 1_000)):
            chain["validity_context"] = "missing" if label == "missing" else "ok"
            chain["validity_slot"] = slot
            reason = {"missing": "context_missing", "soon": "context_malformed", "boolean": "context_malformed", "negative": "context_malformed", "stale": "context_stale", "future": "context_future"}[label]
            self.assertEqual(desk_loop._screen_quote(transport, WHEN, mint, evidence)["reserve_reason"], reason, label)
        chain["validity_context"] = "ok"
        chain["validity_slot"] = 2_000_028
        rfq = desk_loop._bind_provider_window(
            {"ok": True, "block_height": 999_000, "blockhash_valid": True, "latest_blockhash": chain["latest_blockhash"], "latest_blockhash_paired": False},
            {"router": "jupiterz", "expire_at": int(WHEN) + 100, "transaction_b64": "quoted", "provider_expiry_malformed": False, "provider_expiry_field": "expireAt"},
            chain["blockhash"], WHEN,
        )
        self.assertTrue(rfq["bind_ok"], rfq)
        self.assertTrue(rfq["blockhash_still_valid"])
        self.assertTrue(rfq["expire_at_enforced"])
        self.assertFalse(rfq["height_proof"])
        self.assertFalse(rfq["inclusion_window_proven"])
        self.assertFalse(rfq["price_freshness_proven"])
        self.assertFalse(rfq["quote_expiry_proven"])
        self.assertFalse(rfq["gate_complete"])
        chain["expire_at"] = int(WHEN) - 10
        self.assertEqual(desk_loop._screen_quote(transport, WHEN, mint, evidence)["reserve_reason"], "quote_expired")
        chain["expire_at"] = None
        self.assertIsNone(memecoin_desk.view(case_id)["snapshot"].get("entry_inflight"))
        booked = desk_loop.run_supervised(transport, WHEN, rounds=1)
        quote = next(item["quote"] for item in booked["discovery"]["screened"] if item.get("mint") == mint)
        self.assertFalse(quote["quote_expiry_proven"], quote)
        self.assertFalse(quote["price_freshness_proven"])
        self.assertTrue(quote["inclusion_window_proven"], quote)
        self.assertTrue(quote["height_proof"])
        self.assertFalse(quote["gate_complete"])
        self.assertTrue(quote["reserved"], quote)
        self.assertTrue(quote["execution_eligible"], quote)
        self.assertEqual(quote["operation"], "unimplemented")
        self.assertFalse(quote["latest_blockhash_paired"])
        self.assertEqual(quote["last_valid_provenance"], "jupiter_order")
        self.assertFalse(quote["block_height_missing"])
        self.assertIsNone(quote["internal_time_limit"])
        self.assertFalse(quote["send"])
        self.assertFalse(quote["sign"])
        self.assertFalse(quote["execution"]["simulate"])
        chain["preflight_mode"] = "clear"
        chain["sim_calls"] = 0
        preview = desk_loop.run_supervised(transport, WHEN, rounds=1)
        entry_preflight = next(item["preflight"] for item in preview["discovery"]["reservations"] if item.get("case_id") == case_id and item.get("side") == "entry")
        self.assertTrue(entry_preflight["simulation_clear"], entry_preflight)
        self.assertTrue(entry_preflight["account_validated"], entry_preflight)
        self.assertFalse(entry_preflight["network_fee_arithmetic_match"])
        self.assertTrue(entry_preflight["requested_priority_arithmetic_match"], entry_preflight)
        self.assertFalse(entry_preflight["preflight_ready"], entry_preflight)
        self.assertEqual(entry_preflight["reason"], "fee_composition_unproven")
        self.assertEqual(entry_preflight["economic_reason"], "instruction_unsupported")
        self.assertTrue(entry_preflight["instruction_unproven"])
        self.assertFalse(entry_preflight["fee_lamports_match"])
        self.assertFalse(entry_preflight["fee_composition_proven"])
        self.assertFalse(entry_preflight["rent_bound_proven"])
        self.assertTrue(entry_preflight["rent_unproven"])
        self.assertFalse(entry_preflight["fee_usd_proven"])
        self.assertFalse(entry_preflight["ticket_limit_proven"])
        self.assertFalse(entry_preflight["instruction_slippage_within_cap"])
        self.assertIsNone(entry_preflight["instruction_slippage_bps"])
        self.assertFalse(entry_preflight["network_fee_instruction_proven"])
        self.assertFalse(entry_preflight["base_fee_proven"])
        self.assertTrue(entry_preflight["fee_for_message_observed"], entry_preflight)
        self.assertEqual(entry_preflight["fee_for_message_lamports"], 5000)
        self.assertFalse(entry_preflight["charged_fee_proven"])
        self.assertNotEqual(chain["fee_messages"][-1], db.query("SELECT message_b64 FROM memecoin_reservations WHERE case_id = ? AND side = 'entry'", (case_id,))[0]["message_b64"])
        self.assertEqual(chain["fee_messages"][-1], __import__("base64").b64encode(solana_tx.message_bytes(db.query("SELECT message_b64 FROM memecoin_reservations WHERE case_id = ? AND side = 'entry'", (case_id,))[0]["message_b64"])).decode("ascii"))
        self.assertFalse(entry_preflight["min_out_bound_proven"])
        self.assertFalse(entry_preflight["program_rounding_proven"])
        self.assertFalse(entry_preflight["dex_fee_proven"])
        self.assertFalse(entry_preflight["combined_fee_usd_proven"])
        self.assertFalse(entry_preflight["allocation_proven"])
        self.assertFalse(entry_preflight["slippage_proven_by_simulation"])
        self.assertFalse(entry_preflight["execution_guarantee"])
        self.assertFalse(entry_preflight["slippage_proven_by_simulation"])
        self.assertFalse(entry_preflight["price_freshness_proven"])
        self.assertFalse(entry_preflight["gate_complete"])
        self.assertFalse(entry_preflight["sign"])
        self.assertFalse(entry_preflight["send"])
        self.assertEqual(entry_preflight["operation"], "unimplemented")
        self.assertFalse(entry_preflight["replace_recent_blockhash"])
        self.assertFalse(entry_preflight["sig_verify"])
        self.assertEqual(chain["last_sim"]["transaction"], db.query("SELECT message_b64 FROM memecoin_reservations WHERE case_id = ? AND side = 'entry'", (case_id,))[0]["message_b64"])
        self.assertFalse(chain["last_sim"]["replaceRecentBlockhash"])
        self.assertFalse(chain["last_sim"]["sigVerify"])
        self.assertEqual(chain["sim_calls"], 1)
        again = desk_loop.preflight_reserved(case_id, db.query("SELECT message_sha256 FROM memecoin_reservations WHERE case_id = ? AND side = 'entry'", (case_id,))[0]["message_sha256"], transport, WHEN + 1)
        self.assertEqual(chain["sim_calls"], 2, again)
        self.assertTrue(again["simulation_clear"], again)
        entry_hash = db.query("SELECT message_sha256, blockhash FROM memecoin_reservations WHERE case_id = ? AND side = 'entry'", (case_id,))[0]
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM memecoin_preflight_attempts WHERE message_sha256 = ?", (entry_hash["message_sha256"],))[0]["n"], 2)
        for mode, reason in (
            ("null", "fee_for_message_unavailable"),
            ("malformed", "fee_for_message_context_malformed"),
            ("stale", "fee_for_message_context_stale"),
            ("mismatch", "fee_for_message_message_mismatch"),
            ("missing", "fee_for_message_context_missing"),
        ):
            chain["fee_mode"] = mode
            quoted_fee = desk_loop.preflight_reserved(case_id, entry_hash["message_sha256"], transport, WHEN)
            self.assertFalse(quoted_fee["fee_for_message_observed"], mode)
            self.assertIsNone(quoted_fee["fee_for_message_lamports"], mode)
            self.assertFalse(quoted_fee["charged_fee_proven"], mode)
            self.assertEqual(quoted_fee["fee_for_message_reason"], reason, mode)
            self.assertFalse(quoted_fee["combined_fee_usd_proven"], mode)
            self.assertFalse(quoted_fee["preflight_ready"], mode)
            self.assertNotIn("fee_for_message", json.dumps(memecoin_desk.view(case_id)["snapshot"]))
        for label, amount in (("zero", 0), ("max", 2**64 - 1)):
            chain["fee_mode"] = "value"
            chain["fee_value"] = amount
            quoted_fee = desk_loop.preflight_reserved(case_id, entry_hash["message_sha256"], transport, WHEN)
            self.assertTrue(quoted_fee["fee_for_message_observed"], label)
            self.assertEqual(quoted_fee["fee_for_message_lamports"], amount)
            self.assertFalse(quoted_fee["charged_fee_proven"], label)
            self.assertFalse(quoted_fee["preflight_ready"], label)
            self.assertFalse(quoted_fee["gate_complete"], label)
            self.assertFalse(quoted_fee["fee_usd_proven"], label)
            self.assertFalse(quoted_fee["combined_fee_usd_proven"], label)
            self.assertFalse(quoted_fee["rent_bound_proven"], label)
            self.assertFalse(quoted_fee["ticket_limit_proven"], label)
            self.assertNotIn("fee_for_message", json.dumps(memecoin_desk.view(case_id)["snapshot"]))
        for label, amount in (("overflow", 2**64), ("huge", 2**64 + 5), ("negative", -1), ("bool", True), ("noninteger", "5000")):
            chain["fee_mode"] = "value"
            chain["fee_value"] = amount
            quoted_fee = desk_loop.preflight_reserved(case_id, entry_hash["message_sha256"], transport, WHEN)
            self.assertFalse(quoted_fee["fee_for_message_observed"], label)
            self.assertIsNone(quoted_fee["fee_for_message_lamports"], label)
            self.assertEqual(quoted_fee["fee_for_message_reason"], "fee_for_message_malformed", label)
            self.assertFalse(quoted_fee["charged_fee_proven"], label)
            self.assertFalse(quoted_fee["preflight_ready"], label)
        chain["fee_mode"] = "clear"
        chain.pop("fee_value", None)
        attempt = db.query("SELECT id, blockhash, case_id FROM memecoin_preflight_attempts WHERE message_sha256 = ? ORDER BY id DESC LIMIT 1", (entry_hash["message_sha256"],))[0]
        self.assertEqual(attempt["case_id"], case_id)
        self.assertEqual(attempt["blockhash"], entry_hash["blockhash"])
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE memecoin_preflight_attempts SET case_id = 'other-case' WHERE id = ?", (attempt["id"],))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("DELETE FROM memecoin_preflight_attempts WHERE id = ?", (attempt["id"],))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO memecoin_preflight_attempts (message_sha256, case_id, side, blockhash, context_slot, err_clear, created_at) VALUES (?, ?, 'entry', 'different-hash', 1, 1, ?)",
                (entry_hash["message_sha256"], case_id, WHEN),
            )
        rfq_tx = message("buy", 2_000, chain["blockhash"])[0]
        rfq_hash = __import__("hashlib").sha256(solana_tx.message_bytes(rfq_tx)).hexdigest()
        db.execute(
            "INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, created_at, updated_at, ledger, ledger_provenance) VALUES ('rfq-case', ?, 'protected', ?, ?, ?, 'live', 'server')",
            (solana_tx.b58encode(b"\x63" * 32), json.dumps({"reserved_usd": "1.00"}), WHEN, WHEN),
        )
        db.execute(
            "INSERT INTO memecoin_reservations (message_sha256, case_id, side, message_b64, router, blockhash, expire_at, ledger, ledger_provenance, created_at) VALUES (?, 'rfq-case', 'entry', ?, 'jupiterz', ?, ?, 'live', 'server', ?)",
            (rfq_hash, rfq_tx, chain["blockhash"], int(WHEN) + 30, WHEN),
        )
        rfq_calls = chain["sim_calls"]
        rfq_fresh = desk_loop.preflight_reserved("rfq-case", rfq_hash, transport, WHEN)
        self.assertFalse(rfq_fresh["preflight_ready"], rfq_fresh)
        self.assertEqual(rfq_fresh["reason"], "fee_composition_unproven")
        self.assertFalse(rfq_fresh["fee_lamports_match"])
        self.assertFalse(rfq_fresh["rent_bound_proven"])
        self.assertEqual(rfq_fresh["case_id"], "rfq-case")
        self.assertFalse(rfq_fresh["gate_complete"])
        self.assertFalse(rfq_fresh["price_freshness_proven"])
        self.assertGreater(chain["sim_calls"], rfq_calls)
        rfq_after = chain["sim_calls"]
        rfq_late = desk_loop.preflight_reserved("rfq-case", rfq_hash, transport, WHEN + 31)
        self.assertEqual(rfq_late["reason"], "quote_expired")
        self.assertFalse(rfq_late["attempted"])
        self.assertFalse(rfq_late["resend"])
        self.assertEqual(chain["sim_calls"], rfq_after)
        entry_id = db.query("SELECT message_sha256 FROM memecoin_reservations WHERE case_id = ? AND side = 'entry'", (case_id,))[0]["message_sha256"]
        chain["preflight_mode"] = "fee_only"
        fee_only = desk_loop.preflight_reserved(case_id, entry_id, transport, WHEN)
        self.assertTrue(fee_only["simulation_clear"], fee_only)
        self.assertFalse(fee_only["account_validated"])
        self.assertFalse(fee_only["fee_lamports_match"])
        self.assertFalse(fee_only["fee_composition_proven"])
        self.assertFalse(fee_only["rent_bound_proven"])
        self.assertFalse(fee_only["network_fee_arithmetic_match"])
        self.assertFalse(fee_only["rent_constant_arithmetic_match"])
        self.assertIsNone(fee_only["observed_lamport_delta"])
        self.assertEqual(fee_only["reason"], "simulation_account_malformed")
        self.assertFalse(fee_only["preflight_ready"])
        chain["preflight_mode"] = "rent_constant"
        rent_constant = desk_loop.preflight_reserved(case_id, entry_id, transport, WHEN)
        self.assertTrue(rent_constant["account_validated"], rent_constant)
        self.assertTrue(rent_constant["rent_constant_arithmetic_match"], rent_constant)
        self.assertFalse(rent_constant["rent_bound_proven"])
        self.assertFalse(rent_constant["fee_lamports_match"])
        self.assertFalse(rent_constant["fee_composition_proven"])
        self.assertFalse(rent_constant["network_fee_arithmetic_match"])
        self.assertEqual(rent_constant["reason"], "fee_composition_unproven")
        self.assertFalse(rent_constant["preflight_ready"])
        self.assertFalse(rent_constant["gate_complete"])
        chain["preflight_mode"] = "malformed_owner"
        malformed_owner = desk_loop.preflight_reserved(case_id, entry_id, transport, WHEN)
        self.assertEqual(malformed_owner["reason"], "simulation_account_malformed")
        self.assertFalse(malformed_owner["account_validated"])
        self.assertFalse(malformed_owner["fee_lamports_match"])
        self.assertFalse(malformed_owner["rent_bound_proven"])
        chain["preflight_mode"] = "malformed_data"
        malformed_data = desk_loop.preflight_reserved(case_id, entry_id, transport, WHEN)
        self.assertEqual(malformed_data["reason"], "simulation_account_malformed")
        self.assertFalse(malformed_data["fee_lamports_match"])
        self.assertFalse(malformed_data["rent_bound_proven"])
        chain["preflight_mode"] = "clear"
        chain["fee_payer_shape"] = "lamports_only"
        bare_payer = desk_loop.preflight_reserved(case_id, entry_id, transport, WHEN)
        chain["fee_payer_shape"] = None
        self.assertEqual(bare_payer["reason"], "simulation_account_malformed")
        self.assertFalse(bare_payer["network_fee_arithmetic_match"])
        self.assertFalse(bare_payer["fee_lamports_match"])
        self.assertFalse(bare_payer["rent_bound_proven"])

        def assert_account_closed(found, label):
            self.assertEqual(found["reason"], "simulation_account_malformed", label)
            self.assertTrue(found["simulation_clear"], label)
            self.assertFalse(found["account_validated"], label)
            self.assertIsNone(found["observed_lamport_delta"], label)
            self.assertFalse(found["network_fee_arithmetic_match"], label)
            self.assertFalse(found["rent_constant_arithmetic_match"], label)
            self.assertFalse(found["fee_lamports_match"], label)
            self.assertFalse(found["fee_composition_proven"], label)
            self.assertFalse(found["rent_bound_proven"], label)
            self.assertTrue(found["rent_unproven"], label)
            self.assertFalse(found["fee_usd_proven"], label)
            self.assertFalse(found["slippage_proven_by_simulation"], label)
            self.assertFalse(found["ticket_limit_proven"], label)
            self.assertFalse(found["preflight_ready"], label)
            self.assertFalse(found["gate_complete"], label)

        for mode in ("bad_alphabet", "bad_padding", "bad_type", "bad_encoding", "nonempty_system", "executable_system"):
            chain["preflight_mode"] = mode
            assert_account_closed(desk_loop.preflight_reserved(case_id, entry_id, transport, WHEN), mode)
        chain["preflight_mode"] = "clear"
        for shape in ("bad_alphabet", "bad_padding", "bad_type", "nonempty_system"):
            chain["fee_payer_shape"] = shape
            assert_account_closed(desk_loop.preflight_reserved(case_id, entry_id, transport, WHEN), "pre-" + shape)
        chain["fee_payer_shape"] = None
        empty_system = desk_loop.preflight_reserved(case_id, entry_id, transport, WHEN)
        self.assertTrue(empty_system["account_validated"], empty_system)
        self.assertFalse(empty_system["network_fee_arithmetic_match"])
        self.assertTrue(empty_system["requested_priority_arithmetic_match"], empty_system)
        self.assertEqual(empty_system["reason"], "fee_composition_unproven")
        self.assertFalse(empty_system["fee_lamports_match"])
        self.assertFalse(empty_system["fee_composition_proven"])
        self.assertFalse(empty_system["rent_bound_proven"])
        self.assertTrue(empty_system["rent_unproven"])
        self.assertFalse(empty_system["preflight_ready"])
        self.assertFalse(empty_system["gate_complete"])
        stored_tx = db.query("SELECT message_b64 FROM memecoin_reservations WHERE message_sha256 = ?", (entry_id,))[0]["message_b64"]
        stored_econ = desk_loop._exact_message_economics(solana_tx.decode_transaction(base64.b64decode(stored_tx)))
        self.assertEqual(stored_econ["instruction_slippage_bps"], entry_preflight["instruction_slippage_bps"])
        self.assertFalse(stored_econ["slippage_proven_by_simulation"])
        self.assertFalse(stored_econ["price_freshness_proven"])
        self.assertFalse(stored_econ["combined_fee_usd_proven"])
        self.assertFalse(stored_econ["dex_fee_proven"])
        self.assertFalse(stored_econ["ticket_limit_proven"])
        self.assertFalse(stored_econ["rent_bound_proven"])
        import phantom_transport

        def bare(route, limit=200_000, price=1_000, extra=()):
            payer = phantom_transport.REQUIRED_WALLET
            keys = [payer, solana_tx.COMPUTE_BUDGET, solana_jupiter.PROGRAM_ID]
            instructions = [
                (1, [], bytes([2]) + int(limit).to_bytes(4, "little")),
                (1, [], bytes([3]) + int(price).to_bytes(8, "little")),
                (2, [], route),
            ]
            for program, data in extra:
                if program == solana_tx.COMPUTE_BUDGET:
                    instructions.append((1, [], data))
                else:
                    keys.append(program)
                    instructions.append((len(keys) - 1, [], data))
            return solana_tx.encode_legacy(payer, keys, len(keys) - 1, instructions, chain["blockhash"])

        agreed = bare(_route(10_000, 50, True, amount=1_000_000))
        agreed_econ = desk_loop._exact_message_economics(solana_tx.decode_transaction(base64.b64decode(agreed)))
        self.assertTrue(agreed_econ["instruction_slippage_within_cap"], agreed_econ)
        self.assertTrue(agreed_econ["min_out_bound_proven"], agreed_econ)
        self.assertEqual(agreed_econ["candidate_min_out"], agreed_econ["api_sample_min_out"])
        self.assertEqual(agreed_econ["requested_compute_unit_limit"], 200_000)
        self.assertEqual(agreed_econ["requested_micro_lamports_per_cu"], 1_000)
        self.assertEqual(agreed_econ["requested_priority_lamports"], (1_000 * 200_000 + 999_999) // 1_000_000)
        self.assertEqual(agreed_econ["requested_priority_formula"], "ceil(requested_cu_limit * micro_lamports_per_cu / 1000000)")
        self.assertFalse(agreed_econ["priority_uses_consumed_units"])
        self.assertFalse(agreed_econ["priority_fee_observed"])
        self.assertFalse(agreed_econ["network_fee_instruction_proven"])
        self.assertFalse(agreed_econ["base_fee_proven"])
        self.assertFalse(agreed_econ["program_rounding_proven"])
        self.assertFalse(agreed_econ["slippage_proven_by_simulation"])
        self.assertFalse(agreed_econ["combined_fee_usd_proven"])
        self.assertFalse(agreed_econ["ticket_limit_proven"])
        rounded = jupiter_route_v2.network_fee([bytes([2]) + (1_000_001).to_bytes(4, "little"), bytes([3]) + (1).to_bytes(8, "little")])
        self.assertEqual(rounded["requested_priority_lamports"], 2)
        self.assertFalse(rounded["network_fee_proven"])
        self.assertFalse(rounded["base_fee_proven"])
        self.assertFalse(rounded["priority_fee_observed"])
        self.assertFalse(rounded["priority_uses_consumed_units"])
        even = jupiter_route_v2.network_fee([bytes([2]) + (1_000_000).to_bytes(4, "little"), bytes([3]) + (1).to_bytes(8, "little")])
        self.assertEqual(even["requested_priority_lamports"], 1)
        duplicate = jupiter_route_v2.network_fee([
            bytes([3]) + (1).to_bytes(8, "little"),
            bytes([3]) + (9).to_bytes(8, "little"),
            bytes([2]) + (200_000).to_bytes(4, "little"),
        ])
        self.assertFalse(duplicate["ok"])
        self.assertEqual(duplicate["reason"], "compute_budget_duplicate")
        self.assertFalse(duplicate["network_fee_proven"])
        split = bare(_route(10_001, 50, True, amount=1_000_000))
        split_econ = desk_loop._exact_message_economics(solana_tx.decode_transaction(base64.b64decode(split)))
        self.assertTrue(split_econ["instruction_slippage_within_cap"], split_econ)
        self.assertNotEqual(split_econ["candidate_min_out"], split_econ["api_sample_min_out"])
        self.assertFalse(split_econ["min_out_bound_proven"])
        self.assertFalse(split_econ["program_rounding_proven"])
        self.assertEqual(split_econ["economic_reason"], "fee_composition_unproven")
        wide = bare(_route(10_000, 80, True, amount=1_000_000))
        wide_econ = desk_loop._exact_message_economics(solana_tx.decode_transaction(base64.b64decode(wide)))
        self.assertFalse(wide_econ["instruction_slippage_within_cap"])
        self.assertEqual(wide_econ["economic_reason"], "slippage_exceeds")
        self.assertFalse(wide_econ["min_out_bound_proven"])
        self.assertFalse(wide_econ["slippage_proven_by_simulation"])
        moved = bare(_route(10_000, 50, True, amount=1_000_000), extra=[(solana_tx.SYSTEM_PROGRAM, (2).to_bytes(4, "little") + (1).to_bytes(8, "little"))])
        moved_econ = desk_loop._exact_message_economics(solana_tx.decode_transaction(base64.b64decode(moved)))
        self.assertEqual(moved_econ["economic_reason"], "instruction_unsupported")
        self.assertFalse(moved_econ["instruction_slippage_within_cap"])
        self.assertFalse(moved_econ["min_out_bound_proven"])
        token2022 = bare(_route(10_000, 50, True, amount=1_000_000), extra=[(solana_tx.TOKEN_2022, b"\x00")])
        token_econ = desk_loop._exact_message_economics(solana_tx.decode_transaction(base64.b64decode(token2022)))
        self.assertEqual(token_econ["economic_reason"], "instruction_unsupported")
        self.assertFalse(token_econ["instruction_slippage_within_cap"])
        self.assertFalse(token_econ["min_out_bound_proven"])
        second_price = bare(_route(10_000, 50, True, amount=1_000_000), extra=[(solana_tx.COMPUTE_BUDGET, bytes([3]) + (9).to_bytes(8, "little"))])
        second_econ = desk_loop._exact_message_economics(solana_tx.decode_transaction(base64.b64decode(second_price)))
        self.assertEqual(second_econ["economic_reason"], "instruction_unsupported")
        self.assertFalse(second_econ["instruction_slippage_within_cap"])
        self.assertFalse(second_econ["network_fee_instruction_proven"])
        self.assertIsNone(second_econ["requested_compute_unit_limit"])
        priced = solana_tx.decode_transaction(base64.b64decode(agreed))
        for item in priced.instructions:
            if priced.static_keys[item.program_index] == solana_jupiter.PROGRAM_ID:
                mutated = bytearray(item.data)
                mutated[26:28] = (1).to_bytes(2, "little")
                item.data = bytes(mutated)
        fee_econ = desk_loop._exact_message_economics(priced)
        self.assertEqual(fee_econ["economic_reason"], "platform_fee_unpriced")
        self.assertFalse(fee_econ["instruction_slippage_within_cap"])
        self.assertFalse(fee_econ["combined_fee_usd_proven"])
        looked = solana_tx.decode_transaction(base64.b64decode(agreed))
        looked.lookups = [solana_tx.Lookup(solana_tx.b58encode(b"\x03" * 32), [0], [])]
        looked_econ = desk_loop._exact_message_economics(looked)
        self.assertTrue(looked_econ["lookup_unresolved"])
        self.assertTrue(looked_econ["instruction_unproven"])
        self.assertFalse(looked_econ["instruction_slippage_within_cap"])
        self.assertFalse(looked_econ["network_fee_instruction_proven"])
        self.assertFalse(looked_econ["min_out_bound_proven"])
        unknown = solana_tx.decode_transaction(base64.b64decode(agreed))
        unknown.static_keys = list(unknown.static_keys) + [solana_tx.b58encode(b"\xab" * 32)]
        unknown.instructions = list(unknown.instructions) + [solana_tx.Instruction(len(unknown.static_keys) - 1, [], b"\x00")]
        unknown_econ = desk_loop._exact_message_economics(unknown)
        self.assertTrue(unknown_econ["instruction_unproven"])
        self.assertFalse(unknown_econ["instruction_slippage_within_cap"])
        self.assertFalse(unknown_econ["dex_fee_proven"])
        self.assertFalse(unknown_econ["rent_bound_proven"])
        held_usd = memecoin_desk.view(case_id)["snapshot"]["reserved_usd"]
        chain["preflight_mode"] = "err"
        failed_sim = desk_loop.run_supervised(transport, WHEN, rounds=1)
        failed_row = next(item["preflight"] for item in failed_sim["discovery"]["reservations"] if item.get("case_id") == case_id and item.get("side") == "entry")
        self.assertEqual(failed_row["reason"], "simulation_failed")
        self.assertFalse(failed_row["preflight_ready"])
        self.assertFalse(failed_row["resend"])
        self.assertEqual(memecoin_desk.view(case_id)["snapshot"]["reserved_usd"], held_usd)
        chain["preflight_mode"] = "context_missing"
        self.assertEqual(desk_loop.preflight_reserved(case_id, db.query("SELECT message_sha256 FROM memecoin_reservations WHERE case_id = ? AND side = 'entry'", (case_id,))[0]["message_sha256"], transport, WHEN)["reason"], "simulation_context_missing")
        chain["preflight_mode"] = "context_malformed"
        self.assertEqual(desk_loop.preflight_reserved(case_id, db.query("SELECT message_sha256 FROM memecoin_reservations WHERE case_id = ? AND side = 'entry'", (case_id,))[0]["message_sha256"], transport, WHEN)["reason"], "simulation_context_malformed")
        chain["preflight_mode"] = "malformed"
        self.assertEqual(desk_loop.preflight_reserved(case_id, db.query("SELECT message_sha256 FROM memecoin_reservations WHERE case_id = ? AND side = 'entry'", (case_id,))[0]["message_sha256"], transport, WHEN)["reason"], "simulation_malformed")
        chain["preflight_mode"] = "stale"
        self.assertEqual(desk_loop.preflight_reserved(case_id, db.query("SELECT message_sha256 FROM memecoin_reservations WHERE case_id = ? AND side = 'entry'", (case_id,))[0]["message_sha256"], transport, WHEN)["reason"], "simulation_context_stale")
        chain["preflight_mode"] = None
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO memecoin_preflight_attempts (message_sha256, case_id, side, blockhash, context_slot, err_clear, created_at) VALUES (?, 'other-case', 'entry', 'hash', 1, 1, ?)",
                (db.query("SELECT message_sha256 FROM memecoin_reservations WHERE case_id = ? AND side = 'entry'", (case_id,))[0]["message_sha256"], WHEN),
            )
        flight = memecoin_desk.view(case_id)["snapshot"]["entry_inflight"]
        self.assertEqual(flight["blockhash"], saved_hash)
        self.assertNotEqual(flight["blockhash"], chain["latest_blockhash"])
        self.assertEqual(flight["last_valid_block_height"], 1_000_000)
        self.assertEqual(flight["last_valid_provenance"], "jupiter_order")
        self.assertEqual(flight["block_height"], 999_000)
        self.assertLessEqual(flight["instruction_slippage_bps"], 50)
        self.assertIsNone(flight.get("resend"))
        reserved_usd = memecoin_desk.view(case_id)["snapshot"]["reserved_usd"]
        digest = flight["message_sha256"]
        original_snap = db.query("SELECT snapshot_json FROM memecoin_cases WHERE id = ?", (case_id,))[0]["snapshot_json"]
        forged = json.loads(original_snap)
        forged["entry_inflight"]["blockhash"] = solana_tx.b58encode(b"\x49" * 32)
        forged["entry_inflight"]["message_sha256"] = "f" * 64
        db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = ?", (json.dumps(forged), case_id))
        real = desk_loop.execution_gate(case_id, digest, transport, WHEN)
        self.assertTrue(real["execution_eligible"], real)
        self.assertEqual(real["blockhash"], saved_hash)
        self.assertFalse(real["gate_complete"])
        self.assertFalse(real["price_freshness_proven"])
        mismatched = desk_loop.execution_gate(case_id, "f" * 64, transport, WHEN)
        self.assertEqual(mismatched["reason"], "message_mismatch")
        crossed = desk_loop.execution_gate("other-case", digest, transport, WHEN)
        self.assertEqual(crossed["reason"], "message_mismatch")
        durable = db.query("SELECT message_sha256, blockhash, side FROM memecoin_reservations WHERE case_id = ?", (case_id,))[0]
        self.assertEqual(durable["message_sha256"], digest)
        self.assertEqual(durable["blockhash"], saved_hash)
        self.assertEqual(durable["side"], "entry")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE memecoin_reservations SET blockhash = ? WHERE message_sha256 = ?", (chain["latest_blockhash"], digest))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(
                "INSERT OR REPLACE INTO memecoin_reservations (message_sha256, case_id, side, message_b64, router, blockhash, last_valid_block_height, last_valid_provenance, ledger, ledger_provenance, created_at) SELECT message_sha256, case_id, side, message_b64, router, blockhash, 9000000, last_valid_provenance, ledger, ledger_provenance, created_at FROM memecoin_reservations WHERE message_sha256 = ?",
                (digest,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("DELETE FROM memecoin_reservations WHERE message_sha256 = ?", (digest,))
        self.assertEqual(db.query("SELECT last_valid_block_height FROM memecoin_reservations WHERE message_sha256 = ?", (digest,))[0]["last_valid_block_height"], 1_000_000)
        loss_before = memecoin_desk.loss_totals()["live_loss_usd"]

        def clear_receipt(sig):
            db.execute("DELETE FROM memecoin_entry_receipts WHERE signature = ?", (sig,))
            db.execute("DELETE FROM memecoin_exit_receipts WHERE signature = ?", (sig,))

        def plant_entry(sig, ledger, provenance, target=case_id, hashed=digest):
            db.execute(
                "INSERT INTO memecoin_entry_receipts (signature, case_id, message_sha256, slot, qty, cost_basis_lamports, network_fee_lamports, dex_fee_lamports, fee_usd, failed, ledger, ledger_provenance) VALUES (?, ?, ?, 1, 5000, 1, 1, 1, '0.01', 0, ?, ?)",
                (sig, target, hashed, ledger, provenance),
            )

        def assert_entry_still_supervised(label):
            watched = desk_loop.run_supervised(transport, WHEN, rounds=1)
            listed = [item for item in watched["discovery"]["reservations"] if item.get("message_sha256") == digest]
            self.assertEqual(len(listed), 1, label)
            self.assertNotIn(listed[0]["execution"].get("reason"), {"reservation_consumed", "reservation_failed"}, label)
            self.assertTrue(listed[0]["execution"]["execution_eligible"], listed[0])
            self.assertEqual(memecoin_desk.view(case_id)["snapshot"]["reserved_usd"], reserved_usd, label)
            self.assertEqual(memecoin_desk.loss_totals()["live_loss_usd"], loss_before, label)
            saved = db.query("SELECT snapshot_json FROM memecoin_cases WHERE id = ?", (case_id,))[0]["snapshot_json"]
            stripped = json.loads(saved)
            stripped.pop("entry_inflight", None)
            db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = ?", (json.dumps(stripped), case_id))
            previous_hash = chain["blockhash"]
            chain["blockhash"] = solana_tx.b58encode(b"\x49" * 32)
            probed = desk_loop._screen_quote(transport, WHEN, mint, evidence)
            chain["blockhash"] = previous_hash
            self.assertIsNot(probed.get("reserved"), True, (label, probed))
            self.assertEqual(probed.get("reserve_reason"), "entry_inflight", (label, probed))
            self.assertEqual([row["message_sha256"] for row in db.query("SELECT message_sha256 FROM memecoin_reservations WHERE case_id = ? AND side = 'entry'", (case_id,))], [digest], label)
            db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = ?", (saved, case_id))
            self.assertEqual(memecoin_desk.view(case_id)["snapshot"]["reserved_usd"], reserved_usd, label)

        for sig, ledger, provenance in (
            ("paper-entry", "paper", "server"),
            ("synthetic-entry", "synthetic", "server"),
            ("legacy-entry", "live", "legacy_default"),
        ):
            plant_entry(sig, ledger, provenance)
            assert_entry_still_supervised(sig)
            clear_receipt(sig)
        db.execute("DROP TRIGGER IF EXISTS memecoin_entry_receipts_ledger")
        db.execute(
            "INSERT INTO memecoin_entry_receipts (signature, case_id, message_sha256, slot, qty, cost_basis_lamports, network_fee_lamports, dex_fee_lamports, fee_usd, failed, ledger, ledger_provenance) VALUES ('unknown-entry', ?, ?, 1, 5000, 1, 1, 1, '0.01', 0, 'unknown', 'server')",
            (case_id, digest),
        )
        db.execute(
            "INSERT INTO memecoin_entry_receipts (signature, case_id, message_sha256, slot, qty, cost_basis_lamports, network_fee_lamports, dex_fee_lamports, fee_usd, failed, ledger) VALUES ('missing-entry', ?, ?, 1, 5000, 1, 1, 1, '0.01', 0, 'live')",
            (case_id, digest),
        )
        memecoin_desk._ensure()
        assert_entry_still_supervised("ambiguous-entry")
        clear_receipt("unknown-entry")
        clear_receipt("missing-entry")
        plant_entry("cross-case-entry", "live", "server", target="other-case")
        assert_entry_still_supervised("cross-case-entry")
        clear_receipt("cross-case-entry")
        db.execute(
            "INSERT INTO memecoin_exit_receipts (signature, case_id, message_sha256, slot, fee_usd, qty, ledger, ledger_provenance) VALUES ('cross-side-entry', ?, ?, 2, '0.01', 400, 'live', 'server')",
            (case_id, digest),
        )
        assert_entry_still_supervised("cross-side-entry")
        clear_receipt("cross-side-entry")
        spare_tx, _, _ = message("buy", 1_000, chain["blockhash"])
        spare = __import__("hashlib").sha256(solana_tx.message_bytes(spare_tx)).hexdigest()
        db.execute(
            "INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, created_at, updated_at, ledger, ledger_provenance) VALUES ('spare-case', ?, 'protected', ?, ?, ?, 'live', 'server')",
            (solana_tx.b58encode(b"\x63" * 32), json.dumps({"reserved_usd": "1.00"}), WHEN, WHEN),
        )
        db.execute(
            "INSERT INTO memecoin_reservations (message_sha256, case_id, side, message_b64, router, blockhash, last_valid_block_height, last_valid_provenance, ledger, ledger_provenance, created_at) VALUES (?, 'spare-case', 'entry', ?, 'metis', ?, 1000200, 'jupiter_order', 'live', 'server', ?)",
            (spare, spare_tx, chain["blockhash"], WHEN),
        )
        for name in (
            "memecoin_reservations_identity_guard",
            "memecoin_reservations_identity_record",
            "memecoin_reservations_active_guard",
            "memecoin_reservation_identity_guard",
            "memecoin_reservation_identity_update",
            "memecoin_reservation_identity_delete",
            "memecoin_reservation_anchor_guard",
            "memecoin_reservation_anchor_update",
            "memecoin_reservation_anchor_delete",
            "memecoin_reservation_pin_update",
            "memecoin_reservation_pin_delete",
        ):
            db.execute("DROP TRIGGER IF EXISTS " + name)
        db.execute("DROP TABLE IF EXISTS memecoin_reservation_identity")
        db.execute("DROP TABLE IF EXISTS memecoin_reservation_anchor")
        db.execute("DROP TABLE IF EXISTS memecoin_reservation_pin")
        db.execute("DROP TABLE IF EXISTS memecoin_reservation_active")
        self.assertEqual(db.query("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'memecoin_reservation_identity'"), [])
        self.assertIn("BEFORE DELETE", db.query("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'memecoin_reservations_delete'")[0]["sql"])
        desk_path = os.path.join(os.environ["DESK_DATA_DIR"], "desk.db")
        replace_sql = "INSERT OR REPLACE INTO memecoin_reservations (message_sha256, case_id, side, message_b64, router, blockhash, last_valid_block_height, last_valid_provenance, ledger, ledger_provenance, created_at) VALUES (?, 'spare-case', 'entry', ?, 'metis', ?, 9000000, 'jupiter_order', 'live', 'server', ?)"
        upsert_sql = "INSERT INTO memecoin_reservations (message_sha256, case_id, side, message_b64, router, blockhash, last_valid_block_height, last_valid_provenance, ledger, ledger_provenance, created_at) VALUES (?, 'spare-case', 'entry', ?, 'metis', ?, 9000000, 'jupiter_order', 'live', 'server', ?) ON CONFLICT(message_sha256) DO UPDATE SET last_valid_block_height = 9000000"
        replace_params = (spare, spare_tx, chain["blockhash"], WHEN)
        raw = sqlite3.connect(desk_path)
        try:
            self.assertEqual(raw.execute("PRAGMA recursive_triggers").fetchone()[0], 0)
            raw.execute("BEGIN")
            raw.execute(replace_sql, replace_params)
            self.assertEqual(raw.execute("SELECT last_valid_block_height FROM memecoin_reservations WHERE message_sha256 = ?", (spare,)).fetchone()[0], 9_000_000)
            raw.rollback()
        finally:
            raw.close()
        self.assertEqual(db.query("SELECT last_valid_block_height FROM memecoin_reservations WHERE message_sha256 = ?", (spare,))[0]["last_valid_block_height"], 1_000_200)
        real_replace = memecoin_desk._replace_reservation_triggers

        def explode(conn):
            real_replace(conn)
            raise RuntimeError("reservation upgrade failed")

        memecoin_desk._replace_reservation_triggers = explode
        try:
            with self.assertRaises(RuntimeError):
                memecoin_desk._ensure()
        finally:
            memecoin_desk._replace_reservation_triggers = real_replace
        self.assertEqual(db.query("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'memecoin_reservation_identity'"), [])
        raw = sqlite3.connect(desk_path)
        try:
            raw.execute("BEGIN")
            raw.execute(replace_sql, replace_params)
            self.assertEqual(raw.execute("SELECT last_valid_block_height FROM memecoin_reservations WHERE message_sha256 = ?", (spare,)).fetchone()[0], 9_000_000)
            raw.rollback()
        finally:
            raw.close()
        memecoin_desk._ensure()
        self.assertEqual(db.query("SELECT last_valid_block_height FROM memecoin_reservation_identity WHERE message_sha256 = ?", (spare,))[0]["last_valid_block_height"], 1_000_200)
        self.assertEqual(db.exclusive(lambda conn: conn.execute("PRAGMA user_version").fetchone()[0]), 12)
        self.assertEqual(db.get_setting("memecoin_lineage_trigger"), "14")

        def reject_raw(sql, params):
            raw = sqlite3.connect(desk_path)
            try:
                self.assertEqual(raw.execute("PRAGMA recursive_triggers").fetchone()[0], 0)
                with self.assertRaises(sqlite3.IntegrityError):
                    raw.execute(sql, params)
                raw.rollback()
            finally:
                raw.close()

        reject_raw(replace_sql, replace_params)
        reject_raw(upsert_sql, replace_params)
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(replace_sql, replace_params)
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(upsert_sql, replace_params)
        db.execute("DROP TRIGGER IF EXISTS memecoin_reservations_delete")
        reject_raw(replace_sql, replace_params)
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(replace_sql, replace_params)
        self.assertEqual(db.query("SELECT last_valid_block_height FROM memecoin_reservations WHERE message_sha256 = ?", (spare,))[0]["last_valid_block_height"], 1_000_200)
        self.assertEqual(db.query("SELECT last_valid_block_height FROM memecoin_reservations WHERE message_sha256 = ?", (digest,))[0]["last_valid_block_height"], 1_000_000)
        self.assertEqual(memecoin_desk.view(case_id)["snapshot"]["reserved_usd"], reserved_usd)
        self.assertEqual(json.loads(db.query("SELECT snapshot_json FROM memecoin_cases WHERE id = 'spare-case'")[0]["snapshot_json"])["reserved_usd"], "1.00")
        boots = []

        def boot():
            try:
                memecoin_desk._ensure()
            except Exception as exc:
                boots.append(repr(exc))

        starters = [threading.Thread(target=boot) for _ in range(4)]
        for thread in starters:
            thread.start()
        for thread in starters:
            thread.join()
        self.assertEqual(boots, [])
        reject_raw(replace_sql, replace_params)
        memecoin_desk._ensure()
        trigger = db.query("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'memecoin_reservations_delete'")[0]["sql"]
        self.assertIn("BEFORE DELETE", trigger)
        self.assertNotIn("IF NOT EXISTS", trigger)
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(
                "INSERT OR REPLACE INTO memecoin_reservations (message_sha256, case_id, side, message_b64, router, blockhash, last_valid_block_height, last_valid_provenance, ledger, ledger_provenance, created_at) VALUES (?, 'spare-case', 'entry', ?, 'metis', ?, 42, 'jupiter_order', 'live', 'server', ?)",
                (spare, spare_tx, chain["blockhash"], WHEN),
            )
        self.assertEqual(db.query("SELECT last_valid_block_height FROM memecoin_reservations WHERE message_sha256 = ?", (spare,))[0]["last_valid_block_height"], 1_000_200)
        identity_replace = "INSERT OR REPLACE INTO memecoin_reservation_identity (message_sha256, case_id, side, message_b64, router, blockhash, last_valid_block_height, last_valid_provenance, expire_at, pool, intent_id, ledger, ledger_provenance) VALUES (?, 'other', 'exit', 'different-bytes', 'okx', ?, 9000000, 'jupiter_order', 77, 'other-pool', 'other-intent', 'live', 'server')"
        identity_upsert = "INSERT INTO memecoin_reservation_identity (message_sha256, case_id, side, message_b64, router, blockhash, last_valid_block_height, last_valid_provenance, expire_at, pool, intent_id, ledger, ledger_provenance) VALUES (?, 'other', 'exit', 'different-bytes', 'okx', ?, 9000000, 'jupiter_order', 77, 'other-pool', 'other-intent', 'live', 'server') ON CONFLICT(message_sha256) DO UPDATE SET case_id = 'other', side = 'exit', router = 'okx', last_valid_block_height = 9000000, expire_at = 77, message_b64 = 'different-bytes'"
        identity_params = (spare, chain["blockhash"])
        reject_raw(identity_replace, identity_params)
        reject_raw(identity_upsert, identity_params)
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(identity_replace, identity_params)
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(identity_upsert, identity_params)
        reject_raw("INSERT OR REPLACE INTO memecoin_reservation_anchor (message_sha256) VALUES (?)", (spare,))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT OR REPLACE INTO memecoin_reservation_anchor (message_sha256) VALUES (?)", (spare,))
        frozen = db.query("SELECT case_id, side, router, message_b64, last_valid_block_height, expire_at FROM memecoin_reservation_identity WHERE message_sha256 = ?", (spare,))[0]
        self.assertEqual(frozen["case_id"], "spare-case")
        self.assertEqual(frozen["side"], "entry")
        self.assertEqual(frozen["router"], "metis")
        self.assertEqual(frozen["message_b64"], spare_tx)
        self.assertEqual(frozen["last_valid_block_height"], 1_000_200)
        self.assertIsNone(frozen["expire_at"])
        db.execute("DROP TRIGGER IF EXISTS memecoin_reservation_identity_update")
        db.execute("UPDATE memecoin_reservation_identity SET last_valid_block_height = 9000000, side = 'exit', router = 'okx', expire_at = 77 WHERE message_sha256 = ?", (spare,))
        memecoin_desk._ensure()
        diverged = []

        def diverge(request):
            diverged.append(request.get("method"))
            raise AssertionError("rpc")

        mismatch = desk_loop.execution_gate("spare-case", spare, diverge, WHEN)
        self.assertEqual(mismatch["reason"], "reservation_identity_mismatch", mismatch)
        self.assertEqual(diverged, [])
        self.assertFalse(mismatch["execution_eligible"])
        self.assertTrue(mismatch["holds"])
        self.assertFalse(mismatch["resend"])
        self.assertEqual(db.query("SELECT last_valid_block_height, side FROM memecoin_reservations WHERE message_sha256 = ?", (spare,))[0]["last_valid_block_height"], 1_000_200)
        self.assertEqual(db.query("SELECT side FROM memecoin_reservations WHERE message_sha256 = ?", (spare,))[0]["side"], "entry")
        self.assertEqual(memecoin_desk.view(case_id)["snapshot"]["reserved_usd"], reserved_usd)
        db.execute("DROP TRIGGER IF EXISTS memecoin_reservation_identity_update")
        db.execute("UPDATE memecoin_reservation_identity SET last_valid_block_height = 1000200, side = 'entry', router = 'metis', expire_at = NULL WHERE message_sha256 = ?", (spare,))
        memecoin_desk._ensure()
        self.assertTrue(desk_loop.execution_gate("spare-case", spare, transport, WHEN)["execution_eligible"])
        second_tx = message("buy", 900_000, chain["blockhash"])[0]
        second = __import__("hashlib").sha256(solana_tx.message_bytes(second_tx)).hexdigest()
        second_sql = "INSERT INTO memecoin_reservations (message_sha256, case_id, side, message_b64, router, blockhash, last_valid_block_height, last_valid_provenance, ledger, ledger_provenance, created_at) VALUES (?, ?, 'entry', ?, 'metis', ?, 1000000, 'jupiter_order', 'live', 'server', ?)"
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(second_sql, (second, case_id, second_tx, chain["blockhash"], WHEN))
        reject_raw(second_sql, (second, case_id, second_tx, chain["blockhash"], WHEN))
        self.assertEqual([row["message_sha256"] for row in db.query("SELECT message_sha256 FROM memecoin_reservations WHERE case_id = ? AND side = 'entry'", (case_id,))], [digest])
        self.assertTrue(desk_loop.execution_gate(case_id, digest, transport, WHEN)["execution_eligible"])
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("DELETE FROM memecoin_reservation_active WHERE case_id = ? AND side = 'entry'", (case_id,))
        race_case = "race-case"
        db.execute(
            "INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, created_at, updated_at, ledger, ledger_provenance) VALUES (?, ?, 'protected', ?, ?, ?, 'live', 'server')",
            (race_case, solana_tx.b58encode(b"\x64" * 32), json.dumps({"reserved_usd": "1.25"}), WHEN, WHEN),
        )
        race_left = message("buy", 800_000, chain["blockhash"])[0]
        race_right = message("buy", 700_000, chain["blockhash"])[0]
        race_ids = (
            __import__("hashlib").sha256(solana_tx.message_bytes(race_left)).hexdigest(),
            __import__("hashlib").sha256(solana_tx.message_bytes(race_right)).hexdigest(),
        )
        race_ok = []
        race_err = []

        def race_insert(tx, digest_value):
            conn = sqlite3.connect(desk_path, timeout=5)
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(second_sql, (digest_value, race_case, tx, chain["blockhash"], WHEN))
                conn.commit()
                race_ok.append(digest_value)
            except sqlite3.IntegrityError:
                race_err.append(digest_value)
                conn.rollback()
            finally:
                conn.close()

        racers = [threading.Thread(target=race_insert, args=pair) for pair in ((race_left, race_ids[0]), (race_right, race_ids[1]))]
        for thread in racers:
            thread.start()
        for thread in racers:
            thread.join()
        self.assertEqual(len(race_ok), 1, (race_ok, race_err))
        self.assertEqual(len(race_err), 1, (race_ok, race_err))
        self.assertEqual(len(db.query("SELECT message_sha256 FROM memecoin_reservations WHERE case_id = ?", (race_case,))), 1)
        winner = race_ok[0]
        self.assertTrue(desk_loop.execution_gate(race_case, winner, transport, WHEN)["execution_eligible"])
        self.assertFalse(desk_loop.execution_gate(race_case, race_err[0], transport, WHEN)["execution_eligible"])
        race_round = desk_loop.run_supervised(transport, WHEN, rounds=1)
        race_listed = [item for item in race_round["discovery"]["reservations"] if item.get("case_id") == race_case and item.get("execution", {}).get("execution_eligible") is True]
        self.assertEqual([item["message_sha256"] for item in race_listed], [winner])
        db.execute(
            "INSERT INTO memecoin_entry_receipts (signature, case_id, message_sha256, slot, qty, cost_basis_lamports, network_fee_lamports, dex_fee_lamports, fee_usd, failed, ledger, ledger_provenance) VALUES ('race-fail', ?, ?, 1, 1, 1, 1, 1, '0.01', 1, 'live', 'server')",
            (race_case, winner),
        )
        self.assertEqual(desk_loop.execution_gate(race_case, winner, transport, WHEN)["reason"], "reservation_failed")
        dual_tx = message("buy", 600_000, chain["blockhash"])[0]
        dual_other = message("buy", 500_000, chain["blockhash"])[0]
        dual_ids = (
            __import__("hashlib").sha256(solana_tx.message_bytes(dual_tx)).hexdigest(),
            __import__("hashlib").sha256(solana_tx.message_bytes(dual_other)).hexdigest(),
        )
        db.execute(
            "INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, created_at, updated_at, ledger, ledger_provenance) VALUES ('dual-case', ?, 'protected', ?, ?, ?, 'live', 'server')",
            (solana_tx.b58encode(b"\x65" * 32), json.dumps({"reserved_usd": "2.00"}), WHEN, WHEN),
        )
        db.execute(second_sql, (dual_ids[0], "dual-case", dual_tx, chain["blockhash"], WHEN))
        db.execute("DROP TRIGGER IF EXISTS memecoin_reservations_active_guard")
        db.execute("DROP TRIGGER IF EXISTS memecoin_reservations_identity_record")
        db.execute("DROP TABLE IF EXISTS memecoin_reservation_active")
        db.execute(second_sql, (dual_ids[1], "dual-case", dual_other, chain["blockhash"], WHEN))
        self.assertEqual(len(db.query("SELECT message_sha256 FROM memecoin_reservations WHERE case_id = 'dual-case'")), 2)
        real_slots = memecoin_desk._replace_reservation_triggers

        def slots_fail(conn):
            real_slots(conn)
            raise RuntimeError("slot upgrade failed")

        memecoin_desk._replace_reservation_triggers = slots_fail
        try:
            with self.assertRaises(RuntimeError):
                memecoin_desk._ensure()
        finally:
            memecoin_desk._replace_reservation_triggers = real_slots
        self.assertEqual(db.query("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'memecoin_reservation_active'"), [])
        self.assertEqual(len(db.query("SELECT message_sha256 FROM memecoin_reservations WHERE case_id = 'dual-case'")), 2)
        self.assertEqual(json.loads(db.query("SELECT snapshot_json FROM memecoin_cases WHERE id = 'dual-case'")[0]["snapshot_json"])["reserved_usd"], "2.00")
        memecoin_desk._ensure()
        db.init_db()
        memecoin_desk._ensure()
        self.assertEqual(db.query("SELECT message_sha256 FROM memecoin_reservation_active WHERE case_id = 'dual-case'")[0]["message_sha256"], "ambiguous")
        self.assertEqual(len(db.query("SELECT message_sha256 FROM memecoin_reservations WHERE case_id = 'dual-case'")), 2)
        dual_calls = []

        def dual_count(request):
            dual_calls.append(request.get("method"))
            raise AssertionError("rpc")

        for dual_id in dual_ids:
            dual_gate = desk_loop.execution_gate("dual-case", dual_id, dual_count, WHEN)
            self.assertEqual(dual_gate["reason"], "reservation_ambiguous", dual_gate)
            self.assertFalse(dual_gate["execution_eligible"])
            self.assertTrue(dual_gate["holds"])
            self.assertFalse(dual_gate["resend"])
        self.assertEqual(dual_calls, [])
        dual_round = desk_loop.run_supervised(transport, WHEN, rounds=1)
        dual_listed = [item for item in dual_round["discovery"]["reservations"] if item.get("case_id") == "dual-case"]
        self.assertEqual(len(dual_listed), 2)
        self.assertTrue(all(item["execution"]["reason"] == "reservation_ambiguous" and item["execution"]["execution_eligible"] is False for item in dual_listed))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(second_sql, ("ab" * 32, "dual-case", dual_tx, chain["blockhash"], WHEN))
        self.assertEqual(json.loads(db.query("SELECT snapshot_json FROM memecoin_cases WHERE id = 'dual-case'")[0]["snapshot_json"])["reserved_usd"], "2.00")
        self.assertEqual(memecoin_desk.view(case_id)["snapshot"]["reserved_usd"], reserved_usd)
        lied = message("buy", 1_000, chain["latest_blockhash"])[0]
        lied_hash = __import__("hashlib").sha256(solana_tx.message_bytes(lied)).hexdigest()
        db.execute(
            "INSERT INTO memecoin_reservations (message_sha256, case_id, side, message_b64, router, blockhash, last_valid_block_height, last_valid_provenance, ledger, ledger_provenance, created_at) VALUES (?, 'lied-case', 'entry', ?, 'metis', ?, 1000000, 'jupiter_order', 'live', 'server', ?)",
            (lied_hash, lied, chain["blockhash"], WHEN),
        )
        calls = []

        def counting(request):
            calls.append(request.get("method"))
            raise AssertionError("rpc")

        lied_gate = desk_loop.execution_gate("lied-case", lied_hash, counting, WHEN)
        self.assertEqual(lied_gate["reason"], "reservation_proof_mismatch")
        self.assertEqual(calls, [])
        self.assertFalse(lied_gate["execution_eligible"])
        plant_entry("spare-paper", "paper", "server", target="spare-case", hashed=spare)
        paper_spare = desk_loop.execution_gate("spare-case", spare, transport, WHEN)
        self.assertNotIn(paper_spare.get("reason"), {"reservation_consumed", "reservation_failed"}, paper_spare)
        self.assertTrue(paper_spare["execution_eligible"], paper_spare)
        listed_spare = desk_loop.run_supervised(transport, WHEN, rounds=1)
        self.assertTrue(any(item.get("message_sha256") == spare for item in listed_spare["discovery"]["reservations"]), listed_spare["discovery"]["reservations"])
        self.assertEqual(memecoin_desk.view(case_id)["snapshot"]["reserved_usd"], reserved_usd)
        self.assertEqual(json.loads(db.query("SELECT snapshot_json FROM memecoin_cases WHERE id = 'spare-case'")[0]["snapshot_json"])["reserved_usd"], "1.00")
        self.assertEqual(memecoin_desk.loss_totals()["live_loss_usd"], loss_before)
        clear_receipt("spare-paper")
        db.execute(
            "INSERT INTO memecoin_entry_receipts (signature, case_id, message_sha256, slot, qty, cost_basis_lamports, network_fee_lamports, dex_fee_lamports, fee_usd, failed, ledger, ledger_provenance) VALUES ('spare-fail', 'spare-case', ?, 1, 1, 1, 1, 1, '0.01', 1, 'live', 'server')",
            (spare,),
        )
        failed_gate = desk_loop.execution_gate("spare-case", spare, counting, WHEN)
        self.assertEqual(failed_gate["reason"], "reservation_failed")
        self.assertEqual(calls, [])
        self.assertNotEqual(failed_gate["reason"], "reservation_consumed")
        omitted = desk_loop.run_supervised(transport, WHEN, rounds=1)
        self.assertFalse(any(item.get("message_sha256") == spare for item in omitted["discovery"]["reservations"]))
        self.assertTrue(any(item.get("message_sha256") == digest for item in omitted["discovery"]["reservations"]))
        self.assertEqual(memecoin_desk.loss_totals()["live_loss_usd"], loss_before)
        db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = ?", (original_snap, case_id))
        chain["height"] = 999_100
        again = desk_loop._screen_quote(transport, WHEN + 5, mint, evidence)
        self.assertEqual(again["reason"], "intent_inflight", again)
        self.assertFalse(again.get("reserved") is True)
        held = desk_loop.recheck_bound(case_id, digest, transport, WHEN + 5)
        self.assertTrue(held["holds"], held)
        self.assertFalse(held["resend"])
        self.assertFalse(held["rewritten"])
        self.assertEqual(held["message_sha256"], digest)
        self.assertEqual(memecoin_desk.view(case_id)["snapshot"]["entry_inflight"]["message_sha256"], digest)
        chain["height"] = 1_000_050
        expired = desk_loop.recheck_bound(case_id, digest, transport, WHEN + 40)
        self.assertEqual(expired["reason"], "blockhash_expired")
        self.assertTrue(expired["holds"])
        self.assertFalse(expired["resend"])
        self.assertFalse(expired["rewritten"])
        self.assertEqual(expired["message_sha256"], digest)
        self.assertEqual(memecoin_desk.view(case_id)["snapshot"]["entry_inflight"]["message_sha256"], digest)
        self.assertEqual(memecoin_desk.view(case_id)["snapshot"]["reserved_usd"], reserved_usd)
        chain["preflight_mode"] = "clear"
        calls_before = chain.get("sim_calls", 0)
        expired_preflight = desk_loop.preflight_reserved(case_id, digest, transport, WHEN + 40)
        self.assertEqual(expired_preflight["reason"], "blockhash_expired")
        self.assertFalse(expired_preflight["attempted"])
        self.assertFalse(expired_preflight["resend"])
        self.assertEqual(chain.get("sim_calls", 0), calls_before)
        changed = desk_loop.preflight_reserved(case_id, "f" * 64, transport, WHEN + 40)
        self.assertEqual(changed["reason"], "message_mismatch")
        self.assertFalse(changed["attempted"])
        self.assertEqual(chain.get("sim_calls", 0), calls_before)
        chain["preflight_mode"] = None
        db.init_db()
        self.assertEqual(memecoin_desk.view(case_id)["snapshot"]["entry_inflight"]["message_sha256"], digest)
        db.execute(
            "INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, created_at, updated_at, ledger, ledger_provenance) VALUES ('legacy-snap', ?, 'protected', ?, ?, ?, 'live', 'server')",
            (solana_tx.b58encode(b"\x62" * 32), json.dumps({"entry_inflight": {"message_sha256": "a" * 64, "blockhash": "legacy"}, "reserved_usd": "1.00"}), WHEN, WHEN),
        )
        chain["boosts_fail"] = True
        restarted = desk_loop.run_supervised(transport, WHEN + 45, rounds=1)
        chain["boosts_fail"] = False
        self.assertIsNotNone(restarted.get("discovery"), restarted)
        listed = [item for item in restarted["discovery"]["reservations"] if item.get("case_id") == case_id]
        self.assertEqual(len(listed), 1, restarted["discovery"]["reservations"])
        self.assertEqual(listed[0]["message_sha256"], digest)
        self.assertEqual(listed[0]["side"], "entry")
        self.assertFalse(listed[0]["requoted"])
        self.assertFalse(listed[0]["jupiter_called"])
        self.assertFalse(listed[0]["execution"]["execution_eligible"])
        self.assertEqual(listed[0]["execution"]["reason"], "blockhash_expired")
        self.assertFalse(listed[0]["execution"]["resend"])
        self.assertEqual(db.query("SELECT message_sha256 FROM memecoin_reservations WHERE case_id = ?", (case_id,))[0]["message_sha256"], digest)
        legacy = next(item for item in restarted["discovery"]["reservations"] if item.get("case_id") == "legacy-snap")
        self.assertEqual(legacy["execution"]["reason"], "reservation_proof_missing")
        self.assertFalse(legacy["execution"]["execution_eligible"])
        self.assertFalse(legacy["requoted"])
        self.assertEqual(json.loads(db.query("SELECT snapshot_json FROM memecoin_cases WHERE id = 'legacy-snap'")[0]["snapshot_json"])["reserved_usd"], "1.00")
        self.assertEqual(json.loads(db.query("SELECT snapshot_json FROM memecoin_cases WHERE id = 'legacy-snap'")[0]["snapshot_json"])["entry_inflight"]["message_sha256"], "a" * 64)
        import urllib.request
        def urlopen(req, timeout=8):
            payload = json.loads(req.data.decode())
            if payload["method"] == "getGenesisHash":
                body = {"jsonrpc": "2.0", "result": solana_inspect.MAINNET_GENESIS}
            else:
                body = {"jsonrpc": "2.0", "result": None}
            class _Response:
                status = 200
                def read(self):
                    return json.dumps(body).encode()
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    return False
            return _Response()
        original = urllib.request.urlopen
        urllib.request.urlopen = urlopen
        self.addCleanup(lambda: setattr(urllib.request, "urlopen", original))
        unknown = memecoin_desk.confirm_entry(case_id, "sig-unknown", WHEN + 50)
        self.assertEqual(unknown["reason"], "receipt_unconfirmed")
        self.assertTrue(unknown["holds"])
        self.assertFalse(unknown["resend"])
        self.assertEqual(memecoin_desk.view(case_id)["snapshot"]["entry_inflight"]["message_sha256"], digest)
        self.assertEqual(memecoin_desk.view(case_id)["snapshot"]["reserved_usd"], reserved_usd)
        watched = desk_loop.run_supervised(transport, WHEN + 50, rounds=1)
        unknown_row = next(item for item in watched["discovery"]["reservations"] if item.get("message_sha256") == digest)
        self.assertEqual(unknown_row["execution"]["reason"], "reservation_unknown")
        self.assertFalse(unknown_row["execution"]["execution_eligible"])
        self.assertFalse(unknown_row["execution"]["resend"])
        self.assertFalse(unknown_row["preflight"]["attempted"])
        self.assertFalse(unknown_row["preflight"]["resend"])
        self.assertFalse(unknown_row["preflight"]["simulate"])
        unknown_calls = []

        def unknown_transport(request):
            unknown_calls.append(request.get("method"))
            raise AssertionError("rpc")

        unknown_gate = desk_loop.execution_gate(case_id, digest, unknown_transport, WHEN + 50)
        self.assertEqual(unknown_gate["reason"], "reservation_unknown")
        self.assertEqual(unknown_calls, [])
        self.assertEqual(memecoin_desk.view(case_id)["snapshot"]["reserved_usd"], reserved_usd)
        unknown_tx = message("buy", 400_000, chain["blockhash"])[0]
        unknown_hash = __import__("hashlib").sha256(solana_tx.message_bytes(unknown_tx)).hexdigest()
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO memecoin_reservations (message_sha256, case_id, side, message_b64, router, blockhash, last_valid_block_height, last_valid_provenance, ledger, ledger_provenance, created_at) VALUES (?, ?, 'entry', ?, 'metis', ?, 1000000, 'jupiter_order', 'live', 'server', ?)",
                (unknown_hash, case_id, unknown_tx, chain["blockhash"], WHEN),
            )
        self.assertEqual([row["message_sha256"] for row in db.query("SELECT message_sha256 FROM memecoin_reservations WHERE case_id = ? AND side = 'entry'", (case_id,))], [digest])
        chain["height"] = 999_000
        results = []

        def once():
            quoted = desk_loop._screen_quote(transport, WHEN + 60, mint, evidence)
            results.append(quoted.get("reserve_reason") or quoted.get("reason"))

        threads = [threading.Thread(target=once) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 4, results)
        self.assertEqual(len(db.query("SELECT message_sha256 FROM memecoin_reservations WHERE case_id = ? AND side = 'entry'", (case_id,))), 1)
        self.assertEqual(memecoin_desk.view(case_id)["snapshot"]["entry_inflight"]["message_sha256"], digest)
        snap = memecoin_desk.view(case_id)["snapshot"]
        snap.pop("entry_inflight", None)
        snap["remaining_raw"] = 1_000
        snap["exit_intent"] = {"source": "server_pool_pyth", "live": True, "action": "stop"}
        db.execute(
            "UPDATE memecoin_cases SET stage = 'exit_intent', snapshot_json = ? WHERE id = ?",
            (json.dumps(snap), case_id),
        )
        db.execute(
            "INSERT INTO memecoin_entry_receipts (signature, case_id, message_sha256, slot, qty, cost_basis_lamports, network_fee_lamports, dex_fee_lamports, fee_usd, failed, ledger, pool, ledger_provenance) VALUES ('entry-bound', ?, ?, 1, 1000, 1000, 1, 1, '0.01', 0, 'live', ?, 'server')",
            (case_id, digest, pool),
        )
        db.execute(
            """
            INSERT INTO memecoin_risk_intents (
                id, case_id, ledger, ledger_provenance, entry_signature, action, pool, slot, mark_price_usd,
                entry_price_usd, stop_price_usd, qty, basis_cost_lamports, basis_decimals, evidence_json, created_at
            ) VALUES ('intent-bound', ?, 'live', 'server', 'entry-bound', 'stop', ?, 1, '0.90', '1', '0.94', 1000, 1000, 6, '{}', ?)
            """,
            (case_id, pool, WHEN),
        )
        chain["side"] = "exit"
        chain["height"] = 999_000
        chain["publish_expiry"] = True
        chain["expire_at"] = None
        chain["malformed"] = False
        import phantom_transport
        payer = phantom_transport.REQUIRED_WALLET
        two_signers = solana_tx.encode_legacy(
            payer,
            [payer, solana_tx.b58encode(b"\x22" * 32), solana_tx.COMPUTE_BUDGET],
            1,
            [(2, [], bytes([2]) + (200_000).to_bytes(4, "little"))],
            chain["blockhash"],
            required=2,
        )
        refused_exit = memecoin_desk.reserve_exit(case_id, two_signers, 1_000, jupiter_route_v2.Snapshot({}, 1, 1, WHEN), transport, WHEN)
        self.assertEqual(refused_exit["reason"], "extra_signer", refused_exit)
        self.assertFalse(refused_exit.get("live_ready"))
        self.assertIsNone(memecoin_desk.view(case_id)["snapshot"].get("inflight"))
        two_econ = desk_loop._exact_message_economics(solana_tx.decode_transaction(__import__("base64").b64decode(two_signers)))
        self.assertEqual(two_econ["economic_reason"], "extra_signer")
        self.assertFalse(two_econ["instruction_slippage_within_cap"])
        self.assertFalse(two_econ["network_fee_instruction_proven"])
        exited = desk_loop.run_supervised(transport, WHEN, rounds=1)
        self.assertTrue(exited.get("discovery"), exited)
        position = next(item for item in exited["discovery"]["positions"] if item.get("mint") == mint)
        self.assertFalse(position["quote"]["quote_expiry_proven"])
        self.assertTrue(position["quote"]["inclusion_window_proven"], position)
        self.assertTrue(position["execution"]["execution_eligible"], position)
        self.assertEqual(position["execution"]["operation"], "unimplemented")
        self.assertFalse(position["execution"]["sign"])
        self.assertFalse(position["execution"]["send"])
        self.assertTrue(position["quote"]["reserved"], position)
        self.assertEqual(position["quote"]["pool"], pool)
        self.assertLessEqual(position["quote"]["requested_slippage_bps"], 50)
        self.assertFalse(position["quote"]["send"])
        stored = memecoin_desk.view(case_id)["snapshot"]["inflight"]
        self.assertEqual(stored["qty"], 1_000)
        self.assertEqual(stored["pool"], pool)
        self.assertEqual(stored["blockhash"], chain["blockhash"])
        self.assertEqual(db.query("SELECT side FROM memecoin_reservations WHERE message_sha256 = ?", (digest,))[0]["side"], "entry")
        self.assertNotEqual(digest, stored["message_sha256"])
        chain["preflight_mode"] = "clear"
        chain["sim_calls"] = 0
        exit_round = desk_loop.run_supervised(transport, WHEN, rounds=1)
        exit_preflight = next(item["preflight"] for item in exit_round["discovery"]["reservations"] if item.get("message_sha256") == stored["message_sha256"])
        self.assertEqual(exit_preflight["side"], "exit")
        self.assertTrue(exit_preflight["simulation_clear"], exit_preflight)
        self.assertTrue(exit_preflight["account_validated"], exit_preflight)
        self.assertFalse(exit_preflight["network_fee_arithmetic_match"])
        self.assertTrue(exit_preflight["requested_priority_arithmetic_match"], exit_preflight)
        self.assertFalse(exit_preflight["fee_lamports_match"], exit_preflight)
        self.assertFalse(exit_preflight["fee_composition_proven"])
        self.assertFalse(exit_preflight["rent_bound_proven"])
        self.assertTrue(exit_preflight["rent_unproven"])
        self.assertFalse(exit_preflight["preflight_ready"], exit_preflight)
        self.assertEqual(exit_preflight["reason"], "fee_composition_unproven")
        self.assertFalse(exit_preflight["instruction_slippage_within_cap"])
        self.assertEqual(exit_preflight["economic_reason"], "instruction_unsupported")
        self.assertFalse(exit_preflight["network_fee_instruction_proven"])
        self.assertFalse(exit_preflight["base_fee_proven"])
        self.assertTrue(exit_preflight["fee_for_message_observed"], exit_preflight)
        self.assertEqual(exit_preflight["fee_for_message_lamports"], 5000)
        self.assertFalse(exit_preflight["charged_fee_proven"])
        self.assertEqual(chain["fee_messages"][-1], __import__("base64").b64encode(solana_tx.message_bytes(db.query("SELECT message_b64 FROM memecoin_reservations WHERE message_sha256 = ?", (stored["message_sha256"],))[0]["message_b64"])).decode("ascii"))
        for label, amount in (("zero", 0), ("max", 2**64 - 1)):
            chain["fee_mode"] = "value"
            chain["fee_value"] = amount
            exit_fee = desk_loop.preflight_reserved(case_id, stored["message_sha256"], transport, WHEN)
            self.assertTrue(exit_fee["fee_for_message_observed"], label)
            self.assertEqual(exit_fee["fee_for_message_lamports"], amount)
            self.assertFalse(exit_fee["charged_fee_proven"], label)
            self.assertFalse(exit_fee["preflight_ready"], label)
            self.assertFalse(exit_fee["gate_complete"], label)
            self.assertFalse(exit_fee["fee_usd_proven"], label)
            self.assertNotIn("fee_for_message", json.dumps(memecoin_desk.view(case_id)["snapshot"]))
        for label, amount in (("overflow", 2**64), ("huge", 10**30), ("negative", -1), ("bool", True), ("noninteger", 1.5)):
            chain["fee_mode"] = "value"
            chain["fee_value"] = amount
            exit_fee = desk_loop.preflight_reserved(case_id, stored["message_sha256"], transport, WHEN)
            self.assertFalse(exit_fee["fee_for_message_observed"], label)
            self.assertIsNone(exit_fee["fee_for_message_lamports"], label)
            self.assertEqual(exit_fee["fee_for_message_reason"], "fee_for_message_malformed", label)
            self.assertFalse(exit_fee["charged_fee_proven"], label)
            self.assertFalse(exit_fee["preflight_ready"], label)
        chain["fee_mode"] = "clear"
        chain.pop("fee_value", None)
        self.assertEqual(db.query("SELECT network_fee_lamports FROM memecoin_entry_receipts WHERE signature = 'entry-bound'")[0]["network_fee_lamports"], 1)
        self.assertFalse(exit_preflight["slippage_proven_by_simulation"])
        self.assertFalse(exit_preflight["program_rounding_proven"])
        self.assertFalse(exit_preflight["dex_fee_proven"])
        self.assertFalse(exit_preflight["combined_fee_usd_proven"])
        self.assertFalse(exit_preflight["ticket_limit_proven"])
        self.assertFalse(exit_preflight["allocation_proven"])
        self.assertFalse(exit_preflight["gate_complete"])
        self.assertFalse(exit_preflight["price_freshness_proven"])
        self.assertFalse(exit_preflight["sign"])
        self.assertFalse(exit_preflight["send"])
        self.assertEqual(chain["last_sim"]["transaction"], db.query("SELECT message_b64 FROM memecoin_reservations WHERE message_sha256 = ?", (stored["message_sha256"],))[0]["message_b64"])
        self.assertFalse(chain["last_sim"]["replaceRecentBlockhash"])
        chain["preflight_mode"] = None
        spent = []

        def counting(request):
            method = request.get("method")
            if method in {"isBlockhashValid", "getSlot", "getBlockHeight", "getLatestBlockhash", "getGenesisHash"}:
                spent.append(method)
                raise AssertionError(method)
            return transport(request)

        consumed_entry = desk_loop.execution_gate(case_id, digest, counting, WHEN)
        self.assertEqual(consumed_entry["reason"], "reservation_consumed")
        self.assertEqual(spent, [])
        self.assertFalse(consumed_entry["execution_eligible"])
        exit_hash = stored["message_sha256"]
        self.assertEqual(desk_loop.execution_gate(case_id, exit_hash, transport, WHEN)["side"], "exit")
        body = memecoin_desk.view(case_id)["snapshot"]
        body.pop("inflight", None)
        db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = ?", (json.dumps(body), case_id))
        self.assertEqual(memecoin_desk.receipt_remaining(case_id)["remaining"], 1_000)

        def assert_exit_still_open(sig):
            gate = desk_loop.execution_gate(case_id, exit_hash, transport, WHEN)
            self.assertNotIn(gate.get("reason"), {"reservation_consumed", "reservation_failed"}, (sig, gate))
            self.assertTrue(gate["execution_eligible"], (sig, gate))
            chain["boosts_fail"] = True
            supervised = desk_loop.run_supervised(transport, WHEN, rounds=1)
            chain["boosts_fail"] = False
            self.assertTrue(any(item.get("message_sha256") == exit_hash for item in supervised["discovery"]["reservations"]), sig)
            self.assertEqual(len(db.query("SELECT message_sha256 FROM memecoin_reservations WHERE case_id = ? AND side = 'exit'", (case_id,))), 1, sig)
            self.assertEqual(memecoin_desk.view(case_id)["snapshot"]["reserved_usd"], reserved_usd, sig)
            self.assertEqual(memecoin_desk.loss_totals()["live_loss_usd"], loss_before, sig)
            clear_receipt(sig)

        db.execute(
            "INSERT INTO memecoin_entry_receipts (signature, case_id, message_sha256, slot, qty, cost_basis_lamports, network_fee_lamports, dex_fee_lamports, fee_usd, failed, ledger, pool, ledger_provenance) VALUES ('paper-buy', ?, ?, 1, 5000, 1, 1, 1, '0.01', 0, 'paper', ?, 'server')",
            (case_id, digest, pool),
        )
        self.assertEqual(memecoin_desk.receipt_remaining(case_id)["remaining"], 1_000)
        clear_receipt("paper-buy")
        for sig, ledger, provenance in (
            ("paper-exit", "paper", "server"),
            ("synthetic-exit", "synthetic", "server"),
        ):
            db.execute(
                "INSERT INTO memecoin_exit_receipts (signature, case_id, message_sha256, slot, fee_usd, qty, ledger, ledger_provenance) VALUES (?, ?, ?, 2, '0.01', 400, ?, ?)",
                (sig, case_id, exit_hash, ledger, provenance),
            )
            self.assertEqual(memecoin_desk.receipt_remaining(case_id)["remaining"], 1_000, sig)
            assert_exit_still_open(sig)
        db.execute(
            "INSERT INTO memecoin_exit_receipts (signature, case_id, message_sha256, slot, fee_usd, qty, ledger, ledger_provenance) VALUES ('paper-legacy-exit', ?, ?, 2, '0.01', 99999, 'paper', 'legacy_default')",
            (case_id, exit_hash),
        )
        self.assertEqual(memecoin_desk.receipt_remaining(case_id)["remaining"], 1_000)
        clear_receipt("paper-legacy-exit")
        db.execute(
            "INSERT INTO memecoin_exit_receipts (signature, case_id, message_sha256, slot, fee_usd, qty, ledger, ledger_provenance) VALUES ('legacy-exit', ?, ?, 2, '0.01', 400, 'live', 'legacy_default')",
            (case_id, exit_hash),
        )
        self.assertEqual(memecoin_desk.receipt_remaining(case_id)["reason"], "provenance_unproven")
        assert_exit_still_open("legacy-exit")
        self.assertEqual(memecoin_desk.receipt_remaining(case_id)["remaining"], 1_000)
        db.execute("DROP TRIGGER IF EXISTS memecoin_exit_receipts_ledger")
        db.execute(
            "INSERT INTO memecoin_exit_receipts (signature, case_id, message_sha256, slot, fee_usd, qty, ledger, ledger_provenance) VALUES ('unknown-exit', ?, ?, 2, '0.01', 400, 'unknown', 'server')",
            (case_id, exit_hash),
        )
        db.execute(
            "INSERT INTO memecoin_exit_receipts (signature, case_id, message_sha256, slot, fee_usd, qty) VALUES ('missing-exit', ?, ?, 2, '0.01', 400)",
            (case_id, exit_hash),
        )
        memecoin_desk._ensure()
        self.assertEqual(memecoin_desk.receipt_remaining(case_id)["reason"], "provenance_unproven")
        assert_exit_still_open("unknown-exit")
        clear_receipt("missing-exit")
        self.assertEqual(memecoin_desk.receipt_remaining(case_id)["remaining"], 1_000)
        db.execute(
            "INSERT INTO memecoin_exit_receipts (signature, case_id, message_sha256, slot, fee_usd, qty, ledger, ledger_provenance) VALUES ('cross-case-exit', 'other-case', ?, 2, '0.01', 400, 'live', 'server')",
            (exit_hash,),
        )
        self.assertEqual(memecoin_desk.receipt_remaining(case_id)["remaining"], 1_000)
        assert_exit_still_open("cross-case-exit")
        db.execute(
            "INSERT INTO memecoin_entry_receipts (signature, case_id, message_sha256, slot, qty, cost_basis_lamports, network_fee_lamports, dex_fee_lamports, fee_usd, failed, ledger, pool, ledger_provenance) VALUES ('cross-side-exit', ?, ?, 1, 0, 1, 1, 1, '0.01', 0, 'live', ?, 'server')",
            (case_id, exit_hash, pool),
        )
        crossed = desk_loop.execution_gate(case_id, exit_hash, transport, WHEN)
        self.assertNotEqual(crossed.get("reason"), "reservation_consumed", crossed)
        chain["boosts_fail"] = True
        crossed_round = desk_loop.run_supervised(transport, WHEN, rounds=1)
        chain["boosts_fail"] = False
        self.assertTrue(any(item.get("message_sha256") == exit_hash for item in crossed_round["discovery"]["reservations"]))
        self.assertEqual(len(db.query("SELECT message_sha256 FROM memecoin_reservations WHERE case_id = ? AND side = 'exit'", (case_id,))), 1)
        self.assertEqual(memecoin_desk.view(case_id)["snapshot"]["reserved_usd"], reserved_usd)
        clear_receipt("cross-side-exit")
        self.assertEqual(memecoin_desk.receipt_remaining(case_id)["remaining"], 1_000)
        slot_replace = "INSERT OR REPLACE INTO memecoin_reservation_active (case_id, side, message_sha256) VALUES (?, 'exit', 'shifted')"
        slot_upsert = "INSERT INTO memecoin_reservation_active (case_id, side, message_sha256) VALUES (?, 'exit', 'shifted') ON CONFLICT(case_id, side) DO UPDATE SET message_sha256 = 'shifted'"
        reject_raw(slot_replace, (case_id,))
        reject_raw(slot_upsert, (case_id,))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(slot_replace, (case_id,))
        self.assertEqual(db.query("SELECT message_sha256 FROM memecoin_reservation_active WHERE case_id = ? AND side = 'exit'", (case_id,))[0]["message_sha256"], exit_hash)
        db.execute("DROP TRIGGER IF EXISTS memecoin_reservation_active_pin_delete")
        db.execute("DROP TRIGGER IF EXISTS memecoin_reservation_active_pin_guard")
        db.execute("DELETE FROM memecoin_reservation_active_pin WHERE case_id = ? AND side = 'exit'", (case_id,))
        raw_slot = sqlite3.connect(desk_path)
        try:
            self.assertEqual(raw_slot.execute("PRAGMA recursive_triggers").fetchone()[0], 0)
            raw_slot.execute(slot_replace, (case_id,))
            raw_slot.commit()
        finally:
            raw_slot.close()
        self.assertEqual(db.query("SELECT message_sha256 FROM memecoin_reservation_active WHERE case_id = ? AND side = 'exit'", (case_id,))[0]["message_sha256"], "shifted")
        memecoin_desk._ensure()
        self.assertEqual(db.query("SELECT message_sha256 FROM memecoin_reservation_active WHERE case_id = ? AND side = 'exit'", (case_id,))[0]["message_sha256"], exit_hash)
        self.assertEqual(db.query("SELECT message_sha256 FROM memecoin_reservation_active_pin WHERE case_id = ? AND side = 'exit'", (case_id,))[0]["message_sha256"], exit_hash)
        reject_raw(slot_replace, (case_id,))
        self.assertEqual(memecoin_desk.view(case_id)["snapshot"]["reserved_usd"], reserved_usd)
        db.execute(
            "INSERT INTO memecoin_exit_receipts (signature, case_id, message_sha256, slot, fee_usd, qty, ledger, ledger_provenance) VALUES ('exit-slice-1', ?, ?, 2, '0.01', 400, 'live', 'server')",
            (case_id, exit_hash),
        )
        consumed_exit = desk_loop.execution_gate(case_id, exit_hash, counting, WHEN)
        self.assertEqual(consumed_exit["reason"], "reservation_consumed")
        self.assertEqual(spent, [])
        self.assertEqual(db.query("SELECT last_valid_block_height FROM memecoin_reservations WHERE message_sha256 = ?", (exit_hash,))[0]["last_valid_block_height"], 1_000_000)
        chain["boosts_fail"] = True
        quiet = desk_loop.run_supervised(counting, WHEN, rounds=1)
        chain["boosts_fail"] = False
        self.assertIsNotNone(quiet.get("discovery"), quiet)
        self.assertFalse(any(item.get("message_sha256") in {digest, exit_hash} for item in quiet["discovery"]["reservations"]))
        self.assertFalse(any(item in {"isBlockhashValid", "getBlockHeight", "getLatestBlockhash"} for item in spent))
        self.assertEqual(memecoin_desk.receipt_remaining(case_id)["remaining"], 600)
        db.execute(
            "INSERT INTO memecoin_exit_receipts (signature, case_id, message_sha256, slot, fee_usd, qty, ledger, ledger_provenance) VALUES ('paper-sized-exit', ?, ?, 2, '0.01', 99999, 'paper', 'legacy_default')",
            (case_id, exit_hash),
        )
        self.assertEqual(memecoin_desk.receipt_remaining(case_id)["remaining"], 600)
        db.execute(
            "INSERT INTO memecoin_exit_receipts (signature, case_id, message_sha256, slot, fee_usd, qty, ledger, ledger_provenance) VALUES ('legacy-sized-exit', ?, ?, 2, '0.01', 50, 'live', 'legacy_default')",
            (case_id, exit_hash),
        )
        self.assertEqual(memecoin_desk.receipt_remaining(case_id)["reason"], "provenance_unproven")
        chain["boosts_fail"] = True
        blocked = desk_loop.run_supervised(transport, WHEN, rounds=1)
        chain["boosts_fail"] = False
        blocked_position = next(item for item in blocked["discovery"]["positions"] if item.get("mint") == mint)
        self.assertEqual(blocked_position["reason"], "provenance_unproven")
        self.assertFalse(blocked_position.get("jupiter_called") is True)
        self.assertEqual(len(db.query("SELECT message_sha256 FROM memecoin_reservations WHERE case_id = ? AND side = 'exit'", (case_id,))), 1)
        self.assertEqual(memecoin_desk.view(case_id)["snapshot"]["reserved_usd"], reserved_usd)
        clear_receipt("legacy-sized-exit")
        self.assertEqual(memecoin_desk.receipt_remaining(case_id)["remaining"], 600)
        chain["blockhash"] = solana_tx.b58encode(b"\x4a" * 32)
        chain["height"] = 999_000
        chain["side"] = "exit"
        nxt = desk_loop.run_supervised(transport, WHEN, rounds=1)
        self.assertTrue(nxt.get("discovery"), nxt)
        follow = next(item for item in nxt["discovery"]["positions"] if item.get("mint") == mint)
        self.assertTrue(follow["quote"]["reserved"], follow)
        self.assertEqual(follow["quote"]["amount"], 600)
        self.assertFalse(follow["quote"]["price_freshness_proven"])
        self.assertFalse(follow["quote"]["gate_complete"])
        self.assertEqual(follow["execution"]["operation"], "unimplemented")
        fresh = db.query("SELECT message_sha256, blockhash FROM memecoin_reservations WHERE case_id = ? AND side = 'exit' AND message_sha256 != ?", (case_id, exit_hash))
        self.assertEqual(len(fresh), 1)
        self.assertNotEqual(fresh[0]["message_sha256"], exit_hash)
        self.assertEqual(fresh[0]["blockhash"], chain["blockhash"])
        self.assertNotEqual(fresh[0]["blockhash"], chain["latest_blockhash"])
        self.assertEqual(desk_loop.execution_gate(case_id, exit_hash, counting, WHEN)["reason"], "reservation_consumed")
        self.assertEqual(memecoin_desk.view(case_id)["snapshot"]["reserved_usd"], reserved_usd)
        self.assertEqual(db.query("SELECT message_sha256 FROM memecoin_reservations WHERE message_sha256 = ?", (exit_hash,))[0]["message_sha256"], exit_hash)
        self.assertFalse(desk_loop.execution_gate(case_id, exit_hash, counting, WHEN)["execution_eligible"])
        self.assertFalse(desk_loop.scheduler_status()["armed"])

    def test_predecessor_preflight_attempt_cannot_be_replaced(self):
        import shutil
        import sqlite3
        import subprocess
        import sys
        import tempfile
        import threading
        import db
        root = Path(__file__).resolve().parents[1]
        source = tempfile.mkdtemp()
        built = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(source, ignore_errors=True))
        self.addCleanup(lambda: shutil.rmtree(built, ignore_errors=True))
        archive = subprocess.run(["git", "archive", "3c48ff3cd45ea36c8301ceb6c828d4839b18b692"], cwd=root, check=True, capture_output=True)
        subprocess.run(["tar", "-x"], cwd=source, check=True, input=archive.stdout)
        script = r"""
import os, sqlite3
import db
import memecoin_desk
db.init_db()
memecoin_desk._ensure()
db.execute("INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, created_at, updated_at, ledger, ledger_provenance) VALUES ('bound-case', 'mint-bound', 'protected', '{\"reserved_usd\": \"8.00\"}', 1, 1, 'live', 'server')")
db.execute("INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, created_at, updated_at, ledger, ledger_provenance) VALUES ('other-case', 'mint-other', 'protected', '{\"reserved_usd\": \"1.00\"}', 1, 1, 'live', 'server')")
db.execute("INSERT INTO memecoin_reservations (message_sha256, case_id, side, message_b64, router, blockhash, last_valid_block_height, last_valid_provenance, ledger, ledger_provenance, created_at) VALUES ('entryhashentryhashentryhashentryhashentry', 'bound-case', 'entry', 'YQ==', 'metis', 'entry-block', 1000000, 'jupiter_order', 'live', 'server', 1)")
db.execute("INSERT INTO memecoin_reservations (message_sha256, case_id, side, message_b64, router, blockhash, last_valid_block_height, last_valid_provenance, ledger, ledger_provenance, created_at) VALUES ('exithashexithashexithashexithashexit', 'bound-case', 'exit', 'Yg==', 'metis', 'exit-block', 1000000, 'jupiter_order', 'live', 'server', 1)")
db.execute("INSERT INTO memecoin_reservations (message_sha256, case_id, side, message_b64, router, blockhash, last_valid_block_height, last_valid_provenance, ledger, ledger_provenance, created_at) VALUES ('otherhashotherhashotherhashotherhashot', 'other-case', 'entry', 'Yw==', 'metis', 'other-block', 1000000, 'jupiter_order', 'live', 'server', 1)")
db.execute("INSERT INTO memecoin_preflight_attempts (message_sha256, case_id, side, blockhash, context_slot, err_clear, created_at) VALUES ('entryhashentryhashentryhashentryhashentry', 'bound-case', 'entry', 'entry-block', 2000028, 0, 1)")
path = os.path.join(os.environ["DESK_DATA_DIR"], "desk.db")
conn = sqlite3.connect(path)
conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
conn.commit()
conn.close()
"""
        env = os.environ.copy()
        env["DESK_DATA_DIR"] = built
        env["PYTHONPATH"] = source + os.pathsep + env.get("PYTHONPATH", "")
        env.pop("STALKCHAIN_API_KEY", None)
        ran = subprocess.run([sys.executable, "-c", script], cwd=source, env=env, capture_output=True, text=True)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        desk = Path(os.environ["DESK_DATA_DIR"]) / "desk.db"
        for suffix in ("", "-wal", "-shm"):
            extra = Path(str(desk) + suffix)
            if extra.exists():
                extra.unlink()
        shutil.copy(Path(built) / "desk.db", desk)
        path = str(desk)
        original = ("entryhashentryhashentryhashentryhashentry", "bound-case", "entry", "entry-block", 2000028, 0)
        retarget = "INSERT OR REPLACE INTO memecoin_preflight_attempts (id, message_sha256, case_id, side, blockhash, context_slot, err_clear, created_at) VALUES (1, 'exithashexithashexithashexithashexit', 'bound-case', 'exit', 'exit-block', 999, 1, 9)"

        def row(conn):
            return tuple(conn.execute("SELECT message_sha256, case_id, side, blockhash, context_slot, err_clear FROM memecoin_preflight_attempts WHERE id = 1").fetchone())

        raw = sqlite3.connect(path)
        try:
            self.assertEqual(raw.execute("PRAGMA recursive_triggers").fetchone()[0], 0)
            raw.execute("BEGIN")
            raw.execute(retarget)
            self.assertEqual(row(raw), ("exithashexithashexithashexithashexit", "bound-case", "exit", "exit-block", 999, 1))
            raw.rollback()
            self.assertEqual(row(raw), original)
        finally:
            raw.close()
        before = sqlite3.connect(path)
        try:
            names = {item[0] for item in before.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'memecoin_preflight%'")}
        finally:
            before.close()
        self.assertIn("memecoin_preflight_attempts_insert", names)
        self.assertNotIn("memecoin_preflight_attempts_record", names)
        real_install = memecoin_desk._install_preflight_audit

        def explode(conn):
            real_install(conn)
            raise RuntimeError("preflight audit failed")

        memecoin_desk._install_preflight_audit = explode
        try:
            with self.assertRaises(RuntimeError):
                memecoin_desk._ensure()
        finally:
            memecoin_desk._install_preflight_audit = real_install
        rolled = sqlite3.connect(path)
        try:
            rolled_names = {item[0] for item in rolled.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'memecoin_preflight%'")}
            self.assertEqual(rolled_names, names)
            rolled.execute("BEGIN")
            rolled.execute(retarget)
            self.assertEqual(row(rolled), ("exithashexithashexithashexithashexit", "bound-case", "exit", "exit-block", 999, 1))
            rolled.rollback()
            self.assertEqual(row(rolled), original)
        finally:
            rolled.close()
        memecoin_desk._ensure()
        db.init_db()
        stored = db.query("SELECT message_sha256, case_id, side, blockhash, context_slot, err_clear FROM memecoin_preflight_attempts WHERE id = 1")[0]
        self.assertEqual(tuple(stored[key] for key in ("message_sha256", "case_id", "side", "blockhash", "context_slot", "err_clear")), original)
        pinned = db.query("SELECT message_sha256, case_id, side, blockhash, context_slot, err_clear FROM memecoin_preflight_attempt_pin WHERE id = 1")[0]
        self.assertEqual(tuple(pinned[key] for key in ("message_sha256", "case_id", "side", "blockhash", "context_slot", "err_clear")), original)
        self.assertEqual(db.query("SELECT id FROM memecoin_preflight_attempt_anchor WHERE id = 1")[0]["id"], 1)
        self.assertEqual(db.query("SELECT id FROM memecoin_preflight_attempt_seal WHERE id = 1")[0]["id"], 1)

        def reject(sql, params=()):
            raw = sqlite3.connect(path)
            try:
                self.assertEqual(raw.execute("PRAGMA recursive_triggers").fetchone()[0], 0)
                with self.assertRaises(sqlite3.IntegrityError):
                    raw.execute(sql, params)
                raw.rollback()
                self.assertEqual(row(raw), original)
            finally:
                raw.close()
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(sql, params)
            stayed = db.query("SELECT message_sha256, case_id, side, blockhash, context_slot, err_clear FROM memecoin_preflight_attempts WHERE id = 1")[0]
            self.assertEqual(tuple(stayed[key] for key in ("message_sha256", "case_id", "side", "blockhash", "context_slot", "err_clear")), original)

        reject(retarget)
        reject("INSERT INTO memecoin_preflight_attempts (id, message_sha256, case_id, side, blockhash, context_slot, err_clear, created_at) VALUES (1, 'exithashexithashexithashexithashexit', 'bound-case', 'exit', 'exit-block', 999, 1, 9) ON CONFLICT(id) DO UPDATE SET message_sha256 = 'exithashexithashexithashexithashexit', case_id = 'bound-case', side = 'exit', blockhash = 'exit-block', context_slot = 999, err_clear = 1")
        reject("INSERT OR REPLACE INTO memecoin_preflight_attempts (id, message_sha256, case_id, side, blockhash, context_slot, err_clear, created_at) VALUES (1, 'otherhashotherhashotherhashotherhashot', 'other-case', 'entry', 'other-block', 2000028, 0, 1)")
        reject("INSERT OR REPLACE INTO memecoin_preflight_attempts (id, message_sha256, case_id, side, blockhash, context_slot, err_clear, created_at) VALUES (1, 'exithashexithashexithashexithashexit', 'bound-case', 'entry', 'entry-block', 2000028, 0, 1)")
        reject("INSERT OR REPLACE INTO memecoin_preflight_attempts (id, message_sha256, case_id, side, blockhash, context_slot, err_clear, created_at) VALUES (1, 'entryhashentryhashentryhashentryhashentry', 'bound-case', 'exit', 'entry-block', 2000028, 0, 1)")
        reject("INSERT OR REPLACE INTO memecoin_preflight_attempts (id, message_sha256, case_id, side, blockhash, context_slot, err_clear, created_at) VALUES (1, 'entryhashentryhashentryhashentryhashentry', 'bound-case', 'entry', 'entry-block', 999, 0, 1)")
        reject("INSERT OR REPLACE INTO memecoin_preflight_attempt_pin (id, message_sha256, case_id, side, blockhash, context_slot, err_clear) VALUES (1, 'exithashexithashexithashexithashexit', 'bound-case', 'exit', 'exit-block', 999, 1)")
        reject("INSERT OR REPLACE INTO memecoin_preflight_attempt_anchor (id) VALUES (1)")
        reject("UPDATE memecoin_preflight_attempts SET context_slot = 999 WHERE id = 1")
        reject("DELETE FROM memecoin_preflight_attempts WHERE id = 1")
        db.execute(
            "INSERT INTO memecoin_preflight_attempts (message_sha256, case_id, side, blockhash, context_slot, err_clear, created_at) VALUES ('entryhashentryhashentryhashentryhashentry', 'bound-case', 'entry', 'entry-block', 2000028, 1, 2)"
        )
        fresh = db.query("SELECT id, message_sha256, side, context_slot FROM memecoin_preflight_attempts WHERE id != 1")
        self.assertEqual(len(fresh), 1, fresh)
        self.assertNotEqual(fresh[0]["id"], 1)
        self.assertEqual(fresh[0]["message_sha256"], original[0])
        self.assertEqual(db.query("SELECT side, context_slot FROM memecoin_preflight_attempt_pin WHERE id = ?", (fresh[0]["id"],))[0]["side"], "entry")
        errors = []

        def append():
            try:
                db.execute(
                    "INSERT INTO memecoin_preflight_attempts (message_sha256, case_id, side, blockhash, context_slot, err_clear, created_at) VALUES ('entryhashentryhashentryhashentryhashentry', 'bound-case', 'entry', 'entry-block', 2000028, 1, 3)"
                )
            except Exception as exc:
                errors.append(repr(exc))

        threads = [threading.Thread(target=append) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        ids = [item["id"] for item in db.query("SELECT id FROM memecoin_preflight_attempts ORDER BY id")]
        self.assertEqual(ids[0], 1)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(ids), 4)
        stayed = db.query("SELECT message_sha256, case_id, side, blockhash, context_slot, err_clear FROM memecoin_preflight_attempts WHERE id = 1")[0]
        self.assertEqual(tuple(stayed[key] for key in ("message_sha256", "case_id", "side", "blockhash", "context_slot", "err_clear")), original)
        self.assertEqual(db.exclusive(lambda conn: conn.execute("PRAGMA user_version").fetchone()[0]), 12)
        self.assertEqual(db.get_setting("memecoin_lineage_trigger"), "14")

    def test_predecessor_slot_pointer_is_repaired_from_the_proofs(self):
        import hashlib
        import json
        import shutil
        import sqlite3
        import subprocess
        import sys
        import tempfile
        from tests.test_jupiter_route_v2 import _metas, _pool, _route, _tx
        import db
        root = Path(__file__).resolve().parents[1]
        source = tempfile.mkdtemp()
        built = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(source, ignore_errors=True))
        self.addCleanup(lambda: shutil.rmtree(built, ignore_errors=True))
        archive = subprocess.run(["git", "archive", "32534f5ddffb8971b458c272b156efa1b50e20d8"], cwd=root, check=True, capture_output=True)
        subprocess.run(["tar", "-x"], cwd=source, check=True, input=archive.stdout)
        env = os.environ.copy()
        env["DESK_DATA_DIR"] = built
        env["PYTHONPATH"] = source + os.pathsep + env.get("PYTHONPATH", "")
        env.pop("STALKCHAIN_API_KEY", None)
        ran = subprocess.run([sys.executable, "-c", "import db, memecoin_desk\ndb.init_db()\nmemecoin_desk.loss_totals()\n"], cwd=source, env=env, capture_output=True, text=True)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        desk = Path(os.environ["DESK_DATA_DIR"]) / "desk.db"
        for suffix in ("", "-wal", "-shm"):
            extra = Path(str(desk) + suffix)
            if extra.exists():
                extra.unlink()
        shutil.copy(Path(built) / "desk.db", desk)
        mint = solana_tx.b58encode(b"\x71" * 32)
        values, _accounts = _pool(solana_tx.NATIVE_MINT, mint)
        blockhash = solana_tx.b58encode(b"\x46" * 32)

        def proof(amount: int):
            tx = _tx(_metas(solana_tx.NATIVE_MINT, mint, values), _route(1_000, 0, amount=amount), side="buy", blockhash=blockhash)
            return tx, hashlib.sha256(solana_tx.message_bytes(tx)).hexdigest()

        open_tx, open_hash = proof(1_000_000)
        consumed_tx, consumed_hash = proof(900_000)
        unknown_tx, unknown_hash = proof(800_000)
        first_tx, first_hash = proof(700_000)
        second_tx, second_hash = proof(600_000)
        fresh_tx, fresh_hash = proof(500_000)
        rows = {
            "open-case": (open_tx, open_hash, "8.00"),
            "consumed-case": (consumed_tx, consumed_hash, "4.00"),
            "unknown-case": (unknown_tx, unknown_hash, "3.00"),
            "dual-case": (first_tx, first_hash, "2.00"),
        }
        for case_id, (tx, digest, hold) in rows.items():
            db.execute(
                "INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, created_at, updated_at, ledger, ledger_provenance) VALUES (?, ?, 'protected', ?, 1, 1, 'live', 'server')",
                (case_id, mint, json.dumps({"reserved_usd": hold, "entry_inflight": {"message_sha256": digest}})),
            )
            db.execute(
                "INSERT INTO memecoin_reservations (message_sha256, case_id, side, message_b64, router, blockhash, last_valid_block_height, last_valid_provenance, ledger, ledger_provenance, created_at) VALUES (?, ?, 'entry', ?, 'metis', ?, 1000000, 'jupiter_order', 'live', 'server', 1)",
                (digest, case_id, tx, blockhash),
            )
        db.execute(
            "INSERT INTO memecoin_entry_receipts (signature, case_id, message_sha256, slot, qty, cost_basis_lamports, network_fee_lamports, dex_fee_lamports, fee_usd, failed, ledger, ledger_provenance) VALUES ('consumed-fill', 'consumed-case', ?, 1, 900, 11, 1, 1, '0.01', 0, 'live', 'server')",
            (consumed_hash,),
        )
        db.execute(
            "INSERT INTO memecoin_reservation_active (case_id, side, message_sha256) VALUES ('consumed-case', 'entry', ?)",
            (consumed_hash,),
        )
        db.execute(
            "INSERT INTO memecoin_reservation_signatures (signature, message_sha256, case_id, kind, created_at) VALUES ('sig-unknown-slot', ?, 'unknown-case', 'unknown', 1)",
            (unknown_hash,),
        )
        db.execute("DROP TRIGGER IF EXISTS memecoin_reservations_active_guard")
        db.execute("DROP TRIGGER IF EXISTS memecoin_reservations_identity_record")
        db.execute(
            "INSERT INTO memecoin_reservations (message_sha256, case_id, side, message_b64, router, blockhash, last_valid_block_height, last_valid_provenance, ledger, ledger_provenance, created_at) VALUES (?, 'dual-case', 'entry', ?, 'metis', ?, 1000000, 'jupiter_order', 'live', 'server', 1)",
            (second_hash, second_tx, blockhash),
        )
        db.execute(
            "INSERT INTO memecoin_reservation_active (case_id, side, message_sha256) VALUES ('stray-case', 'entry', 'other-case-pointer')",
        )
        path = str(desk)
        raw = sqlite3.connect(path)
        try:
            self.assertEqual(raw.execute("PRAGMA recursive_triggers").fetchone()[0], 0)
            raw.execute("INSERT OR REPLACE INTO memecoin_reservation_active (case_id, side, message_sha256) VALUES ('open-case', 'entry', 'shifted')")
            raw.commit()
        finally:
            raw.close()
        self.assertEqual(db.query("SELECT message_sha256 FROM memecoin_reservation_active WHERE case_id = 'open-case'")[0]["message_sha256"], "shifted")
        holds = {row["id"]: json.loads(row["snapshot_json"])["reserved_usd"] for row in db.query("SELECT id, snapshot_json FROM memecoin_cases")}
        real_replace = memecoin_desk._replace_reservation_triggers

        def explode(conn):
            real_replace(conn)
            raise RuntimeError("slot repair failed")

        memecoin_desk._replace_reservation_triggers = explode
        try:
            with self.assertRaises(RuntimeError):
                memecoin_desk._ensure()
        finally:
            memecoin_desk._replace_reservation_triggers = real_replace
        self.assertEqual(db.query("SELECT message_sha256 FROM memecoin_reservation_active WHERE case_id = 'open-case'")[0]["message_sha256"], "shifted")
        self.assertEqual({row["id"]: json.loads(row["snapshot_json"])["reserved_usd"] for row in db.query("SELECT id, snapshot_json FROM memecoin_cases")}, holds)
        memecoin_desk._ensure()
        db.init_db()
        memecoin_desk._ensure()
        slots = {row["case_id"]: row["message_sha256"] for row in db.query("SELECT case_id, message_sha256 FROM memecoin_reservation_active")}
        pins = {row["case_id"]: row["message_sha256"] for row in db.query("SELECT case_id, message_sha256 FROM memecoin_reservation_active_pin")}
        self.assertEqual(slots["open-case"], open_hash)
        self.assertEqual(pins["open-case"], open_hash)
        self.assertNotIn("consumed-case", slots)
        self.assertNotIn("stray-case", slots)
        self.assertEqual(slots["unknown-case"], unknown_hash)
        self.assertEqual(slots["dual-case"], "ambiguous")
        self.assertEqual(pins, slots)
        self.assertEqual(db.query("SELECT cost_basis_lamports FROM memecoin_entry_receipts WHERE signature = 'consumed-fill'")[0]["cost_basis_lamports"], 11)
        self.assertEqual({row["id"]: json.loads(row["snapshot_json"])["reserved_usd"] for row in db.query("SELECT id, snapshot_json FROM memecoin_cases")}, holds)
        self.assertEqual(len(db.query("SELECT message_sha256 FROM memecoin_reservations WHERE case_id = 'dual-case'")), 2)
        self.assertEqual(db.exclusive(lambda conn: conn.execute("PRAGMA user_version").fetchone()[0]), 12)

        def reject(sql, params=()):
            raw = sqlite3.connect(path)
            try:
                self.assertEqual(raw.execute("PRAGMA recursive_triggers").fetchone()[0], 0)
                with self.assertRaises(sqlite3.IntegrityError):
                    raw.execute(sql, params)
                raw.rollback()
            finally:
                raw.close()
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(sql, params)

        reject("INSERT OR REPLACE INTO memecoin_reservation_active (case_id, side, message_sha256) VALUES ('open-case', 'entry', 'shifted')")
        reject("INSERT INTO memecoin_reservation_active (case_id, side, message_sha256) VALUES ('open-case', 'entry', 'shifted') ON CONFLICT(case_id, side) DO UPDATE SET message_sha256 = 'shifted'")
        reject("INSERT OR REPLACE INTO memecoin_reservation_active_pin (case_id, side, message_sha256) VALUES ('open-case', 'entry', 'shifted')")
        self.assertEqual(db.query("SELECT message_sha256 FROM memecoin_reservation_active WHERE case_id = 'open-case'")[0]["message_sha256"], open_hash)
        errors = []

        def boot():
            try:
                memecoin_desk._ensure()
            except Exception as exc:
                errors.append(repr(exc))

        threads = [__import__("threading").Thread(target=boot) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        memecoin_desk._ensure()
        self.assertEqual(db.query("SELECT message_sha256 FROM memecoin_reservation_active WHERE case_id = 'open-case'")[0]["message_sha256"], open_hash)
        self.assertEqual(db.query("SELECT message_sha256 FROM memecoin_reservation_active WHERE case_id = 'dual-case'")[0]["message_sha256"], "ambiguous")
        db.execute(
            "INSERT INTO memecoin_entry_receipts (signature, case_id, message_sha256, slot, qty, cost_basis_lamports, network_fee_lamports, dex_fee_lamports, fee_usd, failed, ledger, ledger_provenance) VALUES ('paper-unknown', 'unknown-case', ?, 1, 1, 1, 1, 1, '0.01', 0, 'paper', 'server')",
            (unknown_hash,),
        )
        memecoin_desk._ensure()
        self.assertEqual(db.query("SELECT message_sha256 FROM memecoin_reservation_active WHERE case_id = 'unknown-case'")[0]["message_sha256"], unknown_hash)
        calls = []

        def counting(request):
            calls.append(request.get("method"))
            raise AssertionError("rpc")

        consumed_gate = desk_loop.execution_gate("consumed-case", consumed_hash, counting, 1)
        self.assertEqual(consumed_gate["reason"], "reservation_consumed")
        self.assertFalse(consumed_gate["execution_eligible"])
        self.assertEqual(calls, [])
        for digest in (first_hash, second_hash):
            ambiguous = desk_loop.execution_gate("dual-case", digest, counting, 1)
            self.assertEqual(ambiguous["reason"], "reservation_ambiguous")
            self.assertFalse(ambiguous["execution_eligible"])
        self.assertEqual(calls, [])
        db.execute(
            "INSERT INTO memecoin_entry_receipts (signature, case_id, message_sha256, slot, qty, cost_basis_lamports, network_fee_lamports, dex_fee_lamports, fee_usd, failed, ledger, ledger_provenance) VALUES ('open-fill', 'open-case', ?, 1, 400, 11, 1, 1, '0.01', 0, 'live', 'server')",
            (open_hash,),
        )
        self.assertEqual(db.query("SELECT case_id FROM memecoin_reservation_active WHERE case_id = 'open-case'"), [])
        db.execute(
            "INSERT INTO memecoin_reservations (message_sha256, case_id, side, message_b64, router, blockhash, last_valid_block_height, last_valid_provenance, ledger, ledger_provenance, created_at) VALUES (?, 'open-case', 'entry', ?, 'metis', ?, 1000000, 'jupiter_order', 'live', 'server', 2)",
            (fresh_hash, fresh_tx, blockhash),
        )
        self.assertEqual(db.query("SELECT message_sha256 FROM memecoin_reservation_active WHERE case_id = 'open-case'")[0]["message_sha256"], fresh_hash)
        follow = desk_loop.execution_gate("open-case", open_hash, counting, 1)
        self.assertEqual(follow["reason"], "reservation_consumed")
        self.assertFalse(follow["execution_eligible"])
        self.assertEqual(calls, [])
        self.assertEqual(db.query("SELECT cost_basis_lamports FROM memecoin_entry_receipts WHERE signature = 'consumed-fill'")[0]["cost_basis_lamports"], 11)
        self.assertEqual({row["id"]: json.loads(row["snapshot_json"])["reserved_usd"] for row in db.query("SELECT id, snapshot_json FROM memecoin_cases")}, holds)
