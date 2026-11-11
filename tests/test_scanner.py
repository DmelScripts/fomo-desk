"""Scanner kill order and the reason strings from the prompt."""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.scanner import format_h1, format_liq, format_top10, kill_reason, parse_largest_accounts
from tests.support import load

SETTINGS = {
    "min_liquidity_usd": 40000,
    "max_top10_pct": 25,
    "max_h1_change_pct": 80,
    "min_pool_age_min": 10,
    "max_wait_min": 60,
    "runner_h24_pct": 300,
}


def _snap(**kwargs):
    base = {
        "liquidity_usd": 50000,
        "top10_pct": 10,
        "h1_change_pct": 5,
        "name": "Labs",
        "symbol": "LABS",
        "age_min": 15,
        "has_pair": True,
        "runners": [],
    }
    base.update(kwargs)
    return base


class TestScanner(unittest.TestCase):
    def test_kill_order_and_reason_strings(self):
        self.assertEqual(format_liq(31200), "liq $31.2K")
        self.assertEqual(format_top10(38), "top 10 hold 38%")
        self.assertEqual(format_h1(112), "+112% in 1h")

        first = kill_reason(_snap(
            liquidity_usd=31200, top10_pct=38, h1_change_pct=112,
            runners=[{"name": "Labs", "symbol": "OLD"}],
        ), SETTINGS)
        self.assertEqual(first, {"verdict": "kill", "reason": "liq $31.2K"})

        second = kill_reason(_snap(top10_pct=38, h1_change_pct=112), SETTINGS)
        self.assertEqual(second["reason"], "top 10 hold 38%")

        third = kill_reason(_snap(h1_change_pct=112), SETTINGS)
        self.assertEqual(third["reason"], "+112% in 1h")

        fourth = kill_reason(_snap(runners=[{"name": "Labs", "symbol": "OLD"}]), SETTINGS)
        self.assertEqual(fourth["reason"], "copycat of OLD")

        self.assertEqual(kill_reason(_snap(), SETTINGS)["verdict"], "pass")
        self.assertEqual(kill_reason(_snap(age_min=5), SETTINGS)["verdict"], "wait")

    def test_no_largest_accounts_is_never_a_pass(self):
        sample = load("rpc_getTokenLargestAccounts.json")
        self.assertIsNone(parse_largest_accounts(sample, 1000, set()))
        # Missing top 10 is not a listed kill. The coin waits, then drops.
        waiting = kill_reason(_snap(top10_pct=None, age_min=15), SETTINGS)
        self.assertEqual(waiting["verdict"], "wait")
        dropped = kill_reason(_snap(top10_pct=None, age_min=61), SETTINGS)
        self.assertEqual(dropped, {"verdict": "kill", "reason": "never qualified"})

    def test_largest_accounts_retries_then_still_red(self):
        import db
        from agents.scanner import Scanner
        from bus import Desk
        from notifier import Notifier
        from tests.support import temp_desk_env

        tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        db.init_db()
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        import agents.scanner as scanner_mod
        original = scanner_mod.sleep
        scanner_mod.sleep = fake_sleep
        self.addCleanup(lambda: setattr(scanner_mod, "sleep", original))

        calls = {"n": 0}
        import solana_rpc

        def transport(payload):
            if payload.get("method") == "getTokenLargestAccounts":
                calls["n"] += 1
                return {"status": 200, "body": load("rpc_getTokenLargestAccounts.json")}
            return {"status": 200, "body": load("rpc_getTokenSupply.json")}

        solana_rpc.TRANSPORT = transport
        self.addCleanup(lambda: setattr(solana_rpc, "TRANSPORT", None))

        class Http:
            async def post_json(self, url, body, headers=None):
                raise AssertionError("scanner rpc must use solana_rpc")

            async def get_json(self, url):
                raise AssertionError(url)

        scanner = Scanner(Desk(Http(), Notifier(mode="PAPER")))

        async def run():
            return await scanner._top10("Mint", "Pool")

        self.assertIsNone(asyncio.run(run()))
        self.assertEqual(calls["n"], 3)
        self.assertEqual(sleeps, [2, 2])

    def test_rpc_errors_do_not_carry_the_helius_key(self):
        import io
        import logging
        import os
        import traceback
        import urllib.error
        import urllib.request
        import db
        import solana_rpc
        from agents.scanner import Scanner
        from bus import Desk
        from notifier import Notifier
        from config import install_secret_redaction
        from tests.support import temp_desk_env

        tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        db.init_db()
        key = "helius-scanner-secret"
        os.environ["HELIUS_API_KEY"] = key
        self.addCleanup(lambda: os.environ.pop("HELIUS_API_KEY", None))
        install_secret_redaction()
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("desk.scanner.rpc.test")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        self.addCleanup(lambda: logger.removeHandler(handler))
        solana_rpc.TRANSPORT = None
        self.addCleanup(lambda: setattr(solana_rpc, "TRANSPORT", None))

        class Http:
            async def post_json(self, url, body, headers=None):
                raise AssertionError("keyed url must not reach post_json")

            async def get_json(self, url):
                raise AssertionError("get")

        scanner = Scanner(Desk(Http(), Notifier(mode="PAPER")))
        seen = {}

        def check(exc):
            try:
                raise exc
            except solana_rpc.RpcRefused:
                logger.exception("rpc failed")
            text = stream.getvalue()
            stream.seek(0)
            stream.truncate(0)
            chained = "".join(traceback.format_exception(exc))
            blob = " ".join([str(exc), repr(exc), str(exc.args), text, chained, str(exc.__cause__), str(exc.__context__)])
            self.assertNotIn(key, blob)
            self.assertNotIn("api-key=", blob)
            self.assertIsNone(exc.__cause__)
            self.assertIsNone(exc.__context__)

        def run(opener):
            original = urllib.request.urlopen
            urllib.request.urlopen = opener
            try:
                async def go():
                    return await scanner._rpc("getSlot", [])

                with self.assertRaises(solana_rpc.RpcRefused) as caught:
                    asyncio.run(go())
            finally:
                urllib.request.urlopen = original
            return caught.exception

        def status(code):
            def opener(request, timeout=8):
                host = request.full_url.split("/")[2].split("?")[0]
                seen["host"] = host
                seen["keyed"] = "api-key=" in request.full_url and key in request.full_url
                raise urllib.error.HTTPError(request.full_url, code, "no", None, None)
            return opener

        refused = run(status(403))
        self.assertEqual(refused.reason, "rpc_http_403")
        self.assertEqual(seen["host"], "mainnet.helius-rpc.com")
        self.assertTrue(seen["keyed"])
        check(refused)
        refused = run(status(500))
        self.assertEqual(refused.reason, "rpc_http_500")
        check(refused)

        def timed_out(request, timeout=8):
            raise TimeoutError("timed out")

        refused = run(timed_out)
        self.assertEqual(refused.reason, "rpc_timeout")
        check(refused)
