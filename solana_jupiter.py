"""Narrow decoder for Jupiter v6 route instructions.

The program id and account order come from the Jupiter instruction-parser IDL
for JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4. A matching program id is not
enough: the 8-byte discriminator, the route-step enum, the mints, the wallet
authority, and the amounts must all decode. Anything else fails closed.
"""

from __future__ import annotations

import hashlib

import phantom_transport
import solana_tx

PROGRAM_ID = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
_MAX_SLIPPAGE_BPS = 100
_MAX_STEPS = 16

# Anchor discriminators: sha256("global:<name>")[:8].
_SHARED = hashlib.sha256(b"global:shared_accounts_route").digest()[:8]

# Extra bytes after the Swap enum tag, in the order published by the v6 IDL.
# None means the variant carries a RemainingAccountsInfo option.
_SWAP_TAIL: dict[int, int | None] = {index: 0 for index in range(62)}
for index, size in {
    8: 1, 12: 1, 15: 1, 16: 1, 17: 1, 18: 1, 21: 1, 23: 1, 24: 1, 27: 1, 28: 1,
    29: 16, 33: 4, 39: 1, 41: 4, 42: 3, 43: 10, 44: 5, 45: 5, 58: 1, 60: 1, 61: 1,
}.items():
    _SWAP_TAIL[index] = size
_SWAP_TAIL[47] = None


def check_instruction(index: int, instruction: solana_tx.Instruction, keys: list[str], program_ids: list[str]) -> None:
    from solana_inspect import GateError
    data = instruction.data
    if len(data) < 8 or data[:8] != _SHARED:
        raise GateError(
            f"jupiter instruction {index} is not shared_accounts_route",
            "jupiter_instruction_unsupported", program_ids, index, data[0] if data else None,
        )
    if len(instruction.account_indexes) < 9:
        raise GateError(
            f"jupiter instruction {index} is missing its documented accounts",
            "jupiter_accounts_unbound", program_ids, index, None,
        )
    authority = keys[instruction.account_indexes[2]]
    source_mint = keys[instruction.account_indexes[7]]
    destination_mint = keys[instruction.account_indexes[8]]
    if authority != phantom_transport.REQUIRED_WALLET:
        raise GateError(
            f"jupiter instruction {index} authority is not the allowlisted wallet",
            "wallet_unbound", program_ids, index, None,
        )
    if source_mint != solana_tx.NATIVE_MINT or destination_mint != solana_tx.USDC_MINT:
        raise GateError(
            f"jupiter instruction {index} mints are not native SOL to USDC",
            "token_mint_unbound", program_ids, index, None, destination_mint, authority,
        )
    try:
        cursor, steps = _route_plan(data, 9)
        in_amount = int.from_bytes(data[cursor:cursor + 8], "little")
        quoted = int.from_bytes(data[cursor + 8:cursor + 16], "little")
        slippage_bps = int.from_bytes(data[cursor + 16:cursor + 18], "little")
        platform_fee_bps = data[cursor + 18]
    except (ValueError, IndexError):
        raise GateError(
            f"jupiter instruction {index} route is not supported",
            "unsupported_route", program_ids, index, None,
        )
    if cursor + 19 != len(data):
        raise GateError(
            f"jupiter instruction {index} route is not supported",
            "unsupported_route", program_ids, index, None,
        )
    if in_amount <= 0 or in_amount > 10_000_000:
        raise GateError(
            f"jupiter instruction {index} input is outside the 0.01 SOL bound",
            "amount_unbound", program_ids, index, None,
        )
    if quoted <= 0 or slippage_bps > _MAX_SLIPPAGE_BPS or platform_fee_bps != 0:
        raise GateError(
            f"jupiter instruction {index} slippage or fee is outside the request",
            "slippage_unbound", program_ids, index, None,
        )
    instruction.parsed = {
        "instruction": "shared_accounts_route",
        "in_amount": in_amount,
        "quoted_out_amount": str(quoted),
        "slippage_bps": slippage_bps,
        "route_steps": steps,
    }


def _route_plan(data: bytes, cursor: int) -> tuple[int, int]:
    if cursor + 4 > len(data):
        raise ValueError("short")
    count = int.from_bytes(data[cursor:cursor + 4], "little")
    cursor += 4
    if not 1 <= count <= _MAX_STEPS:
        raise ValueError("count")
    for _ in range(count):
        if cursor >= len(data):
            raise ValueError("short")
        variant = data[cursor]
        cursor += 1
        tail = _SWAP_TAIL.get(variant)
        if tail is None and variant != 47:
            raise ValueError("variant")
        if variant == 47:
            cursor = _whirlpool_v2(data, cursor)
        else:
            cursor += tail
        if cursor + 3 > len(data):
            raise ValueError("short")
        percent = data[cursor]
        if not 1 <= percent <= 100:
            raise ValueError("percent")
        cursor += 3
    return cursor, count


def _whirlpool_v2(data: bytes, cursor: int) -> int:
    if cursor + 2 > len(data):
        raise ValueError("short")
    cursor += 1
    flag = data[cursor]
    cursor += 1
    if flag == 0:
        return cursor
    if flag != 1 or cursor + 4 > len(data):
        raise ValueError("option")
    count = int.from_bytes(data[cursor:cursor + 4], "little")
    cursor += 4
    if count > 8:
        raise ValueError("slices")
    for _ in range(count):
        if cursor + 2 > len(data) or data[cursor] > 1:
            raise ValueError("accounts type")
        cursor += 2
    return cursor
