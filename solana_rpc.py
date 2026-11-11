"""The desk's Solana JSON-RPC target.

The default is Helius mainnet. Helius documents the key as the ``api-key``
query parameter. The key is attached only inside the POST and is not returned.
A missing key fails closed. An ``rpc_url`` override is used as stored. This
module does not fall back to the public mainnet endpoint.
"""

from __future__ import annotations

import json
import os
from urllib.parse import urlparse

HELIUS_RPC = "https://mainnet.helius-rpc.com"
HELIUS_HOST = "mainnet.helius-rpc.com"
KEY_ENV = "HELIUS_API_KEY"
# Tests may install a transport. Production leaves this unset.
TRANSPORT = None


class RpcRefused(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def configured_url() -> str:
    import config
    import db
    raw = db.get_setting("rpc_url")
    if type(raw) is not str or raw.strip() == "":
        return config.DEFAULT_RPC_URL
    return raw.strip()


def describe() -> dict:
    """Host and provider only. Never the key or a URL that carries it."""
    host = urlparse(configured_url()).hostname or ""
    helius = host == HELIUS_HOST
    present = _key() is not None
    return {
        "ok": (not helius) or present,
        "reason": None if (not helius or present) else "helius_api_key_missing",
        "provider": "helius" if helius else "configured_rpc",
        "host": host,
        "uses_helius_key": helius,
        "helius_key_present": present if helius else False,
    }


def post(payload: dict) -> dict:
    """One JSON-RPC POST. A failure reason does not include the URL."""
    if callable(TRANSPORT):
        result = TRANSPORT(payload)
        if not isinstance(result, dict) or type(result.get("status")) is not int:
            raise RpcRefused("rpc_transport")
        return result
    info = describe()
    if info["ok"] is not True:
        raise RpcRefused("helius_api_key_missing")
    import urllib.error
    import urllib.request
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        _wire_url(), data=data, headers={"content-type": "application/json", "accept": "application/json"}, method="POST",
    )
    status = None
    raw = b""
    reason = None
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            status = response.status
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            raw = exc.read()
        except Exception:
            raw = b""
    except TimeoutError:
        reason = "rpc_timeout"
    except urllib.error.URLError as exc:
        reason = "rpc_timeout" if isinstance(getattr(exc, "reason", None), TimeoutError) else "rpc_transport"
    except Exception:
        reason = "rpc_transport"
    if reason is not None:
        raise RpcRefused(reason)
    try:
        body = json.loads(raw.decode("utf-8")) if raw else None
    except Exception:
        body = None
    return {"status": status, "body": body}


def call(payload: dict) -> dict:
    """The JSON-RPC object. HTTP failures are reason codes with no request URL."""
    info = describe()
    if info["ok"] is not True:
        raise RpcRefused("helius_api_key_missing")
    posted = post(payload)
    status = posted.get("status")
    if type(status) is not int or status != 200:
        code = status if type(status) is int else 0
        raise RpcRefused("rpc_http_%s" % code)
    body = posted.get("body")
    if not isinstance(body, dict):
        raise RpcRefused("rpc_transport")
    return body


def _wire_url() -> str:
    """The POST target. Do not log, store, or put this string in an exception."""
    base = configured_url()
    if urlparse(base).hostname != HELIUS_HOST:
        return base
    key = _key()
    if key is None:
        raise RpcRefused("helius_api_key_missing")
    joiner = "&" if "?" in base else "?"
    return base + joiner + "api-key=" + key


def _key() -> str | None:
    value = os.environ.get(KEY_ENV)
    if type(value) is not str or value.strip() == "":
        return None
    return value.strip()
