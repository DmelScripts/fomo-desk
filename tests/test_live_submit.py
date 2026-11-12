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
        moment = time.time()
        self._clock = moment
        case_id, message = self._message(_route(100, 50), when=moment)
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
            if payload["method"] == "getTransaction":
                return {"status": 200, "body": {"result": self._fill(signed_tx, 5000, "100")}}
            return {"status": 200, "body": {"result": {"value": [{
                "confirmationStatus": state["status"], "err": None, "slot": 9,
            }]}}}

        solana_rpc.TRANSPORT = transport
        self._prove_owned_reads(message)
        pending = live_submit.submit(case_id, digest, time.time(), self._reader(), signer)
        self.assertEqual(pending["reason"], "confirmation_pending", pending)
        self.assertFalse(pending["reconciled"])
        self.assertEqual(signs, [message])
        self.assertEqual(methods, ["sendTransaction", "getSignatureStatuses"])
        again = live_submit.submit(case_id, digest, time.time(), self._reader(), signer)
        self.assertEqual(again["reason"], "confirmation_pending")
        self.assertEqual(signs, [message])
        self.assertEqual(methods, ["sendTransaction", "getSignatureStatuses", "getSignatureStatuses"])
        state["status"] = "confirmed"
        done = live_submit.submit(case_id, digest, time.time(), self._reader(), signer)
        self.assertTrue(done["reconciled"], done)
        self.assertEqual(done["signature"], signature)
        self.assertFalse(done["live_ready"])
        self.assertEqual(signs, [message])
        self.assertEqual(methods.count("sendTransaction"), 1)
        logged = db.query("SELECT evidence_json FROM memecoin_log WHERE stage = 'live_submit'")
        self.assertEqual(len(logged), 1)
        self.assertNotIn(SECRET, logged[0]["evidence_json"])
        repeat = live_submit.submit(case_id, digest, time.time(), self._reader(), signer)
        self.assertTrue(repeat["reconciled"])
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM memecoin_log WHERE stage = 'live_submit'")[0]["n"], 1)
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM live_submit")[0]["n"], 1)
        self.assertEqual(methods.count("sendTransaction"), 1)

    def test_a_keyed_transport_error_is_a_reason_code(self):
        self._arm()
        moment = time.time()
        self._clock = moment
        case_id, message = self._message(_route(100, 50), when=moment)
        digest = live_submit.message_digest(message)
        raw = bytearray(base64.b64decode(message))
        raw[1:65] = b"\x22" * 64
        signed_tx = base64.b64encode(bytes(raw)).decode()

        def signer(message_b64):
            return {"signed_tx": signed_tx}

        def transport(payload):
            raise RuntimeError("https://mainnet.helius-rpc.com/?api-key=" + SECRET)

        solana_rpc.TRANSPORT = transport
        self._prove_owned_reads(message)
        failed = live_submit._sign_send_confirm(case_id, digest, time.time(), self._reader(), signer)
        self.assertEqual(failed["reason"], "rpc_transport")
        self.assertNotIn(SECRET, repr(failed))
        self.assertNotIn("api-key=", repr(failed))
        self.assertFalse(failed["send"])
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM live_submit")[0]["n"], 1)

    def test_overlapping_calls_sign_and_send_the_reservation_once(self):
        self._arm()
        moment = time.time()
        self._clock = moment
        case_id, message = self._message(_route(102, 50), when=moment)
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
            if payload["method"] == "getTransaction":
                return {"status": 200, "body": {"result": self._fill(signed_tx, 5000, "102")}}
            return {"status": 200, "body": {"result": {"value": [{
                "confirmationStatus": "confirmed", "err": None, "slot": 4,
            }]}}} if payload["method"] == "getSignatureStatuses" else {"status": 200, "body": {"result": signature}}

        solana_rpc.TRANSPORT = transport
        results = []

        def run():
            gate.wait()
            results.append(live_submit.submit(case_id, digest, time.time(), self._reader(), signer))

        self._prove_owned_reads(message)
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
        moment = time.time()
        self._clock = moment
        case_id, message = self._message(_route(103, 50), when=moment)
        digest = live_submit.message_digest(message)
        raw = bytearray(base64.b64decode(message))
        raw[1:65] = b"\x55" * 64
        signed_tx = base64.b64encode(bytes(raw)).decode()
        signature = solana_tx.first_signature(signed_tx)
        missing = live_submit.submit(case_id, digest, time.time(), self._reader(), None)
        self.assertEqual(missing["reason"], "phantom_session_missing")
        self.assertEqual(missing["sign"], False)
        self.assertEqual(self._rows(), [])
        wide_id, wide = self._message(_route(104, 80))
        refused = live_submit.submit(wide_id, live_submit.message_digest(wide), NOW, self._reader(), lambda message_b64: {"signed_tx": message_b64})
        self.assertEqual(refused["reason"], "slippage_unproven")
        self.assertEqual(self._rows(), [])

        def boom(message_b64):
            raise RuntimeError("session")

        self._prove_owned_reads(message)
        broken = live_submit.submit(case_id, digest, time.time(), self._reader(), boom)
        self.assertEqual(broken["reason"], "signature_refused")
        self.assertEqual(self._rows(), [])
        signs = []

        def signer(message_b64):
            signs.append(message_b64)
            return {"signed_tx": signed_tx}

        solana_rpc.TRANSPORT = lambda payload: {
            "status": 200,
            "body": {"result": signature} if payload["method"] == "sendTransaction" else (
                {"result": self._fill(signed_tx, 5000, "103")} if payload["method"] == "getTransaction" else {"result": {"value": [{
                    "confirmationStatus": "confirmed", "err": None, "slot": 9,
                }]}}
            ),
        }
        done = live_submit.submit(case_id, digest, time.time(), self._reader(), signer)
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

    def test_a_stale_blockhash_is_not_signed_again(self):
        self._arm()
        case_id, message = self._message(_route(100, 50))
        calls = []

        def reader(request):
            if request.get("method") == "getBlockHeight":
                return {"status": 200, "body": {"result": 500}}
            return self._reader()(request)

        refused = live_submit.submit(case_id, live_submit.message_digest(message), NOW, reader, lambda body: calls.append(body))
        self.assertEqual(refused["reason"], "blockhash_expired", refused)
        self.assertEqual(calls, [])
        self.assertFalse(refused["sign"])
        self.assertEqual(self._rows(), [])

    def test_a_different_signer_or_a_changed_message_is_not_sent(self):
        self._arm()
        moment = time.time()
        self._clock = moment
        case_id, message = self._message(_route(110, 50), when=moment)
        digest = live_submit.message_digest(message)
        self._prove_owned_reads(message)
        methods = []
        solana_rpc.TRANSPORT = lambda payload: methods.append(payload["method"]) or {"status": 200, "body": {"result": None}}
        foreign = solana_tx.encode_legacy_transfer(solana_tx.b58encode(b"\x22" * 32), phantom_transport.REQUIRED_WALLET, 1, BLOCK)
        raw = bytearray(base64.b64decode(foreign))
        raw[1:65] = b"\x44" * 64
        mismatch = live_submit.submit(case_id, digest, time.time(), self._reader(), lambda body: {"signed_tx": base64.b64encode(bytes(raw)).decode()})
        self.assertEqual(mismatch["reason"], "signer_mismatch", mismatch)
        self.assertEqual(methods, [])
        self.assertEqual(self._rows(), [])
        flipped = bytearray(base64.b64decode(message))
        flipped[1:65] = b"\x11" * 64
        flipped[200] ^= 1
        tampered = live_submit.submit(case_id, digest, time.time(), self._reader(), lambda body: {"signed_tx": base64.b64encode(bytes(flipped)).decode()})
        self.assertEqual(tampered["reason"], "message_mismatch", tampered)
        self.assertEqual(methods, [])

    def test_a_confirmation_timeout_does_not_send_twice(self):
        self._arm()
        moment = time.time()
        self._clock = moment
        case_id, message = self._message(_route(111, 50), when=moment)
        digest = live_submit.message_digest(message)
        signed_tx, signature = self._signed(message)
        methods = []

        def transport(payload):
            methods.append(payload["method"])
            if payload["method"] == "sendTransaction":
                return {"status": 200, "body": {"result": signature}}
            raise TimeoutError("confirmation")

        solana_rpc.TRANSPORT = transport
        self._prove_owned_reads(message)
        pending = live_submit.submit(case_id, digest, time.time(), self._reader(), lambda body: {"signed_tx": signed_tx})
        self.assertEqual(pending["reason"], "open_liability", pending)
        self.assertEqual(methods.count("sendTransaction"), 1)
        self.assertIn("getSignatureStatuses", methods)
        self.assertIn("getTransaction", methods)
        self.assertEqual(self._rows()[0]["status"], "unknown")
        self.assertEqual(self._rows()[0]["idempotency_key"], digest)
        again = live_submit.submit(case_id, digest, time.time(), self._reader(), lambda body: {"signed_tx": signed_tx})
        self.assertEqual(methods.count("sendTransaction"), 1)
        self.assertNotEqual(again["reason"], None)
        import live_gate_proof
        self.assertFalse(live_gate_proof.real_submit_observed())

    def test_the_same_claim_is_not_submitted_twice(self):
        self._arm()
        moment = time.time()
        self._clock = moment
        case_id, message = self._message(_route(112, 50), when=moment)
        digest = live_submit.message_digest(message)
        signed_tx, signature = self._signed(message)
        signs = []
        sends = []

        def transport(payload):
            if payload["method"] == "sendTransaction":
                sends.append(payload["method"])
                return {"status": 200, "body": {"result": signature}}
            if payload["method"] == "getTransaction":
                return {"status": 200, "body": {"result": self._fill(signed_tx, 5000, "112")}}
            return {"status": 200, "body": {"result": {"value": [{"confirmationStatus": "confirmed", "err": None, "slot": 3}]}}}

        solana_rpc.TRANSPORT = transport
        self._prove_owned_reads(message)
        signer = lambda body: signs.append(body) or {"signed_tx": signed_tx}
        first = live_submit.submit(case_id, digest, time.time(), self._reader(), signer)
        second = live_submit.submit(case_id, digest, time.time(), self._reader(), signer)
        self.assertTrue(first["reconciled"], first)
        self.assertTrue(second["reconciled"], second)
        self.assertEqual(len(signs), 1)
        self.assertEqual(len(sends), 1)
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM live_submit")[0]["n"], 1)

    def test_an_rpc_error_is_not_retried_as_a_new_send(self):
        self._arm()
        moment = time.time()
        self._clock = moment
        case_id, message = self._message(_route(113, 50), when=moment)
        digest = live_submit.message_digest(message)
        signed_tx, _signature = self._signed(message)
        methods = []

        def transport(payload):
            methods.append(payload["method"])
            if payload["method"] == "sendTransaction":
                return {"status": 200, "body": {"error": {"code": -32002, "message": "blockhash"}}}
            raise TimeoutError("status")

        solana_rpc.TRANSPORT = transport
        self._prove_owned_reads(message)
        failed = live_submit.submit(case_id, digest, time.time(), self._reader(), lambda body: {"signed_tx": signed_tx})
        self.assertEqual(failed["reason"], "send_refused", failed)
        self.assertNotIn(SECRET, repr(failed))
        live_submit.submit(case_id, digest, time.time(), self._reader(), lambda body: {"signed_tx": signed_tx})
        self.assertEqual(methods.count("sendTransaction"), 1)

    def test_a_fee_or_slippage_past_the_cap_halts_new_entries(self):
        self._arm()
        moment = time.time()
        self._clock = moment
        case_id, message = self._message(_route(114, 50), when=moment)
        digest = live_submit.message_digest(message)
        signed_tx, signature = self._signed(message)
        self._prove_owned_reads(message)
        solana_rpc.TRANSPORT = lambda payload: {
            "status": 200,
            "body": {"result": signature} if payload["method"] == "sendTransaction" else (
                {"result": self._fill(signed_tx, 2_000_000, "114")} if payload["method"] == "getTransaction" else {"result": {"value": [{
                    "confirmationStatus": "confirmed", "err": None, "slot": 6,
                }]}}
            ),
        }
        done = live_submit.submit(case_id, digest, time.time(), self._reader(), lambda body: {"signed_tx": signed_tx})
        self.assertTrue(done["reconciled"], done)
        self.assertTrue(memecoin_desk.loss_totals()["live_halts_new_entries"])
        import live_gate_proof
        logged = db.query("SELECT evidence_json FROM memecoin_log WHERE stage = 'live_halt_review'")
        self.assertEqual(len(logged), 1)
        self.assertNotIn("api-key=", logged[0]["evidence_json"])
        self.assertFalse(live_gate_proof.real_submit_observed())
        later_id, later = self._message(_route(115, 50), when=time.time())
        calls = []
        halted = live_submit.submit(later_id, live_submit.message_digest(later), time.time(), self._reader(), lambda body: calls.append(body))
        self.assertEqual(halted["reason"], "stop_new_entries", halted)
        self.assertEqual(calls, [])

    def test_slippage_past_50_bps_halts_and_a_mock_does_not_close_the_gate(self):
        self._arm()
        moment = time.time()
        self._clock = moment
        case_id, message = self._message(_route(100, 50), when=moment)
        digest = live_submit.message_digest(message)
        signed_tx, signature = self._signed(message)
        os.environ["DESK_REAL_SUBMIT"] = "1"
        self.addCleanup(lambda: os.environ.pop("DESK_REAL_SUBMIT", None))
        import live_gate_proof
        live_gate_proof._apply("https://mainnet.helius-rpc.com/?api-key=hidden", {"result": signature})
        self.assertFalse(live_gate_proof.real_submit_observed())
        self._prove_owned_reads(message)
        solana_rpc.TRANSPORT = lambda payload: {
            "status": 200,
            "body": {"result": signature} if payload["method"] == "sendTransaction" else (
                {"result": self._fill(signed_tx, 5000, "1")} if payload["method"] == "getTransaction" else {"result": {"value": [{
                    "confirmationStatus": "confirmed", "err": None, "slot": 7,
                }]}}
            ),
        }
        done = live_submit.submit(case_id, digest, time.time(), self._reader(), lambda body: {"signed_tx": signed_tx})
        self.assertTrue(done["reconciled"], done)
        self.assertTrue(memecoin_desk.loss_totals()["live_halts_new_entries"])
        self.assertFalse(live_gate_proof.real_submit_observed())
        view = desk_loop.readiness()
        self.assertFalse(view["live_ready"])
        self.assertFalse(view["sign"])
        self.assertFalse(view["send"])

    def test_an_owned_confirmed_fill_closes_real_submit_and_an_exit_uses_it(self):
        import live_gate_proof
        self.addCleanup(live_gate_proof._reset_for_tests)
        self._arm()
        moment = time.time()
        self._clock = moment
        case_id, message = self._message(_route(100, 50), when=moment)
        digest = live_submit.message_digest(message)
        signed_tx, _signature = self._signed(message)
        self._prove_owned_reads(message)
        solana_rpc.TRANSPORT = None
        done = live_submit.submit(case_id, digest, time.time(), self._reader(), lambda body: {"signed_tx": signed_tx})
        self.assertTrue(done["reconciled"], done)
        self.assertTrue(live_gate_proof.real_submit_observed())
        gates = {item["gate"]: item["closed"] for item in self._gates()}
        self.assertTrue(gates["real_submit"])
        view = desk_loop.readiness()
        self.assertFalse(view["live_ready"])
        self.assertFalse(view["sign"])
        self.assertFalse(view["send"])
        fill = db.query("SELECT evidence_json FROM memecoin_log WHERE stage = 'live_fill'")
        self.assertEqual(len(fill), 1)
        self.assertNotIn("api-key=", fill[0]["evidence_json"])
        self.assertIn("fee_usd", fill[0]["evidence_json"])
        self.assertEqual(self._rows()[0]["idempotency_key"], digest)
        snap = db.query("SELECT snapshot_json FROM memecoin_cases WHERE id = ?", (case_id,))[0]
        self.assertIn("0.11", snap["snapshot_json"])
        def refuse_second(_body):
            raise AssertionError("signed twice")

        again = live_submit.submit(case_id, digest, time.time(), self._reader(), refuse_second)
        self.assertTrue(again["reconciled"], again)
        exit_id, exit_message = self._message(_route(80, 50), when=time.time(), side="exit")
        exit_signed, _exit_sig = self._signed(exit_message)
        self._prove_owned_reads(exit_message)
        exited = live_submit.submit(exit_id, live_submit.message_digest(exit_message), time.time(), self._reader(), lambda body: {"signed_tx": exit_signed})
        self.assertTrue(exited["reconciled"], exited)
        exits = db.query("SELECT evidence_json FROM memecoin_log WHERE stage = 'live_fill' AND case_id = ?", (exit_id,))
        self.assertEqual(len(exits), 1)
        self.assertIn(solana_tx.NATIVE_MINT, exits[0]["evidence_json"])

    def test_a_live_exit_feeds_realized_loss_and_the_allocation(self):
        self._arm()
        moment = time.time()
        self._clock = moment
        case_id, message = self._message(_route(100, 50), when=moment, ledger="live")
        digest = live_submit.message_digest(message)
        signed_tx, signature = self._signed(message)
        self._prove_owned_reads(message)

        def transport(payload, signed=signed_tx, sig=signature, amount="100"):
            if payload["method"] == "sendTransaction":
                return {"status": 200, "body": {"result": sig}}
            if payload["method"] == "getTransaction":
                return {"status": 200, "body": {"result": self._fill(signed, 5000, amount)}}
            return {"status": 200, "body": {"result": {"value": [{"confirmationStatus": "confirmed", "err": None, "slot": 4}]}}}

        solana_rpc.TRANSPORT = lambda payload: transport(payload)
        opened = live_submit.submit(case_id, digest, time.time(), self._reader(), lambda body: {"signed_tx": signed_tx})
        self.assertTrue(opened["reconciled"], opened)
        receipt = db.query("SELECT qty, ledger FROM memecoin_entry_receipts WHERE case_id = ?", (case_id,))
        self.assertEqual(len(receipt), 1)
        self.assertEqual(receipt[0]["ledger"], "live")
        held = db.query("SELECT snapshot_json FROM memecoin_cases WHERE id = ?", (case_id,))[0]["snapshot_json"]
        self.assertIn("0.11", held)
        import json
        body = json.loads(held)
        body["remaining_raw"] = 1_000_000
        db.execute("UPDATE memecoin_cases SET snapshot_json = ? WHERE id = ?", (json.dumps(body), case_id))
        _exit_id, exit_message = self._message(_route(100, 50), when=time.time(), side="exit", ledger="live", case_id=case_id)
        exit_signed, exit_sig = self._signed(exit_message)
        self._prove_owned_reads(exit_message)
        solana_rpc.TRANSPORT = lambda payload: transport(payload, exit_signed, exit_sig, "1")
        closed = live_submit.submit(case_id, live_submit.message_digest(exit_message), time.time(), self._reader(), lambda body: {"signed_tx": exit_signed})
        self.assertTrue(closed["reconciled"], closed)
        totals = memecoin_desk.loss_totals()
        self.assertNotEqual(totals["live_loss_usd"], "0.00")
        self.assertLess(float(totals["live_loss_usd"]), 20)

    def test_a_get_transaction_must_be_the_sent_signature(self):
        import json
        from decimal import Decimal
        import live_gate_proof
        self.addCleanup(live_gate_proof._reset_for_tests)
        self.addCleanup(lambda: setattr(self, "_foreign_tx", None))
        self._arm()
        self._clock = time.time()
        case_a, message_a = self._message(_route(100, 50, amount=1_400_000), when=time.time(), ledger="live")
        digest_a = live_submit.message_digest(message_a)
        signed_a, signature_a = self._signed(message_a)
        _case_b, message_b = self._message(_route(100, 50, amount=1_500_000), when=time.time(), ledger="live")
        raw_b = bytearray(base64.b64decode(message_b))
        raw_b[1:65] = b"\x22" * 64
        signed_b = base64.b64encode(bytes(raw_b)).decode()
        signature_b = solana_tx.first_signature(signed_b)
        self.assertNotEqual(signature_a, signature_b)
        self.assertNotEqual(solana_tx.message_bytes(signed_a), solana_tx.message_bytes(signed_b))
        self._prove_owned_reads(message_a)
        self._foreign_tx = signed_b
        solana_rpc.TRANSPORT = None
        swapped = live_submit.submit(case_a, digest_a, time.time(), self._reader(), lambda body, signed=signed_a: {"signed_tx": signed})
        self.assertFalse(swapped["reconciled"], swapped)
        self.assertEqual(swapped["reason"], "fill_unmatched", swapped)
        self.assertEqual(db.query("SELECT status FROM live_submit WHERE message_sha256 = ?", (digest_a,))[0]["status"], "unknown")
        self.assertFalse(live_gate_proof.real_submit_observed())
        self._assert_open_loss(case_a)
        db.execute("DELETE FROM settings WHERE key = 'live_halt_review'")
        case_c, message_c = self._message(_route(100, 50, amount=1_600_000), when=time.time(), ledger="live")
        digest_c = live_submit.message_digest(message_c)
        signed_c, signature_c = self._signed(message_c)
        _case_d, message_d = self._message(_route(100, 50, amount=1_700_000), when=time.time(), ledger="live")
        signed_d, _signature_d = self._signed(message_d)
        raw_c = bytearray(base64.b64decode(signed_c))
        raw_d = bytearray(base64.b64decode(signed_d))
        raw_d[1:65] = raw_c[1:65]
        spliced = base64.b64encode(bytes(raw_d)).decode()
        self.assertEqual(solana_tx.first_signature(spliced), signature_c)
        self.assertNotEqual(solana_tx.message_bytes(spliced), solana_tx.message_bytes(signed_c))
        solana_rpc.TRANSPORT = lambda payload, signed=signed_c, signature=signature_c, body=self._fill(spliced, 5000, "100"): {
            "status": 200,
            "body": {"result": signature} if payload["method"] == "sendTransaction" else (
                {"result": body} if payload["method"] == "getTransaction" else {"result": {"value": [{
                    "confirmationStatus": "confirmed", "err": None, "slot": 8,
                }]}}
            ),
        }
        moved = live_submit.submit(case_c, digest_c, time.time(), self._reader(), lambda body, signed=signed_c: {"signed_tx": signed})
        self.assertFalse(moved["reconciled"], moved)
        self.assertEqual(moved["reason"], "fill_unmatched", moved)
        self.assertEqual(db.query("SELECT status FROM live_submit WHERE message_sha256 = ?", (digest_c,))[0]["status"], "unknown")
        self.assertFalse(live_gate_proof.real_submit_observed())
        self._assert_open_loss(case_c)
        db.execute("DELETE FROM settings WHERE key = 'live_halt_review'")
        case_e, message_e = self._message(_route(100, 50, amount=1_800_000), when=time.time(), ledger="live")
        digest_e = live_submit.message_digest(message_e)
        signed_e, signature_e = self._signed(message_e)
        other = solana_tx.b58encode(b"\x99" * 64)
        self.assertNotEqual(other, signature_e)
        methods = []

        def transport(payload):
            methods.append(payload["method"])
            if payload["method"] == "sendTransaction":
                return {"status": 200, "body": {"result": other}}
            raise AssertionError("polled a signature the send did not match")

        solana_rpc.TRANSPORT = transport
        lied = live_submit.submit(case_e, digest_e, time.time(), self._reader(), lambda body, signed=signed_e: {"signed_tx": signed})
        self.assertFalse(lied["reconciled"], lied)
        self.assertEqual(lied["reason"], "fill_unmatched", lied)
        self.assertEqual(methods, ["sendTransaction"])
        self.assertEqual(db.query("SELECT status FROM live_submit WHERE message_sha256 = ?", (digest_e,))[0]["status"], "unknown")
        self.assertFalse(live_gate_proof.real_submit_observed())
        self._assert_open_loss(case_e)
        totals = memecoin_desk.loss_totals()
        self.assertTrue(totals["live_halts_new_entries"])
        self.assertGreater(Decimal(totals["live_loss_usd"]), 0)
        self.assertNotIn("api-key=", json.dumps(totals))

    def _assert_open_loss(self, case_id: str) -> None:
        import json
        from decimal import Decimal
        row = db.query("SELECT stage, snapshot_json FROM memecoin_cases WHERE id = ?", (case_id,))[0]
        snap = json.loads(row["snapshot_json"])
        self.assertEqual(row["stage"], "entry_failed")
        self.assertLess(Decimal(snap["realized_pnl"]), 0)
        self.assertEqual(snap["reserved_usd"], format(-Decimal(snap["realized_pnl"]), "f"))
        logged = json.loads(db.query("SELECT evidence_json FROM memecoin_log WHERE case_id = ? AND stage = 'live_fill'", (case_id,))[0]["evidence_json"])
        self.assertTrue(logged["anomalous"])
        self.assertEqual(logged["reason"], "fill_unmatched")

    def test_a_bad_confirmed_fill_is_an_open_loss(self):
        import json
        from decimal import Decimal
        import live_gate_proof
        self.addCleanup(live_gate_proof._reset_for_tests)
        self._arm()
        self._clock = time.time()
        foreign = solana_tx.b58encode(b"\x22" * 32)

        def wsol_only(tx):
            payer = solana_tx.decode_transaction(base64.b64decode(tx)).fee_payer
            def row(amount):
                return {
                    "accountIndex": 2, "mint": solana_tx.NATIVE_MINT, "owner": payer,
                    "uiTokenAmount": {"amount": amount, "decimals": 9},
                }
            return {"slot": 6, "transaction": tx, "meta": {
                "err": None, "fee": 5000,
                "preTokenBalances": [row("0")], "postTokenBalances": [row("5000000")],
            }}

        def missing(tx):
            return {"slot": 6, "transaction": tx, "meta": {
                "err": None, "fee": 5000, "preTokenBalances": [], "postTokenBalances": [],
            }}

        specs = [
            (1_000_000, lambda tx: self._fill(tx, 5000, "0", pre="1000000"), "fill_anomaly"),
            (1_100_000, wsol_only, "fill_unmatched"),
            (1_200_000, lambda tx: self._fill(tx, 5000, "100", owner=foreign), "fill_unmatched"),
            (1_300_000, missing, "fill_unmatched"),
        ]
        proved = False
        for amount, build, reason in specs:
            db.execute("DELETE FROM settings WHERE key = 'live_halt_review'")
            case_id, message = self._message(_route(100, 50, amount=amount), when=time.time(), ledger="live")
            digest = live_submit.message_digest(message)
            signed, signature = self._signed(message)
            if not proved:
                self._prove_owned_reads(message)
                proved = True
            solana_rpc.TRANSPORT = lambda payload, signed=signed, signature=signature, build=build: {
                "status": 200,
                "body": {"result": signature} if payload["method"] == "sendTransaction" else (
                    {"result": build(signed)} if payload["method"] == "getTransaction" else {"result": {"value": [{
                        "confirmationStatus": "confirmed", "err": None, "slot": 8,
                    }]}}
                ),
            }
            done = live_submit.submit(case_id, digest, time.time(), self._reader(), lambda body, signed=signed: {"signed_tx": signed})
            self.assertFalse(done["reconciled"], (reason, done))
            self.assertEqual(done["reason"], reason, done)
            status = db.query("SELECT status FROM live_submit WHERE message_sha256 = ?", (digest,))[0]["status"]
            self.assertEqual(status, "unknown")
            self.assertFalse(live_gate_proof.real_submit_observed())
            row = db.query("SELECT stage, snapshot_json FROM memecoin_cases WHERE id = ?", (case_id,))[0]
            snap = json.loads(row["snapshot_json"])
            self.assertEqual(row["stage"], "entry_failed")
            self.assertLess(Decimal(snap["realized_pnl"]), 0)
            self.assertEqual(snap["reserved_usd"], format(-Decimal(snap["realized_pnl"]), "f"))
            self.assertTrue(snap["entry_funding"]["anomalous"])
            logged = db.query("SELECT evidence_json FROM memecoin_log WHERE case_id = ? AND stage = 'live_fill'", (case_id,))
            body = json.loads(logged[0]["evidence_json"])
            self.assertTrue(body["anomalous"])
            self.assertGreater(body["slippage_bps"], 50)
        totals = memecoin_desk.loss_totals()
        self.assertTrue(totals["live_halts_new_entries"])
        self.assertGreater(Decimal(totals["live_loss_usd"]), 0)

    def _signed(self, message: str):
        raw = bytearray(base64.b64decode(message))
        raw[1:65] = b"\x11" * 64
        signed_tx = base64.b64encode(bytes(raw)).decode()
        return signed_tx, solana_tx.first_signature(signed_tx)

    def _fill(self, signed_tx: str, fee: int, amount: str, pre: str = "0", owner: str | None = None) -> dict:
        tx = solana_tx.decode_transaction(base64.b64decode(signed_tx))
        import jupiter_route_v2
        import live_gate_proof
        import solana_jupiter
        output = live_gate_proof._traded_output_mint(tx)
        who = tx.fee_payer if owner is None else owner
        pre_rows = [{
            "accountIndex": 2, "mint": output, "owner": who,
            "uiTokenAmount": {"amount": pre, "decimals": 6},
        }]
        post_rows = [{
            "accountIndex": 2, "mint": output, "owner": who,
            "uiTokenAmount": {"amount": amount, "decimals": 6},
        }]
        if output == solana_tx.NATIVE_MINT and owner is None:
            hop = next(item for item in tx.instructions if tx.static_keys[item.program_index] == solana_jupiter.PROGRAM_ID)
            sold = tx.static_keys[hop.account_indexes[3]]
            incoming = "0"
            for item in tx.instructions:
                if tx.static_keys[item.program_index] == solana_jupiter.PROGRAM_ID:
                    parsed = jupiter_route_v2.parse(bytes(item.data))
                    if parsed.get("ok") is True:
                        incoming = str(parsed["in_amount"])
            pre_rows.append({
                "accountIndex": 3, "mint": sold, "owner": who,
                "uiTokenAmount": {"amount": incoming, "decimals": 6},
            })
            post_rows.append({
                "accountIndex": 3, "mint": sold, "owner": who,
                "uiTokenAmount": {"amount": "0", "decimals": 6},
            })
        return {
            "slot": 6,
            "transaction": signed_tx,
            "meta": {"err": None, "fee": fee, "preTokenBalances": pre_rows, "postTokenBalances": post_rows},
        }

    def _prove_owned_reads(self, message: str) -> None:
        """Read Hermes, the Jupiter quote, and leave Helius up. The desk's own TLS records the proofs."""
        import json
        import socket
        import ssl
        import subprocess
        import tempfile
        import threading
        import urllib.request
        from pathlib import Path
        import jupiter_route_v2
        import jupiter_swap_v2
        import live_gate_proof
        import solana_jupiter
        from tests.test_live_gates import _open_blocked_socket, _redirected_getaddrinfo

        os.environ["DESK_QUOTE_MAX_AGE_S"] = "30"
        self.addCleanup(lambda: os.environ.pop("DESK_QUOTE_MAX_AGE_S", None))
        try:
            probe = socket.socket()
            probe.bind(("8.8.8.8", 0))
            probe.close()
        except OSError:
            subprocess.run(["sudo", "-n", "python3", "-c",
                "import socket,struct,fcntl; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); "
                "name=b'lo:1'+b'\\x00'*12; sin=struct.pack('=H2x4s8x', socket.AF_INET, socket.inet_aton('8.8.8.8')); "
                "fcntl.ioctl(s, 0x8916, name[:16]+sin)"
            ], check=True, capture_output=True)
        decoded = solana_tx.decode_transaction(base64.b64decode(message))
        route = b""
        for item in decoded.instructions:
            program = decoded.static_keys[item.program_index]
            if program == solana_jupiter.PROGRAM_ID:
                route = bytes(item.data)
                break
        parsed = jupiter_route_v2.parse(route)
        quoted = str(parsed["quoted_out_amount"])
        amount = str(parsed["in_amount"])
        hop = next(item for item in decoded.instructions if decoded.static_keys[item.program_index] == solana_jupiter.PROGRAM_ID)
        left = decoded.static_keys[hop.account_indexes[3]]
        right = decoded.static_keys[hop.account_indexes[4]]
        root = Path(tempfile.mkdtemp())
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", str(root / "ca.key"), "-out", str(root / "ca.pem"), "-days", "1", "-nodes", "-subj", "/CN=Desk Test CA"], check=True, capture_output=True)
        subprocess.run(["openssl", "req", "-newkey", "rsa:2048", "-keyout", str(root / "server.key"), "-out", str(root / "server.csr"), "-nodes", "-subj", "/CN=desk"], check=True, capture_output=True)
        ext = root / "ext.cnf"
        ext.write_text("subjectAltName=DNS:hermes.pyth.network,DNS:api.jup.ag,DNS:mainnet.helius-rpc.com\n")
        subprocess.run(["openssl", "x509", "-req", "-in", str(root / "server.csr"), "-CA", str(root / "ca.pem"), "-CAkey", str(root / "ca.key"), "-CAcreateserial", "-out", str(root / "server.pem"), "-days", "1", "-extfile", str(ext)], check=True, capture_output=True)
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(root / "server.pem", root / "server.key")
        listener = socket.socket()
        listener.setsockopt(socket.SOL_IP, 15, 1)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("8.8.8.8", 0))
        listener.listen(8)
        listener.settimeout(0.5)
        port = listener.getsockname()[1]
        stop = threading.Event()
        sent_tx = {"raw": None}

        def answer(head: bytes, rest: bytes) -> bytes:
            if b"hermes.pyth.network" in head:
                body = {
                    "parsed": [{
                        "id": "ef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d",
                        "price": {"price": "100", "conf": "0", "expo": 0, "publish_time": int(time.time())},
                        "status": "trading",
                    }],
                }
                return json.dumps(body).encode()
            if b"api.jup.ag" in head:
                target = head.split(b"\r\n", 1)[0].decode("latin1")
                query = {}
                if "?" in target:
                    import urllib.parse
                    query = urllib.parse.parse_qs(target.split("?", 1)[1].split(" ", 1)[0])
                input_mint = (query.get("inputMint") or [solana_tx.NATIVE_MINT])[0]
                output_mint = (query.get("outputMint") or [MINT])[0]
                body = {
                    "inputMint": input_mint,
                    "outputMint": output_mint,
                    "inAmount": amount,
                    "outAmount": quoted,
                    "expireAt": int(time.time()) + 120,
                }
                return json.dumps(body).encode()
            sent = json.loads(rest.decode() or "{}")
            method = sent.get("method")
            if method == "sendTransaction":
                sent_tx["raw"] = sent["params"][0]
                signature = solana_tx.first_signature(sent_tx["raw"])
                return json.dumps({"jsonrpc": "2.0", "id": 1, "result": signature}).encode()
            if method == "getSignatureStatuses":
                return json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"value": [{
                    "confirmationStatus": "confirmed", "err": None, "slot": 9,
                }]}}).encode()
            if method == "getTransaction" and sent_tx["raw"]:
                raw = sent_tx["raw"]
                foreign = getattr(self, "_foreign_tx", None)
                if isinstance(foreign, str) and foreign != "":
                    raw = foreign
                tx = solana_tx.decode_transaction(base64.b64decode(raw))
                output = live_gate_proof._traded_output_mint(tx) or MINT
                pre_rows = [{
                    "accountIndex": 2, "mint": output, "owner": tx.fee_payer,
                    "uiTokenAmount": {"amount": "0", "decimals": 6},
                }]
                post_rows = [{
                    "accountIndex": 2, "mint": output, "owner": tx.fee_payer,
                    "uiTokenAmount": {"amount": quoted, "decimals": 6},
                }]
                if output == solana_tx.NATIVE_MINT:
                    pre_rows.append({
                        "accountIndex": 3, "mint": left, "owner": tx.fee_payer,
                        "uiTokenAmount": {"amount": amount, "decimals": 6},
                    })
                    post_rows.append({
                        "accountIndex": 3, "mint": left, "owner": tx.fee_payer,
                        "uiTokenAmount": {"amount": "0", "decimals": 6},
                    })
                return json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
                    "slot": 9,
                    "transaction": raw,
                    "meta": {"err": None, "fee": 5000, "preTokenBalances": pre_rows, "postTokenBalances": post_rows},
                }}).encode()
            encoded = sent["params"][0]
            tx = solana_tx.decode_transaction(base64.b64decode(encoded))
            output = live_gate_proof._traded_output_mint(tx) or MINT
            width = len(tx.static_keys)
            value = {
                "err": None,
                "logs": ["Program log: Instruction: Swap"],
                "unitsConsumed": 8000,
                "fee": 5000,
                "preTokenBalances": [{
                    "accountIndex": 2, "mint": output, "owner": tx.fee_payer,
                    "uiTokenAmount": {"amount": "0", "decimals": 6},
                }],
                "postTokenBalances": [{
                    "accountIndex": 2, "mint": output, "owner": tx.fee_payer,
                    "uiTokenAmount": {"amount": quoted, "decimals": 6},
                }],
                "preBalances": [1_000_000_000] + [1] * (width - 1),
                "postBalances": [999_995_000] + [1] * (width - 1),
            }
            return json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"value": value}}).encode()

        def serve():
            while not stop.is_set():
                try:
                    raw, _addr = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                try:
                    wrapped = server_ctx.wrap_socket(raw, server_side=True)
                    wrapped.settimeout(3)
                    data = b""
                    while b"\r\n\r\n" not in data:
                        chunk = wrapped.recv(8192)
                        if not chunk:
                            break
                        data += chunk
                    head, _, rest = data.partition(b"\r\n\r\n")
                    length = 0
                    for line in head.split(b"\r\n"):
                        if line.lower().startswith(b"content-length:"):
                            length = int(line.split(b":", 1)[1].strip())
                    while len(rest) < length:
                        rest += wrapped.recv(8192)
                    payload = answer(head, rest)
                    wrapped.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(payload)
                        + payload
                    )
                    wrapped.close()
                except Exception:
                    try:
                        raw.close()
                    except Exception:
                        pass

        threading.Thread(target=serve, daemon=True).start()
        bundle = Path(live_gate_proof._CA_FILE)
        original_bundle = bundle.read_bytes()
        bundle.write_bytes(original_bundle + b"\n" + (root / "ca.pem").read_bytes())
        original_lookup = socket.getaddrinfo.__code__
        opener = socket.create_connection
        original_open = opener.__code__
        socket._proof_redirect = ("8.8.8.8", port)
        socket.getaddrinfo.__code__ = _redirected_getaddrinfo.__code__
        opener.__code__ = _open_blocked_socket.__code__

        def restore():
            stop.set()
            socket.getaddrinfo.__code__ = original_lookup
            opener.__code__ = original_open
            socket._proof_redirect = None
            bundle.write_bytes(original_bundle)
            try:
                listener.close()
            except Exception:
                pass

        self.addCleanup(restore)
        self._clock = time.time()
        hermes = urllib.request.Request(
            "https://hermes.pyth.network/v2/updates/price/latest?ids=ef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d",
            headers={"accept": "application/json"},
        )
        status, _raw, proved = live_gate_proof.exchange(hermes, 8, MINT)
        self.assertTrue(proved, status)
        self.assertTrue(live_gate_proof.closed("price_freshness_proven", MINT))
        quote = urllib.request.Request(
            jupiter_swap_v2.order_url(left, right, amount),
            headers={"accept": "application/json"},
        )
        status, _raw, proved = live_gate_proof.exchange(quote, 8, MINT)
        self.assertTrue(proved, status)
        self.assertTrue(live_gate_proof.quote_ready(MINT, time.time(), input_mint=left, output_mint=right))

    def _rows(self):
        try:
            return db.query("SELECT message_sha256, idempotency_key, signature, status FROM live_submit")
        except Exception:
            return []

    def _arm(self) -> None:
        os.environ["HELIUS_API_KEY"] = "test-helius-not-a-secret"
        self.addCleanup(lambda: os.environ.pop("HELIUS_API_KEY", None))
        db.set_setting("mode", "LIVE", "test")
        db.set_setting("agents_enabled", "1", "test")
        desk_loop.remaining_live_gates = lambda: [{"gate": "all", "closed": True}]
        self.assertTrue(live_submit.armed())

    def _message(self, swap: bytes, price: int = 1_000, reserved: str = "1.00", when: float | None = None, side: str = "entry", ledger: str = "paper", case_id: str | None = None):
        source, dest = (MINT, solana_tx.NATIVE_MINT) if side == "exit" else (solana_tx.NATIVE_MINT, MINT)
        values, accounts = _pool(source, dest)
        built = _tx(_metas(source, dest, values), swap, price=price, blockhash=BLOCK, side="sell" if side == "exit" else "buy")
        digest = live_submit.message_digest(built)
        if case_id is None:
            case_id = "case-%s-%s" % (digest[:8], reserved.replace(".", ""))
        snapshot = '{"reserved_usd": "%s"}' % reserved
        if side == "exit":
            snapshot = '{"reserved_usd": "%s", "remaining_raw": %d}' % (reserved, int.from_bytes(swap[8:16], "little"))
        self._store(digest, built, case_id, reserved, when=NOW if when is None else when, side=side, snapshot=snapshot, ledger=ledger)
        self._accounts = accounts
        return case_id, built

    def _store(self, digest: str, message: str, case_id: str, reserved: str = "1.00", when: float | None = None, side: str = "entry", snapshot: str | None = None, ledger: str = "paper") -> None:
        stamp = NOW if when is None else when
        body = snapshot if snapshot is not None else '{"reserved_usd": "%s"}' % reserved
        if db.query("SELECT id FROM memecoin_cases WHERE id = ?", (case_id,)) == []:
            db.execute(
                """
                INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, ledger, ledger_provenance, created_at, updated_at)
                VALUES (?, ?, 'protected', ?, ?, 'server', ?, ?)
                """,
                (case_id, MINT, body, ledger, stamp, stamp),
            )
        db.execute(
            """
            INSERT INTO memecoin_reservations (
                message_sha256, case_id, side, message_b64, router, blockhash, last_valid_block_height,
                expire_at, ledger, ledger_provenance, created_at
            ) VALUES (?, ?, ?, ?, 'jupiterz', ?, 200, ?, ?, 'server', ?)
            """,
            (digest, case_id, side, message, BLOCK, stamp + 60, ledger, stamp),
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
                publish = int(getattr(self, "_clock", NOW))
                return {"status": 200, "body": {
                    "parsed": [{
                        "id": "ef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d",
                        "price": {"price": "100", "conf": "0", "expo": 0, "publish_time": publish},
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
