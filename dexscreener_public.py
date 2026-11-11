"""DexScreener pair parser. Discovery only. It does not prove a buy.

The documented read is GET
https://api.dexscreener.com/token-pairs/v1/{chainId}/{tokenAddress}
and the body is a JSON array of pair objects. The saved dex_pair files came
from GET /latest/dex/pairs/solana/{pair}, whose body is {schemaVersion, pairs}.
The live mint read was GET /tokens/v1/solana/{mint} and is the same array.
and the fields are marketCap, fdv, liquidity.usd, pairCreatedAt, priceChange,
and txns. marketCap is never replaced with fdv. Transaction counts are not
unique buyers. A boost is not a smart-wallet fact. This module does not open
the network unless a caller passes a transport.
"""

from __future__ import annotations

from decimal import Decimal

PAIRS_URL = "https://api.dexscreener.com/token-pairs/v1/solana/"
BOOSTS_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
SOURCE = "dexscreener"
WHIRLPOOL_DEX_ID = "whirlpool"
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


def supported_pool(evidence: dict) -> dict:
    """The pair address is a Whirlpool only when DexScreener says so. No substitute pool."""
    if not isinstance(evidence, dict):
        return {"ok": False, "reason": "pool_identity_missing", "pool": None}
    dex_id = evidence.get("dex_id")
    pool = evidence.get("pair_address")
    if dex_id != WHIRLPOOL_DEX_ID:
        return {"ok": False, "reason": "pool_dex_unsupported", "dex_id": dex_id if isinstance(dex_id, str) else None, "pool": None}
    if not isinstance(pool, str) or len(pool) < 32:
        return {"ok": False, "reason": "pool_identity_missing", "dex_id": dex_id, "pool": None}
    return {"ok": True, "reason": None, "dex_id": dex_id, "pool": pool}


def pairs_url(mint: str) -> str:
    return PAIRS_URL + mint


def parse_boost_mints(payload, limit: int = 30) -> dict:
    """Every Solana mint in the boost list, in list order, bounded. Not a pass."""
    if not isinstance(payload, list):
        return {"ok": False, "reason": "boosts_shape", "source": SOURCE, "mints": [], "skipped": [], "smart_wallet_proof": False}
    mints = []
    skipped = []
    for item in payload:
        if len(mints) >= limit:
            break
        if not isinstance(item, dict):
            skipped.append({"reason": "boost_shape", "source": SOURCE})
            continue
        if item.get("chainId") != "solana":
            skipped.append({"chainId": item.get("chainId"), "reason": "not_solana", "source": SOURCE})
            continue
        mint = item.get("tokenAddress")
        if not isinstance(mint, str) or len(mint) < 32:
            skipped.append({"reason": "mint", "source": SOURCE})
            continue
        mints.append(mint)
    if not mints:
        return {"ok": False, "reason": "solana_mint_missing", "source": SOURCE, "mints": [], "skipped": skipped, "smart_wallet_proof": False}
    return {"ok": True, "reason": None, "source": SOURCE, "mints": mints, "skipped": skipped, "smart_wallet_proof": False, "boost_is_not_a_pass": True}


def parse_boost_mint(payload) -> dict:
    """The first Solana mint. Discovery uses parse_boost_mints so the rest are not dropped."""
    found = parse_boost_mints(payload, limit=30)
    if found.get("ok") is not True:
        return found
    return found | {"mint": found["mints"][0]}


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
    import cross_source
    selected, why = cross_source.select_pair(payload, mint, "")
    if selected is None:
        quote = chosen.get("quoteToken")
        if not isinstance(quote, dict) or not isinstance(quote.get("address"), str) or quote.get("address") == "":
            return refused | {"reason": "quote_unknown"}
        if why == "pool_ambiguous":
            return refused | {"reason": "pool_ambiguous"}
        if quote.get("address") != __import__("solana_tx").NATIVE_MINT:
            return refused | {"reason": "quote_not_sol"}
        return refused | {"reason": why or "pool_missing"}
    chosen = selected
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
            "market_cap_basis": "marketCap",
            "pair_address": chosen.get("pairAddress"),
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
    import memecoin_desk
    return memecoin_desk._num(value)
