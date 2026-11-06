"""Auditor: eight layers from the raw samples, retries, and no-data = red."""
import asyncio
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
from agents.auditor import Auditor, judge, largest_bundle
from bus import Desk
from notifier import Notifier
from tests.support import load, temp_desk_env

POOL = "GWc8HL8vCGMnmg3n9pC4tmWafYiWpuoxvKrrhzDVZyJf"
MINT = "4FHEBKNYy6BZ1QFMKj1xL8HDTM12rdXLRYw8DgiCUnQn"


def _bundle():
    created = load("dexscreener_pair.json")[0]["pairCreatedAt"]
    program = load("rpc_getProgramAccounts_token.json")["result"]
    program = program + load("rpc_getProgramAccounts_token2022.json")["result"]
    return {
        "mint": MINT,
        "pool": POOL,
        "account": load("rpc_getAccountInfo.json"),
        "supply": load("rpc_getTokenSupply.json"),
        "report": load("rugcheck_report.json"),
        "graph": load("rugcheck_insiders_graph.json"),
        "program_accounts": program,
        "pair": load("dexscreener_pair.json")[0],
        "now": created / 1000 + 15 * 60,
    }


class TestAuditor(unittest.TestCase):
    def test_sample_is_green_on_all_eight_layers(self):
        bundle = _bundle()
        result = judge(bundle, now=bundle.pop("now"))
        self.assertTrue(result["passed"], [layer["value"] for layer in result["layers"]])
        self.assertEqual(result["layersChecked"], 8)
        self.assertEqual([layer["n"] for layer in result["layers"]], list(range(1, 9)))
        self.assertTrue(all(layer["pass"] for layer in result["layers"]))
        self.assertEqual(result["holders"], 224)
        self.assertEqual(result["layers"][6]["value"], "None")
        self.assertEqual(result["layers"][7]["value"], "None")

    def test_each_layer_can_go_red(self):
        created = load("dexscreener_pair.json")[0]["pairCreatedAt"]
        now = created / 1000 + 15 * 60

        mint = _bundle()
        mint["account"] = copy.deepcopy(mint["account"])
        mint["account"]["result"]["value"]["data"]["parsed"]["info"]["mintAuthority"] = "SetMintAuth"
        self.assertEqual(judge(mint, now=now)["layers"][0]["value"], "set")

        frozen = _bundle()
        frozen["account"] = copy.deepcopy(frozen["account"])
        frozen["account"]["result"]["value"]["data"]["parsed"]["info"]["freezeAuthority"] = "SetFreeze"
        frozen_result = judge(frozen, now=now)
        self.assertEqual(frozen_result["layers"][1]["value"], "set")
        self.assertTrue(all(layer["value"] == "not checked" for layer in frozen_result["layers"][2:]))
        self.assertFalse(frozen_result["passed"])

        dev = _bundle()
        dev["report"] = copy.deepcopy(dev["report"])
        dev["report"]["creatorBalance"] = 996452587.338732 * 0.2
        self.assertIn("dev holds", judge(dev, now=now)["layers"][2]["value"])
        self.assertFalse(judge(dev, now=now)["layers"][2]["pass"])

        missing_creator = _bundle()
        missing_creator["report"] = copy.deepcopy(missing_creator["report"])
        missing_creator["report"]["creator"] = None
        self.assertEqual(judge(missing_creator, now=now)["layers"][2]["value"], "no creator")

        no_supply = _bundle()
        no_supply["supply"] = copy.deepcopy(no_supply["supply"])
        no_supply["supply"]["result"]["value"]["uiAmount"] = 0
        self.assertEqual(judge(no_supply, now=now)["layers"][2]["value"], "no supply")

        top = _bundle()
        top["report"] = copy.deepcopy(top["report"])
        top["report"]["topHolders"].append({
            "address": "not-the-pool", "owner": "someone", "pct": 80,
        })
        self.assertFalse(judge(top, now=now)["layers"][3]["pass"])

        lp = _bundle()
        lp["report"] = copy.deepcopy(lp["report"])
        lp["report"]["markets"][0]["lp"]["lpLockedPct"] = 10
        self.assertFalse(judge(lp, now=now)["layers"][4]["pass"])

        young = _bundle()
        self.assertEqual(judge(young, now=created / 1000)["layers"][5]["value"], "pool too young")

        bundled = _bundle()
        bundled["graph"] = load("rugcheck_insiders_graph.schema_sample.json")["payload"]
        self.assertGreaterEqual(largest_bundle(bundled["graph"]), 5)
        self.assertFalse(judge(bundled, now=now)["layers"][6]["pass"])
        self.assertIn("bundled wallets", judge(bundled, now=now)["layers"][6]["value"])

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

        class Http:
            def __init__(self):
                self.account_calls = 0

            async def get_json(self, url):
                if url.endswith("/report"):
                    return load("rugcheck_report.json")
                if url.endswith("/graph"):
                    return []
                raise AssertionError(url)

            async def post_json(self, url, body, headers=None):
                method = body.get("method")
                if method == "getAccountInfo":
                    self.account_calls += 1
                    return {"error": {"code": 429, "message": "Too many requests for a specific RPC call"}}
                if method == "getTokenSupply":
                    return load("rpc_getTokenSupply.json")
                if method == "getProgramAccounts":
                    program = body["params"][0]
                    if program.endswith("uEb"):
                        return load("rpc_getProgramAccounts_token2022.json")
                    return load("rpc_getProgramAccounts_token.json")
                raise AssertionError(method)

        http = Http()
        auditor = Auditor(Desk(http, Notifier(mode="PAPER")))
        created = load("dexscreener_pair.json")[0]["pairCreatedAt"]
        result = asyncio.run(auditor.audit(
            MINT, pool=POOL, now=created / 1000 + 15 * 60, pair=load("dexscreener_pair.json")[0],
        ))
        self.assertEqual(http.account_calls, 3)
        self.assertEqual(sleeps, [2, 2])
        self.assertEqual(result["layers"][0]["value"], "no data: mint authority")
        self.assertFalse(result["layers"][0]["pass"])
        self.assertFalse(result["passed"])
