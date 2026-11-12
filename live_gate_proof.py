"""Per-mint proofs taken from a response this process just read.

Nothing here accepts a caller-supplied body. The fetch that opened the
response is the only caller, and a proof for one mint does not cover another.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import math
import os
import socket
import ssl
import sys
import threading
import time
import types
import urllib.error
import urllib.request

import certifi
from urllib.parse import parse_qs, urlsplit

import httpx
from httpcore._backends.sync import SyncStream as _SYNC_STREAM

FRESH_S = 30
QUOTE_MAX_AGE_S = 10
WSOL = "So11111111111111111111111111111111111111112"


def quote_max_age() -> float:
    """Seconds a Jupiter quote may be used. Invalid configuration stays at 10."""
    raw = os.environ.get("DESK_QUOTE_MAX_AGE_S")
    if raw is None or str(raw).strip() == "":
        return float(QUOTE_MAX_AGE_S)
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return float(QUOTE_MAX_AGE_S)
    if value != value or value <= 0 or value > FRESH_S:
        return float(QUOTE_MAX_AGE_S)
    return value


def _sim_within_caps(transaction_b64: str, value: dict, now, notes, fresh_row) -> bool:
    """Fee and output from the transaction that was simulated. No URL, no key."""
    import base64
    try:
        import jupiter_route_v2
        import memecoin_desk
        import preflight_sim
        import pyth_sponsored
        import solana_jupiter
        import solana_tx
        decoded = solana_tx.decode_transaction(base64.b64decode(transaction_b64, validate=True))
    except Exception:
        return False
    route = None
    compute = []
    for item in decoded.instructions:
        program = decoded.static_keys[item.program_index]
        data = bytes(item.data)
        if program == solana_tx.COMPUTE_BUDGET:
            compute.append(data)
        elif program == solana_jupiter.PROGRAM_ID and route is None:
            route = data
    parsed = jupiter_route_v2.parse(route or b"")
    quoted = parsed.get("quoted_out_amount") if parsed.get("ok") is True else None
    if type(quoted) is not int or isinstance(quoted, bool) or quoted <= 0:
        return False
    priced = jupiter_route_v2.network_fee(compute, decoded.required_signatures)
    cu_price = priced.get("requested_micro_lamports_per_cu") if priced.get("ok") is True else 0
    cu_limit = priced.get("requested_compute_unit_limit") if priced.get("ok") is True else None
    if type(cu_price) is not int:
        return False
    if type(cu_limit) is not int:
        cu_limit = None
    sol = None
    for key, row in notes.items():
        if type(key) is not tuple or len(key) != 2 or key[0] != "price_freshness_proven":
            continue
        if not fresh_row(row, now):
            continue
        body = row.get("body")
        if not isinstance(body, dict):
            continue
        sol = {"fresh": True, "used": True, "price": body.get("price"), "conf": body.get("conf"), "expo": body.get("expo")}
        break
    if sol is None:
        return False
    output_mint = _traded_output_mint(decoded)
    if output_mint is None:
        return False
    delta = _one_owner_mint_delta(value, decoded.fee_payer, output_mint)
    if delta is None:
        return False
    minimum = jupiter_route_v2.minimum_out(quoted, 50)
    if minimum is None or delta < minimum:
        return False
    fee = preflight_sim._simulated_fee(value, cu_price, cu_limit)
    if fee is None:
        return False
    network = pyth_sponsored.lamports_usd(fee, sol)
    if network is None or network > memecoin_desk.MAX_FEE:
        return False
    return True


def _traded_output_mint(decoded) -> str | None:
    """Buy output is the non-wSOL destination. Sell output is wSOL, not the fee payer's lamports."""
    import solana_jupiter
    import solana_tx
    found = None
    for item in decoded.instructions:
        program = decoded.static_keys[item.program_index]
        if program != solana_jupiter.PROGRAM_ID:
            continue
        indexes = list(item.account_indexes)
        if len(indexes) < 5:
            return None
        try:
            source = decoded.static_keys[indexes[3]]
            dest = decoded.static_keys[indexes[4]]
        except (IndexError, TypeError):
            return None
        if found is not None:
            return None
        found = (source, dest)
    if found is None:
        return None
    source, dest = found
    if source == solana_tx.NATIVE_MINT and dest != solana_tx.NATIVE_MINT:
        return dest
    if dest == solana_tx.NATIVE_MINT and source != solana_tx.NATIVE_MINT:
        return dest
    return None


def _token_amount(row: dict) -> tuple[int, int] | None:
    ui = row.get("uiTokenAmount")
    if not isinstance(ui, dict):
        return None
    amount = ui.get("amount")
    decimals = ui.get("decimals")
    if type(amount) is not str or not amount.isdigit():
        return None
    if type(decimals) is not int or isinstance(decimals, bool) or decimals < 0:
        return None
    index = row.get("accountIndex")
    if type(index) is not int or isinstance(index, bool) or index < 0:
        return None
    return index, int(amount)


def _one_owner_mint_delta(value: dict, owner: str, mint: str) -> int | None:
    """Exactly one pre row and one post row, same account, this owner and mint."""
    pre_rows = value.get("preTokenBalances")
    post_rows = value.get("postTokenBalances")
    if not isinstance(pre_rows, list) or not isinstance(post_rows, list):
        return None

    def one(rows):
        match = None
        for row in rows:
            if not isinstance(row, dict):
                return None
            if row.get("owner") != owner or row.get("mint") != mint:
                continue
            parsed = _token_amount(row)
            if parsed is None:
                return None
            if match is not None:
                return None
            match = parsed
        return match

    pre = one(pre_rows)
    post = one(post_rows)
    if pre is None or post is None or pre[0] != post[0]:
        return None
    return post[1] - pre[1]


def _trade_legs(decoded):
    """Source mint and destination mint on the single Jupiter hop."""
    import solana_jupiter
    found = None
    for item in decoded.instructions:
        program = decoded.static_keys[item.program_index]
        if program != solana_jupiter.PROGRAM_ID:
            continue
        indexes = list(item.account_indexes)
        if len(indexes) < 5:
            return None
        try:
            source = decoded.static_keys[indexes[3]]
            dest = decoded.static_keys[indexes[4]]
        except (IndexError, TypeError):
            return None
        if found is not None:
            return None
        found = (source, dest)
    return found


def _route_amounts(decoded) -> tuple[int | None, int | None]:
    import solana_jupiter
    import jupiter_route_v2
    route = None
    for item in decoded.instructions:
        program = decoded.static_keys[item.program_index]
        if program == solana_jupiter.PROGRAM_ID and route is None:
            route = bytes(item.data)
    parsed = jupiter_route_v2.parse(route or b"")
    if parsed.get("ok") is not True:
        return None, None
    incoming = parsed.get("in_amount")
    quoted = parsed.get("quoted_out_amount")
    if type(incoming) is not int or isinstance(incoming, bool) or incoming <= 0:
        incoming = None
    if type(quoted) is not int or isinstance(quoted, bool) or quoted <= 0:
        quoted = None
    return incoming, quoted


def _sold_amount_matches(input_delta, in_amount) -> bool:
    """A sell spends about the quoted input of the position mint."""
    if type(input_delta) is not int or isinstance(input_delta, bool) or input_delta >= 0:
        return False
    if type(in_amount) is not int or isinstance(in_amount, bool) or in_amount <= 0:
        return False
    dropped = -input_delta
    slack = max(1, in_amount // 100)
    return abs(dropped - in_amount) <= slack


def _loaded_lists(meta, decoded):
    """Writable then readonly addresses the signed message is allowed to load.

    A legacy message, or a v0 message with no lookup tables, rejects any loaded
    address. A v0 message loads only as many addresses as its tables declare.
    """
    loaded = meta.get("loadedAddresses") if type(meta) is dict else None
    if loaded is None:
        writable, readonly = [], []
    elif type(loaded) is not dict:
        return None
    else:
        writable = loaded.get("writable") if "writable" in loaded else []
        readonly = loaded.get("readonly") if "readonly" in loaded else []
        if type(writable) is not list or type(readonly) is not list:
            return None
        if any(type(item) is not str or item == "" for item in writable + readonly):
            return None
    declared_w = sum(len(item.writable) for item in decoded.lookups)
    declared_r = sum(len(item.readonly) for item in decoded.lookups)
    if decoded.version != "v0" or not decoded.lookups:
        if writable or readonly:
            return None
        return [], []
    if len(writable) != declared_w or len(readonly) != declared_r:
        return None
    return writable, readonly


def _keys_for(decoded, meta):
    """Static keys from the signed message, then loaded writable, then readonly."""
    lists = _loaded_lists(meta, decoded)
    if lists is None:
        return None
    writable, readonly = lists
    non_signers = len(decoded.static_keys) - decoded.required_signatures
    if (
        decoded.required_signatures < 1
        or non_signers < 0
        or decoded.readonly_signed > decoded.required_signatures
        or decoded.readonly_unsigned > non_signers
    ):
        return None
    return list(decoded.static_keys) + list(writable) + list(readonly), len(writable)


def _is_writable(decoded, index: int, n_static: int, n_loaded_writable: int) -> bool:
    if type(index) is not int or isinstance(index, bool) or index < 0:
        return False
    if index < n_static:
        if index < decoded.required_signatures:
            return index < decoded.required_signatures - decoded.readonly_signed
        return index < n_static - decoded.readonly_unsigned
    return index < n_static + n_loaded_writable


def _ata_addresses(owner: str, mint: str) -> set[str]:
    import solana_tx
    found = set()
    for program in (solana_tx.TOKEN_PROGRAM, solana_tx.TOKEN_2022):
        try:
            found.add(solana_tx.associated_token_address(owner, mint, program))
        except Exception:
            continue
    return found


def _resolved_delta(meta, decoded, keys, n_loaded_writable: int, owner: str, mint: str) -> int | None:
    """One pre row and one post row for this owner and mint, on the same writable ATA.

    A missing pre row is zero only when this transaction creates that ATA and the
    account did not exist before. Any other gap is not a delta.
    """
    atas = _ata_addresses(owner, mint)
    if not atas:
        return None
    pre_rows = meta.get("preTokenBalances")
    post_rows = meta.get("postTokenBalances")
    if not isinstance(pre_rows, list) or not isinstance(post_rows, list):
        return None
    n_static = len(decoded.static_keys)

    def one(rows):
        match = None
        saw = False
        for row in rows:
            if not isinstance(row, dict):
                return None
            if row.get("owner") != owner or row.get("mint") != mint:
                continue
            saw = True
            parsed = _token_amount(row)
            if parsed is None:
                return None
            index, amount = parsed
            if index >= len(keys) or not _is_writable(decoded, index, n_static, n_loaded_writable):
                return None
            address = keys[index]
            if address not in atas:
                return None
            if match is not None:
                return None
            match = (address, index, amount)
        if saw and match is None:
            return None
        return match

    pre = one(pre_rows)
    post = one(post_rows)
    if post is None:
        return None
    if pre is None:
        if _owner_mint_rows(meta, owner, mint) and not _post_only(meta, owner, mint):
            return None
        if not _post_only(meta, owner, mint):
            return None
        if not _created_empty(meta, decoded, keys, post[0], post[1], owner, mint):
            return None
        return post[2]
    if pre[0] != post[0]:
        return None
    return post[2] - pre[2]


def _post_only(meta, owner: str, mint: str) -> bool:
    """A post row for this owner and mint, and no pre row."""
    pre = False
    post = False
    for name, flag in (("preTokenBalances", "pre"), ("postTokenBalances", "post")):
        rows = meta.get(name)
        if not isinstance(rows, list):
            return False
        for row in rows:
            if isinstance(row, dict) and row.get("owner") == owner and row.get("mint") == mint:
                if flag == "pre":
                    pre = True
                else:
                    post = True
    return post and not pre


def _created_empty(meta, decoded, keys, address: str, index: int, owner: str, mint: str) -> bool:
    """The signed message creates this ATA, and it held no lamports before."""
    if keys[0] != decoded.fee_payer or decoded.static_keys[0] != decoded.fee_payer:
        return False
    if not _message_creates_ata(decoded, address, owner, mint):
        return False
    balances = _lamport_table(meta, len(keys))
    if balances is None:
        return False
    pre, _post = balances
    return pre[index] == 0


def _message_creates_ata(decoded, address: str, owner: str, mint: str) -> bool:
    """One create or createIdempotent for this ATA, owner, mint, and token program."""
    import solana_tx
    found = False
    for instruction in decoded.instructions:
        try:
            program = decoded.static_keys[instruction.program_index]
            accounts = [decoded.static_keys[item] for item in instruction.account_indexes]
        except (IndexError, TypeError):
            return False
        data = bytes(instruction.data)
        if program != solana_tx.ASSOCIATED_TOKEN or data not in {bytes([0]), bytes([1])} or len(accounts) != 6:
            continue
        payer, ata, row_owner, row_mint, system, token_program = accounts
        if system != solana_tx.SYSTEM_PROGRAM or token_program not in {solana_tx.TOKEN_PROGRAM, solana_tx.TOKEN_2022}:
            continue
        if payer != owner or row_owner != owner or row_mint != mint or ata != address:
            continue
        try:
            derived = solana_tx.associated_token_address(owner, mint, token_program)
        except Exception:
            return False
        if derived != address:
            continue
        if found:
            return False
        found = True
    return found


def _lamport_table(meta, nkeys: int):
    pre = meta.get("preBalances")
    post = meta.get("postBalances")
    if not isinstance(pre, list) or not isinstance(post, list) or len(pre) != nkeys or len(post) != nkeys:
        return None
    before, after = [], []
    for left, right in zip(pre, post):
        if type(left) is not int or type(right) is not int or isinstance(left, bool) or isinstance(right, bool):
            return None
        if left < 0 or right < 0:
            return None
        before.append(left)
        after.append(right)
    return before, after


def _native_sol_output(meta, decoded, keys) -> int | None:
    parts = _lamport_parts(meta, decoded, keys)
    if parts is None:
        return None
    return parts["output"]


def _lamport_parts(meta, decoded, keys):
    """SOL unwrapped to the fee payer. None when the lamports are not attributable.

    output = (post - pre) + fee - rent refunded by closing accounts that already
    existed + rent left in accounts this transaction created and did not close.
    A wSOL account created and closed here contributes no rent.
    """
    import solana_tx
    if not keys or keys[0] != decoded.fee_payer or decoded.static_keys[0] != decoded.fee_payer:
        return None
    balances = _lamport_table(meta, len(keys))
    if balances is None:
        return None
    pre, post = balances
    fee = meta.get("fee")
    if type(fee) is not int or isinstance(fee, bool) or fee < 0:
        return None
    try:
        wsol = solana_tx.associated_token_address(decoded.fee_payer, solana_tx.NATIVE_MINT, solana_tx.TOKEN_PROGRAM)
    except Exception:
        return None
    creates = _ata_touches(decoded, decoded.fee_payer, close=False)
    closes = _ata_touches(decoded, decoded.fee_payer, close=True)
    if creates is None or closes is None:
        return None
    attributed = set()
    refund = 0
    opened = 0
    for address in set(creates) | set(closes):
        if keys.count(address) != 1:
            return None
        index = keys.index(address)
        before, after = pre[index], post[index]
        if address in creates and address in closes:
            if before != 0 or after != 0:
                return None
            attributed.add(index)
            continue
        if address in closes:
            if after != 0:
                return None
            if before == 0:
                attributed.add(index)
                continue
            if address == wsol and _token_amount_of(meta, "preTokenBalances", decoded.fee_payer, solana_tx.NATIVE_MINT) not in {None, 0}:
                return None
            refund += before
            attributed.add(index)
            continue
        if before != 0 or after <= 0 or address == wsol:
            return None
        opened += after
        attributed.add(index)
    for index, (before, after) in enumerate(zip(pre, post)):
        if index != 0 and index not in attributed and before != after:
            return None
    output = (post[0] - pre[0]) + fee - refund + opened
    if output < 0:
        return None
    return {"output": output, "refund": refund, "opened": opened}


def _ata_touches(decoded, owner: str, close: bool):
    """ATA addresses this message creates, or closes back to the owner. None is ambiguous."""
    import solana_tx
    found = []
    for instruction in decoded.instructions:
        try:
            program = decoded.static_keys[instruction.program_index]
            accounts = [decoded.static_keys[item] for item in instruction.account_indexes]
        except (IndexError, TypeError):
            return None
        data = bytes(instruction.data)
        if not close and program == solana_tx.ASSOCIATED_TOKEN and data in {bytes([0]), bytes([1])} and len(accounts) == 6:
            payer, ata, row_owner, _mint, system, token_program = accounts
            if system != solana_tx.SYSTEM_PROGRAM or token_program not in {solana_tx.TOKEN_PROGRAM, solana_tx.TOKEN_2022}:
                return None
            if payer != owner or row_owner != owner:
                return None
            try:
                derived = solana_tx.associated_token_address(owner, _mint, token_program)
            except Exception:
                return None
            if ata != derived:
                return None
            found.append(ata)
        elif close and program == solana_tx.TOKEN_PROGRAM and data == bytes([9]) and len(accounts) == 3:
            account, dest, row_owner = accounts
            if dest != owner or row_owner != owner:
                return None
            found.append(account)
    if len(found) != len(set(found)):
        return None
    return found


def _token_amount_of(meta, name: str, owner: str, mint: str):
    rows = meta.get(name)
    if not isinstance(rows, list):
        return None
    found = None
    for row in rows:
        if not isinstance(row, dict) or row.get("owner") != owner or row.get("mint") != mint:
            continue
        parsed = _token_amount(row)
        if parsed is None:
            return None
        if found is not None:
            return None
        found = parsed[1]
    return found


def _open_rent(meta, decoded, keys) -> int:
    """Lamports left in ATAs this message created and did not close. Not a trade loss."""
    if not keys:
        return 0
    balances = _lamport_table(meta, len(keys))
    creates = _ata_touches(decoded, decoded.fee_payer, close=False)
    closes = _ata_touches(decoded, decoded.fee_payer, close=True)
    if balances is None or creates is None or closes is None:
        return 0
    pre, post = balances
    total = 0
    for address in creates:
        if address in closes or keys.count(address) != 1:
            continue
        index = keys.index(address)
        if pre[index] == 0 and post[index] > 0:
            total += post[index]
    return total


def _fill_summary(result) -> dict | None:
    """Owner+mint deltas for the hop. An index outside the signed keys is not a fill."""
    if type(result) is not dict:
        return None
    meta = result.get("meta")
    if type(meta) is not dict or meta.get("err") is not None:
        return None
    try:
        import base64
        import solana_tx
        encoded = _transaction_b64(result)
        if encoded is None:
            return None
        decoded = solana_tx.decode_transaction(base64.b64decode(encoded, validate=True))
    except Exception:
        return None
    legs = _trade_legs(decoded)
    if legs is None:
        return None
    source, dest = legs
    fee = meta.get("fee")
    if type(fee) is not int or isinstance(fee, bool) or fee < 0:
        fee = None
    keyed = _keys_for(decoded, meta)
    accounts_ok = keyed is not None
    output_delta = None
    input_delta = None
    rent_lamports = 0
    rent_released = 0
    if keyed is not None:
        keys, n_loaded_writable = keyed
        output_delta = _resolved_delta(meta, decoded, keys, n_loaded_writable, decoded.fee_payer, dest)
        input_delta = _resolved_delta(meta, decoded, keys, n_loaded_writable, decoded.fee_payer, source)
        parts = _lamport_parts(meta, decoded, keys)
        if parts is None:
            rent_lamports = _open_rent(meta, decoded, keys)
            rent_released = 0
        else:
            rent_lamports = parts["opened"]
            rent_released = parts["refund"]
        if dest == solana_tx.NATIVE_MINT and output_delta is None and not _bad_token_rows(meta, decoded, keys, n_loaded_writable, decoded.fee_payer, dest):
            native = None if parts is None else parts["output"]
            if native is None:
                accounts_ok = False
            else:
                output_delta = native
        elif _owner_mint_rows(meta, decoded.fee_payer, dest) and output_delta is None:
            accounts_ok = False
        if dest == solana_tx.NATIVE_MINT and _owner_mint_rows(meta, decoded.fee_payer, source) and input_delta is None:
            accounts_ok = False
    in_amount, quoted = _route_amounts(decoded)
    positive = accounts_ok and type(output_delta) is int and not isinstance(output_delta, bool) and output_delta > 0
    if positive and dest == solana_tx.NATIVE_MINT:
        positive = type(input_delta) is int and not isinstance(input_delta, bool) and _sold_amount_matches(input_delta, in_amount)
    return {
        "mint": dest,
        "input_mint": source,
        "output_delta": output_delta,
        "input_delta": input_delta,
        "in_amount": in_amount,
        "quoted_out": quoted,
        "fee_lamports": fee,
        "rent_lamports": rent_lamports,
        "rent_released_lamports": rent_released,
        "accounts_ok": accounts_ok,
        "positive": positive is True,
    }


def _bad_token_rows(meta, decoded, keys, n_loaded_writable: int, owner: str, mint: str) -> bool:
    """A present owner+mint row that does not resolve. Absence is not this failure."""
    if not _owner_mint_rows(meta, owner, mint):
        return False
    return _resolved_delta(meta, decoded, keys, n_loaded_writable, owner, mint) is None


def _owner_mint_rows(meta, owner: str, mint: str) -> bool:
    for name in ("preTokenBalances", "postTokenBalances"):
        rows = meta.get(name)
        if not isinstance(rows, list):
            return False
        for row in rows:
            if isinstance(row, dict) and row.get("owner") == owner and row.get("mint") == mint:
                return True
    return False


def _result_signature(result) -> str | None:
    """signatures[0] of a getTransaction body. Bytes and a listed signature must agree."""
    transaction = result.get("transaction") if type(result) is dict else None
    listed = None
    if type(transaction) is dict:
        sigs = transaction.get("signatures")
        if type(sigs) is list and sigs and type(sigs[0]) is str and sigs[0] != "":
            listed = sigs[0]
    encoded = _transaction_b64(result)
    embedded = None
    if encoded is not None:
        try:
            import solana_tx
            embedded = solana_tx.first_signature(encoded)
        except Exception:
            embedded = None
    if listed is not None and embedded is not None and listed != embedded:
        return None
    return embedded if embedded is not None else listed


def _tx_bound(result, signature: str, reference_b64: str) -> bool:
    """The returned transaction is the one we signed, not another signature's fill."""
    if type(result) is not dict or type(signature) is not str or signature == "" or type(reference_b64) is not str or reference_b64 == "":
        return False
    encoded = _transaction_b64(result)
    if encoded is None or _result_signature(result) != signature:
        return False
    try:
        import base64
        import solana_tx
        if solana_tx.message_bytes(encoded) != solana_tx.message_bytes(reference_b64):
            return False
        returned = solana_tx.decode_transaction(base64.b64decode(encoded, validate=True))
        reference = solana_tx.decode_transaction(base64.b64decode(reference_b64, validate=True))
    except Exception:
        return False
    if returned.fee_payer != reference.fee_payer:
        return False
    return list(returned.static_keys) == list(reference.static_keys)


def _sent_binding(payload: str) -> dict | None:
    """Message hash, fee payer, and account keys of the transaction we asked Helius to send."""
    if type(payload) is not str or payload == "":
        return None
    try:
        import base64
        import hashlib
        import solana_tx
        decoded = solana_tx.decode_transaction(base64.b64decode(payload, validate=True))
        local = solana_tx.first_signature(payload)
        digest = hashlib.sha256(solana_tx.message_bytes(payload)).hexdigest()
    except Exception:
        return None
    if local == "" or decoded.fee_payer == "":
        return None
    return {
        "signature": local,
        "message_sha256": digest,
        "fee_payer": decoded.fee_payer,
        "keys": list(decoded.static_keys),
    }


def _bound_to_note(result, signature: str, sent_body) -> bool:
    """A getTransaction read matches the send this process recorded for that signature."""
    if not isinstance(sent_body, dict) or _result_signature(result) != signature:
        return False
    encoded = _transaction_b64(result)
    if encoded is None:
        return False
    try:
        import base64
        import hashlib
        import solana_tx
        digest = hashlib.sha256(solana_tx.message_bytes(encoded)).hexdigest()
        decoded = solana_tx.decode_transaction(base64.b64decode(encoded, validate=True))
    except Exception:
        return False
    keys = sent_body.get("keys")
    return (
        digest == sent_body.get("message_sha256")
        and decoded.fee_payer == sent_body.get("fee_payer")
        and type(keys) is list
        and list(decoded.static_keys) == keys
    )


def _fill_matches(result) -> bool:
    """A buy gained the traded mint. A sell gained wSOL and spent about the quoted input."""
    summary = _fill_summary(result)
    return isinstance(summary, dict) and summary.get("positive") is True


def _transaction_b64(result) -> str | None:
    transaction = result.get("transaction") if type(result) is dict else None
    if type(transaction) is str and transaction != "":
        return transaction
    if type(transaction) is list and transaction and type(transaction[0]) is str and transaction[0] != "":
        return transaction[0]
    if type(transaction) is not dict:
        return None
    message = transaction.get("message")
    if type(message) is str and message != "":
        return message
    return None


def quote_age_ok(row, now, limit) -> bool:
    """The owned-read timestamp, not a caller clock planted in the body."""
    if not isinstance(row, dict) or row.get("ok") is not True:
        return False
    stamp = row.get("at")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return False
    if isinstance(now, bool) or not isinstance(now, (int, float)):
        return False
    if isinstance(limit, bool) or not isinstance(limit, (int, float)) or limit <= 0:
        return False
    age = now - stamp
    if not (0 <= age <= limit):
        return False
    until = row.get("until")
    if until is not None and (isinstance(until, bool) or not isinstance(until, (int, float)) or until <= now):
        return False
    return True


_LOCK = threading.Lock()
_NOTES: dict = {}


def _from_fetch(response, url: str, parsed, mint: str | None = None) -> None:
    """A response object is not a read. Only this module's handshake records a proof."""
    return None


def _from_history(response, subject: str, kind: str, parsed, past_mint: str | None = None) -> None:
    """A response object is not a read. Only this module's handshake records a proof."""
    return None


def _seal_history(subject, past, clean) -> None:
    """History is closed by the fetch that read it. This name does not write."""
    return None


def _apply(url, body, mint=None, now=None) -> None:
    """A URL and a body are not a read. This name does not write."""
    return None


def _record(url, raw, mint=None, history=None) -> None:
    """Raw bytes are not a read. This name does not write."""
    return None


def _plan(url, body, mint, now, history, existing, lp_ok, mint_at, plan_dev, plan_history, parse_holder, bundle_check, assess_price, split, parse_query) -> list:
    """Describe note updates. This does not write them."""
    if type(url) is not str or type(body) is not dict or isinstance(now, bool) or not isinstance(now, (int, float)):
        return []
    updates = []

    def put(key, ok, until=None, stored=None):
        row = {"at": now, "ok": ok is True, "until": until}
        if stored is not None:
            row["body"] = stored
        updates.append((key, row))

    def view():
        data = dict(existing)
        for key, row in updates:
            data[key] = row
        return data

    if url.startswith("https://api.rugcheck.xyz/v1/tokens/") and url.endswith("/report/summary"):
        token = mint_at(url, "/tokens/", "/report/summary")
        if isinstance(token, str):
            put(("lp_lock_live", token), lp_ok(body), None)
    elif url.startswith("https://api.rugcheck.xyz/v1/tokens/") and url.endswith("/report"):
        token = mint_at(url, "/tokens/", "/report")
        if isinstance(token, str) and "creatorBalance" in body:
            put(("rug_body", token), True, None, body)
            plan_dev(token, now, view, put)
    elif url.startswith("https://data.solanatracker.io/holders/chart/"):
        token = url.split("/holders/chart/", 1)[1].split("?", 1)[0].strip("/")
        if token != "" and "/" not in token:
            parsed = parse_holder(body, token)
            grew = parsed.get("ok") is True and type(parsed.get("delta")) is int and parsed["delta"] > 0
            put(("holder_growth_live", token), grew, None)
    elif url.startswith("https://data.solanatracker.io/tokens/"):
        token = url.split("/tokens/", 1)[1].split("?", 1)[0].strip("/")
        if token != "" and "/" not in token:
            checked = bundle_check(body, None)
            put(("bundle", token), checked.get("ok") is True, None)
            put(("tracker_body", token), True, None, body)
            plan_dev(token, now, view, put)
    elif url.startswith("https://hermes.pyth.network/") and isinstance(mint, str) and mint != "":
        try:
            priced = assess_price(body, now)
        except (OverflowError, ValueError, ArithmeticError):
            priced = {"ok": False}
        stored = None
        if priced.get("ok") is True:
            stored = {"price": priced.get("price"), "conf": priced.get("conf"), "expo": priced.get("expo")}
        put(("price_freshness_proven", mint), priced.get("ok") is True, None, stored)
    elif url.startswith("https://api.jup.ag/swap/v2/order"):
        query = parse_query(split(url).query)
        inputs = query.get("inputMint") or []
        outputs = query.get("outputMint") or []
        if len(inputs) == 1 and len(outputs) == 1:
            left, right = inputs[0], outputs[0]
            if isinstance(left, str) and isinstance(right, str) and left != "" and right != "" and body.get("inputMint", left) == left and body.get("outputMint", right) == right:
                expire = body.get("expireAt")
                if type(expire) is int and expire > now:
                    put(("quote_expiry", left, right), True, expire)
                else:
                    put(("quote_expiry", left, right), False, None)
    if type(history) is tuple and len(history) == 3:
        plan_history(history, body, now, view, put)
    return updates


def _plan_dev(token, now, view, put, fresh, dev_check, bundle_check) -> None:
    state = view()
    tracker = state.get(("tracker_body", token))
    rug = state.get(("rug_body", token))
    if not fresh(tracker, now) or not fresh(rug, now):
        return
    dev = dev_check(rug.get("body"), tracker.get("body"))
    bundle = bundle_check(tracker.get("body"), None)
    put(("dev", token), dev.get("status") == "PASS", None)
    put(("bundle", token), bundle.get("ok") is True, None)


def _plan_history(history, body, now, view, put, fresh, asset_ids) -> None:
    kind, subject, past_mint = history
    if not isinstance(subject, str) or subject == "":
        return
    if kind == "page":
        ids = asset_ids(body)
        if ids is None or subject not in ids:
            put(("rug_history_live", subject), False, None)
            return
        put(("rug_page", subject), True, None)
        put(("rug_ids", subject), True, None, ids)
        past = [item for item in ids if item != subject]
        if not past:
            put(("rug_history_live", subject), True, None)
        return
    if kind != "report" or not isinstance(past_mint, str) or type(body.get("rugged")) is not bool:
        return
    put(("rug_report", subject, past_mint), True, None, {"rugged": body["rugged"]})
    state = view()
    page = state.get(("rug_page", subject))
    listed = state.get(("rug_ids", subject))
    if not fresh(page, now) or not isinstance(listed, dict):
        return
    ids = listed.get("body")
    if not isinstance(ids, list) or subject not in ids:
        put(("rug_history_live", subject), False, None)
        return
    past = [item for item in ids if item != subject]
    for item in past:
        report = state.get(("rug_report", subject, item))
        saved = report.get("body") if isinstance(report, dict) else None
        if not fresh(report, now) or not isinstance(saved, dict) or saved.get("rugged") is not False:
            if item == past_mint and body.get("rugged") is True:
                put(("rug_history_live", subject), False, None)
            return
    put(("rug_history_live", subject), True, None)


def _lp_ok(body: dict, unit) -> bool:
    """Burned or locked, each finite and inside 0..100, covering exactly 100."""
    if not isinstance(body, dict):
        return False
    has_burn = "lpBurn" in body
    has_lock = "lpLockedPct" in body
    burn = body.get("lpBurn") if has_burn else None
    locked = body.get("lpLockedPct") if has_lock else None
    if has_burn and has_lock:
        return unit(burn) and unit(locked) and burn + locked == 100
    if has_burn:
        return unit(burn) and burn == 100
    if has_lock:
        return unit(locked) and locked == 100
    return False


def _unit(value, finite=math.isfinite) -> bool:
    if type(value) is int:
        return 0 <= value <= 100
    if type(value) is float and finite(value):
        return 0 <= value <= 100
    return False


def _asset_ids(body: dict) -> list | None:
    result = body.get("result") if isinstance(body.get("result"), dict) else None
    if not isinstance(result, dict):
        return None
    total = result.get("total")
    items = result.get("items")
    if type(total) is not int or not isinstance(items, list) or total != len(items) or total <= 0 or total >= 20:
        return None
    ids = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or item["id"] in ids:
            return None
        ids.append(item["id"])
    if len(ids) != total:
        return None
    return ids


def _fresh(row, now: float, limit: float) -> bool:
    if not isinstance(row, dict) or row.get("ok") is not True:
        return False
    stamp = row.get("at")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return False
    age = now - stamp
    if not (0 <= age <= limit):
        return False
    until = row.get("until")
    if until is not None and (isinstance(until, bool) or not isinstance(until, (int, float)) or until <= now):
        return False
    return True




def _mint_at(url: str, left: str, right: str) -> str | None:
    if left not in url:
        return None
    token = url.split(left, 1)[1]
    if right not in token:
        return None
    token = token.split(right, 1)[0].strip("/")
    if token == "" or "/" in token:
        return None
    return token


_SOCKET = socket.socket
_CONNECT = getattr(socket.socket, "connect")
_CREATE_CONNECTION = socket.create_connection
_SSL_SOCKET = ssl.SSLSocket
_GETPEERCERT = ssl.SSLSocket.getpeercert
_WRAP = ssl.SSLContext.wrap_socket
_LOAD_VERIFY = ssl.SSLContext.load_verify_locations
_HTTPS_CONNECT = http.client.HTTPSConnection.connect
_HTTP_CONNECT = http.client.HTTPConnection.connect
_URLOPEN = urllib.request.urlopen
_HTTPS_CONNECTION = http.client.HTTPSConnection
_CA_FILE = certifi.where()
_HTTP_TRANSPORT_HANDLE = httpx.HTTPTransport.handle_request
_CLIENT_GET = httpx.Client.get
_CLIENT = None


def _stack_intact() -> bool:
    return (
        socket.socket is _SOCKET
        and getattr(socket.socket, "connect") is _CONNECT
        and socket.create_connection is _CREATE_CONNECTION
        and ssl.SSLSocket is _SSL_SOCKET
        and ssl.SSLSocket.getpeercert is _GETPEERCERT
        and ssl.SSLContext.wrap_socket is _WRAP
        and urllib.request.urlopen is _URLOPEN
        and http.client.HTTPSConnection is _HTTPS_CONNECTION
        and httpx.HTTPTransport.handle_request is _HTTP_TRANSPORT_HANDLE
        and httpx.Client.get is _CLIENT_GET
    )


def _global_address(value) -> bool:
    """True only for a globally routable address. CGNAT and mapped CGNAT are not."""
    try:
        addr = ipaddress.ip_address(value)
    except (TypeError, ValueError):
        return False
    mapped = addr.ipv4_mapped if type(addr) is ipaddress.IPv6Address else None
    if mapped is not None:
        addr = mapped
    return addr.is_global is True


def _cert_matches(cert, host: str) -> bool:
    if type(cert) is not dict or type(host) is not str or host == "":
        return False
    names = cert.get("subjectAltName")
    if type(names) is not tuple or not names:
        return False
    wanted = host.lower()
    matched = False
    for item in names:
        if type(item) is not tuple or len(item) != 2:
            return False
        kind, value = item
        if kind == "DNS" and type(value) is str and value.lower() == wanted:
            matched = True
    return matched


def _handshake_host(sock, host: str) -> bool:
    if not _stack_intact() or type(sock) is not _SSL_SOCKET or type(host) is not str:
        return False
    if type(sock).getpeercert is not _GETPEERCERT:
        return False
    try:
        cert = _GETPEERCERT(sock)
    except Exception:
        return False
    return _cert_matches(cert, host)


class _DeskHTTPSConnection(_HTTPS_CONNECTION):
    """Opens the TLS socket. The proof is the certificate read afterwards, not this class."""

    def connect(self):
        super().connect()


class _OwnedTransport(httpx.BaseTransport):
    """The httpx transport this module owns. A replaced get() never enters here."""

    def __init__(self):
        self._inner = httpx.HTTPTransport(retries=0)

    def handle_request(self, request):
        response = self._inner.handle_request(request)
        self._note(response, request)
        return response

    def _note(self, response, request) -> None:
        """Replaced at import by the closure that verified this socket. This body does not write."""
        return None


def http_client():
    """The one client. Replacing it, or its get method, does not record a proof."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = httpx.Client(transport=_OwnedTransport(), follow_redirects=False, timeout=8.0)
    return _CLIENT


def _wire():
    """Bind the handshake objects and the note store. Later global lookups are not the originals."""
    global closed, exchange, _owned_https, _reset_for_tests, _seen
    global _CLOSED, _RESET, _EXCHANGE, _OWNED, _SEEN, _commit_verified, _handshake_host, _stack_intact, _NOTES
    global attributes_intact, monitored
    global _fresh, _lp_ok, _plan, _plan_dev, _plan_history
    global quote_ready, preflight_observed, real_submit_observed, fresh_sol_leg
    secret = os.urandom(32)
    notes = {}
    lock = threading.Lock()
    connection_cls = _DeskHTTPSConnection
    ssl_socket = _SSL_SOCKET
    getpeercert = _GETPEERCERT
    getpeername = ssl.SSLSocket.getpeername
    cert_matches = _cert_matches
    ssl_context_cls = ssl.SSLContext
    tls_client = ssl.PROTOCOL_TLS_CLIENT
    cert_required = ssl.CERT_REQUIRED
    load_verify = _LOAD_VERIFY
    https_connect = _HTTPS_CONNECT
    http_connect = _HTTP_CONNECT
    global_address = _global_address
    ca_file = _CA_FILE
    getaddrinfo_fn = socket.getaddrinfo
    socket_type = _SOCKET
    connect_fn = _CONNECT
    create_fn = _CREATE_CONNECTION
    wrap = _WRAP
    urlopen = _URLOPEN
    https_base = _HTTPS_CONNECTION
    transport_handle = _HTTP_TRANSPORT_HANDLE
    client_get = _CLIENT_GET
    sync_stream = _SYNC_STREAM
    fresh_body = _fresh
    lp_body = _lp_ok
    plan_body = _plan
    dev_body = _plan_dev
    hist_body = _plan_history
    unit = _unit
    mint_at = _mint_at
    asset_ids = _asset_ids
    apply_fn = _apply
    record_fn = _record
    client_fn = http_client
    hmac_new = hmac.new
    digest_new = hashlib.sha256
    compare_digest = hmac.compare_digest
    clock = time.time
    dumps = json.dumps
    loads = json.loads
    window = FRESH_S
    import public_filter_docs
    import cross_source
    import pyth_sponsored
    docs = public_filter_docs
    markets = cross_source
    pyth = pyth_sponsored
    parse_holder = docs.parse_holder_chart
    bundle_check = markets.bundle_check
    dev_check = markets.dev_check
    assess_price = pyth.assess_signer_price
    split = urlsplit
    parse_query = parse_qs

    def fresh(row, now) -> bool:
        return fresh_body(row, now, window)

    def lp_ok(body) -> bool:
        return lp_body(body, unit)

    def plan_dev(token, now, view, put) -> None:
        return dev_body(token, now, view, put, fresh, dev_check, bundle_check)

    def plan_history(history, body, now, view, put) -> None:
        return hist_body(history, body, now, view, put, fresh, asset_ids)

    def plan(url, body, mint, now, history, existing) -> list:
        return plan_body(
            url, body, mint, now, history, existing,
            lp_ok, mint_at, plan_dev, plan_history, parse_holder, bundle_check, assess_price, split, parse_query,
        )

    def canonical(value):
        if type(value) is tuple:
            return [canonical(item) for item in value]
        if type(value) is list:
            return [canonical(item) for item in value]
        if type(value) is dict:
            return {str(key): canonical(item) for key, item in value.items()}
        return value

    def mac_for(key, row) -> str:
        gate = key[0] if type(key) is tuple and key else ""
        digest = digest_new(dumps(canonical({
            "ok": row.get("ok"),
            "until": row.get("until"),
            "body": row.get("body"),
        }), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        message = dumps(canonical({
            "gate": gate,
            "key": key,
            "timestamp": row.get("at"),
            "digest": digest,
        }), sort_keys=True, separators=(",", ":")).encode()
        return hmac_new(secret, message, digest_new).hexdigest()

    def authentic(key, row) -> bool:
        if type(row) is not dict or type(row.get("mac")) is not str:
            return False
        try:
            expect = mac_for(key, row)
        except Exception:
            return False
        return compare_digest(expect, row["mac"])

    def sign(key, row) -> dict:
        signed = dict(row)
        signed["mac"] = mac_for(key, signed)
        return signed

    def seen() -> dict:
        with lock:
            copy = dict(notes)
        return {key: row for key, row in copy.items() if authentic(key, row)}

    def save(rows) -> None:
        stamped = []
        for key, row in rows:
            stamped.append((key, sign(key, row)))
        with lock:
            for key, row in stamped:
                notes[key] = row

    def public_peer(sock) -> bool:
        if ssl.SSLSocket.getpeername is not getpeername:
            return False
        try:
            peer = getpeername(sock)
        except Exception:
            return False
        if type(peer) is not tuple or not peer or type(peer[0]) is not str:
            return False
        return global_address(peer[0]) is True

    def handshake(sock, host) -> bool:
        if not stack_intact() or type(sock) is not ssl_socket or type(host) is not str:
            return False
        if type(sock).getpeercert is not getpeercert:
            return False
        if not public_peer(sock):
            return False
        try:
            cert = getpeercert(sock)
        except Exception:
            return False
        return cert_matches(cert, host)

    def stack_intact() -> bool:
        mod = sys.modules[__name__]
        return (
            socket.socket is socket_type
            and getattr(socket.socket, "connect") is connect_fn
            and socket.create_connection is create_fn
            and socket.getaddrinfo is getaddrinfo_fn
            and ssl.SSLSocket is ssl_socket
            and ssl.SSLSocket.getpeercert is getpeercert
            and ssl.SSLSocket.getpeername is getpeername
            and ssl.SSLContext is ssl_context_cls
            and ssl.SSLContext.wrap_socket is wrap
            and ssl.SSLContext.load_verify_locations is load_verify
            and ssl.PROTOCOL_TLS_CLIENT is tls_client
            and ssl.CERT_REQUIRED is cert_required
            and urllib.request.urlopen is urlopen
            and http.client.HTTPSConnection is https_base
            and http.client.HTTPSConnection.connect is https_connect
            and http.client.HTTPConnection.connect is http_connect
            and httpx.HTTPTransport.handle_request is transport_handle
            and httpx.Client.get is client_get
            and mod._DeskHTTPSConnection is connection_cls
            and mod._CA_FILE is ca_file
            and mod._cert_matches is cert_matches
            and mod._handshake_host is handshake
            and mod._owned_https is owned_https
            and mod._commit_verified is commit
            and mod._OwnedTransport.handle_request is handle
        )

    def client_context():
        ctx = ssl_context_cls(tls_client)
        ctx.verify_mode = cert_required
        ctx.check_hostname = True
        load_verify(ctx, cafile=ca_file)
        return ctx

    def sim_rows(raw, parsed, now):
        """A Helius simulateTransaction this socket just read. The URL is not stored."""
        if not isinstance(raw, (bytes, bytearray)) or type(parsed) is not dict:
            return []
        try:
            sent = loads(bytes(raw))
        except Exception:
            return []
        if type(sent) is not dict or sent.get("method") != "simulateTransaction":
            return []
        params = sent.get("params")
        if type(params) is not list or not params or type(params[0]) is not str or params[0] == "":
            return []
        options = params[1] if len(params) > 1 else None
        if type(options) is not dict or options.get("sigVerify") is not False:
            return []
        digest = digest_new(params[0].encode()).hexdigest()
        result = parsed.get("result")
        value = result.get("value") if type(result) is dict else None
        ok = False
        if type(value) is dict and value.get("err") is None and "error" not in parsed:
            logs = value.get("logs")
            denied = False
            if type(logs) is list:
                for line in logs:
                    if type(line) is str and "insufficient" in line.lower():
                        denied = True
            if not denied:
                ok = _sim_within_caps(params[0], value, now, seen(), fresh)
        return [(("preflight_ready", digest), {"at": now, "ok": ok is True, "until": None})]

    def submit_rows(raw, parsed, now):
        """A Helius send or confirmation this socket just read. The URL is not stored."""
        if not isinstance(raw, (bytes, bytearray)) or type(parsed) is not dict or "error" in parsed:
            return []
        try:
            sent = loads(bytes(raw))
        except Exception:
            return []
        if type(sent) is not dict:
            return []
        method = sent.get("method")
        params = sent.get("params")
        if method == "sendTransaction" and type(params) is list and params and type(params[0]) is str:
            binding = _sent_binding(params[0])
            result_sig = parsed.get("result")
            if binding is None:
                return []
            local = binding["signature"]
            body = {
                "message_sha256": binding["message_sha256"],
                "fee_payer": binding["fee_payer"],
                "keys": binding["keys"],
            }
            ok = type(result_sig) is str and result_sig == local
            return [(("submit_sent", local), {"at": now, "ok": ok, "until": None, "body": body})]
        if method == "getSignatureStatuses" and type(params) is list and params and type(params[0]) is list and params[0]:
            signature = params[0][0]
            if type(signature) is not str or signature == "":
                return []
            result = parsed.get("result")
            values = result.get("value") if type(result) is dict else None
            item = values[0] if type(values) is list and values else None
            ok = (
                type(item) is dict
                and item.get("err") is None
                and item.get("confirmationStatus") in {"confirmed", "finalized"}
            )
            return [(("submit_status", signature), {"at": now, "ok": ok, "until": None})]
        if method == "getTransaction" and type(params) is list and params and type(params[0]) is str:
            signature = params[0]
            result = parsed.get("result")
            sent = seen().get(("submit_sent", signature))
            sent_body = sent.get("body") if isinstance(sent, dict) else None
            bound = _bound_to_note(result, signature, sent_body)
            summary = _fill_summary(result) if bound else None
            ok = bound and isinstance(summary, dict) and summary.get("positive") is True
            return [(("real_submit", signature), {"at": now, "ok": ok, "until": None, "body": summary if ok else None})]
        return []

    def seal(url, parsed, mint, history, request_body=None) -> None:
        if not check():
            return
        moment = clock()
        rows = list(plan(url, parsed, mint, moment, history, seen()))
        host = split(url).hostname if type(url) is str else None
        if host == "mainnet.helius-rpc.com":
            rows.extend(sim_rows(request_body, parsed, moment))
            rows.extend(submit_rows(request_body, parsed, moment))
        if rows:
            save(rows)
        if _submit_ready(seen(), moment):
            submit_mark["closed"] = True

    def commit(sock, url, parsed, mint, history) -> None:
        host = split(url).hostname if type(url) is str else None
        if not handshake(sock, host if type(host) is str else ""):
            return
        seal(url, parsed, mint, history)

    def owned_https(request, timeout, mint=None, history=None):
        parts = split(request.full_url)
        if parts.scheme != "https" or type(parts.hostname) is not str or parts.hostname == "":
            raise urllib.error.URLError("host")
        conn = connection_cls(parts.hostname, timeout=timeout, context=client_context())
        path = parts.path or "/"
        if parts.query:
            path = path + "?" + parts.query
        headers = {key: value for key, value in request.header_items()}
        try:
            conn.connect()
            sock = conn.sock
            accepted = handshake(sock, parts.hostname)
            conn.request(request.get_method(), path, body=request.data, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            status = response.status
            parsed = None
            if status == 200:
                try:
                    parsed = loads(raw.decode("utf-8"))
                except Exception:
                    parsed = None
            if conn.sock is not None and conn.sock is not sock:
                accepted = False
            proved = accepted and type(parsed) is dict and check()
            if proved:
                seal(request.full_url, parsed, mint, history, request.data)
            return status, raw, proved
        finally:
            conn.close()

    def do_exchange(request, timeout, mint=None, history=None):
        if not stack_intact():
            response = urllib.request.urlopen(request, timeout=timeout)
            try:
                return response.status, response.read(), False
            finally:
                closer = getattr(response, "close", None)
                if callable(closer):
                    closer()
        return owned_https(request, timeout, mint, history)

    age_limit = quote_max_age
    age_ok = quote_age_ok

    def note_ok(gate, mint, now=None, *, input_mint=None, output_mint=None) -> bool:
        """True only for a fresh proof of this gate for this mint."""
        moment = clock() if now is None else now
        if isinstance(moment, bool) or not isinstance(moment, (int, float)):
            return False
        if not isinstance(mint, str) or mint == "":
            return False
        current = seen()
        if gate == "bundle_unique_buyers_smart_wallets_dev_holding":
            return fresh(current.get(("bundle", mint)), moment) and fresh(current.get(("dev", mint)), moment)
        if gate == "quote_expiry":
            if mint not in {input_mint, output_mint}:
                return False
            row = current.get(("quote_expiry", input_mint, output_mint))
            return fresh(row, moment) and age_ok(row, moment, age_limit())
        if gate == "preflight_ready":
            return fresh(current.get(("preflight_ready", mint)), moment)
        return fresh(current.get((gate, mint)), moment)

    def quote_ready(mint=None, now=None, *, input_mint=None, output_mint=None) -> bool:
        """A quote this process read over its own TLS connection, inside the max age."""
        moment = clock() if now is None else now
        if isinstance(moment, bool) or not isinstance(moment, (int, float)):
            return False
        current = seen()
        limit = age_limit()

        def row_ok(row) -> bool:
            return fresh(row, moment) and age_ok(row, moment, limit)

        if isinstance(input_mint, str) and isinstance(output_mint, str) and input_mint and output_mint:
            if isinstance(mint, str) and mint not in {input_mint, output_mint}:
                return False
            return row_ok(current.get(("quote_expiry", input_mint, output_mint)))
        if isinstance(mint, str) and mint:
            for key, row in current.items():
                if type(key) is not tuple or len(key) != 3 or key[0] != "quote_expiry":
                    continue
                if mint not in {key[1], key[2]} or WSOL not in {key[1], key[2]}:
                    continue
                if row_ok(row):
                    return True
            return False
        for key, row in current.items():
            if type(key) is tuple and len(key) == 3 and key[0] == "quote_expiry" and WSOL in {key[1], key[2]} and row_ok(row):
                return True
        return False

    def preflight_observed(mint=None, now=None, *, digest=None) -> bool:
        """A simulateTransaction this process read, not a passed-in RPC body."""
        moment = clock() if now is None else now
        if isinstance(moment, bool) or not isinstance(moment, (int, float)):
            return False
        current = seen()
        if isinstance(digest, str) and digest:
            return fresh(current.get(("preflight_ready", digest)), moment)
        if isinstance(mint, str) and mint:
            return fresh(current.get(("preflight_ready", mint)), moment)
        for key, row in current.items():
            if type(key) is tuple and len(key) == 2 and key[0] == "preflight_ready" and fresh(row, moment):
                return True
        return False

    submit_mark = {"closed": False}

    def _submit_ready(current, moment) -> bool:
        """Send, confirmed status, and one owner+mint fill, all from this socket."""
        if not isinstance(current, dict):
            return False
        for key, row in current.items():
            if type(key) is not tuple or len(key) != 2 or key[0] != "real_submit":
                continue
            if not fresh(row, moment) or row.get("ok") is not True:
                continue
            signature = key[1]
            if not isinstance(signature, str) or signature == "":
                continue
            sent = current.get(("submit_sent", signature))
            status = current.get(("submit_status", signature))
            if fresh(sent, moment) and sent.get("ok") is True and fresh(status, moment) and status.get("ok") is True:
                return True
        return False

    def real_submit_observed(mint=None, now=None) -> bool:
        """Closed only after seal saw a confirmed fill. A note write does not flip this."""
        return submit_mark["closed"] is True

    def fresh_sol_leg(now=None):
        """The SOL/USD note this process read. A caller-supplied price is not a leg."""
        moment = clock() if now is None else now
        if isinstance(moment, bool) or not isinstance(moment, (int, float)):
            return None
        current = seen()
        for key, row in current.items():
            if type(key) is not tuple or len(key) != 2 or key[0] != "price_freshness_proven":
                continue
            if not fresh(row, moment):
                continue
            body = row.get("body")
            if not isinstance(body, dict):
                continue
            return {
                "fresh": True,
                "used": True,
                "price": body.get("price"),
                "conf": body.get("conf"),
                "expo": body.get("expo"),
            }
        return None

    def reset() -> None:
        with lock:
            notes.clear()
        submit_mark["closed"] = False

    view = types.MappingProxyType(notes)
    snapshots = {"classes": (), "functions": (), "methods": ()}

    def mapping_matches(saved, live) -> bool:
        if set(saved) != set(live):
            return False
        for key, value in saved.items():
            if live[key] is not value:
                return False
        return True

    def check() -> bool:
        mod = sys.modules[__name__]
        expected = (
            ("closed", note_ok),
            ("exchange", do_exchange),
            ("_owned_https", owned_https),
            ("_commit_verified", commit),
            ("_handshake_host", handshake),
            ("_cert_matches", cert_matches),
            ("_stack_intact", stack_intact),
            ("_NOTES", view),
            ("_DeskHTTPSConnection", connection_cls),
            ("_CA_FILE", ca_file),
            ("_global_address", global_address),
            ("_fresh", fresh),
            ("_lp_ok", lp_ok),
            ("_plan", plan),
            ("_plan_dev", plan_dev),
            ("_plan_history", plan_history),
            ("_mint_at", mint_at),
            ("_asset_ids", asset_ids),
            ("_unit", unit),
            ("urlsplit", split),
            ("parse_qs", parse_query),
            ("_apply", apply_fn),
            ("_record", record_fn),
            ("_CLOSED", note_ok),
            ("_EXCHANGE", do_exchange),
            ("_OWNED", owned_https),
            ("_SEEN", seen),
            ("_RESET", reset),
            ("http_client", client_fn),
            ("_seen", seen),
            ("attributes_intact", check),
            ("monitored", names),
        )
        for name, original in expected:
            if getattr(mod, name, None) is not original:
                return False
        if mod._OwnedTransport._note is not note or mod._OwnedTransport.handle_request is not handle:
            return False
        if mod.FRESH_S is not window:
            return False
        if hmac.new is not hmac_new or hashlib.sha256 is not digest_new or hmac.compare_digest is not compare_digest:
            return False
        if time.time is not clock or json.dumps is not dumps or json.loads is not loads:
            return False
        if docs.parse_holder_chart is not parse_holder:
            return False
        if markets.bundle_check is not bundle_check or markets.dev_check is not dev_check:
            return False
        if pyth.assess_signer_price is not assess_price:
            return False
        if not snapshots["classes"] or not snapshots["functions"] or not snapshots["methods"]:
            return False
        for cls, saved in snapshots["classes"]:
            if mapping_matches(saved, cls.__dict__) is not True:
                return False
        for fn, saved in snapshots["methods"]:
            if mapping_matches(saved, fn.__dict__) is not True:
                return False
        mod_functions = {
            fname: fvalue
            for fname, fvalue in vars(mod).items()
            if isinstance(fvalue, types.FunctionType)
        }
        if set(mod_functions) != {fname for fname, _fn, _saved in snapshots["functions"]}:
            return False
        for fname, fvalue, saved in snapshots["functions"]:
            if mod_functions[fname] is not fvalue:
                return False
            if mapping_matches(saved, fvalue.__dict__) is not True:
                return False
        return stack_intact()

    def names():
        return (
            "closed",
            "exchange",
            "_owned_https",
            "_commit_verified",
            "_handshake_host",
            "_cert_matches",
            "_stack_intact",
            "_NOTES",
            "_DeskHTTPSConnection",
            "_CA_FILE",
            "_global_address",
            "_fresh",
            "_lp_ok",
            "_plan",
            "_plan_dev",
            "_plan_history",
            "_mint_at",
            "_asset_ids",
            "_unit",
            "urlsplit",
            "parse_qs",
            "_apply",
            "_record",
            "_CLOSED",
            "_EXCHANGE",
            "_OWNED",
            "_SEEN",
            "_RESET",
            "http_client",
            "_seen",
            "attributes_intact",
            "monitored",
            "FRESH_S",
            "_note",
            "handle_request",
        )

    def note(self, response, request) -> None:
        if self._inner.handle_request.__func__ is not transport_handle:
            return
        if not stack_intact() or type(response) is not httpx.Response:
            return
        extensions = response.extensions if type(response.extensions) is dict else None
        stream = None if extensions is None else extensions.get("network_stream")
        if type(stream) is not sync_stream:
            return
        host = request.url.host
        if not handshake(stream._sock, host if type(host) is str else ""):
            return
        try:
            parsed = loads(response.content.decode("utf-8"))
        except Exception:
            return
        if type(parsed) is not dict:
            return
        commit(stream._sock, str(request.url), parsed, None, None)

    def handle(self, request):
        ctx = client_context()
        inner = httpx.HTTPTransport(verify=ctx, retries=0)
        previous = self._inner
        self._inner = inner
        try:
            response = inner.handle_request(request)
            note(self, response, request)
            return response
        finally:
            self._inner = previous
            inner.close()

    closed = note_ok
    exchange = do_exchange
    _owned_https = owned_https
    _reset_for_tests = reset
    _seen = seen
    _fresh = fresh
    _lp_ok = lp_ok
    _plan = plan
    _plan_dev = plan_dev
    _plan_history = plan_history
    _CLOSED = note_ok
    _RESET = reset
    _EXCHANGE = do_exchange
    _OWNED = owned_https
    _SEEN = seen
    _commit_verified = commit
    _handshake_host = handshake
    _stack_intact = stack_intact
    _NOTES = view
    attributes_intact = check
    monitored = names
    quote_ready = quote_ready
    preflight_observed = preflight_observed
    real_submit_observed = real_submit_observed
    fresh_sol_leg = fresh_sol_leg
    _OwnedTransport._note = note
    _OwnedTransport.handle_request = handle
    mod = sys.modules[__name__]
    snapshots["classes"] = tuple(
        (cls, dict(cls.__dict__))
        for cls in (connection_cls, _OwnedTransport)
    )
    snapshots["functions"] = tuple(
        (fname, fvalue, dict(fvalue.__dict__))
        for fname, fvalue in vars(mod).items()
        if isinstance(fvalue, types.FunctionType) and fname != "_wire"
    )
    snapshots["methods"] = tuple(
        (fn, dict(fn.__dict__))
        for _cls, saved in snapshots["classes"]
        for fn in saved.values()
        if isinstance(fn, types.FunctionType)
    )


_wire()
del _wire
