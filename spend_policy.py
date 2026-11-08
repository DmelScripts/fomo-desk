"""Owner financial targets from the 21:37 widget.

Stop new entries at $20 cumulative loss, including fees, inside the $100
allocation. No daily reset and no refill. This is not a guaranteed loss limit.
Each transaction may spend at most $0.05 on the DEX swap fee plus the network
fee. Refundable account deposits are excluded from that fee and still count
as exposure. Import does not write a row. persist_owner_approval() writes the
exact server-side values and does not accept a caller-supplied amount or slot.
The owner approves those limits. A fresh FX slot is checked when an order is
built, and it is not stored on the policy row.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal, InvalidOperation

import db
import phantom_runner

_FEE = "fee_policy"
_AGGREGATE = "aggregate_loss_policy"
ALLOCATION_USD = Decimal("100.00")
MAX_FEE_USD = Decimal("0.05")
MAX_CUMULATIVE_LOSS_USD = Decimal("20.00")
_SOURCE = "owner_widget_21:37"


def target() -> dict:
    """The accepted values. recorded and approved stay false in this process."""
    return {
        "source": _SOURCE,
        "allocation_usd": format(ALLOCATION_USD, "f"),
        "max_fee_usd_per_tx": format(MAX_FEE_USD, "f"),
        "fee_includes_dex_and_network": True,
        "refundable_deposits_excluded_from_fee": True,
        "deposits_count_as_exposure": True,
        "stop_new_entries_cumulative_loss_usd": format(MAX_CUMULATIVE_LOSS_USD, "f"),
        "loss_includes_fees": True,
        "daily_reset": False,
        "refill": False,
        "guaranteed_loss_limit": False,
        "new_entries_stop_once_realized_reaches": format(MAX_CUMULATIVE_LOSS_USD, "f"),
        "open_exposure_is_not_realized_loss": True,
        "an_open_trade_can_realize_past_the_stop": True,
        "recorded": False,
        "approved": False,
    }


def fee_view() -> dict:
    row = _read(_FEE)
    approved = _fee_approved(row)
    return {
        "approved": approved,
        "recorded": approved,
        "kind": "fee_policy",
        "specified_max_fee_usd": format(MAX_FEE_USD, "f"),
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
        "recorded": approved,
        "kind": "aggregate_loss_policy",
        "specified_max_loss_usd": format(MAX_CUMULATIVE_LOSS_USD, "f"),
        "max_loss_usd": row.get("max_loss_usd") if isinstance(row, dict) else None,
        "includes_fees": True,
        "daily_reset": False,
        "refill": False,
        "guaranteed_loss_limit": False,
        "stops_new_entries": True,
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


def usd_source() -> dict:
    """USDC/USD only when a configured quote is usable. Pool micro-USDC is not USD."""
    import usdc_valuation
    quote = usdc_valuation.current()
    return {
        "approved": quote.get("approved") is True,
        "usable": quote.get("usable") is True,
        "source": quote.get("source"),
        "accepted_band": None,
        "reason": quote.get("reason"),
        "pool_micro_usdc_is_usd": False,
        "confidence": quote.get("confidence"),
        "feed_id": quote.get("feed_id"),
        "upper": quote.get("upper"),
        "lower": quote.get("lower"),
        "origin": quote.get("origin"),
        "live_provider": quote.get("live_provider") is True,
        "authenticated_live": quote.get("authenticated_live") is True,
        "publish_time": quote.get("publish_time"),
        "price": quote.get("price"),
        "conf": quote.get("conf"),
        "expo": quote.get("expo"),
    }


def policy_stamp() -> dict:
    """Versions read inside the same transaction that would insert a hold."""
    fee = _stamp(_FEE)
    loss = _stamp(_AGGREGATE)
    halt = db.query("SELECT value, updated_at FROM phantom_control WHERE key = 'halt'")
    exit_row = db.query("SELECT updated_at FROM phantom_control WHERE key = 'exit_policy_approval'")
    return {
        "fee": fee,
        "loss": loss,
        "halt": None if not halt else halt[0]["value"],
        "halt_at": None if not halt else halt[0]["updated_at"],
        "exit": None if not exit_row else exit_row[0]["updated_at"],
    }


def persist_owner_approval() -> dict:
    """Record the widget. The numbers are the constants above, not a request body."""
    _drop_live_consent()
    _write(_FEE, {
        "approved": True,
        "owner_approved": True,
        "kind": "fee_policy",
        "source": _SOURCE,
        "max_fee_usd": format(MAX_FEE_USD, "f"),
        "includes_dex_fee": True,
        "includes_network_fee": True,
        "excludes_refundable_deposits": True,
        "deposits_count_as_exposure": True,
    })
    _write(_AGGREGATE, {
        "approved": True,
        "owner_approved": True,
        "kind": "aggregate_loss_policy",
        "source": _SOURCE,
        "max_loss_usd": format(MAX_CUMULATIVE_LOSS_USD, "f"),
        "includes_fees": True,
        "daily_reset": False,
        "refill": False,
        "guaranteed_loss_limit": False,
        "stops_new_entries": True,
    })
    return {"fee": fee_view(), "aggregate": aggregate_view()}


def clear_owner_approval() -> dict:
    """Drop approval. Amounts already stored are not replaced by a caller."""
    _drop_live_consent()
    for key in (_FEE, _AGGREGATE):
        row = _read(key)
        if isinstance(row, dict):
            row["approved"] = False
            row["owner_approved"] = False
            _write(key, row)
    return {"fee": fee_view(), "aggregate": aggregate_view()}


def _fee_approved(row) -> bool:
    """True only for the $0.05 limit. A slot on the row is not an approval."""
    if not isinstance(row, dict) or row.get("owner_approved") is not True or row.get("kind") != "fee_policy":
        return False
    if row.get("source") != _SOURCE or "fx_slot" in row:
        return False
    amount = _money(row.get("max_fee_usd"))
    return (
        amount == MAX_FEE_USD
        and row.get("includes_dex_fee") is True
        and row.get("includes_network_fee") is True
        and row.get("excludes_refundable_deposits") is True
        and row.get("deposits_count_as_exposure") is True
    )


def _aggregate_approved(row) -> bool:
    """True only for the $20 stop-new-entries target. Any other amount is rejected."""
    if not isinstance(row, dict) or row.get("owner_approved") is not True or row.get("kind") != "aggregate_loss_policy":
        return False
    if row.get("source") != _SOURCE:
        return False
    amount = _money(row.get("max_loss_usd"))
    return (
        amount == MAX_CUMULATIVE_LOSS_USD
        and amount <= phantom_runner.BANKROLL
        and row.get("includes_fees") is True
        and row.get("daily_reset") is False
        and row.get("refill") is False
        and row.get("guaranteed_loss_limit") is False
        and row.get("stops_new_entries") is True
    )


def _stamp(key: str):
    rows = db.query("SELECT updated_at FROM phantom_control WHERE key = ?", (key,))
    return None if not rows else rows[0]["updated_at"]


def _drop_live_consent() -> None:
    """A financial change retires an older LIVE request. It does not write a new one."""
    db.execute("DELETE FROM phantom_control WHERE key = 'live_trading_consent'")


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
