"""Orca Whirlpool account checks for one Jupiter route_v2 hop.

The pool field order is programs/whirlpool/src/state/whirlpool.rs. The swap
account order and oracle seed are programs/whirlpool/src/instructions/swap.rs.
Tick arrays are marked checked only inside the handler, so they are not treated
as verified here. Jupiter's IDL does not name its whirlpool signer PDA.
"""

from __future__ import annotations

import hashlib

import solana_tx

PROGRAM_ID = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
LABEL = "Whirlpool"
_DISC = hashlib.sha256(b"account:Whirlpool").digest()[:8]
_MINT_A = 8 + 93
_VAULT_A = 8 + 125
_MINT_B = 8 + 173
_VAULT_B = 8 + 205
_MIN_LEN = 8 + 261


class WhirlpoolError(Exception):
    def __init__(self, reason: str, category: str):
        self.reason = reason
        self.category = category


def reference_pool(mint_a: str, vault_a: str, mint_b: str, vault_b: str) -> bytes:
    """Synthetic pool using the published field offsets. Not an on-chain account."""
    raw = bytearray(_MIN_LEN)
    raw[:8] = _DISC
    raw[_MINT_A:_MINT_A + 32] = solana_tx.b58decode(mint_a)
    raw[_VAULT_A:_VAULT_A + 32] = solana_tx.b58decode(vault_a)
    raw[_MINT_B:_MINT_B + 32] = solana_tx.b58decode(mint_b)
    raw[_VAULT_B:_VAULT_B + 32] = solana_tx.b58decode(vault_b)
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
    derived, _bump = _find_program_address(
        [b"oracle", solana_tx.b58decode(pool)],
        solana_tx.b58decode(PROGRAM_ID),
    )
    return solana_tx.b58encode(derived)


def check_swap_accounts(keys: list[str], remaining: list[int], user_source: str, user_dest: str, source_mint: str, dest_mint: str, a_to_b: bool, owner: str | None, pool_data: bytes | None) -> None:
    """Check the published Orca swap account positions against a pool account."""
    if len(remaining) != 11:
        raise WhirlpoolError("whirlpool swap account count is not the published swap layout", "whirlpool_accounts_unbound")
    if keys[remaining[0]] != solana_tx.TOKEN_PROGRAM:
        raise WhirlpoolError("whirlpool token program is not the SPL token program", "whirlpool_accounts_unbound")
    pool = keys[remaining[2]]
    if owner != PROGRAM_ID or pool_fields(pool_data or b"") is None:
        raise WhirlpoolError("whirlpool pool account is not owned by the Whirlpool program", "whirlpool_pool_unbound")
    mint_a, vault_a, mint_b, vault_b = pool_fields(pool_data)
    if a_to_b:
        expect_in, expect_out = mint_a, mint_b
        owner_a, owner_b = user_source, user_dest
    else:
        expect_in, expect_out = mint_b, mint_a
        owner_a, owner_b = user_dest, user_source
    if expect_in != source_mint or expect_out != dest_mint:
        raise WhirlpoolError("whirlpool pool mints do not match the route direction", "whirlpool_mint_unbound")
    if keys[remaining[3]] != owner_a or keys[remaining[5]] != owner_b:
        raise WhirlpoolError("whirlpool owner token accounts do not match the route", "whirlpool_owner_unbound")
    if keys[remaining[4]] != vault_a or keys[remaining[6]] != vault_b:
        raise WhirlpoolError("whirlpool vault accounts do not match the pool", "whirlpool_vault_unbound")
    if keys[remaining[10]] != oracle_address(pool):
        raise WhirlpoolError("whirlpool oracle is not the published PDA", "whirlpool_oracle_unbound")


def _find_program_address(seeds: list[bytes], program: bytes) -> tuple[bytes, int]:
    for bump in range(255, -1, -1):
        digest = hashlib.sha256(b"".join(seeds) + bytes([bump]) + program + b"ProgramDerivedAddress").digest()
        if not _on_curve(digest):
            return digest, bump
    raise WhirlpoolError("whirlpool oracle PDA cannot be derived", "whirlpool_oracle_unbound")


def _on_curve(point: bytes) -> bool:
    """Edwards25519 membership, the same check web3.js uses for PDAs."""
    if len(point) != 32:
        return False
    prime = (1 << 255) - 19
    y = int.from_bytes(point, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= prime:
        return False
    d = (-121665 * pow(121666, prime - 2, prime)) % prime
    y2 = y * y % prime
    numerator = (y2 - 1) % prime
    denominator = (d * y2 + 1) % prime
    if denominator == 0:
        return False
    x2 = numerator * pow(denominator, prime - 2, prime) % prime
    if x2 == 0:
        return sign == 0
    return pow(x2, (prime - 1) // 2, prime) == 1
