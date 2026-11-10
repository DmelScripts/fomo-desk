"""Past-rug check behind one reader.

Helius ``getAssetsByCreator`` lists assets for a creator address. The mint is
``result.items[].id``. A verified ``creators[].address`` is the creator. If
there is not exactly one verified creator, one ``authorities[].address`` is
the update authority. ``getAssetsByCreator`` matches ``creatorAddress``, so an
update authority that is not a creator returns a different set, and DAS is not
a full chain history.

RugCheck's swagger does not name a per-token rugged flag. ``GET
/v1/tokens/{id}/report`` has no response schema. ``dto.TokenCheckSummary`` has
no rugged field. ``services.AnalyticsStats.rugPullCount`` is a platform total.
``GET /v1/stats/rugs/ticker`` is ``additionalProperties: true``. A past token
therefore cannot be marked rugged. The reader fails closed with
``dev_rug_history_unverified`` instead of inventing a field. A missing key, a
fetch or parse error, or a truncated list does the same. No past token, with
the current mint present in a complete page, is zero rugs.

The Helius key is ``HELIUS_API_KEY``. It is not written into results.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request

KEY_ENV = "HELIUS_API_KEY"
MAX_TOKENS = 20
TIMEOUT_S = 10
CACHE_TTL_S = 300
SOURCE = "helius_getAssetsByCreator"
# Cited absence: https://api.rugcheck.xyz/swagger/doc.json
RUGGED_FIELD = None
RUGCHECK_SUMMARY = "https://api.rugcheck.xyz/v1/tokens/{id}/report/summary"

_READER = None
_CACHE: dict[str, tuple[float, tuple[str, ...]]] = {}
_RECORDED: dict[str, dict] = {}


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def set_source(reader) -> None:
    """Install one reader. None restores the Helius reader."""
    global _READER
    _READER = reader if callable(reader) else None


def reset_source() -> None:
    global _READER
    _READER = None


def clear_cache() -> None:
    _CACHE.clear()


def remember(mint: str, asset, assets, reports=None) -> None:
    """A recorded Helius page and RugCheck bodies. This is not a caller verdict."""
    _RECORDED[mint] = {"asset": asset, "assets": assets, "reports": reports or {}}


def clear_recorded() -> None:
    _RECORDED.clear()


def assess(reading, mint=None) -> dict:
    """Ignore a caller verdict. Only this reader, with source_wired, may pass."""
    verdict = _ask(mint)
    if verdict.get("source_wired") is not True or verdict.get("source") != SOURCE or verdict.get("verified") is not True:
        return {"ok": False, "verified": False, "reason": "dev_rug_history_unverified", "rugs": None, "source": verdict.get("source"), "source_wired": verdict.get("source_wired") is True}
    rugs = verdict.get("rugs")
    if isinstance(rugs, bool) or not isinstance(rugs, int) or rugs < 0:
        return {"ok": False, "verified": False, "reason": "dev_rug_history_unverified", "rugs": None}
    if rugs > 0:
        return {"ok": False, "verified": True, "reason": "dev_rug_history", "rugs": rugs, "source": SOURCE, "source_wired": True}
    return {"ok": True, "verified": True, "reason": None, "rugs": 0, "source": SOURCE, "source_wired": True}


def read_history(mint, creator=None, asset=None, assets=None, reports=None, now=None, allow_network: bool = False) -> dict:
    """List one creator's assets, then stop because no rugged field is documented."""
    moment = time.time() if now is None else now
    if not isinstance(moment, (int, float)) or isinstance(moment, bool):
        return _unverified("clock")
    if not isinstance(mint, str) or mint == "" or "/" in mint:
        return _unverified("mint")
    recorded = _RECORDED.get(mint) if isinstance(mint, str) else None
    if isinstance(recorded, dict):
        if asset is None:
            asset = recorded.get("asset")
        if assets is None:
            assets = recorded.get("assets")
        if reports is None:
            reports = recorded.get("reports")
        allow_network = False
    if reports is None:
        reports = {}
    if creator is not None and (not isinstance(creator, str) or creator == "" or "/" in creator):
        return _unverified("creator")
    if creator is None:
        if asset is None:
            fetched = _helius({"jsonrpc": "2.0", "id": "1", "method": "getAsset", "params": {"id": mint}}, allow_network)
            if fetched.get("ok") is not True:
                return fetched
            asset = fetched["body"]
        creator, how = _creator_of(asset)
        if creator is None:
            return _unverified(how or "creator")
    else:
        how = "caller"
    cached = _CACHE.get(creator)
    if assets is None and cached is not None and cached[0] >= moment:
        listed = cached[1]
        items = [{"id": item} for item in listed]
        from_cache = True
    else:
        if assets is None:
            fetched = _helius({
                "jsonrpc": "2.0", "id": "1", "method": "getAssetsByCreator",
                "params": {"creatorAddress": creator, "page": 1, "limit": MAX_TOKENS},
            }, allow_network)
            if fetched.get("ok") is not True:
                return fetched
            assets = fetched["body"]
        parsed = _asset_ids(assets)
        if parsed.get("ok") is not True:
            return parsed
        listed = parsed["ids"]
        items = parsed["items"]
        _CACHE[creator] = (float(moment) + CACHE_TTL_S, listed)
        from_cache = False
    if mint not in listed:
        return _unverified("creator_not_in_index", creator=creator, creator_source=how)
    past = [item for item in items if isinstance(item, dict) and item.get("id") != mint]
    rugs = 0
    for item in past:
        flag = _rugged_flag(item, reports)
        if flag is None:
            return _unverified(
                "undocumented_field", creator=creator, creator_source=how,
                past_tokens=len(past), cached=from_cache,
            )
        if flag is True:
            rugs += 1
    if rugs > 0:
        return {
            "ok": False, "verified": True, "reason": "dev_rug_history", "rugs": rugs,
            "source": SOURCE, "source_wired": True, "creator": creator, "creator_source": how,
            "past_tokens": len(past), "cached": from_cache, "chain_complete": False,
        }
    return {
        "ok": True, "verified": True, "reason": None, "rugs": 0, "source": SOURCE,
        "source_wired": True, "creator": creator, "creator_source": how, "past_tokens": len(past),
        "cached": from_cache, "rugged_field": "rugged", "chain_complete": False,
        "limit": "getAssetsByCreator matches creatorAddress, not every mint that address can update",
    }


def _creator_of(asset) -> tuple[str | None, str | None]:
    body = _result(asset)
    if not isinstance(body, dict):
        return None, "creator_parse"
    creators = body.get("creators")
    verified = []
    if isinstance(creators, list):
        for item in creators:
            if not isinstance(item, dict) or item.get("verified") is not True:
                continue
            address = item.get("address")
            if isinstance(address, str) and address != "":
                verified.append(address)
    if len(verified) > 1:
        return None, "several_creators"
    if len(verified) == 1:
        return verified[0], "creator"
    authorities = body.get("authorities")
    found = []
    if isinstance(authorities, list):
        for item in authorities:
            if not isinstance(item, dict):
                continue
            address = item.get("address")
            if isinstance(address, str) and address != "":
                found.append(address)
    if len(found) == 1:
        return found[0], "update_authority"
    return None, "creator_missing"


def _asset_ids(body) -> dict:
    result = _result(body)
    if not isinstance(result, dict):
        return _unverified("parse")
    total = result.get("total")
    items = result.get("items")
    page = result.get("page")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        return _unverified("parse")
    if isinstance(page, bool) or not isinstance(page, int) or page != 1:
        return _unverified("truncated")
    if not isinstance(items, list) or len(items) != total or total > MAX_TOKENS:
        return _unverified("truncated" if isinstance(items, list) else "parse")
    ids = []
    for item in items:
        if not isinstance(item, dict):
            return _unverified("parse")
        mint = item.get("id")
        if not isinstance(mint, str) or mint == "" or mint in ids:
            return _unverified("parse")
        ids.append(mint)
    return {"ok": True, "ids": tuple(ids), "items": items}


def _result(body):
    if not isinstance(body, dict):
        return None
    if "result" in body:
        return body.get("result")
    return body


def _rugged_flag(item: dict, reports: dict):
    """The RugCheck body nested on the past token. Only a boolean rugged is read."""
    nested = item.get("rugcheck") if isinstance(item.get("rugcheck"), dict) else None
    report = reports.get(item.get("id")) if isinstance(reports, dict) else None
    body = report if isinstance(report, dict) else nested
    if not isinstance(body, dict) or not isinstance(body.get("rugged"), bool):
        return None
    return body.get("rugged")


def _helius(payload: dict, allow_network: bool) -> dict:
    key = os.environ.get(KEY_ENV)
    if not isinstance(key, str) or key.strip() == "":
        return _unverified("missing_key", key_present=False)
    if allow_network is not True:
        return _unverified("network_not_requested", key_present=True)
    request = urllib.request.Request(
        "https://mainnet.helius-rpc.com/?api-key=" + key.strip(),
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json", "accept": "application/json"},
        method="POST",
    )
    try:
        raw = _post(request)
        body = json.loads(raw.decode("utf-8"))
    except Exception:
        return _unverified("fetch", key_present=True)
    if not isinstance(body, dict) or body.get("error") is not None:
        return _unverified("fetch", key_present=True)
    return {"ok": True, "body": body}


def _post(request):
    opener = urllib.request.build_opener(_RefuseRedirect)
    with opener.open(request, timeout=TIMEOUT_S) as response:
        if getattr(response, "status", 200) != 200:
            raise OSError("status")
        return response.read()


def _unverified(cause: str, **extra) -> dict:
    body = {
        "ok": False, "verified": False, "reason": "dev_rug_history_unverified",
        "rugs": None, "source": SOURCE, "cause": cause, "rugged_field": RUGGED_FIELD,
        "rugcheck_summary": RUGCHECK_SUMMARY, "fetched": cause == "fetch",
        "source_wired": True,
    }
    body.update(extra)
    return body


def _checked(reading) -> bool:
    if not isinstance(reading, dict) or reading.get("status") != "checked":
        return False
    source = reading.get("source")
    if not isinstance(source, str) or source == "" or source == "dexscreener":
        return False
    rugs = reading.get("rugs")
    seen = reading.get("tokens_seen")
    if isinstance(rugs, bool) or not isinstance(rugs, int) or rugs < 0:
        return False
    if isinstance(seen, bool) or not isinstance(seen, int) or seen < 0:
        return False
    return True


def _ask(mint) -> dict:
    """The wired reader only. A plugged-in callable cannot verify a token."""
    try:
        recorded = isinstance(mint, str) and mint in _RECORDED
        result = read_history(mint, allow_network=not recorded)
    except Exception:
        return {"verified": False, "reason": "dev_rug_history_unverified", "source": SOURCE, "source_wired": True}
    if not isinstance(result, dict):
        return {"verified": False, "reason": "dev_rug_history_unverified", "source": SOURCE, "source_wired": True}
    return result
