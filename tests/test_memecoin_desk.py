"""Six-stage memecoin desk. Fixtures are not a live provider and nothing is sent."""
import json
import os
import threading
import time
import unittest
from decimal import Decimal

import db
import memecoin_desk
import phantom_transport
import raydium_cpmm
import solana_tx
import solana_whirlpool
import token_execution
from fastapi.testclient import TestClient
from tests.support import temp_desk_env

TOKEN = "dummy-app-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
NOW = 1_800_000_000.0


def _mint(number: int) -> str:
    return solana_tx.b58encode(bytes([number]) * 32)


def _pair(mint: str, **over):
    body = {
        "mint": mint,
        "source": "dexscreener",
        "fetched_at": NOW,
        "pair_created_at": int((NOW - 3600) * 1000),
        "market_cap_usd": "1000000",
        "liquidity_usd": "250000",
        "price_usd": "1",
        "price_change_h1": "10",
    }
    body.update(over)
    return body


def _onchain(**over):
    body = {
        "source": "stalkchain_token_onchain",
        "fetched_at": NOW,
        "mint_authority_revoked": True,
        "freeze_authority_revoked": True,
        "lp_burned_or_locked": True,
        "dev_holding_pct": "3",
        "dev_rugs": {"status": "checked", "source": "stalkchain_token_deployer", "rugs": 0, "tokens_seen": 2},
        "bundled_launch_pct": "10",
    }
    body.update(over)
    return body


def _components(value="70", **over):
    body = {
        name: {"value": value, "source": "holder_reader", "fetched_at": NOW}
        for name in memecoin_desk.COMPONENTS
    }
    body.update(over)
    return body


def _protect(case_id: str):
    pool = solana_whirlpool.reference_pool(
        solana_tx.NATIVE_MINT, solana_tx.b58encode(b"\x11" * 32),
        solana_tx.USDC_MINT, solana_tx.b58encode(b"\x12" * 32),
        sqrt_price=5531210526019943285,
    )
    quote = {"usable": True, "upper": "1", "lower": "1", "origin": "test_harness"}
    return memecoin_desk.protect(case_id, pool, quote)


def _buy(mint: str, amount: int = 1000, minimum: int = 50) -> str:
    keys = [
        phantom_transport.REQUIRED_WALLET,
        solana_tx.b58encode(b"\x11" * 32),
        solana_tx.b58encode(b"\x12" * 32),
        solana_tx.b58encode(b"\x13" * 32),
        solana_tx.b58encode(b"\x14" * 32),
        solana_tx.b58encode(b"\x15" * 32),
        solana_tx.b58encode(b"\x16" * 32),
        solana_tx.b58encode(b"\x21" * 32),
        solana_tx.b58encode(b"\x22" * 32),
        solana_tx.b58encode(b"\x23" * 32),
        solana_tx.b58encode(b"\x24" * 32),
        raydium_cpmm.WSOL,
        mint,
        raydium_cpmm.PROGRAM_ID,
    ]
    accounts = [0, 7, 8, 1, 2, 3, 4, 5, 9, 10, 11, 12, 6]
    data = raydium_cpmm.DISC + amount.to_bytes(8, "little") + minimum.to_bytes(8, "little")
    return solana_tx.encode_legacy(keys[0], keys, 7, [(13, accounts, data)], solana_tx.b58encode(b"\x44" * 32))


class MemecoinDeskTests(unittest.TestCase):
    def setUp(self):
        _tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        os.environ["APP_TOKEN"] = TOKEN
        self.addCleanup(lambda: os.environ.pop("APP_TOKEN", None))
        db.init_db()
        db.execute("UPDATE phantom_control SET value = '0' WHERE key = 'halt'")
        db.set_setting("agents_enabled", "0", "test")

    def _open(self, number: int, **pair):
        found = memecoin_desk.scout(_pair(_mint(number), **pair), now=NOW)
        self.assertTrue(found["ok"], found)
        return found["case_id"]

    def _filtered(self, number: int, **pair):
        case_id = self._open(number, **pair)
        result = memecoin_desk.filter_case(case_id, _onchain(), now=NOW)
        self.assertTrue(result["ok"], result)
        return case_id

    def _analyzed(self, number: int, **pair):
        case_id = self._filtered(number, **pair)
        result = memecoin_desk.analyze(case_id, _components(), now=NOW)
        self.assertTrue(result["ok"], result)
        return case_id

    def _filled(self, number: int):
        case_id = self._analyzed(number)
        protected = _protect(case_id)
        self.assertEqual(protected["stage"], "protected", protected)
        tx = _buy(_mint(number))
        offered = memecoin_desk.offer_fixture_route(case_id, tx, amount_in=1000, minimum_out=50, fee_usd="0.05")
        self.assertTrue(offered["ok"], offered)
        digest = solana_tx.message_bytes(tx)
        import hashlib
        filled = memecoin_desk.harness_fill(case_id, {"message_sha256": hashlib.sha256(digest).hexdigest(), "tokens_out": 50})
        self.assertEqual(filled["stage"], "filled", filled)
        return case_id

    def test_age_and_market_cap_bounds_use_market_cap_not_fdv(self):
        self.assertTrue(self._open(2, pair_created_at=int((NOW - 30 * 60) * 1000)))
        self.assertEqual(memecoin_desk.scout(_pair(_mint(3), pair_created_at=int((NOW - 30 * 60 + 1) * 1000)), now=NOW)["reason"], "age")
        self.assertTrue(self._open(4, pair_created_at=int((NOW - 72 * 3600) * 1000)))
        self.assertEqual(memecoin_desk.scout(_pair(_mint(5), pair_created_at=int((NOW - 72 * 3600 - 1) * 1000)), now=NOW)["reason"], "age")
        self.assertEqual(memecoin_desk.scout(_pair(_mint(6), pair_created_at=int(NOW - 3600)), now=NOW)["reason"], "pair_created_at")
        self.assertTrue(self._open(7, market_cap_usd="300000", liquidity_usd="75000"))
        self.assertEqual(memecoin_desk.scout(_pair(_mint(8), market_cap_usd="299999"), now=NOW)["reason"], "market_cap")
        self.assertTrue(self._open(9, market_cap_usd="20000000", liquidity_usd="5000000"))
        self.assertEqual(memecoin_desk.scout(_pair(_mint(10), market_cap_usd="20000001"), now=NOW)["reason"], "market_cap")
        missing = _pair(_mint(11))
        missing["market_cap_usd"] = None
        missing["fdv"] = "5000000"
        self.assertEqual(memecoin_desk.scout(missing, now=NOW)["reason"], "market_cap_missing")
        basis = _pair(_mint(12), market_cap_basis="fdv")
        self.assertEqual(memecoin_desk.scout(basis, now=NOW)["reason"], "market_cap_basis")
        for bad in ("0", "NaN", "Infinity", "-1"):
            self.assertEqual(memecoin_desk.scout(_pair(_mint(13), market_cap_usd=bad), now=NOW)["reason"], "market_cap")
            self.assertEqual(memecoin_desk.scout(_pair(_mint(13), liquidity_usd=bad), now=NOW)["reason"], "liquidity")
        stale = memecoin_desk.scout(_pair(_mint(14), fetched_at=NOW - 31), now=NOW)
        self.assertEqual(stale["reason"], "snapshot_stale")
        self.assertTrue(any(row["reason"] == "market_cap_missing" for row in db.query("SELECT reason FROM memecoin_log")))

    def test_liquidity_ratio_uses_the_scout_snapshot(self):
        exact = self._open(15, market_cap_usd="1000000", liquidity_usd="250000")
        self.assertTrue(memecoin_desk.filter_case(exact, _onchain(), now=NOW)["ok"])
        short = self._open(16, market_cap_usd="1000000", liquidity_usd="249999")
        refused = memecoin_desk.filter_case(short, _onchain(liquidity_usd="900000"), now=NOW)
        self.assertEqual(refused["reason"], "liquidity_below_25pct_mcap")
        self.assertEqual(memecoin_desk.view(short)["stage"], "rejected")
        dexscreener = self._open(17)
        self.assertEqual(memecoin_desk.filter_case(dexscreener, _onchain(source="dexscreener"), now=NOW)["reason"], "onchain_source")

    def test_filter_rejects_missing_or_failed_onchain_facts(self):
        self.assertEqual(memecoin_desk.filter_case(self._open(18), _onchain(mint_authority_revoked=False), now=NOW)["reason"], "mint_authority")
        self.assertEqual(memecoin_desk.filter_case(self._open(19), _onchain(freeze_authority_revoked=False), now=NOW)["reason"], "freeze_authority")
        self.assertEqual(memecoin_desk.filter_case(self._open(20), _onchain(lp_burned_or_locked=False), now=NOW)["reason"], "lp")
        self.assertEqual(memecoin_desk.filter_case(self._open(21), _onchain(dev_holding_pct="3.01"), now=NOW)["reason"], "dev_holding")
        self.assertTrue(memecoin_desk.filter_case(self._open(22), _onchain(dev_holding_pct="3"), now=NOW)["ok"])
        self.assertEqual(memecoin_desk.filter_case(self._open(23), _onchain(dev_rugs={"status": "unchecked"}), now=NOW)["reason"], "dev_rug_history_unproven")
        rugs = _onchain()
        rugs["dev_rugs"] = {"status": "checked", "source": "stalkchain_token_deployer", "rugs": 1, "tokens_seen": 4}
        self.assertEqual(memecoin_desk.filter_case(self._open(24), rugs, now=NOW)["reason"], "dev_rug_history")
        self.assertEqual(memecoin_desk.filter_case(self._open(25), _onchain(bundled_launch_pct="10.01"), now=NOW)["reason"], "bundle")
        self.assertTrue(memecoin_desk.filter_case(self._open(26), _onchain(bundled_launch_pct="10"), now=NOW)["ok"])
        pumped = self._open(27, price_change_h1="80")
        self.assertEqual(memecoin_desk.filter_case(pumped, _onchain(), now=NOW)["reason"], "already_up_1h")
        self.assertTrue(memecoin_desk.filter_case(self._open(28, price_change_h1="79"), _onchain(), now=NOW)["ok"])

    def test_score_boundaries_and_missing_provenance(self):
        low = self._filtered(29)
        scored = memecoin_desk.analyze(low, _components(momentum={"value": "65", "source": "holder_reader", "fetched_at": NOW}), now=NOW)
        self.assertEqual(scored["reason"], "score")
        evidence = db.query("SELECT evidence_json FROM memecoin_log WHERE case_id = ? AND reason = 'score'", (low,))[0]["evidence_json"]
        self.assertIn("69", evidence)
        self.assertIn("engineering_default", evidence)
        passed = memecoin_desk.analyze(self._filtered(30), _components(), now=NOW)
        self.assertTrue(passed["ok"], passed)
        self.assertEqual(Decimal(memecoin_desk.view(passed["case_id"])["snapshot"]["score"]), Decimal("70"))
        incomplete = _components()
        del incomplete["unique_buyers"]
        self.assertEqual(memecoin_desk.analyze(self._filtered(31), incomplete, now=NOW)["reason"], "unique_buyers_missing")
        stale = _components()
        stale["holder_growth"] = {"value": "70", "source": "holder_reader", "fetched_at": NOW - 31}
        self.assertEqual(memecoin_desk.analyze(self._filtered(32), stale, now=NOW)["reason"], "holder_growth_stale")
        counts = _components()
        counts["unique_buyers"] = {"value": "70", "source": "dexscreener_txns", "fetched_at": NOW}
        self.assertEqual(memecoin_desk.analyze(self._filtered(33), counts, now=NOW)["reason"], "unique_buyers_unproven")
        boost = _components()
        boost["smart_wallets"] = {"value": "70", "source": "dexscreener_boost", "fetched_at": NOW}
        self.assertEqual(memecoin_desk.analyze(self._filtered(34), boost, now=NOW)["reason"], "smart_wallets_unproven")
        skipped = self._open(35)
        self.assertEqual(memecoin_desk.analyze(skipped, _components(), now=NOW)["reason"], "stage")
        self.assertEqual(memecoin_desk.view(skipped)["stage"], "scout_passed")

    def test_stop_and_trail_are_persisted_before_entry_and_are_not_a_profit_lock(self):
        case_id = self._analyzed(36)
        early = memecoin_desk.offer_fixture_route(case_id, _buy(_mint(36)), amount_in=1000, minimum_out=50, fee_usd="0.05")
        self.assertEqual(early["reason"], "stage")
        protected = _protect(case_id)
        self.assertEqual(protected["stage"], "protected")
        snap = memecoin_desk.view(case_id)["snapshot"]
        self.assertEqual(Decimal(snap["stop_price_usd"]), Decimal("0.94"))
        self.assertEqual(Decimal(snap["trail_arm_price_usd"]), Decimal("1.12"))
        self.assertEqual(Decimal(snap["trail_floor_if_armed_at_threshold"]), Decimal("0.9856"))
        self.assertIs(snap["trail_breakeven_guaranteed"], False)
        self.assertFalse(snap["send"])
        self.assertFalse(snap["live_ready"])
        priced = memecoin_desk.offer_fixture_route(case_id, _buy(_mint(36)), amount_in=1000, minimum_out=50, fee_usd="0.0500001")
        self.assertEqual(priced["reason"], "fee_cap")
        case_id = self._analyzed(37)
        _protect(case_id)
        missing = memecoin_desk.offer_fixture_route(case_id, _buy(_mint(37)), amount_in=1000, minimum_out=50, fee_usd="NaN")
        self.assertEqual(missing["reason"], "fee_unpriced")
        case_id = self._filled(38)
        held = memecoin_desk.mark(case_id, "1.119", source="fixture_price", fetched_at=NOW, now=NOW)
        self.assertEqual(held["action"], "hold")
        self.assertFalse(memecoin_desk.view(case_id)["snapshot"]["trail_armed"])
        armed = memecoin_desk.mark(case_id, "1.12", source="fixture_price", fetched_at=NOW, now=NOW)
        self.assertEqual(armed["action"], "hold")
        self.assertTrue(memecoin_desk.view(case_id)["snapshot"]["trail_armed"])
        exited = memecoin_desk.mark(case_id, "0.9856", source="fixture_price", fetched_at=NOW, now=NOW)
        self.assertEqual(exited["action"], "trail")
        closed = memecoin_desk.view(case_id)["snapshot"]
        self.assertLess(Decimal(closed["realized_pnl"]), 0)
        self.assertFalse(closed["trail_breakeven_guaranteed"])
        self.assertEqual(db.query("SELECT intent_id FROM phantom_reservations"), [])

    def test_stop_exit_still_runs_when_loss_is_large_and_halt_blocks(self):
        live = self._filled(44)
        db.execute("UPDATE phantom_control SET value = '1' WHERE key = 'halt'")
        halted = memecoin_desk.mark(live, "0.50", source="fixture_price", fetched_at=NOW, now=NOW)
        self.assertEqual(halted["reason"], "kill_switch")
        self.assertEqual(memecoin_desk.view(live)["stage"], "filled")
        db.execute("UPDATE phantom_control SET value = '0' WHERE key = 'halt'")
        for number in (40, 41, 42):
            case_id = self._filled(number)
            stopped = memecoin_desk.mark(case_id, "0.01", source="fixture_price", fetched_at=NOW, now=NOW)
            self.assertEqual(stopped["action"], "stop")
        blocked = self._analyzed(43)
        self.assertEqual(_protect(blocked)["reason"], "stop_new_entries")
        stopped = memecoin_desk.mark(live, "0.94", source="fixture_price", fetched_at=NOW, now=NOW)
        self.assertEqual(stopped["action"], "stop")
        self.assertTrue(memecoin_desk.view(live)["snapshot"]["exit"]["fee_counted_once"])

    def test_allocation_restart_and_one_winner(self):
        for number in range(50, 61):
            self.assertEqual(_protect(self._analyzed(number))["stage"], "protected")
        left = self._analyzed(61)
        right = self._analyzed(62)
        results = []

        def race(case_id):
            results.append(_protect(case_id))

        threads = [threading.Thread(target=race, args=(case_id,)) for case_id in (left, right)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        reasons = [result["reason"] for result in results]
        self.assertCountEqual(reasons, [None, "allocation"])
        raced = []

        def scout_once():
            raced.append(memecoin_desk.scout(_pair(_mint(70)), now=NOW))

        threads = [threading.Thread(target=scout_once) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertCountEqual([item["reason"] for item in raced], [None, "already_open"])

    def test_a_failed_log_does_not_leave_a_pass(self):
        original = memecoin_desk._log
        def fail(conn, case_id, mint, stage, verdict, reason, source, evidence, moment):
            raise RuntimeError("log_failed")
        memecoin_desk._log = fail
        try:
            with self.assertRaises(RuntimeError):
                memecoin_desk.scout(_pair(_mint(90)), now=NOW)
        finally:
            memecoin_desk._log = original
        self.assertIsNone(memecoin_desk.view(_mint(90)))

    def test_exit_pnl_counts_the_entry_fee_and_the_exit_fee_once_each(self):
        case_id = self._filled(91)
        snap = memecoin_desk.view(case_id)["snapshot"]
        snap["fill"]["fee_usd"] = "0.02"
        snap["route"]["fee_usd"] = "0.03"
        db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = ?", (json.dumps(snap), case_id))
        closed = memecoin_desk.mark(case_id, "0.94", source="fixture_price", fetched_at=NOW, now=NOW)
        self.assertEqual(closed["action"], "stop")
        body = memecoin_desk.view(case_id)["snapshot"]["exit"]
        notional = Decimal(memecoin_desk.view(case_id)["snapshot"]["notional_usd"])
        proceeds = notional * Decimal("0.94")
        self.assertEqual(Decimal(body["realized_pnl"]), proceeds - notional - Decimal("0.02") - Decimal("0.03"))
        self.assertTrue(body["fee_counted_once"])

    def test_a_restart_keeps_the_fill_and_does_not_send_again(self):
        kept = self._filled(63)
        db.init_db()
        self.assertEqual(memecoin_desk.view(kept)["stage"], "filled")
        self.assertTrue(memecoin_desk.log_for(kept))
        self.assertFalse(memecoin_desk.view(kept)["snapshot"]["send"])
        again = memecoin_desk.harness_fill(kept, {"message_sha256": "nope", "tokens_out": 50})
        self.assertEqual(again["reason"], "stage")
        self.assertEqual(db.query("SELECT intent_id FROM phantom_reservations"), [])

    def test_decoder_rejects_a_different_program_and_http_cannot_skip(self):
        checked = raydium_cpmm.inspect_buy(_buy(_mint(80)), payer=phantom_transport.REQUIRED_WALLET, output_mint=_mint(80), amount_in=1000, minimum_out=50)
        self.assertTrue(checked["ok"], checked)
        self.assertFalse(checked["send"])
        self.assertFalse(checked["live_ready"])
        wrong = raydium_cpmm.inspect_buy(_buy(_mint(80)), payer=phantom_transport.REQUIRED_WALLET, output_mint=_mint(80), amount_in=1001, minimum_out=50)
        self.assertEqual(wrong["reason"], "amount")
        self.assertEqual(token_execution.supported_buy(_mint(80))["reason"], "message_required")
        self.assertEqual(token_execution.supported_buy(_mint(80))["decoder"], "jupiter_route_v2_whirlpool")
        self.assertEqual(token_execution.supported_buy(_mint(80))["jupiter_route_v2"], "message_required")
        import server
        client = TestClient(server.app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        forged = client.post("/api/memecoin/scout", headers=AUTH, json=_pair(_mint(81)) | {"passed": True})
        self.assertEqual(forged.status_code, 400)
        routed = client.post("/api/memecoin/protect", headers=AUTH, json={"case_id": "nope", "transaction": _buy(_mint(81))})
        self.assertEqual(routed.status_code, 400)
        moment = time.time()
        opened = client.post("/api/memecoin/scout", headers=AUTH, json=_pair(_mint(81), fetched_at=moment, pair_created_at=int((moment - 3600) * 1000)))
        self.assertEqual(opened.status_code, 200, opened.text)
        self.assertEqual(opened.json()["stage"], "scout_passed")
        self.assertEqual(db.get_setting("agents_enabled"), "0")
        body = client.get("/api/memecoin/" + opened.json()["case_id"], headers=AUTH).json()
        self.assertFalse(body["live_ready"])
        self.assertFalse(body["send"])
