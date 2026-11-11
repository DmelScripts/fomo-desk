"""The production reader uses stubbed HTTP. It does not open a socket."""
import json
import os
import unittest
from pathlib import Path

import cross_source
import db
import desk_loop
import desk_providers
from tests.support import temp_desk_env
from tests.test_live_readonly import EXPECTED, TOKENS, _bundle

ROOT = Path(__file__).resolve().parent / "fixtures" / "live_readonly_2026_11_10" / "raw"
NOW = 1_800_000_000.0


def _saved(symbol: str, kind: str):
    return json.loads((ROOT / ("tok_%s_%s.json" % (symbol, kind))).read_text(encoding="utf-8"))["body"]


class DeskProviderTests(unittest.TestCase):
    def setUp(self):
        _tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        db.init_db()
        os.environ["SOLANA_TRACKER_API_KEY"] = "test-tracker-not-a-secret"
        os.environ["HELIUS_API_KEY"] = "test-helius-not-a-secret"
        os.environ["PYTH_API_KEY"] = "test-pyth-not-a-secret"
        self.addCleanup(lambda: os.environ.pop("SOLANA_TRACKER_API_KEY", None))
        self.addCleanup(lambda: os.environ.pop("HELIUS_API_KEY", None))
        self.addCleanup(lambda: os.environ.pop("PYTH_API_KEY", None))
        desk_providers.set_gap(0)
        desk_providers.set_transport(None)
        self.addCleanup(lambda: desk_providers.set_transport(None))
        self.addCleanup(lambda: desk_providers.set_gap(None))
        self.addCleanup(cross_source.clear_fetcher)

    def _creators(self):
        found = {}
        for symbol, mint in TOKENS.items():
            creator = _saved(symbol, "st_token")["token"]["creation"]["creator"]
            found[creator] = symbol
            found[mint] = symbol
        return found

    def test_saved_bodies_keep_the_five_verdicts(self):
        creators = self._creators()
        seen = []

        def transport(spec):
            seen.append(spec)
            url = spec["url"]
            self.assertEqual(spec["headers"]["User-Agent"], "fomo-desk/1.0")
            self.assertNotIn("test-tracker-not-a-secret", url)
            self.assertNotIn("test-helius-not-a-secret", url)
            self.assertNotIn("test-pyth-not-a-secret", url)
            if "token-pairs" in url:
                symbol = creators[url.rsplit("/", 1)[-1]]
                return {"status": 200, "body": _saved(symbol, "dex_pair")}
            if "rugcheck.xyz" in url:
                symbol = creators[url.split("/tokens/")[1].split("/")[0]]
                return {"status": 200, "body": _saved(symbol, "rugcheck")}
            if "/holders/chart/" in url:
                symbol = creators[url.rsplit("/", 1)[-1]]
                return {"status": 200, "body": _saved(symbol, "st_holders_chart")}
            if "/deployer/" in url:
                symbol = creators[url.rsplit("/", 1)[-1]]
                return {"status": 200, "body": _saved(symbol, "st_deployer")}
            if "/tokens/" in url:
                symbol = creators[url.rsplit("/", 1)[-1]]
                return {"status": 200, "body": _saved(symbol, "st_token")}
            self.assertEqual(url, "https://mainnet.helius-rpc.com/")
            self.assertEqual(spec["headers"]["_helius_key"], "test-helius-not-a-secret")
            symbol = creators[spec["json"]["params"]["creatorAddress"]]
            return {"status": 200, "body": _saved(symbol, "das_byCreator")}

        desk_providers.set_transport(transport)
        cross_source.set_fetcher(desk_providers.production_fetch)
        for symbol, expected in EXPECTED.items():
            fetched = cross_source.fetch_bundle(TOKENS[symbol], "")
            self.assertIsNotNone(fetched, symbol)
            verdict = cross_source.judge(fetched)
            direct = cross_source.judge(_bundle(symbol))
            self.assertEqual(verdict["overall"], direct["overall"], symbol)
            self.assertEqual(verdict["overall"], expected["overall"], symbol)
            for name, status in expected.items():
                if name == "overall":
                    continue
                self.assertEqual(verdict["checks"][name]["status"], status, symbol + " " + name)
        self.assertTrue(seen)
        self.assertTrue(all(item["headers"]["User-Agent"] == "fomo-desk/1.0" for item in seen))

    def test_a_missing_key_timeout_or_refusal_fails_closed(self):
        mint = TOKENS["X"]
        os.environ.pop("SOLANA_TRACKER_API_KEY")
        with self.assertRaises(cross_source.FetchFailed) as missing:
            desk_providers.production_fetch("holders_chart", mint)
        self.assertEqual(missing.exception.reason, "solana_tracker_api_key_missing")
        self.assertNotIn("secret", missing.exception.reason)
        os.environ["SOLANA_TRACKER_API_KEY"] = "test-tracker-not-a-secret"

        def refuse(spec):
            raise TimeoutError("timed out " + os.environ["SOLANA_TRACKER_API_KEY"])

        desk_providers.set_transport(refuse)
        with self.assertRaises(cross_source.FetchFailed) as timed:
            desk_providers.production_fetch("solana_tracker_token", mint)
        self.assertEqual(timed.exception.reason, "solana_tracker_timeout")
        self.assertNotIn("test-tracker-not-a-secret", str(timed.exception))
        desk_providers.set_transport(lambda spec: {"status": 403, "body": "error code: 1010"})
        with self.assertRaises(cross_source.FetchFailed) as blocked:
            desk_providers.production_fetch("dex", mint)
        self.assertEqual(blocked.exception.reason, "dexscreener_forbidden")
        desk_providers.set_transport(lambda spec: {"status": 429, "body": {}})
        with self.assertRaises(cross_source.FetchFailed) as limited:
            desk_providers.production_fetch("rugcheck", mint)
        self.assertEqual(limited.exception.reason, "rugcheck_rate_limited")
        desk_providers.set_transport(lambda spec: {"status": 200, "body": ["nope"]})
        with self.assertRaises(cross_source.FetchFailed) as bad:
            desk_providers.production_fetch("dex", mint)
        self.assertEqual(bad.exception.reason, "dexscreener_unparsed")
        desk_providers.set_gap(10)
        calls = {"n": 0}

        def once(spec):
            calls["n"] += 1
            return {"status": 200, "body": {"pairs": []}}

        desk_providers.set_transport(once)
        desk_providers.production_fetch("dex", mint)
        with self.assertRaises(cross_source.FetchFailed) as paced:
            desk_providers.production_fetch("dex", mint)
        self.assertEqual(paced.exception.reason, "dexscreener_rate_limited")
        self.assertEqual(calls["n"], 1)
        desk_providers.set_transport(lambda spec: {"status": 403, "body": "error code: 1010"})
        hermes = desk_providers.fetch_hermes(NOW)
        self.assertEqual(hermes["reason"], "price_unknown")
        self.assertEqual(hermes["cause"], "hermes_unauthorized")
        self.assertFalse(hermes["onchain_substituted"])
        self.assertNotIn("price", hermes)
        seen = {}

        def capture(spec):
            seen.update(spec)
            return {"status": 200, "body": {"parsed": []}}

        desk_providers.set_gap(0)
        desk_providers.set_transport(capture)
        desk_providers.fetch_hermes(NOW)
        self.assertEqual(seen["headers"]["User-Agent"], "fomo-desk/1.0")
        self.assertEqual(seen["headers"]["Accept"], "application/json")
        self.assertTrue(seen["headers"]["Authorization"].startswith("Bearer "))
        self.assertNotIn("test-pyth-not-a-secret", seen["url"])

    def test_startup_reports_configured_providers_without_enabling_live(self):
        shown = desk_providers.configured()
        self.assertEqual(set(shown), {"dexscreener", "rugcheck", "solana_tracker", "helius", "hermes"})
        self.assertTrue(all(type(value) is bool for value in shown.values()))
        self.assertTrue(shown["solana_tracker"])
        self.assertNotIn("test-tracker-not-a-secret", json.dumps(shown))
        os.environ.pop("HELIUS_API_KEY")
        self.assertFalse(desk_providers.configured()["helius"])
        ready = desk_loop.readiness()
        self.assertFalse(ready["live_ready"])
        self.assertFalse(ready["send"])
        self.assertFalse(ready["sign"])
        self.assertFalse(ready["execution_eligible"])
        self.assertFalse(ready["preflight_ready"])
        self.assertFalse(ready["gate_complete"])
        self.assertTrue(ready["providers"]["dexscreener"])
        self.assertFalse(ready["providers"]["helius"])
        self.assertEqual(db.get_setting("agents_enabled", "0"), "0")
