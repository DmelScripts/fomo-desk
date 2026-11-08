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

from decimal import Decimal

import phantom_transport
import solana_jupiter
import solana_tx
import solana_whirlpool
import usdc_valuation

EVENT = "D8cy77BBepLMngZx6ZukaTff5hCt1HrWyKk3Hnd9oitf"
DISC = bytes([187, 100, 250, 204, 49, 196, 175, 20])
WHIRLPOOL = 17
FEE_DENOMINATOR = 1_000_000
BASE_FEE_LAMPORTS = 5_000
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


def minimum_out(quoted: int, slippage_bps: int) -> int | None:
    """Floor enforced by the instruction fields. A JSON threshold is not read."""
    if isinstance(quoted, bool) or isinstance(slippage_bps, bool):
        return None
    if not isinstance(quoted, int) or not isinstance(slippage_bps, int):
        return None
    if quoted <= 0 or not 0 <= slippage_bps < 10_000:
        return None
    slip = quoted * slippage_bps // 10_000
    if slip >= quoted:
        return None
    return quoted - slip


def parse(data: bytes) -> dict:
    refused = {"ok": False, "min_out_proven": False, "send": False, "live_ready": False}
    if not isinstance(data, (bytes, bytearray)) or len(data) < 34 or bytes(data[:8]) != DISC:
        return refused | {"reason": "route_v2_bytes"}
    quoted = int.from_bytes(data[16:24], "little")
    slippage = int.from_bytes(data[24:26], "little")
    floor = minimum_out(quoted, slippage)
    if floor is None:
        return refused | {"reason": "min_out", "quoted_out_amount": quoted, "slippage_bps": slippage}
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


def network_fee(instructions: list[bytes]) -> dict:
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
    priority = (price * limit + 999_999) // 1_000_000
    return {
        "ok": True,
        "reason": None,
        "network_fee_proven": True,
        "compute_unit_limit": limit,
        "micro_lamports_per_cu": price,
        "priority_lamports": priority,
        "base_lamports": BASE_FEE_LAMPORTS,
        "network_lamports": priority + BASE_FEE_LAMPORTS,
    }


def whirlpool_fee(amount_in: int, fee_rate: int) -> int | None:
    """Ceiling of the Whirlpool input fee. fee_rate is hundredths of a basis point."""
    if isinstance(amount_in, bool) or isinstance(fee_rate, bool):
        return None
    if not isinstance(amount_in, int) or not isinstance(fee_rate, int):
        return None
    if amount_in <= 0 or fee_rate < 0 or fee_rate > FEE_DENOMINATOR:
        return None
    return (amount_in * fee_rate + FEE_DENOMINATOR - 1) // FEE_DENOMINATOR


def decode(accounts: list[dict], data: bytes, *, side: str, mint: str, account_data: dict, compute: list[bytes], sol_micro_usdc: int | None, quote: dict | None) -> dict:
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
        "diagnostic_sol_usdc_is_not_this_buy": False,
        "wallet": phantom_transport.REQUIRED_WALLET,
    }
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
    pool = keys[remaining[3]]
    state = solana_whirlpool.pool_state(account_data[pool][1])
    fee = None if state is None else whirlpool_fee(parsed["in_amount"], state["fee_rate"])
    if fee is None:
        return result | {"reason": "dex_fee", "min_out": parsed["min_out"], "min_out_proven": True}
    priced = network_fee(compute)
    result["dex_fee_lamports"] = fee
    result["dex_fee_proven"] = True
    result["network_fee_proven"] = priced.get("network_fee_proven") is True
    result["network_lamports"] = priced.get("network_lamports")
    if priced.get("ok") is not True:
        return result | {"reason": priced.get("reason"), "min_out": parsed["min_out"], "min_out_proven": True}
    if isinstance(sol_micro_usdc, bool) or not isinstance(sol_micro_usdc, int) or sol_micro_usdc <= 0 or not isinstance(quote, dict):
        return result | {"reason": "usd_unpriced", "min_out": parsed["min_out"], "min_out_proven": True, "dex_fee_proven": True, "network_fee_proven": True}
    micro = (fee + priced["network_lamports"]) * sol_micro_usdc // 1_000_000
    ceiling = usdc_valuation.usd_ceiling(micro, quote)
    if ceiling is None:
        return result | {"reason": quote.get("reason") or "usd_unpriced", "min_out": parsed["min_out"], "min_out_proven": True, "dex_fee_proven": True, "network_fee_proven": True}
    result["combined_fee_usd"] = format(ceiling, "f")
    result["combined_fee_usd_proven"] = True
    if ceiling > Decimal("0.05"):
        return result | {"reason": "fee_cap", "min_out": parsed["min_out"]}
    return result | {
        "ok": True,
        "reason": None,
        "decoded": True,
        "min_out": parsed["min_out"],
        "min_out_proven": True,
        "in_amount": parsed["in_amount"],
        "quoted_out_amount": parsed["quoted_out_amount"],
        "slippage_bps": parsed["slippage_bps"],
        "a_to_b": parsed["a_to_b"],
        "fee_micro_usdc": micro,
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
        if accounts[cursor].get("isWritable") is True or accounts[cursor].get("isSigner") is True:
            return {"ok": False, "reason": "optional_account_writable"}
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
