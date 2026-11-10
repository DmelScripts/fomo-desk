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
# Larger / smaller above this is not the same pool figure.
LIQUIDITY_SOURCE_TOLERANCE = Decimal("3")
MCAP_SOURCE_TOLERANCE = Decimal("3")
CREATOR_RUG_RISK = "Creator history of rugged tokens"
DAS_PAGE_LIMIT = 20
# A creator or dev risk fails when its name or description contains the stem
# "rug" (case-insensitive: "rugged", "Rug pull history") or its level is
# "danger" and the text refers to the creator or the dev.
RUG_STEMS = ("rug",)
CREATOR_RISK_WORDS = ("creator", "dev")


def quote_is_sol(quote) -> bool:
    """True only when quoteToken.address is the native wSOL mint. The symbol is ignored."""
    if not isinstance(quote, dict):
        return False
    return quote.get("address") == SOL_MINT


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


def _iter_pairs(dex_body):
    body = unwrap(dex_body)
    rows = []
    if isinstance(body, dict):
        if isinstance(body.get("pairs"), list):
            rows.extend(body["pairs"])
        extra = body.get("pair")
        if isinstance(extra, dict):
            rows.append(extra)
    elif isinstance(body, list):
        rows.extend(body)
    for pair in rows:
        if isinstance(pair, dict):
            yield pair


def select_pair(dex_body, mint: str, pair_id: str):
    """The one pool for this mint, quoted in native wSOL, at this exact pair id.

    Base58 ids are compared with ``==``. A different case is a different id.
    """
    if not isinstance(mint, str) or mint == "" or not isinstance(pair_id, str):
        return None, "pool_missing"
    matched = []
    seen = set()
    for pair in _iter_pairs(dex_body):
        base = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
        address = pair.get("pairAddress")
        if base.get("address") != mint or not isinstance(address, str) or address in seen:
            continue
        if not quote_is_sol(pair.get("quoteToken")):
            continue
        if pair_id != "" and address != pair_id:
            continue
        seen.add(address)
        matched.append(pair)
    if not matched:
        return None, "pool_missing"
    if len(matched) > 1:
        return None, "pool_ambiguous"
    return matched[0], None


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


def liquidity_check(dex_body, rugcheck_body, tracker_body, mint: str = "", pair_id: str = "") -> dict:
    """Quote-side liquidity over the larger confirmed market cap.

    DexScreener liquidity.usd is not read. The denominator is the larger of the
    DexScreener marketCap and the Solana Tracker pool marketCap.usd, and only
    after those two caps agree within MCAP_SOURCE_TOLERANCE.
    """
    pair, why = select_pair(dex_body, mint, pair_id)
    if pair is None:
        return {"status": "FAIL-CLOSED", "reason": why or "pool_missing"}
    pool_id = pair["pairAddress"]
    market = _matched_market(rugcheck_body, pool_id)
    pool = _matched_pool(tracker_body, pool_id)
    lp = market.get("lp") if isinstance(market, dict) and isinstance(market.get("lp"), dict) else None
    rc_quote = _money(lp.get("quoteUSD")) if lp else None
    st_quote = _quote_side_usd(pool)
    st_cap = _money((pool.get("marketCap") or {}).get("usd")) if isinstance(pool, dict) and isinstance(pool.get("marketCap"), dict) else None
    dex_cap = _money(pair.get("marketCap"))
    if rc_quote is None or st_quote is None or st_cap is None or dex_cap is None:
        return {"status": "FAIL-CLOSED", "reason": "liquidity_source_missing"}
    if not within(rc_quote, st_quote) or not within(dex_cap, st_cap, MCAP_SOURCE_TOLERANCE):
        return {
            "status": "FAIL-CLOSED",
            "reason": "liquidity_source_conflict",
            "quote_usd": format(min(rc_quote, st_quote), "f"),
            "market_cap_usd": format(max(dex_cap, st_cap), "f"),
        }
    used = min(rc_quote, st_quote)
    denominator = max(dex_cap, st_cap)
    ratio = used / denominator if denominator > 0 else None
    if ratio is None or ratio < memecoin_desk.LIQ_OF_MCAP:
        return {
            "status": "FAIL",
            "reason": "liquidity_below_25pct_mcap",
            "quote_usd": format(used, "f"),
            "market_cap_usd": format(denominator, "f"),
        }
    return {
        "status": "PASS",
        "reason": None,
        "quote_usd": format(used, "f"),
        "market_cap_usd": format(denominator, "f"),
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


def _risk_items(body) -> list:
    if not isinstance(body, dict):
        return []
    rows = []
    for item in body.get("risks") or []:
        rows.append(item)
    risk = body.get("risk") if isinstance(body.get("risk"), dict) else {}
    for item in risk.get("risks") or []:
        rows.append(item)
    return rows


def creator_risk(item) -> bool:
    """True for a rug stem, or a danger-level creator/dev risk. See RUG_STEMS."""
    if isinstance(item, str):
        text = item.lower()
        return any(stem in text for stem in RUG_STEMS)
    if not isinstance(item, dict):
        return False
    text = " ".join(str(item.get(key) or "") for key in ("name", "description")).lower()
    if any(stem in text for stem in RUG_STEMS):
        return True
    level = str(item.get("level") or "").lower()
    if level == "danger" and any(word in text for word in CREATOR_RISK_WORDS):
        return True
    return False


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
    flagged = any(creator_risk(item) for item in _risk_items(unwrap(rugcheck_body))) or any(
        creator_risk(item) for item in _risk_items(unwrap(tracker_body))
    )
    gap = enumeration_gap(das_body, deployer_body, rugcheck_body, mint)
    if flagged:
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


def _quote_status(dex_body, mint: str, pair_id: str) -> dict:
    base_matches = []
    for pair in _iter_pairs(dex_body):
        base = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
        if base.get("address") == mint and (pair_id == "" or pair.get("pairAddress") == pair_id):
            base_matches.append(pair)
    if not base_matches:
        return {"status": "FAIL-CLOSED", "reason": "quote_unknown"}
    quote = base_matches[0].get("quoteToken")
    if not isinstance(quote, dict) or not isinstance(quote.get("address"), str) or quote.get("address") == "":
        return {"status": "FAIL-CLOSED", "reason": "quote_unknown"}
    if quote.get("address") != SOL_MINT:
        return {"status": "FAIL", "reason": "quote_not_sol"}
    return {"status": "PASS", "reason": None}


def judge(bundle: dict) -> dict:
    """One token. bundle carries the saved bodies. Every check is reported."""
    dex = bundle.get("dex")
    rc = bundle.get("rugcheck")
    st = bundle.get("tracker")
    mint = bundle.get("mint") if isinstance(bundle.get("mint"), str) else ""
    pair_id = bundle.get("pair_id") if isinstance(bundle.get("pair_id"), str) else ""
    if pair_id == "":
        for pair in _iter_pairs(dex):
            base = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
            if base.get("address") == mint and isinstance(pair.get("pairAddress"), str):
                pair_id = pair["pairAddress"]
                break
    pool_id = pair_id
    checks = {
        "quote_sol": _quote_status(dex, mint, pair_id),
        "mint_freeze_revoked": authority_check(rc, st, pool_id),
        "lp_100": lp_check(rc, st, pool_id),
        "liq_ge_25pct": liquidity_check(dex, rc, st, mint, pair_id),
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
