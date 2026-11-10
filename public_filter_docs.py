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
            "viable": False,
            "implemented": True,
            "source_wired": False,
            "endpoint": None,
            "documented_field": None,
            "api_key": None,
            "reason": "no cited schema names a past-rug count, so the reader stays unconfigured",
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
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return refused
    if value != 100:
        return refused | {"lpLockedPct": value}
    return {"ok": True, "reason": None, "lpLockedPct": 100, "expiry_checked": False, "source": "rugcheck_summary"}


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_holder_chart(mint: str, chart=None, allow_network: bool = False) -> dict:
    """A supplied body is the test path. The key is required before any GET."""
    unknown = {
        "ok": False,
        "reason": "holder_growth_unknown",
        "score_measured": False,
        "live_ready": False,
        "fetched": False,
        "source": SOURCE,
    }
    if chart is not None:
        parsed = parse_holder_chart(chart, mint)
        if parsed.get("ok") is not True:
            return unknown | {"parse_reason": parsed.get("reason")}
        return parsed | {"fetched": False}
    key = os.environ.get(KEY_ENV)
    if not isinstance(key, str) or key.strip() == "":
        return unknown | {"reason": "holder_growth_unknown", "key_present": False}
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
