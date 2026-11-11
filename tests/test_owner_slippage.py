"""The 16:28 Asia/Saigon decision is 50 basis points per swap. It does not start the desk."""
import json
import unittest

import db
import desk_loop
import jupiter_route_v2
import owner_slippage
import solana_tx
from tests.support import temp_desk_env
from tests.test_jupiter_route_v2 import TOKEN, _decode, _metas, _pool, _route, _tx


class OwnerSlippageTests(unittest.TestCase):
    def setUp(self):
        _tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        db.init_db()

    def test_init_db_installs_the_record_and_a_second_boot_does_not_rewrite_it(self):
        body = owner_slippage.configuration()
        self.assertEqual(body["code_target_bps"], 50)
        self.assertEqual(body["code_target_percent"], "0.5")
        self.assertEqual(body["statement"], "Set maximum slippage to 0.5% per swap.")
        self.assertEqual(body["approved_at"], "2026-11-08T16:28:00+07:00")
        self.assertEqual(body["record_state"], "already_applied")
        self.assertEqual(body["applied_bps"], 50)
        self.assertTrue(body["applied"])
        self.assertFalse(body["deployed"])
        self.assertFalse(body["production"])
        self.assertFalse(body["live"])
        self.assertFalse(body["live_ready"])
        self.assertFalse(body["send"])
        self.assertFalse(body["sign"])
        first = db.get_setting(owner_slippage.KEY)
        written = db.query("SELECT COUNT(*) AS n FROM change_log WHERE key = ?", (owner_slippage.KEY,))[0]["n"]
        self.assertEqual(written, 1)
        db.init_db()
        self.assertEqual(db.get_setting(owner_slippage.KEY), first)
        self.assertEqual(
            db.query("SELECT COUNT(*) AS n FROM change_log WHERE key = ?", (owner_slippage.KEY,))[0]["n"],
            1,
        )
        self.assertEqual(owner_slippage.configuration()["startup_action"], "unchanged")
        status = desk_loop.scheduler_status()
        self.assertNotIn("owner_slippage_unapproved", status["ready_because"])
        self.assertIn("lp_fields_unpublished", status["ready_because"])
        self.assertIn("rug_history_unpublished", status["ready_because"])
        self.assertIn("holder_growth_unproven", status["ready_because"])
        self.assertFalse(status["ready"])
        self.assertFalse(status["live_ready"])
        self.assertFalse(status["armed"])
        self.assertFalse(status["send"])
        self.assertFalse(status["sign"])
        policy = desk_loop.readiness()["slippage_policy"]
        self.assertEqual(policy["code_target_bps"], 50)
        self.assertEqual(policy["applied_bps"], 50)
        self.assertEqual(policy["record_state"], "already_applied")
        self.assertTrue(policy["approved"])
        self.assertFalse(policy["deployed"])
        self.assertFalse(policy["live_ready"])
        self.assertFalse(policy["send"])
        self.assertFalse(policy["sign"])

    def test_a_revoked_or_conflicting_row_survives_startup(self):
        revoked = dict(owner_slippage.target())
        revoked["record_state"] = "revoked"
        revoked["revoked_at"] = "2026-11-08T18:00:00+07:00"
        db.set_setting(owner_slippage.KEY, json.dumps(revoked, sort_keys=True), "owner_revocation")
        db.init_db()
        self.assertEqual(owner_slippage.configuration()["record_state"], "revoked")
        self.assertIsNone(owner_slippage.ceiling_bps())
        self.assertIn("owner_slippage_unapproved", desk_loop.scheduler_status()["ready_because"])
        self.assertEqual(json.loads(db.get_setting(owner_slippage.KEY))["record_state"], "revoked")
        conflict = dict(owner_slippage.target())
        conflict["bps"] = 100
        db.set_setting(owner_slippage.KEY, json.dumps(conflict, sort_keys=True), "old_config")
        db.init_db()
        self.assertEqual(owner_slippage.configuration()["record_state"], "conflict")
        self.assertEqual(owner_slippage.configuration()["stored_bps"], 100)
        self.assertIsNone(owner_slippage.configuration()["applied_bps"])
        self.assertFalse(owner_slippage.configuration()["approved"])
        owner_slippage.persist()
        self.assertEqual(json.loads(db.get_setting(owner_slippage.KEY))["bps"], 100)
        conn = db.connect()
        try:
            conn.execute("DELETE FROM settings WHERE key = ?", (owner_slippage.KEY,))
            conn.commit()
        finally:
            conn.close()
        db.init_db()
        self.assertEqual(owner_slippage.configuration()["record_state"], "absent_after_migration")
        self.assertIsNone(owner_slippage.ceiling_bps())
        blocked = jupiter_route_v2.quote_request(999_899)
        self.assertFalse(blocked["ok"])
        self.assertFalse(blocked["approved"])
        self.assertIsNone(blocked["slippage_bps"])
        self.assertEqual(blocked["reason"], "owner_slippage_unapproved")
        import importlib
        import public_market
        importlib.reload(public_market)
        db.init_db()
        self.assertIsNone(db.get_setting(owner_slippage.KEY, None))
        self.assertEqual(jupiter_route_v2.quote_request(999_899)["reason"], "owner_slippage_unapproved")
        self.assertIsNone(db.get_setting(owner_slippage.KEY, None))
        self.assertIn("owner_slippage_unapproved", desk_loop.scheduler_status()["ready_because"])

    def test_lifespan_boots_the_same_policy_and_a_second_start_does_not_rewrite_it(self):
        import asyncio
        import os
        os.environ["DESK_PRICE_LOOP"] = "0"
        self.addCleanup(lambda: os.environ.pop("DESK_PRICE_LOOP", None))
        from server import app, lifespan

        async def boot():
            async with lifespan(app):
                return owner_slippage.configuration(), db.get_setting(owner_slippage.KEY)

        first, raw = asyncio.run(boot())
        self.assertTrue(first["applied"])
        self.assertEqual(first["applied_bps"], 50)
        self.assertFalse(first["deployed"])
        second, again = asyncio.run(boot())
        self.assertEqual(again, raw)
        self.assertEqual(second["startup_action"], "unchanged")
        self.assertFalse(second["live_ready"])
        self.assertFalse(second["send"])

    def test_the_minimum_rounds_up_to_the_50_bps_ceiling(self):
        self.assertEqual(owner_slippage.minimum_units(10001, 50), 9951)
        self.assertFalse(owner_slippage.check_route(50, 9950, 10001)["ok"])
        self.assertEqual(owner_slippage.check_route(50, 9950, 10001)["reason"], "min_below_amm")
        self.assertTrue(owner_slippage.check_route(50, 9951, 10001)["ok"])
        self.assertTrue(owner_slippage.check_route(0, 10001, 10001)["ok"])
        self.assertFalse(owner_slippage.check_route(0, 9950, 10001)["ok"])
        self.assertTrue(owner_slippage.check_route(50, 9950, 10000)["ok"])
        self.assertFalse(owner_slippage.check_route(50, 9949, 10000)["ok"])
        self.assertEqual(owner_slippage.check_route(51, 10001, 10001)["reason"], "slippage_exceeds")
        self.assertEqual(owner_slippage.minimum_units(1, 50), 1)
        self.assertFalse(owner_slippage.check_route(50, 0, 1)["ok"])
        huge = 10**30 + 1
        required = owner_slippage.minimum_units(huge, 50)
        self.assertEqual(required, (huge * 9950 + 9999) // 10000)
        self.assertFalse(owner_slippage.check_route(10, required - 1, huge)["ok"])
        self.assertTrue(owner_slippage.check_route(10, required, huge)["ok"])
        self.assertGreaterEqual(required * 10_000, huge * 9950)
        exact = jupiter_route_v2.quote_request(10_000)
        self.assertEqual(exact["slippage_bps"], 50)
        self.assertEqual(exact["min_out"], 9950)
        self.assertEqual(exact["quoted_out_amount"], 10_000)
        nondivisible = jupiter_route_v2.quote_request(999_899)
        self.assertEqual(nondivisible["slippage_bps"], 49)
        self.assertEqual(nondivisible["quoted_out_amount"], 999_899)
        self.assertEqual(nondivisible["owner_floor"], 994_900)
        self.assertGreaterEqual(nondivisible["min_out"], 994_900)
        self.assertLess(jupiter_route_v2.minimum_out(999_899, 50), 994_900)
        self.assertFalse(owner_slippage.check_route(50, 994_899, 999_899)["ok"])
        self.assertTrue(owner_slippage.check_route(49, nondivisible["min_out"], 999_899)["ok"])
        self.assertEqual(owner_slippage.check_route(51, nondivisible["min_out"], 999_899)["reason"], "slippage_exceeds")
        asked = jupiter_route_v2.quote_request(10001)
        self.assertEqual(asked["slippage_bps"], 49)
        self.assertGreaterEqual(asked["min_out"], 9951)
        self.assertFalse(owner_slippage.check_route(50, 9950, 10001)["ok"])
        self.assertFalse(owner_slippage.check_route(49, 9950, 10001)["ok"])
        tiny = jupiter_route_v2.quote_request(1)
        self.assertEqual(tiny["slippage_bps"], 0)
        self.assertEqual(tiny["min_out"], 1)
        self.assertIsNone(jupiter_route_v2.minimum_out(1, 50))
        only_zero = jupiter_route_v2.quote_request(2)
        self.assertEqual(only_zero["slippage_bps"], 0)
        self.assertEqual(only_zero["min_out"], 2)
        self.assertFalse(owner_slippage.check_route(1, 1, 2)["ok"])
        huge_request = jupiter_route_v2.quote_request(huge)
        self.assertLessEqual(huge_request["slippage_bps"], 50)
        self.assertEqual(huge_request["quoted_out_amount"], huge)
        self.assertGreaterEqual(huge_request["min_out"] * 10_000, huge * 9950)
        self.assertLess(jupiter_route_v2.minimum_out(huge, 50), huge_request["owner_floor"])
        self.assertFalse(owner_slippage.check_route(50, jupiter_route_v2.minimum_out(huge, 50), huge)["ok"])
        self.assertTrue(owner_slippage.check_route(huge_request["slippage_bps"], huge_request["min_out"], huge)["ok"])
        self.assertFalse(huge_request["idl_has_explicit_minimum"])
        self.assertEqual(huge_request["network"], "not_called")

    def test_persisting_the_owner_record_clears_only_the_slippage_blocker(self):
        stored = owner_slippage.persist()
        self.assertEqual(stored["approved_bps"], 50)
        self.assertTrue(stored["persisted"])
        self.assertFalse(stored["deployed"])
        reread = owner_slippage.configuration()
        self.assertEqual(reread["approved_bps"], 50)
        self.assertEqual(reread["source"], owner_slippage.SOURCE)
        status = desk_loop.scheduler_status()
        self.assertNotIn("owner_slippage_unapproved", status["ready_because"])
        self.assertIn("lp_fields_unpublished", status["ready_because"])
        self.assertIn("rug_history_unpublished", status["ready_because"])
        self.assertIn("holder_growth_unproven", status["ready_because"])
        self.assertFalse(status["ready"])
        self.assertFalse(status["live_ready"])
        self.assertFalse(status["send"])
        self.assertFalse(status["sign"])
        blocked = desk_loop.readiness()["blocked_because"]
        self.assertNotIn("owner_slippage_unapproved", blocked)
        self.assertNotIn("stalkchain_response_fields_for_lp_rug_growth_are_unpublished", blocked)
        self.assertFalse(desk_loop.readiness()["live_ready"])

    def test_a_caller_or_old_value_does_not_raise_the_ceiling(self):
        widened = dict(owner_slippage.target())
        widened["bps"] = 100
        values, data = _pool(solana_tx.NATIVE_MINT, TOKEN)
        installed = _decode(
            _tx(_metas(solana_tx.NATIVE_MINT, TOKEN, values), _route(999_899, 51)),
            data, allowance=5_000,
        )
        self.assertEqual(installed["reason"], "slippage_exceeds")
        self.assertTrue(installed["owner_slippage_approved"])
        db.set_setting(owner_slippage.KEY, json.dumps(widened), "caller")
        db.init_db()
        owner_slippage.persist()
        self.assertIsNone(owner_slippage.ceiling_bps())
        self.assertEqual(json.loads(db.get_setting(owner_slippage.KEY))["bps"], 100)
        db.set_setting(owner_slippage.KEY, "50", "caller")
        db.init_db()
        self.assertIsNone(owner_slippage.ceiling_bps())
        self.assertEqual(db.get_setting(owner_slippage.KEY), "50")
        self.assertIn("owner_slippage_unapproved", desk_loop.scheduler_status()["ready_because"])
        claimed = _decode(
            _tx(_metas(solana_tx.NATIVE_MINT, TOKEN, values), _route(999_899, 51)),
            data, allowance=5_000,
        )
        self.assertEqual(claimed["reason"], "slippage_exceeds")
        self.assertEqual(claimed["owner_ceiling_bps"], 50)
        self.assertFalse(claimed["owner_slippage_approved"])
        self.assertEqual(owner_slippage.effective_test_allowance(5_000), 50)
        still = _decode(
            _tx(_metas(solana_tx.NATIVE_MINT, TOKEN, values), _route(999_899, 51)),
            data, allowance=5_000,
        )
        self.assertEqual(still["reason"], "slippage_exceeds")
        self.assertFalse(still["owner_slippage_approved"])
        values_out, data_out = _pool(TOKEN, solana_tx.NATIVE_MINT)
        exit_wide = _decode(
            _tx(_metas(TOKEN, solana_tx.NATIVE_MINT, values_out), _route(999_899, 51), side="exit"),
            data_out, side="exit", allowance=51,
        )
        self.assertEqual(exit_wide["reason"], "slippage_exceeds")
        tight = _decode(
            _tx(_metas(solana_tx.NATIVE_MINT, TOKEN, values), _route(999_899, 50)),
            data, allowance=50,
        )
        self.assertEqual(tight["reason"], "min_below_amm", tight)
        retry = _decode(
            _tx(_metas(solana_tx.NATIVE_MINT, TOKEN, values), _route(999_899, 80)),
            data, allowance=80,
        )
        self.assertEqual(retry["reason"], "slippage_exceeds")
        self.assertEqual(jupiter_route_v2.decode(
            _metas(solana_tx.NATIVE_MINT, TOKEN, values), _route(999_899, 50),
            side="buy", mint=TOKEN, account_data=data, compute=[], slippage_allowance_bps=50,
        )["reason"], "message_unbound")
