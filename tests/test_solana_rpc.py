"""The default Solana RPC is Helius. The key is not logged and there is no public fallback."""
import io
import logging
import os
import unittest
import urllib.error
import urllib.request
from urllib.parse import urlparse

import config
import db
import solana_rpc
from config import install_secret_redaction
from tests.support import temp_desk_env


class SolanaRpcTests(unittest.TestCase):
    def setUp(self):
        _tmp, restore = temp_desk_env()
        self.addCleanup(restore)
        db.init_db()
        os.environ.pop("HELIUS_API_KEY", None)

    def test_the_default_is_helius_and_a_missing_key_does_not_post(self):
        self.assertEqual(config.DEFAULT_RPC_URL, "https://mainnet.helius-rpc.com")
        self.assertEqual(db.get_setting("rpc_url"), "https://mainnet.helius-rpc.com")
        info = solana_rpc.describe()
        self.assertEqual(info["provider"], "helius")
        self.assertEqual(info["host"], "mainnet.helius-rpc.com")
        self.assertFalse(info["ok"])
        self.assertEqual(info["reason"], "helius_api_key_missing")
        self.assertNotIn("api-key", info)
        calls = {"n": 0}

        def urlopen(request, timeout=8):
            calls["n"] += 1
            raise AssertionError("posted")

        original = urllib.request.urlopen
        urllib.request.urlopen = urlopen
        self.addCleanup(lambda: setattr(urllib.request, "urlopen", original))
        with self.assertRaises(solana_rpc.RpcRefused) as missing:
            solana_rpc.post({"jsonrpc": "2.0", "id": 1, "method": "getSlot", "params": []})
        self.assertEqual(missing.exception.reason, "helius_api_key_missing")
        self.assertEqual(calls["n"], 0)
        self.assertNotIn("api.mainnet-beta.solana.com", str(missing.exception))

    def test_an_override_is_used_and_the_key_is_redacted(self):
        os.environ["HELIUS_API_KEY"] = "helius-wire-secret"
        self.addCleanup(lambda: os.environ.pop("HELIUS_API_KEY", None))
        seen = {}

        def urlopen(request, timeout=8):
            seen["url"] = request.full_url
            raise urllib.error.URLError("down")

        original = urllib.request.urlopen
        urllib.request.urlopen = urlopen
        self.addCleanup(lambda: setattr(urllib.request, "urlopen", original))
        with self.assertRaises(solana_rpc.RpcRefused) as refused:
            solana_rpc.post({"jsonrpc": "2.0", "id": 1, "method": "getSlot", "params": []})
        self.assertEqual(refused.exception.reason, "rpc_transport")
        self.assertIsNone(refused.exception.__cause__)
        self.assertNotIn("helius-wire-secret", str(refused.exception))
        self.assertEqual(urlparse(seen["url"]).hostname, "mainnet.helius-rpc.com")
        install_secret_redaction()
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("desk.rpc.redact")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        self.addCleanup(lambda: logger.removeHandler(handler))
        logger.info("rpc %s", seen["url"])
        text = stream.getvalue()
        self.assertNotIn("helius-wire-secret", text)
        self.assertIn("api-key=[REDACTED]", text)
        db.set_setting("rpc_url", "https://override.example/solana", "test")
        seen.clear()

        def override_open(request, timeout=8):
            seen["url"] = request.full_url
            return _Response()

        urllib.request.urlopen = override_open
        posted = solana_rpc.post({"jsonrpc": "2.0", "id": 1, "method": "getHealth", "params": []})
        self.assertEqual(posted["status"], 200)
        self.assertEqual(urlparse(seen["url"]).hostname, "override.example")
        self.assertNotIn("api-key", seen["url"])
        self.assertNotIn("helius-wire-secret", seen["url"])
        self.assertTrue(solana_rpc.describe()["ok"])
        self.assertEqual(solana_rpc.describe()["provider"], "configured_rpc")


class _Response:
    status = 200

    def read(self):
        return b'{"result":"ok"}'

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False
