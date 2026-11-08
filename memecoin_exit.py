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
    """Caller documents are not a chain receipt."""
    return {"ok": False, "live_ready": False, "send": False, "reason": "receipt_unbound", "holds": True, "resend": False}


def pull(signature: str, transport) -> dict:
    """One confirmed getTransaction. A missing or failed read holds the position and does not fill."""
    refused = {"ok": False, "holds": True, "resend": False, "live_ready": False, "send": False}
    if transport is None or not isinstance(signature, str):
        return refused | {"reason": "receipt_unknown"}
    try:
        response = transport({
            "method": "getTransaction",
            "signature": signature,
            "commitment": "confirmed",
            "url": "https://api.mainnet-beta.solana.com",
        })
    except Exception:
        return refused | {"reason": "receipt_unknown"}
    if not isinstance(response, dict) or response.get("status") != 200 or not isinstance(response.get("body"), dict):
        return refused | {"reason": "receipt_unknown"}
    result = response["body"].get("result")
    if result is None:
        return refused | {"reason": "receipt_unconfirmed"}
    if not isinstance(result, dict):
        return refused | {"reason": "receipt_unknown"}
    meta = result.get("meta")
    if not isinstance(meta, dict):
        return refused | {"reason": "receipt_unknown"}
    if meta.get("err") is not None:
        return refused | {"reason": "receipt_failed"}
    encoded = result.get("transaction")
    if not isinstance(encoded, list) or not encoded or not isinstance(encoded[0], str):
        return refused | {"reason": "receipt_unknown"}
    slot = result.get("slot")
    if isinstance(slot, bool) or not isinstance(slot, int):
        return refused | {"reason": "slot_unproven"}
    return {"ok": True, "transaction": encoded[0], "meta": meta, "slot": slot, "holds": False, "resend": False}


def match_landed(reserved: dict, signature: str, pulled: dict) -> dict:
    """The landed transaction must be the reserved message and must carry this signature."""
    refused = {"ok": False, "live_ready": False, "send": False, "holds": True, "resend": False}
    if not isinstance(reserved, dict) or not isinstance(pulled, dict) or pulled.get("ok") is not True:
        return refused | {"reason": pulled.get("reason") if isinstance(pulled, dict) else "receipt_unknown"}
    try:
        landed = solana_tx.first_signature(pulled["transaction"])
    except Exception:
        return refused | {"reason": "receipt_unsigned"}
    if landed != signature:
        return refused | {"reason": "signature_mismatch"}
    try:
        digest = hashlib.sha256(solana_tx.message_bytes(pulled["transaction"])).hexdigest()
    except Exception:
        return refused | {"reason": "message"}
    if digest != reserved.get("message_sha256"):
        return refused | {"reason": "message_mismatch"}
    decoded = solana_tx.decode_transaction(base64.b64decode(pulled["transaction"], validate=True))
    route = _route(decoded)
    if route is None or route["source_mint"] != reserved.get("mint") or route["in_amount"] != reserved.get("qty"):
        return refused | {"reason": "exit_mint"}
    if reserved.get("pool") not in route["keys"]:
        return refused | {"reason": "pool_unbound"}
    sold = _token_delta(pulled["meta"], reserved["mint"], phantom_transport.REQUIRED_WALLET)
    if sold is None:
        return refused | {"reason": "token_delta_missing"}
    if sold != reserved["qty"]:
        return refused | {"reason": "token_delta"}
    fee = pulled["meta"].get("fee")
    native = _native(decoded.static_keys, pulled["meta"].get("preBalances"), pulled["meta"].get("postBalances"), fee if isinstance(fee, int) else -1)
    if native.get("ok") is not True:
        return refused | {"reason": native.get("reason")}
    if native["proceeds"] < route["min_out"]:
        return refused | {"reason": "proceeds_below_min"}
    return {
        "ok": True,
        "reason": None,
        "signature": signature,
        "slot": pulled["slot"],
        "message_sha256": digest,
        "token_in": sold,
        "sol_proceeds": native["proceeds"],
        "network_fee_lamports": fee,
        "min_out": route["min_out"],
        "holds": False,
        "resend": False,
        "live_ready": False,
        "send": False,
        "live_ledger": True,
    }


def plan_slices(raw: int, sqrt_price: int, token_is_a: bool, sol_leg: dict, cap: str = "8") -> dict:
    """Split a token balance into reduce-only slices whose SOL value is at most the ticket."""
    from decimal import Decimal
    limit = Decimal(cap)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        return {"ok": False, "reason": "quantity", "slices": []}

    def dollars(qty: int):
        lamports = jupiter_route_v2.token_raw_to_lamports(qty, sqrt_price, token_is_a)
        if lamports is None:
            return None
        return __import__("pyth_sponsored").lamports_usd(lamports, sol_leg)

    one = dollars(1)
    if one is None:
        return {"ok": False, "reason": "usd_source_unbound", "slices": []}
    if one > limit:
        return {"ok": False, "reason": "unit_above_cap", "slices": []}
    slices = []
    left = raw
    while left > 0:
        lo, hi, best = 1, left, 1
        while lo <= hi:
            mid = (lo + hi) // 2
            value = dollars(mid)
            if value is not None and value <= limit:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        slices.append(best)
        left -= best
        if len(slices) > 64:
            return {"ok": False, "reason": "too_many_slices", "slices": slices}
    return {"ok": True, "reason": None, "slices": slices, "cap": cap, "whole_raw": raw}


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
