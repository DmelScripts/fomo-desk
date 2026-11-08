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


def pull_response(response: dict) -> dict:
    """Parse one getTransaction response. A null result is unconfirmed, not a fill."""
    refused = {"ok": False, "holds": True, "resend": False, "live_ready": False, "send": False}
    if not isinstance(response, dict) or response.get("status") != 200 or not isinstance(response.get("body"), dict):
        return refused | {"reason": "receipt_unknown"}
    if response["body"].get("error"):
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
    block_time = result.get("blockTime")
    return {
        "ok": True,
        "transaction": encoded[0],
        "meta": meta,
        "slot": slot,
        "block_time": block_time if isinstance(block_time, int) and not isinstance(block_time, bool) else None,
        "holds": False,
        "resend": False,
    }


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
    block_time = result.get("blockTime")
    return {
        "ok": True,
        "transaction": encoded[0],
        "meta": meta,
        "slot": slot,
        "block_time": block_time if isinstance(block_time, int) and not isinstance(block_time, bool) else None,
        "holds": False,
        "resend": False,
    }


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
    bound = _provenance(reserved, decoded, pulled["slot"], route)
    if bound.get("ok") is not True:
        return refused | {key: value for key, value in bound.items() if key != "ok"}
    rent = rent_ledger(decoded, pulled["meta"], reserved["mint"])
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
        "rent": rent,
        **bound,
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


def _provenance(reserved: dict, decoded, landed_slot: int, route: dict) -> dict:
    """The receipt matches the reserved message. It does not remeasure the pool fee.

    The snapshot account slot has to be at or before the landed slot, and the
    landed slot has to fall inside the 150-slot recent-blockhash window measured
    from that snapshot. The message hash already covers the blockhash bytes.
    This does not call isBlockhashValid, and a missing block time is not a clock
    proof. The DEX fee stays the figure stored from the snapshot pool state.
    """
    import jupiter_route_v2
    snapshot_slot = reserved.get("snapshot_slot")
    tip = reserved.get("snapshot_tip_slot")
    if isinstance(snapshot_slot, bool) or not isinstance(snapshot_slot, int):
        return {"ok": False, "reason": "slot_unbound"}
    if isinstance(tip, bool) or not isinstance(tip, int) or tip < snapshot_slot:
        return {"ok": False, "reason": "slot_unbound"}
    if snapshot_slot > landed_slot:
        return {"ok": False, "reason": "snapshot_after_landed", "snapshot_slot": snapshot_slot, "slot": landed_slot}
    distance = landed_slot - snapshot_slot
    if distance > jupiter_route_v2.SLOT_LAG_MAX:
        return {"ok": False, "reason": "blockhash_window", "slot_distance": distance, "blockhash_window_slots": jupiter_route_v2.SLOT_LAG_MAX}
    if not isinstance(reserved.get("blockhash"), str) or reserved.get("blockhash") == "":
        return {"ok": False, "reason": "blockhash_unbound"}
    if decoded.blockhash != reserved["blockhash"]:
        return {"ok": False, "reason": "blockhash_mismatch"}
    if route.get("pool") != reserved.get("pool") and reserved.get("pool") not in route.get("keys", []):
        return {"ok": False, "reason": "pool_unbound"}
    dex = reserved.get("dex_fee_lamports")
    if isinstance(dex, bool) or not isinstance(dex, int) or dex < 0:
        return {"ok": False, "reason": "dex_fee_unbound"}
    block_time = reserved.get("block_time")
    fetched = reserved.get("snapshot_fetched_at")
    clock = isinstance(block_time, int) and not isinstance(block_time, bool) and isinstance(fetched, (int, float)) and not isinstance(fetched, bool)
    if clock and fetched > block_time + 5:
        return {"ok": False, "reason": "snapshot_after_landed"}
    return {
        "ok": True,
        "snapshot_slot": snapshot_slot,
        "snapshot_tip_slot": tip,
        "snapshot_not_after_landed": True,
        "slot_distance": distance,
        "blockhash": decoded.blockhash,
        "blockhash_matches_reservation": True,
        "blockhash_rechecked": False,
        "blockhash_window_slots": jupiter_route_v2.SLOT_LAG_MAX,
        "quote_clock_proven": False,
        "pool": reserved.get("pool"),
        "pool_matches_reservation": True,
        "dex_fee_lamports": dex,
        "dex_fee_source": "reserved_pool_state",
        "dex_fee_remeasured": False,
    }


def rent_ledger(decoded, meta, mint: str) -> dict:
    """Separate the wrapped-SOL close from the mint account that an exit leaves open.

    A wrapped-SOL account that is absent before the transaction and absent after
    a close back to the wallet has net rent zero for that account only. The mint
    associated-token account is a different account. A zero written without a
    close, and a preexisting account, are not a refund.
    """
    wallet = phantom_transport.REQUIRED_WALLET
    wsol = solana_tx.associated_token_address(wallet, solana_tx.NATIVE_MINT, solana_tx.TOKEN_PROGRAM)
    canonical = solana_tx.associated_token_address(wallet, mint, solana_tx.TOKEN_PROGRAM)
    route = _route(decoded)
    source = None if route is None else route["keys"][1]
    created, closed = _account_actions(decoded)
    return {
        "wsol": _account_row(decoded, meta, wsol, "wsol", created, closed, wallet),
        "mint_ata": _account_row(decoded, meta, canonical, "mint_ata", created, closed, wallet),
        "position_account": _account_row(decoded, meta, source, "position_account", created, closed, wallet),
        "mint_ata_is_position_account": source == canonical,
    }


def match_refund(recorded: dict, signature: str, pulled: dict) -> dict:
    """A refund is the recorded account reaching zero and that exact lamport count arriving in the wallet."""
    refused = {"ok": False, "holds": True, "resend": False, "refunded": False, "live_ready": False, "send": False}
    if not isinstance(recorded, dict) or recorded.get("preexisting") is True:
        return refused | {"reason": "preexisting_account"}
    rent = recorded.get("rent_lamports")
    account = recorded.get("account")
    if not isinstance(account, str) or isinstance(rent, bool) or not isinstance(rent, int) or rent <= 0:
        return refused | {"reason": "deposit_unobserved"}
    if not isinstance(pulled, dict) or pulled.get("ok") is not True:
        return refused | {"reason": pulled.get("reason") if isinstance(pulled, dict) else "receipt_unknown"}
    try:
        landed = solana_tx.first_signature(pulled["transaction"])
        decoded = solana_tx.decode_transaction(base64.b64decode(pulled["transaction"], validate=True))
    except Exception:
        return refused | {"reason": "receipt_unsigned"}
    if landed != signature:
        return refused | {"reason": "signature_mismatch"}
    created, closed = _account_actions(decoded)
    if closed.get(account) != phantom_transport.REQUIRED_WALLET:
        return refused | {"reason": "refund_not_to_wallet"}
    if account in created:
        return refused | {"reason": "refund_balance"}
    balances = _lamports(decoded, pulled.get("meta"), account)
    wallet = _lamports(decoded, pulled.get("meta"), phantom_transport.REQUIRED_WALLET)
    fee = pulled["meta"].get("fee") if isinstance(pulled.get("meta"), dict) else None
    if balances is None or wallet is None or isinstance(fee, bool) or not isinstance(fee, int) or fee < 0:
        return refused | {"reason": "refund_balance"}
    if balances[0] != rent or balances[1] != 0 or wallet[1] - wallet[0] != rent - fee:
        return refused | {"reason": "refund_balance"}
    meta = pulled["meta"]
    keys = decoded.static_keys
    for index, key in enumerate(keys):
        if key in {account, phantom_transport.REQUIRED_WALLET}:
            continue
        if meta["preBalances"][index] != meta["postBalances"][index]:
            return refused | {"reason": "refund_balance"}
    return {
        "ok": True,
        "reason": None,
        "refunded": True,
        "holds": False,
        "resend": False,
        "account": account,
        "rent_lamports": rent,
        "wallet": phantom_transport.REQUIRED_WALLET,
        "slot": pulled["slot"],
        "signature": signature,
        "send": False,
        "live_ready": False,
    }


def _account_actions(decoded) -> tuple[dict, dict]:
    wallet = phantom_transport.REQUIRED_WALLET
    created = {}
    closed = {}
    for instruction in decoded.instructions:
        program = decoded.static_keys[instruction.program_index]
        keys = [decoded.static_keys[item] for item in instruction.account_indexes]
        data = bytes(instruction.data)
        if program == solana_tx.ASSOCIATED_TOKEN and data == bytes([1]) and len(keys) == 6:
            payer, ata, owner, created_mint, system, token_program = keys
            if payer == wallet and owner == wallet and system == solana_tx.SYSTEM_PROGRAM and token_program == solana_tx.TOKEN_PROGRAM:
                created[ata] = {"funder": payer, "mint": created_mint}
        if program == solana_tx.TOKEN_PROGRAM and data == bytes([9]) and len(keys) == 3:
            account, destination, authority = keys
            if authority == wallet:
                closed[account] = destination
    return created, closed


def _lamports(decoded, meta, account: str):
    if not isinstance(account, str) or account not in decoded.static_keys or not isinstance(meta, dict):
        return None
    pre = meta.get("preBalances")
    post = meta.get("postBalances")
    if not isinstance(pre, list) or not isinstance(post, list) or len(pre) != len(decoded.static_keys) or len(post) != len(pre):
        return None
    index = decoded.static_keys.index(account)
    before, after = pre[index], post[index]
    if isinstance(before, bool) or isinstance(after, bool) or not isinstance(before, int) or not isinstance(after, int):
        return None
    if before < 0 or after < 0:
        return None
    return before, after


def _account_row(decoded, meta, account, kind: str, created: dict, closed: dict, wallet: str) -> dict:
    blank = {
        "account": account,
        "kind": kind,
        "observed": False,
        "pre_lamports": None,
        "post_lamports": None,
        "rent_lamports": None,
        "created_in_message": False,
        "closed_in_message": False,
        "close_destination": None,
        "preexisting": None,
        "bot_funded": None,
        "funder": None,
        "provenance": "unobserved",
        "refund_proven": False,
        "net_proven": False,
        "net_lamports": None,
        "user_account_closed": False,
    }
    if not isinstance(account, str):
        return blank
    balances = _lamports(decoded, meta, account)
    if balances is None:
        return blank
    pre, post = balances
    create = created.get(account)
    destination = closed.get(account)
    closed_here = account in closed
    preexisting = pre > 0 and create is None
    bot_funded = create is not None and pre == 0
    net_proven = kind == "wsol" and pre == 0 and post == 0 and destination == wallet
    if net_proven and create is not None:
        provenance = "created_and_closed_same_transaction"
    elif net_proven:
        provenance = "closed_empty_or_created_inside_transaction"
    elif preexisting:
        provenance = "preexisting"
    elif bot_funded and post > 0:
        provenance = "bot_created_this_transaction"
    elif pre == 0 and post > 0:
        provenance = "funded_funder_unproven"
    elif pre == 0 and post == 0 and not closed_here:
        provenance = "lamports_zero_without_close"
    elif closed_here and post == 0 and pre > 0:
        provenance = "closed_to_wallet" if destination == wallet else "closed_elsewhere"
    else:
        provenance = "unproven"
    rent = post if post > 0 else None
    return {
        "account": account,
        "kind": kind,
        "observed": True,
        "pre_lamports": pre,
        "post_lamports": post,
        "rent_lamports": 0 if net_proven else rent,
        "created_in_message": create is not None,
        "closed_in_message": closed_here,
        "close_destination": destination,
        "preexisting": preexisting,
        "bot_funded": bot_funded,
        "funder": wallet if bot_funded else ("preexisting" if preexisting else None),
        "provenance": provenance,
        "refund_proven": False,
        "net_proven": net_proven,
        "net_lamports": 0 if net_proven else None,
        "user_account_closed": preexisting and closed_here,
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
