"""Sample migration through the five agents.

The unmodified DexScreener sample is $12,489.45, so Scanner kills it
("liq $12.5K") before anyone else runs. The pass-through below changes only:

1. liquidity.usd 12489.45 -> 50000, because $12.5K is under the $40,000 kill.
2. getTokenLargestAccounts returns a synthetic success (pool excluded, top 10
   at 20%). The captured file is a JSON-RPC 429 and has no account list.
3. getTokenAccountsByOwner returns an empty list (creator balance 0). The
   archive has no capture of that method; RugCheck creatorBalance is 0.

Jev is mocked. The clock moves 15 minutes so pool age can clear 10 minutes.
"""
import asyncio
import copy
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db
import links
from agents.analyst import Analyst
from agents.auditor import Auditor
from agents.executor import Executor
from agents.risk import Risk
from agents.scanner import Scanner
from bus import Desk
from notifier import Notifier
from tests.support import load, temp_desk_env

MINT = "4FHEBKNYy6BZ1QFMKj1xL8HDTM12rdXLRYw8DgiCUnQn"
POOL = "GWc8HL8vCGMnmg3n9pC4tmWafYiWpuoxvKrrhzDVZyJf"


def _largest():
    supply = load("rpc_getTokenSupply.json")["result"]["value"]["uiAmount"]
    accounts = [{"address": POOL, "owner": POOL, "uiAmount": supply * 0.21}]
    accounts += [{"address": f"h{i}", "owner": f"o{i}", "uiAmount": supply * 0.02} for i in range(10)]
    return {"jsonrpc": "2.0", "result": {"value": accounts}, "id": 1}


class FakeHttp:
    def __init__(self, dex, largest):
        self.dex = dex
        self.largest = largest

    async def get_json(self, url):
        if "dexscreener" in url:
            return self.dex
        if url.endswith("/report"):
            return load("rugcheck_report.json")
        if "insiders/graph" in url:
            return load("rugcheck_insiders_graph.json")
        raise AssertionError(url)

    async def post_json(self, url, body, headers=None, timeout=None):
        if "typesafe" in url:
            return {
                "model": "jev-latest",
                "usage": {"input_tokens": 11},
                "answers": {
                    "alive_6h": {"noul": 0.80},
                    "organic": {"noul": 0.80},
                    "flush": {"noul": 0.80},
                    "chase": {"noul": 0.10},
                },
            }
        method = body.get("method")
        if method == "getTokenSupply":
            return load("rpc_getTokenSupply.json")
        if method == "getTokenLargestAccounts":
            return self.largest
        if method == "getAccountInfo":
            return load("rpc_getAccountInfo.json")
        if method == "getTokenAccountsByOwner":
            return {"result": {"value": []}}
        if method == "getProgramAccounts":
            program = body["params"][0]
            name = (
                "rpc_getProgramAccounts_token2022.json"
                if str(program).endswith("uEb")
                else "rpc_getProgramAccounts_token.json"
            )
            return load(name)
        raise AssertionError(method)


class TestReplay(unittest.TestCase):
    def test_raw_sample_is_a_liquidity_kill(self):
        tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        db.init_db()
        http = FakeHttp(load("dexscreener_pair.json"), load("rpc_getTokenLargestAccounts.json"))
        desk = Desk(http, Notifier(mode="PAPER"), now=lambda: 1_000_000.0)
        scanned = asyncio.run(Scanner(desk).ingest(load("pumpportal_migration.json")))
        self.assertEqual(scanned["verdict"], "kill")
        self.assertEqual(scanned["reason"], "liq $12.5K")

    def test_sample_migration_reaches_a_paper_position(self):
        tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        db.init_db()
        os.environ["TYPESAFE_API_KEY"] = "replay-key-not-real"
        self.addCleanup(lambda: os.environ.pop("TYPESAFE_API_KEY", None))
        dex = copy.deepcopy(load("dexscreener_pair.json"))
        dex[0]["liquidity"]["usd"] = 50000
        clock = {"t": 2_000_000.0}
        http = FakeHttp(dex, _largest())
        notifier = Notifier(mode="PAPER")
        desk = Desk(http, notifier, now=lambda: clock["t"])
        scanner = Scanner(desk)
        auditor = Auditor(desk)
        analyst = Analyst(desk)
        executor = Executor(desk)
        risk = Risk(desk)

        async def run():
            first = await scanner.ingest(load("pumpportal_migration.json"))
            self.assertEqual(first["verdict"], "wait", first)
            clock["t"] += 15 * 60
            scanned = await scanner.ingest(load("pumpportal_migration.json"))
            self.assertEqual(scanned["verdict"], "pass", scanned)
            self.assertEqual(scanned["pool"], POOL)
            self.assertEqual(scanned["creator"], "4tKiUbkTbbcPqrTDsjmMWgZWZ7mZucMKJqk2nHE2HP4f")
            audited = await auditor.audit_payload(await desk.bus.auditor.get())
            self.assertTrue(audited["passed"], [layer["value"] for layer in audited["layers"]])
            verdict = await analyst.judge(await desk.bus.analyst.get())
            self.assertTrue(verdict["passed"], verdict)
            filled = await executor.handle(await desk.bus.executor.get())
            self.assertEqual(filled["status"], "bought")
            self.assertEqual(filled["sizeUsd"], 47.50)
            row = db.query("SELECT * FROM positions WHERE mint = ?", (MINT,))[0]
            held = risk.poll_position(dict(row), clock["t"], price=float(dex[0]["priceUsd"]), liquidity=50000)
            self.assertEqual(held["action"], "hold")

        asyncio.run(run())
        self.assertEqual(db.query("SELECT link, mode FROM signals WHERE mint = ?", (MINT,))[0]["link"], links.coin_url(MINT, "solana"))
        self.assertEqual(db.query("SELECT mode FROM positions WHERE mint = ?", (MINT,))[0]["mode"], "PAPER")
        self.assertTrue(db.query("SELECT id FROM audits WHERE mint = ?", (MINT,)))
        self.assertEqual(db.query('SELECT "pass" FROM verdicts WHERE mint = ?', (MINT,))[0]["pass"], 1)
        self.assertTrue(db.query("SELECT id FROM activity WHERE mint = ?", (MINT,)))
        for push in notifier.pushes:
            if push["url"]:
                self.assertEqual(push["url"], links.coin_url(MINT, "solana"))
            for button in push["buttons"]:
                if button.get("url"):
                    self.assertEqual(button["url"], links.coin_url(MINT, "solana"))
        beats = {row["agent"] for row in db.query("SELECT agent FROM heartbeats")}
        self.assertEqual(beats, {"scanner", "auditor", "analyst", "executor", "risk"})


class TestReplayGuard(unittest.TestCase):
    def test_guard_refuses_to_start_without_an_isolated_directory(self):
        import replay
        previous_replay = os.environ.get("DESK_REPLAY")
        previous_dir = os.environ.get("DESK_DATA_DIR")
        self.addCleanup(lambda: _put("DESK_REPLAY", previous_replay))
        self.addCleanup(lambda: _put("DESK_DATA_DIR", previous_dir))
        os.environ.pop("DESK_REPLAY", None)
        with self.assertRaises(SystemExit):
            replay.guard()
        os.environ["DESK_REPLAY"] = "1"
        os.environ["DESK_DATA_DIR"] = "/var/data"
        with self.assertRaises(SystemExit):
            replay.guard()
        os.environ["DESK_DATA_DIR"] = "/tmp/production-data"
        with self.assertRaises(SystemExit):
            replay.guard()
        os.environ["DESK_DATA_DIR"] = "/tmp/replay-step13"
        self.assertEqual(replay.guard().name, "replay-step13")

    def test_prepare_forces_paper_and_does_not_enable_agents(self):
        import replay
        tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        db.init_db()
        db.set_setting("mode", "LIVE", "test")
        db.set_setting("agents_enabled", "1", "test")
        previous_jev = os.environ.pop("TYPESAFE_API_KEY", None)
        self.addCleanup(lambda: _put("TYPESAFE_API_KEY", previous_jev))
        replay.prepare()
        self.assertEqual(db.get_setting("mode"), "PAPER")
        self.assertEqual(db.get_setting("agents_enabled"), "0")
        self.assertEqual(db.get_setting("analyst_mode"), "rules")
        self.assertFalse(__import__("agents.executor", fromlist=["paper_night_done"]).paper_night_done())

    def test_notify_off_records_activity_without_calling_webpush(self):
        from notifier import Notifier
        tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        db.init_db()
        previous = os.environ.get("DESK_NOTIFY")
        os.environ["DESK_NOTIFY"] = "0"
        self.addCleanup(lambda: _put("DESK_NOTIFY", previous))
        called = {"n": 0}

        def boom(*_args, **_kwargs):
            called["n"] += 1

        import webpush
        original = webpush.deliver
        webpush.deliver = boom
        self.addCleanup(lambda: setattr(webpush, "deliver", original))
        Notifier(mode="PAPER").push(
            "Buy FIX", "body", links.coin_url(MINT, "solana"),
            [{"label": "Buy on FOMO", "url": links.coin_url(MINT, "solana")}],
        )
        self.assertEqual(called["n"], 0)
        self.assertTrue(db.query("SELECT id FROM activity"))


def _put(key, value):
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value
