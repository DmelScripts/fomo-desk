"""PAPER round-trip fees.

The owner's prompt says the referral link takes 10% off trading fees for the
life of the account. The tier table itself was cut out of the prompt file
after that sentence, so the base rate below is an explicit stand-in: 1% of
notional, then the 10% referral discount. It is not a fee schedule copied
from FOMO.
"""

from __future__ import annotations

BASE_FEE_RATE = 0.01


def fee_usd(ticket_usd: float, discount: float = 0.10) -> float:
    if ticket_usd <= 0:
        return 0.0
    rate = BASE_FEE_RATE * (1.0 - discount)
    return float(ticket_usd) * rate


def round_trip_fees(size_usd: float, entry: float, exit_price: float, discount: float = 0.10) -> float:
    """Fee on the way in and the way out. Exit notional scales with price."""
    if entry <= 0:
        exit_notional = size_usd
    else:
        exit_notional = size_usd * (exit_price / entry)
    return fee_usd(size_usd, discount) + fee_usd(exit_notional, discount)


def pnl_usd(size_usd: float, entry: float, exit_price: float, discount: float = 0.10) -> tuple[float, float]:
    if entry <= 0:
        return 0.0, 0.0
    gross = size_usd * (exit_price - entry) / entry
    fees = round_trip_fees(size_usd, entry, exit_price, discount)
    return gross - fees, fees
