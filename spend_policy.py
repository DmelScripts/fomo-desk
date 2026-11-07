"""Typed fee and aggregate-loss records. Both stay unapproved until the owner answers.

A lamport ceiling is not a fee policy. The questions that are still open are a
per-transaction USD cap on DEX plus network fees, excluding refundable account
deposits, and a cumulative USD loss cap that includes fees. Deposits still
count as exposure. This module does not write an approval.
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
    approved = _fee_approved(row)
    return {
        "approved": approved,
        "kind": "fee_policy",
        "max_fee_usd": row.get("max_fee_usd") if isinstance(row, dict) else None,
        "includes_dex_fee": True,
        "includes_network_fee": True,
        "excludes_refundable_deposits": True,
        "deposits_count_as_exposure": True,
        "lamport_only_is_enough": False,
    }


def aggregate_view() -> dict:
    row = _read(_AGGREGATE)
    approved = _aggregate_approved(row)
    return {
        "approved": approved,
        "kind": "aggregate_loss_policy",
        "max_loss_usd": row.get("max_loss_usd") if isinstance(row, dict) else None,
        "includes_fees": True,
    }


def store_fee_ceiling(max_priority_fee_lamports: int) -> dict:
    """A lamport note. It does not approve a USD fee cap."""
    if isinstance(max_priority_fee_lamports, bool) or not isinstance(max_priority_fee_lamports, int):
        raise ValueError("fee ceiling is not usable")
    if max_priority_fee_lamports < 0 or max_priority_fee_lamports >= 2**63:
        raise ValueError("fee ceiling is not usable")
    _write(_FEE, {
        "approved": False,
        "owner_approved": False,
        "kind": "fee_policy",
        "max_priority_fee_lamports": max_priority_fee_lamports,
        "max_fee_usd": None,
    })
    return fee_view()


def store_aggregate_loss(max_loss_usd: str) -> dict:
    """A draft loss note. It does not mark the owner as having approved it."""
    amount = _money(max_loss_usd)
    if amount is None or amount <= 0 or amount > phantom_runner.BANKROLL:
        raise ValueError("aggregate loss ceiling is not usable")
    _write(_AGGREGATE, {
        "approved": False,
        "owner_approved": False,
        "kind": "aggregate_loss_policy",
        "max_loss_usd": format(amount, "f"),
        "includes_fees": True,
    })
    return aggregate_view()


def _fee_approved(row) -> bool:
    if not isinstance(row, dict) or row.get("owner_approved") is not True or row.get("kind") != "fee_policy":
        return False
    amount = _money(row.get("max_fee_usd"))
    return (
        amount is not None
        and amount > 0
        and row.get("includes_dex_fee") is True
        and row.get("includes_network_fee") is True
        and row.get("excludes_refundable_deposits") is True
        and row.get("deposits_count_as_exposure") is True
        and isinstance(row.get("fx_slot"), int)
        and not isinstance(row.get("fx_slot"), bool)
    )


def _aggregate_approved(row) -> bool:
    if not isinstance(row, dict) or row.get("owner_approved") is not True or row.get("kind") != "aggregate_loss_policy":
        return False
    amount = _money(row.get("max_loss_usd"))
    return amount is not None and 0 < amount <= phantom_runner.BANKROLL and row.get("includes_fees") is True


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
