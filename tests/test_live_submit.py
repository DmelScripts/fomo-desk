"""Live sign and send stays behind LIVE, the agents, and every readiness gate."""
import base64
import os
import threading
import time
import unittest

import db
import desk_loop
import live_submit
import memecoin_desk
import phantom_transport
import solana_rpc
import solana_tx
from tests.support import temp_desk_env
from tests.test_fee_estimate import _pool
from tests.test_jupiter_route_v2 import _metas, _route, _tx

SECRET = "helius-live-path-secret"
MINT = solana_tx.b58encode(b"\x73" * 32)
NOW = 1_700_000_000
BLOCK = solana_tx.b58encode(b"\x44" * 32)


class LiveSubmitTests(unittest.TestCase):
    def setUp(self):
        _tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        db.init_db()
        db.set_setting("mode", "PAPER", "test")
        db.set_setting("agents_enabled", "0", "test")
        memecoin_desk._ensure()
        self._gates = desk_loop.remaining_live_gates
        self.addCleanup(lambda: setattr(desk_loop, "remaining_live_gates", self._gates))
        self._transport = solana_rpc.TRANSPORT
        self.addCleanup(lambda: setattr(solana_rpc, "TRANSPORT", self._transport))

    def test_the_sign_and_send_functions_refuse_unless_live_and_every_gate_is_closed(self):
        calls = []

        def signer(message):
            calls.append(("sign", message))

        paper = live_submit._exchange("case", "hash", NOW, None, signer)
        direct = live_submit._sign_send_confirm("case", "hash", NOW, None, signer)
        self.assertEqual(paper["reason"], "live_unreachable")
        self.assertEqual(direct["reason"], "live_unreachable")
        self.assertEqual(calls, [])
        db.set_setting("mode", "LIVE", "test")
        db.set_setting("agents_enabled", "1", "test")
        self.assertFalse(live_submit.armed())
        opened = live_submit._sign_send_confirm("case", "hash", NOW, None, signer)
        self.assertEqual(opened["reason"], "live_unreachable")
        self.assertFalse(opened["sign"])
        self.assertFalse(opened["send"])
        self.assertEqual(calls, [])

    def test_limits_and_the_fee_payer_are_checked_before_signing(self):
        self._arm()
        calls = []

        def signer(message):
            calls.append(message)

        wide_id, wide = self._message(_route(100, 80))
        refused = live_submit._sign_send_confirm(wide_id, live_submit.message_digest(wide), NOW, self._reader(), signer)
        self.assertEqual(refused["reason"], "slippage_unproven")
        self.assertEqual(calls, [])
        foreign = solana_tx.encode_legacy_transfer(solana_tx.b58encode(b"\x22" * 32), phantom_transport.REQUIRED_WALLET, 1, BLOCK)
        self._store("foreign", foreign, "foreign")
        payer = live_submit._sign_send_confirm("foreign", "foreign", NOW, self._reader(), signer)
        self.assertEqual(payer["reason"], "wallet_mismatch")
        self.assertEqual(calls, [])
        fee_id, costly = self._message(_route(100, 50), price=10**12)
        capped = live_submit._sign_send_confirm(fee_id, live_submit.message_digest(costly), NOW, self._reader(), signer)
        self.assertEqual(capped["reason"], "fee_cap", capped)
        self.assertEqual(calls, [])
        ticket_id, huge = self._message(_route(100, 50), reserved="9.00")
        ticket = live_submit._sign_send_confirm(ticket_id, live_submit.message_digest(huge), NOW, self._reader(), signer)
        self.assertEqual(ticket["reason"], "ticket", ticket)
        self.assertEqual(calls, [])
        self._lose()
        loss_id, halted_message = self._message(_route(101, 50))
        halted = live_submit._sign_send_confirm(loss_id, live_submit.message_digest(halted_message), NOW, self._reader(), signer)
        self.assertEqual(halted["reason"], "stop_new_entries")
        self.assertEqual(calls, [])

    def test_only_the_configured_helius_transport_can_send(self):
        self._arm()
        calls = []
        solana_rpc.TRANSPORT = lambda payload: calls.append(payload) or {"status": 200, "body": {"result": None}}
        os.environ.pop("HELIUS_API_KEY", None)
        missing = live_submit._sign_send_confirm("case", "hash", NOW, self._reader(), lambda message: {"signed_tx": message})
        self.assertEqual(missing["reason"], "helius_api_key_missing")
        self.assertEqual(calls, [])
        os.environ["HELIUS_API_KEY"] = "test-helius-not-a-secret"
        db.set_setting("rpc_url", "https://override.example/solana", "test")
        other = live_submit._sign_send_confirm("case", "hash", NOW, self._reader(), lambda message: {"signed_tx": message})
        self.assertEqual(other["reason"], "rpc_refused")
        self.assertEqual(calls, [])

    def test_a_retry_reconciles_the_same_signature_and_processed_stays_pending(self):
        self._arm()
        case_id, message = self._message(_route(100, 50))
        digest = live_submit.message_digest(message)
        raw = bytearray(base64.b64decode(message))
        raw[1:65] = b"\x11" * 64
        signed_tx = base64.b64encode(bytes(raw)).decode()
        signature = solana_tx.first_signature(signed_tx)
        signs = []
        methods = []
        state = {"status": "processed", "send_timeouts": 1}

        def signer(message_b64):
            self.assertEqual(message_b64, message)
            signs.append(message_b64)
            return {"signed_tx": signed_tx}

        def transport(payload):
            methods.append(payload["method"])
            if SECRET in repr(payload):
                raise AssertionError("key in request")
            if payload["method"] == "sendTransaction":
                if state["send_timeouts"]:
                    state["send_timeouts"] -= 1
                    raise TimeoutError("timed out api-key=" + SECRET)
                return {"status": 200, "body": {"result": signature}}
            return {"status": 200, "body": {"result": {"value": [{
                "confirmationStatus": state["status"], "err": None, "slot": 9,
            }]}}}

        solana_rpc.TRANSPORT = transport
        pending = live_submit.submit(case_id, digest, NOW, self._reader(), signer)
        self.assertEqual(pending["reason"], "confirmation_pending", pending)
        self.assertFalse(pending["reconciled"])
        self.assertEqual(signs, [message])
        self.assertEqual(methods, ["sendTransaction", "getSignatureStatuses"])
        again = live_submit.submit(case_id, digest, NOW, self._reader(), signer)
        self.assertEqual(again["reason"], "confirmation_pending")
        self.assertEqual(signs, [message])
        self.assertEqual(methods, ["sendTransaction", "getSignatureStatuses", "getSignatureStatuses"])
        state["status"] = "confirmed"
        done = live_submit.submit(case_id, digest, NOW, self._reader(), signer)
        self.assertTrue(done["reconciled"], done)
        self.assertEqual(done["signature"], signature)
        self.assertFalse(done["live_ready"])
        self.assertEqual(signs, [message])
        self.assertEqual(methods.count("sendTransaction"), 1)
        logged = db.query("SELECT evidence_json FROM memecoin_log WHERE stage = 'live_submit'")
        self.assertEqual(len(logged), 1)
        self.assertNotIn(SECRET, logged[0]["evidence_json"])
        repeat = live_submit.submit(case_id, digest, NOW, self._reader(), signer)
        self.assertTrue(repeat["reconciled"])
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM memecoin_log WHERE stage = 'live_submit'")[0]["n"], 1)
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM live_submit")[0]["n"], 1)
        self.assertEqual(methods.count("sendTransaction"), 1)

    def test_a_keyed_transport_error_is_a_reason_code(self):
        self._arm()
        case_id, message = self._message(_route(100, 50))
        digest = live_submit.message_digest(message)
        raw = bytearray(base64.b64decode(message))
        raw[1:65] = b"\x22" * 64
        signed_tx = base64.b64encode(bytes(raw)).decode()

        def signer(message_b64):
            return {"signed_tx": signed_tx}

        def transport(payload):
            raise RuntimeError("https://mainnet.helius-rpc.com/?api-key=" + SECRET)

        solana_rpc.TRANSPORT = transport
        failed = live_submit._sign_send_confirm(case_id, digest, NOW, self._reader(), signer)
        self.assertEqual(failed["reason"], "rpc_transport")
        self.assertNotIn(SECRET, repr(failed))
        self.assertNotIn("api-key=", repr(failed))
        self.assertFalse(failed["send"])
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM live_submit")[0]["n"], 1)

    def test_overlapping_calls_sign_and_send_the_reservation_once(self):
        self._arm()
        case_id, message = self._message(_route(102, 50))
        digest = live_submit.message_digest(message)
        raw = bytearray(base64.b64decode(message))
        raw[1:65] = b"\x33" * 64
        signed_tx = base64.b64encode(bytes(raw)).decode()
        signature = solana_tx.first_signature(signed_tx)
        signs = []
        sends = []
        gate = threading.Barrier(2)
        lock = threading.Lock()

        def signer(message_b64):
            with lock:
                signs.append(message_b64)
            time.sleep(0.2)
            return {"signed_tx": signed_tx}

        def transport(payload):
            if payload["method"] == "sendTransaction":
                with lock:
                    sends.append(payload["method"])
            return {"status": 200, "body": {"result": {"value": [{
                "confirmationStatus": "confirmed", "err": None, "slot": 4,
            }]}}} if payload["method"] == "getSignatureStatuses" else {"status": 200, "body": {"result": signature}}

        solana_rpc.TRANSPORT = transport
        results = []

        def run():
            gate.wait()
            results.append(live_submit.submit(case_id, digest, NOW, self._reader(), signer))

        threads = [threading.Thread(target=run), threading.Thread(target=run)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(signs), 1)
        self.assertEqual(len(sends), 1)
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM live_submit")[0]["n"], 1)
        self.assertEqual(len(results), 2)
        self.assertTrue(any(item.get("reason") == "already_in_flight" or item.get("reconciled") is True for item in results))
        self.assertEqual(sum(1 for item in results if item.get("reconciled") is True), 1)

    def test_a_missing_signer_does_not_stick_and_the_next_call_signs_once(self):
        self._arm()
        case_id, message = self._message(_route(103, 50))
        digest = live_submit.message_digest(message)
        raw = bytearray(base64.b64decode(message))
        raw[1:65] = b"\x55" * 64
        signed_tx = base64.b64encode(bytes(raw)).decode()
        signature = solana_tx.first_signature(signed_tx)
        missing = live_submit.submit(case_id, digest, NOW, self._reader(), None)
        self.assertEqual(missing["reason"], "phantom_session_missing")
        self.assertEqual(missing["sign"], False)
        self.assertEqual(self._rows(), [])
        wide_id, wide = self._message(_route(104, 80))
        refused = live_submit.submit(wide_id, live_submit.message_digest(wide), NOW, self._reader(), lambda message_b64: {"signed_tx": message_b64})
        self.assertEqual(refused["reason"], "slippage_unproven")
        self.assertEqual(self._rows(), [])

        def boom(message_b64):
            raise RuntimeError("session")

        broken = live_submit.submit(case_id, digest, NOW, self._reader(), boom)
        self.assertEqual(broken["reason"], "signature_refused")
        self.assertEqual(self._rows(), [])
        signs = []

        def signer(message_b64):
            signs.append(message_b64)
            return {"signed_tx": signed_tx}

        solana_rpc.TRANSPORT = lambda payload: {
            "status": 200,
            "body": {"result": signature} if payload["method"] == "sendTransaction" else {"result": {"value": [{
                "confirmationStatus": "confirmed", "err": None, "slot": 9,
            }]}},
        }
        done = live_submit.submit(case_id, digest, NOW, self._reader(), signer)
        self.assertTrue(done["reconciled"], done)
        self.assertEqual(signs, [message])
        self.assertEqual(len(self._rows()), 1)

    def test_an_armed_approach_uses_the_session_signer(self):
        import phantom_session
        captured = {}

        def submit(case_id, message, now, transport, signer=None):
            captured["signer"] = signer
            return {"reason": "phantom_session_missing", "sign": False, "send": False, "network_called": False, "broadcast": False}

        original = (
            desk_loop.fee_estimate_before_sign, desk_loop._gates_after_fee, desk_loop._server_auth,
            desk_loop.simulate_before_sign, live_submit.armed, live_submit.submit,
        )
        self.addCleanup(lambda: (
            setattr(desk_loop, "fee_estimate_before_sign", original[0]),
            setattr(desk_loop, "_gates_after_fee", original[1]),
            setattr(desk_loop, "_server_auth", original[2]),
            setattr(desk_loop, "simulate_before_sign", original[3]),
            setattr(live_submit, "armed", original[4]),
            setattr(live_submit, "submit", original[5]),
        ))
        live_submit.submit = submit
        quiet = desk_loop.approach_signer("case", "hash", self._reader(), NOW)
        self.assertNotIn("signer", captured)
        self.assertNotEqual(quiet.get("signer_is_mock"), False)
        desk_loop.fee_estimate_before_sign = lambda *args, **kwargs: {"fee_estimate_skip": False}
        desk_loop._gates_after_fee = lambda *args, **kwargs: {"ok": True}
        desk_loop._server_auth = lambda *args, **kwargs: {"ok": True}
        desk_loop.simulate_before_sign = lambda *args, **kwargs: {"ok": True}
        live_submit.armed = lambda: True
        armed = desk_loop.approach_signer("case", "hash", self._reader(), NOW)
        self.assertIs(captured["signer"], phantom_session.sign)
        self.assertFalse(armed["signer_is_mock"])
        self.assertFalse(armed["live_ready"])
        self.assertFalse(phantom_session.ready())

    def _rows(self):
        try:
            return db.query("SELECT message_sha256, signature, status FROM live_submit")
        except Exception:
            return []

    def _arm(self) -> None:
        os.environ["HELIUS_API_KEY"] = "test-helius-not-a-secret"
        self.addCleanup(lambda: os.environ.pop("HELIUS_API_KEY", None))
        db.set_setting("mode", "LIVE", "test")
        db.set_setting("agents_enabled", "1", "test")
        desk_loop.remaining_live_gates = lambda: [{"gate": "all", "closed": True}]
        self.assertTrue(live_submit.armed())

    def _message(self, swap: bytes, price: int = 1_000, reserved: str = "1.00"):
        values, accounts = _pool(solana_tx.NATIVE_MINT, MINT)
        message = _tx(_metas(solana_tx.NATIVE_MINT, MINT, values), swap, price=price, blockhash=BLOCK)
        digest = live_submit.message_digest(message)
        case_id = "case-%s-%s" % (digest[:8], reserved.replace(".", ""))
        self._store(digest, message, case_id, reserved)
        self._accounts = accounts
        return case_id, message

    def _store(self, digest: str, message: str, case_id: str, reserved: str = "1.00") -> None:
        db.execute(
            """
            INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, ledger, ledger_provenance, created_at, updated_at)
            VALUES (?, ?, 'protected', ?, 'paper', 'server', ?, ?)
            """,
            (case_id, MINT, '{"reserved_usd": "%s"}' % reserved, NOW, NOW),
        )
        db.execute(
            """
            INSERT INTO memecoin_reservations (
                message_sha256, case_id, side, message_b64, router, blockhash, last_valid_block_height,
                expire_at, ledger, ledger_provenance, created_at
            ) VALUES (?, ?, 'entry', ?, 'jupiterz', ?, 200, ?, 'paper', 'server', ?)
            """,
            (digest, case_id, message, BLOCK, NOW + 60, NOW),
        )

    def _reader(self):
        accounts = getattr(self, "_accounts", {})

        def transport(request):
            if request.get("method") == "getSlot":
                return {"status": 200, "body": {"result": 12}}
            if request.get("method") == "getBlockHeight":
                return {"status": 200, "body": {"result": 90}}
            if request.get("method") == "isBlockhashValid":
                return {"status": 200, "body": {"result": {"value": True}}}
            if request.get("method") == "hermes_price":
                return {"status": 200, "body": {
                    "parsed": [{
                        "id": "ef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d",
                        "price": {"price": "100", "conf": "0", "expo": 0, "publish_time": NOW},
                        "status": "trading",
                    }],
                }}
            if request.get("method") == "getMinimumBalanceForRentExemption":
                return {"status": 200, "body": {"result": solana_tx.TOKEN_ACCOUNT_RENT}}
            if request.get("method") == "getAccountInfo":
                item = accounts.get(request.get("account"))
                if item is None:
                    return {"status": 200, "body": {"result": {"value": None}}}
                owner, raw = item
                encoded = base64.b64encode(bytes(raw)).decode()
                return {"status": 200, "body": {"result": {"value": {"owner": owner, "data": [encoded, "base64"]}}}}
            return {"status": 200, "body": {"result": None}}
        return transport

    def _lose(self) -> None:
        db.execute(
            """
            INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, ledger, ledger_provenance, created_at, updated_at)
            VALUES ('loss-case', ?, 'entry_failed', ?, 'live', 'server', ?, ?)
            """,
            (MINT, '{"entry_funding": {"signature": "loss-sig"}, "realized_pnl": "-20.00", "pnl_live": true}', NOW, NOW),
        )
        db.execute(
            """
            INSERT INTO memecoin_entry_receipts (
                signature, case_id, message_sha256, slot, qty, cost_basis_lamports, network_fee_lamports,
                dex_fee_lamports, fee_usd, failed, ledger, ledger_provenance
            ) VALUES ('loss-sig', 'loss-case', 'loss', 1, 1, 1, 1, 1, '0.01', 1, 'live', 'server')
            """
        )
