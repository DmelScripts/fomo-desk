"""Six-role cut log. It records observations and does not change a trade decision."""
import json
import os
import sqlite3
import subprocess
import sys
import unittest
from decimal import Decimal

from fastapi.testclient import TestClient

import cross_source
import db
import desk_loop
import memecoin_desk
import solana_tx
from tests.support import arm_clean_creator, arm_holder_chart, clear_filter_fixtures, temp_desk_env
from tests.test_memecoin_desk import NOW, _onchain, _pair, _passing_cross

TOKEN = "dummy-app-token"
AUTH = {"Authorization": "Bearer " + TOKEN}


def _mint(number: int) -> str:
    return solana_tx.b58encode(bytes([number]) * 32)


def _latest(mint: str) -> dict:
    rows = db.query(
        "SELECT * FROM memecoin_cuts WHERE mint = ? ORDER BY id DESC LIMIT 1",
        (mint,),
    )
    return rows[0]


class CutLogTests(unittest.TestCase):
    def setUp(self):
        _tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        os.environ["APP_TOKEN"] = TOKEN
        os.environ["DESK_PRICE_LOOP"] = "0"
        self.addCleanup(lambda: os.environ.pop("APP_TOKEN", None))
        self.addCleanup(lambda: os.environ.pop("DESK_PRICE_LOOP", None))
        db.init_db()
        memecoin_desk._CUT_WRITER = None
        memecoin_desk._UNPERSISTED_FAILURES = 0
        memecoin_desk._RECONCILE_FAILED = False
        self.addCleanup(lambda: setattr(memecoin_desk, "_CUT_WRITER", None))
        db.execute("UPDATE phantom_control SET value = '0' WHERE key = 'halt'")
        db.set_setting("mode", "PAPER", "test")
        db.set_setting("agents_enabled", "0", "test")
        cross_source.arm_test_fetcher()
        self.addCleanup(cross_source.clear_fetcher)
        arm_holder_chart()
        self.addCleanup(clear_filter_fixtures)

    def test_exact_ratio_is_quote_side_and_a_dollar_floor_is_a_different_rule(self):
        floor = _mint(11)
        cross_source.note_dex(floor, liquidityUsd="1000")
        refused = memecoin_desk.scout(_pair(floor, market_cap_usd="299999"), now=NOW)
        self.assertEqual(refused["reason"], "market_cap")
        self.assertNotEqual(refused["reason"], "liquidity_below_25pct_mcap")
        logged = _latest(floor)
        self.assertEqual(logged["pipeline"], "six_role")
        self.assertEqual(logged["rule"], "market_cap")
        self.assertEqual(logged["stage"], "scout")
        self.assertEqual(logged["market_cap_usd"], "299999")
        self.assertEqual(logged["market_cap_basis"], "marketCap")
        self.assertEqual(logged["liquidity_usd"], "1000")
        self.assertEqual(logged["liquidity_basis"], "dex_liquidity_usd")
        self.assertEqual(logged["ratio"], format(Decimal("1000") / Decimal("299999"), "f"))
        self.assertEqual(logged["pair_address"], cross_source.pair_id_for(floor))
        self.assertEqual(logged["pair_provenance"], "pinned")
        self.assertEqual(logged["fdv_used_as_market_cap"], 0)
        self.assertEqual(logged["measured"], 1)

        mint = _mint(12)
        opened = memecoin_desk.scout(_pair(mint), now=NOW)
        self.assertTrue(opened["ok"], opened)
        arm_clean_creator(mint)
        cross_source.note_bundle(mint, _passing_cross(mint, quote="26", dex_cap="100", tracker_cap="300"))
        rejected = memecoin_desk.filter_case(opened["case_id"], _onchain(), now=NOW)
        self.assertEqual(rejected["reason"], "liquidity_below_25pct_mcap")
        self.assertEqual(memecoin_desk.view(opened["case_id"])["stage"], "rejected")
        ratio = _latest(mint)
        self.assertEqual(ratio["rule"], "liquidity_below_25pct_mcap")
        self.assertEqual(ratio["stage"], "filter")
        self.assertEqual(ratio["liquidity_usd"], "26")
        self.assertEqual(ratio["liquidity_basis"], "quote_side")
        self.assertEqual(ratio["market_cap_usd"], "300")
        self.assertEqual(ratio["market_cap_basis"], "marketCap")
        self.assertEqual(ratio["ratio"], format(Decimal("26") / Decimal("300"), "f"))
        self.assertEqual(ratio["pair_provenance"], "pinned")
        self.assertNotEqual(ratio["liquidity_usd"], "999999999")

    def test_missing_zero_and_invalid_values_stay_explicit(self):
        missing = _mint(21)
        cross_source.note_dex(missing, marketCap=None, fdv="9000000")
        killed = memecoin_desk.scout(_pair(missing, fdv="9000000"), now=NOW)
        self.assertEqual(killed["reason"], "market_cap_missing")
        row = _latest(missing)
        self.assertIsNone(row["market_cap_usd"])
        self.assertIsNone(row["market_cap_basis"])
        self.assertIsNone(row["ratio"])
        self.assertEqual(row["fdv_used_as_market_cap"], 0)
        self.assertNotIn("fdv", row.keys())

        zero_cap = _mint(22)
        cross_source.note_dex(zero_cap, liquidityUsd="50")
        self.assertEqual(memecoin_desk.scout(_pair(zero_cap, market_cap_usd="0"), now=NOW)["reason"], "market_cap")
        zero = _latest(zero_cap)
        self.assertEqual(zero["market_cap_usd"], "0")
        self.assertIsNone(zero["ratio"])

        invalid = _mint(23)
        cross_source.note_dex(invalid, liquidityUsd="NaN")
        self.assertEqual(memecoin_desk.scout(_pair(invalid), now=NOW)["reason"], "liquidity")
        bad = _latest(invalid)
        self.assertIsNone(bad["liquidity_usd"])
        self.assertIsNone(bad["liquidity_basis"])
        self.assertIsNone(bad["ratio"])
        self.assertEqual(bad["market_cap_usd"], "1000000")

        zero_liq = _mint(24)
        cross_source.note_dex(zero_liq, liquidityUsd="0")
        self.assertEqual(memecoin_desk.scout(_pair(zero_liq), now=NOW)["reason"], "liquidity")
        none_as_zero = _latest(zero_liq)
        self.assertEqual(none_as_zero["liquidity_usd"], "0")
        self.assertEqual(none_as_zero["ratio"], "0")

    def test_a_pinned_pool_is_the_one_recorded_and_a_pass_is_unchanged(self):
        mint = _mint(31)
        shallow = "shallow-pool-" + mint[:8]
        deep = "deep-pool-" + mint[:8]
        created = int((NOW - 3600) * 1000)
        bundle = _passing_cross(mint)
        bundle["dex"] = {"pairs": [
            {
                "pairAddress": deep, "baseToken": {"address": mint},
                "quoteToken": {"address": solana_tx.NATIVE_MINT},
                "marketCap": 5_000_000, "fdv": 9_000_000, "liquidity": {"usd": 500},
                "priceUsd": "2", "priceChange": {"h1": 1}, "pairCreatedAt": created,
            },
            {
                "pairAddress": shallow, "baseToken": {"address": mint},
                "quoteToken": {"address": solana_tx.NATIVE_MINT},
                "marketCap": 1000, "liquidity": {"usd": 50},
                "priceUsd": "9", "priceChange": {"h1": 1}, "pairCreatedAt": created,
            },
        ]}
        cross_source.note_bundle(mint, bundle)
        refused = memecoin_desk.scout(_pair(mint, pair_address=shallow), now=NOW)
        self.assertEqual(refused["reason"], "market_cap", refused)
        row = _latest(mint)
        self.assertEqual(row["pair_address"], shallow)
        self.assertEqual(row["pair_provenance"], "pinned")
        self.assertEqual(row["market_cap_usd"], "1000")
        self.assertEqual(row["liquidity_usd"], "50")
        self.assertNotEqual(row["market_cap_usd"], "5000000")
        self.assertNotEqual(row["liquidity_usd"], "500")

        passed_mint = _mint(32)
        opened = memecoin_desk.scout(_pair(passed_mint), now=NOW)
        self.assertTrue(opened["ok"], opened)
        arm_clean_creator(passed_mint)
        passed = memecoin_desk.filter_case(opened["case_id"], _onchain(), now=NOW)
        self.assertTrue(passed["ok"], passed)
        self.assertEqual(memecoin_desk.view(opened["case_id"])["stage"], "filter_passed")
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM memecoin_cuts WHERE case_id = ?", (opened["case_id"],))[0]["n"], 0)

    def test_repeats_are_counted_separately_from_unique_mints_and_scanner_kills_stay_out(self):
        mint = _mint(41)
        opened = memecoin_desk.scout(_pair(mint), now=NOW)
        arm_clean_creator(mint)
        cross_source.note_bundle(mint, _passing_cross(mint, quote="26", dex_cap="100", tracker_cap="300"))
        rejected = memecoin_desk.filter_case(opened["case_id"], _onchain(), now=NOW)
        self.assertEqual(rejected["reason"], "liquidity_below_25pct_mcap")
        db.execute("UPDATE memecoin_cases SET updated_at = ? WHERE id = ?", (NOW, opened["case_id"]))

        def boom(_request):
            raise AssertionError("cooldown must not refetch")

        again = desk_loop.run_once(mint, boom, NOW + 10)
        self.assertEqual(again["reason"], "reeval_cooldown")
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM memecoin_cuts WHERE rule = 'reeval_cooldown'")[0]["n"], 0)
        report = memecoin_desk.cut_report()
        cooldown = next(item for item in report["by_rule"] if item["rule"] == "reeval_cooldown")
        self.assertEqual(cooldown["events"], 1)
        self.assertEqual(cooldown["measured_events"], 0)
        self.assertEqual(cooldown["ratio_unknown"], 1)
        self.assertEqual(report["repeat_windows"][0]["unmeasured_events"], 1)
        self.assertEqual(report["repeat_windows"][0]["unique_mints"], 1)
        self.assertEqual(report["events"], 2)
        self.assertEqual(report["unique_mints"], 1)
        self.assertEqual(report["measured_events"], 1)
        self.assertEqual(report["unmeasured_events"], 1)
        self.assertIsNone(report["proposal"])
        self.assertTrue(report["legacy_scanner_excluded"])
        self.assertTrue(report["not_a_threshold_proposal"])
        self.assertFalse(report["send"])
        self.assertFalse(report["sign"])

        db.execute(
            "INSERT INTO scans (mint, ts, liquidity_usd, top10_pct, h1_change_pct, age_min, verdict, reason) VALUES (?, ?, ?, ?, ?, ?, 'kill', ?)",
            ("scanner-only-mint", NOW, 12489.45, 10, 1, 5, "$12,489"),
        )
        after = memecoin_desk.cut_report()
        self.assertEqual(after["events"], 2)
        self.assertNotIn("scanner-only-mint", [item["mint"] for item in after["rows"]])
        self.assertEqual(db.query("SELECT reason FROM scans WHERE mint = 'scanner-only-mint'")[0]["reason"], "$12,489")

        import server
        client = TestClient(server.app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        denied = client.get("/api/memecoin/cuts")
        self.assertEqual(denied.status_code, 401)
        body = client.get("/api/memecoin/cuts?limit=10", headers=AUTH)
        self.assertEqual(body.status_code, 200, body.text)
        payload = body.json()
        self.assertEqual(payload["pipeline"], "six_role")
        self.assertEqual(payload["events"], 2)
        self.assertEqual(payload["unique_mints"], 1)
        self.assertIsNone(payload["proposal"])
        self.assertTrue(all(item["pipeline"] == "six_role" for item in payload["rows"]))
        self.assertTrue(payload["complete"])
        self.assertFalse(payload["truncated"])
        self.assertEqual(payload["horizon_s"], memecoin_desk.CUT_HORIZON_S)

    def _cut(self, mint, moment, measured, rule):
        return memecoin_desk.make_cut(
            observed_at=moment, mint=mint, pair_address=None, pair_provenance="unknown",
            case_id=None, stage="scout" if measured else "rejected", rule=rule, measured=measured,
            liquidity="1000" if measured else None, market_cap="299999" if measured else None,
            liquidity_basis="dex_liquidity_usd" if measured else None,
            market_cap_basis="marketCap" if measured else None,
        )

    def test_overnight_samples_survive_a_cooldown_flood(self):
        mints = [_mint(index) for index in range(1, 24)]
        span = 16 * 3600

        def load(conn):
            memecoin_desk._ensure_conn(conn)
            for mint in mints:
                for offset in range(0, span, 60):
                    measured = offset % memecoin_desk.REEVAL_COOLDOWN_S == 0
                    memecoin_desk._insert_cut(conn, self._cut(
                        mint, NOW + offset, measured, "market_cap" if measured else "reeval_cooldown",
                    ))

        db.exclusive(load)
        per_mint_passes = span // 60
        per_mint_measured = span // memecoin_desk.REEVAL_COOLDOWN_S
        report = memecoin_desk.cut_report()
        self.assertEqual(report["measured_events"], per_mint_measured * len(mints))
        self.assertEqual(report["unmeasured_events"], (per_mint_passes - per_mint_measured) * len(mints))
        self.assertEqual(report["unique_mints"], len(mints))
        self.assertEqual(report["events"], per_mint_passes * len(mints))
        self.assertTrue(report["complete"], report)
        self.assertFalse(report["truncated"])
        self.assertGreaterEqual(report["covers_through"] - report["covers_from"], span - 60)
        self.assertLess(db.query("SELECT COUNT(*) AS n FROM memecoin_cut_repeats")[0]["n"], report["unmeasured_events"])
        before = report["measured_events"]
        flood_at = NOW + span - 60
        window = int(flood_at // memecoin_desk.REEVAL_COOLDOWN_S) * memecoin_desk.REEVAL_COOLDOWN_S
        prior = db.query(
            "SELECT events FROM memecoin_cut_repeats WHERE mint = ? AND window_start = ?",
            (mints[0], window),
        )[0]["events"]
        flood = 400
        for _ in range(flood):
            memecoin_desk.persist_cut(self._cut(mints[0], flood_at, False, "reeval_cooldown"))
        flooded = memecoin_desk.cut_report()
        self.assertEqual(flooded["measured_events"], before)
        self.assertEqual(flooded["unmeasured_events"], report["unmeasured_events"] + flood)
        self.assertEqual(flooded["unique_mints"], len(mints))
        self.assertTrue(flooded["complete"])
        self.assertEqual(
            db.query("SELECT events FROM memecoin_cut_repeats WHERE mint = ? AND window_start = ?", (mints[0], window))[0]["events"],
            prior + flood,
        )

    def test_horizon_drops_whole_windows_and_a_cap_is_reported(self):
        mint = _mint(61)
        memecoin_desk.persist_cut(self._cut(mint, NOW, True, "age"))
        for _ in range(5):
            memecoin_desk.persist_cut(self._cut(mint, NOW, False, "reeval_cooldown"))
        kept = memecoin_desk.persist_cut(self._cut(mint, NOW + memecoin_desk.CUT_HORIZON_S, True, "age"))
        self.assertTrue(kept)
        still = memecoin_desk.cut_report()
        self.assertEqual(still["measured_events"], 2)
        self.assertEqual(still["unmeasured_events"], 5)
        self.assertTrue(still["complete"])
        memecoin_desk.persist_cut(self._cut(mint, NOW + memecoin_desk.CUT_HORIZON_S + memecoin_desk.REEVAL_COOLDOWN_S, False, "reeval_cooldown"))
        moved = memecoin_desk.cut_report()
        self.assertEqual(moved["measured_events"], 1)
        self.assertEqual(moved["unmeasured_events"], 1)
        self.assertTrue(moved["complete"])
        self.assertFalse(moved["truncated"])
        self.assertGreaterEqual(moved["covers_from"], NOW + memecoin_desk.CUT_HORIZON_S)

        previous = memecoin_desk.CUT_MEASURED_CAP
        memecoin_desk.CUT_MEASURED_CAP = 2
        self.addCleanup(lambda: setattr(memecoin_desk, "CUT_MEASURED_CAP", previous))
        for index in range(2):
            memecoin_desk.persist_cut(self._cut(_mint(70 + index), NOW + memecoin_desk.CUT_HORIZON_S + 10 + index, True, "age"))
        capped = memecoin_desk.cut_report()
        self.assertEqual(capped["measured_events"], 2)
        self.assertTrue(capped["truncated"])
        self.assertFalse(capped["complete"])
        self.assertIn("measured_cap", capped["truncation_reason"])
        self.assertTrue(capped["diagnostic_ok"])
        self.assertEqual(capped["cut_write_failures"], 0)

    def test_a_cut_write_failure_does_not_change_the_decision(self):
        def explode(_conn, _cut):
            raise sqlite3.OperationalError("injected cut failure")

        memecoin_desk._CUT_WRITER = explode
        floor = _mint(81)
        cross_source.note_dex(floor, liquidityUsd="1000")
        scouted = memecoin_desk.scout(_pair(floor, market_cap_usd="299999"), now=NOW)
        self.assertEqual(scouted["reason"], "market_cap")
        self.assertIsNone(memecoin_desk.view(floor))
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM memecoin_cuts")[0]["n"], 0)

        opened = memecoin_desk.scout(_pair(_mint(82)), now=NOW)
        self.assertTrue(opened["ok"], opened)
        refused = memecoin_desk.filter_case(opened["case_id"], {"source": "dexscreener"}, now=NOW)
        self.assertEqual(refused["reason"], "onchain_source")
        self.assertEqual(memecoin_desk.view(opened["case_id"])["stage"], "rejected")
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM memecoin_cuts WHERE case_id = ?", (opened["case_id"],))[0]["n"], 0)

        bare = _mint(83)

        def pairs(request):
            return {"status": 200, "body": [{
                "chainId": "solana", "pairAddress": "pool-" + bare[:8],
                "baseToken": {"address": bare}, "quoteToken": {"address": solana_tx.NATIVE_MINT},
                "liquidity": {"usd": 10}, "priceUsd": "1", "priceChange": {"h1": 1},
                "pairCreatedAt": int((NOW - 3600) * 1000), "fdv": 9_000_000,
            }]}

        parsed = desk_loop.run_once(bare, pairs, NOW)
        self.assertEqual(parsed["reason"], "market_cap_missing")
        self.assertIsNone(memecoin_desk.view(bare))

        db.execute("UPDATE memecoin_cases SET updated_at = ? WHERE id = ?", (NOW, opened["case_id"]))
        cooled = desk_loop.run_once(_mint(82), lambda _request: (_ for _ in ()).throw(AssertionError("refetch")), NOW + 10)
        self.assertEqual(cooled["reason"], "reeval_cooldown")
        self.assertEqual(memecoin_desk.view(opened["case_id"])["stage"], "rejected")

        noted = memecoin_desk.cut_from_case(opened["case_id"], observed_at=NOW, stage="filter", rule="lp")
        self.assertEqual(noted["rule"], "lp")
        self.assertEqual(memecoin_desk.view(opened["case_id"])["stage"], "rejected")
        broken = memecoin_desk.cut_report()
        self.assertFalse(broken["diagnostic_ok"])
        self.assertFalse(broken["complete"])
        self.assertFalse(broken["read_failed"])
        self.assertGreaterEqual(broken["cut_write_failures"], 4)

        left = _mint(84)
        right = _mint(85)

        def discovery(request):
            url = str(request.get("url") or "")
            if url.endswith("/token-boosts/latest/v1"):
                return {"status": 200, "body": [
                    {"chainId": "solana", "tokenAddress": left},
                    {"chainId": "solana", "tokenAddress": right},
                ]}
            mint = url.rsplit("/", 1)[-1]
            return {"status": 200, "body": [{
                "chainId": "solana", "pairAddress": "pool-" + mint[:8],
                "baseToken": {"address": mint}, "quoteToken": {"address": solana_tx.NATIVE_MINT},
                "liquidity": {"usd": 10}, "priceUsd": "1", "priceChange": {"h1": 1},
                "pairCreatedAt": int((NOW - 3600) * 1000),
            }]}

        found = desk_loop.run_discovery(discovery, NOW)
        self.assertEqual(
            {item.get("mint"): item.get("reason") for item in found["kills"] if item.get("mint")},
            {left: "market_cap_missing", right: "market_cap_missing"},
        )
        supervised = desk_loop.run_supervised(discovery, NOW, rounds=1)
        self.assertTrue(supervised["ok"], supervised)
        self.assertEqual(supervised["faults"], 0)
        self.assertNotEqual(db.get_setting("memecoin_entry_halt", ""), "1")

    def test_a_lost_cut_stays_incomplete_after_restart(self):
        kept = _mint(91)
        missed = _mint(92)
        self.assertTrue(memecoin_desk.persist_cut(self._cut(kept, NOW, True, "age")))
        self.assertTrue(memecoin_desk.cut_report()["complete"])

        def explode(_conn, _cut):
            raise sqlite3.OperationalError("injected cut failure")

        memecoin_desk._CUT_WRITER = explode
        self.assertFalse(memecoin_desk.persist_cut(self._cut(missed, NOW + 1, True, "market_cap")))
        self.assertEqual(db.query("SELECT mint FROM memecoin_cuts")[0]["mint"], kept)
        here = memecoin_desk.cut_report()
        self.assertFalse(here["complete"])
        self.assertFalse(here["diagnostic_ok"])
        self.assertGreaterEqual(here["cut_write_failures"], 1)

        code = (
            "import json, sys\n"
            "sys.path.insert(0, %r)\n"
            "import memecoin_desk\n"
            "report = memecoin_desk.cut_report()\n"
            "rows = __import__('db').query('SELECT mint FROM memecoin_cuts')\n"
            "json.dump({'complete': report['complete'], 'diagnostic_ok': report['diagnostic_ok'],"
            " 'failures': report['cut_write_failures'], 'mints': [row['mint'] for row in rows],"
            " 'measured': report['measured_events']}, sys.stdout)\n"
        ) % os.getcwd()
        env = os.environ.copy()
        env["DESK_DATA_DIR"] = os.environ["DESK_DATA_DIR"]
        fresh = json.loads(subprocess.check_output([sys.executable, "-c", code], env=env, cwd=os.getcwd()))
        self.assertFalse(fresh["complete"])
        self.assertFalse(fresh["diagnostic_ok"])
        self.assertGreaterEqual(fresh["failures"], 1)
        self.assertEqual(fresh["measured"], 1)
        self.assertEqual(fresh["mints"], [kept])
        self.assertNotIn(missed, fresh["mints"])

        db.execute("UPDATE memecoin_cut_health SET attempt_open = 1, failed = 0, failures = 0 WHERE id = 1")
        crashed = json.loads(subprocess.check_output([sys.executable, "-c", code], env=env, cwd=os.getcwd()))
        self.assertFalse(crashed["complete"])
        self.assertFalse(crashed["diagnostic_ok"])
        self.assertGreaterEqual(crashed["failures"], 1)
        self.assertEqual(crashed["mints"], [kept])
