"""Bind one token-to-SOL exit to a transaction and its meta.

A positive fee decimal is not a receipt. Canonical SOL/USDC proof is not this
exit. The network fee is meta.fee. The token decrease has to equal the route
input. SOL proceeds are the wallet native increase plus that fee, and they have
to cover the instruction minimum. A missing token row is not zero.
"""

from __future__ import annotations

import base64
import hashlib

import jupiter_route_v2
import phantom_transport
import solana_inspect
import solana_jupiter
import solana_tx
import solana_whirlpool


def bind(mint: str, body: dict, sol_leg: dict) -> dict:
    """Check one mainnet meta. This does not write and it does not send."""
    refused = {"ok": False, "live_ready": False, "send": False}
    if not isinstance(body, dict) or "meta" not in body:
        return refused | {"reason": "receipt_unbound"}
    if body.get("genesis") != solana_inspect.MAINNET_GENESIS:
        return refused | {"reason": "genesis"}
    signature = body.get("signature")
    try:
        raw_sig = solana_tx.b58decode(signature, 64)
    except Exception:
        return refused | {"reason": "signature"}
    if len(raw_sig) != 64 or isinstance(body.get("slot"), bool) or not isinstance(body.get("slot"), int):
        return refused | {"reason": "signature"}
    meta = body.get("meta")
    if not isinstance(meta, dict):
        return refused | {"reason": "receipt_unbound"}
    if meta.get("err") is not None:
        return refused | {"reason": "receipt_failed", "holds": True}
    fee = meta.get("fee")
    if isinstance(fee, bool) or not isinstance(fee, int) or fee <= 0:
        return refused | {"reason": "fee_missing"}
    try:
        decoded = solana_tx.decode_transaction(base64.b64decode(body.get("transaction"), validate=True))
    except Exception:
        return refused | {"reason": "message"}
    if decoded.static_keys[0] != phantom_transport.REQUIRED_WALLET or decoded.required_signatures != 1:
        return refused | {"reason": "wallet"}
    route = _route(decoded)
    if route is None or route["destination_mint"] != solana_tx.NATIVE_MINT or route["source_mint"] != mint:
        return refused | {"reason": "exit_mint"}
    if route["in_amount"] <= 0 or route["min_out"] <= 0:
        return refused | {"reason": "min_out"}
    sold = _token_delta(meta, mint, phantom_transport.REQUIRED_WALLET)
    if sold is None:
        return refused | {"reason": "token_delta_missing"}
    if sold != route["in_amount"]:
        return refused | {"reason": "token_delta"}
    native = _native(decoded.static_keys, meta.get("preBalances"), meta.get("postBalances"), fee)
    if native.get("ok") is not True:
        return refused | {"reason": native.get("reason")}
    if native["proceeds"] < route["min_out"]:
        return refused | {"reason": "proceeds_below_min"}
    pool = None
    for key in route["keys"]:
        found = _pool(body.get("accounts"), key, route["source_mint"])
        if found is not None:
            pool = found
            break
    if pool is None:
        return refused | {"reason": "pool_unbound"}
    dex_raw = jupiter_route_v2.whirlpool_fee(route["in_amount"], pool["fee_rate"])
    dex_lamports = None if dex_raw is None else jupiter_route_v2.token_raw_to_lamports(dex_raw, pool["sqrt_price"], pool["token_is_a"])
    if dex_lamports is None:
        return refused | {"reason": "dex_fee_unproven"}
    priced = __import__("pyth_sponsored").lamports_usd(dex_lamports + fee, sol_leg)
    if priced is None:
        return refused | {"reason": "usd_source_unbound"}
    if priced > __import__("memecoin_desk").MAX_FEE:
        return refused | {"reason": "fee_cap", "fee_usd": format(priced, "f")}
    message_hash = hashlib.sha256(solana_tx.message_bytes(body["transaction"])).hexdigest()
    return {
        "ok": True,
        "reason": None,
        "signature": signature,
        "slot": body["slot"],
        "message_sha256": message_hash,
        "network_fee_lamports": fee,
        "dex_fee_lamports": dex_lamports,
        "fee_usd": format(priced, "f"),
        "sol_proceeds": native["proceeds"],
        "token_in": sold,
        "min_out": route["min_out"],
        "live_ready": False,
        "send": False,
        "holds": False,
    }


def _route(decoded) -> dict | None:
    for instruction in decoded.instructions:
        program = decoded.static_keys[instruction.program_index]
        if program != solana_jupiter.PROGRAM_ID:
            continue
        parsed = jupiter_route_v2.parse(bytes(instruction.data))
        if parsed.get("hop") != "Whirlpool":
            return None
        indexes = instruction.account_indexes
        if len(indexes) < 12:
            return None
        keys = [decoded.static_keys[item] for item in indexes]
        return {
            "in_amount": parsed["in_amount"],
            "min_out": parsed["min_out"],
            "source_mint": keys[3],
            "destination_mint": keys[4],
            "keys": keys,
        }
    return None


def _pool(accounts, pool: str, source_mint: str) -> dict | None:
    if not isinstance(accounts, dict):
        return None
    record = accounts.get(pool)
    if not isinstance(record, dict) or record.get("owner") != solana_whirlpool.PROGRAM_ID:
        return None
    try:
        raw = base64.b64decode(record.get("data"), validate=True)
    except Exception:
        return None
    fields = solana_whirlpool.pool_fields(raw)
    state = solana_whirlpool.pool_state(raw)
    if fields is None or state is None:
        return None
    mints = {fields[0], fields[2]}
    if source_mint not in mints or solana_tx.NATIVE_MINT not in mints:
        return None
    return {"fee_rate": state["fee_rate"], "sqrt_price": state["sqrt_price"], "token_is_a": fields[0] == source_mint}


def _token_delta(meta, mint: str, owner: str) -> int | None:
    pre = _amount(_rows(meta.get("preTokenBalances")), mint, owner)
    post = _amount(_rows(meta.get("postTokenBalances")), mint, owner)
    if pre is None or post is None or pre < post:
        return None
    return pre - post


def _rows(value) -> list:
    return value if isinstance(value, list) else []


def _amount(rows, mint: str, owner: str) -> int | None:
    found = [row for row in rows if isinstance(row, dict) and row.get("mint") == mint and row.get("owner") == owner]
    if len(found) != 1:
        return None
    token = found[0].get("uiTokenAmount")
    amount = token.get("amount") if isinstance(token, dict) else None
    if not isinstance(amount, str) or not amount.isdigit():
        return None
    return int(amount)


def _native(keys, pre, post, fee: int) -> dict:
    if not isinstance(pre, list) or not isinstance(post, list) or len(pre) != len(keys) or len(post) != len(keys):
        return {"ok": False, "reason": "proceeds_missing"}
    if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in pre + post):
        return {"ok": False, "reason": "proceeds_missing"}
    if keys[0] != phantom_transport.REQUIRED_WALLET:
        return {"ok": False, "reason": "wallet"}
    delta = post[0] - pre[0]
    if delta <= 0:
        return {"ok": False, "reason": "proceeds_missing"}
    proceeds = delta + fee
    if proceeds <= fee:
        return {"ok": False, "reason": "proceeds_missing"}
    wsol = solana_tx.associated_token_address(phantom_transport.REQUIRED_WALLET, solana_tx.NATIVE_MINT, solana_tx.TOKEN_PROGRAM)
    if wsol not in keys:
        return {"ok": False, "reason": "rent_unproven"}
    index = keys.index(wsol)
    if pre[index] != 0 or post[index] != 0:
        return {"ok": False, "reason": "rent_unproven"}
    return {"ok": True, "proceeds": proceeds}
