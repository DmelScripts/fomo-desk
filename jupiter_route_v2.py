"""Jupiter route_v2 buy and exit decoder. It does not sign or send.

The layout is the program-owned IDL at
C88XWfp26heEmDkmfSzeXP7Fd7GQJ2j9dDTUsyiZbUTa, decompressed sha256
e6c2b425ec740922ee91aa7a3278cea07f4bc00a3ed9efa4eca16facb481bfca.
route_v2 discriminator is [187, 100, 250, 204, 49, 196, 175, 20].
Arguments are in_amount, quoted_out_amount, slippage_bps, platform_fee_bps,
positive_slippage_bps, then one route step.

The enforced minimum is not the JSON otherAmountThreshold. Jupiter's quote
schema says that field is not what /swap writes. The program publishes
SlippageToleranceExceeded (6001) for the instruction's quoted_out_amount and
slippage_bps. The integer floor is quoted_out_amount minus
quoted_out_amount * slippage_bps // 10000. A keyless SOL/USDC build matched
that floor exactly (889185 at 50 bps -> 884740).

Swap::Whirlpool is variant 17 and carries a_to_b. A keyless build restricted
to Whirlpool placed the Whirlpool program id immediately before Orca's
published 11-account swap. Other hop enums are named and then refused: the
IDL does not list their remaining accounts.
"""

from __future__ import annotations

import time
from decimal import Decimal

import phantom_transport
import pyth_sponsored
import solana_jupiter
import solana_tx
import solana_whirlpool
import usdc_valuation

SLOT_LAG_MAX = 150

EVENT = "D8cy77BBepLMngZx6ZukaTff5hCt1HrWyKk3Hnd9oitf"
DISC = bytes([187, 100, 250, 204, 49, 196, 175, 20])
WHIRLPOOL = 17
FEE_DENOMINATOR = 1_000_000
BASE_FEE_LAMPORTS = 5_000
LAMPORTS_PER_SOL = 1_000_000_000
# Payload bytes after the variant tag. None cannot be walked. Absent means 0.
_PAYLOAD = {
    8: 1, 12: None, 15: None, 16: None, 17: 1, 18: 1, 21: 1, 23: 1, 24: None,
    27: None, 28: None, 29: 16, 33: 4, 39: None, 41: 4, 42: 3, 43: 10, 44: 5,
    45: 5, 47: None, 58: 1, 60: 1, 61: 1, 64: None, 71: 2, 75: None, 81: 8,
    82: 8, 85: None, 86: 2, 87: 9, 89: None, 94: 1, 95: 1, 103: None, 104: 1,
    106: 1, 107: None, 110: None, 111: None, 116: None, 117: 1, 118: 9, 119: 1,
    120: None, 121: 1, 122: 16, 123: None, 125: None, 126: None, 127: 1, 129: 1,
    132: None, 135: 10, 136: 1, 141: 1, 145: 1, 146: None, 151: 1, 152: 1,
    153: 1, 155: None, 157: 9, 159: 8, 160: 1, 161: None, 162: 1, 164: 1,
    165: 1, 166: 1, 167: 2, 168: 1, 170: 1, 171: None, 172: None, 174: None,
    177: 1, 178: None, 181: 1, 182: None, 183: None, 184: 1, 185: None, 186: None,
    187: 2, 189: 1, 190: None, 191: 2, 192: 1, 193: 1, 194: 1, 196: 1, 197: None,
    199: 1, 203: 1,
}


def candidate_floors(quoted: int, slippage_bps: int) -> tuple[int, int] | None:
    """Two integer readings of the same fields. The program's choice is not in the IDL.

    `lower` is quoted * (10000 - bps) // 10000. `higher` subtracts the truncated
    slippage and is 1 raw unit larger when there is a remainder. A keyless API
    sample matched `higher`. That sample is not the deployed comparison.
    """
    if isinstance(quoted, bool) or isinstance(slippage_bps, bool):
        return None
    if not isinstance(quoted, int) or not isinstance(slippage_bps, int):
        return None
    if quoted <= 0 or not 0 <= slippage_bps < 10_000:
        return None
    lower = quoted * (10_000 - slippage_bps) // 10_000
    higher = quoted - quoted * slippage_bps // 10_000
    if lower <= 0 or higher <= 0:
        return None
    return lower, higher


def minimum_out(quoted: int, slippage_bps: int) -> int | None:
    """The lower reading. We do not claim the program uses it."""
    floors = candidate_floors(quoted, slippage_bps)
    if floors is None:
        return None
    return floors[0]


def parse(data: bytes) -> dict:
    refused = {"ok": False, "min_out_proven": False, "send": False, "live_ready": False}
    if not isinstance(data, (bytes, bytearray)) or len(data) < 34 or bytes(data[:8]) != DISC:
        return refused | {"reason": "route_v2_bytes"}
    quoted = int.from_bytes(data[16:24], "little")
    slippage = int.from_bytes(data[24:26], "little")
    floors = candidate_floors(quoted, slippage)
    if floors is None:
        return refused | {"reason": "min_out", "quoted_out_amount": quoted, "slippage_bps": slippage, "program_rounding_proven": False}
    floor, api_floor = floors
    count = int.from_bytes(data[30:34], "little")
    if count != 1:
        return refused | {"reason": "route_steps", "min_out": floor}
    variant = data[34]
    width = _PAYLOAD.get(variant, 0)
    if width is None or 35 + width + 4 != len(data):
        return refused | {"reason": "hop_layout_unpublished", "variant": variant, "min_out": floor, "min_out_proven": True}
    cursor = 35 + width
    bps = int.from_bytes(data[cursor:cursor + 2], "little")
    input_index = data[cursor + 2]
    output_index = data[cursor + 3]
    if bps != 10_000 or input_index != 0 or output_index != 1:
        return refused | {"reason": "route_split", "min_out": floor, "min_out_proven": True}
    body = {
        "ok": variant == WHIRLPOOL,
        "reason": None if variant == WHIRLPOOL else "hop_accounts_unpublished",
        "variant": variant,
        "hop": "Whirlpool" if variant == WHIRLPOOL else "variant_%s" % variant,
        "in_amount": int.from_bytes(data[8:16], "little"),
        "quoted_out_amount": quoted,
        "slippage_bps": slippage,
        "platform_fee_bps": int.from_bytes(data[26:28], "little"),
        "positive_slippage_bps": int.from_bytes(data[28:30], "little"),
        "min_out": floor,
        "api_sample_floor": api_floor,
        "program_rounding_proven": False,
        "min_out_proven": True,
        "a_to_b": bool(data[35]) if variant == WHIRLPOOL else None,
        "bytes_consumed": True,
        "send": False,
        "sign": False,
        "live_ready": False,
        "idl_sha256": solana_jupiter.IDL_SHA256,
    }
    if variant == WHIRLPOOL and data[35] > 1:
        return refused | {"reason": "whirlpool_direction", "min_out": floor}
    return body


def _wsol_amount(record) -> int | None:
    if not isinstance(record, tuple) or len(record) != 2 or not isinstance(record[1], (bytes, bytearray)):
        return None
    if len(record[1]) < 72:
        return None
    return int.from_bytes(record[1][64:72], "little")


def network_fee(instructions: list[bytes], signatures: int = 1) -> dict:
    """Priority fee from the two compute-budget instructions, plus one signature."""
    limit = None
    price = None
    if not isinstance(instructions, list):
        return {"ok": False, "reason": "compute_budget", "network_fee_proven": False}
    for raw in instructions:
        if not isinstance(raw, (bytes, bytearray)) or not raw:
            return {"ok": False, "reason": "compute_budget", "network_fee_proven": False}
        tag = raw[0]
        if tag == 2 and len(raw) == 5:
            limit = int.from_bytes(raw[1:5], "little")
        elif tag == 3 and len(raw) == 9:
            price = int.from_bytes(raw[1:9], "little")
        else:
            return {"ok": False, "reason": "compute_budget", "network_fee_proven": False}
    if limit is None or price is None or limit <= 0:
        return {"ok": False, "reason": "compute_unit_limit_missing", "network_fee_proven": False}
    if isinstance(signatures, bool) or not isinstance(signatures, int) or signatures < 1:
        return {"ok": False, "reason": "extra_signer", "network_fee_proven": False}
    priority = (price * limit + 999_999) // 1_000_000
    base = BASE_FEE_LAMPORTS * signatures
    return {
        "ok": True,
        "reason": None,
        "network_fee_proven": True,
        "compute_unit_limit": limit,
        "micro_lamports_per_cu": price,
        "priority_lamports": priority,
        "base_lamports": base,
        "signatures": signatures,
        "network_lamports": priority + base,
    }


def whirlpool_fee(amount_in: int, fee_rate: int) -> int | None:
    """Ceiling of the Whirlpool input fee, in the input mint's raw units."""
    if isinstance(amount_in, bool) or isinstance(fee_rate, bool):
        return None
    if not isinstance(amount_in, int) or not isinstance(fee_rate, int):
        return None
    if amount_in <= 0 or fee_rate < 0 or fee_rate > FEE_DENOMINATOR:
        return None
    return (amount_in * fee_rate + FEE_DENOMINATOR - 1) // FEE_DENOMINATOR


def lamports_to_micro_usdc(lamports: int, micro_usdc_per_sol: int) -> int | None:
    """Ceiling. micro_usdc_per_sol prices 1 SOL, which is 1e9 lamports, not 1e6."""
    if isinstance(lamports, bool) or isinstance(micro_usdc_per_sol, bool):
        return None
    if not isinstance(lamports, int) or not isinstance(micro_usdc_per_sol, int):
        return None
    if lamports < 0 or micro_usdc_per_sol <= 0:
        return None
    return (lamports * micro_usdc_per_sol + LAMPORTS_PER_SOL - 1) // LAMPORTS_PER_SOL


def token_raw_to_lamports(raw: int, sqrt_price: int, token_is_mint_a: bool) -> int | None:
    """Ceiling SOL lamports for a token-denominated Whirlpool fee. Not a lamport count."""
    if isinstance(raw, bool) or isinstance(sqrt_price, bool) or isinstance(token_is_mint_a, bool) is False and not isinstance(token_is_mint_a, bool):
        return None
    if not isinstance(raw, int) or not isinstance(sqrt_price, int) or not isinstance(token_is_mint_a, bool):
        return None
    if raw <= 0 or sqrt_price <= 0:
        return None
    square = sqrt_price * sqrt_price
    if token_is_mint_a:
        return (raw * square + (1 << 128) - 1) // (1 << 128)
    return (raw * (1 << 128) + square - 1) // square


def sol_micro_from_pool(pool_data: bytes) -> int | None:
    """Micro-USDC per SOL from the canonical SOL/USDC pool bytes. A caller integer is not accepted."""
    import solana_direct_swap
    fields = solana_whirlpool.pool_fields(pool_data)
    if fields is None:
        return None
    mints = {fields[0], fields[2]}
    if mints != {solana_tx.NATIVE_MINT, solana_tx.USDC_MINT}:
        return None
    sqrt_price = solana_whirlpool.sqrt_price(pool_data)
    if sqrt_price is None or sqrt_price <= 0:
        return None
    try:
        return solana_direct_swap.marginal_micro_usdc_per_sol(sqrt_price)
    except ValueError:
        return None


class Snapshot:
    """Accounts from one getMultipleAccounts and a later getSlot. A dict is not a snapshot."""

    def __init__(self, accounts: dict, slot: int, tip_slot: int, fetched_at: float):
        self.accounts = accounts
        self.slot = slot
        self.tip_slot = tip_slot
        self.fetched_at = fetched_at


def read_snapshot(transport, keys: list[str], now: float) -> dict:
    """Bind pool, tick, and wallet bytes to the slot the server just read."""
    refused = {"ok": False, "snapshot": None}
    if transport is None or not isinstance(keys, list) or not keys:
        return refused | {"reason": "snapshot_unbound"}
    if isinstance(now, bool) or not isinstance(now, (int, float)):
        return refused | {"reason": "snapshot_clock"}
    try:
        batch = transport({"method": "getMultipleAccounts", "accounts": list(keys)})
        tip = transport({"method": "getSlot"})
    except Exception:
        return refused | {"reason": "snapshot_transport"}
    if not isinstance(batch, dict) or batch.get("status") != 200 or not isinstance(tip, dict) or tip.get("status") != 200:
        return refused | {"reason": "snapshot_status"}
    body = batch.get("body") if isinstance(batch.get("body"), dict) else None
    result = body.get("result") if isinstance(body, dict) and isinstance(body.get("result"), dict) else None
    tip_body = tip.get("body") if isinstance(tip.get("body"), dict) else None
    if result is None or tip_body is None:
        return refused | {"reason": "snapshot_shape"}
    context = result.get("context") if isinstance(result.get("context"), dict) else {}
    slot = context.get("slot")
    tip_slot = tip_body.get("result")
    if isinstance(slot, bool) or not isinstance(slot, int) or isinstance(tip_slot, bool) or not isinstance(tip_slot, int):
        return refused | {"reason": "slot_unproven"}
    lag = tip_slot - slot
    if lag == 0:
        return refused | {"reason": "slot_echo", "slot": slot, "tip_slot": tip_slot}
    if lag < 0 or lag > SLOT_LAG_MAX:
        return refused | {"reason": "slot_lag", "slot": slot, "tip_slot": tip_slot}
    value = result.get("value")
    if not isinstance(value, list) or len(value) != len(keys):
        return refused | {"reason": "snapshot_shape"}
    import base64
    accounts = {}
    for key, item in zip(keys, value):
        if not isinstance(item, dict) or not isinstance(item.get("data"), list) or not isinstance(item.get("owner"), str):
            return refused | {"reason": "account_missing", "account": key}
        try:
            raw = base64.b64decode(item["data"][0], validate=True)
        except Exception:
            return refused | {"reason": "account_encoding", "account": key}
        accounts[key] = (item["owner"], raw)
    return {"ok": True, "reason": None, "snapshot": Snapshot(accounts, slot, tip_slot, float(now))}


def _snapshot_clock(snapshot: Snapshot, now: float) -> str | None:
    lag = snapshot.tip_slot - snapshot.slot
    if lag == 0:
        return "slot_echo"
    if lag < 0 or lag > SLOT_LAG_MAX:
        return "slot_lag"
    if isinstance(now, bool) or not isinstance(now, (int, float)):
        return "snapshot_clock"
    age = float(now) - snapshot.fetched_at
    if age < -5 or age > usdc_valuation.MAX_AGE_S:
        return "snapshot_stale"
    return None


def _hop_privileges(accounts: list[dict], remaining: list[int]) -> str | None:
    """Writable and signer bits for the hop. The inner swap is not a second instruction."""
    if len(remaining) != 12:
        return "privilege"
    def meta(index: int) -> dict:
        return accounts[remaining[index]]
    for index in (1, 11):
        if meta(index).get("isWritable") is True or meta(index).get("isSigner") is True:
            return "privilege"
    if meta(2).get("isSigner") is not True:
        return "privilege"
    for index in (3, 4, 5, 6, 7, 8, 9, 10):
        if meta(index).get("isWritable") is not True or meta(index).get("isSigner") is True:
            return "privilege"
    return None


def _amm_bound(parsed: dict, state: dict, a_to_b: bool, tick_accounts: list[bytes], pool: str) -> dict:
    """Quote from the tick accounts in the message. Empty stand-ins are not a quote."""
    import solana_whirlpool_quote
    arrays = []
    for raw in tick_accounts:
        parsed_array = solana_whirlpool.tick_array_state(raw)
        if parsed_array is None or parsed_array.get("pool") != pool:
            return {"ok": False, "reason": "tick_state_unverified"}
        arrays.append({"start_tick_index": parsed_array["start_tick_index"], "ticks": parsed_array["ticks"]})
    try:
        quoted = solana_whirlpool_quote.quote_exact_in(state, arrays, parsed["in_amount"], a_to_b, 0, 0, 1)
    except solana_whirlpool_quote.QuoteError:
        return {"ok": False, "reason": "amm_quote_failed"}
    if quoted.get("token_est_out", 0) <= 0:
        return {"ok": False, "reason": "amm_quote_zero"}
    if parsed["quoted_out_amount"] > quoted["token_est_out"]:
        return {"ok": False, "reason": "quoted_above_amm", "est_out": quoted["token_est_out"]}
    return {"ok": True, "est_out": quoted["token_est_out"], "min_out": quoted["token_min_out"]}


def _wrap_shape(decoded: solana_tx.Decoded, *, side: str, mint: str) -> dict:
    """Compute budget, wallet ATA creates, the SOL wrap, one Jupiter swap, and the close.

    A buy must transfer exactly the swap input into the wallet's wSOL account and
    sync it. An exit closes that account back to the wallet and does not wrap
    extra SOL. Any other program or a second transfer is rejected.
    """
    wallet = phantom_transport.REQUIRED_WALLET
    wsol = solana_tx.associated_token_address(wallet, solana_tx.NATIVE_MINT, solana_tx.TOKEN_PROGRAM)
    kinds = []
    transfer = None
    swap = None
    compute = []
    for instruction in decoded.instructions:
        program = decoded.static_keys[instruction.program_index]
        keys = [decoded.static_keys[item] for item in instruction.account_indexes]
        data = bytes(instruction.data)
        if program == solana_tx.COMPUTE_BUDGET:
            kinds.append("compute")
            compute.append(data)
            continue
        if program == solana_tx.ASSOCIATED_TOKEN and data == bytes([1]) and len(keys) == 6:
            payer, ata, owner, created_mint, system, token_program = keys
            expected = solana_tx.associated_token_address(owner, created_mint, token_program)
            if payer != wallet or owner != wallet or system != solana_tx.SYSTEM_PROGRAM or token_program != solana_tx.TOKEN_PROGRAM or ata != expected:
                return {"error": "wrap_unbound"}
            if created_mint not in {solana_tx.NATIVE_MINT, mint}:
                return {"error": "wrap_unbound"}
            kinds.append("create")
            continue
        if program == solana_tx.SYSTEM_PROGRAM and solana_tx.system_transfer(data) is not None and len(keys) == 2:
            if transfer is not None or keys[0] != wallet or keys[1] != wsol:
                return {"error": "spend_unbound"}
            transfer = solana_tx.system_transfer(data)
            kinds.append("transfer")
            continue
        if program == solana_tx.TOKEN_PROGRAM and data == bytes([17]) and keys == [wsol]:
            kinds.append("sync")
            continue
        if program == solana_tx.TOKEN_PROGRAM and data == bytes([9]) and keys == [wsol, wallet, wallet]:
            kinds.append("close")
            continue
        if program == solana_jupiter.PROGRAM_ID:
            if swap is not None:
                return {"error": "extra_swap"}
            swap = instruction
            kinds.append("swap")
            continue
        return {"error": "sibling_unverified"}
    body = [kind for kind in kinds if kind != "compute"]
    while body and body[0] == "create":
        body = body[1:]
    if side == "buy":
        if body != ["transfer", "sync", "swap", "close"]:
            return {"error": "wrap_unbound"}
    elif side == "exit":
        if transfer is not None or body != ["swap", "close"]:
            return {"error": "wrap_unbound"}
    else:
        return {"error": "side"}
    if swap is None or "compute" not in kinds:
        return {"error": "swap_missing"}
    return {"swap": swap, "compute": compute, "transfer": transfer, "wsol": wsol}


def decode_message(tx_b64: str, snapshot, *, side: str, mint: str, now: float | None = None, slippage_allowance_bps: int | None = None, price_transport=None) -> dict:
    """Read the wrap and the swap from a slot-bound snapshot. A price dict is not the rate."""
    import base64
    refused = {
        "ok": False, "send": False, "sign": False, "live_ready": False, "message_bound": False,
        "owner_slippage_approved": False, "buy_proven": False, "exit_proven": False,
    }
    if not isinstance(snapshot, Snapshot):
        return refused | {"reason": "snapshot_unbound"}
    moment = time.time() if now is None else now
    clock = _snapshot_clock(snapshot, moment)
    if clock is not None:
        return refused | {"reason": clock, "slot": snapshot.slot, "tip_slot": snapshot.tip_slot}
    try:
        decoded = solana_tx.decode_transaction(base64.b64decode(tx_b64, validate=True))
    except Exception:
        return refused | {"reason": "message"}
    if decoded.lookups:
        return refused | {"reason": "lookup_unresolved", "message_bound": True}
    if decoded.required_signatures != 1:
        return refused | {"reason": "extra_signer", "message_bound": True, "required_signatures": decoded.required_signatures}
    shape = _wrap_shape(decoded, side=side, mint=mint)
    if shape.get("error"):
        return refused | {"reason": shape["error"], "message_bound": True}
    swap = shape["swap"]
    parsed = parse(bytes(swap.data))
    if side == "buy" and shape.get("transfer") != parsed.get("in_amount"):
        return refused | {"reason": "wrap_amount", "message_bound": True, "min_out": parsed.get("min_out")}
    if side == "buy" and decoded.static_keys[swap.account_indexes[1]] != shape["wsol"]:
        return refused | {"reason": "wrap_unbound", "message_bound": True}
    if side == "exit" and decoded.static_keys[swap.account_indexes[2]] != shape["wsol"]:
        return refused | {"reason": "wrap_unbound", "message_bound": True}
    held = _wsol_amount(snapshot.accounts.get(shape["wsol"]))
    if held is None:
        return refused | {"reason": "wsol_balance_unproven", "message_bound": True}
    if held > 0:
        return refused | {"reason": "preexisting_wsol", "message_bound": True, "wrap_verified": False}
    accounts = []
    for index in swap.account_indexes:
        try:
            signer, writable = decoded.account_flags(index)
        except ValueError:
            return refused | {"reason": "accounts", "message_bound": True}
        accounts.append({"pubkey": decoded.static_keys[index], "isSigner": signer, "isWritable": writable})
    result = decode(
        accounts, bytes(swap.data), side=side, mint=mint, account_data=snapshot.accounts,
        compute=shape["compute"], message_bound=True,
        now=moment, slippage_allowance_bps=slippage_allowance_bps, price_transport=price_transport,
    )
    if not isinstance(result, dict):
        return refused | {"reason": "decode_return", "message_bound": True}
    result["wrap_verified"] = result.get("ok") is True
    result["slot"] = snapshot.slot
    result["tip_slot"] = snapshot.tip_slot
    result["owner_slippage_approved"] = False
    return result


def decode(accounts: list[dict], data: bytes, *, side: str, mint: str, account_data: dict, compute: list[bytes], message_bound: bool = False, now: float | None = None, slippage_allowance_bps: int | None = None, price_transport=None) -> dict:
    """One Whirlpool route_v2. SOL/USDC is recognized and is not a memecoin fill."""
    parsed = parse(data)
    result = {
        "ok": False,
        "send": False,
        "sign": False,
        "resend": False,
        "live_ready": False,
        "signer_enabled": False,
        "side": side,
        "mint": mint,
        "min_out": parsed.get("min_out"),
        "min_out_proven": parsed.get("min_out_proven") is True,
        "dex_fee_proven": False,
        "network_fee_proven": False,
        "combined_fee_usd_proven": False,
        "dex_fee_counted_in_input": True,
        "network_fee_added_once": True,
        "message_bound": message_bound,
        "program_rounding_proven": False,
        "diagnostic_sol_usdc_is_not_this_buy": False,
        "wallet": phantom_transport.REQUIRED_WALLET,
    }
    if message_bound is not True:
        return result | {"reason": "message_unbound", "min_out": parsed.get("min_out"), "min_out_proven": parsed.get("min_out_proven") is True}
    if parsed.get("bytes_consumed") is not True or parsed.get("hop") != "Whirlpool":
        return result | {"reason": parsed.get("reason") or "route_v2_bytes"}
    if parsed["platform_fee_bps"] != 0 or parsed["positive_slippage_bps"] != 0:
        return result | {"reason": "platform_fee_unpriced", "min_out": parsed["min_out"], "min_out_proven": True}
    if side not in {"buy", "exit"} or not isinstance(mint, str):
        return result | {"reason": "side"}
    want_source = solana_tx.NATIVE_MINT if side == "buy" else mint
    want_dest = mint if side == "buy" else solana_tx.NATIVE_MINT
    if mint in {solana_tx.NATIVE_MINT, solana_tx.USDC_MINT}:
        result["diagnostic_sol_usdc_is_not_this_buy"] = True
        return result | {"reason": "diagnostic_is_not_a_token_buy", "min_out": parsed["min_out"], "min_out_proven": True}
    bound = _fixed_accounts(accounts)
    if bound.get("ok") is not True:
        return result | {"reason": bound.get("reason"), "min_out": parsed["min_out"], "min_out_proven": True}
    if bound["source_mint"] != want_source or bound["destination_mint"] != want_dest:
        return result | {"reason": "mints", "min_out": parsed["min_out"], "min_out_proven": True}
    if {bound["source_mint"], bound["destination_mint"]} == {solana_tx.NATIVE_MINT, solana_tx.USDC_MINT}:
        result["diagnostic_sol_usdc_is_not_this_buy"] = True
        return result | {"reason": "diagnostic_is_not_a_token_buy", "min_out": parsed["min_out"], "min_out_proven": True}
    keys = [item["pubkey"] for item in accounts]
    remaining = bound["remaining"]
    if len(remaining) != 12 or keys[remaining[0]] != solana_whirlpool.PROGRAM_ID or accounts[remaining[0]].get("isWritable") is True or accounts[remaining[0]].get("isSigner") is True:
        return result | {"reason": "whirlpool_program_prefix", "min_out": parsed["min_out"], "min_out_proven": True}
    try:
        solana_whirlpool.check_swap_accounts(
            keys, remaining[1:], bound["user"], bound["source"], bound["destination"],
            bound["source_mint"], bound["destination_mint"], parsed["a_to_b"] is True, account_data,
        )
    except solana_whirlpool.WhirlpoolError as exc:
        return result | {"reason": exc.category, "min_out": parsed["min_out"], "min_out_proven": True}
    if _hop_privileges(accounts, remaining) is not None:
        return result | {"reason": "privilege", "min_out": parsed["min_out"], "min_out_proven": True}
    pool = keys[remaining[3]]
    state = solana_whirlpool.pool_state(account_data[pool][1])
    fee = None if state is None else whirlpool_fee(parsed["in_amount"], state["fee_rate"])
    if fee is None:
        return result | {"reason": "dex_fee", "min_out": parsed["min_out"], "min_out_proven": True}
    priced = network_fee(compute)
    result["dex_fee_raw"] = fee
    result["dex_fee_unit"] = "lamports" if bound["source_mint"] == solana_tx.NATIVE_MINT else "token_raw"
    result["dex_fee_proven"] = True
    result["network_fee_proven"] = priced.get("network_fee_proven") is True
    result["network_lamports"] = priced.get("network_lamports")
    if priced.get("ok") is not True:
        return result | {"reason": priced.get("reason"), "min_out": parsed["min_out"], "min_out_proven": True}
    moment = time.time() if now is None else now
    prices = pyth_sponsored.public_fetch(moment) if price_transport is None else pyth_sponsored.read_accounts(price_transport, moment)
    sol = prices.get("sol") if isinstance(prices, dict) else None
    if not isinstance(sol, dict) or prices.get("sol_used") is not True or sol.get("fresh") is not True:
        return result | {"reason": prices.get("reason") if isinstance(prices, dict) and prices.get("reason") else "usd_source_unbound", "usd_live": False, "usd_origin": "sol_usd", "exposure_usd": None, "pool_priced_sol": False, "min_out": parsed["min_out"], "min_out_proven": True, "dex_fee_proven": True, "network_fee_proven": True}
    if side != "buy":
        return result | {"reason": "token_fee_unpriced", "usd_live": False, "usd_origin": "sol_usd", "exposure_usd": None, "exposure_cap_applies": False, "token_raw_counted_as_lamports": False, "dex_fee_unit": "token_raw", "min_out": parsed["min_out"]}
    spend_fee = fee + priced["network_lamports"]
    ceiling = pyth_sponsored.lamports_usd(spend_fee, sol)
    result["dex_fee_lamports"] = fee
    if ceiling is None:
        return result | {"reason": "usd_unpriced", "usd_live": False, "usd_origin": "sol_usd", "exposure_usd": None, "min_out": parsed["min_out"]}
    result["combined_fee_usd"] = format(ceiling, "f")
    result["combined_fee_usd_proven"] = True
    result["usd_live"] = True
    result["usd_origin"] = "sol_usd"
    result["pool_priced_sol"] = False
    if ceiling > Decimal("0.05"):
        return result | {"reason": "fee_cap", "min_out": parsed["min_out"]}
    rent = solana_tx.TOKEN_ACCOUNT_RENT * 2
    exposure = pyth_sponsored.lamports_usd(parsed["in_amount"] + rent, sol)
    result["exposure_unit"] = "lamports"
    result["exposure_cap_applies"] = True
    result["exposure_usd"] = None if exposure is None else format(exposure, "f")
    if exposure is None or exposure > Decimal("8.00"):
        return result | {"reason": "exposure_cap" if exposure is not None else "exposure_unpriced", "min_out": parsed["min_out"]}
    tick_bytes = []
    for index in remaining[8:11]:
        record = account_data.get(keys[index])
        if not isinstance(record, tuple):
            return result | {"reason": "tick_state_unverified", "min_out": parsed["min_out"]}
        tick_bytes.append(record[1])
    quoted_ok = _amm_bound(parsed, state, parsed["a_to_b"] is True, tick_bytes, pool)
    if quoted_ok.get("ok") is not True:
        return result | {"reason": quoted_ok.get("reason"), "min_out": parsed["min_out"], "amm_est_out": quoted_ok.get("est_out")}
    result["amm_est_out"] = quoted_ok.get("est_out")
    result["amm_min_out"] = quoted_ok.get("min_out")
    allowance = slippage_allowance_bps
    result["owner_slippage_approved"] = False
    if allowance is None:
        return result | {"reason": "slippage_unapproved", "min_out": parsed["min_out"], "slippage_bps": parsed["slippage_bps"], "buy_proven": False, "exit_proven": False}
    if isinstance(allowance, bool) or not isinstance(allowance, int) or not 0 <= allowance <= 10_000:
        return result | {"reason": "slippage_allowance_unbound", "min_out": parsed["min_out"]}
    result["slippage_allowance_bps"] = allowance
    result["slippage_allowance_source"] = "test_fixture"
    if parsed["slippage_bps"] > allowance:
        return result | {"reason": "slippage_exceeds", "min_out": parsed["min_out"], "slippage_bps": parsed["slippage_bps"]}
    floor = quoted_ok["est_out"] * (10_000 - allowance) // 10_000
    if parsed["min_out"] < floor:
        return result | {"reason": "min_below_amm", "min_out": parsed["min_out"], "allowance_floor": floor}
    return result | {
        "ok": True,
        "reason": None,
        "buy_proven": side == "buy",
        "exit_proven": side == "exit",
        "min_out": parsed["min_out"],
        "slippage_bps": parsed["slippage_bps"],
        "send": False,
        "sign": False,
        "live_ready": False,
        "owner_slippage_approved": False,
    }


def _fixed_accounts(accounts: list[dict]) -> dict:
    if not isinstance(accounts, list) or len(accounts) < 10:
        return {"ok": False, "reason": "accounts"}
    for item in accounts:
        if not isinstance(item, dict) or not isinstance(item.get("pubkey"), str):
            return {"ok": False, "reason": "accounts"}
    keys = [item["pubkey"] for item in accounts]
    if accounts[0].get("isSigner") is not True or keys[0] != phantom_transport.REQUIRED_WALLET:
        return {"ok": False, "reason": "wallet"}
    if accounts[1].get("isWritable") is not True or accounts[2].get("isWritable") is not True:
        return {"ok": False, "reason": "token_accounts"}
    if keys[5] != solana_tx.TOKEN_PROGRAM or keys[6] != solana_tx.TOKEN_PROGRAM:
        return {"ok": False, "reason": "token_program"}
    cursor = 7
    if keys[cursor] != EVENT:
        identified = keys[cursor] == solana_jupiter.PROGRAM_ID and accounts[cursor].get("isWritable") is not True and accounts[cursor].get("isSigner") is not True
        if not identified:
            return {"ok": False, "reason": "unidentified_account"}
        cursor += 1
    if cursor + 1 >= len(keys) or keys[cursor] != EVENT or keys[cursor + 1] != solana_jupiter.PROGRAM_ID:
        return {"ok": False, "reason": "event_authority"}
    if accounts[cursor].get("isWritable") is True or accounts[cursor + 1].get("isWritable") is True:
        return {"ok": False, "reason": "event_authority"}
    return {
        "ok": True,
        "user": keys[0],
        "source": keys[1],
        "destination": keys[2],
        "source_mint": keys[3],
        "destination_mint": keys[4],
        "remaining": list(range(cursor + 2, len(keys))),
    }
