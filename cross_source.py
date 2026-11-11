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

import math
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
# "danger" and the text refers to the creator, the dev, or the account that
# deployed or owns the mint. The stem "rug" also matches "drug" and "shrug";
# those false positives fail closed on purpose.
RUG_STEMS = ("rug",)
CREATOR_RISK_WORDS = ("deployer", "owner", "creator", "dev", "developer", "authority")


def quote_is_sol(quote) -> bool:
    """True only when quoteToken.address is the native wSOL mint. The symbol is ignored."""
    if not isinstance(quote, dict):
        return False
    return quote.get("address") == SOL_MINT


_FETCHER = None


class FetchFailed(Exception):
    """A provider failed closed. The reason is a code, never a secret or a URL."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def set_fetcher(fetcher) -> None:
    """Install the desk's RugCheck and Solana Tracker reader. A caller body is not this."""
    global _FETCHER
    _FETCHER = fetcher if callable(fetcher) else None


def fetcher_installed() -> bool:
    return callable(_FETCHER)


_LAST_FAILURE: str | None = None


def note_failure(reason: str) -> None:
    global _LAST_FAILURE
    _LAST_FAILURE = reason


def take_failure() -> str | None:
    global _LAST_FAILURE
    reason = _LAST_FAILURE
    _LAST_FAILURE = None
    return reason


def clear_fetcher() -> None:
    global _FETCHER
    _FETCHER = None
    global _TEST_CLOCK
    _TEST_CAPS.clear()
    _TEST_BUNDLES.clear()
    _TEST_DEX.clear()
    _TEST_CLOCK = 1_800_000_000


_TEST_CAPS: dict[str, str] = {}
_TEST_BUNDLES: dict[str, dict] = {}
_TEST_DEX: dict[str, dict] = {}
_TEST_CLOCK = 1_800_000_000


def pair_id_for(mint: str) -> str:
    return "P" + mint[:20]


def note_cap(mint: str, cap) -> None:
    _TEST_CAPS[mint] = str(cap)


def note_bundle(mint: str, bundle: dict) -> None:
    _TEST_BUNDLES[mint] = bundle


def note_dex(mint: str, **fields) -> None:
    """Override the fixture pair's price, change, or creation time. Not a caller body."""
    _TEST_DEX.setdefault(mint, {}).update(fields)


def note_clock(moment) -> None:
    """The fixture pair is one hour old at this desk clock."""
    global _TEST_CLOCK
    _TEST_CLOCK = moment


def arm_test_fetcher() -> None:
    """A fixture reader for tests. Desk startup installs the production reader only when this is unset."""

    def fetch(kind, mint, pair_id=None):
        if mint in _TEST_BUNDLES:
            bundle = _TEST_BUNDLES[mint]
            mapped = {
                "dex": "dex",
                "rugcheck": "rugcheck",
                "solana_tracker_token": "tracker",
                "holders_chart": "chart",
                "das": "das",
                "deployer": "deployer",
            }
            return bundle.get(mapped.get(kind, kind))
        cap = _TEST_CAPS.get(mint, "1000000")
        pid = pair_id if isinstance(pair_id, str) and pair_id else pair_id_for(mint or "")
        if kind == "dex":
            extra = _TEST_DEX.get(mint, {})
            row = {
                "pairAddress": pid,
                "baseToken": {"address": mint},
                "quoteToken": {"address": SOL_MINT},
                "priceUsd": extra.get("priceUsd", "1"),
                "priceChange": {"h1": extra.get("h1", 10)},
                "pairCreatedAt": extra.get("pairCreatedAt", int((_TEST_CLOCK - 3600) * 1000)),
                "txns": {"h1": {"buys": extra.get("buys", 40), "sells": extra.get("sells", 20)}},
                "volume": {"h1": extra.get("volume_h1", 1000)},
            }
            market_cap = extra["marketCap"] if "marketCap" in extra else cap
            if market_cap is not None:
                row["marketCap"] = market_cap
            if extra.get("fdv") is not None:
                row["fdv"] = extra["fdv"]
            liquidity_usd = extra["liquidityUsd"] if "liquidityUsd" in extra else "1"
            row["liquidity"] = {} if liquidity_usd is None else {"usd": liquidity_usd}
            return {"pairs": [row]}
        if kind == "rugcheck":
            return {
                "creator": "SameCreator", "creatorBalance": 0, "token": {"supply": 1000},
                "mintAuthority": None, "freezeAuthority": None, "risks": [],
                "creatorTokens": [{"mint": mint}],
                "markets": [{"pubkey": pid, "lp": {"quoteUSD": cap, "lpLockedPct": 100}}],
            }
        if kind == "solana_tracker_token":
            return {
                "holders": 10,
                "token": {"creation": {"creator": "SameCreator"}},
                "risk": {
                    "dev": {"percentage": 0}, "risks": [],
                    "bundlers": {"count": 0, "totalBalance": 0, "totalPercentage": 0, "wallets": []},
                },
                "pools": [{
                    "poolId": pid, "lpBurn": 100,
                    "security": {"mintAuthority": None, "freezeAuthority": None},
                    "liquidity": {"quote": "1", "usd": 1},
                    "marketCap": {"quote": "1", "usd": cap},
                }],
            }
        if kind == "holders_chart":
            return {"holders": [{"holders": 2, "time": 1}, {"holders": 4, "time": 2}]}
        if kind == "das":
            return {"result": {"total": 1, "items": [{"id": mint}]}}
        if kind == "deployer":
            return {"total": 1, "data": [{"mint": mint}]}
        return None

    set_fetcher(fetch)


def _fetch(kind: str, mint: str, pair_id: str | None = None):
    if not callable(_FETCHER):
        return None
    try:
        return _FETCHER(kind, mint, pair_id)
    except FetchFailed as exc:
        note_failure(exc.reason)
        return None
    except Exception:
        note_failure("provider_unparsed")
        return None


BUNDLE_SOURCE = "solana_tracker_initial_bundler_share"


def _real_number(value):
    """Exactly an int or a float. Subclasses, bools, and strings are not numbers."""
    if type(value) is int:
        return Decimal(value)
    if type(value) is float and math.isfinite(value):
        return Decimal(repr(float(value)))
    return None


def _bundle_percentage(value):
    """A finite share from 0 through 100. Strings, NaN, negatives, and anything above 100 are missing."""
    number = _real_number(value)
    if number is None or number < 0 or number > 100:
        return None
    return number


def bundle_check(tracker_body, rugcheck_body) -> dict:
    """Initial bundler share from the desk's Solana Tracker token read.

    Thành approved this on 2026-11-11 as the substitute for creation-block share.
    risk.bundlers.totalInitialPercentage above 10 fails. A bundlers object with
    count 0 and no percentage is 0. Count and the percentage must be exactly an
    int or a float, not a subclass. A string or a lying subclass, plus a missing,
    NaN, negative, or over-100 figure, fails closed. RugCheck is not this measure.
    """
    del rugcheck_body
    refused = {"ok": False, "reason": "bundle_unverified", "bundled_launch_pct": None, "source": BUNDLE_SOURCE}
    body = unwrap(tracker_body)
    risk = body.get("risk") if isinstance(body, dict) else None
    bundlers = risk.get("bundlers") if isinstance(risk, dict) else None
    if not isinstance(bundlers, dict):
        return refused
    if "count" in bundlers and _real_number(bundlers.get("count")) is None:
        return refused
    count = _real_number(bundlers.get("count")) if "count" in bundlers else None
    if "totalInitialPercentage" not in bundlers:
        if count is not None and count == 0:
            return {"ok": True, "reason": None, "bundled_launch_pct": "0", "source": BUNDLE_SOURCE}
        return refused
    pct = _bundle_percentage(bundlers.get("totalInitialPercentage"))
    if pct is None:
        return refused
    shown = format(pct, "f")
    if pct > Decimal("10"):
        return {"ok": False, "reason": "bundle", "bundled_launch_pct": shown, "source": BUNDLE_SOURCE}
    return {"ok": True, "reason": None, "bundled_launch_pct": shown, "source": BUNDLE_SOURCE}


def fetch_dex(mint: str):
    """DexScreener pairs from the desk fetcher. The caller does not name the pool."""
    if not isinstance(mint, str) or mint == "":
        return None
    return _fetch("dex", mint, None)


def fetch_market_cap(mint: str, pair_id: str):
    """Market cap of this pool from the desk's Solana Tracker read. Not a request field."""
    body = _fetch("solana_tracker_token", mint, pair_id)
    pool = _matched_pool(body, pair_id)
    cap = pool.get("marketCap") if isinstance(pool, dict) and isinstance(pool.get("marketCap"), dict) else None
    if not isinstance(cap, dict):
        return None
    return _money(cap.get("usd"))


def fetch_bundle(mint: str, pair_id: str):
    """Bodies the filter fetched. pair_id is the one stored at scout time."""
    kinds = (
        ("dex", "dex"),
        ("rugcheck", "rugcheck"),
        ("tracker", "solana_tracker_token"),
        ("chart", "holders_chart"),
        ("das", "das"),
        ("deployer", "deployer"),
    )
    bundle = {"mint": mint, "pair_id": pair_id if isinstance(pair_id, str) else ""}
    for key, kind in kinds:
        body = _fetch(kind, mint, pair_id)
        if body is None:
            return None
        bundle[key] = body
    return bundle


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


def select_pair(dex_body, mint: str, pair_id: str = ""):
    """The one native-wSOL pool for this mint.

    More than one such pool is pool_ambiguous. None is pool_missing. A supplied
    pair id must equal that pool's pairAddress exactly; base58 is not case-folded.
    """
    if not isinstance(mint, str) or mint == "":
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
        seen.add(address)
        matched.append(pair)
    if not matched:
        return None, "pool_missing"
    if len(matched) > 1:
        return None, "pool_ambiguous"
    only = matched[0]
    if isinstance(pair_id, str) and pair_id != "" and only.get("pairAddress") != pair_id:
        return None, "pool_missing"
    return only, None


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
    """True for a rug stem, or a danger-level risk naming the creator.

    CREATOR_RISK_WORDS includes deployer, owner, creator, dev, developer, and
    authority. A stem match also catches drug and shrug. Those false positives
    fail closed.
    """
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
    if rc.get("mintAuthority") is not None or security.get("mintAuthority") is not None:
        return {"status": "FAIL", "reason": "mint_authority"}
    if rc.get("freezeAuthority") is not None or security.get("freezeAuthority") is not None:
        return {"status": "FAIL", "reason": "freeze_authority"}
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


def _base_pair_id(dex_body, mint: str) -> str:
    """The mint's pair id for LP and authority reads. Empty when it is not unique."""
    found = []
    for pair in _iter_pairs(dex_body):
        base = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
        address = pair.get("pairAddress")
        if base.get("address") == mint and isinstance(address, str) and address not in found:
            found.append(address)
    if len(found) == 1:
        return found[0]
    return ""


def _quote_status(dex_body, mint: str, pair_id: str) -> dict:
    bases = []
    for pair in _iter_pairs(dex_body):
        base = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
        if base.get("address") == mint:
            bases.append(pair)
    if not bases:
        return {"status": "FAIL-CLOSED", "reason": "quote_unknown"}
    chosen = [pair for pair in bases if pair_id == "" or pair.get("pairAddress") == pair_id]
    if pair_id != "" and not chosen:
        return {"status": "FAIL-CLOSED", "reason": "pool_missing"}
    quote = (chosen or bases)[0].get("quoteToken")
    if not isinstance(quote, dict) or not isinstance(quote.get("address"), str) or quote.get("address") == "":
        return {"status": "FAIL-CLOSED", "reason": "quote_unknown"}
    if quote.get("address") != SOL_MINT:
        return {"status": "FAIL", "reason": "quote_not_sol"}
    return {"status": "PASS", "reason": None}


def _bundle_row(tracker_body) -> dict:
    checked = bundle_check(tracker_body, None)
    if checked.get("ok") is True:
        return {
            "status": "PASS", "reason": None,
            "bundled_launch_pct": checked.get("bundled_launch_pct"), "source": checked.get("source"),
        }
    return {
        "status": "FAIL" if checked.get("reason") == "bundle" else "FAIL-CLOSED",
        "reason": checked.get("reason") or "bundle_unverified",
        "bundled_launch_pct": checked.get("bundled_launch_pct"),
        "source": checked.get("source"),
    }


def judge(bundle: dict) -> dict:
    """One token. bundle carries the saved bodies. Every check is reported."""
    dex = bundle.get("dex")
    rc = bundle.get("rugcheck")
    st = bundle.get("tracker")
    mint = bundle.get("mint") if isinstance(bundle.get("mint"), str) else ""
    pair_id = bundle.get("pair_id") if isinstance(bundle.get("pair_id"), str) else ""
    selected, why = select_pair(dex, mint, pair_id)
    pool_id = selected["pairAddress"] if selected is not None else _base_pair_id(dex, mint)
    checks = {
        "quote_sol": _quote_status(dex, mint, pair_id),
        "mint_freeze_revoked": authority_check(rc, st, pool_id),
        "lp_100": lp_check(rc, st, pool_id),
        "liq_ge_25pct": liquidity_check(dex, rc, st, mint, pair_id) if selected is not None else {"status": "FAIL-CLOSED", "reason": why or "pool_missing"},
        "dev_le_3pct": dev_check(rc, st),
        "no_past_rug": rug_check(rc, st, bundle.get("das"), bundle.get("deployer"), mint),
        "holder_growth": holder_check(bundle.get("chart"), st, rc, mint),
        "bundle_le_10": _bundle_row(st),
    }
    failing = [item["status"] for item in checks.values() if item["status"] != "PASS"]
    if not failing:
        overall = "PASS"
    elif "FAIL" in failing:
        overall = "FAIL"
    else:
        overall = "FAIL-CLOSED"
    return {"ok": overall == "PASS", "overall": overall, "checks": checks, "mint": mint, "pool": pool_id}
