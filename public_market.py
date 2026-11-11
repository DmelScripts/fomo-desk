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

MAX_CALLS = 5


class PublicStop(RuntimeError):
    def __init__(self, reason: str, status: int | None = None):
        self.reason = reason
        self.status = status
        super().__init__(reason)


def run_bounded(
    transport,
    now: float,
    *,
    pool_accounts=None,
    pool=None,
    amount=None,
    input_mint=None,
    output_mint=None,
    slippage_bps=None,
    slippage_allowance_bps=None,
) -> dict:
    """Walk one public mint into the desk, then assess Jupiter without sending.

    A caller slippage number, including 50, 51, or 100, is refused before any
    network call. The Jupiter URL is built only from the stored owner record
    and the estimate measured from pool_accounts. Without that measurement the
    build is not requested.
    """
    if slippage_bps is not None or slippage_allowance_bps is not None:
        return _refused_route("slippage_override")
    import owner_slippage
    if owner_slippage.ceiling_bps() != owner_slippage.BPS:
        return _refused_route("owner_slippage_unapproved")
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
    order, build, build_status, instruction = _jupiter_route(
        get, pool_accounts, pool, amount, input_mint, output_mint,
    )
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
        "instruction_data_b64": instruction.get("instruction_data_b64"),
        "instruction_sha256": instruction.get("instruction_sha256"),
        "instruction_matches_request": instruction.get("instruction_matches_request") is True,
        "measured_est_out": instruction.get("measured_est_out"),
        "requested_slippage_bps": instruction.get("requested_slippage_bps"),
        "rewritten": False,
        "calls": calls,
        "send": False,
        "sign": False,
        "live_ready": False,
        "fixture": False,
    }


def _refused_route(reason: str) -> dict:
    """No transport. A missing policy or a caller slippage number never becomes a URL."""
    return {
        "ok": False,
        "reason": reason,
        "calls": [],
        "http_calls": 0,
        "send": False,
        "sign": False,
        "live_ready": False,
        "fixture": False,
        "instruction_matches_request": False,
        "rewritten": False,
        "requested_slippage_bps": None,
        "measured_est_out": None,
        "jupiter_order": {"reason": reason, "execution_allowed": False, "min_out_proven": False, "transaction_present": False},
        "jupiter_build": {"reason": reason, "execution_allowed": False, "send": False, "min_out_proven": False, "buy_proven": False, "exit_proven": False},
    }


def request_quote(get, estimate: int, amount: int, input_mint: str, output_mint: str) -> tuple:
    """HTTP order and build for an estimate the snapshot review already measured.

    This does not read pool bytes and it does not accept a slippage override.
    """
    import base64
    import hashlib
    import jupiter_route_v2
    import owner_slippage
    order = {"ok": False, "reason": "estimate_unavailable", "execution_allowed": False, "min_out_proven": False, "transaction_present": False}
    build = {"ok": False, "reason": "estimate_unavailable", "execution_allowed": False, "send": False, "min_out_proven": False, "buy_proven": False, "exit_proven": False}
    instruction = {"instruction_matches_request": False, "rewritten": False, "requested_slippage_bps": None, "measured_est_out": estimate, "instruction_data_b64": None, "instruction_sha256": None, "jupiter_called": False}
    if isinstance(estimate, bool) or not isinstance(estimate, int) or estimate <= 0:
        return order, build, instruction
    selected = jupiter_route_v2.quote_request(estimate)
    if selected.get("ok") is not True or selected.get("approved") is not True:
        order["reason"] = selected.get("reason") or "owner_slippage_unapproved"
        build["reason"] = order["reason"]
        return order, build, instruction
    requested = selected["slippage_bps"]
    instruction["requested_slippage_bps"] = requested
    try:
        order = jupiter_swap_v2.assess_order(get(jupiter_swap_v2.order_url(input_mint, output_mint, str(amount))))
        build_body = get(jupiter_swap_v2.build_url(input_mint, output_mint, str(amount), slippage_bps=requested))
    except PublicStop as exc:
        build = {"ok": False, "reason": exc.reason, "status": exc.status, "execution_allowed": False, "send": False, "min_out_proven": False, "buy_proven": False, "exit_proven": False}
        instruction["jupiter_called"] = True
        return order, build, instruction
    instruction["jupiter_called"] = True
    if not isinstance(build_body, dict):
        build = {"ok": False, "reason": "build_shape", "execution_allowed": False, "send": False, "min_out_proven": False, "buy_proven": False, "exit_proven": False}
        return order, build, instruction
    raw = _swap_bytes(build_body)
    build = jupiter_swap_v2.assess_build(build_body)
    parsed = jupiter_route_v2.parse(raw) if raw else {"ok": False}
    check = owner_slippage.check_route(parsed.get("slippage_bps"), parsed.get("min_out"), estimate)
    matches = (
        raw != b""
        and parsed.get("slippage_bps") == requested
        and parsed.get("quoted_out_amount") == estimate
        and check.get("ok") is True
    )
    instruction = {
        "instruction_data_b64": None if not raw else base64.b64encode(raw).decode("ascii"),
        "instruction_sha256": None if not raw else hashlib.sha256(raw).hexdigest(),
        "instruction_matches_request": matches,
        "measured_est_out": estimate,
        "requested_slippage_bps": requested,
        "rewritten": False,
        "jupiter_called": True,
        "quote_expiry_proven": False,
        "block_height_missing": True,
    }
    build["requested_slippage_bps"] = requested
    build["measured_est_out"] = estimate
    build["instruction_matches_request"] = matches
    build["execution_allowed"] = False
    build["buy_proven"] = False
    build["exit_proven"] = False
    build["send"] = False
    if not matches:
        build["reason"] = check.get("reason") or parsed.get("reason") or "instruction_mismatch"
    return order, build, instruction


def _jupiter_route(get, pool_accounts, pool, amount, input_mint, output_mint) -> tuple:
    """Measure the pool, select the bps, then fetch. The response bytes are not edited."""
    import base64
    import hashlib
    import jupiter_route_v2
    import owner_slippage
    order = {"ok": False, "reason": "estimate_unavailable", "execution_allowed": False, "min_out_proven": False, "transaction_present": False}
    build = {"ok": False, "reason": "estimate_unavailable", "execution_allowed": False, "send": False, "min_out_proven": False, "buy_proven": False, "exit_proven": False}
    instruction = {"instruction_matches_request": False, "rewritten": False, "requested_slippage_bps": None, "measured_est_out": None, "instruction_data_b64": None, "instruction_sha256": None}
    if pool_accounts is None or pool is None or amount is None or input_mint is None or output_mint is None:
        return order, build, None, instruction
    measured = jupiter_route_v2.measure_pool(pool_accounts, pool, amount, input_mint)
    if measured.get("ok") is not True:
        order["reason"] = measured.get("reason") or "estimate_unavailable"
        build["reason"] = order["reason"]
        return order, build, None, instruction
    selected = jupiter_route_v2.quote_request(measured["est_out"])
    instruction["measured_est_out"] = measured["est_out"]
    if selected.get("ok") is not True or selected.get("approved") is not True:
        order["reason"] = selected.get("reason") or "owner_slippage_unapproved"
        build["reason"] = order["reason"]
        return order, build, None, instruction
    requested = selected["slippage_bps"]
    instruction["requested_slippage_bps"] = requested
    try:
        order = jupiter_swap_v2.assess_order(get(jupiter_swap_v2.order_url(input_mint, output_mint, str(amount))))
        build_body = get(jupiter_swap_v2.build_url(input_mint, output_mint, str(amount), slippage_bps=requested))
    except PublicStop as exc:
        if order.get("reason") == "estimate_unavailable":
            order = {"ok": False, "reason": exc.reason, "status": exc.status, "execution_allowed": False, "min_out_proven": False, "transaction_present": False}
            return order, build, exc.status, instruction
        build = {"ok": False, "reason": exc.reason, "status": exc.status, "execution_allowed": False, "send": False, "min_out_proven": False, "buy_proven": False, "exit_proven": False}
        return order, build, exc.status, instruction
    if not isinstance(build_body, dict):
        build = {"ok": False, "reason": "build_shape", "execution_allowed": False, "send": False, "min_out_proven": False, "buy_proven": False, "exit_proven": False}
        return order, build, None, instruction
    raw = _swap_bytes(build_body)
    build = jupiter_swap_v2.assess_build(build_body)
    parsed = jupiter_route_v2.parse(raw) if raw else {"ok": False}
    check = owner_slippage.check_route(parsed.get("slippage_bps"), parsed.get("min_out"), measured["est_out"])
    matches = (
        raw != b""
        and parsed.get("slippage_bps") == requested
        and parsed.get("quoted_out_amount") == measured["est_out"]
        and check.get("ok") is True
    )
    encoded = base64.b64encode(raw).decode("ascii") if raw else None
    instruction = {
        "instruction_data_b64": encoded,
        "instruction_sha256": None if not raw else hashlib.sha256(raw).hexdigest(),
        "instruction_matches_request": matches,
        "measured_est_out": measured["est_out"],
        "requested_slippage_bps": requested,
        "rewritten": False,
    }
    build["requested_slippage_bps"] = requested
    build["measured_est_out"] = measured["est_out"]
    build["instruction_matches_request"] = matches
    build["execution_allowed"] = False
    build["buy_proven"] = False
    build["exit_proven"] = False
    build["send"] = False
    if not matches:
        build["reason"] = check.get("reason") or parsed.get("reason") or "instruction_mismatch"
    return order, build, None, instruction


def _swap_bytes(body: dict) -> bytes:
    import base64
    instruction = body.get("swapInstruction") if isinstance(body.get("swapInstruction"), dict) else {}
    data = instruction.get("data")
    if not isinstance(data, str) or data == "":
        return b""
    try:
        return base64.b64decode(data, validate=True)
    except Exception:
        return b""


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
        import solana_rpc
        info = solana_rpc.describe()
        if info["ok"] is not True:
            raise PublicStop("helius_api_key_missing")
        try:
            response = httpx.post(solana_rpc._wire_url(), json=payload, timeout=8.0, follow_redirects=False)
        except Exception:
            raise PublicStop("transport") from None
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
