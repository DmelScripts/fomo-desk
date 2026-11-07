"""Guarded Phantom sign-and-send adapter. It is not mounted and not enabled.

Phantom's documented call is client.signAndSendTransaction, which signs and
broadcasts. This module does not construct that client. submit() can call an
injected signer only after the desk's own rows and a fresh inspection agree.
Fee policy and aggregate loss are separate decisions and are not approved.
The Jupiter route_v2 Whirlpool proof gaps block activation as well.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time

import db
import exit_policy
import phantom_runner
import phantom_transport
import solana_inspect

_REVIEW = "send_review:"
_LEDGER = "send_ledger"
_CONSENT = "live_trading_consent"
_STATEMENT = "Finish everything then enable LIVE trade."
SWAP_BLOCKERS = (
    "Jupiter does not publish the route_v2 remaining-account list for Swap::Whirlpool. The observed build had 12 accounts after the 10 fixed route accounts; Orca's swap struct has 11, and no Jupiter source identifies the extra account.",
    "Jupiter does not publish how route_v2 slippage_bps and quoted_out_amount are enforced as the Whirlpool swap other_amount_threshold.",
)


class DisabledSigner:
    """Default signer. It has no client and cannot reach Phantom."""

    def sign_and_send_transaction(self, *, wallet_id, transaction, network_id, account):
        raise RuntimeError("signer is disabled")


def status() -> dict:
    return {
        "wired": False,
        "signing": "disabled",
        "send": "disabled",
        "complete": False,
        "automatic_invocation": False,
        "live_ready": False,
        "executable": False,
        "fee_policy": "not_approved",
        "aggregate_loss_policy": False,
    }


def activation_status() -> dict:
    """Machine-checkable gate for enabling the five agents and LIVE later.

    Ready is false while any criterion fails. This function changes nothing.
    """
    consent = _consent()
    policy = exit_policy.view()
    checks = {
        "mode_still_paper": db.get_setting("mode", "PAPER") == "PAPER",
        "agents_still_off": db.get_setting("agents_enabled", "0") == "0",
        "five_agents_named": db.AGENT_NAMES == ("scanner", "auditor", "analyst", "executor", "risk"),
        "wallet_allowlist": phantom_transport.REQUIRED_WALLET == "7AFJbqsX2UeK7ZW5M1gbXWjiKCai2CJWHpCzXe7woC7t",
        "exit_tuple_effective": policy["effective"] is True and policy["rules"] == exit_policy._RULES,
        "exit_is_not_live_consent": policy["live_consent"] is False,
        "exit_is_not_aggregate": policy["aggregate_loss_policy"] is False and policy["loss_policy"] == "not_approved",
        "live_request_recorded": consent is not None,
        "live_request_not_applied": consent is None or consent.get("applied") is False,
        "fee_policy_approved": False,
        "aggregate_loss_policy_approved": False,
        "swap_path_verified": False,
        "signer_enabled": False,
    }
    return {
        "ready": all(checks.values()),
        "agents": list(db.AGENT_NAMES),
        "checks": checks,
        "unmet": [name for name, passed in checks.items() if not passed],
        "swap_blockers": list(SWAP_BLOCKERS),
        "fee_policy": "not_approved",
        "aggregate_loss_policy": False,
        "live_ready": False,
    }


def remember_review(intent_id: str, tx_b64: str) -> dict:
    """Store the reviewed unsigned-byte hash. This does not send."""
    raw = _bytes(tx_b64)
    payload = {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "wallet": phantom_transport.REQUIRED_WALLET,
        "network": "solana:mainnet",
    }
    _write(_REVIEW + intent_id, payload)
    return payload


def submit(intent_id: str, tx_b64: str, *, signer=None, rpc=None) -> dict:
    """Refuse unless every desk gate and a fresh inspection agree.

    The injected signer is not the default. The default cannot reach Phantom.
    """
    decision = decide(intent_id, tx_b64, rpc)
    decision["signature"] = None
    decision["signer_called"] = False
    decision["send"] = False
    decision["executable"] = False
    decision["live_ready"] = False
    if decision["unmet"]:
        return decision
    if signer is None:
        decision["unmet"].append("signer_disabled")
        return decision
    signer.sign_and_send_transaction(
        wallet_id=phantom_transport.REQUIRED_WALLET_ID,
        transaction=tx_b64,
        network_id="solana:mainnet",
        account=phantom_transport.REQUIRED_WALLET,
    )
    decision["signer_called"] = True
    return decision


def decide(intent_id: str, tx_b64: str, rpc=None) -> dict:
    checks = {
        "intent_id": isinstance(intent_id, str) and bool(intent_id),
        "mode_paper_until_activation": db.get_setting("mode", "PAPER") == "PAPER",
        "kill_switch_open": phantom_runner.halted() is False,
        "exit_tuple_effective": exit_policy.view()["effective"] is True,
        "live_request_recorded": _consent() is not None,
        "fee_policy_approved": False,
        "aggregate_loss_policy_approved": False,
        "reservation_open": False,
        "unknown_submit_clear": False,
        "idempotent": False,
        "reviewed_binding": False,
        "wallet_network": False,
        "inspection_chain_ok": False,
        "spend_verified": False,
        "slippage_verified": False,
        "fee_unbound_clear": False,
        "blockhash_checked": False,
    }
    reservation = _reservation(intent_id) if checks["intent_id"] else None
    if reservation is not None and reservation["status"] == "sim_open":
        checks["reservation_open"] = (
            reservation["wallet"] == phantom_transport.REQUIRED_WALLET
            and reservation["wallet_id"] == phantom_transport.REQUIRED_WALLET_ID
            and reservation["amount_usd"] == "8.00"
            and _money(reservation["reserved_usd"]) is not None
            and _money(reservation["reserved_usd"]) <= phantom_runner.BANKROLL
        )
        checks["unknown_submit_clear"] = True
    elif reservation is not None and reservation["status"] == "submit_unknown":
        checks["unknown_submit_clear"] = False
    elif reservation is None:
        checks["unknown_submit_clear"] = False
    else:
        checks["unknown_submit_clear"] = reservation["status"] != "submit_unknown"
    ledger = _ledger()
    checks["idempotent"] = checks["intent_id"] and intent_id not in ledger
    review = _read(_REVIEW + intent_id) if checks["intent_id"] else None
    raw = b""
    try:
        raw = _bytes(tx_b64)
    except Exception:
        raw = b""
    digest = hashlib.sha256(raw).hexdigest() if raw else ""
    if review and review.get("sha256") == digest and review.get("wallet") == phantom_transport.REQUIRED_WALLET and review.get("network") == "solana:mainnet":
        checks["reviewed_binding"] = True
    inspected = None
    if rpc is not None and raw:
        inspected = solana_inspect.inspect(base64.b64encode(raw).decode("ascii"), rpc)
        checks["inspection_chain_ok"] = inspected.get("chain_ok") is True
        checks["spend_verified"] = inspected.get("spend_verified") is True
        checks["slippage_verified"] = inspected.get("slippage_verified") is True
        checks["fee_unbound_clear"] = inspected.get("error_category") != "fee_unbound" and _price_is_zero(raw)
        checks["blockhash_checked"] = inspected.get("blockhash_status") == "checked" and inspected.get("freshness_guarantee") is False
        checks["wallet_network"] = inspected.get("fee_payer") == phantom_transport.REQUIRED_WALLET
    unmet = [name for name, passed in checks.items() if not passed]
    return {
        "checks": checks,
        "unmet": unmet,
        "effects_allowed": not unmet,
        "inspection_category": None if inspected is None else inspected.get("error_category"),
        "fee_policy": "not_approved",
        "aggregate_loss_policy": False,
        "swap_blockers": list(SWAP_BLOCKERS),
    }


def reconcile(intent_id: str) -> dict:
    """A provider status cannot confirm a send this adapter did not record."""
    if not isinstance(intent_id, str) or intent_id not in _ledger():
        return {"status": "not_run", "send": False, "complete": False, "confirmation": None, "executable": False}
    return {
        "status": "refused",
        "reason": "recorded send is not a confirmation",
        "send": False,
        "complete": False,
        "confirmation": None,
        "executable": False,
    }


def _consent():
    row = _read(_CONSENT)
    if not isinstance(row, dict):
        return None
    if row.get("live_trading_requested") is not True or row.get("applied") is not False:
        return None
    if row.get("agents_requested") != list(db.AGENT_NAMES):
        return None
    if row.get("fee_policy") != "not_approved" or row.get("aggregate_loss_policy") is not False:
        return None
    if row.get("statement") != _STATEMENT:
        return None
    return row


def _reservation(intent_id: str):
    rows = db.query("SELECT * FROM phantom_reservations WHERE intent_id = ?", (intent_id,))
    return rows[0] if rows else None


def _ledger() -> dict:
    row = _read(_LEDGER)
    return row if isinstance(row, dict) else {}


def _money(text):
    from decimal import Decimal, InvalidOperation
    try:
        amount = Decimal(text)
    except (InvalidOperation, TypeError):
        return None
    if amount != amount.quantize(Decimal("0.01")):
        return None
    return amount


def _price_is_zero(raw: bytes) -> bool:
    import solana_tx
    try:
        decoded = solana_tx.decode_transaction(raw)
    except ValueError:
        return False
    for instruction in decoded.instructions:
        program = decoded.static_keys[instruction.program_index] if instruction.program_index < len(decoded.static_keys) else ""
        if program != solana_tx.COMPUTE_BUDGET:
            continue
        parsed = solana_tx.compute_budget_payload(instruction.data)
        if parsed and parsed[0] == 3 and int.from_bytes(parsed[1], "little") != 0:
            return False
    return True


def _bytes(tx_b64: str) -> bytes:
    if not isinstance(tx_b64, str):
        raise ValueError("transaction is not usable")
    return base64.b64decode(tx_b64, validate=True)


def _read(key: str):
    rows = db.query("SELECT value FROM phantom_control WHERE key = ?", (key,))
    if not rows:
        return None
    try:
        return json.loads(rows[0]["value"])
    except json.JSONDecodeError:
        return None


def _write(key: str, payload: dict) -> None:
    db.execute(
        """
        INSERT INTO phantom_control (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, json.dumps(payload, separators=(",", ":"), sort_keys=True), time.time()),
    )
