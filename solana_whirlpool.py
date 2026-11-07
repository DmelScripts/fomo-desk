"""Orca Whirlpool account checks for one Jupiter route_v2 hop.

Pool fields are programs/whirlpool/src/state/whirlpool.rs. Swap account order,
the oracle seed, and the user token-authority are
programs/whirlpool/src/instructions/swap.rs. Tick arrays are
programs/whirlpool/src/state/fixed_tick_array.rs and tick.rs: 88 ticks, PDA
["tick_array", whirlpool, start_tick_index], and the current array must
contain tick_current_index. route_v2's published signer is the user, not a
Jupiter PDA. The pinned Jupiter IDL does not name a whirlpool signer seed.
"""

from __future__ import annotations

import hashlib
import math

import solana_tx

PROGRAM_ID = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
LABEL = "Whirlpool"
TICK_ARRAY_SIZE = 88
MAX_TICK_INDEX = 443636
MIN_TICK_INDEX = -443636
_DISC = hashlib.sha256(b"account:Whirlpool").digest()[:8]
_TICK_DISC = hashlib.sha256(b"account:TickArray").digest()[:8]
_TICK_LEN = 113
_MINT_A = 8 + 93
_VAULT_A = 8 + 125
_MINT_B = 8 + 173
_VAULT_B = 8 + 205
_TICK_SPACING = 8 + 33
_TICK_CURRENT = 8 + 73
_MIN_LEN = 8 + 261
_TICK_ARRAY_LEN = 8 + 4 + TICK_ARRAY_SIZE * _TICK_LEN + 32


class WhirlpoolError(Exception):
    def __init__(self, reason: str, category: str):
        self.reason = reason
        self.category = category


def reference_pool(mint_a: str, vault_a: str, mint_b: str, vault_b: str, tick_spacing: int = 8, tick_current: int = 16) -> bytes:
    """Synthetic pool using the published field offsets. Not an on-chain account."""
    raw = bytearray(_MIN_LEN)
    raw[:8] = _DISC
    raw[_TICK_SPACING:_TICK_SPACING + 2] = int(tick_spacing).to_bytes(2, "little")
    raw[_TICK_CURRENT:_TICK_CURRENT + 4] = int(tick_current).to_bytes(4, "little", signed=True)
    raw[_MINT_A:_MINT_A + 32] = solana_tx.b58decode(mint_a)
    raw[_VAULT_A:_VAULT_A + 32] = solana_tx.b58decode(vault_a)
    raw[_MINT_B:_MINT_B + 32] = solana_tx.b58decode(mint_b)
    raw[_VAULT_B:_VAULT_B + 32] = solana_tx.b58decode(vault_b)
    return bytes(raw)


def reference_tick_array(pool: str, start: int) -> bytes:
    raw = bytearray(_TICK_ARRAY_LEN)
    raw[:8] = _TICK_DISC
    raw[8:12] = int(start).to_bytes(4, "little", signed=True)
    raw[-32:] = solana_tx.b58decode(pool)
    return bytes(raw)


def reference_token_account(mint: str, owner: str) -> bytes:
    raw = bytearray(165)
    raw[0:32] = solana_tx.b58decode(mint)
    raw[32:64] = solana_tx.b58decode(owner)
    return bytes(raw)


def pool_fields(data: bytes) -> tuple[str, str, str, str] | None:
    if len(data) < _MIN_LEN or data[:8] != _DISC:
        return None
    return (
        solana_tx.b58encode(data[_MINT_A:_MINT_A + 32]),
        solana_tx.b58encode(data[_VAULT_A:_VAULT_A + 32]),
        solana_tx.b58encode(data[_MINT_B:_MINT_B + 32]),
        solana_tx.b58encode(data[_VAULT_B:_VAULT_B + 32]),
    )


def oracle_address(pool: str) -> str:
    derived, _bump = solana_tx.find_program_address(
        [b"oracle", solana_tx.b58decode(pool)],
        solana_tx.b58decode(PROGRAM_ID),
    )
    return solana_tx.b58encode(derived)


def tick_array_address(pool: str, start: int) -> str:
    derived, _bump = solana_tx.find_program_address(
        [b"tick_array", solana_tx.b58decode(pool), int(start).to_bytes(4, "little", signed=True)],
        solana_tx.b58decode(PROGRAM_ID),
    )
    return solana_tx.b58encode(derived)


def start_tick_index(tick: int, spacing: int) -> int:
    span = TICK_ARRAY_SIZE * spacing
    return math.floor(tick / span) * span


def check_swap_accounts(keys: list[str], remaining: list[int], user_authority: str, user_source: str, user_dest: str, source_mint: str, dest_mint: str, a_to_b: bool, accounts: dict[str, tuple[str, bytes]]) -> None:
    """Check one Orca swap account list. route_v2's signer is the user."""
    # Orca's swap struct is 11 accounts. A live Jupiter route_v2 had 12 remaining
    # accounts. No published Jupiter layout says the extra one is a program-id
    # prefix, so a longer tail is rejected rather than stripped.
    if len(remaining) != 11:
        raise WhirlpoolError("whirlpool swap account count is not the published swap layout", "whirlpool_accounts_unbound")
    if keys[remaining[0]] != solana_tx.TOKEN_PROGRAM:
        raise WhirlpoolError("whirlpool token program is not the SPL token program", "token_2022_unsupported" if keys[remaining[0]] == solana_tx.TOKEN_2022 else "whirlpool_accounts_unbound")
    if keys[remaining[1]] != user_authority:
        raise WhirlpoolError("whirlpool token authority is not the route_v2 user signer", "whirlpool_authority_unbound")
    pool = keys[remaining[2]]
    owner, pool_data = accounts.get(pool, (None, None))
    if owner != PROGRAM_ID or pool_fields(pool_data or b"") is None:
        raise WhirlpoolError("whirlpool pool account is not owned by the Whirlpool program", "whirlpool_pool_unbound")
    mint_a, vault_a, mint_b, vault_b = pool_fields(pool_data)
    spacing = int.from_bytes(pool_data[_TICK_SPACING:_TICK_SPACING + 2], "little")
    current = int.from_bytes(pool_data[_TICK_CURRENT:_TICK_CURRENT + 4], "little", signed=True)
    if spacing == 0:
        raise WhirlpoolError("whirlpool tick spacing is zero", "whirlpool_tick_array_unbound")
    if a_to_b:
        expect_in, expect_out = mint_a, mint_b
        owner_a, owner_b = user_source, user_dest
        input_mint, output_mint = mint_a, mint_b
    else:
        expect_in, expect_out = mint_b, mint_a
        owner_a, owner_b = user_dest, user_source
        input_mint, output_mint = mint_b, mint_a
    if expect_in != source_mint or expect_out != dest_mint:
        raise WhirlpoolError("whirlpool pool mints do not match the route direction", "whirlpool_mint_unbound")
    if keys[remaining[3]] != owner_a or keys[remaining[5]] != owner_b:
        raise WhirlpoolError("whirlpool owner token accounts do not match the route", "whirlpool_owner_unbound")
    if keys[remaining[4]] != vault_a or keys[remaining[6]] != vault_b:
        raise WhirlpoolError("whirlpool vault accounts do not match the pool", "whirlpool_vault_unbound")
    if keys[remaining[10]] != oracle_address(pool):
        raise WhirlpoolError("whirlpool oracle is not the published PDA", "whirlpool_oracle_unbound")
    _token_identity(accounts, user_source, source_mint, user_authority, "whirlpool_owner_unbound")
    _token_identity(accounts, user_dest, dest_mint, user_authority, "whirlpool_owner_unbound")
    _token_identity(accounts, vault_a, mint_a, None, "whirlpool_vault_unbound")
    _token_identity(accounts, vault_b, mint_b, None, "whirlpool_vault_unbound")
    _tick_arrays(keys, remaining[7:10], pool, spacing, current, a_to_b, accounts)
    if input_mint != source_mint or output_mint != dest_mint:
        raise WhirlpoolError("whirlpool input mint is not the route input", "whirlpool_mint_unbound")


def _token_identity(accounts: dict[str, tuple[str, bytes]], address: str, mint: str, owner: str | None, category: str) -> None:
    record = accounts.get(address)
    if record is None or record[1] is None:
        raise WhirlpoolError("whirlpool token account was not read", "account_unread")
    fields = solana_tx.token_account_fields(record[1])
    if fields is None or fields[0] != mint or (owner is not None and fields[1] != owner):
        raise WhirlpoolError("whirlpool token account mint or owner does not match", category)


def _tick_arrays(keys: list[str], indexes: list[int], pool: str, spacing: int, current: int, a_to_b: bool, accounts: dict[str, tuple[str, bytes]]) -> None:
    base = start_tick_index(current, spacing)
    step = TICK_ARRAY_SIZE * spacing
    starts = [base - i * step for i in range(3)] if a_to_b else [base + i * step for i in range(3)]
    if not (starts[0] <= current < starts[0] + step):
        raise WhirlpoolError("current tick is outside the first tick array", "whirlpool_tick_array_unbound")
    for index, start in zip(indexes, starts):
        if not _valid_start(start, spacing):
            raise WhirlpoolError("tick array start is outside the published range", "whirlpool_tick_array_unbound")
        address = keys[index]
        if address != tick_array_address(pool, start):
            raise WhirlpoolError("tick array address is not the published PDA", "whirlpool_tick_array_unbound")
        owner, data = accounts.get(address, (None, None))
        if owner != PROGRAM_ID or data is None or len(data) != _TICK_ARRAY_LEN or data[:8] != _TICK_DISC:
            raise WhirlpoolError("tick array account is not a fixed Whirlpool tick array", "whirlpool_tick_array_unbound")
        found = int.from_bytes(data[8:12], "little", signed=True)
        bound = solana_tx.b58encode(data[-32:])
        if found != start or bound != pool:
            raise WhirlpoolError("tick array is not bound to this pool and start tick", "whirlpool_tick_array_unbound")


def _valid_start(start: int, spacing: int) -> bool:
    span = TICK_ARRAY_SIZE * spacing
    if span <= 0:
        return False
    if start < MIN_TICK_INDEX:
        minimum = MIN_TICK_INDEX - (MIN_TICK_INDEX % span + span)
        return start == minimum
    if start > MAX_TICK_INDEX:
        return False
    return start % span == 0


