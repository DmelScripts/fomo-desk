"""Bounded public reads. The default transport does not open the network.

One run may call DexScreener, one mint getAccountInfo, one keyless Jupiter
order, and one keyless Jupiter build. Redirects are not followed. No API key,
cookie, or signer is attached. A missing LP, developer, bundle, unique-buyer,
or smart-wallet fact still rejects inside the desk.
"""

from __future__ import annotations

import json

import dexscreener_public
import jupiter_swap_v2
import memecoin_desk
import solana_inspect
import solana_mint_read
import solana_tx

MAX_CALLS = 5


class PublicStop(RuntimeError):
    def __init__(self, reason: str, status: int | None = None):
        self.reason = reason
        self.status = status
        super().__init__(reason)


def run_bounded(transport, now: float) -> dict:
    """Walk one public mint into the desk, then assess Jupiter without sending."""
    if transport is None:
        raise PublicStop("network_disabled")
    calls = []

    def get(url: str):
        if len(calls) >= MAX_CALLS:
            raise PublicStop("call_budget")
        calls.append({"kind": "http", "url": url.split("?", 1)[0]})
        response = transport({"kind": "http", "url": url})
        return _http(response)

    def rpc(mint: str):
        if len(calls) >= MAX_CALLS:
            raise PublicStop("call_budget")
        calls.append({"kind": "rpc", "method": "getAccountInfo"})
        response = transport({"kind": "rpc", "method": "getAccountInfo", "mint": mint})
        return _rpc(response)

    boosts = get(dexscreener_public.BOOSTS_URL)
    found = dexscreener_public.parse_boost_mint(boosts)
    if found.get("ok") is not True:
        return {"ok": False, "reason": found.get("reason"), "stage": "scout", "calls": calls, "send": False, "live_ready": False}
    mint = found["mint"]
    parsed = dexscreener_public.parse_pairs(get(dexscreener_public.pairs_url(mint)), mint, now)
    opened = {"ok": False, "reason": parsed.get("reason"), "stage": "scout", "case_id": None}
    if parsed.get("ok") is True:
        opened = memecoin_desk.scout(parsed["scout"], now=now)
    try:
        account = solana_mint_read.parse_account(rpc(mint), mint)
    except PublicStop as exc:
        account = {"ok": False, "reason": exc.reason, "status": exc.status, "not_proven": list(solana_mint_read.NOT_PROVEN)}
    onchain = {"source": solana_mint_read.SOURCE, "fetched_at": now}
    if account.get("ok") is True:
        onchain["mint_authority_revoked"] = account["mint_authority_revoked"]
        onchain["freeze_authority_revoked"] = account["freeze_authority_revoked"]
    filtered = {"ok": False, "reason": opened.get("reason"), "stage": opened.get("stage")}
    if opened.get("ok") is True:
        filtered = memecoin_desk.filter_case(opened["case_id"], onchain, now=now)
    order = {"ok": False, "reason": "not_called", "execution_allowed": False, "min_out_proven": False}
    build = None
    build_status = None
    try:
        order = jupiter_swap_v2.assess_order(get(jupiter_swap_v2.order_url(solana_tx.NATIVE_MINT, solana_tx.USDC_MINT, "10000000")))
        build_body = get(jupiter_swap_v2.build_url(solana_tx.NATIVE_MINT, solana_tx.USDC_MINT, "10000000"))
        build = jupiter_swap_v2.assess_build(build_body)
    except PublicStop as exc:
        build_status = exc.status
        if order.get("reason") == "not_called":
            order = {"ok": False, "reason": exc.reason, "status": exc.status, "execution_allowed": False, "min_out_proven": False, "transaction_present": False}
        else:
            build = {"ok": False, "reason": exc.reason, "status": exc.status, "execution_allowed": False, "send": False, "min_out_proven": False, "buy_proven": False, "exit_proven": False}
    return {
        "ok": False,
        "reason": filtered.get("reason") or parsed.get("reason"),
        "mint": mint,
        "case_id": opened.get("case_id"),
        "stage": filtered.get("stage") or opened.get("stage"),
        "fdv_used_as_market_cap": False,
        "not_proven": parsed.get("not_proven"),
        "mint_account": {key: account.get(key) for key in ("ok", "reason", "mint_authority_revoked", "freeze_authority_revoked", "not_proven")},
        "jupiter_order": {key: order.get(key) for key in ("reason", "router", "fee_bps", "transaction_present", "execution_allowed", "min_out_proven")},
        "jupiter_build": None if build is None else {key: build.get(key) for key in (
            "reason", "status", "program_id", "program_is_jup6", "matches_route_v2", "data_len", "account_count",
            "discriminator_hex", "min_out_proven", "dex_fee_proven", "network_fee_proven", "combined_fee_usd_proven",
            "execution_allowed", "buy_proven", "exit_proven",
        )},
        "jupiter_build_status": build_status,
        "calls": calls,
        "send": False,
        "sign": False,
        "live_ready": False,
        "fixture": False,
    }


def live_transport(request: dict):
    """One public call. No key, no redirect, no second host."""
    import httpx
    if request.get("kind") == "rpc":
        if request.get("method") != "getAccountInfo":
            raise PublicStop("method_refused")
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAccountInfo",
            "params": [request["mint"], {"encoding": "base64", "commitment": "confirmed"}],
        }
        try:
            response = httpx.post(solana_inspect.MAINNET_RPC, json=payload, timeout=8.0, follow_redirects=False)
        except Exception as exc:
            raise PublicStop("transport") from exc
        if response.status_code in {301, 302, 303, 307, 308}:
            raise PublicStop("redirect", response.status_code)
        if response.status_code != 200:
            raise PublicStop("rpc_status", response.status_code)
        try:
            body = response.json()
        except Exception as exc:
            raise PublicStop("rpc_status") from exc
        return {"status": 200, "body": body}
    url = request.get("url")
    if not isinstance(url, str) or not (url.startswith("https://api.dexscreener.com/") or url.startswith("https://api.jup.ag/swap/v2/")):
        raise PublicStop("host_refused")
    try:
        response = httpx.get(url, timeout=8.0, follow_redirects=False, headers={"accept": "application/json"})
    except Exception as exc:
        raise PublicStop("transport") from exc
    if response.status_code in {301, 302, 303, 307, 308}:
        raise PublicStop("redirect", response.status_code)
    if response.status_code in {401, 403}:
        raise PublicStop("auth", response.status_code)
    if response.status_code != 200:
        raise PublicStop("http_status", response.status_code)
    try:
        body = response.json()
    except Exception as exc:
        raise PublicStop("http_status") from exc
    return {"status": 200, "body": body}


def _http(response) -> dict | list:
    if not isinstance(response, dict):
        raise PublicStop("http_status")
    status = response.get("status")
    if status in {401, 403}:
        raise PublicStop("auth", status if isinstance(status, int) else None)
    if status != 200 or "body" not in response:
        raise PublicStop("http_status", status if isinstance(status, int) else None)
    body = response.get("body")
    if isinstance(body, (dict, list)):
        return body
    raise PublicStop("http_status")


def _rpc(response) -> dict:
    if not isinstance(response, dict) or response.get("status") != 200:
        raise PublicStop("rpc_status")
    body = response.get("body")
    if not isinstance(body, dict) or body.get("error") is not None:
        raise PublicStop("rpc_result")
    result = body.get("result")
    value = result.get("value") if isinstance(result, dict) else None
    if not isinstance(value, dict):
        raise PublicStop("account_missing")
    return value


def dumps(result: dict) -> str:
    return json.dumps(result, sort_keys=True, default=str)
