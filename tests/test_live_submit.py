"""Live sign and send stays unreachable until every gate is closed."""
import base64
import os
import unittest

import db
import live_submit
import memecoin_desk
import phantom_transport
import solana_tx
from tests.support import temp_desk_env
from tests.test_fee_estimate import _pool
from tests.test_jupiter_route_v2 import _metas, _route, _tx

SECRET = "helius-live-path-secret"
MINT = solana_tx.b58encode(b"\x73" * 32)


class LiveSubmitTests(unittest.TestCase):
    def setUp(self):
        _tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        db.init_db()
        db.set_setting("mode", "PAPER", "test")
        db.set_setting("agents_enabled", "0", "test")
        memecoin_desk._ensure()

    def test_paper_and_an_open_gate_never_call_the_wallet_or_helius(self):
        calls = []

        def signer(message):
            calls.append(message)
            raise AssertionError("signed")

        def sender(body):
            calls.append(body)
            raise AssertionError("sent")

        paper = live_submit.submit("case", "hash", 1, signer=signer, sender=sender)
        self.assertEqual(paper["reason"], "live_unreachable")
        self.assertFalse(paper["sign"])
        self.assertFalse(paper["send"])
        self.assertFalse(paper["network_called"])
        self.assertEqual(calls, [])
        db.set_setting("mode", "LIVE", "test")
        db.set_setting("agents_enabled", "1", "test")
        self.assertFalse(live_submit.armed())
        opened = live_submit.submit("case", "hash", 1, signer=signer, sender=sender)
        self.assertEqual(opened["reason"], "live_unreachable")
        self.assertEqual(calls, [])
        self.assertEqual(db.get_setting("agents_enabled"), "1")
        self.assertFalse(live_submit.submit("case", "hash", 1)["live_ready"])

    def test_a_wrong_slippage_does_not_sign(self):
        values, _accounts = _pool(solana_tx.NATIVE_MINT, MINT)
        message = _tx(_metas(solana_tx.NATIVE_MINT, MINT, values), _route(100, 80))
        digest = live_submit.message_digest(message)
        self._store(digest, message)
        calls = []
        result = live_submit._exchange("case", digest, 1, None, lambda message_b64: calls.append(message_b64), lambda body: calls.append(body))
        self.assertEqual(result["reason"], "slippage_unproven")
        self.assertEqual(calls, [])
        self.assertFalse(result["sign"])

    def test_the_signed_message_matches_and_a_keyed_error_is_not_kept(self):
        values, _accounts = _pool(solana_tx.NATIVE_MINT, MINT)
        message = _tx(_metas(solana_tx.NATIVE_MINT, MINT, values), _route(100, 50))
        raw = bytearray(base64.b64decode(message))
        raw[1:65] = b"\x11" * 64
        signed_tx = base64.b64encode(bytes(raw)).decode()
        signature = solana_tx.first_signature(signed_tx)
        seen = []

        def signer(message_b64):
            self.assertEqual(message_b64, message)
            seen.append("sign")
            return {"signed_tx": signed_tx}

        def sender(body):
            seen.append(body["method"])
            if SECRET in repr(body):
                raise AssertionError("key in request")
            if body["method"] == "sendTransaction":
                self.assertEqual(body["params"][0], signed_tx)
                return {"status": 200, "body": {"result": signature}}
            self.assertEqual(body["params"][0], [signature])
            return {"status": 200, "body": {"result": {"value": [{"confirmationStatus": "confirmed", "err": None, "slot": 9}]}}}

        result = live_submit._sign_send_confirm("case-live", message, live_submit.message_digest(message), 1_700_000_000, signer, sender)
        self.assertTrue(result["reconciled"], result)
        self.assertTrue(result["sign"])
        self.assertTrue(result["send"])
        self.assertFalse(result["live_ready"])
        self.assertFalse(result["execution_eligible"])
        self.assertEqual(result["signature"], signature)
        self.assertEqual(seen, ["sign", "sendTransaction", "getSignatureStatuses"])
        logged = db.query("SELECT evidence_json FROM memecoin_log WHERE case_id = 'case-live' AND stage = 'live_submit'")
        self.assertEqual(len(logged), 1)
        self.assertNotIn(SECRET, logged[0]["evidence_json"])

        def leak(_body):
            raise RuntimeError("https://mainnet.helius-rpc.com/?api-key=" + SECRET)

        failed = live_submit._sign_send_confirm("case-live", message, live_submit.message_digest(message), 1_700_000_000, signer, leak)
        self.assertEqual(failed["reason"], "rpc_transport")
        self.assertNotIn(SECRET, repr(failed))
        self.assertNotIn("api-key=", repr(failed))
        self.assertFalse(failed["send"])

        def mismatch(_message):
            return {"signed_tx": base64.b64encode(b"\x01" + b"\x22" * 80).decode()}

        sent = []
        refused = live_submit._sign_send_confirm("case-live", message, "hash", 1, mismatch, lambda body: sent.append(body))
        self.assertEqual(refused["reason"], "message_mismatch")
        self.assertEqual(sent, [])

    def _store(self, digest: str, message: str) -> None:
        db.execute(
            """
            INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, ledger, ledger_provenance, created_at, updated_at)
            VALUES ('case', ?, 'protected', '{}', 'paper', 'server', 1, 1)
            """,
            (MINT,),
        )
        db.execute(
            """
            INSERT INTO memecoin_reservations (
                message_sha256, case_id, side, message_b64, router, blockhash, expire_at, ledger, ledger_provenance, created_at
            ) VALUES (?, 'case', 'entry', ?, 'jupiterz', ?, 1, 'paper', 'server', 1)
            """,
            (digest, message, solana_tx.b58encode(b"\x11" * 32)),
        )


if __name__ == "__main__":
    os.environ.setdefault("DESK_DATA_DIR", "/tmp/empty-desk")
    unittest.main()
