"""STEP 11 fee boundaries. Discount is 10% off, gas is 0."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fees import fee_usd, gross_fee_usd


class TestFees(unittest.TestCase):
    def test_boundaries(self):
        self.assertAlmostEqual(gross_fee_usd(5), 0.10)
        self.assertAlmostEqual(gross_fee_usd(47.50), 0.95)
        self.assertAlmostEqual(gross_fee_usd(100), 0.95)
        self.assertAlmostEqual(gross_fee_usd(190), 0.95)
        self.assertAlmostEqual(gross_fee_usd(200), 1.00)
        # 10% off each of those.
        self.assertAlmostEqual(fee_usd(5, 0.10), 0.09)
        self.assertAlmostEqual(fee_usd(47.50, 0.10), 0.855)
        self.assertAlmostEqual(fee_usd(190, 0.10), 0.855)
        self.assertAlmostEqual(fee_usd(200, 0.10), 0.90)
        self.assertEqual(gross_fee_usd(0), 0.0)
        # Just below, on, and just above each stated tier. Gas stays 0.
        self.assertAlmostEqual(gross_fee_usd(4.99), 4.99 * 0.02)
        self.assertAlmostEqual(gross_fee_usd(5), 0.10)
        self.assertAlmostEqual(gross_fee_usd(47.49), 47.49 * 0.02)
        self.assertAlmostEqual(gross_fee_usd(47.51), 0.95)
        self.assertAlmostEqual(gross_fee_usd(189.99), 0.95)
        self.assertAlmostEqual(gross_fee_usd(190.01), 190.01 * 0.005)
        self.assertAlmostEqual(fee_usd(47.51, 0.10), 0.855)
        self.assertAlmostEqual(fee_usd(190.01, 0.10), 190.01 * 0.005 * 0.9)
