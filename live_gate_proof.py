"""Per-mint proofs taken from a response this process just read.

Nothing here accepts a caller-supplied body. The fetch that opened the
response is the only caller, and a proof for one mint does not cover another.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
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

import certifi
from urllib.parse import parse_qs, urlsplit

import httpx
from httpcore._backends.sync import SyncStream as _SYNC_STREAM

FRESH_S = 30
_LOCK = threading.Lock()
_NOTES: dict = {}


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


def _plan(url, body, mint, now, history, existing, lp_ok, mint_at, plan_dev, plan_history, parse_holder, bundle_check, assess_price, split, parse_query) -> list:
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
        token = mint_at(url, "/tokens/", "/report/summary")
        if isinstance(token, str):
            put(("lp_lock_live", token), lp_ok(body), None)
    elif url.startswith("https://api.rugcheck.xyz/v1/tokens/") and url.endswith("/report"):
        token = mint_at(url, "/tokens/", "/report")
        if isinstance(token, str) and "creatorBalance" in body:
            put(("rug_body", token), True, None, body)
            plan_dev(token, now, view, put)
    elif url.startswith("https://data.solanatracker.io/holders/chart/"):
        token = url.split("/holders/chart/", 1)[1].split("?", 1)[0].strip("/")
        if token != "" and "/" not in token:
            parsed = parse_holder(body, token)
            grew = parsed.get("ok") is True and type(parsed.get("delta")) is int and parsed["delta"] > 0
            put(("holder_growth_live", token), grew, None)
    elif url.startswith("https://data.solanatracker.io/tokens/"):
        token = url.split("/tokens/", 1)[1].split("?", 1)[0].strip("/")
        if token != "" and "/" not in token:
            checked = bundle_check(body, None)
            put(("bundle", token), checked.get("ok") is True, None)
            put(("tracker_body", token), True, None, body)
            plan_dev(token, now, view, put)
    elif url.startswith("https://hermes.pyth.network/") and isinstance(mint, str) and mint != "":
        try:
            priced = assess_price(body, now)
        except (OverflowError, ValueError, ArithmeticError):
            priced = {"ok": False}
        put(("price_freshness_proven", mint), priced.get("ok") is True, None)
    elif url.startswith("https://api.jup.ag/swap/v2/order"):
        query = parse_query(split(url).query)
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
        plan_history(history, body, now, view, put)
    return updates


def _plan_dev(token, now, view, put, fresh, dev_check, bundle_check) -> None:
    state = view()
    tracker = state.get(("tracker_body", token))
    rug = state.get(("rug_body", token))
    if not fresh(tracker, now) or not fresh(rug, now):
        return
    dev = dev_check(rug.get("body"), tracker.get("body"))
    bundle = bundle_check(tracker.get("body"), None)
    put(("dev", token), dev.get("status") == "PASS", None)
    put(("bundle", token), bundle.get("ok") is True, None)


def _plan_history(history, body, now, view, put, fresh, asset_ids) -> None:
    kind, subject, past_mint = history
    if not isinstance(subject, str) or subject == "":
        return
    if kind == "page":
        ids = asset_ids(body)
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
    if not fresh(page, now) or not isinstance(listed, dict):
        return
    ids = listed.get("body")
    if not isinstance(ids, list) or subject not in ids:
        put(("rug_history_live", subject), False, None)
        return
    past = [item for item in ids if item != subject]
    for item in past:
        report = state.get(("rug_report", subject, item))
        saved = report.get("body") if isinstance(report, dict) else None
        if not fresh(report, now) or not isinstance(saved, dict) or saved.get("rugged") is not False:
            if item == past_mint and body.get("rugged") is True:
                put(("rug_history_live", subject), False, None)
            return
    put(("rug_history_live", subject), True, None)


def _lp_ok(body: dict, unit) -> bool:
    """Burned or locked, each finite and inside 0..100, covering exactly 100."""
    if not isinstance(body, dict):
        return False
    has_burn = "lpBurn" in body
    has_lock = "lpLockedPct" in body
    burn = body.get("lpBurn") if has_burn else None
    locked = body.get("lpLockedPct") if has_lock else None
    if has_burn and has_lock:
        return unit(burn) and unit(locked) and burn + locked == 100
    if has_burn:
        return unit(burn) and burn == 100
    if has_lock:
        return unit(locked) and locked == 100
    return False


def _unit(value, finite=math.isfinite) -> bool:
    if type(value) is int:
        return 0 <= value <= 100
    if type(value) is float and finite(value):
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


def _fresh(row, now: float, limit: float) -> bool:
    if not isinstance(row, dict) or row.get("ok") is not True:
        return False
    stamp = row.get("at")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return False
    age = now - stamp
    if not (0 <= age <= limit):
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


_SOCKET = socket.socket
_CONNECT = getattr(socket.socket, "connect")
_CREATE_CONNECTION = socket.create_connection
_SSL_SOCKET = ssl.SSLSocket
_GETPEERCERT = ssl.SSLSocket.getpeercert
_WRAP = ssl.SSLContext.wrap_socket
_LOAD_VERIFY = ssl.SSLContext.load_verify_locations
_HTTPS_CONNECT = http.client.HTTPSConnection.connect
_HTTP_CONNECT = http.client.HTTPConnection.connect
_URLOPEN = urllib.request.urlopen
_HTTPS_CONNECTION = http.client.HTTPSConnection
_CA_FILE = certifi.where()
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


def _global_address(value) -> bool:
    """True only for a globally routable address. CGNAT and mapped CGNAT are not."""
    try:
        addr = ipaddress.ip_address(value)
    except (TypeError, ValueError):
        return False
    mapped = addr.ipv4_mapped if type(addr) is ipaddress.IPv6Address else None
    if mapped is not None:
        addr = mapped
    return addr.is_global is True


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
        """Replaced at import by the closure that verified this socket. This body does not write."""
        return None


def http_client():
    """The one client. Replacing it, or its get method, does not record a proof."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = httpx.Client(transport=_OwnedTransport(), follow_redirects=False, timeout=8.0)
    return _CLIENT


def _wire():
    """Bind the handshake objects and the note store. Later global lookups are not the originals."""
    global closed, exchange, _owned_https, _reset_for_tests, _seen
    global _CLOSED, _RESET, _EXCHANGE, _OWNED, _SEEN, _commit_verified, _handshake_host, _stack_intact, _NOTES
    global attributes_intact, monitored
    global _fresh, _lp_ok, _plan, _plan_dev, _plan_history
    secret = os.urandom(32)
    notes = {}
    lock = threading.Lock()
    connection_cls = _DeskHTTPSConnection
    ssl_socket = _SSL_SOCKET
    getpeercert = _GETPEERCERT
    getpeername = ssl.SSLSocket.getpeername
    cert_matches = _cert_matches
    ssl_context_cls = ssl.SSLContext
    tls_client = ssl.PROTOCOL_TLS_CLIENT
    cert_required = ssl.CERT_REQUIRED
    load_verify = _LOAD_VERIFY
    https_connect = _HTTPS_CONNECT
    http_connect = _HTTP_CONNECT
    global_address = _global_address
    ca_file = _CA_FILE
    getaddrinfo_fn = socket.getaddrinfo
    socket_type = _SOCKET
    connect_fn = _CONNECT
    create_fn = _CREATE_CONNECTION
    wrap = _WRAP
    urlopen = _URLOPEN
    https_base = _HTTPS_CONNECTION
    transport_handle = _HTTP_TRANSPORT_HANDLE
    client_get = _CLIENT_GET
    sync_stream = _SYNC_STREAM
    fresh_body = _fresh
    lp_body = _lp_ok
    plan_body = _plan
    dev_body = _plan_dev
    hist_body = _plan_history
    unit = _unit
    mint_at = _mint_at
    asset_ids = _asset_ids
    apply_fn = _apply
    record_fn = _record
    client_fn = http_client
    hmac_new = hmac.new
    digest_new = hashlib.sha256
    compare_digest = hmac.compare_digest
    clock = time.time
    dumps = json.dumps
    loads = json.loads
    window = FRESH_S
    import public_filter_docs
    import cross_source
    import pyth_sponsored
    docs = public_filter_docs
    markets = cross_source
    pyth = pyth_sponsored
    parse_holder = docs.parse_holder_chart
    bundle_check = markets.bundle_check
    dev_check = markets.dev_check
    assess_price = pyth.assess_signer_price
    split = urlsplit
    parse_query = parse_qs

    def fresh(row, now) -> bool:
        return fresh_body(row, now, window)

    def lp_ok(body) -> bool:
        return lp_body(body, unit)

    def plan_dev(token, now, view, put) -> None:
        return dev_body(token, now, view, put, fresh, dev_check, bundle_check)

    def plan_history(history, body, now, view, put) -> None:
        return hist_body(history, body, now, view, put, fresh, asset_ids)

    def plan(url, body, mint, now, history, existing) -> list:
        return plan_body(
            url, body, mint, now, history, existing,
            lp_ok, mint_at, plan_dev, plan_history, parse_holder, bundle_check, assess_price, split, parse_query,
        )

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
        digest = digest_new(dumps(canonical({
            "ok": row.get("ok"),
            "until": row.get("until"),
            "body": row.get("body"),
        }), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        message = dumps(canonical({
            "gate": gate,
            "key": key,
            "timestamp": row.get("at"),
            "digest": digest,
        }), sort_keys=True, separators=(",", ":")).encode()
        return hmac_new(secret, message, digest_new).hexdigest()

    def authentic(key, row) -> bool:
        if type(row) is not dict or type(row.get("mac")) is not str:
            return False
        try:
            expect = mac_for(key, row)
        except Exception:
            return False
        return compare_digest(expect, row["mac"])

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

    def public_peer(sock) -> bool:
        if ssl.SSLSocket.getpeername is not getpeername:
            return False
        try:
            peer = getpeername(sock)
        except Exception:
            return False
        if type(peer) is not tuple or not peer or type(peer[0]) is not str:
            return False
        return global_address(peer[0]) is True

    def handshake(sock, host) -> bool:
        if not stack_intact() or type(sock) is not ssl_socket or type(host) is not str:
            return False
        if type(sock).getpeercert is not getpeercert:
            return False
        if not public_peer(sock):
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
            and socket.getaddrinfo is getaddrinfo_fn
            and ssl.SSLSocket is ssl_socket
            and ssl.SSLSocket.getpeercert is getpeercert
            and ssl.SSLSocket.getpeername is getpeername
            and ssl.SSLContext is ssl_context_cls
            and ssl.SSLContext.wrap_socket is wrap
            and ssl.SSLContext.load_verify_locations is load_verify
            and ssl.PROTOCOL_TLS_CLIENT is tls_client
            and ssl.CERT_REQUIRED is cert_required
            and urllib.request.urlopen is urlopen
            and http.client.HTTPSConnection is https_base
            and http.client.HTTPSConnection.connect is https_connect
            and http.client.HTTPConnection.connect is http_connect
            and httpx.HTTPTransport.handle_request is transport_handle
            and httpx.Client.get is client_get
            and mod._DeskHTTPSConnection is connection_cls
            and mod._CA_FILE is ca_file
            and mod._cert_matches is cert_matches
            and mod._handshake_host is handshake
            and mod._owned_https is owned_https
            and mod._commit_verified is commit
            and mod._OwnedTransport.handle_request is handle
        )

    def client_context():
        ctx = ssl_context_cls(tls_client)
        ctx.verify_mode = cert_required
        ctx.check_hostname = True
        load_verify(ctx, cafile=ca_file)
        return ctx

    def seal(url, parsed, mint, history) -> None:
        if not check():
            return
        save(plan(url, parsed, mint, clock(), history, seen()))

    def commit(sock, url, parsed, mint, history) -> None:
        host = split(url).hostname if type(url) is str else None
        if not handshake(sock, host if type(host) is str else ""):
            return
        seal(url, parsed, mint, history)

    def owned_https(request, timeout, mint=None, history=None):
        parts = split(request.full_url)
        if parts.scheme != "https" or type(parts.hostname) is not str or parts.hostname == "":
            raise urllib.error.URLError("host")
        conn = connection_cls(parts.hostname, timeout=timeout, context=client_context())
        path = parts.path or "/"
        if parts.query:
            path = path + "?" + parts.query
        headers = {key: value for key, value in request.header_items()}
        try:
            conn.connect()
            sock = conn.sock
            accepted = handshake(sock, parts.hostname)
            conn.request(request.get_method(), path, body=request.data, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            status = response.status
            parsed = None
            if status == 200:
                try:
                    parsed = loads(raw.decode("utf-8"))
                except Exception:
                    parsed = None
            if conn.sock is not sock:
                accepted = False
            proved = accepted and type(parsed) is dict and check()
            if proved:
                seal(request.full_url, parsed, mint, history)
            return status, raw, proved
        finally:
            conn.close()

    def do_exchange(request, timeout, mint=None, history=None):
        if not stack_intact():
            response = urllib.request.urlopen(request, timeout=timeout)
            try:
                return response.status, response.read(), False
            finally:
                closer = getattr(response, "close", None)
                if callable(closer):
                    closer()
        return owned_https(request, timeout, mint, history)

    def note_ok(gate, mint, now=None, *, input_mint=None, output_mint=None) -> bool:
        """True only for a fresh proof of this gate for this mint."""
        moment = clock() if now is None else now
        if isinstance(moment, bool) or not isinstance(moment, (int, float)):
            return False
        if not isinstance(mint, str) or mint == "":
            return False
        current = seen()
        if gate == "bundle_unique_buyers_smart_wallets_dev_holding":
            return fresh(current.get(("bundle", mint)), moment) and fresh(current.get(("dev", mint)), moment)
        if gate == "quote_expiry":
            if mint not in {input_mint, output_mint}:
                return False
            return fresh(current.get(("quote_expiry", input_mint, output_mint)), moment)
        return fresh(current.get((gate, mint)), moment)

    def reset() -> None:
        with lock:
            notes.clear()

    view = types.MappingProxyType(notes)
    snapshots = {"classes": (), "functions": (), "methods": ()}

    def mapping_matches(saved, live) -> bool:
        if set(saved) != set(live):
            return False
        for key, value in saved.items():
            if live[key] is not value:
                return False
        return True

    def check() -> bool:
        mod = sys.modules[__name__]
        expected = (
            ("closed", note_ok),
            ("exchange", do_exchange),
            ("_owned_https", owned_https),
            ("_commit_verified", commit),
            ("_handshake_host", handshake),
            ("_cert_matches", cert_matches),
            ("_stack_intact", stack_intact),
            ("_NOTES", view),
            ("_DeskHTTPSConnection", connection_cls),
            ("_CA_FILE", ca_file),
            ("_global_address", global_address),
            ("_fresh", fresh),
            ("_lp_ok", lp_ok),
            ("_plan", plan),
            ("_plan_dev", plan_dev),
            ("_plan_history", plan_history),
            ("_mint_at", mint_at),
            ("_asset_ids", asset_ids),
            ("_unit", unit),
            ("urlsplit", split),
            ("parse_qs", parse_query),
            ("_apply", apply_fn),
            ("_record", record_fn),
            ("_CLOSED", note_ok),
            ("_EXCHANGE", do_exchange),
            ("_OWNED", owned_https),
            ("_SEEN", seen),
            ("_RESET", reset),
            ("http_client", client_fn),
            ("_seen", seen),
            ("attributes_intact", check),
            ("monitored", names),
        )
        for name, original in expected:
            if getattr(mod, name, None) is not original:
                return False
        if mod._OwnedTransport._note is not note or mod._OwnedTransport.handle_request is not handle:
            return False
        if mod.FRESH_S is not window:
            return False
        if hmac.new is not hmac_new or hashlib.sha256 is not digest_new or hmac.compare_digest is not compare_digest:
            return False
        if time.time is not clock or json.dumps is not dumps or json.loads is not loads:
            return False
        if docs.parse_holder_chart is not parse_holder:
            return False
        if markets.bundle_check is not bundle_check or markets.dev_check is not dev_check:
            return False
        if pyth.assess_signer_price is not assess_price:
            return False
        if not snapshots["classes"] or not snapshots["functions"] or not snapshots["methods"]:
            return False
        for cls, saved in snapshots["classes"]:
            if mapping_matches(saved, cls.__dict__) is not True:
                return False
        for fn, saved in snapshots["methods"]:
            if mapping_matches(saved, fn.__dict__) is not True:
                return False
        mod_functions = {
            fname: fvalue
            for fname, fvalue in vars(mod).items()
            if isinstance(fvalue, types.FunctionType)
        }
        if set(mod_functions) != {fname for fname, _fn, _saved in snapshots["functions"]}:
            return False
        for fname, fvalue, saved in snapshots["functions"]:
            if mod_functions[fname] is not fvalue:
                return False
            if mapping_matches(saved, fvalue.__dict__) is not True:
                return False
        return stack_intact()

    def names():
        return (
            "closed",
            "exchange",
            "_owned_https",
            "_commit_verified",
            "_handshake_host",
            "_cert_matches",
            "_stack_intact",
            "_NOTES",
            "_DeskHTTPSConnection",
            "_CA_FILE",
            "_global_address",
            "_fresh",
            "_lp_ok",
            "_plan",
            "_plan_dev",
            "_plan_history",
            "_mint_at",
            "_asset_ids",
            "_unit",
            "urlsplit",
            "parse_qs",
            "_apply",
            "_record",
            "_CLOSED",
            "_EXCHANGE",
            "_OWNED",
            "_SEEN",
            "_RESET",
            "http_client",
            "_seen",
            "attributes_intact",
            "monitored",
            "FRESH_S",
            "_note",
            "handle_request",
        )

    def note(self, response, request) -> None:
        if self._inner.handle_request.__func__ is not transport_handle:
            return
        if not stack_intact() or type(response) is not httpx.Response:
            return
        extensions = response.extensions if type(response.extensions) is dict else None
        stream = None if extensions is None else extensions.get("network_stream")
        if type(stream) is not sync_stream:
            return
        host = request.url.host
        if not handshake(stream._sock, host if type(host) is str else ""):
            return
        try:
            parsed = loads(response.content.decode("utf-8"))
        except Exception:
            return
        if type(parsed) is not dict:
            return
        commit(stream._sock, str(request.url), parsed, None, None)

    def handle(self, request):
        ctx = client_context()
        inner = httpx.HTTPTransport(verify=ctx, retries=0)
        previous = self._inner
        self._inner = inner
        try:
            response = inner.handle_request(request)
            note(self, response, request)
            return response
        finally:
            self._inner = previous
            inner.close()

    closed = note_ok
    exchange = do_exchange
    _owned_https = owned_https
    _reset_for_tests = reset
    _seen = seen
    _fresh = fresh
    _lp_ok = lp_ok
    _plan = plan
    _plan_dev = plan_dev
    _plan_history = plan_history
    _CLOSED = note_ok
    _RESET = reset
    _EXCHANGE = do_exchange
    _OWNED = owned_https
    _SEEN = seen
    _commit_verified = commit
    _handshake_host = handshake
    _stack_intact = stack_intact
    _NOTES = view
    attributes_intact = check
    monitored = names
    _OwnedTransport._note = note
    _OwnedTransport.handle_request = handle
    mod = sys.modules[__name__]
    snapshots["classes"] = tuple(
        (cls, dict(cls.__dict__))
        for cls in (connection_cls, _OwnedTransport)
    )
    snapshots["functions"] = tuple(
        (fname, fvalue, dict(fvalue.__dict__))
        for fname, fvalue in vars(mod).items()
        if isinstance(fvalue, types.FunctionType) and fname != "_wire"
    )
    snapshots["methods"] = tuple(
        (fn, dict(fn.__dict__))
        for _cls, saved in snapshots["classes"]
        for fn in saved.values()
        if isinstance(fn, types.FunctionType)
    )


_wire()
del _wire
