"""Past-rug check behind one reader.

Helius ``getAssetsByCreator`` lists assets for a creator address. The mint is
``result.items[].id``. A verified ``creators[].address`` is the creator. If
there is not exactly one verified creator, one ``authorities[].address`` is
the update authority. ``getAssetsByCreator`` matches ``creatorAddress``, so an
update authority that is not a creator returns a different set, and DAS is not
a full chain history.

RugCheck ``GET /v1/tokens/{mint}/report`` is keyless. A live report observed
on 2026-11-10 at 12:15 Asia/Saigon had a top-level boolean ``rugged`` and a
``creator`` string. That boolean is not in the swagger, so it is labeled
``observed, undocumented``. Only that fetched boolean is read. ``true``
rejects. Anything else, including a value nested on a Helius asset or passed
by the caller, fails closed. A clean pass uses a fresh Helius page. The cache
can only repeat a rejection.

The Helius key is ``HELIUS_API_KEY``. It is not written into results.
"""

from __future__ import annotations

import http.client
import json
import os
import time
import urllib.request

KEY_ENV = "HELIUS_API_KEY"
MAX_TOKENS = 20
TIMEOUT_S = 10
CACHE_TTL_S = 300
SOURCE = "helius_getAssetsByCreator"
# Observed on a live full report. Not in https://api.rugcheck.xyz/swagger/doc.json.
RUGGED_FIELD = "rugged"
RUGGED_FIELD_STATUS = "observed, undocumented"
RUGCHECK_REPORT = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"

_READER = None
_CACHE: dict[str, tuple[float, dict]] = {}
_RECORDED: dict[str, dict] = {}
_REPORTS: dict[str, dict] = {}


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


def remember(mint: str, asset, assets) -> None:
    """A recorded Helius page for this mint. It is not a rug verdict."""
    _RECORDED[mint] = {"asset": asset, "assets": assets}


def remember_report(mint: str, body, status: int = 200) -> None:
    """The body the RugCheck GET would return for this past mint."""
    _REPORTS[mint] = {"status": status, "body": body}


def clear_recorded() -> None:
    _RECORDED.clear()
    _REPORTS.clear()


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
    return {
        "ok": True, "verified": True, "reason": None, "rugs": 0, "source": SOURCE, "source_wired": True,
        "rugged_source": verdict.get("rugged_source"),
    }


def read_history(mint, creator=None, asset=None, assets=None, now=None, allow_network: bool = False) -> dict:
    """A clear result reads a Helius page supplied for this call, then RugCheck."""
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
        allow_network = False
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
    das_live = False
    if assets is None:
        cached = _CACHE.get(creator)
        if cached is not None and cached[0] >= moment:
            return dict(cached[1]) | {"cached": True}
        fetched = _helius({
            "jsonrpc": "2.0", "id": "1", "method": "getAssetsByCreator",
            "params": {"creatorAddress": creator, "page": 1, "limit": MAX_TOKENS},
        }, allow_network)
        if fetched.get("ok") is not True:
            return fetched
        assets = fetched["body"]
        das_live = fetched.get("network") is True
    parsed = _asset_ids(assets)
    if parsed.get("ok") is not True:
        if parsed.get("cause") == "truncated":
            _cache_rejection(creator, moment, parsed)
        return parsed
    if mint not in parsed["ids"]:
        return _unverified("creator_not_in_index", creator=creator, creator_source=how)
    past = [item.get("id") for item in parsed["items"] if isinstance(item, dict) and item.get("id") != mint]
    rugs = 0
    reports_live = True
    for past_mint in past:
        report = _rugcheck(past_mint, creator, allow_network)
        reports_live = reports_live and report.get("network") is True
        if report.get("ok") is not True:
            published = {key: value for key, value in report.items() if key != "network"}
            return published | {"creator": creator, "creator_source": how, "past_tokens": len(past), "cached": False}
        if report.get("rugged") is True:
            rugs += 1
    if rugs > 0:
        rejected = {
            "ok": False, "verified": True, "reason": "dev_rug_history", "rugs": rugs,
            "source": SOURCE, "source_wired": True, "creator": creator, "creator_source": how,
            "past_tokens": len(past), "cached": False, "chain_complete": False,
            "rugged_field": RUGGED_FIELD, "rugged_field_status": RUGGED_FIELD_STATUS,
        }
        _cache_rejection(creator, moment, rejected)
        if das_live and reports_live:
            import live_gate_proof
            live_gate_proof.note_history(past, False)
        return rejected
    if das_live and reports_live:
        import live_gate_proof
        live_gate_proof.note_history(past, True)
    return {
        "ok": True, "verified": True, "reason": None, "rugs": 0, "source": SOURCE,
        "source_wired": True, "creator": creator, "creator_source": how, "past_tokens": len(past),
        "cached": False, "rugged_field": RUGGED_FIELD, "rugged_field_status": RUGGED_FIELD_STATUS,
        "rugged_source": "rugcheck_report_observed_undocumented",
        "chain_complete": False,
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


def _cache_rejection(creator: str, moment: float, result: dict) -> None:
    """Store a rejection only. A clear result is never cached."""
    if result.get("verified") is True and result.get("rugs") == 0:
        return
    _CACHE[creator] = (float(moment) + CACHE_TTL_S, dict(result))


def _rugcheck(mint: str, helius_creator: str, allow_network: bool) -> dict:
    """GET the keyless full report. Ignore every rugged value that was not fetched."""
    recorded = _REPORTS.get(mint)
    if recorded is None:
        if allow_network is not True or not isinstance(mint, str) or mint == "" or "/" in mint:
            return _unverified("rugged_not_fetched")
        request = urllib.request.Request(
            RUGCHECK_REPORT.format(mint=mint),
            headers={"accept": "application/json"},
            method="GET",
        )
        try:
            status, raw, network = _get(request)
            body = json.loads(raw.decode("utf-8")) if status == 200 else None
        except Exception:
            return _unverified("rugcheck_fetch")
    else:
        status = recorded.get("status")
        body = recorded.get("body")
        network = False
    if status != 200 or not isinstance(body, dict) or not isinstance(body.get(RUGGED_FIELD), bool):
        return _unverified("rugged_not_bool" if status == 200 else "rugcheck_status")
    creator = body.get("creator")
    if not isinstance(creator, str) or creator == "":
        return _unverified("creator_missing")
    if creator != helius_creator:
        return _unverified("creator_mismatch") | {"network": network}
    return {"ok": True, "rugged": body[RUGGED_FIELD], "network": network}


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
        raw, network = _post(request)
        body = json.loads(raw.decode("utf-8"))
    except Exception:
        return _unverified("fetch", key_present=True)
    if not isinstance(body, dict) or body.get("error") is not None:
        return _unverified("fetch", key_present=True)
    return {"ok": True, "body": body, "network": network is True}


def _get(request):
    opener = urllib.request.build_opener(_RefuseRedirect)
    with opener.open(request, timeout=TIMEOUT_S) as response:
        live = isinstance(response, http.client.HTTPResponse)
        status = getattr(response, "status", 200)
        if status != 200:
            return status, b"", live
        return status, response.read(), live


def _post(request):
    opener = urllib.request.build_opener(_RefuseRedirect)
    with opener.open(request, timeout=TIMEOUT_S) as response:
        live = isinstance(response, http.client.HTTPResponse)
        if getattr(response, "status", 200) != 200:
            raise OSError("status")
        return response.read(), live


def _unverified(cause: str, **extra) -> dict:
    body = {
        "ok": False, "verified": False, "reason": "dev_rug_history_unverified",
        "rugs": None, "source": SOURCE, "cause": cause, "rugged_field": RUGGED_FIELD,
        "rugcheck_report": RUGCHECK_REPORT, "rugged_field_status": RUGGED_FIELD_STATUS,
        "fetched": cause == "fetch", "source_wired": True,
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
