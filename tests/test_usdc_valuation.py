"""Pyth USDC/USD fields. No network and no invented depeg band."""
import time
import unittest
from decimal import Decimal

import db
import spend_policy
import usdc_valuation
from tests.support import temp_desk_env

FEED = usdc_valuation.FEED_ID


def _quote(**overrides):
    body = {"price": 99_990_000, "conf": 10_000, "expo": -8, "publish_time": int(time.time()), "feed_id": FEED}
    body.update(overrides)
    return body


class ValuationTests(unittest.TestCase):
    def setUp(self):
        _tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        db.init_db()
        self.addCleanup(lambda: usdc_valuation.set_quote(None))

    def test_disabled_quote_is_not_usd_and_has_no_band(self):
        usdc_valuation.set_quote(_quote())
        body = spend_policy.usd_source()
        self.assertFalse(body["approved"])
        self.assertEqual(body["reason"], "usd_source_unapproved")
        self.assertIsNone(body["accepted_band"])
        self.assertFalse(body["pool_micro_usdc_is_usd"])

    def test_published_confidence_enforces_the_existing_limits(self):
        usdc_valuation.enable()
        now = time.time()
        usable = usdc_valuation.assess(_quote(), now)
        self.assertTrue(usable["usable"])
        self.assertEqual(Decimal(usable["upper"]), Decimal("1"))
        self.assertEqual(Decimal(usable["lower"]), Decimal("0.9998"))
        self.assertIsNone(usable["accepted_band"])
        usdc_valuation.set_quote(_quote())
        limits = usdc_valuation.within_limits(4_541, 8_000_000, 8_134_000, spend_policy.usd_source())
        self.assertTrue(limits["ok"])
        self.assertEqual(limits["fee_usd"], "0.01")
        self.assertEqual(limits["ticket_usd"], "8.00")
        wide = usdc_valuation.assess(_quote(price=100_000_000, conf=5_000_000), now)
        self.assertEqual(Decimal(wide["upper"]), Decimal("1.05"))
        self.assertFalse(usdc_valuation.within_limits(8_000_000, 8_000_000, 8_000_000, wide)["ticket_ok"])
        crossed = usdc_valuation.assess(_quote(price=10_000, conf=10_000), now)
        self.assertEqual(crossed["reason"], "valuation_not_positive")
        stale = usdc_valuation.assess(_quote(publish_time=int(now) - 120), now)
        self.assertEqual(stale["reason"], "valuation_stale")

    def test_loss_prices_cost_high_and_proceeds_low(self):
        usdc_valuation.enable()
        usdc_valuation.set_quote(_quote())
        loss = usdc_valuation.conservative_loss(8_000_000, 7_000_000, spend_policy.usd_source())
        self.assertEqual(loss["spent_usd"], "8.00")
        self.assertEqual(loss["proceeds_usd"], "6.99")
        self.assertEqual(Decimal(loss["loss_usd"]), Decimal("1.01"))
