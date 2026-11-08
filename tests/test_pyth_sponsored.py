"""Sponsored Pyth Core accounts. The fixture is one mainnet read, not a second call."""
import json
import unittest
from pathlib import Path

import pyth_sponsored
import usdc_valuation

FIXTURE = json.loads(Path("tests/fixtures/pyth_sponsored_read.json").read_text())
SOL = Path("tests/fixtures/pyth_sol_usd_price_update_v2.bin").read_bytes()
USDC = Path("tests/fixtures/pyth_usdc_usd_price_update_v2.bin").read_bytes()
WHEN = FIXTURE["fetched_at"]


class SponsoredPythTests(unittest.TestCase):
    def tearDown(self):
        pyth_sponsored._LAST = None

    def test_shard_zero_matches_the_published_accounts(self):
        self.assertEqual(pyth_sponsored.account_for(pyth_sponsored.SOL_FEED), pyth_sponsored.SOL_ACCOUNT)
        self.assertEqual(pyth_sponsored.account_for(pyth_sponsored.USDC_FEED), pyth_sponsored.USDC_ACCOUNT)
        self.assertEqual(pyth_sponsored.USDC_FEED, usdc_valuation.FEED_ID)

    def test_the_mainnet_accounts_are_full_and_were_fresh_at_the_read(self):
        sol = pyth_sponsored.parse_account(
            SOL, owner=pyth_sponsored.RECEIVER_PROGRAM_ID, address=pyth_sponsored.SOL_ACCOUNT,
            feed_id=pyth_sponsored.SOL_FEED, now=WHEN,
        )
        usdc = pyth_sponsored.parse_account(
            USDC, owner=pyth_sponsored.RECEIVER_PROGRAM_ID, address=pyth_sponsored.USDC_ACCOUNT,
            feed_id=pyth_sponsored.USDC_FEED, now=WHEN,
        )
        self.assertTrue(sol["valid"], sol)
        self.assertTrue(sol["fresh"], sol)
        self.assertEqual(sol["price"], 8_909_321_745)
        self.assertEqual(sol["expo"], -8)
        self.assertEqual(sol["posted_slot"], 466_835_718)
        self.assertLessEqual(sol["age_s"], 30)
        self.assertTrue(usdc["fresh"], usdc)
        self.assertEqual(usdc["price"], 99_978_846)
        self.assertEqual(usdc["conf"], 10_032)
        self.assertEqual(usdc["publish_time"], 1_794_123_863)
        self.assertFalse(usdc["live"])
        self.assertFalse(usdc["hermes_called"])
        stale = pyth_sponsored.parse_account(
            USDC, owner=pyth_sponsored.RECEIVER_PROGRAM_ID, address=pyth_sponsored.USDC_ACCOUNT,
            feed_id=pyth_sponsored.USDC_FEED, now=usdc["publish_time"] + 31,
        )
        self.assertTrue(stale["valid"])
        self.assertFalse(stale["fresh"])
        self.assertEqual(stale["reason"], "valuation_stale")
        self.assertFalse(stale["usable"])

    def test_partial_or_unbound_accounts_are_not_a_price(self):
        partial = bytearray(USDC)
        partial[40] = 0
        refused = pyth_sponsored.parse_account(
            bytes(partial), owner=pyth_sponsored.RECEIVER_PROGRAM_ID, address=pyth_sponsored.USDC_ACCOUNT,
            feed_id=pyth_sponsored.USDC_FEED, now=WHEN,
        )
        self.assertEqual(refused["reason"], "verification_not_full")
        self.assertFalse(refused["usable"])
        other = pyth_sponsored.parse_account(
            USDC, owner=pyth_sponsored.PUSH_ORACLE_PROGRAM_ID, address=pyth_sponsored.USDC_ACCOUNT,
            feed_id=pyth_sponsored.USDC_FEED, now=WHEN,
        )
        self.assertEqual(other["reason"], "account_owner")
        moved = pyth_sponsored.parse_account(
            USDC, owner=pyth_sponsored.RECEIVER_PROGRAM_ID, address=pyth_sponsored.SOL_ACCOUNT,
            feed_id=pyth_sponsored.USDC_FEED, now=WHEN,
        )
        self.assertEqual(moved["reason"], "account_binding")

    def test_a_fixture_transport_records_both_feeds_without_hermes(self):
        import base64

        def transport(request):
            self.assertEqual(request["url"], pyth_sponsored.RPC)
            self.assertEqual(request["accounts"], [pyth_sponsored.SOL_ACCOUNT, pyth_sponsored.USDC_ACCOUNT])
            return {"status": 200, "body": {"result": {
                "context": {"slot": FIXTURE["slot"]},
                "value": [
                    {"owner": pyth_sponsored.RECEIVER_PROGRAM_ID, "data": [base64.b64encode(SOL).decode(), "base64"]},
                    {"owner": pyth_sponsored.RECEIVER_PROGRAM_ID, "data": [base64.b64encode(USDC).decode(), "base64"]},
                ],
            }}}

        read = pyth_sponsored.read_accounts(transport, WHEN)
        self.assertTrue(read["fresh"], read)
        self.assertEqual(read["slot"], 466_835_746)
        self.assertEqual(read["sol"]["price"], 8_909_321_745)
        self.assertFalse(read["live"])
        self.assertFalse(read["hermes_called"])
        view = pyth_sponsored.status(WHEN)
        self.assertTrue(view["configured"])
        self.assertTrue(view["valid"])
        self.assertTrue(view["fresh"])
        self.assertTrue(view["usable"])
        self.assertFalse(view["stale_observation"])
        self.assertFalse(view["live"])
        self.assertEqual(view["valuation"], "sol_usd")
        self.assertFalse(view["usdc_used"])
        later = pyth_sponsored.status(WHEN + 40)
        self.assertFalse(later["fresh"])
        self.assertFalse(later["usable"])
        self.assertGreater(later["age_s"], 30)
        self.assertNotEqual(later["age_s"], view["age_s"])

    def test_a_future_posted_slot_is_rejected_and_a_slot_behind_is_not(self):
        import base64
        def transport(slot):
            def inner(request):
                return {"status": 200, "body": {"result": {
                    "context": {"slot": slot},
                    "value": [
                        {"owner": pyth_sponsored.RECEIVER_PROGRAM_ID, "data": [base64.b64encode(SOL).decode(), "base64"]},
                        {"owner": pyth_sponsored.RECEIVER_PROGRAM_ID, "data": [base64.b64encode(USDC).decode(), "base64"]},
                    ],
                }}}
            return inner
        behind = pyth_sponsored.read_accounts(transport(466835746), WHEN)
        self.assertTrue(behind["fresh"], behind)
        self.assertEqual(behind["sol"]["posted_slot"], 466835718)
        self.assertLess(behind["sol"]["posted_slot"], behind["slot"])
        future = pyth_sponsored.read_accounts(transport(1), WHEN)
        self.assertFalse(future["ok"])
        self.assertEqual(future["reason"], "posted_slot_future")
        self.assertFalse(future["fresh"])

    def test_a_stale_sol_price_is_not_saved_by_a_fresh_usdc_leg(self):
        import base64
        sol_publish = 1794123880
        usdc = bytearray(USDC)
        usdc[93:101] = int(sol_publish + 120).to_bytes(8, "little", signed=True)
        def transport(request):
            return {"status": 200, "body": {"result": {
                "context": {"slot": 466835746},
                "value": [
                    {"owner": pyth_sponsored.RECEIVER_PROGRAM_ID, "data": [base64.b64encode(SOL).decode(), "base64"]},
                    {"owner": pyth_sponsored.RECEIVER_PROGRAM_ID, "data": [base64.b64encode(bytes(usdc)).decode(), "base64"]},
                ],
            }}}
        read = pyth_sponsored.read_accounts(transport, sol_publish + 120)
        self.assertFalse(read["sol"]["fresh"])
        self.assertTrue(read["usdc"]["fresh"])
        self.assertFalse(read["usdc"]["used"])
        self.assertFalse(read["ok"])
        self.assertEqual(read["reason"], "valuation_stale")
