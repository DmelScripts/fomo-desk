"""StalkChain REST adapter. It does not read a connector token.

POST https://data.stalkchain.com/api/v1/tools/{name}
Body for the on-chain mint tools is exactly {"token": mint}.
Authorization is Bearer. The OpenAPI security scheme is global bearerAuth:
an sc_ dashboard key or an OAuth access token. This process uses only
STALKCHAIN_API_KEY when that variable is set in this process. A missing
variable here does not say whether Render has one. The value is never returned.

The 200 body is documented as {data, notes?, credits, meta}. The tool text
names marketCapBasis, marketCapSource, and fdvUsd. It does not name JSON
fields for LP lock expiry, rug history, or holder growth. Those stay unproven
even after a well-formed body. fdvUsd is not used as market cap.
https://data.stalkchain.com/api/v1/openapi.json
"""

from __future__ import annotations

import os

URL = "https://data.stalkchain.com/api/v1/tools/"
ENV = "STALKCHAIN_API_KEY"
TOOLS = (
    "stalkchain_token_onchain",
    "stalkchain_token_quality",
    "stalkchain_token_early_buyers",
    "stalkchain_token_deployer",
)
NAMED = ("marketCapBasis", "marketCapSource", "fdvUsd")
UNPROVEN = (
    "lp_burned_or_locked",
    "lp_lock_expiry",
    "dev_holding_pct",
    "dev_rugs",
    "bundled_launch_pct",
    "holder_growth",
    "unique_buyers",
    "smart_wallets",
)


def this_process_has_key() -> bool:
    value = os.environ.get(ENV)
    return isinstance(value, str) and bool(value.strip())


def request_for(tool: str, mint: str) -> dict:
    """The documented call. The bearer value is attached by the transport wrapper, not this dict."""
    if tool not in TOOLS:
        raise ValueError("tool")
    return {
        "method": "POST",
        "url": URL + tool,
        "json": {"token": mint},
        "accept": "application/json",
    }


def normalize(body) -> dict:
    """Map a documented envelope. Unpublished facts stay unpublished."""
    refused = {
        "ok": False,
        "fdv_used_as_market_cap": False,
        "unproven": list(UNPROVEN),
        "named": {},
        "score_measured": False,
    }
    if not isinstance(body, dict) or set(body) - {"data", "notes", "credits", "meta"} or not isinstance(body.get("data"), dict):
        return refused | {"reason": "envelope"}
    data = body["data"]
    named = {}
    for key in NAMED:
        if key in data:
            named[key] = data[key]
    return refused | {
        "ok": True,
        "reason": "response_fields_unpublished",
        "named": named,
        "fdv_used_as_market_cap": False,
    }


def read_mint(mint: str, transport) -> dict:
    """Call the four mint tools only when this process has a key. Never echo it."""
    public = {
        "called": False,
        "this_process_key_present": this_process_has_key(),
        "render_presence": "unknown",
        "unproven": list(UNPROVEN),
        "score_measured": False,
        "fdv_used_as_market_cap": False,
        "live_ready": False,
        "credential_env": ENV,
        "url": URL,
    }
    if not public["this_process_key_present"]:
        return public | {"ok": False, "reason": "stalkchain_credentials_missing"}
    if transport is None:
        return public | {"ok": False, "reason": "stalkchain_transport_missing"}
    secret = os.environ.get(ENV, "").strip()
    named = {}
    for tool in TOOLS:
        request = request_for(tool, mint)
        request["headers"] = {"authorization": "Bearer " + secret}
        try:
            response = transport(request)
        except Exception:
            return public | {"ok": False, "reason": "stalkchain_transport", "called": True}
        status = response.get("status") if isinstance(response, dict) else None
        if status == 401 or status == 403:
            return public | {"ok": False, "reason": "stalkchain_unauthorized", "called": True, "http_status": status}
        if status != 200 or not isinstance(response, dict):
            return public | {"ok": False, "reason": "stalkchain_status", "called": True, "http_status": status}
        parsed = normalize(response.get("body"))
        if parsed.get("ok") is not True:
            return public | {"ok": False, "reason": parsed.get("reason"), "called": True}
        named.update(parsed["named"])
    return public | {
        "ok": False,
        "called": True,
        "reason": "response_fields_unpublished",
        "named": named,
    }
