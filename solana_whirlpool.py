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

import solana_tx

PROGRAM_ID = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
LABEL = "Whirlpool"
TICK_ARRAY_SIZE = 88
MAX_TICK_INDEX = 443636
MIN_TICK_INDEX = -443636
_DISC = hashlib.sha256(b"account:Whirlpool").digest()[:8]
_TICK_DISC = hashlib.sha256(b"account:TickArray").digest()[:8]
_ORACLE_DISC = hashlib.sha256(b"account:Oracle").digest()[:8]
_ORACLE_LEN = 8 + 32 + 8 + 34 + 44 + 128
_TICK_LEN = 113
_MINT_A = 8 + 93
_VAULT_A = 8 + 125
_MINT_B = 8 + 173
_VAULT_B = 8 + 205
_TICK_SPACING = 8 + 33
_FEE_TIER_INDEX = _TICK_SPACING + 2
_TICK_CURRENT = 8 + 73
_MIN_LEN = 8 + 261 + 384
_TICK_ARRAY_LEN = 8 + 4 + TICK_ARRAY_SIZE * _TICK_LEN + 32


class WhirlpoolError(Exception):
    def __init__(self, reason: str, category: str):
        self.reason = reason
        self.category = category


def reference_pool(mint_a: str, vault_a: str, mint_b: str, vault_b: str, tick_spacing: int = 8, tick_current: int = 16, sqrt_price: int = 0, *, fee_rate: int = 0, liquidity: int = 0) -> bytes:
    """Synthetic pool using the published field offsets. Not an on-chain account."""
    raw = bytearray(_MIN_LEN)
    raw[:8] = _DISC
    raw[_TICK_SPACING:_TICK_SPACING + 2] = int(tick_spacing).to_bytes(2, "little")
    raw[_TICK_SPACING + 2:_TICK_SPACING + 4] = int(tick_spacing).to_bytes(2, "little")
    raw[_TICK_SPACING + 4:_TICK_SPACING + 6] = int(fee_rate).to_bytes(2, "little")
    raw[_TICK_CURRENT - 32:_TICK_CURRENT - 16] = int(liquidity).to_bytes(16, "little")
    raw[_TICK_CURRENT - 16:_TICK_CURRENT] = int(sqrt_price).to_bytes(16, "little")
    raw[_TICK_CURRENT:_TICK_CURRENT + 4] = int(tick_current).to_bytes(4, "little", signed=True)
    raw[_MINT_A:_MINT_A + 32] = solana_tx.b58decode(mint_a)
    raw[_VAULT_A:_VAULT_A + 32] = solana_tx.b58decode(vault_a)
    raw[_MINT_B:_MINT_B + 32] = solana_tx.b58decode(mint_b)
    raw[_VAULT_B:_VAULT_B + 32] = solana_tx.b58decode(vault_b)
    return bytes(raw)


def pool_state(data: bytes) -> dict | None:
    """Fee, liquidity, price, and the adaptive-fee seed. Seed equal to tick spacing is classic."""
    if pool_fields(data) is None or sqrt_price(data) is None:
        return None
    spacing = int.from_bytes(data[_TICK_SPACING:_TICK_SPACING + 2], "little")
    seed = int.from_bytes(data[_TICK_SPACING + 2:_TICK_SPACING + 4], "little")
    current = int.from_bytes(data[_TICK_CURRENT:_TICK_CURRENT + 4], "little", signed=True)
    if spacing == 0 or not MIN_TICK_INDEX <= current <= MAX_TICK_INDEX:
        return None
    return {
        "fee_rate": int.from_bytes(data[_TICK_SPACING + 4:_TICK_SPACING + 6], "little"),
        "liquidity": int.from_bytes(data[_TICK_CURRENT - 32:_TICK_CURRENT - 16], "little"),
        "sqrt_price": sqrt_price(data),
        "tick_current_index": current,
        "tick_spacing": spacing,
        "fee_tier_seed": seed,
        "adaptive_fee": seed != spacing,
    }


def tick_array_state(data: bytes) -> dict | None:
    """Initialized flag and liquidity_net from each published 113-byte tick."""
    if not isinstance(data, bytes) or len(data) != _TICK_ARRAY_LEN or data[:8] != _TICK_DISC:
        return None
    ticks = []
    offset = 12
    for _ in range(TICK_ARRAY_SIZE):
        if data[offset] not in (0, 1):
            return None
        ticks.append({
            "initialized": data[offset] == 1,
            "liquidity_net": int.from_bytes(data[offset + 1:offset + 17], "little", signed=True),
        })
        offset += _TICK_LEN
    return {
        "start_tick_index": int.from_bytes(data[8:12], "little", signed=True),
        "ticks": ticks,
        "pool": solana_tx.b58encode(data[-32:]),
    }


def sqrt_price(data: bytes) -> int | None:
    """The u128 immediately before tick_current_index in the published pool struct."""
    if len(data) < _TICK_CURRENT or data[:8] != _DISC:
        return None
    return int.from_bytes(data[_TICK_CURRENT - 16:_TICK_CURRENT], "little")


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
    raw[108] = 1  # AccountState::Initialized, not an uninitialized token account.
    return bytes(raw)


def pool_fields(data: bytes) -> tuple[str, str, str, str] | None:
    if not isinstance(data, bytes) or len(data) != _MIN_LEN or data[:8] != _DISC:
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
    # initialize_tick_array.rs uses start_tick_index.to_string().as_bytes().
    # Binary i32 bytes derive a different, nonexistent account.
    derived, _bump = solana_tx.find_program_address(
        [b"tick_array", solana_tx.b58decode(pool), str(int(start)).encode("ascii")],
        solana_tx.b58decode(PROGRAM_ID),
    )
    return solana_tx.b58encode(derived)


def start_tick_index(tick: int, spacing: int) -> int:
    span = TICK_ARRAY_SIZE * spacing
    return tick // span * span


def pool_uses_adaptive_fees(pool_data: bytes) -> bool:
    """Published SDK discriminator: fee-tier index differs from tick spacing."""
    if pool_fields(pool_data) is None:
        raise WhirlpoolError("whirlpool pool layout is not usable", "whirlpool_pool_unbound")
    spacing = int.from_bytes(pool_data[_TICK_SPACING:_TICK_SPACING + 2], "little")
    fee_tier = int.from_bytes(pool_data[_FEE_TIER_INDEX:_FEE_TIER_INDEX + 2], "little")
    return spacing != fee_tier


def check_oracle_account(pool: str, pool_data: bytes, accounts: dict[str, tuple[str, bytes]], writable: bool, timestamp: int | None = None) -> None:
    """Validate the oracle snapshot; this does not compute an adaptive quote.

    OracleAccessor accepts a system-owned empty account for fixed-fee pools.
    An initialized oracle must be program-owned, pool-bound and writable.
    Timestamp must come from the same trusted snapshot used for a quote.
    """
    record = accounts.get(oracle_address(pool))
    if record is None:
        raise WhirlpoolError("whirlpool oracle was not read", "account_unread")
    owner, data = record
    adaptive = pool_uses_adaptive_fees(pool_data)
    if not adaptive:
        if owner != solana_tx.SYSTEM_PROGRAM or data != b"":
            raise WhirlpoolError("fixed-fee oracle is not an uninitialized system account", "whirlpool_oracle_unbound")
        return
    if (
        owner != PROGRAM_ID or not isinstance(data, bytes) or len(data) != _ORACLE_LEN
        or data[:8] != _ORACLE_DISC or data[8:40] != solana_tx.b58decode(pool)
    ):
        raise WhirlpoolError("adaptive oracle layout, owner or pool does not match", "whirlpool_oracle_unbound")
    if not writable:
        raise WhirlpoolError("adaptive oracle is not writable", "whirlpool_oracle_readonly")
    if type(timestamp) is not int or timestamp < 0:
        raise WhirlpoolError("oracle timestamp was not bound to the quote snapshot", "whirlpool_oracle_timestamp_unbound")
    if int.from_bytes(data[40:48], "little") > timestamp:
        raise WhirlpoolError("whirlpool trading is not enabled at the snapshot timestamp", "whirlpool_trade_disabled")


def check_swap_accounts(keys: list[str], remaining: list[int], user_authority: str, user_source: str, user_dest: str, source_mint: str, dest_mint: str, a_to_b: bool, accounts: dict[str, tuple[str, bytes] | None], *, planned_creations: dict[str, tuple[str, str]] | None = None) -> None:
    """Check one swap. Direct callers may supply validated prior ATA creates.

    A planned creation only covers an explicitly absent RPC account (None),
    never an unread key or invalid existing account. The caller must first
    validate the actual create instructions, their order, payer and metadata.
    Jupiter callers do not supply a creation plan and retain strict reads.
    """
    # Orca's swap struct is 11 accounts. A live Jupiter route_v2 had 12 remaining
    # accounts. No published Jupiter layout says the extra one is a program-id
    # prefix, so a longer tail is rejected rather than stripped.
    if len(remaining) != 11:
        raise WhirlpoolError("whirlpool swap account count is not the published swap layout", "whirlpool_accounts_unbound")
    if any(type(index) is not int or not 0 <= index < len(keys) for index in remaining):
        raise WhirlpoolError("whirlpool swap account index is invalid", "whirlpool_accounts_unbound")
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
    creations = planned_creations or {}
    _token_identity(accounts, user_source, source_mint, user_authority, "whirlpool_owner_unbound", planned_creation=creations.get(user_source))
    _token_identity(accounts, user_dest, dest_mint, user_authority, "whirlpool_owner_unbound", planned_creation=creations.get(user_dest))
    _token_identity(accounts, vault_a, mint_a, pool, "whirlpool_vault_unbound")
    _token_identity(accounts, vault_b, mint_b, pool, "whirlpool_vault_unbound")
    _tick_arrays(keys, remaining[7:10], pool, spacing, current, a_to_b, accounts)
    if input_mint != source_mint or output_mint != dest_mint:
        raise WhirlpoolError("whirlpool input mint is not the route input", "whirlpool_mint_unbound")


def _token_identity(accounts: dict[str, tuple[str, bytes] | None], address: str, mint: str, owner: str | None, category: str, *, planned_creation: tuple[str, str] | None = None) -> None:
    record = accounts.get(address)
    if address in accounts and record is None and planned_creation is not None:
        if (
            owner is not None and planned_creation == (mint, owner)
            and address == solana_tx.associated_token_address(owner, mint, solana_tx.TOKEN_PROGRAM)
        ):
            return
        raise WhirlpoolError("missing token account is not bound to its ATA creation", category)
    if record is None or record[1] is None:
        raise WhirlpoolError("whirlpool token account was not read", "account_unread")
    if record[0] != solana_tx.TOKEN_PROGRAM or len(record[1]) != 165 or record[1][108] != 1:
        raise WhirlpoolError("whirlpool token account is not an initialized SPL token account", category)
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
        minimum = MIN_TICK_INDEX // span * span
        return start == minimum
    if start > MAX_TICK_INDEX:
        return False
    return start % span == 0


