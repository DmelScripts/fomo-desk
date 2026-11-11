"""Auditor: eight layers from the raw samples, retries, and no-data = red."""
import asyncio
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
from agents.auditor import Auditor, judge, largest_holder_group as largest_bundle
from bus import Desk
from notifier import Notifier
from tests.support import load, temp_desk_env

POOL = "GWc8HL8vCGMnmg3n9pC4tmWafYiWpuoxvKrrhzDVZyJf"
MINT = "4FHEBKNYy6BZ1QFMKj1xL8HDTM12rdXLRYw8DgiCUnQn"


def _bundle():
    supply = load("rpc_getTokenSupply.json")["result"]["value"]["uiAmount"]
    program = load("rpc_getProgramAccounts_token.json")["result"]
    program = program + load("rpc_getProgramAccounts_token2022.json")["result"]
    # The captured getTokenLargestAccounts body is a 429. These amounts are the
    # RugCheck top-holder shape with the pool excluded, so the sum stays under 25%.
    largest = [{"address": POOL, "owner": POOL, "uiAmount": supply * 0.21}]
    largest += [{"address": f"h{i}", "owner": f"o{i}", "uiAmount": supply * 0.02} for i in range(10)]
    migrated = 1_000_000.0
    return {
        "mint": MINT,
        "pool": POOL,
        "account": load("rpc_getAccountInfo.json"),
        "supply": load("rpc_getTokenSupply.json"),
        "supply_ui": supply,
        "report": load("rugcheck_report.json"),
        "graph": load("rugcheck_insiders_graph.json"),
        "program_accounts": program,
        "largest": {"result": {"value": largest}},
        "creator": load("rugcheck_report.json")["creator"],
        "creator_balance_at_migration": 0,
        "dev_balance_ui": 0.0,
        "migrated_at": migrated,
        "scanner_liquidity": 50000,
        "liquidity_now": 50000,
        "now": migrated + 15 * 60,
    }


class TestAuditor(unittest.TestCase):
    def setUp(self):
        _tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        db.init_db()

    def test_sample_is_green_on_all_eight_layers(self):
        bundle = _bundle()
        result = judge(bundle, now=bundle.pop("now"))
        self.assertTrue(result["passed"], [layer["value"] for layer in result["layers"]])
        self.assertEqual(result["layersChecked"], 8)
        self.assertEqual([layer["n"] for layer in result["layers"]], list(range(1, 9)))
        self.assertTrue(all(layer["pass"] for layer in result["layers"]))
        self.assertEqual(result["holders"], 224)
        self.assertEqual(result["layers"][0]["value"], "Revoked")
        self.assertEqual(result["layers"][1]["value"], "Revoked")
        self.assertEqual(result["layers"][2]["value"], "0.0% · sold 0")
        self.assertEqual(result["layers"][4]["value"], "100% locked")
        self.assertEqual(result["layers"][5]["value"], "15m")
        self.assertEqual(result["layers"][6]["value"], "None")
        self.assertEqual(result["layers"][7]["value"], "None")

    def test_each_layer_can_go_red(self):
        created = load("dexscreener_pair.json")[0]["pairCreatedAt"]
        now = created / 1000 + 15 * 60

        mint = _bundle()
        mint["account"] = copy.deepcopy(mint["account"])
        mint["account"]["result"]["value"]["data"]["parsed"]["info"]["mintAuthority"] = "SetMintAuth"
        self.assertEqual(judge(mint, now=now)["layers"][0]["value"], "mint not revoked")

        frozen = _bundle()
        frozen["account"] = copy.deepcopy(frozen["account"])
        frozen["account"]["result"]["value"]["data"]["parsed"]["info"]["freezeAuthority"] = "SetFreeze"
        frozen_result = judge(frozen, now=now)
        self.assertEqual(frozen_result["layers"][1]["value"], "freeze not revoked")
        self.assertTrue(all(layer["value"] == "not checked" for layer in frozen_result["layers"][2:]))
        self.assertFalse(frozen_result["passed"])

        dev = _bundle()
        dev["dev_balance_ui"] = dev["supply_ui"] * 0.31
        self.assertEqual(judge(dev, now=now)["layers"][2]["value"], "dev holds 31%")

        missing_creator = _bundle()
        missing_creator["creator"] = None
        missing_creator["report"] = copy.deepcopy(missing_creator["report"])
        missing_creator["report"]["creator"] = None
        self.assertEqual(judge(missing_creator, now=now)["layers"][2]["value"], "no data: dev")

        no_supply = _bundle()
        no_supply["supply_ui"] = 0
        self.assertEqual(judge(no_supply, now=now)["layers"][2]["value"], "no supply")

        top = _bundle()
        top["largest"] = {"result": {"value": [
            {"address": "whale", "owner": "whale", "uiAmount": top["supply_ui"] * 0.58},
        ]}}
        self.assertEqual(judge(top, now=now)["layers"][3]["value"], "top 10 hold 58%")

        lp = _bundle()
        lp["report"] = copy.deepcopy(lp["report"])
        lp["report"]["markets"][0]["lp"]["lpLockedPct"] = 10
        self.assertFalse(judge(lp, now=now)["layers"][4]["pass"])

        young = _bundle()
        self.assertEqual(judge(young, now=young["migrated_at"])["layers"][5]["value"], "pool too young")

        bundled = _bundle()
        bundled["graph"] = [{
            "nodes": [{"id": f"w{i}", "holdings": 1} for i in range(6)],
            "links": [{"source": f"w{i}", "target": f"w{i+1}"} for i in range(5)],
        }]
        self.assertGreaterEqual(largest_bundle(bundled["graph"]), 5)
        self.assertEqual(judge(bundled, now=now)["layers"][6]["value"], "6 bundled wallets")
        missing_edges = _bundle()
        missing_edges["graph"] = [{"nodes": [{"id": "a", "holdings": 1}]}]
        self.assertEqual(judge(missing_edges, now=now)["layers"][6]["value"], "no data: bundles")

        delegated = _bundle()
        accounts = []
        delegate = "9fQxDELGATEACCOUNTXXXXXXXXXXXXT2a"
        for index in range(11):
            accounts.append({
                "pubkey": f"acct{index}",
                "account": {"data": {"parsed": {"info": {
                    "tokenAmount": {"uiAmount": 1},
                    "delegate": delegate,
                }}}},
            })
        delegated["program_accounts"] = accounts
        import fields
        fields.RPC_TOKEN_ACCOUNT["delegate"] = "[].account.data.parsed.info.delegate"
        try:
            layer = judge(delegated, now=now)["layers"][7]
        finally:
            fields.RPC_TOKEN_ACCOUNT["delegate"] = None
        self.assertFalse(layer["pass"])
        self.assertIn("wallets →", layer["value"])

    def test_retries_then_no_data_is_red(self):
        tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        db.init_db()
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        import agents.auditor as auditor_mod
        original = auditor_mod.sleep
        auditor_mod.sleep = fake_sleep
        self.addCleanup(lambda: setattr(auditor_mod, "sleep", original))

        import solana_rpc

        class Http:
            def __init__(self):
                self.account_calls = 0
                self._prior_rpc = solana_rpc.TRANSPORT
                solana_rpc.TRANSPORT = self._rpc

            def close(self):
                solana_rpc.TRANSPORT = self._prior_rpc

            def _rpc(self, payload):
                method = payload.get("method")
                if method == "getAccountInfo":
                    self.account_calls += 1
                    return {"status": 200, "body": {"error": {"code": 429, "message": "Too many requests for a specific RPC call"}}}
                if method == "getTokenSupply":
                    return {"status": 200, "body": load("rpc_getTokenSupply.json")}
                if method in ("getTokenLargestAccounts", "getTokenAccountsByOwner"):
                    return {"status": 200, "body": {"result": {"value": []}}}
                if method == "getProgramAccounts":
                    program = payload["params"][0]
                    if program.endswith("uEb"):
                        return {"status": 200, "body": load("rpc_getProgramAccounts_token2022.json")}
                    return {"status": 200, "body": load("rpc_getProgramAccounts_token.json")}
                raise AssertionError(method)

            async def get_json(self, url):
                if url.endswith("/report"):
                    return load("rugcheck_report.json")
                if url.endswith("/graph"):
                    return []
                raise AssertionError(url)

            async def post_json(self, url, body, headers=None, timeout=None):
                method = body.get("method")
                if method == "getAccountInfo":
                    self.account_calls += 1
                    return {"error": {"code": 429, "message": "Too many requests for a specific RPC call"}}
                if method == "getTokenSupply":
                    return load("rpc_getTokenSupply.json")
                if method in ("getTokenLargestAccounts", "getTokenAccountsByOwner"):
                    return {"result": {"value": []}}
                if method == "getProgramAccounts":
                    program = body["params"][0]
                    if program.endswith("uEb"):
                        return load("rpc_getProgramAccounts_token2022.json")
                    return load("rpc_getProgramAccounts_token.json")
                raise AssertionError(method)

        http = Http()
        self.addCleanup(http.close)
        auditor = Auditor(Desk(http, Notifier(mode="PAPER")))
        created = load("dexscreener_pair.json")[0]["pairCreatedAt"]
        result = asyncio.run(auditor.audit(
            MINT,
            payload={"pool": POOL, "migrated_at": created / 1000, "pair": load("dexscreener_pair.json")[0]},
            now=created / 1000 + 15 * 60,
        ))
        self.assertEqual(http.account_calls, 3)
        self.assertEqual(sleeps, [2, 2])
        self.assertEqual(result["layers"][0]["value"], "no data: mint authority")
        self.assertFalse(result["layers"][0]["pass"])
        self.assertFalse(result["passed"])

    def test_auditor_rpc_uses_the_shared_resolver(self):
        import os
        import solana_rpc
        from agents.auditor import Auditor
        tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        db.init_db()
        os.environ.pop("HELIUS_API_KEY", None)
        auditor = Auditor(Desk(object(), Notifier(mode="PAPER")))
        with self.assertRaises(solana_rpc.RpcRefused) as missing:
            asyncio.run(auditor._rpc("getSlot", []))
        self.assertEqual(missing.exception.reason, "helius_api_key_missing")
        self.assertNotIn("api-key", str(missing.exception))
        db.set_setting("rpc_url", "https://override.example/solana", "test")
        seen = {}

        def transport(payload):
            seen["method"] = payload.get("method")
            return {"status": 200, "body": {"result": "ok"}}

        solana_rpc.TRANSPORT = transport
        self.addCleanup(lambda: setattr(solana_rpc, "TRANSPORT", None))
        os.environ["HELIUS_API_KEY"] = "helius-auditor-secret"
        self.addCleanup(lambda: os.environ.pop("HELIUS_API_KEY", None))
        result = asyncio.run(auditor._rpc("getHealth", []))
        self.assertEqual(result, {"result": "ok"})
        self.assertEqual(seen["method"], "getHealth")
        self.assertEqual(solana_rpc.describe()["host"], "override.example")
        self.assertEqual(solana_rpc.describe()["provider"], "configured_rpc")
        self.assertNotIn("helius-auditor-secret", str(result))
