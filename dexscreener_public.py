"""DexScreener pair parser. Discovery only. It does not prove a buy.

The documented read is GET
https://api.dexscreener.com/token-pairs/v1/{chainId}/{tokenAddress}
and the fields are marketCap, fdv, liquidity.usd, pairCreatedAt, priceChange,
and txns. marketCap is never replaced with fdv. Transaction counts are not
unique buyers. A boost is not a smart-wallet fact. This module does not open
the network unless a caller passes a transport.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

PAIRS_URL = "https://api.dexscreener.com/token-pairs/v1/solana/"
BOOSTS_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
SOURCE = "dexscreener"
NOT_PROVEN = (
    "unique_buyers",
    "smart_wallets",
    "holder_growth",
    "lp_burned_or_locked",
    "dev_holding_pct",
    "dev_rugs",
    "bundled_launch_pct",
    "mint_authority",
    "freeze_authority",
)


def pairs_url(mint: str) -> str:
    return PAIRS_URL + mint


def parse_boost_mint(payload) -> dict:
    """A boost names a mint. It is not a pass and not a smart-wallet proof."""
    if not isinstance(payload, list):
        return {"ok": False, "reason": "boosts_shape", "source": SOURCE, "smart_wallet_proof": False}
    for item in payload:
        if not isinstance(item, dict):
            continue
        if item.get("chainId") != "solana":
            continue
        mint = item.get("tokenAddress")
        if isinstance(mint, str) and len(mint) >= 32:
            return {"ok": True, "mint": mint, "source": SOURCE, "smart_wallet_proof": False, "boost_is_not_a_pass": True}
    return {"ok": False, "reason": "solana_mint_missing", "source": SOURCE, "smart_wallet_proof": False}


def parse_pairs(payload, mint: str, fetched_at: float) -> dict:
    """One Solana pair for this mint. FDV is reported and then ignored."""
    refused = {"ok": False, "source": SOURCE, "mint": mint, "fdv_used_as_market_cap": False, "not_proven": list(NOT_PROVEN)}
    if not isinstance(payload, list):
        return refused | {"reason": "pairs_shape"}
    chosen = None
    for pair in payload:
        if not isinstance(pair, dict) or pair.get("chainId") != "solana":
            continue
        base = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
        if base.get("address") == mint:
            chosen = pair
            break
    if chosen is None:
        return refused | {"reason": "pair_missing"}
    created = chosen.get("pairCreatedAt")
    if isinstance(created, bool) or not isinstance(created, int):
        return refused | {"reason": "pair_created_at"}
    change = _nested(chosen.get("priceChange"), "h1")
    liquidity = chosen.get("liquidity")
    txns = chosen.get("txns") if isinstance(chosen.get("txns"), dict) else {}
    hour = txns.get("h1") if isinstance(txns.get("h1"), dict) else {}
    if chosen.get("marketCap") is None:
        return refused | {"reason": "market_cap_missing", "fdv_present": chosen.get("fdv") is not None}
    market_cap = _num(chosen.get("marketCap"))
    if market_cap is None:
        return refused | {"reason": "market_cap"}
    if liquidity is None:
        return refused | {"reason": "liquidity_missing", "fdv_present": chosen.get("fdv") is not None}
    liq = _num(_nested(liquidity, "usd"))
    if liq is None:
        return refused | {"reason": "liquidity"}
    price = _num(chosen.get("priceUsd"))
    if price is None:
        return refused | {"reason": "price"}
    if change is None:
        return refused | {"reason": "price_change_h1_missing"}
    move = _num(change)
    if move is None:
        return refused | {"reason": "price"}
    return {
        "ok": True,
        "reason": None,
        "source": SOURCE,
        "mint": mint,
        "fdv_present": chosen.get("fdv") is not None,
        "fdv_used_as_market_cap": False,
        "not_proven": list(NOT_PROVEN),
        "transaction_counts_are_not_unique_buyers": True,
        "scout": {
            "mint": mint,
            "source": SOURCE,
            "fetched_at": fetched_at,
            "pair_created_at": created,
            "market_cap_usd": format(market_cap, "f"),
            "market_cap_basis": "market_cap",
            "liquidity_usd": format(liq, "f"),
            "price_usd": format(price, "f"),
            "price_change_h1": format(move, "f"),
        },
        "evidence": {
            "pair_address": chosen.get("pairAddress") if isinstance(chosen.get("pairAddress"), str) else None,
            "dex_id": chosen.get("dexId") if isinstance(chosen.get("dexId"), str) else None,
            "h1_buys": hour.get("buys"),
            "h1_sells": hour.get("sells"),
            "fdv": chosen.get("fdv"),
        },
    }


def _nested(value, key: str):
    if not isinstance(value, dict):
        return None
    return value.get(key)


def _num(value) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    return number
