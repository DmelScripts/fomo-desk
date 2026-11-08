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


_HARNESS = None
HERMES_LATEST_URL = "https://hermes.pyth.network/v2/updates/price/latest"
CREDENTIAL_ENV = "PYTH_API_KEY"
# A test may install a callable that returns the documented JSON. This module does not call Hermes.
PRICE_TRANSPORT = None


def current(now: float | None = None) -> dict:
    """The configured quote, or a disabled result. This does not fetch.

    A harness is a test double. set_quote remains an injection and is not a live read.
    """
    moment = time.time() if now is None else now
    if not enabled():
        return _unused("usd_source_unapproved")
    if isinstance(_HARNESS, dict):
        body = assess(_HARNESS, moment)
        body["origin"] = "test_harness"
        body["live_provider"] = False
        body["authenticated_live"] = False
        body["fetched"] = False
        return body
    if not isinstance(_QUOTE, dict):
        return _unused("valuation_missing")
    body = assess(_QUOTE, moment)
    body["authenticated_live"] = False
    return body


def install_harness(quote: dict | None) -> None:
    """Test double for the diagnostic service. This is not an authenticated price read."""
    global _HARNESS
    _HARNESS = quote


def authenticated_read(now: float | None = None) -> dict:
    """Documented Hermes latest-price contract. No network call is made.

    A missing credential fails closed. A parsed fixture is not a live provider.
    """
    import os
    moment = time.time() if now is None else now
    refused = {
        "ok": False,
        "usable": False,
        "approved": False,
        "authenticated_live": False,
        "live_provider": False,
        "fetched": False,
        "origin": "hermes",
        "accepted_band": None,
        "credential_env": CREDENTIAL_ENV,
        "url": HERMES_LATEST_URL,
    }
    if not enabled():
        return refused | {"reason": "usd_source_unapproved"}
    credential = os.environ.get(CREDENTIAL_ENV)
    if not isinstance(credential, str) or not credential.strip():
        return refused | {"reason": "credentials_missing"}
    if PRICE_TRANSPORT is None:
        return refused | {"reason": "live_price_call_disabled"}
    try:
        payload = PRICE_TRANSPORT(HERMES_LATEST_URL, FEED_ID)
    except Exception:
        return refused | {"reason": "price_transport"}
    parsed = parse_hermes_latest(payload)
    if parsed is None:
        return refused | {"reason": "valuation_fields"}
    body = assess(parsed, moment)
    body["ok"] = body.get("usable") is True
    body["authenticated_live"] = False
    body["live_provider"] = False
    body["fetched"] = False
    body["origin"] = "hermes_fixture"
    return body


def parse_hermes_latest(payload: dict) -> dict | None:
    """The documented `parsed[].price` object. Binary proof bytes are not treated as a price."""
    if not isinstance(payload, dict):
        return None
    rows = payload.get("parsed")
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return None
    row = rows[0]
    price = row.get("price")
    if not isinstance(price, dict):
        return None
    feed = str(row.get("id") or "").removeprefix("0x")
    parsed = {
        "feed_id": feed,
        "price": _hermes_int(price.get("price")),
        "conf": _hermes_int(price.get("conf")),
        "expo": _hermes_int(price.get("expo")),
        "publish_time": _hermes_int(price.get("publish_time")),
    }
    if any(value is None for value in parsed.values()):
        return None
    return parsed


def _hermes_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None


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
        "price": price,
        "conf": conf,
        "expo": expo,
        "upper": format(upper, "f"),
        "lower": format(lower, "f"),
        "live_provider": False,
        "fetched": False,
        "origin": "injected_quote",
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


def snapshot(quote: dict) -> dict:
    """Freeze one injected quote. A later set_quote does not rewrite this copy."""
    if not isinstance(quote, dict):
        return {"usable": False, "live_provider": False, "fetched": False, "origin": "injected_quote", "accepted_band": None}
    return {
        "usable": quote.get("usable") is True,
        "live_provider": False,
        "fetched": False,
        "origin": quote.get("origin") or "injected_quote",
        "authenticated_live": quote.get("authenticated_live") is True,
        "accepted_band": None,
        "reason": quote.get("reason"),
        "upper": quote.get("upper"),
        "lower": quote.get("lower"),
        "publish_time": quote.get("publish_time"),
        "feed_id": quote.get("feed_id"),
        "price": quote.get("price"),
        "conf": quote.get("conf"),
        "expo": quote.get("expo"),
    }


def round_trip_pnl(entry_sol: int, entry_fee: int, exit_sol: int, exit_fee: int, entry_micro: int, exit_micro: int, entry_quote: dict, exit_quote: dict) -> dict:
    """SOL out once, both chain fees once, SOL back once. The DEX stays inside those SOL amounts.

    Cost uses the entry quote's upper bound. Proceeds and the exit fee use the exit quote.
    """
    if not isinstance(entry_quote, dict) or not isinstance(exit_quote, dict) or entry_quote.get("usable") is not True or exit_quote.get("usable") is not True:
        return {"ok": False, "reason": "valuation_missing", "realized_pnl": None, "dex_added_twice": False}
    cost_micro = _ceil_lamports(entry_sol, entry_micro) + _ceil_lamports(entry_fee, entry_micro)
    exit_fee_micro = _ceil_lamports(exit_fee, exit_micro)
    proceeds_micro = _floor_lamports(exit_sol, exit_micro)
    if cost_micro is None or exit_fee_micro is None or proceeds_micro is None:
        return {"ok": False, "reason": "valuation_fields", "realized_pnl": None, "dex_added_twice": False}
    cost = usd_ceiling(cost_micro, entry_quote)
    fee = usd_ceiling(exit_fee_micro, exit_quote)
    proceeds = usd_floor(proceeds_micro, exit_quote)
    if cost is None or fee is None or proceeds is None:
        return {"ok": False, "reason": entry_quote.get("reason") or exit_quote.get("reason") or "valuation_missing", "realized_pnl": None, "dex_added_twice": False}
    signed = proceeds - cost - fee
    return {
        "ok": True,
        "reason": None,
        "realized_pnl": format(signed, "f"),
        "cost_usd": format(cost, "f"),
        "exit_fee_usd": format(fee, "f"),
        "proceeds_usd": format(proceeds, "f"),
        "dex_added_twice": False,
        "proceeds_are_exit_sol": True,
    }


def _ceil_lamports(lamports: int, micro_per_sol: int) -> int | None:
    if isinstance(lamports, bool) or isinstance(micro_per_sol, bool):
        return None
    if not isinstance(lamports, int) or not isinstance(micro_per_sol, int) or lamports < 0 or micro_per_sol <= 0:
        return None
    if lamports == 0:
        return 0
    return (lamports * micro_per_sol + 10**9 - 1) // 10**9


def _floor_lamports(lamports: int, micro_per_sol: int) -> int | None:
    if isinstance(lamports, bool) or isinstance(micro_per_sol, bool):
        return None
    if not isinstance(lamports, int) or not isinstance(micro_per_sol, int) or lamports < 0 or micro_per_sol <= 0:
        return None
    return (lamports * micro_per_sol) // 10**9


def set_quote(quote: dict | None) -> None:
    """Install a test dict. This is not a Hermes read and not a live provider."""
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
        "live_provider": False,
        "fetched": False,
        "origin": "injected_quote",
    }


def _int(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value
