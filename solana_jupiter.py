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
# The anchor name matches discriminator bb64facc31c4af14. jup-ag/instruction-parser
# does not publish this instruction, so the accounts and arguments are not decoded.
ROUTE_V2 = hashlib.sha256(b"global:route_v2").digest()[:8]

# Extra bytes after the Swap enum tag, in the order published by the v6 IDL.
# None means the variant carries a RemainingAccountsInfo option.
_SWAP_TAIL: dict[int, int | None] = {index: 0 for index in range(62)}
for index, size in {
    8: 1, 12: 1, 15: 1, 16: 1, 17: 1, 18: 1, 21: 1, 23: 1, 24: 1, 27: 1, 28: 1,
    29: 16, 33: 4, 39: 1, 41: 4, 42: 3, 43: 10, 44: 5, 45: 5, 58: 1, 60: 1, 61: 1,
}.items():
    _SWAP_TAIL[index] = size
_SWAP_TAIL[47] = None


# Pinned from the program-owned IDL account C88XWfp26heEmDkmfSzeXP7Fd7GQJ2j9dDTUsyiZbUTa.
# Decompressed JSON sha256 e6c2b425ec740922ee91aa7a3278cea07f4bc00a3ed9efa4eca16facb481bfca.
# metadata jupiter 0.1.0. Swap::GoonFiV2 is variant 121 and carries only is_bid.
_GOONFI_V2 = 121
IDL_SHA256 = "e6c2b425ec740922ee91aa7a3278cea07f4bc00a3ed9efa4eca16facb481bfca"
IDL_ACCOUNT = "C88XWfp26heEmDkmfSzeXP7Fd7GQJ2j9dDTUsyiZbUTa"


def check_instruction(index: int, instruction: solana_tx.Instruction, keys: list[str], program_ids: list[str]) -> None:
    from solana_inspect import GateError
    data = instruction.data
    if len(data) >= 8 and data[:8] == ROUTE_V2:
        _check_route_v2(index, instruction, keys, program_ids)
        return
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


def _check_route_v2(index: int, instruction: solana_tx.Instruction, keys: list[str], program_ids: list[str]) -> None:
    """Decode the published route_v2 args. GoonFi accounts stay unverified."""
    from solana_inspect import GateError
    data = instruction.data
    if len(instruction.account_indexes) < 10:
        raise GateError(
            f"jupiter instruction {index} is missing its documented accounts",
            "jupiter_accounts_unbound", program_ids, index, None,
        )
    authority = keys[instruction.account_indexes[0]]
    source_mint = keys[instruction.account_indexes[3]]
    destination_mint = keys[instruction.account_indexes[4]]
    source_program = keys[instruction.account_indexes[5]]
    destination_program = keys[instruction.account_indexes[6]]
    route_program = keys[instruction.account_indexes[9]]
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
    if source_program not in {solana_tx.TOKEN_PROGRAM, solana_tx.TOKEN_2022} or destination_program not in {solana_tx.TOKEN_PROGRAM, solana_tx.TOKEN_2022}:
        raise GateError(
            f"jupiter instruction {index} token program is not documented",
            "jupiter_accounts_unbound", program_ids, index, None,
        )
    if route_program != PROGRAM_ID:
        raise GateError(
            f"jupiter instruction {index} program account is not JUP6",
            "jupiter_accounts_unbound", program_ids, index, None,
        )
    if len(data) != 40:
        raise GateError(
            f"jupiter instruction {index} route is not supported",
            "unsupported_route", program_ids, index, None,
        )
    in_amount = int.from_bytes(data[8:16], "little")
    quoted = int.from_bytes(data[16:24], "little")
    slippage_bps = int.from_bytes(data[24:26], "little")
    platform_fee_bps = int.from_bytes(data[26:28], "little")
    positive_slippage_bps = int.from_bytes(data[28:30], "little")
    count = int.from_bytes(data[30:34], "little")
    variant = data[34]
    is_bid = data[35]
    bps = int.from_bytes(data[36:38], "little")
    if count != 1 or variant != _GOONFI_V2 or is_bid > 1 or not 1 <= bps <= 10_000:
        raise GateError(
            f"jupiter instruction {index} route is not supported",
            "unsupported_route", program_ids, index, variant,
        )
    if in_amount <= 0 or in_amount > 10_000_000:
        raise GateError(
            f"jupiter instruction {index} input is outside the 0.01 SOL bound",
            "amount_unbound", program_ids, index, None,
        )
    if quoted <= 0 or slippage_bps > _MAX_SLIPPAGE_BPS or platform_fee_bps != 0 or positive_slippage_bps != 0:
        raise GateError(
            f"jupiter instruction {index} slippage or fee is outside the request",
            "slippage_unbound", program_ids, index, None,
        )
    instruction.parsed = {
        "instruction": "route_v2",
        "swap": "GoonFiV2",
        "in_amount": in_amount,
        "quoted_out_amount": str(quoted),
        "slippage_bps": slippage_bps,
        "route_steps": 1,
        "dex_accounts_verified": False,
        "idl_sha256": IDL_SHA256,
    }
    raise GateError(
        f"jupiter instruction {index} route_v2 GoonFiV2 accounts are not verified",
        "dex_accounts_unverified", program_ids, index, variant,
    )


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
