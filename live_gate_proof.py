"""Per-mint proofs taken from a response this process just read.

Nothing here accepts a caller-supplied body. The fetch that opened the
response is the only caller, and a proof for one mint does not cover another.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import math
import os
import socket
import ssl
import sys
import threading
import time
import types
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit

import httpx
from httpcore._backends.sync import SyncStream as _SYNC_STREAM

FRESH_S = 30
_LOCK = threading.Lock()
_NOTES: dict = {}


def closed(gate: str, mint: str | None, now: float | None = None, *, input_mint: str | None = None, output_mint: str | None = None) -> bool:
    """True only for a fresh proof of this gate for this mint."""
    return _CLOSED(gate, mint, now, input_mint=input_mint, output_mint=output_mint)


def _from_fetch(response, url: str, parsed, mint: str | None = None) -> None:
    """A response object is not a read. Only this module's handshake records a proof."""
    return None


def _from_history(response, subject: str, kind: str, parsed, past_mint: str | None = None) -> None:
    """A response object is not a read. Only this module's handshake records a proof."""
    return None


def _seal_history(subject, past, clean) -> None:
    """History is closed by the fetch that read it. This name does not write."""
    return None


def _apply(url, body, mint=None, now=None) -> None:
    """A URL and a body are not a read. This name does not write."""
    return None


def _record(url, raw, mint=None, history=None) -> None:
    """Raw bytes are not a read. This name does not write."""
    return None


def _plan(url, body, mint, now, history, existing) -> list:
    """Describe note updates. This does not write them."""
    if type(url) is not str or type(body) is not dict or isinstance(now, bool) or not isinstance(now, (int, float)):
        return []
    updates = []

    def put(key, ok, until=None, stored=None):
        row = {"at": now, "ok": ok is True, "until": until}
        if stored is not None:
            row["body"] = stored
        updates.append((key, row))

    def view():
        data = dict(existing)
        for key, row in updates:
            data[key] = row
        return data

    if url.startswith("https://api.rugcheck.xyz/v1/tokens/") and url.endswith("/report/summary"):
        token = _mint_at(url, "/tokens/", "/report/summary")
        if isinstance(token, str):
            put(("lp_lock_live", token), _lp_ok(body), None)
    elif url.startswith("https://api.rugcheck.xyz/v1/tokens/") and url.endswith("/report"):
        token = _mint_at(url, "/tokens/", "/report")
        if isinstance(token, str) and "creatorBalance" in body:
            put(("rug_body", token), True, None, body)
            _plan_dev(token, now, view, put)
    elif url.startswith("https://data.solanatracker.io/holders/chart/"):
        token = url.split("/holders/chart/", 1)[1].split("?", 1)[0].strip("/")
        if token != "" and "/" not in token:
            import public_filter_docs
            parsed = public_filter_docs.parse_holder_chart(body, token)
            grew = parsed.get("ok") is True and type(parsed.get("delta")) is int and parsed["delta"] > 0
            put(("holder_growth_live", token), grew, None)
    elif url.startswith("https://data.solanatracker.io/tokens/"):
        token = url.split("/tokens/", 1)[1].split("?", 1)[0].strip("/")
        if token != "" and "/" not in token:
            import cross_source
            checked = cross_source.bundle_check(body, None)
            put(("bundle", token), checked.get("ok") is True, None)
            put(("tracker_body", token), True, None, body)
            _plan_dev(token, now, view, put)
    elif url.startswith("https://hermes.pyth.network/") and isinstance(mint, str) and mint != "":
        import pyth_sponsored
        try:
            priced = pyth_sponsored.assess_signer_price(body, now)
        except (OverflowError, ValueError, ArithmeticError):
            priced = {"ok": False}
        put(("price_freshness_proven", mint), priced.get("ok") is True, None)
    elif url.startswith("https://api.jup.ag/swap/v2/order"):
        query = parse_qs(urlsplit(url).query)
        inputs = query.get("inputMint") or []
        outputs = query.get("outputMint") or []
        if len(inputs) == 1 and len(outputs) == 1:
            left, right = inputs[0], outputs[0]
            if isinstance(left, str) and isinstance(right, str) and left != "" and right != "" and body.get("inputMint", left) == left and body.get("outputMint", right) == right:
                expire = body.get("expireAt")
                if type(expire) is int and expire > now:
                    put(("quote_expiry", left, right), True, expire)
                else:
                    put(("quote_expiry", left, right), False, None)
    if type(history) is tuple and len(history) == 3:
        _plan_history(history, body, now, view, put)
    return updates


def _plan_dev(token, now, view, put) -> None:
    state = view()
    tracker = state.get(("tracker_body", token))
    rug = state.get(("rug_body", token))
    if not _fresh(tracker, now) or not _fresh(rug, now):
        return
    import cross_source
    dev = cross_source.dev_check(rug.get("body"), tracker.get("body"))
    bundle = cross_source.bundle_check(tracker.get("body"), None)
    put(("dev", token), dev.get("status") == "PASS", None)
    put(("bundle", token), bundle.get("ok") is True, None)


def _plan_history(history, body, now, view, put) -> None:
    kind, subject, past_mint = history
    if not isinstance(subject, str) or subject == "":
        return
    if kind == "page":
        ids = _asset_ids(body)
        if ids is None or subject not in ids:
            put(("rug_history_live", subject), False, None)
            return
        put(("rug_page", subject), True, None)
        put(("rug_ids", subject), True, None, ids)
        past = [item for item in ids if item != subject]
        if not past:
            put(("rug_history_live", subject), True, None)
        return
    if kind != "report" or not isinstance(past_mint, str) or type(body.get("rugged")) is not bool:
        return
    put(("rug_report", subject, past_mint), True, None, {"rugged": body["rugged"]})
    state = view()
    page = state.get(("rug_page", subject))
    listed = state.get(("rug_ids", subject))
    if not _fresh(page, now) or not isinstance(listed, dict):
        return
    ids = listed.get("body")
    if not isinstance(ids, list) or subject not in ids:
        put(("rug_history_live", subject), False, None)
        return
    past = [item for item in ids if item != subject]
    for item in past:
        report = state.get(("rug_report", subject, item))
        saved = report.get("body") if isinstance(report, dict) else None
        if not _fresh(report, now) or not isinstance(saved, dict) or saved.get("rugged") is not False:
            if item == past_mint and body.get("rugged") is True:
                put(("rug_history_live", subject), False, None)
            return
    put(("rug_history_live", subject), True, None)


def _lp_ok(body: dict) -> bool:
    """Burned or locked, each finite and inside 0..100, covering exactly 100."""
    if not isinstance(body, dict):
        return False
    has_burn = "lpBurn" in body
    has_lock = "lpLockedPct" in body
    burn = body.get("lpBurn") if has_burn else None
    locked = body.get("lpLockedPct") if has_lock else None
    if has_burn and has_lock:
        return _unit(burn) and _unit(locked) and burn + locked == 100
    if has_burn:
        return _unit(burn) and burn == 100
    if has_lock:
        return _unit(locked) and locked == 100
    return False


def _unit(value) -> bool:
    if type(value) is int:
        return 0 <= value <= 100
    if type(value) is float and math.isfinite(value):
        return 0 <= value <= 100
    return False


def _asset_ids(body: dict) -> list | None:
    result = body.get("result") if isinstance(body.get("result"), dict) else None
    if not isinstance(result, dict):
        return None
    total = result.get("total")
    items = result.get("items")
    if type(total) is not int or not isinstance(items, list) or total != len(items) or total <= 0 or total >= 20:
        return None
    ids = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or item["id"] in ids:
            return None
        ids.append(item["id"])
    if len(ids) != total:
        return None
    return ids


def _fresh(row, now: float) -> bool:
    if not isinstance(row, dict) or row.get("ok") is not True:
        return False
    stamp = row.get("at")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return False
    age = now - stamp
    if not (0 <= age <= FRESH_S):
        return False
    until = row.get("until")
    if until is not None and (isinstance(until, bool) or not isinstance(until, (int, float)) or until <= now):
        return False
    return True




def _mint_at(url: str, left: str, right: str) -> str | None:
    if left not in url:
        return None
    token = url.split(left, 1)[1]
    if right not in token:
        return None
    token = token.split(right, 1)[0].strip("/")
    if token == "" or "/" in token:
        return None
    return token


def _reset_for_tests() -> None:
    _RESET()


_SOCKET = socket.socket
_CONNECT = getattr(socket.socket, "connect")
_CREATE_CONNECTION = socket.create_connection
_SSL_SOCKET = ssl.SSLSocket
_GETPEERCERT = ssl.SSLSocket.getpeercert
_WRAP = ssl.SSLContext.wrap_socket
_URLOPEN = urllib.request.urlopen
_HTTPS_CONNECTION = http.client.HTTPSConnection
_CONTEXT = ssl.create_default_context()
_HTTP_TRANSPORT_HANDLE = httpx.HTTPTransport.handle_request
_CLIENT_GET = httpx.Client.get
_CLIENT = None


def _stack_intact() -> bool:
    return (
        socket.socket is _SOCKET
        and getattr(socket.socket, "connect") is _CONNECT
        and socket.create_connection is _CREATE_CONNECTION
        and ssl.SSLSocket is _SSL_SOCKET
        and ssl.SSLSocket.getpeercert is _GETPEERCERT
        and ssl.SSLContext.wrap_socket is _WRAP
        and urllib.request.urlopen is _URLOPEN
        and http.client.HTTPSConnection is _HTTPS_CONNECTION
        and httpx.HTTPTransport.handle_request is _HTTP_TRANSPORT_HANDLE
        and httpx.Client.get is _CLIENT_GET
    )


def _cert_matches(cert, host: str) -> bool:
    if type(cert) is not dict or type(host) is not str or host == "":
        return False
    names = cert.get("subjectAltName")
    if type(names) is not tuple or not names:
        return False
    wanted = host.lower()
    matched = False
    for item in names:
        if type(item) is not tuple or len(item) != 2:
            return False
        kind, value = item
        if kind == "DNS" and type(value) is str and value.lower() == wanted:
            matched = True
    return matched


def _handshake_host(sock, host: str) -> bool:
    if not _stack_intact() or type(sock) is not _SSL_SOCKET or type(host) is not str:
        return False
    if type(sock).getpeercert is not _GETPEERCERT:
        return False
    try:
        cert = _GETPEERCERT(sock)
    except Exception:
        return False
    return _cert_matches(cert, host)


class _DeskHTTPSConnection(_HTTPS_CONNECTION):
    """Opens the TLS socket. The proof is the certificate read afterwards, not this class."""

    def connect(self):
        super().connect()


class _OwnedTransport(httpx.BaseTransport):
    """The httpx transport this module owns. A replaced get() never enters here."""

    def __init__(self):
        self._inner = httpx.HTTPTransport(retries=0)

    def handle_request(self, request):
        response = self._inner.handle_request(request)
        self._note(response, request)
        return response

    def _note(self, response, request) -> None:
        if self._inner.handle_request.__func__ is not _HTTP_TRANSPORT_HANDLE:
            return
        if not _stack_intact() or type(response) is not httpx.Response:
            return
        extensions = response.extensions if type(response.extensions) is dict else None
        stream = None if extensions is None else extensions.get("network_stream")
        if type(stream) is not _SYNC_STREAM:
            return
        host = request.url.host
        if not _handshake_host(stream._sock, host if type(host) is str else ""):
            return
        if str(request.url) == "":
            return
        try:
            parsed = json.loads(response.content.decode("utf-8"))
        except Exception:
            return
        if type(parsed) is not dict:
            return
        _commit_verified(stream._sock, str(request.url), parsed, None, None)


def http_client():
    """The one client. Replacing it, or its get method, does not record a proof."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = httpx.Client(transport=_OwnedTransport(), follow_redirects=False, timeout=8.0)
    return _CLIENT


def exchange(request, timeout: float, mint: str | None = None, history=None):
    """Read one HTTPS request. A patched urlopen is returned and is not a proof."""
    return _EXCHANGE(request, timeout, mint, history)


def _owned_https(request, timeout: float, mint, history):
    return _OWNED(request, timeout, mint, history)


def _seen() -> dict:
    return _SEEN()


def _CLOSED(gate, mint, now, *, input_mint=None, output_mint=None) -> bool:
    return False


def _RESET() -> None:
    return None


def _EXCHANGE(request, timeout, mint, history):
    raise RuntimeError("unbound")


def _OWNED(request, timeout, mint, history):
    raise RuntimeError("unbound")


def _SEEN() -> dict:
    return {}


def _commit_verified(sock, url, parsed, mint, history) -> None:
    return None


def _wire():
    """Bind the handshake objects and the note store. Later global lookups are not the originals."""
    secret = os.urandom(32)
    notes = {}
    lock = threading.Lock()
    connection_cls = _DeskHTTPSConnection
    context = _CONTEXT
    ssl_socket = _SSL_SOCKET
    getpeercert = _GETPEERCERT
    cert_matches = _cert_matches
    socket_type = _SOCKET
    connect_fn = _CONNECT
    create_fn = _CREATE_CONNECTION
    wrap = _WRAP
    urlopen = _URLOPEN
    https_base = _HTTPS_CONNECTION
    transport_handle = _HTTP_TRANSPORT_HANDLE
    client_get = _CLIENT_GET
    sync_stream = _SYNC_STREAM

    def canonical(value):
        if type(value) is tuple:
            return [canonical(item) for item in value]
        if type(value) is list:
            return [canonical(item) for item in value]
        if type(value) is dict:
            return {str(key): canonical(item) for key, item in value.items()}
        return value

    def mac_for(key, row) -> str:
        gate = key[0] if type(key) is tuple and key else ""
        digest = hashlib.sha256(json.dumps(canonical({
            "ok": row.get("ok"),
            "until": row.get("until"),
            "body": row.get("body"),
        }), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        message = json.dumps(canonical({
            "gate": gate,
            "key": key,
            "timestamp": row.get("at"),
            "digest": digest,
        }), sort_keys=True, separators=(",", ":")).encode()
        return hmac.new(secret, message, hashlib.sha256).hexdigest()

    def authentic(key, row) -> bool:
        if type(row) is not dict or type(row.get("mac")) is not str:
            return False
        try:
            expect = mac_for(key, row)
        except Exception:
            return False
        return hmac.compare_digest(expect, row["mac"])

    def sign(key, row) -> dict:
        signed = dict(row)
        signed["mac"] = mac_for(key, signed)
        return signed

    def seen() -> dict:
        with lock:
            copy = dict(notes)
        return {key: row for key, row in copy.items() if authentic(key, row)}

    def save(rows) -> None:
        stamped = []
        for key, row in rows:
            stamped.append((key, sign(key, row)))
        with lock:
            for key, row in stamped:
                notes[key] = row

    def handshake(sock, host) -> bool:
        if not stack_intact() or type(sock) is not ssl_socket or type(host) is not str:
            return False
        if type(sock).getpeercert is not getpeercert:
            return False
        try:
            cert = getpeercert(sock)
        except Exception:
            return False
        return cert_matches(cert, host)

    def stack_intact() -> bool:
        mod = sys.modules[__name__]
        return (
            socket.socket is socket_type
            and getattr(socket.socket, "connect") is connect_fn
            and socket.create_connection is create_fn
            and ssl.SSLSocket is ssl_socket
            and ssl.SSLSocket.getpeercert is getpeercert
            and ssl.SSLContext.wrap_socket is wrap
            and urllib.request.urlopen is urlopen
            and http.client.HTTPSConnection is https_base
            and httpx.HTTPTransport.handle_request is transport_handle
            and httpx.Client.get is client_get
            and mod._DeskHTTPSConnection is connection_cls
            and mod._CONTEXT is context
            and mod._cert_matches is cert_matches
            and mod._handshake_host is handshake
            and mod._owned_https is owned_https
            and mod._commit_verified is commit
        )

    def commit(sock, url, parsed, mint, history) -> None:
        host = urlsplit(url).hostname if type(url) is str else None
        if not handshake(sock, host if type(host) is str else ""):
            return
        save(_plan(url, parsed, mint, time.time(), history, seen()))

    def owned_https(request, timeout, mint, history):
        parts = urlsplit(request.full_url)
        if parts.scheme != "https" or type(parts.hostname) is not str or parts.hostname == "":
            raise urllib.error.URLError("host")
        conn = connection_cls(parts.hostname, timeout=timeout, context=context)
        path = parts.path or "/"
        if parts.query:
            path = path + "?" + parts.query
        headers = {key: value for key, value in request.header_items()}
        sock = None
        try:
            conn.request(request.get_method(), path, body=request.data, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            status = response.status
            sock = conn.sock
            parsed = None
            if status == 200:
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                except Exception:
                    parsed = None
            proved = type(parsed) is dict and handshake(sock, parts.hostname)
            if proved:
                commit(sock, request.full_url, parsed, mint, history)
            return status, raw, proved
        finally:
            conn.close()

    def do_exchange(request, timeout, mint, history):
        if not stack_intact():
            response = urllib.request.urlopen(request, timeout=timeout)
            try:
                return response.status, response.read(), False
            finally:
                closer = getattr(response, "close", None)
                if callable(closer):
                    closer()
        return owned_https(request, timeout, mint, history)

    def note_ok(gate, mint, now, *, input_mint=None, output_mint=None) -> bool:
        moment = time.time() if now is None else now
        if isinstance(moment, bool) or not isinstance(moment, (int, float)):
            return False
        if not isinstance(mint, str) or mint == "":
            return False
        current = seen()
        if gate == "bundle_unique_buyers_smart_wallets_dev_holding":
            return _fresh(current.get(("bundle", mint)), moment) and _fresh(current.get(("dev", mint)), moment)
        if gate == "quote_expiry":
            if mint not in {input_mint, output_mint}:
                return False
            return _fresh(current.get(("quote_expiry", input_mint, output_mint)), moment)
        return _fresh(current.get((gate, mint)), moment)

    def reset() -> None:
        with lock:
            notes.clear()

    global _CLOSED, _RESET, _EXCHANGE, _OWNED, _SEEN, _commit_verified, _handshake_host, _stack_intact, _owned_https, _NOTES
    _CLOSED = note_ok
    _RESET = reset
    _EXCHANGE = do_exchange
    _OWNED = owned_https
    _SEEN = seen
    _commit_verified = commit
    _handshake_host = handshake
    _stack_intact = stack_intact
    _owned_https = owned_https
    _NOTES = types.MappingProxyType(notes)


_wire()
del _wire
