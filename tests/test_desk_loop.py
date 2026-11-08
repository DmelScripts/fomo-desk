"""The six-role handoff fetches. Caller labels are not a source. Nothing is armed."""
import os
import unittest

import desk_loop
import memecoin_desk
import solana_tx
import usdc_valuation
from tests.support import temp_desk_env

NOW = 1_800_000_000.0
MINT = solana_tx.b58encode(b"\x55" * 32)


def _pair():
    return [{
        "chainId": "solana",
        "dexId": "raydium",
        "pairAddress": solana_tx.b58encode(b"\x66" * 32),
        "baseToken": {"address": MINT},
        "priceUsd": "1.25",
        "marketCap": 1_000_000,
        "fdv": 5_000_000,
        "liquidity": {"usd": 250_000},
        "pairCreatedAt": int((NOW - 3600) * 1000),
        "priceChange": {"h1": 10},
    }]


class DeskLoopTests(unittest.TestCase):
    def setUp(self):
        _tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        os.environ.pop("STALKCHAIN_API_KEY", None)
        import db
        db.init_db()
        db.execute("UPDATE phantom_control SET value = '0' WHERE key = 'halt'")

    def test_a_missing_stalkchain_key_stops_the_handoff(self):
        def transport(request):
            if request["kind"] == "http":
                return {"status": 200, "body": _pair()}
            raw = bytearray(82)
            raw[44] = 6
            raw[45] = 1
            import base64
            return {"status": 200, "body": {"result": {"value": {"owner": solana_tx.TOKEN_PROGRAM, "data": [base64.b64encode(bytes(raw)).decode("ascii"), "base64"]}}}}

        result = desk_loop.run_once(MINT, transport, NOW)
        self.assertEqual(result["reason"], "lp")
        self.assertEqual(result["stage"], "rejected")
        self.assertFalse(result["stalkchain"]["called"])
        self.assertIn("dev_rugs", result["stalkchain"]["unproven"])
        self.assertIn("holder_growth", result["stalkchain"]["unproven"])
        self.assertIn("lp_lock_expiry", result["stalkchain"]["unproven"])
        self.assertFalse(result["score_measured"])
        self.assertFalse(result["armed"])
        self.assertFalse(result["live_ready"])
        self.assertFalse(result["usd_live"])
        self.assertEqual(usdc_valuation.keyless_catalog_is_not_a_price()["reason"], "metadata_not_a_price")
        self.assertTrue(memecoin_desk.log_for(result["case_id"]))

    def test_without_a_transport_nothing_is_fetched(self):
        result = desk_loop.run_once(MINT, None, NOW)
        self.assertEqual(result["reason"], "network_disabled")
        self.assertFalse(result["armed"])
