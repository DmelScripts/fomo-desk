"""The 2026-11-10 read-only fixtures. All five tokens fail. Nothing here calls the network."""
import json
import os
import unittest
from pathlib import Path

import cross_source
import dexscreener_public
import memecoin_desk
import public_filter_docs
import pyth_sponsored
from tests.support import temp_desk_env

ROOT = Path(__file__).resolve().parent / "fixtures" / "live_readonly_2026_11_10"
NOW = 1_800_000_000.0
TOKENS = {
    "BIG": "34DXEU2ttEtBSb1TqGJm7izgvYTTkqKcCoiNWdPDpump",
    "X": "9VZerdNL1x6EjetDqRnh5bYcdmk1y8sLAGs1ybzGpump",
    "TRUMP": "3yjyHCC4EzLb7J7cx3uczgTrqhpXDCBkfShK8Rf3pump",
    "MYWAY": "tx41eAXzN6jHH22LSgF7eSxpTNvJXwcbrcGsbDrpump",
    "Nothing": "6GMjV5PHfeNMaXykCktvbdmXAx54TdKfRxSRaD5Spump",
}
EXPECTED = {
    "BIG": {
        "overall": "FAIL",
        "quote_sol": "FAIL",
        "mint_freeze_revoked": "PASS",
        "lp_100": "PASS",
        "liq_ge_25pct": "FAIL-CLOSED",
        "dev_le_3pct": "FAIL-CLOSED",
        "no_past_rug": "FAIL-CLOSED",
        "holder_growth": "PASS",
    },
    "X": {
        "overall": "FAIL",
        "quote_sol": "PASS",
        "mint_freeze_revoked": "PASS",
        "lp_100": "PASS",
        "liq_ge_25pct": "FAIL",
        "dev_le_3pct": "FAIL-CLOSED",
        "no_past_rug": "FAIL-CLOSED",
        "holder_growth": "FAIL-CLOSED",
    },
    "TRUMP": {
        "overall": "FAIL",
        "quote_sol": "PASS",
        "mint_freeze_revoked": "PASS",
        "lp_100": "PASS",
        "liq_ge_25pct": "FAIL-CLOSED",
        "dev_le_3pct": "PASS",
        "no_past_rug": "FAIL",
        "holder_growth": "FAIL-CLOSED",
    },
    "MYWAY": {
        "overall": "FAIL",
        "quote_sol": "PASS",
        "mint_freeze_revoked": "PASS",
        "lp_100": "PASS",
        "liq_ge_25pct": "FAIL",
        "dev_le_3pct": "PASS",
        "no_past_rug": "FAIL-CLOSED",
        "holder_growth": "PASS",
    },
    "Nothing": {
        "overall": "FAIL-CLOSED",
        "quote_sol": "PASS",
        "mint_freeze_revoked": "PASS",
        "lp_100": "PASS",
        "liq_ge_25pct": "FAIL-CLOSED",
        "dev_le_3pct": "FAIL-CLOSED",
        "no_past_rug": "FAIL-CLOSED",
        "holder_growth": "FAIL-CLOSED",
    },
}


def _load(symbol: str, kind: str):
    return json.loads((ROOT / "raw" / ("tok_%s_%s.json" % (symbol, kind))).read_text(encoding="utf-8"))


def _bundle(symbol: str) -> dict:
    return {
        "mint": TOKENS[symbol],
        "dex": _load(symbol, "dex_pair"),
        "rugcheck": _load(symbol, "rugcheck"),
        "tracker": _load(symbol, "st_token"),
        "chart": _load(symbol, "st_holders_chart"),
        "das": _load(symbol, "das_byCreator"),
        "deployer": _load(symbol, "st_deployer"),
    }


class LiveReadonlyTests(unittest.TestCase):
    def test_each_saved_token_matches_the_report(self):
        self.assertTrue((ROOT / "REPORT.md").is_file())
        for symbol, expected in EXPECTED.items():
            verdict = cross_source.judge(_bundle(symbol))
            self.assertFalse(verdict["ok"], symbol)
            self.assertEqual(verdict["overall"], expected["overall"], symbol)
            for name, status in expected.items():
                if name == "overall":
                    continue
                self.assertEqual(verdict["checks"][name]["status"], status, "%s %s %s" % (symbol, name, verdict["checks"][name]))
            growth = verdict["checks"]["holder_growth"]
            if growth["status"] == "PASS":
                self.assertTrue(growth["holder_count_disagreement"], symbol)

    def test_mutated_fixtures_still_fail_closed(self):
        big = _bundle("BIG")
        listed = cross_source.unwrap(big["dex"])["pairs"]
        self.assertEqual(dexscreener_public.parse_pairs(listed, TOKENS["BIG"], NOW)["reason"], "quote_not_sol")
        chart = cross_source.unwrap(big["chart"])
        chart["holders"] = [row for row in chart["holders"] if row.get("holders")][:1]
        self.assertLess(public_filter_docs.parse_holder_chart(chart, TOKENS["BIG"]).get("nonzero_points", 0), 2)
        self.assertFalse(public_filter_docs.parse_holder_chart(chart, TOKENS["BIG"])["ok"])

        trump = _bundle("TRUMP")
        body = cross_source.unwrap(trump["rugcheck"])
        body["risks"] = [item for item in body["risks"] if item.get("name") != cross_source.CREATOR_RUG_RISK]
        rug = cross_source.rug_check(trump["rugcheck"], trump["tracker"], trump["das"], trump["deployer"], TOKENS["TRUMP"])
        self.assertEqual(rug["status"], "FAIL-CLOSED")
        self.assertEqual(rug["reason"], "das_capped")

        nothing = _bundle("Nothing")
        tracker = cross_source.unwrap(nothing["tracker"])
        tracker["token"]["creation"]["creator"] = cross_source.unwrap(nothing["rugcheck"])["creator"]
        verdict = cross_source.judge(nothing)
        self.assertEqual(verdict["checks"]["dev_le_3pct"]["status"], "PASS")
        self.assertEqual(verdict["checks"]["liq_ge_25pct"]["status"], "FAIL-CLOSED")
        self.assertNotEqual(verdict["overall"], "PASS")

        x = _bundle("X")
        pair = cross_source.unwrap(x["dex"])["pairs"][0]
        pair["quoteToken"] = {"address": "stock", "symbol": "SOL"}
        self.assertEqual(cross_source.judge(x)["checks"]["quote_sol"]["reason"], "quote_not_sol")
        self.assertEqual(dexscreener_public.parse_pairs([{"chainId": "solana", "baseToken": {"address": TOKENS["X"]}}], TOKENS["X"], NOW)["reason"], "quote_unknown")
        lookalike = {"chainId": "solana", "baseToken": {"address": TOKENS["X"]}, "quoteToken": {"address": "not-wsol", "symbol": "SOL"}}
        self.assertEqual(dexscreener_public.parse_pairs([lookalike], TOKENS["X"], NOW)["reason"], "quote_not_sol")
        self.assertFalse(cross_source.quote_is_sol({"address": "not-wsol", "symbol": "WSOL"}))
        decoy = _bundle("X")
        real = cross_source.unwrap(decoy["dex"])["pairs"][0]
        twin = json.loads(json.dumps(real))
        twin["pairAddress"] = real["pairAddress"].swapcase()
        cross_source.unwrap(decoy["dex"])["pairs"] = [twin, real]
        decoy["pair_id"] = real["pairAddress"]
        self.assertEqual(cross_source.judge(decoy)["checks"]["liq_ge_25pct"]["status"] != "pool_ambiguous", True)
        self.assertEqual(cross_source.select_pair({"pairs": [real]}, TOKENS["X"], twin["pairAddress"])[1], "pool_missing")
        self.assertEqual(cross_source.select_pair(decoy["dex"], TOKENS["X"], real["pairAddress"])[0]["pairAddress"], real["pairAddress"])
        other = json.loads(json.dumps(real))
        other["pairAddress"] = real["pairAddress"][:-1] + ("A" if real["pairAddress"][-1] != "A" else "B")
        cross_source.unwrap(decoy["dex"])["pairs"] = [other, real]
        self.assertEqual(cross_source.select_pair(decoy["dex"], TOKENS["X"], real["pairAddress"])[0]["pairAddress"], real["pairAddress"])
        self.assertEqual(cross_source.select_pair({"pairs": [other, real]}, TOKENS["X"], "")[1], "pool_ambiguous")
        body = cross_source.unwrap(trump["rugcheck"])
        body["risks"] = [{"name": "Rug pull history", "description": "serial", "level": "warning"}]
        again = cross_source.rug_check(trump["rugcheck"], trump["tracker"], trump["das"], trump["deployer"], TOKENS["TRUMP"])
        self.assertEqual(again["status"], "FAIL")
        body["risks"] = [{"name": "Creator concentration", "description": "dev wallet", "level": "danger"}]
        danger = cross_source.rug_check(trump["rugcheck"], trump["tracker"], trump["das"], trump["deployer"], TOKENS["TRUMP"])
        self.assertEqual(danger["reason"], "creator_rugged_history")

    def test_scout_rejects_an_unconfirmed_market_cap_and_the_filter_uses_the_cross_check(self):
        _tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        import db
        db.init_db()
        mint = TOKENS["TRUMP"]
        unconfirmed = memecoin_desk.scout({
            "mint": mint,
            "source": "dexscreener",
            "fetched_at": NOW,
            "pair_created_at": int((NOW - 3600) * 1000),
            "market_cap_usd": "1000000",
            "market_cap_basis": "marketCap",
            "confirmed_market_cap_usd": "2358",
            "liquidity_usd": "250000",
            "price_usd": "1",
            "price_change_h1": "10",
        }, now=NOW)
        self.assertEqual(unconfirmed["reason"], "market_cap_unconfirmed")
        opened = memecoin_desk.scout({
            "mint": mint + "x",
            "source": "dexscreener",
            "fetched_at": NOW,
            "pair_created_at": int((NOW - 3600) * 1000),
            "market_cap_usd": "1000000",
            "market_cap_basis": "marketCap",
            "liquidity_usd": "250000",
            "price_usd": "1",
            "price_change_h1": "10",
        }, now=NOW)
        self.assertEqual(opened["reason"], "market_cap_unconfirmed")
        opened = memecoin_desk.scout({
            "mint": mint + "x",
            "source": "dexscreener",
            "fetched_at": NOW,
            "pair_created_at": int((NOW - 3600) * 1000),
            "market_cap_usd": "1000000",
            "market_cap_basis": "marketCap",
            "confirmed_market_cap_usd": "1000000",
            "liquidity_usd": "250000",
            "price_usd": "1",
            "price_change_h1": "10",
        }, now=NOW)
        self.assertTrue(opened["ok"], opened)
        bundle = _bundle("TRUMP")
        refused = memecoin_desk.filter_case(opened["case_id"], {
            "source": "solana_mint",
            "fetched_at": NOW,
            "mint_authority_revoked": True,
            "freeze_authority_revoked": True,
            "lpLockedPct": 100,
            "bundled_launch_pct": 0,
            "cross_sources": bundle,
        }, now=NOW)
        self.assertEqual(refused["reason"], "dev_rug_history", refused)
        self.assertFalse(refused.get("live_ready") is True)

    def test_hermes_requires_a_bearer_and_does_not_use_the_stale_onchain_account(self):
        saved = json.loads((ROOT / "raw" / "pyth_0.json").read_text(encoding="utf-8"))
        onchain = json.loads((ROOT / "raw" / "pyth_onchain_4.json").read_text(encoding="utf-8"))
        os.environ.pop("PYTH_API_KEY", None)
        missing = pyth_sponsored.read_hermes_response(saved, NOW, onchain=onchain)
        self.assertEqual(missing["reason"], "price_unknown")
        self.assertEqual(missing["cause"], "pyth_api_key_missing")
        self.assertFalse(missing["onchain_substituted"])
        self.assertTrue(missing["onchain_ignored"])
        self.assertNotIn("price", missing)
        os.environ["PYTH_API_KEY"] = "test-pyth-not-a-secret"
        self.addCleanup(lambda: os.environ.pop("PYTH_API_KEY", None))
        headers = pyth_sponsored.hermes_headers()
        self.assertTrue(headers["Authorization"].startswith("Bearer "))
        self.assertNotIn("test-pyth-not-a-secret", json.dumps(missing))
        refused = pyth_sponsored.read_hermes_response(saved, NOW, onchain=onchain)
        self.assertEqual(refused["reason"], "price_unknown")
        self.assertEqual(refused["cause"], "hermes_unauthorized")
        self.assertFalse(refused["onchain_substituted"])
        self.assertNotIn("test-pyth-not-a-secret", json.dumps(refused))
        self.assertNotIn("test-pyth-not-a-secret", json.dumps({key: value for key, value in headers.items() if key != "Authorization"}))
