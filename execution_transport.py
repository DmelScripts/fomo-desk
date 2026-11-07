"""Isolated signing and broadcast gate. The send primitive is a stub.

No function in this module signs, broadcasts, or reads a provider. A snapshot
can satisfy every listed prerequisite and send() still returns no signature.
Signature status from a fake provider is not applied unless this adapter has
a send record, and it never does. There is no route to this module.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


def status() -> dict:
    return {
        "wired": False,
        "signing": "unimplemented",
        "send": "stub",
        "complete": False,
        "automatic_invocation": False,
        "live_ready": False,
        "executable": False,
    }


def prerequisites(snapshot: dict) -> dict:
    """Name every gate. A true check is not permission to sign."""
    if not isinstance(snapshot, dict):
        snapshot = {}
    reservation = snapshot.get("reservation")
    intent_id = snapshot.get("intent_id")
    used = snapshot.get("used_intent_ids")
    height = snapshot.get("last_valid_block_height")
    checks = {
        "mode_paper": snapshot.get("mode") == "PAPER",
        "live_consent": snapshot.get("live_consent") is True,
        "kill_switch_open": snapshot.get("halted") is False,
        "loss_policy_approved": snapshot.get("loss_policy") == "approved",
        "risk_allows_send": snapshot.get("risk_allows_send") is True,
        "intent_verified": snapshot.get("intent_verified") is True,
        "spend_verified": snapshot.get("spend_verified") is True,
        "slippage_verified": snapshot.get("slippage_verified") is True,
        "blockhash_checked": snapshot.get("blockhash_status") == "checked" and snapshot.get("blockhash_valid") is True,
        "last_valid_block_height": isinstance(height, int) and not isinstance(height, bool) and height > 0,
        "freshness_not_invented": snapshot.get("freshness_guarantee") is False and snapshot.get("provider_expiry") is None,
        "reservation_held": _reservation(reservation, intent_id),
        "idempotent": isinstance(intent_id, str) and bool(intent_id) and isinstance(used, list) and intent_id not in used,
    }
    return {
        "ready": False,
        "checks": checks,
        "unmet": [name for name, passed in checks.items() if not passed],
        "send": False,
        "complete": False,
        "live_ready": False,
    }


def send(snapshot: dict) -> dict:
    """Refuse. The body does not accept a signer, a transaction, or a client."""
    report = prerequisites(snapshot)
    report["status"] = "stub"
    report["reason"] = "send primitive is not implemented"
    report["signature"] = None
    report["executable"] = False
    return report


def reconcile(snapshot: dict, provider_result: dict) -> dict:
    """A fake status cannot confirm a signature this adapter did not send."""
    if not isinstance(snapshot, dict) or not isinstance(provider_result, dict):
        return {"status": "not_usable", "send": False, "complete": False, "confirmation": None}
    if snapshot.get("sent") is True or snapshot.get("signature") is not None:
        return {
            "status": "refused",
            "reason": "this adapter has no send record",
            "send": False,
            "complete": False,
            "confirmation": None,
            "executable": False,
        }
    return {
        "status": "not_run",
        "send": False,
        "complete": False,
        "confirmation": None,
        "executable": False,
        "provider_seen": "value" in provider_result,
    }


def _reservation(value, intent_id) -> bool:
    if not isinstance(value, dict) or value.get("status") != "reserved" or value.get("intent_id") != intent_id:
        return False
    text = value.get("usd")
    if not isinstance(text, str):
        return False
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return False
    return amount == amount.quantize(Decimal("0.01")) and Decimal("0.01") <= amount <= Decimal("100.00")
