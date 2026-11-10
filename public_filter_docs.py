"""Parsers for public filter facts whose official docs name the fields.

RugCheck's summary schema names ``lpLockedPct`` as a number. Thành requires
that number to be 100, with no expiry check. The summary GET does not require
a key. The fetched swagger does not name an unlock date or a creator rug count.

The Solana Tracker holder chart names ``holders[].holders`` and
``holders[].time``. The header is ``x-api-key``, read from
``SOLANA_TRACKER_API_KEY``. A missing key or a bad body is unknown. Tests pass
a recorded body and do not open a socket.
"""

from __future__ import annotations

import json
import os
import urllib.request

SOURCE = "solana_tracker_holders_chart"
METRIC = "holders"
DOCS = "https://docs.solanatracker.io/data-api/chart/get-holders-chart-data"
RUGCHECK_SUMMARY = "https://api.rugcheck.xyz/v1/tokens/{id}/report/summary"
KEY_ENV = "SOLANA_TRACKER_API_KEY"
_RECORDED_CHART = None


def source_catalog() -> dict:
    """What the fetched official pages actually name. Nothing here is a live read."""
    return {
        "lp_lock": {
            "viable": True,
            "implemented": True,
            "endpoint": "GET " + RUGCHECK_SUMMARY,
            "documented_field": "lpLockedPct",
            "field_is_expiry": False,
            "pass_value": 100,
            "api_key": "not required for the summary; refresh is paid",
            "reason": "pass only when lpLockedPct is exactly 100; a missing value rejects",
            "citation": "https://api.rugcheck.xyz/swagger/doc.json dto.TokenCheckSummary",
        },
        "rug_history": {
            "viable": True,
            "implemented": True,
            "source_wired": True,
            "endpoint": "GET https://api.rugcheck.xyz/v1/tokens/{mint}/report",
            "documented_field": None,
            "rugged_field": "rugged",
            "rugged_field_status": "observed, undocumented",
            "api_key": "keyless report; Helius list uses HELIUS_API_KEY",
            "reason": "top-level rugged was observed on a live report at 2026-11-10 12:15 Asia/Saigon and is not in the swagger",
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
            "api_key": "x-api-key header from " + KEY_ENV,
            "freshness": "up to 1000 most recent points; type defaults to 1d; time_from and time_to are unix seconds",
            "score_measured": False,
            "citation": DOCS,
        },
    }


def parse_lp_locked_pct(body) -> dict:
    """Exactly 100 passes. A missing or non-numeric value rejects. No expiry is read."""
    refused = {"ok": False, "reason": "lp", "lpLockedPct": None, "expiry_checked": False}
    if not isinstance(body, dict) or "lpLockedPct" not in body:
        return refused
    value = body.get("lpLockedPct")
    if type(value) is not int and type(value) is not float:
        return refused
    if value != 100:
        return refused | {"lpLockedPct": value}
    return {"ok": True, "reason": None, "lpLockedPct": 100, "expiry_checked": False, "source": "rugcheck_summary"}


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def remember_chart(body) -> None:
    """The body read_holder_chart returns after the key check. Not a caller chart."""
    global _RECORDED_CHART
    _RECORDED_CHART = body


def clear_chart() -> None:
    global _RECORDED_CHART
    _RECORDED_CHART = None


def growth_points(parsed: dict):
    """Map the fetched holder delta to the 0-100 analyst component."""
    from decimal import Decimal
    start = parsed.get("start_holders")
    delta = parsed.get("delta")
    if type(start) is not int or start <= 0:
        return None
    if type(delta) is not int:
        return None
    points = (Decimal(delta) * Decimal(100)) / Decimal(start)
    if points < 0:
        points = Decimal(0)
    if points > 100:
        points = Decimal(100)
    return points


def read_holder_chart(mint: str, chart=None, allow_network: bool = False) -> dict:
    """Fetch the chart. A caller-supplied body is not a fetch."""
    unknown = {
        "ok": False,
        "reason": "holder_growth_unknown",
        "score_measured": False,
        "live_ready": False,
        "fetched": False,
        "source": SOURCE,
    }
    if chart is not None:
        return unknown | {"cause": "caller_chart"}
    key = os.environ.get(KEY_ENV)
    if not isinstance(key, str) or key.strip() == "":
        return unknown | {"key_present": False}
    if _RECORDED_CHART is not None:
        parsed = parse_holder_chart(_RECORDED_CHART, mint)
        if parsed.get("ok") is not True:
            return unknown | {"fetched": True, "parse_reason": parsed.get("reason")}
        return parsed | {"fetched": True, "key_present": True}
    if allow_network is not True:
        return unknown | {"key_present": True, "network": "not_requested"}
    if not isinstance(mint, str) or mint == "" or "/" in mint:
        return unknown
    url = "https://data.solanatracker.io/holders/chart/" + mint
    request = urllib.request.Request(url, headers={"x-api-key": key.strip(), "accept": "application/json"})
    opener = urllib.request.build_opener(_RefuseRedirect)
    try:
        with opener.open(request, timeout=10) as response:
            if getattr(response, "status", 200) != 200:
                return unknown | {"fetched": True}
            body = json.loads(response.read().decode("utf-8"))
    except Exception:
        return unknown | {"fetched": True}
    parsed = parse_holder_chart(body, mint)
    if parsed.get("ok") is not True:
        return unknown | {"fetched": True, "parse_reason": parsed.get("reason")}
    return parsed | {"fetched": True}


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
        if type(count) is not int or count < 0:
            return refused | {"reason": "count_type"}
        if type(moment) is not int:
            return refused | {"reason": "time_type"}
        points.append((moment, count))
    points.sort()
    nonzero = [item for item in points if item[1] > 0]
    if len(nonzero) < 2:
        return refused | {"points": len(points), "nonzero_points": len(nonzero)}
    if nonzero[0][0] >= nonzero[-1][0]:
        return refused | {"reason": "time_not_increasing", "nonzero_points": len(nonzero)}
    return {
        "ok": True,
        "reason": None,
        "source": SOURCE,
        "mint": mint,
        "metric": METRIC,
        "points": len(points),
        "nonzero_points": len(nonzero),
        "start_time": nonzero[0][0],
        "end_time": nonzero[-1][0],
        "start_holders": nonzero[0][1],
        "end_holders": nonzero[-1][1],
        "delta": nonzero[-1][1] - nonzero[0][1],
        "score_measured": False,
        "live_ready": False,
        "api_key_required": True,
        "fetched": False,
    }
