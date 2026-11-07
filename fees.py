"""FOMO-on-Solana fee schedule (STEP 11).

$5 to $47.50 -> 2% of the ticket
$47.50 to $190 -> flat 0.95
above $190 -> 0.50%
each side, then minus fees_ref_discount (10% off). Gas = 0, FOMO pays it.

The two boundaries are continuous: 2% of $47.50 is $0.95, and 0.50% of $190
is $0.95. Below $5 the prompt does not name a tier; the 2% rate is used so a
smaller ticket is not free.
"""

from __future__ import annotations

import db


def gross_fee_usd(ticket_usd: float) -> float:
    ticket = float(ticket_usd)
    if ticket <= 0:
        return 0.0
    if ticket <= 47.50:
        return ticket * 0.02
    if ticket <= 190:
        return 0.95
    return ticket * 0.005


def fee_usd(ticket_usd: float, discount: float | None = None) -> float:
    if discount is None:
        discount = _discount()
    return gross_fee_usd(ticket_usd) * (1.0 - float(discount))


def round_trip_fees(size_usd: float, entry: float, exit_price: float, discount: float | None = None) -> float:
    sold = exit_notional(size_usd, entry, exit_price)
    return fee_usd(size_usd, discount) + fee_usd(sold, discount)


def exit_notional(size_usd: float, entry: float, exit_price: float) -> float:
    """Dollars received before the exit fee. Entry notional is size_usd."""
    if entry <= 0:
        return float(size_usd)
    return float(size_usd) * (float(exit_price) / float(entry))


def pnl_usd(size_usd: float, entry: float, exit_price: float, discount: float | None = None) -> tuple[float, float]:
    if entry <= 0:
        return 0.0, 0.0
    gross = size_usd * (exit_price - entry) / entry
    fees = round_trip_fees(size_usd, entry, exit_price, discount)
    return gross - fees, fees


def _discount() -> float:
    try:
        return db.get_float("fees_ref_discount")
    except Exception:
        return 0.10
