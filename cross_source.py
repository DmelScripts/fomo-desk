"""Fail-closed reads of the 2026-11-10 live fixtures.

DexScreener liquidity.usd counts both sides at DexScreener's own price and is
not the liquidity figure. Quote-side USD comes from RugCheck
markets[].lp.quoteUSD and from Solana Tracker pools[].liquidity.quote scaled
by that pool's marketCap.usd / marketCap.quote. The two must match by pool id.
They disagree when the larger is more than three times the smaller
(LIQUIDITY_SOURCE_TOLERANCE). Market cap uses marketCap, never fdv, and the
scout window has to be confirmed by Solana Tracker as well as DexScreener.

Provenance: tests/fixtures/live_readonly_2026_11_10 (keys redacted, each
response wrapped as {meta, body}).
"""

from __future__ import annotations

from decimal import Decimal

import memecoin_desk
import public_filter_docs
import solana_tx

SOL_MINT = solana_tx.NATIVE_MINT
SOL_SYMBOLS = {"SOL", "wSOL", "WSOL"}
# Larger / smaller above this is not the same pool figure.
LIQUIDITY_SOURCE_TOLERANCE = Decimal("3")
MCAP_SOURCE_TOLERANCE = Decimal("3")
CREATOR_RUG_RISK = "Creator history of rugged tokens"
DAS_PAGE_LIMIT = 20


def quote_is_sol(quote) -> bool:
    if not isinstance(quote, dict):
        return False
    if quote.get("address") == SOL_MINT:
        return True
    return quote.get("symbol") in SOL_SYMBOLS


def within(left, right, tolerance=LIQUIDITY_SOURCE_TOLERANCE) -> bool:
    if left is None or right is None or left <= 0 or right <= 0:
        return False
    return max(left, right) / min(left, right) <= tolerance


def _money(value):
    return memecoin_desk._money(value) if not isinstance(value, bool) else None


def _num(value):
    return memecoin_desk._num(value)


def unwrap(payload):
    """A saved response is {meta, body}. A bare body is accepted in tests."""
    if isinstance(payload, dict) and "body" in payload and "meta" in payload:
        return payload.get("body")
    return payload


def _pair(dex_body):
    body = unwrap(dex_body)
    if not isinstance(body, dict):
        return None
    pairs = body.get("pairs")
    if isinstance(pairs, list):
        for pair in pairs:
            if isinstance(pair, dict):
                return pair
    pair = body.get("pair")
    return pair if isinstance(pair, dict) else None


def _matched_market(rugcheck_body, pool_id):
    body = unwrap(rugcheck_body)
    if not isinstance(body, dict):
        return None
    for market in body.get("markets") or []:
        if isinstance(market, dict) and market.get("pubkey") == pool_id:
            return market
    return None


def _matched_pool(tracker_body, pool_id):
    body = unwrap(tracker_body)
    if not isinstance(body, dict):
        return None
    for pool in body.get("pools") or []:
        if isinstance(pool, dict) and pool.get("poolId") == pool_id:
            return pool
    return None


def _quote_side_usd(pool) -> Decimal | None:
    """Solana Tracker quote-token liquidity, in USD, from that pool's own price."""
    if not isinstance(pool, dict):
        return None
    liquidity = pool.get("liquidity") if isinstance(pool.get("liquidity"), dict) else {}
    cap = pool.get("marketCap") if isinstance(pool.get("marketCap"), dict) else {}
    quote_size = _money(liquidity.get("quote"))
    cap_quote = _money(cap.get("quote"))
    cap_usd = _money(cap.get("usd"))
    if quote_size is None or cap_quote is None or cap_usd is None or cap_quote <= 0:
        return None
    return quote_size * cap_usd / cap_quote


def liquidity_check(dex_body, rugcheck_body, tracker_body) -> dict:
    """Quote-side liquidity over a second-source market cap. DexScreener is not the numerator."""
    pair = _pair(dex_body)
    if pair is None or not isinstance(pair.get("pairAddress"), str):
        return {"status": "FAIL-CLOSED", "reason": "liquidity_source_missing"}
    pool_id = pair["pairAddress"]
    market = _matched_market(rugcheck_body, pool_id)
    pool = _matched_pool(tracker_body, pool_id)
    lp = market.get("lp") if isinstance(market, dict) and isinstance(market.get("lp"), dict) else None
    rc_quote = _money(lp.get("quoteUSD")) if lp else None
    st_quote = _quote_side_usd(pool)
    st_cap = _money((pool.get("marketCap") or {}).get("usd")) if isinstance(pool, dict) and isinstance(pool.get("marketCap"), dict) else None
    dex_cap = _money(pair.get("marketCap"))
    dex_liq = _money((pair.get("liquidity") or {}).get("usd")) if isinstance(pair.get("liquidity"), dict) else None
    if rc_quote is None or st_quote is None or st_cap is None or dex_cap is None:
        return {"status": "FAIL-CLOSED", "reason": "liquidity_source_missing", "quote_is_sol": quote_is_sol(pair.get("quoteToken"))}
    conflict = (
        not within(rc_quote, st_quote)
        or not within(dex_cap, st_cap, MCAP_SOURCE_TOLERANCE)
        or (dex_liq is not None and not within(dex_liq, min(rc_quote, st_quote)) and not within(dex_liq, max(rc_quote, st_quote)))
    )
    if conflict:
        return {
            "status": "FAIL-CLOSED",
            "reason": "liquidity_source_conflict",
            "quote_usd": format(min(rc_quote, st_quote), "f"),
            "market_cap_usd": format(st_cap, "f"),
            "quote_is_sol": quote_is_sol(pair.get("quoteToken")),
        }
    used = min(rc_quote, st_quote)
    ratio = used / st_cap if st_cap > 0 else None
    if ratio is None or ratio < memecoin_desk.LIQ_OF_MCAP:
        return {
            "status": "FAIL",
            "reason": "liquidity_below_25pct_mcap",
            "quote_usd": format(used, "f"),
            "market_cap_usd": format(st_cap, "f"),
            "quote_is_sol": quote_is_sol(pair.get("quoteToken")),
        }
    return {
        "status": "PASS",
        "reason": None,
        "quote_usd": format(used, "f"),
        "market_cap_usd": format(st_cap, "f"),
        "quote_is_sol": quote_is_sol(pair.get("quoteToken")),
    }


def _creators(rugcheck_body, tracker_body) -> tuple[str | None, str | None]:
    rc = unwrap(rugcheck_body)
    st = unwrap(tracker_body)
    rc_creator = rc.get("creator") if isinstance(rc, dict) else None
    token = st.get("token") if isinstance(st, dict) and isinstance(st.get("token"), dict) else {}
    creation = token.get("creation") if isinstance(token.get("creation"), dict) else {}
    st_creator = creation.get("creator")
    if not isinstance(rc_creator, str) or rc_creator == "":
        rc_creator = None
    if not isinstance(st_creator, str) or st_creator == "":
        st_creator = None
    return rc_creator, st_creator


def _risk_names(rugcheck_body) -> list[str]:
    body = unwrap(rugcheck_body)
    names = []
    if not isinstance(body, dict):
        return names
    for item in body.get("risks") or []:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            names.append(item["name"])
        elif isinstance(item, str):
            names.append(item)
    return names


def dev_check(rugcheck_body, tracker_body) -> dict:
    rc_creator, st_creator = _creators(rugcheck_body, tracker_body)
    if rc_creator is None or st_creator is None or rc_creator != st_creator:
        return {"status": "FAIL-CLOSED", "reason": "creator_conflict", "rc_creator": rc_creator, "st_creator": st_creator}
    rc = unwrap(rugcheck_body)
    token = rc.get("token") if isinstance(rc.get("token"), dict) else {}
    supply = _num(token.get("supply"))
    balance = _num(rc.get("creatorBalance"))
    st = unwrap(tracker_body)
    dev = st.get("risk") if isinstance(st, dict) and isinstance(st.get("risk"), dict) else {}
    dev = dev.get("dev") if isinstance(dev.get("dev"), dict) else {}
    st_pct = _num(dev.get("percentage"))
    if supply is None or supply <= 0 or balance is None or balance < 0 or st_pct is None or st_pct < 0:
        return {"status": "FAIL-CLOSED", "reason": "dev_holding_missing"}
    pct = balance / supply * Decimal(100)
    if pct > memecoin_desk.DEV_HOLDING_MAX or st_pct > memecoin_desk.DEV_HOLDING_MAX:
        return {"status": "FAIL", "reason": "dev_holding", "dev_holding_pct": format(pct, "f")}
    return {"status": "PASS", "reason": None, "dev_holding_pct": format(pct, "f")}


def enumeration_gap(das_body, deployer_body, rugcheck_body, mint: str) -> str | None:
    """A missing, capped, or disagreeing past-token list cannot clear a dev."""
    das = unwrap(das_body)
    result = das.get("result") if isinstance(das, dict) and isinstance(das.get("result"), dict) else das
    if not isinstance(result, dict):
        return "das_unparsed"
    total = result.get("total")
    items = result.get("items")
    if isinstance(total, bool) or not isinstance(total, int) or not isinstance(items, list):
        return "das_unparsed"
    if total == 0 or len(items) == 0:
        return "das_empty"
    if total > len(items) or len(items) >= DAS_PAGE_LIMIT:
        return "das_capped"
    deployer = unwrap(deployer_body)
    dep_total = deployer.get("total") if isinstance(deployer, dict) else None
    rc = unwrap(rugcheck_body)
    creator_tokens = rc.get("creatorTokens") if isinstance(rc, dict) else None
    if isinstance(dep_total, bool) or not isinstance(dep_total, int):
        return "deployer_missing"
    if creator_tokens is None:
        return "creator_tokens_missing"
    if not isinstance(creator_tokens, list):
        return "creator_tokens_unparsed"
    if dep_total != len(creator_tokens):
        return "enumeration_disagreement"
    if dep_total != total:
        return "enumeration_disagreement"
    return None


def rug_check(rugcheck_body, tracker_body, das_body, deployer_body, mint: str) -> dict:
    rc_creator, st_creator = _creators(rugcheck_body, tracker_body)
    if rc_creator is None or st_creator is None or rc_creator != st_creator:
        return {"status": "FAIL-CLOSED", "reason": "creator_conflict"}
    names = _risk_names(rugcheck_body)
    gap = enumeration_gap(das_body, deployer_body, rugcheck_body, mint)
    if CREATOR_RUG_RISK in names or any("rugged" in name.lower() for name in names):
        return {"status": "FAIL", "reason": "creator_rugged_history", "enumeration_incomplete": gap is not None, "gap": gap}
    if gap is not None:
        return {"status": "FAIL-CLOSED", "reason": gap}
    return {"status": "PASS", "reason": None, "enumeration_incomplete": False}


def holder_check(chart_body, tracker_body, rugcheck_body, mint: str) -> dict:
    parsed = public_filter_docs.parse_holder_chart(unwrap(chart_body), mint)
    st = unwrap(tracker_body)
    rc = unwrap(rugcheck_body)
    counts = []
    for value in (st.get("holders") if isinstance(st, dict) else None, rc.get("totalHolders") if isinstance(rc, dict) else None):
        if isinstance(value, int) and not isinstance(value, bool):
            counts.append(value)
    disagreement = len(set(counts)) > 1
    if parsed.get("ok") is not True:
        return {
            "status": "FAIL-CLOSED",
            "reason": "holder_growth_unproven",
            "nonzero_points": parsed.get("nonzero_points", parsed.get("points")),
            "holder_count_disagreement": disagreement,
            "counts": counts,
        }
    return {
        "status": "PASS",
        "reason": None,
        "nonzero_points": parsed.get("nonzero_points"),
        "holder_count_disagreement": disagreement,
        "counts": counts,
    }


def authority_check(rugcheck_body, tracker_body, pool_id: str) -> dict:
    rc = unwrap(rugcheck_body)
    pool = _matched_pool(tracker_body, pool_id)
    security = pool.get("security") if isinstance(pool, dict) and isinstance(pool.get("security"), dict) else None
    if not isinstance(rc, dict) or security is None:
        return {"status": "FAIL-CLOSED", "reason": "authority_missing"}
    revoked = rc.get("mintAuthority") is None and rc.get("freezeAuthority") is None
    revoked = revoked and security.get("mintAuthority") is None and security.get("freezeAuthority") is None
    if not revoked:
        return {"status": "FAIL", "reason": "authority"}
    return {"status": "PASS", "reason": None}


def lp_check(rugcheck_body, tracker_body, pool_id: str) -> dict:
    market = _matched_market(rugcheck_body, pool_id)
    pool = _matched_pool(tracker_body, pool_id)
    lp = market.get("lp") if isinstance(market, dict) else None
    locked = lp.get("lpLockedPct") if isinstance(lp, dict) else None
    burn = pool.get("lpBurn") if isinstance(pool, dict) else None
    if locked != 100 or burn != 100:
        return {"status": "FAIL-CLOSED", "reason": "lp"}
    return {"status": "PASS", "reason": None}


def judge(bundle: dict) -> dict:
    """One token. bundle carries the saved bodies. Every check is reported."""
    dex = bundle.get("dex")
    rc = bundle.get("rugcheck")
    st = bundle.get("tracker")
    pair = _pair(dex) or {}
    pool_id = pair.get("pairAddress") if isinstance(pair.get("pairAddress"), str) else ""
    mint = bundle.get("mint") if isinstance(bundle.get("mint"), str) else ""
    checks = {
        "quote_sol": {"status": "PASS" if quote_is_sol(pair.get("quoteToken")) else "FAIL", "reason": None if quote_is_sol(pair.get("quoteToken")) else "quote_not_sol"},
        "mint_freeze_revoked": authority_check(rc, st, pool_id),
        "lp_100": lp_check(rc, st, pool_id),
        "liq_ge_25pct": liquidity_check(dex, rc, st),
        "dev_le_3pct": dev_check(rc, st),
        "no_past_rug": rug_check(rc, st, bundle.get("das"), bundle.get("deployer"), mint),
        "holder_growth": holder_check(bundle.get("chart"), st, rc, mint),
    }
    failing = [item["status"] for item in checks.values() if item["status"] != "PASS"]
    if not failing:
        overall = "PASS"
    elif "FAIL" in failing:
        overall = "FAIL"
    else:
        overall = "FAIL-CLOSED"
    return {"ok": overall == "PASS", "overall": overall, "checks": checks, "mint": mint, "pool": pool_id}
