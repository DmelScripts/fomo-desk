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
        self.assertIn("owner_slippage_unapproved", status["ready_because"])
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
