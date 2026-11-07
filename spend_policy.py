"""Typed fee and aggregate-loss records. Both default to unapproved.

Nothing here reads a chat message or writes a row on import. A later call
can store an owner-supplied ceiling. This module does not choose that number.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal, InvalidOperation

import db
import phantom_runner

_FEE = "fee_policy"
_AGGREGATE = "aggregate_loss_policy"


def fee_view() -> dict:
    row = _read(_FEE)
    if not _valid_fee(row):
        return {"approved": False, "max_priority_fee_lamports": None, "kind": "fee_policy"}
    return {
        "approved": True,
        "max_priority_fee_lamports": row["max_priority_fee_lamports"],
        "kind": "fee_policy",
    }


def aggregate_view() -> dict:
    row = _read(_AGGREGATE)
    if not _valid_aggregate(row):
        return {"approved": False, "max_loss_usd": None, "kind": "aggregate_loss_policy"}
    return {"approved": True, "max_loss_usd": row["max_loss_usd"], "kind": "aggregate_loss_policy"}


def store_fee_ceiling(max_priority_fee_lamports: int) -> dict:
    """Persist an owner-supplied priority-fee ceiling. Startup does not call this."""
    if isinstance(max_priority_fee_lamports, bool) or not isinstance(max_priority_fee_lamports, int):
        raise ValueError("fee ceiling is not usable")
    if max_priority_fee_lamports < 0 or max_priority_fee_lamports >= 2**63:
        raise ValueError("fee ceiling is not usable")
    _write(_FEE, {"approved": True, "max_priority_fee_lamports": max_priority_fee_lamports, "kind": "fee_policy"})
    return fee_view()


def store_aggregate_loss(max_loss_usd: str) -> dict:
    """Persist an owner-supplied loss ceiling. Startup does not call this."""
    amount = _money(max_loss_usd)
    if amount is None or amount <= 0 or amount > phantom_runner.BANKROLL:
        raise ValueError("aggregate loss ceiling is not usable")
    _write(_AGGREGATE, {"approved": True, "max_loss_usd": format(amount, "f"), "kind": "aggregate_loss_policy"})
    return aggregate_view()


def _valid_fee(row) -> bool:
    return (
        isinstance(row, dict)
        and row.get("kind") == "fee_policy"
        and row.get("approved") is True
        and isinstance(row.get("max_priority_fee_lamports"), int)
        and not isinstance(row.get("max_priority_fee_lamports"), bool)
        and row["max_priority_fee_lamports"] >= 0
    )


def _valid_aggregate(row) -> bool:
    amount = _money(row.get("max_loss_usd") if isinstance(row, dict) else None)
    return (
        isinstance(row, dict)
        and row.get("kind") == "aggregate_loss_policy"
        and row.get("approved") is True
        and amount is not None
        and 0 < amount <= phantom_runner.BANKROLL
    )


def _money(text):
    try:
        amount = Decimal(text)
    except (InvalidOperation, TypeError):
        return None
    if amount != amount.quantize(Decimal("0.01")):
        return None
    return amount


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
