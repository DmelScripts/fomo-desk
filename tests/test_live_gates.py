"""Readiness gates stay open unless this process parsed a live provider body."""
import http.client
import io
import json
import os
import time
import unittest
import urllib.request

import desk_loop
import live_gate_proof
import solana_tx
from tests.support import temp_desk_env

MINT_A = "JEH7cJxAKdprFG5AvdsY2c4ZqojxLBjFmw19zADV6oK5"
MINT_B = "2qEHjDLDLbuBgRYvsxhc5D6uDWAivNFZGan56P1tpump"


def _redirected_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """Runs with the socket module's globals. A redirect tuple replaces the peer."""
    redir = globals().get("_proof_redirect")
    if redir is not None:
        host, port = redir
    addrlist = []
    for res in _socket.getaddrinfo(host, port, family, type, proto, flags):
        af, socktype, proto, canonname, sa = res
        addrlist.append((_intenum_converter(af, AddressFamily), _intenum_converter(socktype, SocketKind), proto, canonname, sa))
    return addrlist


def _open_blocked_socket(*args, **kwargs):
    """Same function object as the suite's blocked opener, with a real TCP connect."""
    import _socket
    import socket
    if args and isinstance(args[0], socket.socket):
        return getattr(_socket.socket, "connect")(args[0], args[1])
    address = args[0]
    timeout = args[1] if len(args) > 1 else kwargs.get("timeout", None)
    host, port = address
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    err = None
    for family, typ, proto, _canon, sockaddr in infos:
        raw = socket.socket(family, typ, proto)
        try:
            if isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
                raw.settimeout(timeout)
            getattr(_socket.socket, "connect")(raw, sockaddr)
            return raw
        except OSError as exc:
            err = exc
            raw.close()
    if err is not None:
        raise err
    raise OSError("connect failed")


def _response(body: bytes):
    raw = b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body

    class Sock:
        def makefile(self, *args, **kwargs):
            return io.BytesIO(raw)

        def close(self):
            return None

    opened = http.client.HTTPResponse(Sock())
    opened.begin()
    return opened


class LiveGateTests(unittest.TestCase):
    def setUp(self):
        _tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        self.addCleanup(live_gate_proof._reset_for_tests)

    def test_provider_gates_stay_open_without_a_live_parse(self):
        gates = {item["gate"]: item for item in desk_loop.remaining_live_gates()}
        for name in (
            "lp_lock_live",
            "rug_history_live",
            "holder_growth_live",
            "bundle_unique_buyers_smart_wallets_dev_holding",
            "price_freshness_proven",
            "quote_expiry",
        ):
            self.assertFalse(gates[name]["closed"], name)
        self.assertNotIn("stalkchain_rest_envelope", gates)
        view = desk_loop.readiness()
        removed = view["removed_live_gates"]
        self.assertEqual(removed[0]["gate"], "stalkchain_rest_envelope")
        self.assertEqual(removed[0]["owner"], "Thành")
        self.assertEqual(removed[0]["decided_at"], "2026-11-11 22:23 Asia/Saigon")
        self.assertEqual(
            removed[0]["facts"],
            ["lp_lock", "rug_history", "holder_growth", "bundles", "dev_holding"],
        )
        self.assertNotIn("stalkchain_response_fields_for_lp_rug_growth_are_unpublished", view["blocked_because"])
        self.assertFalse(view["live_ready"])
        self.assertFalse(view["sign"])
        self.assertFalse(view["send"])
        self.assertFalse(view["execution_eligible"])
        self.assertFalse(view["preflight_ready"])
        self.assertFalse(view["gate_complete"])

    def test_a_passed_in_body_cannot_close_a_gate(self):
        self.assertFalse(hasattr(live_gate_proof, "consider"))
        self.assertFalse(hasattr(live_gate_proof, "consider_jupiter"))
        self.assertFalse(hasattr(live_gate_proof, "note_history"))
        opened = _response(b'{"lpLockedPct": 100}')
        live_gate_proof._from_fetch(
            opened,
            "https://api.rugcheck.xyz/v1/tokens/" + MINT_A + "/report/summary",
            {"lpLockedPct": 100},
        )
        live_gate_proof._seal_history(MINT_A, [], True)
        url = "https://api.rugcheck.xyz/v1/tokens/" + MINT_A + "/report/summary"
        body = {"lpLockedPct": 100}
        raw = b'{"lpLockedPct": 100}'
        live_gate_proof._apply(url, body)
        live_gate_proof._apply(url, body, None, time.time())
        live_gate_proof._record(url, raw, None, None)
        live_gate_proof._plan(url, body, None, time.time(), None, {})
        skip = {"exchange", "_owned_https", "http_client"}
        for name in dir(live_gate_proof):
            if name.startswith("__") or name in skip:
                continue
            helper = getattr(live_gate_proof, name)
            if not callable(helper) or isinstance(helper, type):
                continue
            for args in ((url, body), (url, body, None, time.time()), (url, raw, None, None)):
                try:
                    helper(*args)
                except Exception:
                    pass
        self.assertFalse(live_gate_proof.closed("lp_lock_live", MINT_A))
        self.assertFalse(live_gate_proof.closed("rug_history_live", MINT_A))
        self.assertFalse(any(isinstance(row, dict) and row.get("ok") is True for row in live_gate_proof._NOTES.values()))

    def test_a_replaced_connection_class_or_a_planted_note_stays_open(self):
        original = live_gate_proof._DeskHTTPSConnection

        class Fake(original):
            def connect(self):
                self._proof_host = self.host

            def request(self, method, path, body=None, headers=None):
                self._raw = b'{"lpLockedPct": 100}'

            def getresponse(self):
                raw = self._raw

                class Response:
                    status = 200

                    def read(self_response):
                        return raw

                return Response()

            def close(self):
                return None

        live_gate_proof._DeskHTTPSConnection = Fake
        self.addCleanup(lambda: setattr(live_gate_proof, "_DeskHTTPSConnection", original))
        url = "https://api.rugcheck.xyz/v1/tokens/" + MINT_A + "/report/summary"
        try:
            desk_loop.public_reader({"method": "GET", "url": url})
        except Exception:
            pass
        self.assertFalse(live_gate_proof.closed("lp_lock_live", MINT_A))
        view = live_gate_proof._NOTES
        with self.assertRaises(TypeError):
            view[("lp_lock_live", MINT_A)] = {"ok": True, "at": time.time(), "until": None}
        live_gate_proof._NOTES = {
            ("lp_lock_live", MINT_A): {"ok": True, "at": time.time(), "until": None, "mac": "0" * 64},
        }
        self.addCleanup(lambda: setattr(live_gate_proof, "_NOTES", view))
        self.assertFalse(live_gate_proof.closed("lp_lock_live", MINT_A))

    def test_replacing_each_bound_attribute_leaves_the_live_gates_open(self):
        import hmac
        import hashlib
        live = {
            "lp_lock_live",
            "rug_history_live",
            "holder_growth_live",
            "bundle_unique_buyers_smart_wallets_dev_holding",
            "price_freshness_proven",
            "quote_expiry",
        }
        names = live_gate_proof.monitored()

        def gates_open(label):
            rows = {item["gate"]: item["closed"] for item in desk_loop.remaining_live_gates(MINT_A)}
            for name in live:
                self.assertFalse(rows[name], label + ":" + name)
            self.assertTrue(rows["ticket_limit_proven"], label)

        gates_open("before")
        for name in names:
            if name in {"_note", "handle_request"}:
                owner = live_gate_proof._OwnedTransport
                original = getattr(owner, name)
                setattr(owner, name, lambda *args, **kwargs: True)
                try:
                    self.assertFalse(live_gate_proof.attributes_intact(), name)
                    gates_open(name)
                finally:
                    setattr(owner, name, original)
                continue
            original = getattr(live_gate_proof, name)
            setattr(live_gate_proof, name, (lambda *args, **kwargs: True) if name != "FRESH_S" else 10**9)
            try:
                if name != "attributes_intact":
                    self.assertFalse(live_gate_proof.attributes_intact(), name)
                if name != "closed":
                    self.assertFalse(live_gate_proof.closed("lp_lock_live", MINT_A), name)
                gates_open(name)
            finally:
                setattr(live_gate_proof, name, original)
        import json
        import socket
        import time
        import public_filter_docs
        import cross_source
        import pyth_sponsored
        swapped = (
            (hmac, "new", lambda *args, **kwargs: original_hmac(b"x", b"y")),
            (hmac, "compare_digest", lambda *args, **kwargs: True),
            (hashlib, "sha256", lambda *args, **kwargs: original_sha(b"")),
            (time, "time", lambda: 0),
            (json, "dumps", lambda *args, **kwargs: "{}"),
            (json, "loads", lambda *args, **kwargs: {"lpLockedPct": 100}),
            (public_filter_docs, "parse_holder_chart", lambda *args, **kwargs: {"ok": True, "delta": 5}),
            (cross_source, "bundle_check", lambda *args, **kwargs: {"ok": True}),
            (cross_source, "dev_check", lambda *args, **kwargs: {"status": "PASS"}),
            (pyth_sponsored, "assess_signer_price", lambda *args, **kwargs: {"ok": True}),
            (socket, "getaddrinfo", lambda *args, **kwargs: ()),
        )
        original_hmac = hmac.new
        original_sha = hashlib.sha256
        for owner, attr, replacement in swapped:
            original = getattr(owner, attr)
            setattr(owner, attr, replacement)
            try:
                self.assertFalse(live_gate_proof.attributes_intact(), attr)
                self.assertFalse(live_gate_proof.closed("lp_lock_live", MINT_A), attr)
                gates_open(attr)
            finally:
                setattr(owner, attr, original)

    def test_cgnat_is_rejected_and_a_replaced_connect_stays_open(self):
        import ssl
        self.assertFalse(live_gate_proof._global_address("100.64.1.1"))
        self.assertFalse(live_gate_proof._global_address("::ffff:100.64.1.1"))
        self.assertFalse(live_gate_proof._global_address("10.1.1.1"))
        self.assertFalse(live_gate_proof._global_address("127.0.0.1"))
        self.assertTrue(live_gate_proof._global_address("8.8.8.8"))
        self.assertTrue(live_gate_proof._global_address("2001:4860:4860::8888"))
        self.assertTrue(live_gate_proof._global_address("::ffff:8.8.8.8"))
        live = {
            "lp_lock_live",
            "rug_history_live",
            "holder_growth_live",
            "bundle_unique_buyers_smart_wallets_dev_holding",
            "price_freshness_proven",
            "quote_expiry",
        }

        def gates_open(label):
            rows = {item["gate"]: item["closed"] for item in desk_loop.remaining_live_gates(MINT_A)}
            for name in live:
                self.assertFalse(rows[name], label + ":" + name)
            self.assertTrue(rows["ticket_limit_proven"], label)

        for owner, attr in (
            (http.client.HTTPSConnection, "connect"),
            (http.client.HTTPConnection, "connect"),
            (ssl.SSLContext, "wrap_socket"),
            (ssl.SSLContext, "load_verify_locations"),
        ):
            original = getattr(owner, attr)
            setattr(owner, attr, lambda *args, **kwargs: None)
            try:
                self.assertFalse(live_gate_proof.attributes_intact(), attr)
                gates_open(attr)
            finally:
                setattr(owner, attr, original)

    def test_replacing_each_desk_method_leaves_the_live_gates_open(self):
        import types
        live = {
            "lp_lock_live",
            "rug_history_live",
            "holder_growth_live",
            "bundle_unique_buyers_smart_wallets_dev_holding",
            "price_freshness_proven",
            "quote_expiry",
        }

        def gates_open(label):
            rows = {item["gate"]: item["closed"] for item in desk_loop.remaining_live_gates(MINT_A)}
            for name in live:
                self.assertFalse(rows[name], label + ":" + name)
            self.assertTrue(rows["ticket_limit_proven"], label)

        classes = (live_gate_proof._DeskHTTPSConnection, live_gate_proof._OwnedTransport)
        replaced = False
        for cls in classes:
            for name, value in tuple(cls.__dict__.items()):
                if not isinstance(value, types.FunctionType):
                    continue
                replaced = True
                setattr(cls, name, lambda *args, **kwargs: None)
                try:
                    self.assertFalse(live_gate_proof.attributes_intact(), cls.__name__ + "." + name)
                    self.assertFalse(live_gate_proof.closed("lp_lock_live", MINT_A), name)
                    gates_open(cls.__name__ + "." + name)
                finally:
                    setattr(cls, name, value)
        self.assertTrue(replaced)
        self.assertTrue(live_gate_proof.attributes_intact())

    def test_a_local_certificate_and_redirected_dns_stay_open(self):
        import socket
        import ssl
        import subprocess
        import tempfile
        import threading
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", str(root / "ca.key"), "-out", str(root / "ca.pem"), "-days", "1", "-nodes", "-subj", "/CN=Desk Test CA"],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["openssl", "req", "-newkey", "rsa:2048", "-keyout", str(root / "server.key"), "-out", str(root / "server.csr"), "-nodes", "-subj", "/CN=api.rugcheck.xyz"],
                check=True, capture_output=True,
            )
            ext = root / "ext.cnf"
            ext.write_text("subjectAltName=DNS:api.rugcheck.xyz\n")
            subprocess.run(
                ["openssl", "x509", "-req", "-in", str(root / "server.csr"), "-CA", str(root / "ca.pem"), "-CAkey", str(root / "ca.key"), "-CAcreateserial", "-out", str(root / "server.pem"), "-days", "1", "-extfile", str(ext)],
                check=True, capture_output=True,
            )
            body = b'{"lpLockedPct": 100}'
            server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_ctx.load_cert_chain(root / "server.pem", root / "server.key")
            listener = socket.socket()
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(5)
            self.addCleanup(listener.close)
            port = listener.getsockname()[1]
            served = threading.Event()
            finished = threading.Event()

            def serve():
                try:
                    raw, _addr = listener.accept()
                except OSError:
                    return
                try:
                    wrapped = server_ctx.wrap_socket(raw, server_side=True)
                    data = b""
                    while b"\r\n\r\n" not in data:
                        chunk = wrapped.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    payload = (
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                        + str(len(body)).encode()
                        + b"\r\nConnection: keep-alive\r\n\r\n"
                        + body
                    )
                    wrapped.sendall(payload)
                    served.set()
                    finished.wait(5)
                    wrapped.close()
                except Exception:
                    return

            threading.Thread(target=serve, daemon=True).start()
            bundle = Path(live_gate_proof._CA_FILE)
            original_bundle = bundle.read_bytes()
            bundle.write_bytes(original_bundle + b"\n" + (root / "ca.pem").read_bytes())
            self.addCleanup(lambda: bundle.write_bytes(original_bundle))
            for value in vars(live_gate_proof).values():
                if isinstance(value, ssl.SSLContext):
                    value.load_verify_locations(cafile=str(root / "ca.pem"))
            original_lookup = socket.getaddrinfo.__code__
            opener = socket.create_connection
            original_open = opener.__code__
            socket._proof_redirect = ("127.0.0.1", port)
            socket.getaddrinfo.__code__ = _redirected_getaddrinfo.__code__
            opener.__code__ = _open_blocked_socket.__code__
            try:
                self.assertTrue(live_gate_proof.attributes_intact())
                try:
                    desk_loop.public_reader({
                        "method": "GET",
                        "url": "https://api.rugcheck.xyz/v1/tokens/%s/report/summary" % MINT_A,
                    })
                except Exception:
                    pass
                self.assertTrue(served.wait(3))
                self.assertTrue(live_gate_proof.attributes_intact())
                self.assertFalse(live_gate_proof.closed("lp_lock_live", MINT_A))
                rows = {item["gate"]: item["closed"] for item in desk_loop.remaining_live_gates(MINT_A)}
                self.assertFalse(rows["lp_lock_live"])
            finally:
                finished.set()
                socket.getaddrinfo.__code__ = original_lookup
                opener.__code__ = original_open
                socket._proof_redirect = None

    def test_a_transport_fixture_does_not_close_the_provider_gates(self):
        import desk_providers
        desk_providers.set_transport(lambda spec: {"status": 200, "body": {"lpLockedPct": 100}, "_live": True})
        self.addCleanup(lambda: desk_providers.set_transport(None))
        desk_providers.production_fetch("rugcheck", MINT_A)
        self.assertFalse(live_gate_proof.closed("lp_lock_live", MINT_A))

    def test_lp_values_must_cover_exactly_100_inside_the_range(self):
        for body in (
            {"lpLockedPct": 100},
            {"lpBurn": 100},
            {"lpBurn": 60, "lpLockedPct": 40},
            {"lpBurn": 100.0, "lpLockedPct": 0},
            {"lpBurn": 0, "lpLockedPct": 100},
        ):
            self.assertTrue(live_gate_proof._lp_ok(body), body)
        for body in (
            {"lpLockedPct": 99},
            {"lpLockedPct": "100"},
            {"lpLockedPct": True},
            {"lpBurn": 100, "lpLockedPct": 100},
            {"lpBurn": -1, "lpLockedPct": 101},
            {"lpBurn": 101},
            {"lpLockedPct": 101},
            {"lpBurn": -1},
            {"lpLockedPct": float("nan")},
            {"lpBurn": float("inf"), "lpLockedPct": float("-inf")},
            {"lpBurn": float("nan"), "lpLockedPct": float("nan")},
            {"lpBurn": 50},
            {},
        ):
            self.assertFalse(live_gate_proof._lp_ok(body), body)

    def test_a_urlopen_subclass_does_not_close_lp_lock(self):
        class Opened(http.client.HTTPResponse):
            def __init__(self):
                self.status = 200
                self._body = io.BytesIO(b'{"lpLockedPct": 100, "lpBurn": 0}')

            def read(self, amt=None):
                return self._body.read() if amt is None else self._body.read(amt)

            def close(self):
                return None

        original = urllib.request.urlopen
        urllib.request.urlopen = lambda request, timeout=8: Opened()
        self.addCleanup(lambda: setattr(urllib.request, "urlopen", original))
        url = "https://api.rugcheck.xyz/v1/tokens/" + MINT_A + "/report/summary"
        body = desk_loop.public_reader({"method": "GET", "url": url})
        self.assertEqual(body["body"]["lpLockedPct"], 100)
        self.assertFalse(live_gate_proof.closed("lp_lock_live", MINT_A))
        self.assertFalse(live_gate_proof.closed("lp_lock_live", MINT_B))

    def test_a_quote_and_a_price_belong_to_one_mint(self):
        import desk_providers
        now = time.time()
        os.environ["PYTH_API_KEY"] = "pyth-test-not-a-secret"
        self.addCleanup(lambda: os.environ.pop("PYTH_API_KEY", None))
        previous = desk_providers._GAP
        desk_providers._GAP = 0
        self.addCleanup(lambda: setattr(desk_providers, "_GAP", previous))
        original = urllib.request.urlopen
        hermes = {"parsed": [{
            "id": "ef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d",
            "price": {"price": "100", "conf": "0", "expo": 0, "publish_time": int(now)},
            "status": "trading",
        }]}

        def urlopen(request, timeout=8):
            return _response(json.dumps(hermes).encode())

        urllib.request.urlopen = urlopen
        self.addCleanup(lambda: setattr(urllib.request, "urlopen", original))
        desk_providers.fetch_hermes(now, MINT_A)
        self.assertFalse(live_gate_proof.closed("price_freshness_proven", MINT_A, now=time.time()))
        self.assertFalse(live_gate_proof.closed("price_freshness_proven", MINT_B, now=time.time()))
        order = "https://api.jup.ag/swap/v2/order?inputMint=%s&outputMint=%s" % (solana_tx.NATIVE_MINT, MINT_A)
        quote = {"expireAt": int(now) + 30, "inputMint": solana_tx.NATIVE_MINT, "outputMint": MINT_A}
        urllib.request.urlopen = lambda request, timeout=8: _response(json.dumps(quote).encode())
        desk_loop.public_reader({"method": "GET", "url": order})
        moment = time.time()
        self.assertFalse(live_gate_proof.closed(
            "quote_expiry", MINT_A, now=moment, input_mint=solana_tx.NATIVE_MINT, output_mint=MINT_A,
        ))
        self.assertFalse(live_gate_proof.closed(
            "quote_expiry", MINT_B, now=moment, input_mint=solana_tx.NATIVE_MINT, output_mint=MINT_A,
        ))
        self.assertEqual(desk_loop._live_trade_proofs({"mint": MINT_A}, {"side": "entry"}, moment), "price_unknown")
        self.assertEqual(desk_loop._live_trade_proofs({"mint": MINT_B}, {"side": "entry"}, moment), "price_unknown")

    def test_a_fabricated_httpx_response_does_not_close_the_quote(self):
        import httpx
        import public_market
        now = int(time.time())
        quote = {"expireAt": now + 60, "inputMint": solana_tx.NATIVE_MINT, "outputMint": MINT_A}
        url = "https://api.jup.ag/swap/v2/order?inputMint=%s&outputMint=%s" % (solana_tx.NATIVE_MINT, MINT_A)
        original_get = httpx.get
        httpx.get = lambda *args, **kwargs: httpx.Response(200, json=quote)
        self.addCleanup(lambda: setattr(httpx, "get", original_get))
        try:
            public_market.live_transport({"url": url})
        except public_market.PublicStop:
            pass
        self.assertFalse(live_gate_proof.closed(
            "quote_expiry", MINT_A, input_mint=solana_tx.NATIVE_MINT, output_mint=MINT_A,
        ))
        client, _transport = public_market._http_client()
        self.assertIs(client, live_gate_proof.http_client())
        original_client_get = client.get

        def fabricated(request_url, timeout=8.0, follow_redirects=False, headers=None):
            return httpx.Response(200, json=quote)

        client.get = fabricated
        self.addCleanup(lambda: setattr(client, "get", original_client_get))
        returned = public_market.live_transport({"url": url})
        self.assertEqual(returned["body"]["expireAt"], quote["expireAt"])
        self.assertFalse(live_gate_proof.closed(
            "quote_expiry", MINT_A, input_mint=solana_tx.NATIVE_MINT, output_mint=MINT_A,
        ))
        saved_client = live_gate_proof._CLIENT

        def handler(request):
            return httpx.Response(200, json=quote)

        live_gate_proof._CLIENT = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(lambda: setattr(live_gate_proof, "_CLIENT", saved_client))
        mocked = public_market.live_transport({"url": url})
        self.assertEqual(mocked["status"], 200)
        self.assertFalse(live_gate_proof.closed(
            "quote_expiry", MINT_A, input_mint=solana_tx.NATIVE_MINT, output_mint=MINT_A,
        ))
        import ssl
        from httpcore._backends.sync import SyncStream

        class PretendSocket(ssl.SSLSocket):
            def __new__(cls):
                import socket
                raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self = ssl.SSLSocket.__new__(cls, raw)
                self._raw = raw
                return self

            def getpeername(self):
                return ("203.0.113.10", 443)

            def getpeercert(self, binary_form=False):
                return {"subjectAltName": (("DNS", "api.jup.ag"),)}

        pretend = PretendSocket.__new__(PretendSocket)
        self.addCleanup(pretend._raw.close)
        crafted = httpx.Response(
            200,
            json=quote,
            extensions={"network_stream": SyncStream(pretend)},
            request=httpx.Request("GET", url),
        )
        owned = saved_client._transport if saved_client is not None else live_gate_proof.http_client()._transport
        # The stand-in is what get() returns, so the owned transport's read never runs.
        live_gate_proof._CLIENT = saved_client if saved_client is not None else live_gate_proof.http_client()
        current = live_gate_proof.http_client()
        current.get = lambda *args, **kwargs: crafted
        self.addCleanup(lambda: current.__dict__.pop("get", None))
        stood = public_market.live_transport({"url": url})
        self.assertEqual(stood["body"]["expireAt"], quote["expireAt"])
        self.assertFalse(live_gate_proof.closed(
            "quote_expiry", MINT_A, input_mint=solana_tx.NATIVE_MINT, output_mint=MINT_A,
        ))
        # Even if that response is handed to the owned transport, the socket is not ssl.SSLSocket.
        if type(owned) is live_gate_proof._OwnedTransport:
            owned._note(crafted, httpx.Request("GET", url))
        self.assertFalse(live_gate_proof.closed(
            "quote_expiry", MINT_A, input_mint=solana_tx.NATIVE_MINT, output_mint=MINT_A,
        ))

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

    def test_quote_age_preflight_and_auth_standins_stay_open(self):
        import phantom_auth
        now = 1_700_000_000.0
        fresh = {"ok": True, "at": now - 9, "until": now + 30}
        stale = {"ok": True, "at": now - 11, "until": now + 30}
        expired = {"ok": True, "at": now - 1, "until": now}
        self.assertEqual(live_gate_proof.quote_max_age(), 10.0)
        self.assertTrue(live_gate_proof.quote_age_ok(fresh, now, 10))
        self.assertFalse(live_gate_proof.quote_age_ok(stale, now, 10))
        self.assertFalse(live_gate_proof.quote_age_ok(expired, now, 10))
        os.environ["DESK_QUOTE_MAX_AGE_S"] = "not-a-number"
        self.addCleanup(lambda: os.environ.pop("DESK_QUOTE_MAX_AGE_S", None))
        self.assertEqual(live_gate_proof.quote_max_age(), 10.0)
        self.assertFalse(live_gate_proof.quote_ready(MINT_A, now))
        self.assertFalse(live_gate_proof.preflight_observed(MINT_A, now))
        self.assertFalse(live_gate_proof.preflight_observed(digest="abc", now=now))
        live_gate_proof._plan(
            "https://api.jup.ag/swap/v2/order?inputMint=So11111111111111111111111111111111111111112&outputMint=" + MINT_A,
            {"inputMint": "So11111111111111111111111111111111111111112", "outputMint": MINT_A, "outAmount": "10", "expireAt": int(now) + 30},
            MINT_A,
            now,
            None,
            {},
        )
        live_gate_proof._apply(
            "https://mainnet.helius-rpc.com/?api-key=hidden",
            {"result": {"value": {"err": None, "fee": 5000, "logs": []}}},
        )
        self.assertFalse(live_gate_proof.quote_ready(MINT_A, now))
        self.assertFalse(live_gate_proof.preflight_observed())
        import db
        db.init_db()
        gates = {item["gate"]: item["closed"] for item in desk_loop.remaining_live_gates(MINT_A)}
        self.assertFalse(gates["quote_expiry"])
        self.assertFalse(gates["preflight_ready"])
        self.assertFalse(desk_loop.simulation_allowed())
        blocked = desk_loop.live_simulate("case", "digest", now)
        self.assertEqual(blocked["reason"], "simulation_unreachable")
        self.assertFalse(blocked["preflight_ready"])
        self.assertFalse(blocked["sign"])
        self.assertFalse(blocked["send"])
        self.assertFalse(blocked["network_called"])
        os.environ["DESK_SIM_DRY_RUN"] = "1"
        self.addCleanup(lambda: os.environ.pop("DESK_SIM_DRY_RUN", None))
        import urllib.request
        original = urllib.request.urlopen

        def stand_in(request, timeout=8):
            class Response:
                status = 200

                def read(self):
                    return b'{"result":{"value":{"err":null,"fee":1,"logs":[]}}}'

                def close(self):
                    return None

            return Response()

        urllib.request.urlopen = stand_in
        self.addCleanup(lambda: setattr(urllib.request, "urlopen", original))
        db.set_setting("mode", "LIVE", "test")
        db.set_setting("agents_enabled", "1", "test")
        forged = desk_loop.live_simulate("missing", "digest", now)
        self.assertFalse(forged["preflight_ready"])
        self.assertFalse(forged["sign"])
        self.assertFalse(live_gate_proof.preflight_observed())
        urllib.request.urlopen = original
        original_run = phantom_auth.subprocess.run

        def fake_run(*args, **kwargs):
            class Done:
                returncode = 0
                stdout = b'{"walletId":"eba28e4f-6b99-4543-af72-c7d1b008ce4e","addresses":[{"addressType":"Solana","address":"7AFJbqsX2UeK7ZW5M1gbXWjiKCai2CJWHpCzXe7woC7t"}]}'
                stderr = b""

            return Done()

        phantom_auth.subprocess.run = fake_run
        try:
            started = phantom_auth.reverify_on_startup()
            self.assertFalse(started["ok"])
            self.assertFalse(started["sign"])
            self.assertFalse(phantom_auth.server_auth_closed())
            gate = next(item for item in desk_loop.remaining_live_gates() if item["gate"] == "phantom_server_auth")
            self.assertFalse(gate["closed"])
        finally:
            phantom_auth.subprocess.run = original_run
        phantom_auth._apply_classification({
            "state": "observed",
            "wallet_id": "eba28e4f-6b99-4543-af72-c7d1b008ce4e",
            "address": "7AFJbqsX2UeK7ZW5M1gbXWjiKCai2CJWHpCzXe7woC7t",
            "verify": {},
        })
        self.assertFalse(phantom_auth.server_auth_closed())
