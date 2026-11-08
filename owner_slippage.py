"""Owner slippage ceiling from the 8 Nov 2026 16:28 Asia/Saigon decision.

The words were "Set maximum slippage to 0.5% per swap." That is 50 basis
points on each swap, entry and exit. It is not 0.5 basis points and it is not
50 percent. Exits and retries do not get a wider number. A caller, a test
allowance, or an older stored value cannot raise it. This module does not send.
"""

from __future__ import annotations

import json

import db

BPS = 50
PERCENT = "0.5"
STATEMENT = "Set maximum slippage to 0.5% per swap."
APPROVED_AT = "2026-11-08T16:28:00+07:00"
TIMEZONE = "Asia/Saigon"
SOURCE = "owner_decision_2026-11-08T16:28:00+07:00"
SCOPE = "per_swap"
KEY = "memecoin_max_slippage_bps"


def target() -> dict:
    """The code target. It is not a production deploy and it does not send."""
    return {
        "bps": BPS,
        "percent": PERCENT,
        "statement": STATEMENT,
        "approved_at": APPROVED_AT,
        "timezone": TIMEZONE,
        "source": SOURCE,
        "scope": SCOPE,
        "applies_to": ["entry", "exit"],
        "wider_for_exit": False,
        "wider_for_retry": False,
        "deployed": False,
        "production": False,
        "live": False,
        "send": False,
        "sign": False,
    }


def persist() -> dict:
    """Write the owner record. Arguments cannot change the 50 basis-point ceiling."""
    db.set_setting(KEY, json.dumps(target(), sort_keys=True), SOURCE)
    return configuration()


def configuration() -> dict:
    """Persisted state beside the code target. A mismatch is not approval."""
    approved = _approved_record()
    body = target()
    body.update({
        "persisted": approved is not None,
        "approved": approved is not None,
        "approved_bps": None if approved is None else BPS,
        "stored_bps": None if approved is None else approved.get("bps"),
        "owner_slippage_unapproved": approved is None,
        "deployed": False,
        "production": False,
        "live": False,
        "send": False,
        "sign": False,
    })
    return body


def ceiling_bps() -> int | None:
    """50 when the stored record is the owner decision. Any other row is absent."""
    try:
        approved = _approved_record()
    except Exception:
        return None
    return BPS if approved is not None else None


def check_route(slippage_bps, min_out, amm_est_out) -> dict:
    """Reject an instruction above 50 bps or a minimum under that ceiling's floor.

    The ceiling is the code constant. A stored 100, or a caller allowance, is
    not consulted. Tighter than 50 bps is accepted. The floor uses the
    instruction's own bps, which cannot exceed 50.
    """
    refused = {
        "ok": False,
        "ceiling_bps": BPS,
        "owner_ceiling_bps": BPS,
        "send": False,
        "sign": False,
        "live_ready": False,
    }
    if isinstance(slippage_bps, bool) or not isinstance(slippage_bps, int) or slippage_bps < 0:
        return refused | {"reason": "slippage_unapproved"}
    if slippage_bps > BPS:
        return refused | {"reason": "slippage_exceeds", "slippage_bps": slippage_bps}
    if isinstance(amm_est_out, bool) or not isinstance(amm_est_out, int) or amm_est_out <= 0:
        return refused | {"reason": "quote_unbound"}
    if isinstance(min_out, bool) or not isinstance(min_out, int) or min_out <= 0:
        return refused | {"reason": "min_below_amm"}
    floor = amm_est_out * (10_000 - slippage_bps) // 10_000
    if min_out < floor:
        return refused | {"reason": "min_below_amm", "allowance_floor": floor, "slippage_bps": slippage_bps}
    return {
        "ok": True,
        "reason": None,
        "ceiling_bps": BPS,
        "slippage_bps": slippage_bps,
        "allowance_floor": floor,
        "send": False,
        "sign": False,
        "live_ready": False,
    }


def effective_test_allowance(allowance):
    """A test allowance cannot exceed 50. None stays unset. Above 50 is clamped."""
    if allowance is None:
        return None
    if isinstance(allowance, bool) or not isinstance(allowance, int) or allowance < 0 or allowance > 10_000:
        return "unbound"
    if allowance > BPS:
        return BPS
    return allowance


def _approved_record() -> dict | None:
    raw = db.get_setting(KEY, "")
    if not isinstance(raw, str) or raw == "":
        return None
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return None
    expected = target()
    if not isinstance(body, dict):
        return None
    for key, value in expected.items():
        if body.get(key) != value:
            return None
    return body
