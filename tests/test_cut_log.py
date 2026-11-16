"""Six-role cut log. It records observations and does not change a trade decision."""
import os
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
        repeat = _latest(mint)
        self.assertEqual(repeat["rule"], "reeval_cooldown")
        self.assertEqual(repeat["measured"], 0)
        self.assertIsNone(repeat["liquidity_usd"])
        self.assertIsNone(repeat["market_cap_usd"])
        self.assertIsNone(repeat["ratio"])
        report = memecoin_desk.cut_report()
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

    def test_the_cut_log_keeps_only_the_newest_rows(self):
        previous = memecoin_desk.CUT_KEEP
        memecoin_desk.CUT_KEEP = 2
        self.addCleanup(lambda: setattr(memecoin_desk, "CUT_KEEP", previous))
        for index in range(4):
            memecoin_desk.persist_cut(memecoin_desk.make_cut(
                observed_at=NOW + index, mint=_mint(50 + index), pair_address=None, pair_provenance="unknown",
                case_id=None, stage="scout", rule="age", measured=True,
                liquidity=None, market_cap=None, liquidity_basis=None, market_cap_basis=None,
            ))
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM memecoin_cuts")[0]["n"], 2)
        kept = db.query("SELECT mint FROM memecoin_cuts ORDER BY id")
        self.assertEqual([row["mint"] for row in kept], [_mint(52), _mint(53)])
