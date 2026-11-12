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
            "body": {"result": signature} if payload["method"] == "sendTransaction" else {"result": {"value": [{
                "confirmationStatus": "confirmed", "err": None, "slot": 9,
            }]}},
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
                body = {
                    "inputMint": solana_tx.NATIVE_MINT,
                    "outputMint": MINT,
                    "inAmount": amount,
                    "outAmount": quoted,
                    "expireAt": int(time.time()) + 120,
                }
                return json.dumps(body).encode()
            sent = json.loads(rest.decode() or "{}")
            encoded = sent["params"][0]
            tx = solana_tx.decode_transaction(base64.b64decode(encoded))
            width = len(tx.static_keys)
            value = {
                "err": None,
                "logs": ["Program log: Instruction: Swap"],
                "unitsConsumed": 8000,
                "fee": 5000,
                "preTokenBalances": [{
                    "accountIndex": 2, "mint": MINT, "owner": tx.fee_payer,
                    "uiTokenAmount": {"amount": "0", "decimals": 6},
                }],
                "postTokenBalances": [{
                    "accountIndex": 2, "mint": MINT, "owner": tx.fee_payer,
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
            jupiter_swap_v2.order_url(solana_tx.NATIVE_MINT, MINT, amount),
            headers={"accept": "application/json"},
        )
        status, _raw, proved = live_gate_proof.exchange(quote, 8, MINT)
        self.assertTrue(proved, status)
        self.assertTrue(live_gate_proof.quote_ready(MINT, time.time(), input_mint=solana_tx.NATIVE_MINT, output_mint=MINT))

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

    def _message(self, swap: bytes, price: int = 1_000, reserved: str = "1.00", when: float | None = None):
        values, accounts = _pool(solana_tx.NATIVE_MINT, MINT)
        message = _tx(_metas(solana_tx.NATIVE_MINT, MINT, values), swap, price=price, blockhash=BLOCK)
        digest = live_submit.message_digest(message)
        case_id = "case-%s-%s" % (digest[:8], reserved.replace(".", ""))
        self._store(digest, message, case_id, reserved, when=NOW if when is None else when)
        self._accounts = accounts
        return case_id, message

    def _store(self, digest: str, message: str, case_id: str, reserved: str = "1.00", when: float | None = None) -> None:
        stamp = NOW if when is None else when
        db.execute(
            """
            INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, ledger, ledger_provenance, created_at, updated_at)
            VALUES (?, ?, 'protected', ?, 'paper', 'server', ?, ?)
            """,
            (case_id, MINT, '{"reserved_usd": "%s"}' % reserved, stamp, stamp),
        )
        db.execute(
            """
            INSERT INTO memecoin_reservations (
                message_sha256, case_id, side, message_b64, router, blockhash, last_valid_block_height,
                expire_at, ledger, ledger_provenance, created_at
            ) VALUES (?, ?, 'entry', ?, 'jupiterz', ?, 200, ?, 'paper', 'server', ?)
            """,
            (digest, case_id, message, BLOCK, stamp + 60, stamp),
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
