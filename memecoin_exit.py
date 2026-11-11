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
    encoded = result.get("transaction")
    slot = result.get("slot")
    if meta.get("err") is not None:
        failed = refused | {
            "reason": "receipt_failed",
            "meta": meta,
            "network_fee_lamports": meta.get("fee") if isinstance(meta.get("fee"), int) and not isinstance(meta.get("fee"), bool) else None,
        }
        if isinstance(encoded, list) and encoded and isinstance(encoded[0], str):
            failed["transaction"] = encoded[0]
        if isinstance(slot, int) and not isinstance(slot, bool):
            failed["slot"] = slot
        return failed
    if not isinstance(encoded, list) or not encoded or not isinstance(encoded[0], str):
        return refused | {"reason": "receipt_unknown"}
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
            "url": __import__("solana_rpc").configured_url(),
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

    The snapshot account slot has to be at or before the landed slot. A landed
    slot more than 150 slots after that snapshot is refused as a conservative
    slot-distance guard. That distance is not a block height, and skipped slots
    mean it is not proof the recent blockhash was still valid. The message hash
    covers the blockhash bytes. This does not call isBlockhashValid or
    getBlockHeight. A missing block time is not a fresh quote. The DEX fee stays
    the figure stored from the snapshot pool state.
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
        return {
            "ok": False,
            "reason": "slot_distance_guard",
            "slot_distance": distance,
            "slot_distance_cap": jupiter_route_v2.SLOT_LAG_MAX,
            "slot_distance_is_blockhash_proof": False,
            "blockhash_expiry_needs_block_height": True,
            "quote_expiry_proven": False,
            "quote_clock_proven": False,
        }
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
        "slot_distance_cap": jupiter_route_v2.SLOT_LAG_MAX,
        "slot_distance_is_blockhash_proof": False,
        "blockhash_expiry_needs_block_height": True,
        "quote_clock_proven": False,
        "quote_expiry_proven": False,
        "pool": reserved.get("pool"),
        "pool_matches_reservation": True,
        "dex_fee_lamports": dex,
        "dex_fee_source": "reserved_pool_state",
        "dex_fee_remeasured": False,
    }


def quote_clock(block_time, block_height, last_valid_block_height) -> dict:
    """Quote expiry. A slot distance is not a block height and is not this check.

    Missing block time stays unproven. A block time without a block height does
    not become a fresh quote. Nothing here invents either figure.
    """
    missing = {"quote_clock_proven": False, "quote_expiry_proven": False, "blockhash_proof": False}
    if isinstance(block_time, bool) or not isinstance(block_time, int):
        return missing | {"reason": "block_time_missing"}
    if isinstance(block_height, bool) or not isinstance(block_height, int) or isinstance(last_valid_block_height, bool) or not isinstance(last_valid_block_height, int):
        return missing | {"reason": "block_height_missing"}
    if block_height > last_valid_block_height:
        return {"quote_clock_proven": True, "quote_expiry_proven": False, "blockhash_proof": False, "reason": "blockhash_expired"}
    return {"quote_clock_proven": True, "quote_expiry_proven": True, "blockhash_proof": False, "reason": None}


def match_entry(reserved: dict, signature: str, pulled: dict) -> dict:
    """A buy receipt is the reserved message, the token increase, and the lamports that actually left the wallet."""
    refused = {"ok": False, "live_ready": False, "send": False, "sign": False, "holds": True, "resend": False, "proceeds": None}
    if not isinstance(reserved, dict) or not isinstance(pulled, dict) or pulled.get("ok") is not True:
        return refused | {"reason": pulled.get("reason") if isinstance(pulled, dict) else "receipt_unknown"}
    try:
        landed = solana_tx.first_signature(pulled["transaction"])
        decoded = solana_tx.decode_transaction(base64.b64decode(pulled["transaction"], validate=True))
        digest = hashlib.sha256(solana_tx.message_bytes(pulled["transaction"])).hexdigest()
    except Exception:
        return refused | {"reason": "receipt_unsigned"}
    if landed != signature:
        return refused | {"reason": "signature_mismatch"}
    if digest != reserved.get("message_sha256"):
        return refused | {"reason": "message_mismatch"}
    if list(decoded.static_keys) != list(reserved.get("account_keys") or []):
        return refused | {"reason": "account_index"}
    route = _route(decoded)
    if route is None or route["destination_mint"] != reserved.get("mint") or route["source_mint"] != solana_tx.NATIVE_MINT:
        return refused | {"reason": "entry_mint"}
    if route["in_amount"] != reserved.get("input_lamports"):
        return refused | {"reason": "entry_amount"}
    destination = route["keys"][2]
    index = reserved.get("destination_index")
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(decoded.static_keys):
        return refused | {"reason": "account_index"}
    if destination != reserved.get("destination_account") or decoded.static_keys[index] != destination:
        return refused | {"reason": "account_index"}
    bound = _provenance(reserved, decoded, pulled["slot"], route)
    if bound.get("ok") is not True:
        return refused | {key: value for key, value in bound.items() if key != "ok"}
    clock = quote_clock(pulled.get("block_time"), None, None)
    rent = rent_ledger(decoded, pulled["meta"], reserved["mint"])
    mint_row = rent.get("mint_ata") or {}
    pre_lamports = mint_row.get("pre_lamports")
    created, _closed = _account_actions(decoded)
    received = _received(pulled["meta"], reserved["mint"], phantom_transport.REQUIRED_WALLET, created.get(destination) is not None, pre_lamports)
    if received.get("ok") is not True:
        return refused | {"reason": received.get("reason")}
    if received["qty"] < int(reserved.get("min_out") or 0):
        return refused | {"reason": "fill_below_min", "qty": received["qty"]}
    fee = pulled["meta"].get("fee")
    if isinstance(fee, bool) or not isinstance(fee, int) or fee < 0:
        return refused | {"reason": "network_fee_missing"}
    wallet = _lamports(decoded, pulled["meta"], phantom_transport.REQUIRED_WALLET)
    if wallet is None or wallet[0] < wallet[1]:
        return refused | {"reason": "cost_missing"}
    spent = wallet[0] - wallet[1]
    conserved = _conserved_deposit(decoded, pulled["meta"], destination, created.get(destination) is not None)
    if conserved.get("ok") is not True:
        return refused | {
            "reason": conserved.get("reason"),
            "accounts": conserved.get("accounts"),
            "network_fee_lamports": fee,
            "wallet_spent_lamports": spent,
        }
    deposit = conserved["deposit_lamports"]
    wsol = rent.get("wsol") or {}
    if wsol.get("net_proven") is not True:
        return refused | {"reason": "rent_unproven", "network_fee_lamports": fee, "wallet_spent_lamports": spent}
    if spent != int(reserved["input_lamports"]) + fee + deposit:
        return refused | {"reason": "cost_unsplit", "spent_lamports": spent, "network_fee_lamports": fee, "deposit_lamports": deposit}
    if any((rent.get(name) or {}).get("user_account_closed") is True for name in ("wsol", "mint_ata", "position_account")):
        return refused | {"reason": "preexisting_close_refused", "network_fee_lamports": fee, "wallet_spent_lamports": spent}
    rent_bound = (
        isinstance(deposit, int) and deposit > 0
        and mint_row.get("created_in_message") is True
        and mint_row.get("preexisting") is not True
        and mint_row.get("pre_lamports") == 0
        and mint_row.get("post_lamports") == deposit
    )
    return {
        "ok": True,
        "reason": None,
        "signature": signature,
        "slot": pulled["slot"],
        "message_sha256": digest,
        "qty": received["qty"],
        "mint_decimals": received.get("decimals"),
        "token_pre": received["pre"],
        "token_pre_source": received["pre_source"],
        "input_lamports": int(reserved["input_lamports"]),
        "cost_basis_lamports": int(reserved["input_lamports"]) + fee,
        "network_fee_lamports": fee,
        "dex_fee_lamports": bound["dex_fee_lamports"],
        "dex_fee_counted_in_input": True,
        "network_fee_added_once": True,
        "deposit_lamports": deposit,
        "wallet_spent_lamports": spent,
        "cost_basis_proven": True,
        "rent_bound_lamports": deposit if rent_bound else None,
        "rent_bound_proven": rent_bound,
        "deposit_refund_proven": False,
        "deposit_exposure_open": deposit > 0,
        "preexisting_close_refused": False,
        "proceeds": None,
        "rent": rent,
        "quote_clock_proven": clock["quote_clock_proven"],
        "quote_expiry_proven": clock["quote_expiry_proven"],
        "quote_clock_reason": clock["reason"],
        "holds": False,
        "resend": False,
        "send": False,
        "sign": False,
        "live_ready": False,
        **{key: bound[key] for key in ("snapshot_slot", "snapshot_not_after_landed", "slot_distance", "slot_distance_cap", "slot_distance_is_blockhash_proof", "blockhash_expiry_needs_block_height", "dex_fee_source", "dex_fee_remeasured", "pool_matches_reservation")},
    }


def _received(meta, mint: str, owner: str, created: bool, pre_lamports) -> dict:
    """Token increase. An explicit zero is zero. A missing row is zero only when the account was created from nothing."""
    post_rows = [row for row in _rows(meta.get("postTokenBalances")) if isinstance(row, dict) and row.get("mint") == mint and row.get("owner") == owner]
    pre_rows = [row for row in _rows(meta.get("preTokenBalances")) if isinstance(row, dict) and row.get("mint") == mint and row.get("owner") == owner]
    post = _amount(post_rows, mint, owner)
    if post is None or len(post_rows) != 1:
        return {"ok": False, "reason": "token_delta_missing"}
    if len(pre_rows) > 1:
        return {"ok": False, "reason": "token_prebalance_unknown"}
    if len(pre_rows) == 1:
        pre = _amount(pre_rows, mint, owner)
        if pre is None:
            return {"ok": False, "reason": "token_prebalance_unknown"}
        source = "explicit_zero" if pre == 0 else "explicit"
    elif created is True and pre_lamports == 0:
        pre = 0
        source = "account_absent"
    else:
        return {"ok": False, "reason": "token_prebalance_unknown"}
    if post < pre:
        return {"ok": False, "reason": "token_delta"}
    decimals = _decimals(post_rows + pre_rows)
    return {"ok": True, "qty": post - pre, "pre": pre, "pre_source": source, "decimals": decimals}


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


def match_failed_refund(recorded: dict, signature: str, pulled: dict) -> dict:
    """A reverted close can charge a fee. Attribute that fee only when the message is this account's close."""
    silent = {"attributable": False, "refunded": False, "holds": True, "resend": False, "send": False, "live_ready": False}
    if not isinstance(pulled, dict):
        return silent | {"reason": "receipt_unknown"}
    reason = pulled.get("reason")
    if reason != "receipt_failed":
        return silent | {"reason": reason or "receipt_unknown"}
    transaction = pulled.get("transaction")
    meta = pulled.get("meta") if isinstance(pulled.get("meta"), dict) else None
    if not isinstance(transaction, str) or meta is None:
        return silent | {"reason": "receipt_unknown"}
    if not isinstance(recorded, dict) or recorded.get("preexisting") is True:
        return silent | {"reason": "preexisting_account" if isinstance(recorded, dict) and recorded.get("preexisting") is True else "deposit_unobserved"}
    account = recorded.get("account")
    rent = recorded.get("rent_lamports")
    if not isinstance(account, str) or isinstance(rent, bool) or not isinstance(rent, int) or rent <= 0:
        return silent | {"reason": "deposit_unobserved"}
    try:
        landed = solana_tx.first_signature(transaction)
        decoded = solana_tx.decode_transaction(base64.b64decode(transaction, validate=True))
    except Exception:
        return silent | {"reason": "receipt_unsigned"}
    if landed != signature:
        return silent | {"reason": "signature_mismatch"}
    _created, closed = _account_actions(decoded)
    if closed.get(account) != phantom_transport.REQUIRED_WALLET:
        return silent | {"reason": "refund_not_to_wallet"}
    fee = meta.get("fee")
    if isinstance(fee, bool) or not isinstance(fee, int) or fee < 0:
        return silent | {"reason": "network_fee_missing"}
    deltas = _account_deltas(decoded, meta)
    ordinary = True
    if deltas is None:
        ordinary = False
    else:
        for item in deltas:
            if item["account"] == phantom_transport.REQUIRED_WALLET:
                if item["delta"] != -fee:
                    ordinary = False
            elif item["account"] == account:
                if item["pre"] != rent or item["post"] != rent:
                    ordinary = False
            elif item["delta"] != 0:
                ordinary = False
    return {
        "attributable": True,
        "ok": False,
        "ordinary": ordinary,
        "anomaly": not ordinary,
        "refunded": False,
        "holds": True,
        "resend": False,
        "reason": "receipt_failed",
        "account": account,
        "rent_lamports": rent,
        "network_fee_lamports": fee,
        "wallet": phantom_transport.REQUIRED_WALLET,
        "slot": pulled.get("slot") if isinstance(pulled.get("slot"), int) else 0,
        "signature": signature,
        "send": False,
        "live_ready": False,
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
        "network_fee_lamports": fee,
        "wallet": phantom_transport.REQUIRED_WALLET,
        "slot": pulled["slot"],
        "signature": signature,
        "send": False,
        "live_ready": False,
    }


def _conserved_deposit(decoded, meta: dict, mint_ata: str, created: bool) -> dict:
    """Every native delta has to be the fee, the swap input, or the mint account this buy funds.

    An unchanged preexisting mint account is not our deposit. A lamport change on
    that account, or on any other account, is unexplained and is not a fill.
    """
    deltas = _account_deltas(decoded, meta)
    if deltas is None or not isinstance(mint_ata, str):
        return {"ok": False, "reason": "cost_missing"}
    wallet = phantom_transport.REQUIRED_WALLET
    wsol = solana_tx.associated_token_address(wallet, solana_tx.NATIVE_MINT, solana_tx.TOKEN_PROGRAM)
    unexplained = []
    mint_delta = None
    for item in deltas:
        if item["account"] == wallet:
            continue
        if item["account"] == wsol:
            if item["delta"] != 0:
                unexplained.append(item)
            continue
        if item["account"] == mint_ata:
            mint_delta = item
            continue
        if item["delta"] != 0:
            unexplained.append(item)
    if mint_delta is None:
        return {"ok": False, "reason": "deposit_unobserved", "accounts": unexplained}
    if created and mint_delta["pre"] == 0:
        deposit = mint_delta["post"]
    elif mint_delta["delta"] == 0:
        deposit = 0
    else:
        unexplained.append(mint_delta)
        deposit = None
    if unexplained:
        return {"ok": False, "reason": "unexplained_lamports", "accounts": unexplained}
    return {"ok": True, "deposit_lamports": deposit, "mint_pre": mint_delta["pre"], "mint_post": mint_delta["post"]}


def _account_deltas(decoded, meta: dict):
    pre = meta.get("preBalances") if isinstance(meta, dict) else None
    post = meta.get("postBalances") if isinstance(meta, dict) else None
    keys = decoded.static_keys
    if not isinstance(pre, list) or not isinstance(post, list) or len(pre) != len(keys) or len(post) != len(keys):
        return None
    rows = []
    for index, key in enumerate(keys):
        before, after = pre[index], post[index]
        if isinstance(before, bool) or isinstance(after, bool) or not isinstance(before, int) or not isinstance(after, int):
            return None
        if before < 0 or after < 0:
            return None
        rows.append({"index": index, "account": key, "pre": before, "post": after, "delta": after - before})
    return rows


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


def _decimals(rows) -> int | None:
    found = []
    for row in rows:
        token = row.get("uiTokenAmount") if isinstance(row, dict) else None
        decimals = token.get("decimals") if isinstance(token, dict) else None
        if isinstance(decimals, bool) or not isinstance(decimals, int) or decimals < 0 or decimals > 18:
            return None
        found.append(decimals)
    if len(found) != 1 and len(set(found)) != 1:
        return None
    return found[0] if found else None


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
