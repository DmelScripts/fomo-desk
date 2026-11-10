"""Fixture-only rug history. The Helius key is never written into a result."""
import json
import os
import unittest

import dev_rug_history

MINT = "JEH7cJxAKdprFG5AvdsY2c4ZqojxLBjFmw19zADV6oK5"
OTHER = "2qEHjDLDLbuBgRYvsxhc5D6uDWAivNFZGan56P1tpump"
CREATOR = "D3XrkNZz6wx6cofot7Zohsf2KSsu2ArngNk8VqU9cTY3"
SECRET = "super-secret-helius-key"


def _asset(creators, authorities):
    return {"id": MINT, "creators": creators, "authorities": authorities}


def _page(ids, total=None):
    return {
        "jsonrpc": "2.0",
        "result": {
            "total": len(ids) if total is None else total,
            "limit": dev_rug_history.MAX_TOKENS,
            "page": 1,
            "items": [{"id": item, "interface": "FungibleToken"} for item in ids],
        },
    }


class DevRugHistoryTests(unittest.TestCase):
    def setUp(self):
        dev_rug_history.clear_cache()
        dev_rug_history.clear_recorded()
        dev_rug_history.reset_source()
        self.addCleanup(dev_rug_history.clear_cache)
        self.addCleanup(dev_rug_history.clear_recorded)
        self.addCleanup(dev_rug_history.reset_source)
        os.environ.pop(dev_rug_history.KEY_ENV, None)

    def test_a_missing_key_does_not_fetch(self):
        def trap(_request):
            raise AssertionError("network")

        original = dev_rug_history._post
        dev_rug_history._post = trap
        self.addCleanup(lambda: setattr(dev_rug_history, "_post", original))
        found = dev_rug_history.read_history(MINT, allow_network=True)
        self.assertEqual(found["reason"], "dev_rug_history_unverified")
        self.assertEqual(found["cause"], "missing_key")
        self.assertFalse(found["fetched"])

    def test_creator_comes_from_one_verified_creator_or_one_authority(self):
        asset = _asset([{"address": CREATOR, "share": 100, "verified": True}], [{"address": "auth", "scopes": ["full"]}])
        only = dev_rug_history.read_history(MINT, asset=asset, assets=_page([MINT]), now=10)
        self.assertTrue(only["verified"], only)
        self.assertEqual(only["rugs"], 0)
        self.assertEqual(only["creator_source"], "creator")
        self.assertEqual(only["past_tokens"], 0)
        self.assertTrue(only["source_wired"])
        caller = {"status": "checked", "source": "stalkchain_token_deployer", "rugs": 0, "tokens_seen": 2}
        self.assertEqual(dev_rug_history.assess(caller, MINT)["reason"], "dev_rug_history_unverified")
        dev_rug_history.clear_cache()
        nested = _page([MINT, OTHER])
        nested["result"]["items"][1]["rugcheck"] = {"rugged": True}
        nested["result"]["items"][1]["rugged"] = False
        ignored = dev_rug_history.read_history(MINT, asset=asset, assets=nested, now=11)
        self.assertEqual(ignored["reason"], "dev_rug_history_unverified")
        self.assertEqual(ignored["cause"], "rugged_not_fetched")
        dev_rug_history.remember_report(OTHER, {"rugged": True, "creator": CREATOR})
        flagged = dev_rug_history.read_history(MINT, asset=asset, assets=nested, now=12)
        self.assertEqual(flagged["reason"], "dev_rug_history")
        self.assertEqual(flagged["rugs"], 1)
        self.assertTrue(flagged["source_wired"])
        self.assertEqual(flagged["rugged_field_status"], "observed, undocumented")
        dev_rug_history.clear_recorded()
        self.assertFalse(only["chain_complete"])
        authority = _asset([{"address": CREATOR, "share": 100, "verified": False}], [{"address": "auth", "scopes": []}])
        by_authority = dev_rug_history.read_history(MINT, asset=authority, assets=_page([MINT]), now=10)
        self.assertEqual(by_authority["creator"], "auth")
        self.assertEqual(by_authority["creator_source"], "update_authority")
        several = _asset(
            [{"address": CREATOR, "verified": True}, {"address": "other", "verified": True}],
            [{"address": "auth"}],
        )
        self.assertEqual(dev_rug_history.read_history(MINT, asset=several, assets=_page([MINT]), now=10)["cause"], "several_creators")

    def test_a_truncated_list_or_a_past_token_fails_closed(self):
        asset = _asset([{"address": CREATOR, "verified": True}], [])
        truncated = dev_rug_history.read_history(MINT, asset=asset, assets=_page([MINT], total=3), now=20)
        self.assertEqual(truncated["cause"], "truncated")
        self.assertFalse(truncated["verified"])
        past = dev_rug_history.read_history(
            MINT, asset=asset, assets=_page([MINT, OTHER]), now=20,
        )
        self.assertEqual(past["cause"], "rugged_not_fetched")
        self.assertEqual(past["past_tokens"], 1)
        self.assertEqual(dev_rug_history.RUGGED_FIELD_STATUS, "observed, undocumented")
        dev_rug_history.remember_report(OTHER, {"rugged": "false", "creator": CREATOR})
        self.assertEqual(dev_rug_history.read_history(MINT, asset=asset, assets=_page([MINT, OTHER]), now=21)["cause"], "rugged_not_bool")
        dev_rug_history.remember_report(OTHER, {"rugged": False, "creator": "someone-else"})
        self.assertEqual(dev_rug_history.read_history(MINT, asset=asset, assets=_page([MINT, OTHER]), now=22)["cause"], "creator_mismatch")
        dev_rug_history.remember_report(OTHER, {"rugged": False, "creator": CREATOR}, status=200)
        clean = dev_rug_history.read_history(MINT, asset=asset, assets=_page([MINT, OTHER]), now=23)
        self.assertEqual(clean["rugs"], 0)
        self.assertFalse(clean["cached"])

    def test_a_clean_page_is_not_reused_and_a_rejection_can_be(self):
        first = dev_rug_history.read_history(MINT, creator=CREATOR, assets=_page([MINT]), now=100)
        self.assertEqual(first["rugs"], 0)
        self.assertFalse(first["cached"])
        calls = []

        def refuse(_request):
            calls.append("network")
            raise AssertionError("network")

        original = dev_rug_history._post
        dev_rug_history._post = refuse
        self.addCleanup(lambda: setattr(dev_rug_history, "_post", original))
        second = dev_rug_history.read_history(MINT, creator=CREATOR, now=101, allow_network=True)
        self.assertFalse(second["verified"])
        self.assertNotEqual(second.get("rugs"), 0)
        self.assertEqual(calls, [])
        dev_rug_history.remember_report(OTHER, {"rugged": True, "creator": CREATOR})
        grown = dev_rug_history.read_history(MINT, creator=CREATOR, assets=_page([MINT, OTHER]), now=102)
        self.assertEqual(grown["reason"], "dev_rug_history")
        self.assertFalse(grown["cached"])
        repeated = dev_rug_history.read_history(MINT, creator=CREATOR, now=103, allow_network=True)
        self.assertTrue(repeated["cached"])
        self.assertEqual(repeated["reason"], "dev_rug_history")
        self.assertEqual(calls, [])

    def test_the_key_is_not_in_the_result(self):
        os.environ[dev_rug_history.KEY_ENV] = SECRET
        self.addCleanup(lambda: os.environ.pop(dev_rug_history.KEY_ENV, None))

        def trap(request):
            self.assertTrue(SECRET in getattr(request, "full_url", ""))
            raise OSError("stopped")

        original = dev_rug_history._post
        dev_rug_history._post = trap
        self.addCleanup(lambda: setattr(dev_rug_history, "_post", original))
        found = dev_rug_history.read_history(MINT, allow_network=True)
        self.assertEqual(found["cause"], "fetch")
        self.assertNotIn(SECRET, json.dumps(found))
        held = dev_rug_history.read_history(MINT, creator=CREATOR, assets=_page([MINT]), now=5, allow_network=True)
        self.assertNotIn(SECRET, json.dumps(held))
        self.assertEqual(held["rugs"], 0)
