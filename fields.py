"""STEP 2 field map — exact key paths from logs/raw samples.

Captured 2026-11-06 for graduated mint
4FHEBKNYy6BZ1QFMKj1xL8HDTM12rdXLRYw8DgiCUnQn (PumpPortal txType=migrate).

Every path was read from a file under logs/raw/. A value of None means that
key was not present in the captured JSON. Callers must not invent a fallback
path without updating this map from a new raw sample.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

SAMPLE_MINT = "4FHEBKNYy6BZ1QFMKj1xL8HDTM12rdXLRYw8DgiCUnQn"

SOURCES = {
    "migration": "logs/raw/pumpportal_migration.json",
    "dexscreener": "logs/raw/dexscreener_pair.json",
    "rugcheck_report": "logs/raw/rugcheck_report.json",
    "rugcheck_graph": "logs/raw/rugcheck_insiders_graph.json",
    "rugcheck_graph_schema": "logs/raw/rugcheck_insiders_graph.schema_sample.json",
    "rpc_account": "logs/raw/rpc_getAccountInfo.json",
    "rpc_supply": "logs/raw/rpc_getTokenSupply.json",
    "rpc_largest": "logs/raw/rpc_getTokenLargestAccounts.json",
}

# ---------------------------------------------------------------------------
# PumpPortal subscribeMigration message
# File is one JSON object. Keys observed: signature, mint, txType, pool.
# ---------------------------------------------------------------------------

MIGRATION = {
    "mint": "mint",
    "pool": "pool",
    # Sample value of pool was the string "pump-amm", not a base58 pool address.
    "signature": "signature",
    "creator": None,  # not present; Auditor should fall back to RugCheck creator
}

MIGRATION_NOTES = {
    "pool": (
        "Present, but in this sample the value is the AMM label 'pump-amm', "
        "not a pool pubkey. The PumpSwap pool address appeared later as "
        "DexScreener pairAddress and RugCheck markets[].pubkey "
        "(GWc8HL8vCGMnmg3n9pC4tmWafYiWpuoxvKrrhzDVZyJf)."
    ),
    "creator": "Key absent from the migration message.",
    "extra_keys": "txType (sample: 'migrate') was present and is unused by agents.",
}

# ---------------------------------------------------------------------------
# DexScreener GET /tokens/v1/solana/{mint}
# Response is a JSON list of pair objects. Select dexId == "pumpswap";
# if several, the one with the highest liquidity.usd (master prompt rule).
# First fetch right after migration had only a dexId=pumpfun pair with
# liquidity=null (saved as dexscreener_pair.initial_pumpfun_only.json).
# Official file is the later pumpswap pair, which has liquidity.usd.
# ---------------------------------------------------------------------------

DEXSCREENER_RESPONSE = "list[pair]"
DEXSCREENER_SELECT = "pair.dexId == 'pumpswap'; if several, max pair.liquidity.usd"

DEXSCREENER = {
    "pairAddress": "pairAddress",
    "dexId": "dexId",
    "baseToken.name": "baseToken.name",
    "baseToken.symbol": "baseToken.symbol",
    "priceUsd": "priceUsd",  # string in this sample, e.g. "0.00003086"
    "liquidity.usd": "liquidity.usd",
    "marketCap": "marketCap",
    "volume.m5": "volume.m5",
    "volume.h1": "volume.h1",
    "volume.h24": "volume.h24",
    "txns.m5.buys": "txns.m5.buys",
    "txns.m5.sells": "txns.m5.sells",
    "txns.h1.buys": "txns.h1.buys",
    "txns.h1.sells": "txns.h1.sells",
    "priceChange.m5": "priceChange.m5",
    "priceChange.h1": "priceChange.h1",
    "priceChange.h24": "priceChange.h24",
    "pairCreatedAt": "pairCreatedAt",  # int epoch milliseconds
}

# ---------------------------------------------------------------------------
# RugCheck GET /v1/tokens/{mint}/report
# ---------------------------------------------------------------------------

RUGCHECK_REPORT = {
    "creator": "creator",
    "creatorBalance": "creatorBalance",
    "topHolders": "topHolders",
    "tokenMeta.name": "tokenMeta.name",
    "tokenMeta.symbol": "tokenMeta.symbol",
    # There is no markets[].pool key. The market/pool id is `pubkey`.
    "markets[].pool": None,
    "markets[].pool_key": "markets[].pubkey",
    "markets[].marketType": "markets[].marketType",  # sample: "pump_fun_amm"
    "lp_locked_pct": "markets[].lp.lpLockedPct",
    "lp_burned_pct": None,  # no burned / burn / lpBurned* key anywhere in the report
}

RUGCHECK_REPORT_NOTES = {
    "markets[].pool": (
        "No key named 'pool' on markets[]. Observed pool identifier is "
        "markets[].pubkey. In this sample it equals the DexScreener pumpswap "
        "pairAddress."
    ),
    "lp_burned_pct": (
        "No burned-percent field. Related keys that are not a burned pct: "
        "markets[].lp.lpLocked, lpUnlocked, lpLockedPct, lpCurrentSupply, "
        "currentSupply, totalTokensUnlocked."
    ),
}

# ---------------------------------------------------------------------------
# RugCheck GET /v1/tokens/{mint}/insiders/graph
# This mint: JSON `[]` (empty list). graphInsidersDetected on the report was 0.
# A supplemental call on another token (see schema sample file) returned a
# non-empty list of network objects. There is no top-level {nodes, edges}
# object and no key named "edges".
# ---------------------------------------------------------------------------

RUGCHECK_GRAPH_THIS_MINT = "[]"  # logs/raw/rugcheck_insiders_graph.json

RUGCHECK_GRAPH = {
    "nodes": "[].nodes",
    "edges": None,  # key 'edges' was not present
    "links": "[].links",  # observed edge-equivalent: source/target pairs
    "holder_flag": None,  # no boolean holder / isHolder key
    "holdings": "[].nodes[].holdings",  # numeric; 0 means no balance in the sample
    "node_id": "[].nodes[].id",
    "participant": "[].nodes[].participant",
}

RUGCHECK_GRAPH_NOTES = {
    "shape": (
        "Array of {net_id, network_type, nodes, links, relatedMint}. "
        "This mint's capture is an empty array, so those keys exist only on "
        "the supplemental schema sample."
    ),
    "edges": "Not present. Observed equivalent is links[{source, target}].",
    "holder_flag": (
        "Not present. Closest observed field is nodes[].holdings (number). "
        "nodes[].participant was True for every node in the schema sample."
    ),
}

# ---------------------------------------------------------------------------
# Solana RPC
# ---------------------------------------------------------------------------

RPC_GET_ACCOUNT_INFO = {
    "mintAuthority": "result.value.data.parsed.info.mintAuthority",
    "freezeAuthority": "result.value.data.parsed.info.freezeAuthority",
    "supply": "result.value.data.parsed.info.supply",  # string
    "decimals": "result.value.data.parsed.info.decimals",
    "owner_program": "result.value.owner",
}

RPC_GET_TOKEN_SUPPLY = {
    "supply": "result.value.amount",
    "decimals": "result.value.decimals",
    "uiAmount": "result.value.uiAmount",
    "uiAmountString": "result.value.uiAmountString",
}

# Public RPC returned error 429 "Too many requests for a specific RPC call"
# on every attempt. No successful body, so no field paths observed.
RPC_GET_TOKEN_LARGEST_ACCOUNTS = {
    "accounts": None,
    "address": None,
    "amount": None,
    "uiAmount": None,
}

RPC_NOTES = {
    "owner_program": (
        "This mint's owner is TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb "
        "(Token-2022)."
    ),
    "getTokenLargestAccounts": (
        "Unobserved: api.mainnet-beta.solana.com returned RPC error 429 on "
        "repeated attempts. Sample file is the error body."
    ),
}

# jsonParsed token-account paths from getProgramAccounts (layer 8 sample)
RPC_TOKEN_ACCOUNT = {
    "pubkey": "[].pubkey",
    "mint": "[].account.data.parsed.info.mint",
    "owner": "[].account.data.parsed.info.owner",
    "state": "[].account.data.parsed.info.state",
    "amount": "[].account.data.parsed.info.tokenAmount.amount",
    "uiAmount": "[].account.data.parsed.info.tokenAmount.uiAmount",
    "delegate": None,  # key absent on all 354 Token-2022 accounts in the sample
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parts(path: str) -> list[str | int]:
    out: list[str | int] = []
    for raw in path.split("."):
        if raw.endswith("]") and "[" in raw:
            name, idx = raw[:-1].split("[", 1)
            if name:
                out.append(name)
            if idx == "":
                raise ValueError(f"wildcard index not supported in get(): {path}")
            out.append(int(idx))
        else:
            out.append(raw)
    return out


def get(obj: Any, path: str | None, default: Any = None) -> Any:
    """Read a dotted path such as 'result.value.data.parsed.info.supply'.

    Returns default if path is None or any key/index is missing.
    Does not support [] wildcards (use an explicit index, e.g. markets[0].pubkey).
    """
    if path is None:
        return default
    cur = obj
    try:
        for part in _parts(path):
            if isinstance(part, int):
                cur = cur[part]
            else:
                cur = cur[part]
    except (KeyError, IndexError, TypeError):
        return default
    return cur


def dexscreener_select_pair(pairs: Any) -> dict[str, Any] | None:
    """Pick the PumpSwap pair, or None if DexScreener has no pumpswap pair yet."""
    if not isinstance(pairs, list):
        return None
    pumpswap = [p for p in pairs if isinstance(p, dict) and p.get("dexId") == "pumpswap"]
    if not pumpswap:
        return None

    def liq_usd(pair: dict[str, Any]) -> float:
        liq = pair.get("liquidity")
        if isinstance(liq, dict) and isinstance(liq.get("usd"), (int, float)):
            return float(liq["usd"])
        return float("-inf")

    return max(pumpswap, key=liq_usd)


def rugcheck_market_for_pool(report: Any, pool: str | None) -> dict[str, Any] | None:
    """Return the markets[] entry whose pubkey equals pool (no 'pool' key exists)."""
    markets = get(report, "markets")
    if not isinstance(markets, list) or not pool:
        return None
    for market in markets:
        if isinstance(market, dict) and market.get("pubkey") == pool:
            return market
    return None
