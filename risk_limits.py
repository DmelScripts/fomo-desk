"""Explicit fee/loss limits over the server's PAPER reservation ledger.

This is application evidence only, not a wallet spending policy or LIVE
authorization. A verified source for actual execution fees remains separate.
Entry callers must assess and reserve in the same db.exclusive transaction.
"""

from __future__ import annotations

import json
import re
import time
from decimal import Decimal, InvalidOperation

import db
import phantom_transport

_KEY = "risk_limits_approval"
_FIELDS = {"max_total_loss_usd", "max_round_trip_fee_usd"}
_CENTS = re.compile(r"(?:0|[1-9][0-9]{0,2})\.[0-9]{2}\Z")
_ALLOCATION = Decimal("100.00")
_TICKET = Decimal("8.00")
_OPEN = {"reserved", "quoted", "sim_open", "submit_unknown"}
_RELEASED = {"released", "rejected", "halted"}


def _scope() -> dict:
    return {
        "wallet_id": phantom_transport.REQUIRED_WALLET_ID,
        "wallet": phantom_transport.REQUIRED_WALLET,
        "chain": "solana:mainnet",
        "allocation_usd": "100.00",
        "ticket_usd": "8.00",
        "ledger": "phantom_reservations",
        "authority": "PAPER",
    }


def _validate(body: dict) -> dict:
    if not isinstance(body, dict) or set(body) != _FIELDS:
        raise ValueError("exact fee and total-loss limits are required")
    for value in body.values():
        if not isinstance(value, str) or not _CENTS.fullmatch(value):
            raise ValueError("limits must be finite USD strings with exactly two decimal places")
    loss = Decimal(body["max_total_loss_usd"])
    fee = Decimal(body["max_round_trip_fee_usd"])
    if not 0 < loss <= _ALLOCATION:
        raise ValueError("total-loss limit must be greater than 0 and at most 100.00 USD")
    if not 0 <= fee <= _TICKET:
        raise ValueError("round-trip fee limit must be between 0.00 and 8.00 USD")
    return dict(body)


def approve(body: dict) -> dict:
    """Save explicit limits atomically; authentication belongs to the caller."""
    limits = _validate(body)
    payload = {"version": 1, "limits": limits, "scope": _scope(), "approved_at": time.time()}

    def work(conn):
        conn.execute(
            "INSERT INTO phantom_control (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (_KEY, json.dumps(payload, separators=(",", ":"), sort_keys=True), payload["approved_at"]),
        )
        return _view(conn)

    return db.exclusive(work)


def clear() -> dict:
    """Revoke approval without deleting reservations or resetting loss history."""
    payload = {"version": 1, "scope": _scope(), "revoked": True}

    def work(conn):
        # Preserve revocation so a previously configured desk cannot silently
        # fall back to the legacy, never-configured PAPER behavior.
        conn.execute(
            "INSERT INTO phantom_control (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (_KEY, json.dumps(payload, separators=(",", ":"), sort_keys=True), time.time()),
        )
        return _view(conn)

    return db.exclusive(work)


def _policy(conn) -> tuple[dict | None, str]:
    row = conn.execute("SELECT value FROM phantom_control WHERE key = ?", (_KEY,)).fetchone()
    if row is None:
        return None, "not_approved"
    try:
        body = json.loads(row["value"])
        if not isinstance(body, dict) or body.get("version") != 1 or body.get("scope") != _scope():
            return None, "approval_invalid"
        if body.get("revoked") is True:
            return None, "approval_revoked"
        return _validate(body.get("limits")), "approved"
    except (ValueError, TypeError):
        return None, "approval_invalid"


def _money(value, *, signed: bool = False) -> Decimal:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("ledger amount is not usable")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("ledger amount is not usable") from exc
    if not number.is_finite() or number.copy_abs() > Decimal("1000000000000") or (not signed and number < 0):
        raise ValueError("ledger amount is not usable")
    if number != number.quantize(Decimal("0.01")):
        raise ValueError("ledger amount contains a fraction of one cent")
    return number


def _ledger(conn) -> tuple[Decimal, Decimal]:
    losses = Decimal("0")
    committed = Decimal("0")
    scope = _scope()
    for row in conn.execute(
        "SELECT status, wallet_id, wallet, chain, amount_usd, fee_usd, reserved_usd, realized_pnl "
        "FROM phantom_reservations"
    ):
        status = row["status"]
        if status in _RELEASED:
            if _money(row["reserved_usd"]) != 0:
                raise ValueError("released ledger row still has a reservation")
            if row["realized_pnl"] is not None and _money(row["realized_pnl"], signed=True) != 0:
                raise ValueError("released ledger row has unreconciled realized results")
            continue
        if status not in _OPEN and status != "sim_closed":
            raise ValueError("ledger status is not usable")
        if any(row[key] != scope[key] for key in ("wallet_id", "wallet", "chain")):
            raise ValueError("ledger scope is not bound")
        if status == "sim_closed":
            # Profits never refund previously consumed loss capacity.
            realized = _money(row["realized_pnl"], signed=True)
            losses += max(Decimal("0"), -realized)
        else:
            principal = _money(row["amount_usd"])
            fee = _money(row["fee_usd"])
            reserved = _money(row["reserved_usd"])
            if principal <= 0:
                raise ValueError("ledger principal is not usable")
            # A deficient stored reserve must not hide the full principal+fee.
            committed += max(reserved, principal + fee)
    return losses, committed


def _text(value: Decimal) -> str:
    return format(value, ".2f")


def _view(conn) -> dict:
    policy, state = _policy(conn)
    body = {
        "approved": policy is not None,
        "approval_state": state,
        "limits": policy,
        "scope": _scope(),
        "evidence": "paper_ledger_only",
        "fee_source_verified": False,
        "live_ready": False,
        "execution_allowed": False,
        "send": False,
        "executable": False,
        "profits_restore_loss_capacity": False,
    }
    try:
        losses, committed = _ledger(conn)
    except ValueError:
        return {**body, "ledger_valid": False, "reason": "ledger_invalid"}
    used = losses + committed
    remaining = None if policy is None else max(Decimal("0"), Decimal(policy["max_total_loss_usd"]) - used)
    return {
        **body,
        "ledger_valid": True,
        "cumulative_realized_losses_usd": _text(losses),
        "open_committed_usd": _text(committed),
        "risk_used_usd": _text(used),
        "remaining_loss_capacity_usd": None if remaining is None else _text(remaining),
        "remaining_allocation_usd": _text(max(Decimal("0"), _ALLOCATION - used)),
    }


def view(*, conn=None) -> dict:
    return _view(conn) if conn is not None else db.exclusive(_view)


def assess(fee_usd: str, *, conn=None) -> dict:
    """Assess a new $8 PAPER entry. This function does not reserve capacity.

    For enforcement, pass the connection used for the immediately following
    reservation insert inside db.exclusive, so concurrent entries serialize.
    Never apply this new-entry check to an exit or a pending-send reconciliation.
    """
    try:
        fee = _money(fee_usd)
    except (ValueError, InvalidOperation):
        body = view(conn=conn)
        return {**body, "allowed": False, "reason": "fee_quote_invalid"}

    def work(connection):
        body = _view(connection)
        reserve = _TICKET + fee
        body.update({"allowed": False, "new_entry_reservation_usd": _text(reserve)})
        if not body["approved"]:
            return {**body, "reason": body["approval_state"]}
        if not body["ledger_valid"]:
            return {**body, "reason": "ledger_invalid"}
        policy = body["limits"]
        if fee > Decimal(policy["max_round_trip_fee_usd"]):
            return {**body, "reason": "round_trip_fee_limit"}
        # Re-read exact decimals instead of rounded display values.
        losses, committed = _ledger(connection)
        projected = losses + committed + reserve
        body["projected_risk_usd"] = _text(projected)
        if projected > _ALLOCATION:
            return {**body, "reason": "allocation_limit"}
        if projected > Decimal(policy["max_total_loss_usd"]):
            return {**body, "reason": "total_loss_limit"}
        return {**body, "allowed": True, "reason": "paper_limits_passed"}

    return work(conn) if conn is not None else db.exclusive(work)


def assess_existing(intent_id: str, *, conn=None) -> dict:
    """Recheck a reserved entry using its immutable server row, without adding it twice.

    As with assess(), the caller must keep this and _enter in one exclusive
    transaction. This check never allows exits or unknown submissions to enter.
    """
    def work(connection):
        body = {**_view(connection), "allowed": False, "new_entry_reservation_usd": "0.00"}
        if not body["approved"]:
            return {**body, "reason": body["approval_state"]}
        if not body["ledger_valid"]:
            return {**body, "reason": "ledger_invalid"}
        if not isinstance(intent_id, str) or not intent_id or len(intent_id) > 80:
            return {**body, "reason": "reservation_invalid"}
        row = connection.execute(
            "SELECT status, amount_usd, fee_usd, reserved_usd FROM phantom_reservations WHERE intent_id=?",
            (intent_id,),
        ).fetchone()
        if row is None:
            return {**body, "reason": "reservation_missing"}
        if row["status"] != "reserved":
            return {**body, "reason": "reservation_not_pending_entry"}
        try:
            principal = _money(row["amount_usd"])
            fee = _money(row["fee_usd"])
            reserved = _money(row["reserved_usd"])
        except (ValueError, InvalidOperation):
            return {**body, "reason": "reservation_invalid"}
        if principal != _TICKET or reserved != principal + fee:
            return {**body, "reason": "reservation_invalid"}
        body["existing_reservation_usd"] = _text(reserved)
        policy = body["limits"]
        if fee > Decimal(policy["max_round_trip_fee_usd"]):
            return {**body, "reason": "round_trip_fee_limit"}
        losses, committed = _ledger(connection)
        projected = losses + committed
        body["projected_risk_usd"] = _text(projected)
        if projected > _ALLOCATION:
            return {**body, "reason": "allocation_limit"}
        if projected > Decimal(policy["max_total_loss_usd"]):
            return {**body, "reason": "total_loss_limit"}
        return {**body, "allowed": True, "reason": "paper_limits_passed"}

    return work(conn) if conn is not None else db.exclusive(work)
