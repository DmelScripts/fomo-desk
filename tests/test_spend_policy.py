"""The 21:37 widget values are the only match. This file does not approve production."""
import json
import time
import unittest

import db
import spend_policy
from tests.support import temp_desk_env


class SpendPolicyTests(unittest.TestCase):
    def setUp(self):
        _tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        db.init_db()

    def test_target_is_the_widget_and_nothing_is_recorded(self):
        body = spend_policy.target()
        self.assertEqual(body["max_fee_usd_per_tx"], "0.05")
        self.assertEqual(body["stop_new_entries_cumulative_loss_usd"], "20.00")
        self.assertEqual(body["allocation_usd"], "100.00")
        self.assertTrue(body["fee_includes_dex_and_network"])
        self.assertTrue(body["refundable_deposits_excluded_from_fee"])
        self.assertTrue(body["deposits_count_as_exposure"])
        self.assertTrue(body["loss_includes_fees"])
        self.assertFalse(body["daily_reset"])
        self.assertFalse(body["refill"])
        self.assertFalse(body["guaranteed_loss_limit"])
        self.assertFalse(body["recorded"])
        self.assertFalse(body["approved"])
        self.assertFalse(spend_policy.fee_view()["approved"])
        self.assertFalse(spend_policy.aggregate_view()["approved"])
        self.assertEqual(spend_policy.fee_view()["specified_max_fee_usd"], "0.05")
        self.assertEqual(spend_policy.aggregate_view()["specified_max_loss_usd"], "20.00")

    def test_drafts_and_wrong_amounts_do_not_approve(self):
        self.assertFalse(spend_policy.store_fee_ceiling(0)["approved"])
        self.assertFalse(spend_policy.store_aggregate_loss("20.00")["approved"])
        self.assertFalse(spend_policy.store_aggregate_loss("6.00")["approved"])
        self._put(spend_policy._FEE, {
            "owner_approved": True,
            "kind": "fee_policy",
            "source": spend_policy._SOURCE,
            "max_fee_usd": "0.06",
            "includes_dex_fee": True,
            "includes_network_fee": True,
            "excludes_refundable_deposits": True,
            "deposits_count_as_exposure": True,
            "fx_slot": 466547240,
        })
        self._put(spend_policy._AGGREGATE, {
            "owner_approved": True,
            "kind": "aggregate_loss_policy",
            "source": spend_policy._SOURCE,
            "max_loss_usd": "20.00",
            "includes_fees": True,
            "daily_reset": True,
            "refill": False,
            "guaranteed_loss_limit": False,
            "stops_new_entries": True,
        })
        self.assertFalse(spend_policy.fee_view()["approved"])
        self.assertFalse(spend_policy.aggregate_view()["approved"])

    def test_exact_target_row_matches_and_is_not_written_by_the_module(self):
        self.assertIsNone(spend_policy._read(spend_policy._FEE))
        self._put(spend_policy._FEE, {
            "owner_approved": True,
            "kind": "fee_policy",
            "source": spend_policy._SOURCE,
            "max_fee_usd": "0.05",
            "includes_dex_fee": True,
            "includes_network_fee": True,
            "excludes_refundable_deposits": True,
            "deposits_count_as_exposure": True,
            "fx_slot": 466547240,
        })
        self._put(spend_policy._AGGREGATE, {
            "owner_approved": True,
            "kind": "aggregate_loss_policy",
            "source": spend_policy._SOURCE,
            "max_loss_usd": "20.00",
            "includes_fees": True,
            "daily_reset": False,
            "refill": False,
            "guaranteed_loss_limit": False,
            "stops_new_entries": True,
        })
        self.assertTrue(spend_policy.fee_view()["approved"])
        self.assertTrue(spend_policy.aggregate_view()["approved"])
        self.assertFalse(spend_policy.target()["recorded"])

    def _put(self, key, body):
        db.execute(
            """
            INSERT INTO phantom_control (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, json.dumps(body), time.time()),
        )
