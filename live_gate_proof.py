"""Readiness proofs from a live provider parse in this process.

A test double, a patched urlopen, or a fixture body does not record a proof.
A recorded proof expires. A missing or failing parse leaves the gate open.
"""

from __future__ import annotations

import threading
import time
import urllib.request

FRESH_S = 30
_OPENER = urllib.request.urlopen
_LOCK = threading.Lock()
_NOTES: dict = {}


def consider(provider: str, url: str, body) -> None:
    """Record one production response. Patched sockets and test transports do not."""
    if not _live_channel() or not isinstance(body, dict):
        return
    now = time.time()
    try:
        _consider(provider, url, body, now)
    except Exception:
        return


def consider_jupiter(body) -> None:
    if not _live_channel() or not isinstance(body, dict):
        return
    expire = body.get("expireAt")
    if type(expire) is not int or expire <= time.time():
        return
    _put("quote_expiry", True, time.time(), expire)


def note_history(past_mints, clean: bool) -> None:
    """A getAssetsByCreator page and each past mint's RugCheck rugged flag, just parsed."""
    if not _live_channel() or not isinstance(past_mints, list):
        return
    if any(not isinstance(mint, str) or mint == "" for mint in past_mints):
        return
    _put("rug_history_live", clean is True, time.time(), None)


def closed(gate: str, now: float | None = None) -> bool:
    moment = time.time() if now is None else now
    if isinstance(moment, bool) or not isinstance(moment, (int, float)):
        return False
    with _LOCK:
        if gate == "bundle_unique_buyers_smart_wallets_dev_holding":
            return _bundle_gate(moment)
        row = _NOTES.get(gate)
    return _row_fresh(row, moment)


def _consider(provider: str, url: str, body: dict, now: float) -> None:
    if provider == "rugcheck":
        if "lpLockedPct" in body:
            import public_filter_docs
            locked = public_filter_docs.parse_lp_locked_pct(body)
            _put("lp_lock_live", locked.get("ok") is True, now, None)
        if "creatorBalance" in body:
            _try_dev(body, now)
        return
    if provider == "solana_tracker" and "/holders/chart/" in url:
        import public_filter_docs
        mint = url.rstrip("/").split("/")[-1]
        parsed = public_filter_docs.parse_holder_chart(body, mint)
        grew = parsed.get("ok") is True and type(parsed.get("delta")) is int and parsed["delta"] > 0
        _put("holder_growth_live", grew, now, None)
        return
    if provider == "solana_tracker" and "/tokens/" in url:
        import cross_source
        checked = cross_source.bundle_check(body, None)
        _put_part("bundle", {"ok": checked.get("ok") is True}, now)
        _put_part("tracker_token", body, now)
        _try_dev(None, now)
        return
    if provider == "hermes":
        import pyth_sponsored
        try:
            priced = pyth_sponsored.assess_signer_price(body, now)
        except (OverflowError, ValueError, ArithmeticError):
            priced = {"ok": False}
        _put("price_freshness_proven", priced.get("ok") is True, now, None)


def _try_dev(rugcheck_body, now: float) -> None:
    with _LOCK:
        tracker = _NOTES.get("part:tracker_token")
        rug = _NOTES.get("part:rugcheck_body")
        if rugcheck_body is not None:
            rug = {"at": now, "ok": True, "body": rugcheck_body, "until": None}
            _NOTES["part:rugcheck_body"] = rug
        if tracker is None or rug is None:
            return
        tracker_body = tracker.get("body")
        rug_body = rug.get("body")
    if not _row_fresh(tracker, now) or not _row_fresh(rug, now):
        return
    import cross_source
    dev = cross_source.dev_check(rug_body, tracker_body)
    bundle = cross_source.bundle_check(tracker_body, None)
    _put_part("dev", {"ok": dev.get("status") == "PASS"}, now)
    _put_part("bundle", {"ok": bundle.get("ok") is True}, now)


def _bundle_gate(now: float) -> bool:
    bundle = _NOTES.get("part:bundle")
    dev = _NOTES.get("part:dev")
    if not _row_fresh(bundle, now) or not _row_fresh(dev, now):
        return False
    if not isinstance(bundle.get("body"), dict) or bundle["body"].get("ok") is not True:
        return False
    if not isinstance(dev.get("body"), dict) or dev["body"].get("ok") is not True:
        return False
    return True


def _put(gate: str, ok: bool, now: float, until) -> None:
    with _LOCK:
        _NOTES[gate] = {"at": now, "ok": ok is True, "until": until}


def _put_part(name: str, body, now: float) -> None:
    with _LOCK:
        _NOTES["part:" + name] = {"at": now, "ok": True, "body": body, "until": None}


def _row_fresh(row, now: float) -> bool:
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


def _live_channel() -> bool:
    return urllib.request.urlopen is _OPENER

