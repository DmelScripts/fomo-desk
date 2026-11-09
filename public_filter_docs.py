"""Parsers for public filter facts whose official docs name the fields.

Only the Solana Tracker holder chart is parsed. Its OpenAPI names
``holders[].holders`` and ``holders[].time`` and requires the ``x-api-key``
header. The RugCheck swagger names ``lpLockedPct`` on the summary, which is a
percentage, not an unlock date. Its locker schema names ``total.pct`` and
``total.totalUSDC`` and does not name an unlock date. It does not publish a
creator rug-count field. Those gaps stay closed.
"""

from __future__ import annotations

SOURCE = "solana_tracker_holders_chart"
METRIC = "holders"
DOCS = "https://docs.solanatracker.io/data-api/chart/get-holders-chart-data"


def source_catalog() -> dict:
    """What the fetched official pages actually name. Nothing here is a live read."""
    return {
        "lp_lock_expiry": {
            "viable": False,
            "implemented": False,
            "endpoint": "GET https://api.rugcheck.xyz/v1/tokens/{id}/report/summary",
            "documented_field": "lpLockedPct",
            "field_is_expiry": False,
            "api_key": "not required for the summary; refresh is paid",
            "reason": "official swagger types lpLockedPct as a number and does not name an unlock date",
            "citation": "https://api.rugcheck.xyz/swagger/doc.json dto.TokenCheckSummary",
        },
        "rug_history": {
            "viable": False,
            "implemented": False,
            "endpoint": None,
            "documented_field": None,
            "api_key": None,
            "reason": "the fetched RugCheck swagger has no creator rug-count field",
            "citation": "https://api.rugcheck.xyz/swagger/doc.json",
        },
        "birdeye_holder_chart": {
            "viable": False,
            "implemented": False,
            "endpoint": "GET https://public-api.birdeye.so/token/v1/holder/chart",
            "documented_field": None,
            "api_key": "X-API-KEY header, required",
            "reason": "the fetched page names the path and intervals but not the response field names",
            "citation": "https://birdeye.so/data-api/blog/detail/new-api-token-holder-chart-on-solana",
        },
        "helius_creator_assets": {
            "viable": False,
            "implemented": False,
            "endpoint": "POST https://mainnet.helius-rpc.com getAssetsByCreator",
            "documented_field": None,
            "api_key": "api-key query parameter, required",
            "reason": "the published response is an asset list and does not name a rug-history field",
            "citation": "https://www.helius.dev/docs/api-reference/das/getassetsbycreator",
        },
        "holder_growth": {
            "viable": True,
            "implemented": True,
            "endpoint": "GET https://data.solanatracker.io/holders/chart/{token}",
            "documented_fields": ["holders", "holders.holders", "holders.time"],
            "api_key": "x-api-key header, required",
            "freshness": "up to 1000 most recent points; type defaults to 1d; time_from and time_to are unix seconds",
            "score_measured": False,
            "citation": DOCS,
        },
    }


def parse_holder_chart(body, mint: str) -> dict:
    """Two increasing points of one holder count. A short or mixed body is not growth."""
    refused = {
        "ok": False,
        "reason": "holder_growth_unproven",
        "score_measured": False,
        "live_ready": False,
        "source": SOURCE,
        "api_key_required": True,
    }
    if not isinstance(mint, str) or mint == "":
        return refused | {"reason": "mint"}
    if not isinstance(body, dict) or not isinstance(body.get("holders"), list):
        return refused
    points = []
    for row in body["holders"]:
        if not isinstance(row, dict):
            return refused | {"reason": "point_shape"}
        count = row.get("holders")
        moment = row.get("time")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            return refused | {"reason": "count_type"}
        if isinstance(moment, bool) or not isinstance(moment, int):
            return refused | {"reason": "time_type"}
        points.append((moment, count))
    if len(points) < 2:
        return refused | {"points": len(points)}
    points.sort()
    if points[0][0] >= points[-1][0]:
        return refused | {"reason": "time_not_increasing"}
    return {
        "ok": True,
        "reason": None,
        "source": SOURCE,
        "mint": mint,
        "metric": METRIC,
        "points": len(points),
        "start_time": points[0][0],
        "end_time": points[-1][0],
        "start_holders": points[0][1],
        "end_holders": points[-1][1],
        "delta": points[-1][1] - points[0][1],
        "score_measured": False,
        "live_ready": False,
        "api_key_required": True,
        "fetched": False,
    }
