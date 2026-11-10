"""Documented public-source parsers. The example is the published schema, not a live body."""
import unittest

import desk_loop
import public_filter_docs

MINT = "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN"
DOCUMENTED = {
    "holders": [
        {"holders": 1000, "time": 1700000000},
        {"holders": 1100, "time": 1700086400},
    ]
}


class PublicFilterDocTests(unittest.TestCase):
    def test_only_the_holder_chart_is_implemented(self):
        catalog = public_filter_docs.source_catalog()
        self.assertTrue(catalog["lp_lock"]["implemented"])
        self.assertFalse(catalog["lp_lock"]["field_is_expiry"])
        self.assertEqual(catalog["lp_lock"]["pass_value"], 100)
        self.assertTrue(catalog["rug_history"]["source_wired"])
        self.assertIsNone(catalog["rug_history"]["documented_field"])
        self.assertFalse(catalog["birdeye_holder_chart"]["implemented"])
        self.assertFalse(catalog["helius_creator_assets"]["implemented"])
        self.assertTrue(catalog["holder_growth"]["viable"])
        self.assertIn("SOLANA_TRACKER_API_KEY", catalog["holder_growth"]["api_key"])

    def test_two_documented_points_are_a_series_and_not_a_score(self):
        parsed = public_filter_docs.parse_holder_chart(DOCUMENTED, MINT)
        self.assertTrue(parsed["ok"], parsed)
        self.assertEqual(parsed["delta"], 100)
        self.assertEqual(parsed["metric"], "holders")
        self.assertFalse(parsed["score_measured"])
        self.assertFalse(parsed["live_ready"])
        self.assertFalse(parsed["fetched"])
        self.assertTrue(parsed["api_key_required"])

    def test_a_short_or_bad_chart_fails_closed(self):
        self.assertFalse(public_filter_docs.parse_holder_chart({"holders": [{"holders": 1, "time": 1}]}, MINT)["ok"])
        self.assertFalse(public_filter_docs.parse_holder_chart({"count": 2}, MINT)["ok"])
        self.assertEqual(
            public_filter_docs.parse_holder_chart({"holders": [{"holders": 2, "time": 5}, {"holders": 3, "time": 5}]}, MINT)["reason"],
            "time_not_increasing",
        )
        self.assertFalse(public_filter_docs.parse_holder_chart(DOCUMENTED, "")["ok"])
        self.assertTrue(public_filter_docs.parse_lp_locked_pct({"lpLockedPct": 100})["ok"])
        self.assertFalse(public_filter_docs.parse_lp_locked_pct({"lpLockedPct": 99})["ok"])
        self.assertFalse(public_filter_docs.parse_lp_locked_pct({"lpLockedPct": "100"})["ok"])
        self.assertFalse(public_filter_docs.parse_lp_locked_pct({})["expiry_checked"])

    def test_a_chart_is_not_booked_and_a_bad_chart_stays_unproven(self):
        accepted = desk_loop.collect_holder_count(MINT, {"source": "solana_tracker_holders_chart", "body": DOCUMENTED})
        self.assertTrue(accepted["ok"])
        self.assertFalse(accepted["written"])
        self.assertFalse(accepted["booked"])
        self.assertFalse(accepted["score_measured"])
        refused = desk_loop.collect_holder_count(MINT, {"source": "solana_tracker_holders_chart", "body": {"holders": []}})
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["reason"], "holder_growth_unknown")
        self.assertFalse(refused["written"])
