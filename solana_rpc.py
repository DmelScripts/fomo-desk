"""The desk's Solana JSON-RPC target.

The default is Helius mainnet. Helius documents the key as the ``api-key``
query parameter. The key is attached only inside the POST and is not returned.
A missing key fails closed. An ``rpc_url`` override is used as stored. This
module does not fall back to the public mainnet endpoint.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

    def http_error_302(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, "redirect", headers, fp)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


_STOCK_URLOPEN = urllib.request.urlopen
_NO_REDIRECT = urllib.request.build_opener(_NoRedirect)


def _open(request: urllib.request.Request, timeout: float):
    current = urllib.request.urlopen
    if current is not _STOCK_URLOPEN:
        return current(request, timeout=timeout)
    return _NO_REDIRECT.open(request, timeout=timeout)


def _refuse(reason: str):
    raise RpcRefused(reason)


def exchange(url: str, data: bytes, *, timeout: float = 8, headers: dict | None = None, max_bytes: int | None = None) -> dict:
    """POST one URL. A failure is a reason code: no URL and no body on the exception."""
    reason, result = _exchange_once(url, data, timeout, headers, max_bytes)
    del url, data, headers
    if reason is not None:
        _refuse(reason)
    return result


def _exchange_once(url: str, data: bytes, timeout: float, headers: dict | None, max_bytes: int | None):
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers or {"content-type": "application/json", "accept": "application/json"},
        method="POST",
    )
    status = None
    raw = b""
    reason = None
    try:
        with _open(request, timeout) as response:
            status = getattr(response, "status", None)
            raw = response.read()
    except TimeoutError:
        reason = "rpc_timeout"
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            raw = exc.read() or b""
        except Exception:
            raw = b""
    except urllib.error.URLError as exc:
        reason = "rpc_timeout" if isinstance(getattr(exc, "reason", None), TimeoutError) else "rpc_transport"
    except Exception:
        reason = "rpc_transport"
    if reason is not None:
        return reason, None
    if status in {301, 302, 303, 307, 308}:
        return None, {"status": status, "body": None}
    if status != 200:
        return None, {"status": status if type(status) is int else 0, "body": None}
    if max_bytes is not None and len(raw) > max_bytes:
        return "body", None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception:
        return "rpc_bad_json", None
    return None, {"status": 200, "body": parsed}


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


def post(payload: dict, *, max_bytes: int | None = None) -> dict:
    """One JSON-RPC POST. A failure reason does not include the URL."""
    if callable(TRANSPORT):
        result = TRANSPORT(payload)
        if not isinstance(result, dict) or type(result.get("status")) is not int:
            raise RpcRefused("rpc_transport")
        return result
    info = describe()
    if info["ok"] is not True:
        raise RpcRefused("helius_api_key_missing")
    return exchange(
        _wire_url(),
        json.dumps(payload).encode("utf-8"),
        timeout=8,
        max_bytes=max_bytes,
    )


def call(payload: dict) -> dict:
    """The JSON-RPC object. HTTP failures are reason codes with no request URL."""
    info = describe()
    if info["ok"] is not True:
        raise RpcRefused("helius_api_key_missing")
    posted = post(payload)
    status = posted.get("status")
    body = posted.get("body")
    reason = None
    if type(status) is not int or status != 200:
        code = status if type(status) is int else 0
        reason = "rpc_http_%s" % code
    elif not isinstance(body, dict):
        reason = "rpc_bad_json"
    else:
        return body
    del posted, body, status
    _refuse(reason)


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
