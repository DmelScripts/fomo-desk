"""USDC per USD from Pyth's published price and confidence. Off unless configured.

Hermes price reads require a Pyth API key (since 26 August 2026) and are not
called. The on-chain feed is permissionless, but this process does not read
mainnet. Tests supply the documented price, conf, expo, and publish_time.
No depeg band is added. The source's own confidence is the bound: a fee or
loss is priced at price+conf, and proceeds at price-conf. If that interval
is not strictly positive, or the timestamp is stale, the quote is unusable
and the existing USD limits fail closed.

Documented sources, not live data:
- https://docs.pyth.network/llms-price-feeds-core.txt
- https://docs.pyth.network/pro/understanding-price-data
- Feed id: https://pyth.network/developers/price-feed-ids
  USDC/USD eaa020c61cc479712813461ce153894a96a6c00b21ed0cfc2798d1f9a9e9c94a
- Hermes latest prices require Authorization: https://docs.pyth.network/api-reference/hermes/price-updates-in-interval
- Feed metadata without a key is not a price: https://docs.pyth.network/api-reference/hermes/price-feeds
"""

from __future__ import annotations

import time
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

import db

FEED_ID = "eaa020c61cc479712813461ce153894a96a6c00b21ed0cfc2798d1f9a9e9c94a"
SOURCE = "pyth_usdc_usd"
_KEY = "usdc_valuation_enabled"
# Same wall-clock window as a chain quote. Not a depeg tolerance.
MAX_AGE_S = 30
_QUOTE = None


def current(now: float | None = None) -> dict:
    """The configured quote, or a disabled result. This does not fetch."""
    if not enabled():
        return _unused("usd_source_unapproved")
    if not isinstance(_QUOTE, dict):
        return _unused("valuation_missing")
    return assess(_QUOTE, time.time() if now is None else now)


def assess(quote: dict, now: float) -> dict:
    """Scale price ± conf. Reject a stale, non-positive, or mis-scaled quote."""
    if not isinstance(quote, dict):
        return _unused("valuation_missing")
    feed = str(quote.get("feed_id") or FEED_ID).removeprefix("0x")
    if feed != FEED_ID:
        return _unused("valuation_feed")
    price = _int(quote.get("price"))
    conf = _int(quote.get("conf"))
    expo = _int(quote.get("expo"))
    published = _int(quote.get("publish_time"))
    if price is None or conf is None or expo is None or published is None:
        return _unused("valuation_fields")
    if conf < 0 or expo < -12 or expo > 0:
        return _unused("valuation_scale")
    if isinstance(now, bool) or not isinstance(now, (int, float)):
        return _unused("valuation_clock")
    age = float(now) - published
    if age < -5 or age > MAX_AGE_S:
        return _unused("valuation_stale")
    scale = Decimal(10) ** expo
    upper = (Decimal(price) + Decimal(conf)) * scale
    lower = (Decimal(price) - Decimal(conf)) * scale
    if lower <= 0 or upper <= 0:
        return _unused("valuation_not_positive")
    return {
        "usable": True,
        "approved": True,
        "source": SOURCE,
        "accepted_band": None,
        "reason": None,
        "pool_micro_usdc_is_usd": False,
        "confidence": "published_conf",
        "feed_id": FEED_ID,
        "publish_time": published,
        "upper": format(upper, "f"),
        "lower": format(lower, "f"),
    }


def usd(micro_usdc: int, rate: str, rounding) -> Decimal | None:
    """Convert pool micro-USDC at a USDC/USD rate. None when the rate is absent."""
    if isinstance(micro_usdc, bool) or not isinstance(micro_usdc, int) or micro_usdc < 0:
        return None
    try:
        value = (Decimal(micro_usdc) / Decimal(1_000_000)) * Decimal(rate)
    except Exception:
        return None
    if not value.is_finite():
        return None
    return value.quantize(Decimal("0.01"), rounding=rounding)


def usd_ceiling(micro_usdc: int, quote: dict) -> Decimal | None:
    if not quote.get("usable"):
        return None
    return usd(micro_usdc, quote["upper"], ROUND_CEILING)


def usd_floor(micro_usdc: int, quote: dict) -> Decimal | None:
    if not quote.get("usable"):
        return None
    return usd(micro_usdc, quote["lower"], ROUND_FLOOR)


def within_limits(fee_micro: int, ticket_micro: int, exposure_micro: int, quote: dict) -> dict:
    """Compare ceilings to the existing 0.05 / 8 / 100 limits. No new limit is introduced."""
    fee = usd_ceiling(fee_micro, quote)
    ticket = usd_ceiling(ticket_micro, quote)
    exposure = usd_ceiling(exposure_micro, quote)
    if fee is None or ticket is None or exposure is None:
        return {"ok": False, "reason": quote.get("reason") or "usd_source_unapproved"}
    fee_ok = fee <= Decimal("0.05")
    ticket_ok = ticket <= Decimal("8.00")
    exposure_ok = exposure <= Decimal("100.00")
    return {
        "ok": fee_ok and ticket_ok and exposure_ok,
        "reason": None if fee_ok and ticket_ok and exposure_ok else "cap",
        "fee_usd": format(fee, "f"),
        "ticket_usd": format(ticket, "f"),
        "exposure_usd": format(exposure, "f"),
        "fee_ok": fee_ok,
        "ticket_ok": ticket_ok,
        "exposure_ok": exposure_ok,
    }


def conservative_loss(spent_micro: int, proceeds_micro: int, quote: dict) -> dict:
    """Cost at the high USDC price, proceeds at the low one. Fees belong in spent once."""
    spent = usd_ceiling(spent_micro, quote)
    proceeds = usd_floor(proceeds_micro, quote)
    if spent is None or proceeds is None:
        return {"ok": False, "reason": quote.get("reason") or "usd_source_unapproved", "loss_usd": None}
    loss = spent - proceeds
    return {"ok": True, "reason": None, "loss_usd": format(loss, "f"), "spent_usd": format(spent, "f"), "proceeds_usd": format(proceeds, "f")}


def enabled() -> bool:
    rows = db.query("SELECT value FROM phantom_control WHERE key = ?", (_KEY,))
    return bool(rows) and rows[0]["value"] == "1"


def enable() -> None:
    db.execute(
        """
        INSERT INTO phantom_control (key, value, updated_at) VALUES (?, '1', ?)
        ON CONFLICT(key) DO UPDATE SET value = '1', updated_at = excluded.updated_at
        """,
        (_KEY, time.time()),
    )


def set_quote(quote: dict | None) -> None:
    """Test fixture only. Production leaves this unset."""
    global _QUOTE
    _QUOTE = quote


def _unused(reason: str) -> dict:
    return {
        "usable": False,
        "approved": False,
        "source": SOURCE if reason != "usd_source_unapproved" else None,
        "accepted_band": None,
        "reason": reason,
        "pool_micro_usdc_is_usd": False,
        "feed_id": FEED_ID,
    }


def _int(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value
