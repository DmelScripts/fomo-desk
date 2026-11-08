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

    def test_the_code_target_is_50_bps_and_is_not_deployed(self):
        body = owner_slippage.configuration()
        self.assertEqual(body["bps"], 50)
        self.assertEqual(body["percent"], "0.5")
        self.assertEqual(body["statement"], "Set maximum slippage to 0.5% per swap.")
        self.assertEqual(body["approved_at"], "2026-11-08T16:28:00+07:00")
        self.assertEqual(body["timezone"], "Asia/Saigon")
        self.assertEqual(body["applies_to"], ["entry", "exit"])
        self.assertFalse(body["wider_for_exit"])
        self.assertFalse(body["wider_for_retry"])
        self.assertFalse(body["approved"])
        self.assertFalse(body["deployed"])
        self.assertFalse(body["production"])
        self.assertFalse(body["live"])
        self.assertFalse(body["send"])
        self.assertFalse(body["sign"])
        status = desk_loop.scheduler_status()
        self.assertIn("owner_slippage_unapproved", status["ready_because"])
        self.assertIn("lp_fields_unpublished", status["ready_because"])
        self.assertIn("rug_history_unpublished", status["ready_because"])
        self.assertIn("holder_growth_unproven", status["ready_because"])
        self.assertFalse(status["ready"])
        self.assertFalse(status["live_ready"])
        self.assertFalse(status["armed"])

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
        self.assertIn("stalkchain_response_fields_for_lp_rug_growth_are_unpublished", blocked)
        self.assertFalse(desk_loop.readiness()["live_ready"])

    def test_a_caller_or_old_value_does_not_raise_the_ceiling(self):
        widened = dict(owner_slippage.target())
        widened["bps"] = 100
        db.set_setting(owner_slippage.KEY, json.dumps(widened), "caller")
        self.assertIsNone(owner_slippage.ceiling_bps())
        db.set_setting(owner_slippage.KEY, "50", "caller")
        self.assertIsNone(owner_slippage.ceiling_bps())
        self.assertIn("owner_slippage_unapproved", desk_loop.scheduler_status()["ready_because"])
        values, data = _pool(solana_tx.NATIVE_MINT, TOKEN)
        claimed = _decode(
            _tx(_metas(solana_tx.NATIVE_MINT, TOKEN, values), _route(999_899, 51)),
            data, allowance=5_000,
        )
        self.assertEqual(claimed["reason"], "slippage_exceeds")
        self.assertEqual(claimed["owner_ceiling_bps"], 50)
        self.assertFalse(claimed["owner_slippage_approved"])
        self.assertEqual(owner_slippage.effective_test_allowance(5_000), 50)
        owner_slippage.persist()
        still = _decode(
            _tx(_metas(solana_tx.NATIVE_MINT, TOKEN, values), _route(999_899, 51)),
            data, allowance=5_000,
        )
        self.assertEqual(still["reason"], "slippage_exceeds")
        self.assertTrue(still["owner_slippage_approved"])
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
        self.assertTrue(tight["ok"], tight)
        self.assertEqual(tight["slippage_bps"], 50)
        retry = _decode(
            _tx(_metas(solana_tx.NATIVE_MINT, TOKEN, values), _route(999_899, 80)),
            data, allowance=80,
        )
        self.assertEqual(retry["reason"], "slippage_exceeds")
        self.assertEqual(jupiter_route_v2.decode(
            _metas(solana_tx.NATIVE_MINT, TOKEN, values), _route(999_899, 50),
            side="buy", mint=TOKEN, account_data=data, compute=[], slippage_allowance_bps=50,
        )["reason"], "message_unbound")
