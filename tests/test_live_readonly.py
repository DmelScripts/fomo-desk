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
        "bundle_le_10": "PASS",
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
        "bundle_le_10": "FAIL",
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
        "bundle_le_10": "PASS",
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
        "bundle_le_10": "PASS",
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
        "bundle_le_10": "PASS",
    },
}
BUNDLE_SHARE = {
    "BIG": "0.2864",
    "X": "11.4546",
    "TRUMP": "3.8674",
    "MYWAY": "0",
    "Nothing": "2.8552",
}


def _load(symbol: str, kind: str):
    return json.loads((ROOT / "raw" / ("tok_%s_%s.json" % (symbol, kind))).read_text(encoding="utf-8"))


def _two_pools(mint: str) -> dict:
    """SMALL would pass 30/100 on its own. BIG is the other native-wSOL pool."""
    sol = cross_source.SOL_MINT
    small_id = "small-pool-" + mint[:8]
    big_id = "big-pool-" + mint[:8]

    def pool(pid, quote, cap):
        return {
            "pairAddress": pid,
            "baseToken": {"address": mint},
            "quoteToken": {"address": sol, "symbol": "SOL"},
            "marketCap": cap,
            "liquidity": {"usd": "1"},
        }

    return {
        "mint": mint,
        "dex": {"pairs": [pool(small_id, "30", "100"), pool(big_id, "300000", "1000000")]},
        "rugcheck": {
            "creator": "SameCreator", "creatorBalance": 0, "token": {"supply": 1000},
            "mintAuthority": None, "freezeAuthority": None, "risks": [],
            "creatorTokens": [{"mint": mint}],
            "markets": [
                {"pubkey": small_id, "lp": {"quoteUSD": "30", "lpLockedPct": 100}},
                {"pubkey": big_id, "lp": {"quoteUSD": "300000", "lpLockedPct": 100}},
            ],
        },
        "tracker": {
            "holders": 10,
            "token": {"creation": {"creator": "SameCreator"}},
            "risk": {"dev": {"percentage": 0}, "risks": []},
            "pools": [
                {"poolId": small_id, "lpBurn": 100, "security": {"mintAuthority": None, "freezeAuthority": None},
                 "liquidity": {"quote": "30", "usd": 1}, "marketCap": {"quote": "100", "usd": "100"}},
                {"poolId": big_id, "lpBurn": 100, "security": {"mintAuthority": None, "freezeAuthority": None},
                 "liquidity": {"quote": "300000", "usd": 1}, "marketCap": {"quote": "1000000", "usd": "1000000"}},
            ],
        },
        "chart": {"holders": [{"holders": 2, "time": 1}, {"holders": 4, "time": 2}]},
        "das": {"result": {"total": 1, "items": [{"id": mint}]}},
        "deployer": {"total": 1, "data": [{"mint": mint}]},
    }


def _passing_decoy(mint: str) -> dict:
    """A caller body that would pass if the filter trusted it."""
    from tests.test_memecoin_desk import _passing_cross
    return _passing_cross(mint)


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
        files = {
            "dex": "dex_pair",
            "rugcheck": "rugcheck",
            "solana_tracker_token": "st_token",
            "holders_chart": "st_holders_chart",
            "das": "das_byCreator",
            "deployer": "st_deployer",
        }

        def fetch(kind, mint, pair_id=None):
            symbol = next(name for name, value in TOKENS.items() if value == mint)
            return _load(symbol, files[kind])

        cross_source.set_fetcher(fetch)
        self.addCleanup(cross_source.clear_fetcher)
        for symbol, expected in EXPECTED.items():
            fetched = cross_source.fetch_bundle(TOKENS[symbol], "")
            self.assertIsNotNone(fetched, symbol)
            verdict = cross_source.judge(fetched)
            direct = cross_source.judge(_bundle(symbol))
            self.assertEqual(verdict["overall"], direct["overall"], symbol)
            self.assertFalse(verdict["ok"], symbol)
            self.assertEqual(verdict["overall"], expected["overall"], symbol)
            for name, status in expected.items():
                if name == "overall":
                    continue
                self.assertEqual(verdict["checks"][name]["status"], status, "%s %s %s" % (symbol, name, verdict["checks"][name]))
            growth = verdict["checks"]["holder_growth"]
            if growth["status"] == "PASS":
                self.assertTrue(growth["holder_count_disagreement"], symbol)
            measured = verdict["checks"]["bundle_le_10"]
            self.assertEqual(measured["bundled_launch_pct"], BUNDLE_SHARE[symbol], symbol)
            self.assertEqual(measured["source"], "solana_tracker_initial_bundler_share", symbol)

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
        self.assertEqual(cross_source.judge(decoy)["checks"]["liq_ge_25pct"]["reason"], "pool_ambiguous")
        self.assertEqual(cross_source.select_pair({"pairs": [real]}, TOKENS["X"], twin["pairAddress"])[1], "pool_missing")
        self.assertEqual(cross_source.select_pair(decoy["dex"], TOKENS["X"], real["pairAddress"])[1], "pool_ambiguous")
        other = json.loads(json.dumps(real))
        other["pairAddress"] = real["pairAddress"][:-1] + ("A" if real["pairAddress"][-1] != "A" else "B")
        cross_source.unwrap(decoy["dex"])["pairs"] = [other, real]
        self.assertEqual(cross_source.select_pair(decoy["dex"], TOKENS["X"], real["pairAddress"])[1], "pool_ambiguous")
        self.assertEqual(cross_source.select_pair({"pairs": [other, real]}, TOKENS["X"], "")[1], "pool_ambiguous")
        body = cross_source.unwrap(trump["rugcheck"])
        body["risks"] = [{"name": "Rug pull history", "description": "serial", "level": "warning"}]
        again = cross_source.rug_check(trump["rugcheck"], trump["tracker"], trump["das"], trump["deployer"], TOKENS["TRUMP"])
        self.assertEqual(again["status"], "FAIL")
        body["risks"] = [{"name": "Creator concentration", "description": "dev wallet", "level": "danger"}]
        danger = cross_source.rug_check(trump["rugcheck"], trump["tracker"], trump["das"], trump["deployer"], TOKENS["TRUMP"])
        self.assertEqual(danger["reason"], "creator_rugged_history")
        body["risks"] = [{"name": "Deployer wallet", "description": "controls the mint", "level": "danger"}]
        self.assertTrue(cross_source.creator_risk(body["risks"][0]))

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
            "pair_address": cross_source.pair_id_for(mint),
            "liquidity_usd": "250000",
            "price_usd": "1",
            "price_change_h1": "10",
        }, now=NOW)
        self.assertEqual(unconfirmed["reason"], "pool_missing")
        supplied = dict(unconfirmed) if False else {
            "mint": mint,
            "source": "dexscreener",
            "fetched_at": NOW,
            "pair_created_at": int((NOW - 3600) * 1000),
            "market_cap_usd": "1000000",
            "market_cap_basis": "marketCap",
            "confirmed_market_cap_usd": "1000000",
            "pair_address": cross_source.pair_id_for(mint),
            "liquidity_usd": "250000",
            "price_usd": "1",
            "price_change_h1": "10",
        }
        cross_source.clear_fetcher()
        self.assertEqual(memecoin_desk.scout(supplied, now=NOW)["reason"], "pool_missing")
        opened = memecoin_desk.scout({
            "mint": mint + "x",
            "source": "dexscreener",
            "fetched_at": NOW,
            "pair_created_at": int((NOW - 3600) * 1000),
            "market_cap_usd": "1000000",
            "market_cap_basis": "marketCap",
            "pair_address": cross_source.pair_id_for(mint + "x"),
            "liquidity_usd": "250000",
            "price_usd": "1",
            "price_change_h1": "10",
        }, now=NOW)
        self.assertEqual(opened["reason"], "pool_missing")
        case_mint = mint + "x"
        cross_source.note_cap(case_mint, "1000000")
        cross_source.arm_test_fetcher()
        self.addCleanup(cross_source.clear_fetcher)
        opened = memecoin_desk.scout({
            "mint": case_mint,
            "source": "dexscreener",
            "fetched_at": NOW,
            "pair_created_at": int((NOW - 3600) * 1000),
            "market_cap_usd": "1000000",
            "market_cap_basis": "marketCap",
            "confirmed_market_cap_usd": "1000000",
            "pair_address": "SMALL",
            "pairs": [{"pairAddress": "SMALL"}],
            "liquidity_usd": "250000",
            "price_usd": "1",
            "price_change_h1": "10",
        }, now=NOW)
        self.assertTrue(opened["ok"], opened)
        self.assertEqual(memecoin_desk.view(opened["case_id"])["snapshot"]["pair_address"], cross_source.pair_id_for(case_mint))
        self.assertNotEqual(memecoin_desk.view(opened["case_id"])["snapshot"]["pair_address"], "SMALL")
        pools = _two_pools(case_mint)
        cross_source.note_bundle(case_mint, pools)
        refused = memecoin_desk.filter_case(opened["case_id"], {
            "source": "solana_mint",
            "fetched_at": NOW,
            "mint_authority_revoked": True,
            "freeze_authority_revoked": True,
            "lpLockedPct": 100,
            "bundled_launch_pct": 0,
            "dev_holding_pct": "0",
            "cross_sources": _passing_decoy(case_mint),
        }, now=NOW)
        self.assertEqual(refused["reason"], "pool_ambiguous", refused)
        judged = cross_source.judge(pools)
        self.assertFalse(judged["ok"])
        self.assertEqual(judged["checks"]["liq_ge_25pct"]["reason"], "pool_ambiguous")
        mismatch = _passing_decoy(case_mint)
        mismatch["pair_id"] = "not-the-stored-pool"
        mismatch["dex"]["pairs"][0]["pairAddress"] = "not-the-stored-pool"
        mismatch["rugcheck"]["markets"][0]["pubkey"] = "not-the-stored-pool"
        mismatch["tracker"]["pools"][0]["poolId"] = "not-the-stored-pool"
        cross_source.note_bundle(case_mint, mismatch)
        import db
        db.execute("UPDATE memecoin_cases SET stage = 'scout_passed' WHERE id = ?", (opened["case_id"],))
        missed = memecoin_desk.filter_case(opened["case_id"], {
            "source": "solana_mint",
            "fetched_at": NOW,
            "mint_authority_revoked": True,
            "freeze_authority_revoked": True,
            "lpLockedPct": 100,
            "bundled_launch_pct": 0,
            "dev_holding_pct": "0",
            "cross_sources": _passing_decoy(case_mint),
        }, now=NOW)
        self.assertEqual(missed["reason"], "pool_missing", missed)

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
        self.assertEqual(headers["User-Agent"], "fomo-desk/1.0")
        self.assertEqual(headers["Accept"], "application/json")
        request = pyth_sponsored.hermes_request()
        self.assertEqual(request.get_method(), "GET")
        self.assertIn("hermes.pyth.network", request.full_url)
        self.assertEqual(request.get_header("User-agent"), "fomo-desk/1.0")
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertTrue(request.get_header("Authorization").startswith("Bearer "))
        self.assertNotIn("test-pyth-not-a-secret", request.get_header("User-agent"))
        self.assertNotIn("test-pyth-not-a-secret", json.dumps(missing))
        refused = pyth_sponsored.read_hermes_response(saved, NOW, onchain=onchain)
        self.assertEqual(refused["reason"], "price_unknown")
        self.assertEqual(refused["cause"], "hermes_unauthorized")
        blocked = pyth_sponsored.read_hermes_response({"status": 403, "body": "error code: 1010"}, NOW, onchain=onchain)
        self.assertEqual(blocked["reason"], "price_unknown")
        self.assertEqual(blocked["cause"], "hermes_unauthorized")
        self.assertFalse(blocked["onchain_substituted"])
        self.assertTrue(blocked["onchain_ignored"])
        self.assertNotIn("price", blocked)
        fresh = json.loads((Path(__file__).resolve().parent / "fixtures" / "hermes_sol_usd_200.json").read_text(encoding="utf-8"))
        fresh["parsed"][0]["price"]["publish_time"] = int(NOW)
        priced = pyth_sponsored.assess_signer_price(fresh, NOW)
        self.assertTrue(priced["ok"], priced)
        from decimal import Decimal
        self.assertEqual(Decimal(priced["price"]) * (Decimal(10) ** priced["expo"]), Decimal("83.0165"))

        class LieFloat(float):
            def __str__(self):
                return str(int(NOW))

        class LieStr(str):
            def __str__(self):
                return "100"

        lied_time = pyth_sponsored.assess_signer_price({
            "parsed": [{"status": "trading", "price": {"price": "100", "conf": "1", "expo": "-8", "publish_time": LieFloat(1.0)}}],
        }, NOW)
        self.assertEqual(lied_time["reason"], "price_unknown")
        lied_price = pyth_sponsored.assess_signer_price({
            "parsed": [{"status": "trading", "price": {"price": LieStr("nope"), "conf": "1", "expo": "-8", "publish_time": str(int(NOW))}}],
        }, NOW)
        self.assertEqual(lied_price["reason"], "price_unknown")
        self.assertFalse(refused["onchain_substituted"])
        self.assertNotIn("test-pyth-not-a-secret", json.dumps(refused))
        self.assertNotIn("test-pyth-not-a-secret", json.dumps({key: value for key, value in headers.items() if key != "Authorization"}))

    def test_hermes_v2_without_status_is_a_price_when_the_print_is_fresh(self):
        from decimal import Decimal
        fresh = json.loads((Path(__file__).resolve().parent / "fixtures" / "hermes_sol_usd_200.json").read_text(encoding="utf-8"))
        item = fresh["parsed"][0]
        self.assertIn("status", item)
        del item["status"]
        self.assertNotIn("status", item)
        item["price"]["price"] = "8305775049"
        item["price"]["conf"] = "7800000"
        item["price"]["expo"] = -8
        item["price"]["publish_time"] = int(NOW)
        os.environ["PYTH_API_KEY"] = "test-pyth-not-a-secret"
        self.addCleanup(lambda: os.environ.pop("PYTH_API_KEY", None))
        priced = pyth_sponsored.read_hermes_response({"status": 200, "body": fresh}, NOW)
        self.assertTrue(priced["ok"], priced)
        self.assertIsNone(priced["reason"])
        self.assertEqual(Decimal(priced["price"]) * (Decimal(10) ** priced["expo"]), Decimal("83.05775049"))
        self.assertFalse(priced["onchain_substituted"])

        stale = json.loads(json.dumps(fresh))
        stale["parsed"][0]["price"]["publish_time"] = int(NOW) - 31
        self.assertEqual(pyth_sponsored.assess_signer_price(stale, NOW)["reason"], "price_stale")

        wide = json.loads(json.dumps(fresh))
        wide["parsed"][0]["price"]["conf"] = "83057751"
        self.assertEqual(pyth_sponsored.assess_signer_price(wide, NOW)["reason"], "price_conf")

        halted = json.loads(json.dumps(fresh))
        halted["parsed"][0]["status"] = "halted"
        self.assertEqual(pyth_sponsored.assess_signer_price(halted, NOW)["reason"], "price_unknown")
        blank = json.loads(json.dumps(fresh))
        blank["parsed"][0]["status"] = None
        self.assertEqual(pyth_sponsored.assess_signer_price(blank, NOW)["reason"], "price_unknown")

        for field in ("price", "conf", "publish_time"):
            missing = json.loads(json.dumps(fresh))
            del missing["parsed"][0]["price"][field]
            self.assertEqual(pyth_sponsored.assess_signer_price(missing, NOW)["reason"], "price_unknown", field)
        invalid = json.loads(json.dumps(fresh))
        invalid["parsed"][0]["price"]["price"] = "nope"
        self.assertEqual(pyth_sponsored.assess_signer_price(invalid, NOW)["reason"], "price_unknown")

    def test_hermes_parsed_id_must_equal_the_sol_feed(self):
        saved = json.loads((Path(__file__).resolve().parent / "fixtures" / "hermes_sol_usd_200.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["parsed"][0]["id"], pyth_sponsored.SOL_FEED)
        self.assertFalse(saved["parsed"][0]["id"].startswith("0x"))
        saved["parsed"][0]["price"]["publish_time"] = int(NOW)
        priced = pyth_sponsored.assess_signer_price(saved, NOW)
        self.assertTrue(priced["ok"], priced)
        self.assertNotIn("cause", priced)

        def refused(body):
            result = pyth_sponsored.assess_signer_price(body, NOW)
            self.assertEqual(result["reason"], "price_unknown")
            self.assertEqual(result["cause"], "hermes_feed_mismatch")
            self.assertIsNot(result.get("ok"), True)
            return result

        wrong = json.loads(json.dumps(saved))
        wrong["parsed"][0]["id"] = "a" * 64
        refused(wrong)
        missing = json.loads(json.dumps(saved))
        del missing["parsed"][0]["id"]
        refused(missing)
        numeric = json.loads(json.dumps(saved))
        numeric["parsed"][0]["id"] = 1
        refused(numeric)
        os.environ["PYTH_API_KEY"] = "test-pyth-not-a-secret"
        self.addCleanup(lambda: os.environ.pop("PYTH_API_KEY", None))
        wrapped = pyth_sponsored.read_hermes_response({"status": 200, "body": wrong}, NOW)
        self.assertEqual(wrapped["reason"], "price_unknown")
        self.assertEqual(wrapped["cause"], "hermes_feed_mismatch")
