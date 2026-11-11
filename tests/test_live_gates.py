"""Readiness gates stay open unless this process parsed a live provider body."""
import unittest

import desk_loop
import live_gate_proof
import urllib.request
from tests.support import temp_desk_env


class LiveGateTests(unittest.TestCase):
    def setUp(self):
        _tmp, restore = temp_desk_env()
        self.addCleanup(restore)

    def test_provider_gates_stay_open_without_a_live_parse(self):
        gates = {item["gate"]: item for item in desk_loop.remaining_live_gates()}
        for name in (
            "lp_lock_live",
            "rug_history_live",
            "holder_growth_live",
            "bundle_unique_buyers_smart_wallets_dev_holding",
            "price_freshness_proven",
            "quote_expiry",
            "stalkchain_rest_envelope",
        ):
            self.assertFalse(gates[name]["closed"], name)
        stalk = gates["stalkchain_rest_envelope"]["proves"]
        self.assertIn("has not published", stalk)
        self.assertIn("scored 0", stalk)
        self.assertFalse(stalk.__contains__("schemaVersion"))

    def test_a_fixture_body_does_not_close_a_gate_when_urlopen_is_patched(self):
        original = urllib.request.urlopen
        urllib.request.urlopen = lambda request, timeout=8: None
        self.addCleanup(lambda: setattr(urllib.request, "urlopen", original))
        live_gate_proof.consider("rugcheck", "https://api.rugcheck.xyz/v1/tokens/Mint/report/summary", {"lpLockedPct": 100})
        live_gate_proof.consider_jupiter({"expireAt": 9_999_999_999})
        live_gate_proof.note_history(["Mint"], True)
        self.assertFalse(live_gate_proof.closed("lp_lock_live"))
        self.assertFalse(live_gate_proof.closed("quote_expiry"))
        self.assertFalse(live_gate_proof.closed("rug_history_live"))

    def test_a_transport_fixture_does_not_close_the_provider_gates(self):
        import desk_providers
        mint = "JEH7cJxAKdprFG5AvdsY2c4ZqojxLBjFmw19zADV6oK5"
        desk_providers.set_transport(lambda spec: {"status": 200, "body": {"lpLockedPct": 100}, "_live": True})
        self.addCleanup(lambda: desk_providers.set_transport(None))
        desk_providers.production_fetch("rugcheck", mint)
        self.assertFalse(live_gate_proof.closed("lp_lock_live"))

    def test_stale_hermes_price_and_an_expired_quote_stop_before_signing(self):
        now = 1_700_000_000

        def stale(request):
            self.assertEqual(request.get("method"), "hermes_price")
            return {"status": 200, "body": {"parsed": [{
                "id": "ef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d",
                "price": {"price": "100", "conf": "0", "expo": 0, "publish_time": now - 31},
                "status": "trading",
            }]}}

        priced = desk_loop._signer_sol_price(stale, now)
        self.assertFalse(priced["ok"])
        self.assertEqual(priced["reason"], "price_stale")

        class Message:
            blockhash = "block"

        def height(request):
            if request.get("method") == "getBlockHeight":
                return {"status": 200, "body": {"result": 100}}
            return {"status": 200, "body": {"result": {"value": True}}}

        expired = desk_loop._signer_quote_and_blockhash(
            {"created_at": now - 21, "blockhash": "block", "last_valid_block_height": 200},
            Message(),
            height,
            now,
        )
        self.assertFalse(expired["ok"])
        self.assertEqual(expired["reason"], "quote_expired")
        blocked = desk_loop._signer_quote_and_blockhash(
            {"created_at": now, "blockhash": "block", "last_valid_block_height": 100},
            Message(),
            height,
            now,
        )
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["reason"], "blockhash_expired")
        fresh = desk_loop._signer_quote_and_blockhash(
            {"created_at": now, "blockhash": "block", "last_valid_block_height": 200},
            Message(),
            height,
            now,
        )
        self.assertTrue(fresh["ok"])
