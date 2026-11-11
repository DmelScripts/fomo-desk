"""Per-mint proofs taken from a response this process just read.

Nothing here accepts a caller-supplied body. The fetch that opened the
response is the only caller, and a proof for one mint does not cover another.
"""

from __future__ import annotations

import http.client
import inspect
import threading
import time
from urllib.parse import parse_qs, urlsplit

FRESH_S = 30
_FETCHERS = {"desk_providers", "desk_loop", "public_market", "dev_rug_history"}
_LOCK = threading.Lock()
_NOTES: dict = {}


def closed(gate: str, mint: str | None, now: float | None = None, *, input_mint: str | None = None, output_mint: str | None = None) -> bool:
    """True only for a fresh proof of this gate for this mint."""
    moment = time.time() if now is None else now
    if isinstance(moment, bool) or not isinstance(moment, (int, float)):
        return False
    if not isinstance(mint, str) or mint == "":
        return False
    with _LOCK:
        if gate == "bundle_unique_buyers_smart_wallets_dev_holding":
            return _fresh(_NOTES.get(("bundle", mint)), moment) and _fresh(_NOTES.get(("dev", mint)), moment)
        if gate == "quote_expiry":
            if mint not in {input_mint, output_mint}:
                return False
            return _fresh(_NOTES.get(("quote_expiry", input_mint, output_mint)), moment)
        return _fresh(_NOTES.get((gate, mint)), moment)


def _from_fetch(response, url: str, parsed, mint: str | None = None) -> None:
    """Record one response the fetch function just parsed. Other callers are ignored."""
    if _caller() not in _FETCHERS or not _http(response) or not isinstance(url, str) or not isinstance(parsed, dict):
        return
    now = time.time()
    try:
        _apply(url, parsed, mint, now)
    except Exception:
        return


def _from_history(response, subject: str, kind: str, parsed, past_mint: str | None = None) -> None:
    if _caller() not in _FETCHERS or not _http(response) or not isinstance(subject, str) or subject == "":
        return
    now = time.time()
    if kind == "page" and isinstance(parsed, dict):
        ids = _asset_ids(parsed)
        if ids is None or subject not in ids:
            _put(("rug_history_live", subject), False, now, None)
            return
        _put(("rug_page", subject), True, now, None)
        _put_body(("rug_ids", subject), ids, now)
        return
    if kind == "report" and isinstance(past_mint, str) and isinstance(parsed, dict) and type(parsed.get("rugged")) is bool:
        _put_body(("rug_report", subject, past_mint), {"rugged": parsed["rugged"]}, now)


def _seal_history(subject: str, past: list, clean: bool) -> None:
    """Promote a page and its reports that were read in this process. A bare bool is not a page."""
    if _caller() not in _FETCHERS or not isinstance(subject, str) or subject == "" or not isinstance(past, list):
        return
    now = time.time()
    with _LOCK:
        page = _NOTES.get(("rug_page", subject))
        listed = _NOTES.get(("rug_ids", subject))
    if not _fresh(page, now) or not isinstance(listed, dict):
        return
    ids = listed.get("body")
    if not isinstance(ids, list) or subject not in ids:
        _put(("rug_history_live", subject), False, now, None)
        return
    expected = [item for item in ids if item != subject]
    if expected != list(past):
        _put(("rug_history_live", subject), False, now, None)
        return
    if clean is not True:
        _put(("rug_history_live", subject), False, now, None)
        return
    for item in past:
        report = _NOTES.get(("rug_report", subject, item))
        body = report.get("body") if isinstance(report, dict) else None
        if not _fresh(report, now) or not isinstance(body, dict) or body.get("rugged") is not False:
            _put(("rug_history_live", subject), False, now, None)
            return
    _put(("rug_history_live", subject), True, now, None)


def _apply(url: str, body: dict, mint: str | None, now: float) -> None:
    if url.startswith("https://api.rugcheck.xyz/v1/tokens/") and url.endswith("/report/summary"):
        token = _mint_at(url, "/tokens/", "/report/summary")
        _put(("lp_lock_live", token), _lp_ok(body), now, None)
        return
    if url.startswith("https://api.rugcheck.xyz/v1/tokens/") and url.endswith("/report"):
        token = _mint_at(url, "/tokens/", "/report")
        if isinstance(token, str) and "creatorBalance" in body:
            _put_body(("rug_body", token), body, now)
            _try_dev(token, now)
        return
    if url.startswith("https://data.solanatracker.io/holders/chart/"):
        token = url.split("/holders/chart/", 1)[1].split("?", 1)[0].strip("/")
        if token == "" or "/" in token:
            return
        import public_filter_docs
        parsed = public_filter_docs.parse_holder_chart(body, token)
        grew = parsed.get("ok") is True and type(parsed.get("delta")) is int and parsed["delta"] > 0
        _put(("holder_growth_live", token), grew, now, None)
        return
    if url.startswith("https://data.solanatracker.io/tokens/"):
        token = url.split("/tokens/", 1)[1].split("?", 1)[0].strip("/")
        if token == "" or "/" in token:
            return
        import cross_source
        checked = cross_source.bundle_check(body, None)
        _put(("bundle", token), checked.get("ok") is True, now, None)
        _put_body(("tracker_body", token), body, now)
        _try_dev(token, now)
        return
    if url.startswith("https://hermes.pyth.network/") and isinstance(mint, str) and mint != "":
        import pyth_sponsored
        try:
            priced = pyth_sponsored.assess_signer_price(body, now)
        except (OverflowError, ValueError, ArithmeticError):
            priced = {"ok": False}
        _put(("price_freshness_proven", mint), priced.get("ok") is True, now, None)
        return
    if url.startswith("https://api.jup.ag/swap/v2/order"):
        query = parse_qs(urlsplit(url).query)
        inputs = query.get("inputMint") or []
        outputs = query.get("outputMint") or []
        if len(inputs) != 1 or len(outputs) != 1:
            return
        left, right = inputs[0], outputs[0]
        if not isinstance(left, str) or not isinstance(right, str) or left == "" or right == "":
            return
        if "inputMint" in body and body.get("inputMint") != left:
            return
        if "outputMint" in body and body.get("outputMint") != right:
            return
        expire = body.get("expireAt")
        if type(expire) is not int or expire <= now:
            _put(("quote_expiry", left, right), False, now, None)
            return
        _put(("quote_expiry", left, right), True, now, expire)


def _lp_ok(body: dict) -> bool:
    """100% burned, or 100% locked, or both present and summing to exactly 100."""
    if not isinstance(body, dict):
        return False
    has_burn = "lpBurn" in body
    has_lock = "lpLockedPct" in body
    burn = body.get("lpBurn") if has_burn else None
    locked = body.get("lpLockedPct") if has_lock else None
    if has_burn and has_lock:
        return _strict(burn) and _strict(locked) and burn + locked == 100
    if has_burn:
        return _strict(burn) and burn == 100
    if has_lock:
        return _strict(locked) and locked == 100
    return False


def _strict(value) -> bool:
    return type(value) is int or type(value) is float


def _try_dev(mint: str | None, now: float) -> None:
    if not isinstance(mint, str):
        return
    with _LOCK:
        tracker = _NOTES.get(("tracker_body", mint))
        rug = _NOTES.get(("rug_body", mint))
    if not _fresh(tracker, now) or not _fresh(rug, now):
        return
    import cross_source
    dev = cross_source.dev_check(rug.get("body"), tracker.get("body"))
    bundle = cross_source.bundle_check(tracker.get("body"), None)
    _put(("dev", mint), dev.get("status") == "PASS", now, None)
    _put(("bundle", mint), bundle.get("ok") is True, now, None)


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


def _put(key, ok: bool, now: float, until) -> None:
    with _LOCK:
        _NOTES[key] = {"at": now, "ok": ok is True, "until": until}


def _put_body(key, body, now: float) -> None:
    with _LOCK:
        _NOTES[key] = {"at": now, "ok": True, "until": None, "body": body}


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


def _http(response) -> bool:
    if isinstance(response, http.client.HTTPResponse):
        return True
    try:
        import httpx
    except Exception:
        return False
    return isinstance(response, httpx.Response)


def _caller() -> str | None:
    frame = inspect.currentframe()
    caller = None if frame is None else frame.f_back
    outer = None if caller is None else caller.f_back
    if outer is None:
        return None
    name = outer.f_globals.get("__name__")
    return name if isinstance(name, str) else None


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
    with _LOCK:
        _NOTES.clear()
