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

import json
import os
import urllib.error
import urllib.request

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
# These files were not in this tree. A prose mention in OpenAPI is not a JSON path.
OBSERVATION_FILES = (
    "nov8-observed-mcp-field-mapping.json",
    "fixtures.json",
    "source-paths.txt",
)
FIELDS = (
    {"field": "lp_burned_or_locked", "tool": "stalkchain_token_onchain", "prose": "how much of the liquidity pool is burned", "not": "creation time is not LP lock expiry"},
    {"field": "lp_lock_expiry", "tool": "stalkchain_token_onchain", "prose": None, "not": "no lock-expiry path is named"},
    {"field": "dev_holding_pct", "tool": "stalkchain_token_onchain", "prose": "the developer's own holding", "not": None},
    {"field": "dev_rugs", "tool": "stalkchain_token_deployer", "prose": "has the dev rugged before", "not": "a launch count is not a clean rug proof"},
    {"field": "bundled_launch_pct", "tool": "stalkchain_token_early_buyers", "prose": "wallets that bought in the same block as creation", "not": None},
    {"field": "holder_growth", "tool": "stalkchain_token_onchain", "prose": "total on-chain holder count", "not": "one count is not growth, and a cross-provider difference is not growth"},
    {"field": "unique_buyers", "tool": None, "prose": None, "not": "DexScreener transaction counts are not unique buyers"},
    {"field": "smart_wallets", "tool": None, "prose": None, "not": "a boost is not a smart-wallet fact"},
    {"field": "analyst_score", "tool": "stalkchain_token_quality", "prose": "an organic score with its label", "not": "that organic score is not this desk's analyst score"},
)


def field_requirements() -> list[dict]:
    """What each handoff fact still needs. No JSON path is invented from the prose."""
    rows = []
    for item in FIELDS:
        rows.append({
            "field": item["field"],
            "tool": item["tool"],
            "prose": item["prose"],
            "json_path": None,
            "status": "observation_required",
            "proven": False,
            "not": item["not"],
            "observation_files": list(OBSERVATION_FILES),
            "response_schema": False,
            "observed_mcp_is_rest_envelope": False,
            "fixture_is_mainnet_proof": False,
        })
    return rows


def capability(reason: str, *, called: bool, http_status=None) -> dict:
    """Transport and request shape are separate from an unverified response."""
    transport = {
        "stalkchain_credentials_missing": "unconfigured",
        "host_refused": "host_blocked",
        "method_refused": "request_rejected",
        "tool_refused": "request_rejected",
        "body_refused": "request_rejected",
        "redirect_refused": "redirect_refused",
        "stalkchain_unauthorized": "unauthorized",
        "stalkchain_credit_limit": "credit_limited",
        "stalkchain_status": "http_error",
        "stalkchain_transport": "transport_error",
        "response_fields_unpublished": "ready",
        "envelope": "ready",
        "mint_mismatch": "ready",
    }.get(reason, "unconfigured" if not called else "transport_error")
    if http_status == 402:
        transport = "credit_limited"
    request_schema = "not_sent"
    if reason in {"method_refused", "tool_refused", "body_refused", "host_refused"}:
        request_schema = "rejected"
    elif called:
        request_schema = "documented"
    return {
        "transport": transport,
        "request_schema": request_schema,
        "response_verified": False,
        "freshness": "unobserved",
        "lp": "unproven",
        "rugs": "unproven",
        "growth": "unproven",
        "score": "unapproved",
        "analyst_formula": "UNAPPROVED",
    }


def this_process_has_key() -> bool:
    value = os.environ.get(ENV)
    return isinstance(value, str) and bool(value.strip())


def canonical_mint(value) -> bool:
    """A Solana address is canonical base58 of exactly 32 bytes. Curve membership is not required."""
    if isinstance(value, bool) or not isinstance(value, str) or any(char.isspace() for char in value):
        return False
    try:
        import solana_tx
        raw = solana_tx.b58decode(value, 32)
    except ValueError:
        return False
    return solana_tx.b58encode(raw) == value


def request_for(tool: str, mint: str) -> dict:
    """The documented call. The bearer value is attached by the transport wrapper, not this dict."""
    if tool not in TOOLS:
        raise ValueError("tool")
    if not canonical_mint(mint):
        raise ValueError("mint")
    return {
        "method": "POST",
        "url": URL + tool,
        "json": {"token": mint},
        "accept": "application/json",
    }


def normalize(body, mint: str | None = None) -> dict:
    """Map a documented envelope. Unpublished facts stay unpublished.

    Extra keys, including LP, rug, growth, and score, are not promoted.
    A body that names a different mint is refused.
    """
    refused = {
        "ok": False,
        "fdv_used_as_market_cap": False,
        "unproven": list(UNPROVEN),
        "named": {},
        "proven": {},
        "components": {},
        "synthetic_fields": False,
        "live_mcp": False,
        "score_measured": False,
    }
    if not isinstance(body, dict) or set(body) - {"data", "notes", "credits", "meta"} or not isinstance(body.get("data"), dict):
        return refused | {"reason": "envelope", "fields": field_requirements(), "freshness": "unobserved"}
    data = body["data"]
    echoed = data.get("mint", data.get("token"))
    if echoed is not None and (not isinstance(echoed, str) or (mint is not None and echoed != mint)):
        return refused | {"reason": "mint_mismatch", "fields": field_requirements(), "freshness": "unobserved"}
    named = {}
    for key in NAMED:
        if key in data:
            named[key] = data[key]
    unmapped = sorted(key for key in data if key not in NAMED and key not in {"mint", "token"})
    return refused | {
        "ok": True,
        "reason": "response_fields_unpublished",
        "named": named,
        "unmapped_keys": unmapped,
        "fields": field_requirements(),
        "freshness": "unobserved",
        "fdv_used_as_market_cap": False,
        "fixture_is_mainnet_proof": False,
        "observed_mcp_is_rest_envelope": False,
        "response_schema": False,
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post(request: dict) -> dict:
    """POST one documented tool URL. Redirects are refused. The bearer is not logged."""
    url = request.get("url") if isinstance(request, dict) else None
    if not isinstance(url, str) or not url.startswith(URL) or not isinstance(request.get("json"), dict):
        return {"status": None, "body": None}
    headers = {"accept": "application/json", "content-type": "application/json"}
    authorization = (request.get("headers") or {}).get("authorization")
    if isinstance(authorization, str):
        headers["authorization"] = authorization
    payload = json.dumps(request["json"]).encode("utf-8")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(urllib.request.Request(url, data=payload, headers=headers, method="POST"), timeout=8) as response:
            return {"status": response.status, "body": json.loads(response.read().decode("utf-8"))}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "body": None}
    except Exception:
        return {"status": None, "body": None}


def read_mint(mint: str, transport) -> dict:
    """Call the four mint tools only when this process has a key. Never echo it."""
    public = {
        "called": False,
        "this_process_key_present": this_process_has_key(),
        "render_presence": "unknown",
        "unproven": list(UNPROVEN),
        "fields": field_requirements(),
        "score_measured": False,
        "fdv_used_as_market_cap": False,
        "live_ready": False,
        "credential_env": ENV,
        "url": URL,
        "fixture_is_mainnet_proof": False,
        "observed_mcp_is_rest_envelope": False,
        "response_schema": False,
        "observation_files": list(OBSERVATION_FILES),
        "freshness": "unobserved",
    }
    if not public["this_process_key_present"]:
        return public | {"ok": False, "reason": "stalkchain_credentials_missing", "capability": capability("stalkchain_credentials_missing", called=False)}
    if transport is None:
        return public | {"ok": False, "reason": "stalkchain_transport_missing", "capability": capability("stalkchain_transport_missing", called=False)}
    if not canonical_mint(mint):
        return public | {"ok": False, "reason": "body_refused", "called": False, "capability": capability("body_refused", called=False)}
    secret = os.environ.get(ENV, "").strip()
    named = {}
    for tool in TOOLS:
        request = request_for(tool, mint)
        request["headers"] = {"authorization": "Bearer " + secret}
        try:
            response = transport(request)
        except RuntimeError as exc:
            code = str(exc)
            if code not in {"host_refused", "method_refused", "tool_refused", "body_refused", "redirect_refused", "stalkchain_credentials_missing"}:
                code = "stalkchain_transport"
            return public | {"ok": False, "reason": code, "called": True, "capability": capability(code, called=True)}
        except Exception:
            return public | {"ok": False, "reason": "stalkchain_transport", "called": True, "capability": capability("stalkchain_transport", called=True)}
        status = response.get("status") if isinstance(response, dict) else None
        if status == 401 or status == 403:
            return public | {"ok": False, "reason": "stalkchain_unauthorized", "called": True, "http_status": status, "capability": capability("stalkchain_unauthorized", called=True, http_status=status)}
        if status == 402:
            return public | {"ok": False, "reason": "stalkchain_credit_limit", "called": True, "http_status": 402, "capability": capability("stalkchain_credit_limit", called=True, http_status=402)}
        if status in {301, 302, 303, 307, 308}:
            return public | {"ok": False, "reason": "redirect_refused", "called": True, "http_status": status, "capability": capability("redirect_refused", called=True, http_status=status)}
        if status != 200 or not isinstance(response, dict):
            return public | {"ok": False, "reason": "stalkchain_status", "called": True, "http_status": status, "capability": capability("stalkchain_status", called=True, http_status=status)}
        parsed = normalize(response.get("body"), mint)
        if parsed.get("ok") is not True:
            reason = parsed.get("reason")
            return public | {"ok": False, "reason": reason, "called": True, "tool": tool, "fields": field_requirements(), "freshness": "unobserved", "capability": capability(reason, called=True)}
        named.update(parsed["named"])
        public.setdefault("unmapped_keys", [])
        public["unmapped_keys"] = sorted(set(public["unmapped_keys"]) | set(parsed.get("unmapped_keys") or []))
    return public | {
        "ok": False,
        "called": True,
        "reason": "response_fields_unpublished",
        "named": named,
        "proven": {},
        "unproven": list(UNPROVEN),
        "components": {},
        "fields": field_requirements(),
        "synthetic_fields": False,
        "live_mcp": False,
        "freshness": "unobserved",
        "fixture_is_mainnet_proof": False,
        "capability": capability("response_fields_unpublished", called=True),
    }
