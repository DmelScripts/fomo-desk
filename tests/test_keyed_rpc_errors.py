"""Keyed RPC and DAS failures carry a reason code and not the key."""
import asyncio
import io
import json
import traceback
import unittest
import urllib.error
import urllib.request

import desk_providers
import public_market
import solana_read_rpc
import solana_rpc
from tests.support import temp_desk_env

SECRET = "helius-keyed-url-secret"
KEYED = "https://mainnet.helius-rpc.com/?api-key=" + SECRET
MINT = "Mint1111111111111111111111111111111111111"
CREATOR = "Creator11111111111111111111111111111111"


def _leaks(exc, secret: str) -> bool:
    seen = set()

    def walk(value) -> bool:
        if id(value) in seen:
            return False
        seen.add(id(value))
        if isinstance(value, BaseException):
            if value.__cause__ is not None or value.__context__ is not None:
                return True
            text = "".join(traceback.format_exception(type(value), value, value.__traceback__))
            if secret in text or secret in repr(value):
                return True
            return walk(value.args) or walk(getattr(value, "__dict__", {}))
        if isinstance(value, dict):
            return any(walk(key) or walk(item) for key, item in value.items())
        if isinstance(value, (list, tuple, set, frozenset)):
            return any(walk(item) for item in value)
        if isinstance(value, str):
            return secret in value
        if isinstance(value, (bytes, bytearray)):
            return secret.encode() in bytes(value)
        if isinstance(value, (int, float, bool, type(None))):
            return False
        return walk(getattr(value, "__dict__", {}))

    return walk(exc)


class _Body:
    def __init__(self, status, raw):
        self.status = status
        self._raw = raw

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class KeyedRpcErrorTests(unittest.TestCase):
    def setUp(self):
        _tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        import db
        import os
        db.init_db()
        os.environ["HELIUS_API_KEY"] = SECRET
        self.addCleanup(lambda: os.environ.pop("HELIUS_API_KEY", None))
        solana_rpc.TRANSPORT = None
        self.addCleanup(lambda: setattr(solana_rpc, "TRANSPORT", None))

    def _assert_clean(self, exc):
        self.assertFalse(_leaks(exc, SECRET))
        self.assertIsNone(exc.__cause__)
        self.assertIsNone(exc.__context__)

    def _urlopen(self, mode):
        def opener(request, timeout=8):
            if "solanatracker.io" in request.full_url:
                body = json.dumps({"token": {"creation": {"creator": CREATOR}}}).encode()
                return _Body(200, body)
            if mode == "json":
                return _Body(200, b"not-json " + KEYED.encode())
            if mode == "403":
                raise urllib.error.HTTPError(request.full_url, 403, "no", {}, io.BytesIO(b"denied " + KEYED.encode()))
            if mode == "timeout":
                raise TimeoutError("timed out " + SECRET)
            raise urllib.error.URLError("connect " + SECRET)
        return opener

    def _run(self, mode, call):
        original = urllib.request.urlopen
        urllib.request.urlopen = self._urlopen(mode)
        try:
            with self.assertRaises(Exception) as caught:
                call()
        finally:
            urllib.request.urlopen = original
        self._assert_clean(caught.exception)
        return caught.exception

    def test_solana_rpc_call_drops_the_key(self):
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getSlot", "params": []}
        bad = self._run("json", lambda: solana_rpc.call(payload))
        self.assertEqual(bad.reason, "rpc_bad_json")
        denied = self._run("403", lambda: solana_rpc.call(payload))
        self.assertEqual(denied.reason, "rpc_http_403")
        timed = self._run("timeout", lambda: solana_rpc.call(payload))
        self.assertEqual(timed.reason, "rpc_timeout")
        down = self._run("connect", lambda: solana_rpc.call(payload))
        self.assertEqual(down.reason, "rpc_transport")

    def test_http_transport_drops_the_key(self):
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getGenesisHash", "params": []}
        bad = self._run("json", lambda: solana_read_rpc.http_transport(payload))
        self.assertEqual(bad.reason, "rpc_bad_json")
        denied = self._run("403", lambda: solana_read_rpc.http_transport(payload))
        self.assertEqual(denied.reason, "http_status")
        timed = self._run("timeout", lambda: solana_read_rpc.http_transport(payload))
        self.assertEqual(timed.reason, "timeout")
        down = self._run("connect", lambda: solana_read_rpc.http_transport(payload))
        self.assertEqual(down.reason, "transport")

    def test_live_transport_drops_the_key(self):
        request = {"kind": "rpc", "method": "getAccountInfo", "mint": MINT}
        bad = self._run("json", lambda: public_market.live_transport(request))
        self.assertEqual(bad.reason, "rpc_bad_json")
        denied = self._run("403", lambda: public_market.live_transport(request))
        self.assertEqual(denied.reason, "rpc_status")
        timed = self._run("timeout", lambda: public_market.live_transport(request))
        self.assertEqual(timed.reason, "transport")
        down = self._run("connect", lambda: public_market.live_transport(request))
        self.assertEqual(down.reason, "transport")

    def test_das_urlopen_drops_the_key(self):
        import os
        import cross_source
        os.environ["SOLANA_TRACKER_API_KEY"] = "tracker-not-the-helius-key"
        self.addCleanup(lambda: os.environ.pop("SOLANA_TRACKER_API_KEY", None))
        desk_providers.set_transport(None)
        desk_providers.set_gap(0)
        self.addCleanup(lambda: desk_providers.set_gap(None))
        self.addCleanup(lambda: desk_providers.set_transport(None))

        def fetch():
            desk_providers.production_fetch("das", MINT)

        bad = self._run("json", fetch)
        self.assertIsInstance(bad, cross_source.FetchFailed)
        self.assertEqual(bad.reason, "rpc_bad_json")
        denied = self._run("403", fetch)
        self.assertEqual(denied.reason, "helius_forbidden")
        timed = self._run("timeout", fetch)
        self.assertEqual(timed.reason, "helius_timeout")
        down = self._run("connect", fetch)
        self.assertEqual(down.reason, "helius_status")

    def test_post_json_drops_the_key(self):
        import httpx
        from main import HttpxAdapter

        class Response:
            def __init__(self, status, bad=False):
                self.status_code = status
                self.url = KEYED
                self.request = type("Req", (), {"url": KEYED})()
                self._bad = bad

            def json(self):
                if self._bad:
                    raise json.JSONDecodeError("bad", "not-json " + KEYED, 0)
                return {"ok": True}

        class Client:
            def __init__(self, mode):
                self.mode = mode

            async def post(self, url, json=None, headers=None, timeout=None):
                if self.mode == "timeout":
                    raise httpx.TimeoutException("timed out " + KEYED)
                if self.mode == "connect":
                    raise httpx.ConnectError("connect " + KEYED)
                if self.mode == "403":
                    return Response(403)
                return Response(200, bad=True)

        def run(mode):
            adapter = HttpxAdapter(Client(mode))

            async def go():
                return await adapter.post_json(KEYED, {"jsonrpc": "2.0", "id": 1, "method": "getSlot"})

            with self.assertRaises(solana_rpc.RpcRefused) as caught:
                asyncio.run(go())
            self._assert_clean(caught.exception)
            return caught.exception

        self.assertEqual(run("json").reason, "rpc_bad_json")
        self.assertEqual(run("403").reason, "rpc_http_403")
        self.assertEqual(run("timeout").reason, "rpc_timeout")
        self.assertEqual(run("connect").reason, "rpc_transport")
