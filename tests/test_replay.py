"""Feed the sample migration through all five agents in PAPER.

The captured DexScreener pool is $12,489.45, under the $40,000 kill, and the
captured getTokenLargestAccounts body is a 429 with no field paths. Those two
facts cannot open a position. This replay keeps every other sample byte and
only raises liquidity.usd to 50000 and stubs the unparseable top-10 at 10%
so Auditor, Analyst, Executor and Risk still see the real samples.
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


class FakeHttp:
    def __init__(self, dex):
        self.dex = dex
        self.posts = []

    async def get_json(self, url):
        if "dexscreener" in url:
            return self.dex
        if url.endswith("/report"):
            return load("rugcheck_report.json")
        if "insiders/graph" in url:
            return load("rugcheck_insiders_graph.json")
        raise AssertionError(url)

    async def post_json(self, url, body, headers=None):
        self.posts.append(body.get("method") if isinstance(body, dict) else url)
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
            return load("rpc_getTokenLargestAccounts.json")
        if method == "getAccountInfo":
            return load("rpc_getAccountInfo.json")
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
    def test_sample_migration_reaches_a_paper_position(self):
        tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        db.init_db()
        os.environ["TYPESAFE_API_KEY"] = "replay-key-not-real"
        self.addCleanup(lambda: os.environ.pop("TYPESAFE_API_KEY", None))

        dex = copy.deepcopy(load("dexscreener_pair.json"))
        dex[0]["liquidity"]["usd"] = 50000
        created = dex[0]["pairCreatedAt"] / 1000 + 15 * 60
        http = FakeHttp(dex)
        notifier = Notifier(mode="PAPER")
        desk = Desk(http, notifier, now=lambda: created)

        import agents.scanner as scanner_mod

        def stub_top10(payload, supply, exclude):
            return 10.0

        original = scanner_mod.parse_top10
        scanner_mod.parse_top10 = stub_top10
        self.addCleanup(lambda: setattr(scanner_mod, "parse_top10", original))

        async def fast_sleep(seconds):
            return None

        original_sleep = scanner_mod.sleep
        scanner_mod.sleep = fast_sleep
        self.addCleanup(lambda: setattr(scanner_mod, "sleep", original_sleep))

        scanner = Scanner(desk)
        auditor = Auditor(desk)
        analyst = Analyst(desk)
        executor = Executor(desk)
        risk = Risk(desk)

        async def run():
            scanned = await scanner.ingest(load("pumpportal_migration.json"))
            self.assertEqual(scanned["verdict"], "pass", scanned)
            self.assertEqual(scanned["pool"], "GWc8HL8vCGMnmg3n9pC4tmWafYiWpuoxvKrrhzDVZyJf")
            self.assertEqual(scanned["creator"], "4tKiUbkTbbcPqrTDsjmMWgZWZ7mZucMKJqk2nHE2HP4f")
            payload = await desk.bus.auditor.get()
            audited = await auditor.audit_payload(payload)
            self.assertTrue(audited["passed"], [layer["value"] for layer in audited["layers"]])
            verdict_in = await desk.bus.analyst.get()
            verdict = await analyst.judge(verdict_in)
            self.assertTrue(verdict["passed"], verdict)
            order = await desk.bus.executor.get()
            filled = await executor.handle(order)
            self.assertEqual(filled["mode"], "PAPER")
            row = db.query("SELECT * FROM positions WHERE mint = ?", (MINT,))[0]
            held = risk.poll_position(dict(row), created, price=float(dex[0]["priceUsd"]), liquidity=50000)
            self.assertEqual(held["action"], "hold")

        asyncio.run(run())

        self.assertTrue(db.query("SELECT id FROM scans WHERE mint = ?", (MINT,)))
        self.assertEqual(len(db.query("SELECT id FROM audits WHERE mint = ?", (MINT,))), 8)
        self.assertEqual(db.query("SELECT \"pass\" FROM verdicts WHERE mint = ?", (MINT,))[0]["pass"], 1)
        signal = db.query("SELECT link, mode FROM signals WHERE mint = ?", (MINT,))[0]
        self.assertEqual(signal["mode"], "PAPER")
        self.assertEqual(signal["link"], links.coin_url(MINT, "solana"))
        self.assertEqual(db.query("SELECT mode FROM positions WHERE mint = ?", (MINT,))[0]["mode"], "PAPER")
        self.assertTrue(db.query("SELECT id FROM activity WHERE mint = ?", (MINT,)))
        for push in notifier.pushes:
            if push["url"]:
                self.assertEqual(push["url"], links.coin_url(MINT, "solana"))
            for button in push["buttons"]:
                if button.get("url"):
                    self.assertEqual(button["url"], links.coin_url(MINT, "solana"))
        for row in db.query("SELECT link FROM signals"):
            self.assertEqual(row["link"], links.coin_url(MINT, "solana"))
        beats = {row["agent"] for row in db.query("SELECT agent FROM heartbeats")}
        self.assertEqual(beats, {"scanner", "auditor", "analyst", "executor", "risk"})
