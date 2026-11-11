"""The production reader uses stubbed HTTP. It does not open a socket."""
import json
import os
import threading
import time
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
        self.clock = {"t": 0.0}
        self.slept = []

        def _now():
            return self.clock["t"]

        def _sleep(seconds):
            self.slept.append(seconds)
            self.clock["t"] += seconds

        self._now = _now
        self._sleep = _sleep
        desk_providers.set_clock(_now, _sleep)
        desk_providers.set_transport(None)
        self.addCleanup(lambda: desk_providers.set_transport(None))
        self.addCleanup(lambda: desk_providers.set_clock(None, None))
        self.addCleanup(lambda: desk_providers.set_decision_timeout(None))
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
                saved = _saved(symbol, "dex_pair")
                self.assertIsInstance(saved, dict)
                self.assertIn("pairs", saved)
                return {"status": 200, "body": list(saved["pairs"])}
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
        desk_providers.set_transport(lambda spec: {"status": 200, "body": ["nope"]})
        with self.assertRaises(cross_source.FetchFailed) as other:
            desk_providers.production_fetch("rugcheck", mint)
        self.assertEqual(other.exception.reason, "rugcheck_unparsed")
        desk_providers.set_decision_timeout(0.1)
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
        desk_providers.set_decision_timeout(None)
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

        desk_providers.set_transport(capture)
        desk_providers.fetch_hermes(NOW)
        self.assertEqual(seen["headers"]["User-Agent"], "fomo-desk/1.0")
        self.assertEqual(seen["headers"]["Accept"], "application/json")
        self.assertTrue(seen["headers"]["Authorization"].startswith("Bearer "))
        self.assertNotIn("test-pyth-not-a-secret", seen["url"])

    def test_default_gap_waits_for_the_second_read(self):
        mint = TOKENS["X"]
        seen = []

        def transport(spec):
            seen.append(spec["url"])
            url = spec["url"]
            if "token-pairs" in url:
                return {"status": 200, "body": list(_saved("X", "dex_pair")["pairs"])}
            if "rugcheck.xyz" in url:
                return {"status": 200, "body": _saved("X", "rugcheck")}
            if "/holders/chart/" in url:
                return {"status": 200, "body": _saved("X", "st_holders_chart")}
            if "/deployer/" in url:
                return {"status": 200, "body": _saved("X", "st_deployer")}
            if "/tokens/" in url:
                return {"status": 200, "body": _saved("X", "st_token")}
            return {"status": 200, "body": _saved("X", "das_byCreator")}

        desk_providers.set_transport(transport)
        cross_source.set_fetcher(desk_providers.production_fetch)
        started = self.clock["t"]
        fetched = cross_source.fetch_bundle(mint, "")
        self.assertIsNotNone(fetched)
        self.assertGreater(self.clock["t"], started)
        tracker = [url for url in seen if "solanatracker.io" in url]
        self.assertGreaterEqual(len(tracker), 2)
        self.assertEqual(cross_source.judge(fetched)["overall"], EXPECTED["X"]["overall"])
        self._slept_about(self.slept, [desk_providers.GAPS["solana_tracker"]] * len(self.slept))
        seen.clear()
        self.slept.clear()
        desk_providers.set_clock(self._now, self._sleep)
        body = desk_providers.production_fetch("deployer", mint)
        self.assertEqual(body, _saved("X", "st_deployer"))
        self.assertEqual(len(seen), 2)
        self.assertIn("/tokens/" + mint, seen[0])
        creator = _saved("X", "st_token")["token"]["creation"]["creator"]
        self.assertTrue(seen[1].endswith("/deployer/" + creator))
        self._slept_about(self.slept, [desk_providers.GAPS["solana_tracker"]])
        self.slept.clear()
        desk_providers.set_decision_timeout(None)
        desk_providers.production_fetch("rugcheck", mint)
        desk_providers.production_fetch("rugcheck", mint)
        self._slept_about(self.slept, [desk_providers.GAPS["rugcheck"]])
        self.slept.clear()
        desk_providers.production_fetch("dex", mint)
        desk_providers.production_fetch("dex", mint)
        self._slept_about(self.slept, [desk_providers.GAPS["dexscreener"]])
        self.slept.clear()
        desk_providers.production_fetch("das", mint)
        desk_providers.production_fetch("das", mint)
        self.assertTrue(any(abs(item - desk_providers.GAPS["helius"]) < 1e-6 for item in self.slept))
        self.slept.clear()
        desk_providers.set_transport(lambda spec: {"status": 200, "body": {"parsed": []}})
        desk_providers.fetch_hermes(NOW)
        desk_providers.fetch_hermes(NOW)
        self._slept_about(self.slept, [desk_providers.GAPS["hermes"]])

    def test_helius_key_stays_out_of_errors(self):
        import traceback
        import urllib.error
        import urllib.request
        key = os.environ["HELIUS_API_KEY"]
        seen = {}

        def boom(request, timeout=None):
            seen["url"] = request.full_url
            raise urllib.error.URLError("unreachable")

        original = urllib.request.urlopen
        urllib.request.urlopen = boom
        self.addCleanup(lambda: setattr(urllib.request, "urlopen", original))
        desk_providers.set_transport(None)
        with self.assertRaises(cross_source.FetchFailed) as caught:
            desk_providers._urllib({
                "provider": "helius",
                "method": "POST",
                "url": "https://mainnet.helius-rpc.com/",
                "headers": {"Accept": "application/json", "User-Agent": "fomo-desk/1.0", "_helius_key": key},
                "json": {"method": "getAssetsByCreator"},
            })
        text = "".join(traceback.format_exception(caught.exception))
        self.assertNotIn(key, text)
        self.assertNotIn(key, repr(caught.exception))
        self.assertNotIn("api-key=", text)
        self.assertIn(key, seen["url"])
        self.assertNotIn(key, str(caught.exception))

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

    def test_concurrent_callers_take_distinct_slots(self):
        mint = TOKENS["X"]
        gap = desk_providers.GAPS["dexscreener"]
        desk_providers.set_clock(None, None)
        desk_providers.set_decision_timeout(None)
        sent = []
        guard = threading.Lock()

        def transport(spec):
            with guard:
                sent.append(time.monotonic())
            return {"status": 200, "body": {"pairs": []}}

        desk_providers.set_transport(transport)
        barrier = threading.Barrier(3)
        errors = []

        def run():
            try:
                barrier.wait()
                desk_providers.production_fetch("dex", mint)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(sent), 3)
        sent.sort()
        for earlier, later in zip(sent, sent[1:]):
            self.assertGreaterEqual(later - earlier, gap - 0.05)

    def test_a_slot_past_the_deadline_is_not_reserved(self):
        mint = TOKENS["X"]
        calls = {"n": 0}

        def transport(spec):
            calls["n"] += 1
            return {"status": 200, "body": {"pairs": []}}

        desk_providers.set_transport(transport)
        desk_providers.production_fetch("dex", mint)
        before = list(self.slept)
        desk_providers.set_decision_timeout(0.1)
        with self.assertRaises(cross_source.FetchFailed) as missed:
            desk_providers.production_fetch("dex", mint)
        self.assertEqual(missed.exception.reason, "dexscreener_rate_limited")
        self.assertEqual(calls["n"], 1)
        self.assertEqual(self.slept, before)
        desk_providers.set_decision_timeout(None)
        desk_providers.production_fetch("dex", mint)
        self.assertEqual(calls["n"], 2)
        self._slept_about(self.slept[len(before):], [desk_providers.GAPS["dexscreener"]])

    def _slept_about(self, actual, expected):
        self.assertEqual(len(actual), len(expected))
        for left, right in zip(actual, expected):
            self.assertAlmostEqual(left, right, delta=1e-6)
