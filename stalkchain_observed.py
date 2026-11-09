"""Map one observed MCP fixture. This is not the REST adapter.

The bodies are the 8 November 2026 MCP captures. They are not a verified
REST envelope, not fresh values, and not a mainnet pass. Creation time is
not freshness. A launch count is not a rug history. Ninety-one percent LP
burn is not a complete lock. Holder counts from different tools are not
growth. A win-rate sample is not an analyst score.
"""

from __future__ import annotations

from decimal import Decimal

import memecoin_desk

KIND = "OBSERVED_MCP_FIXTURE_ONLY"
SOURCES = ("onchain", "deployer", "early_buyers", "holder_quality", "snapshot", "stats")


def _data(body):
    if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
        return None
    return body["data"]


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    try:
        number = Decimal(str(value))
    except Exception:
        return None
    if not number.is_finite():
        return None
    return number


def _percent(value):
    number = _number(value)
    if number is None or number < 0 or number > 100:
        return None
    return number


def _text(value):
    return value if isinstance(value, str) and value != "" else None


def _count(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _mint_of(data, key):
    found = _text(None if data is None else data.get(key))
    return found


def _rule(value, limit, above_rejects: bool) -> str:
    if value is None:
        return "unknown"
    if above_rejects and value > limit:
        return "reject"
    if above_rejects and value <= limit:
        return "within"
    return "unknown"


def documented_measurement_gaps() -> dict:
    """What the 8 November capture and the tool contracts actually name.

    stalkchain_token_onchain supplies lpBurnedPercent and one holdersTotal.
    The capture's own note says that burn percent has no unlock time.
    stalkchain_token_deployer supplies tokensLaunched and a tokens array whose
    recorded keys are mint, symbol, liquidityUsd, marketCapUsd,
    lpBurnedPercent, and hasSocials. That array has no prior-rug flag.
    risk.rugged is one token's flag, not a history. DexScreener liquidity is
    not a lock expiry. Two holder samples are growth only when the source,
    mint, and metric are the same. This capture has holdersTotal and a
    different snapshot totalHolders, so it is not a growth series.
    """
    return {
        "lp_lock_expiry": {
            "available": False,
            "endpoint": "stalkchain_token_onchain",
            "documented_field": "$.data.lpBurnedPercent",
            "missing": "unlock time or lock-account expiry",
            "citation": "tests/fixtures/nov8_observed_mcp_field_mapping.json",
        },
        "rug_history": {
            "available": False,
            "endpoint": "stalkchain_token_deployer",
            "documented_fields": ["$.data.tokensLaunched", "$.data.tokens", "$.data.risk.rugged"],
            "missing": "a prior-rug flag or a complete lookback",
            "citation": "tests/fixtures/nov8_observed_mcp_field_mapping.json",
        },
        "holder_growth": {
            "available": False,
            "reason": "one holdersTotal and a different totalHolders are not two samples of one metric",
            "parser": "holder_growth_series",
            "citation": "tests/fixtures/nov8_observed_mcp_fixtures.json",
        },
    }


def holder_growth_series(points) -> dict:
    """Same source, mint, and metric at two increasing capture times. One row is not growth."""
    refused = {"ok": False, "reason": "t0_only", "score_measured": False, "live_ready": False, "cross_source_is_growth": False}
    if not isinstance(points, list) or len(points) < 2:
        return refused | {"points": 0 if not isinstance(points, list) else len(points)}
    first = points[0]
    last = points[-1]
    if not isinstance(first, dict) or not isinstance(last, dict):
        return refused | {"reason": "point_shape"}
    keys = ("source", "mint", "metric", "captured_at")
    if any(first.get(key) != last.get(key) for key in ("source", "mint", "metric")):
        return refused | {"reason": "series_mismatch", "cross_source_is_growth": True}
    if any(not isinstance(item.get(key), str) or item.get(key) == "" for item in (first, last) for key in keys):
        return refused | {"reason": "point_shape"}
    if not (first["captured_at"] < last["captured_at"]):
        return refused | {"reason": "time_not_increasing"}
    start = _count(first.get("value"))
    end = _count(last.get("value"))
    if start is None or end is None:
        return refused | {"reason": "count_type"}
    return {
        "ok": True,
        "reason": None,
        "points": len(points),
        "delta": end - start,
        "source": first["source"],
        "mint": first["mint"],
        "metric": first["metric"],
        "score_measured": False,
        "live_ready": False,
        "cross_source_is_growth": False,
        "is_analyst_component": False,
    }


def map_fixture(bundle, mint: str) -> dict:
    """Read the six observed bodies. A bad join drops the whole measurement."""
    refused = {
        "ok": False,
        "kind": KIND,
        "mint": mint,
        "live_ready": False,
        "score_measured": False,
        "fixture_is_mainnet_proof": False,
        "rest_envelope_verified": False,
        "freshness": "unknown",
        "creation_time_used_as_freshness": False,
        "analyst_score": None,
    }
    if not isinstance(bundle, dict) or bundle.get("kind") != KIND or not isinstance(bundle.get("responses"), dict):
        return refused | {"reason": "observed_shape"}
    if not isinstance(mint, str) or mint == "":
        return refused | {"reason": "mint"}
    responses = bundle["responses"]
    missing = [name for name in SOURCES if name not in responses]
    if missing:
        return refused | {"reason": "source_missing", "missing": missing}
    parsed = {name: _data(responses.get(name)) for name in SOURCES}
    if any(item is None for item in parsed.values()):
        return refused | {"reason": "source_data"}
    joins = {
        "onchain": _mint_of(parsed["onchain"], "mint"),
        "early_buyers": _mint_of(parsed["early_buyers"], "token"),
        "stats": _mint_of(parsed["stats"], "token"),
        "snapshot": _mint_of(parsed["snapshot"], "token"),
        "holder_quality": _mint_of(parsed["holder_quality"], "token"),
    }
    if any(value != mint for value in joins.values()):
        return refused | {"reason": "mint_mismatch", "joins": joins}
    launched = parsed["deployer"].get("tokens")
    if not isinstance(launched, list) or mint not in {row.get("mint") for row in launched if isinstance(row, dict)}:
        return refused | {"reason": "deployer_mint_unjoined"}
    onchain_dev = _text(parsed["onchain"].get("deployer"))
    history_dev = _text(parsed["deployer"].get("deployer"))
    if onchain_dev is None or onchain_dev != history_dev:
        return refused | {"reason": "deployer_unjoined"}
    captured = _text(parsed["snapshot"].get("capturedAt"))
    created = _text(parsed["onchain"].get("createdAt"))
    launch_at = _text((parsed["early_buyers"].get("launch") or {}).get("at") if isinstance(parsed["early_buyers"].get("launch"), dict) else None)
    if captured is None:
        return refused | {"reason": "snapshot_timestamp_missing"}

    authorities = {}
    for name in ("mintAuthorityActive", "freezeAuthorityActive"):
        flag = parsed["onchain"].get(name)
        if not isinstance(flag, bool):
            authorities[name] = {"value": None, "reason": "type"}
        else:
            authorities[name] = {"value": flag, "revoked": flag is False, "independent_account_check": False}

    burned = _percent(parsed["onchain"].get("lpBurnedPercent"))
    lp = {
        "lp_burned_percent": None if burned is None else format(burned, "f"),
        "lp_burned_or_locked": False,
        "lock_expiry": None,
        "reason": "lp_burn_incomplete" if burned is not None else "lp_burn_missing",
        "admitted": False,
    }

    holding = _percent((parsed["onchain"].get("concentration") or {}).get("devPercent") if isinstance(parsed["onchain"].get("concentration"), dict) else None)
    dev_holding = {
        "value": None if holding is None else format(holding, "f"),
        "coded_max": format(memecoin_desk.DEV_HOLDING_MAX, "f"),
        "numeric_rule_result": _rule(holding, memecoin_desk.DEV_HOLDING_MAX, True),
        "admitted": False,
        "reason": "freshness_unknown",
    }

    launch = parsed["early_buyers"].get("launch") if isinstance(parsed["early_buyers"].get("launch"), dict) else {}
    bundlers = parsed["early_buyers"].get("bundlers") if isinstance(parsed["early_buyers"].get("bundlers"), dict) else {}
    buys = launch.get("buys") if isinstance(launch.get("buys"), list) else None
    dev_buy = _percent(launch.get("devBuyPercent"))
    nondev_buy = _percent(bundlers.get("heldAtLaunchPercent"))
    from_buys_dev = Decimal("0")
    from_buys_rest = Decimal("0")
    buys_ok = isinstance(buys, list) and buys != []
    if buys_ok:
        for buy in buys:
            if not isinstance(buy, dict) or buy.get("block") != 0 or not isinstance(buy.get("deployer"), bool):
                buys_ok = False
                break
            part = _percent(buy.get("supplyPercent"))
            if part is None:
                buys_ok = False
                break
            if buy["deployer"] is True:
                from_buys_dev += part
            else:
                from_buys_rest += part
    joined = buys_ok and dev_buy is not None and nondev_buy is not None and from_buys_dev == dev_buy and from_buys_rest == nondev_buy
    inclusive = None if not joined else dev_buy + nondev_buy
    bundle = {
        "dev_launch_percent": None if dev_buy is None else format(dev_buy, "f"),
        "nondev_launch_percent": None if nondev_buy is None else format(nondev_buy, "f"),
        "inclusive_percent": None if inclusive is None else format(inclusive, "f"),
        "coded_max": format(memecoin_desk.BUNDLE_MAX, "f"),
        "numeric_rule_result": _rule(inclusive, memecoin_desk.BUNDLE_MAX, True),
        "admitted": False,
        "reason": None if joined else "bundle_unjoined",
        "double_counted": False,
    }

    launched_count = _count(parsed["deployer"].get("tokensLaunched"))
    rugs = {
        "tokens_launched": launched_count,
        "status": "coverage_unknown",
        "clean": False,
        "provider_rugged_flag": parsed["onchain"].get("risk", {}).get("rugged") if isinstance(parsed["onchain"].get("risk"), dict) else None,
        "reason": "launch_count_is_not_rug_history",
        "admitted": False,
    }

    raw_change = (parsed["onchain"].get("priceChangePercent") or {}).get("1h") if isinstance(parsed["onchain"].get("priceChangePercent"), dict) else None
    change = _number(raw_change)
    hour = {
        "value": None if change is None else format(change, "f"),
        "coded_reject_at": format(memecoin_desk.ALREADY_UP_1H, "f"),
        "numeric_rule_result": "reject" if change is not None and change >= memecoin_desk.ALREADY_UP_1H else ("within" if change is not None else "unknown"),
        "admitted": False,
        "reason": "freshness_unknown",
    }

    windows = parsed["stats"].get("windows") if isinstance(parsed["stats"].get("windows"), dict) else {}
    hour_window = windows.get("1h") if isinstance(windows.get("1h"), dict) else {}
    buyers = _count(hour_window.get("uniqueBuyers"))
    sellers = _count(hour_window.get("uniqueSellers"))
    uniques = {
        "unique_buyers_1h": buyers,
        "unique_sellers_1h": sellers,
        "source": "stats",
        "freshness": "unknown",
        "is_analyst_component": False,
        "admitted": False,
    }

    snapshot_holders = _count(parsed["snapshot"].get("totalHolders"))
    stats_holders = _count(parsed["stats"].get("holders"))
    onchain_holders = _count(parsed["onchain"].get("holdersTotal"))
    growth = {
        "ok": False,
        "reason": "t0_only",
        "snapshot_total_holders": snapshot_holders,
        "stats_holders": stats_holders,
        "onchain_holders_total": onchain_holders,
        "cross_source_is_growth": False,
        "points": 1 if snapshot_holders is not None else 0,
        "score_measured": False,
    }

    quality = parsed["holder_quality"]
    sample = {
        "tracked": _count(quality.get("trackedHolders")),
        "scored": _count(quality.get("scored")),
        "average_win_rate_pct": _percent(quality.get("averageWinRatePct")),
        "is_smart_wallet_proof": False,
        "is_analyst_score": False,
    }
    basis = _text(parsed["onchain"].get("marketCapBasis"))
    return {
        "ok": True,
        "reason": None,
        "kind": KIND,
        "mint": mint,
        "live_ready": False,
        "score_measured": False,
        "fixture_is_mainnet_proof": False,
        "rest_envelope_verified": False,
        "freshness": "unknown",
        "creation_time_used_as_freshness": False,
        "observed_at": captured,
        "token_created_at": created,
        "launch_at": launch_at,
        "deployer": onchain_dev,
        "authorities": authorities,
        "lp": lp,
        "dev_holding": dev_holding,
        "bundle": bundle,
        "rugs": rugs,
        "price_change_1h": hour,
        "uniques": uniques,
        "holder_growth": growth,
        "holder_quality": {
            "tracked": sample["tracked"],
            "scored": sample["scored"],
            "average_win_rate_pct": None if sample["average_win_rate_pct"] is None else format(sample["average_win_rate_pct"], "f"),
            "is_smart_wallet_proof": False,
            "is_analyst_score": False,
        },
        "market_cap_basis": basis,
        "fdv_used_as_market_cap": False,
        "analyst_score": None,
        "analyst_formula": memecoin_desk.analyst_decision(),
        "admits_filter": False,
        "admits_analyst": False,
    }
