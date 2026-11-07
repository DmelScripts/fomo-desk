"""Direct Orca Whirlpool swap for one SOL to USDC readiness amount.

This module is not mounted. The production route is still the guarded Phantom
CLI, and the Jupiter route_v2 blockers stay in place. The layout is the
published 11-account `swap` instruction, not `swap_v2` and not Jupiter.

Sources used for the bytes:
- programs/whirlpool/src/instructions/swap.rs (account order and the
  exact-in comparison other_amount_threshold > output)
- ts-sdk generated swap.ts at f6d9b9e1 (discriminator and field order)
- programs/whirlpool/src/math/tick_math.rs (sqrt-price bounds)
- programs/whirlpool/src/lib.rs (parameter meanings)
- legacy swap-utils calculateSwapAmountsFromQuote: exact-in sets
  other_amount_threshold from the estimated output reduced by slippage.
  The unprotected SDK default for that field is 0.

The quoted output is not computed from pool liquidity here. A passing byte
check does not mean the pool would pay that amount.
"""

from __future__ import annotations

import base64
import hashlib

import phantom_transport
import solana_tx
import solana_whirlpool

ENABLED = False
INPUT_LAMPORTS = 10_000_000
MARKET = "SOL->USDC"
DISC = hashlib.sha256(b"global:swap").digest()[:8]
PUBLISHED_DISC = bytes([248, 198, 158, 145, 225, 117, 135, 200])
MIN_SQRT_PRICE = 4295048016
MAX_SQRT_PRICE = 79226673515401279992447579055
_DATA_LEN = 42
_TICK_ZERO_SQRT = 18446744073709551616


def status() -> dict:
    return {
        "route": "direct_whirlpool_swap",
        "mounted": False,
        "enabled": ENABLED,
        "production_path": "phantom_cli_unchanged",
        "market": MARKET,
        "input_lamports": INPUT_LAMPORTS,
        "fee_policy": "not_approved",
        "aggregate_loss_policy": False,
        "live_ready": False,
        "output_computed_from_pool": False,
        "adaptive_fee_extra_account": "not_verified",
        "wrapped_sol_setup": "not_in_this_transaction",
    }


def exact_in_min_out(quoted_out: int, slippage_bps: int) -> int:
    """Reduce an estimated output by slippage_bps using integer division.

    Exact-in stores that reduced output in other_amount_threshold. This is the
    direction of the SDK helper. Its Percentage rounding was not on the pages
    read for this change, so this function is the desk rule and it is tested
    against the encoded bytes.
    """
    if isinstance(quoted_out, bool) or isinstance(slippage_bps, bool):
        raise ValueError("quote is not usable")
    if not isinstance(quoted_out, int) or not isinstance(slippage_bps, int):
        raise ValueError("quote is not usable")
    if quoted_out <= 0 or not 1 <= slippage_bps <= 9_999:
        raise ValueError("quote is not usable")
    removed = quoted_out * slippage_bps // 10_000
    minimum = quoted_out - removed
    if minimum <= 0 or minimum >= quoted_out:
        raise ValueError("quote is not usable")
    return minimum


def encode_swap_data(amount: int, threshold: int, sqrt_price_limit: int, specified_input: bool, a_to_b: bool) -> bytes:
    """42-byte swap args: discriminator, u64, u64, u128, two booleans."""
    if DISC != PUBLISHED_DISC:
        raise RuntimeError("swap discriminator drifted")
    if not 0 < amount < 2**64 or not 0 <= threshold < 2**64:
        raise ValueError("swap amount is not usable")
    if not 0 <= sqrt_price_limit < 2**128:
        raise ValueError("sqrt price limit is not usable")
    return (
        DISC
        + int(amount).to_bytes(8, "little")
        + int(threshold).to_bytes(8, "little")
        + int(sqrt_price_limit).to_bytes(16, "little")
        + bytes([1 if specified_input else 0, 1 if a_to_b else 0])
    )


def decode_swap_data(data: bytes) -> dict | None:
    if not isinstance(data, bytes) or len(data) != _DATA_LEN or data[:8] != DISC:
        return None
    specified = data[40]
    direction = data[41]
    if specified not in (0, 1) or direction not in (0, 1):
        return None
    return {
        "amount": int.from_bytes(data[8:16], "little"),
        "other_amount_threshold": int.from_bytes(data[16:24], "little"),
        "sqrt_price_limit": int.from_bytes(data[24:40], "little"),
        "amount_specified_is_input": specified == 1,
        "a_to_b": direction == 1,
    }


def build(pool: str, pool_data: bytes, source: str, dest: str, quoted_out: int, slippage_bps: int, sqrt_price_limit: int, blockhash: str) -> str:
    """Unsigned legacy transaction. The threshold is the formula, not a flag."""
    threshold = exact_in_min_out(quoted_out, slippage_bps)
    direction = _direction(pool_data)
    _require_limit(direction, sqrt_price_limit, solana_whirlpool.sqrt_price(pool_data))
    return transaction_from_fields(
        pool, pool_data, source, dest, INPUT_LAMPORTS, threshold, sqrt_price_limit, True, direction, blockhash,
    )


def transaction_from_fields(pool: str, pool_data: bytes, source: str, dest: str, amount: int, threshold: int, sqrt_price_limit: int, specified_input: bool, a_to_b: bool, blockhash: str, authority: str = phantom_transport.REQUIRED_WALLET, token_authority: str | None = None, copies: int = 1, extra_account: bool = False) -> str:
    """Byte constructor used to test a transaction that does not match the rule."""
    fields = solana_whirlpool.pool_fields(pool_data)
    if fields is None:
        raise ValueError("pool is not usable")
    _mint_a, vault_a, _mint_b, vault_b = fields
    owner_a, owner_b = (source, dest) if a_to_b else (dest, source)
    spacing = int.from_bytes(pool_data[solana_whirlpool._TICK_SPACING:solana_whirlpool._TICK_SPACING + 2], "little")
    current = int.from_bytes(pool_data[solana_whirlpool._TICK_CURRENT:solana_whirlpool._TICK_CURRENT + 4], "little", signed=True)
    base = solana_whirlpool.start_tick_index(current, spacing)
    step = solana_whirlpool.TICK_ARRAY_SIZE * spacing
    starts = [base - i * step for i in range(3)] if a_to_b else [base + i * step for i in range(3)]
    ticks = [solana_whirlpool.tick_array_address(pool, start) for start in starts]
    oracle = solana_whirlpool.oracle_address(pool)
    writable = [pool, owner_a, vault_a, owner_b, vault_b, *ticks]
    readonly = [solana_tx.TOKEN_PROGRAM, oracle, solana_whirlpool.PROGRAM_ID]
    signer = authority if token_authority is None else token_authority
    signers = [authority] if signer == authority else [authority, signer]
    keys = [*signers, *writable, *readonly]
    accounts = [
        keys.index(solana_tx.TOKEN_PROGRAM),
        keys.index(signer),
        keys.index(pool),
        keys.index(owner_a),
        keys.index(vault_a),
        keys.index(owner_b),
        keys.index(vault_b),
        *[keys.index(tick) for tick in ticks],
        keys.index(oracle),
    ]
    if extra_account:
        accounts.append(accounts[-1])
    program = keys.index(solana_whirlpool.PROGRAM_ID)
    data = encode_swap_data(amount, threshold, sqrt_price_limit, specified_input, a_to_b)
    if not 1 <= copies <= 2:
        raise ValueError("transaction is not usable")
    return solana_tx.encode_legacy(
        authority, keys, len(readonly), [(program, accounts, data)] * copies, blockhash, required=len(signers),
    )


def inspect(tx_b64: str, accounts: dict[str, tuple[str, bytes]], quoted_out: int, slippage_bps: int) -> dict:
    """Read the swap instruction. Caller claims do not replace those bytes."""
    body = {
        "enabled": False,
        "mounted": False,
        "market": MARKET,
        "input_lamports": INPUT_LAMPORTS,
        "fee_policy": "not_approved",
        "aggregate_loss_policy": False,
        "live_ready": False,
        "output_computed_from_pool": False,
        "spend_verified": False,
        "slippage_verified": False,
        "wallet_verified": False,
        "min_out": None,
        "error_category": None,
        "byte_checks_passed": False,
    }
    try:
        expected = exact_in_min_out(quoted_out, slippage_bps)
        raw = base64.b64decode(tx_b64, validate=True)
        decoded = solana_tx.decode_transaction(raw)
    except ValueError:
        body["error_category"] = "transaction_unusable"
        return body
    if decoded.fee_payer != phantom_transport.REQUIRED_WALLET or len(decoded.instructions) != 1:
        body["error_category"] = "transaction_shape"
        return body
    instruction = decoded.instructions[0]
    if decoded.static_keys[instruction.program_index] != solana_whirlpool.PROGRAM_ID:
        body["error_category"] = "program_unbound"
        return body
    parsed = decode_swap_data(instruction.data)
    if parsed is None or len(instruction.account_indexes) != 11:
        body["error_category"] = "swap_data_unbound"
        return body
    body["min_out"] = parsed["other_amount_threshold"]
    if not parsed["amount_specified_is_input"] or parsed["amount"] != INPUT_LAMPORTS:
        body["error_category"] = "spend_unbound"
        return body
    if parsed["other_amount_threshold"] != expected:
        body["error_category"] = "slippage_unbound"
        return body
    pool = decoded.static_keys[instruction.account_indexes[2]]
    record = accounts.get(pool)
    if record is None:
        body["error_category"] = "account_unread"
        return body
    pool_data = record[1]
    try:
        direction = _direction(pool_data)
    except ValueError:
        body["error_category"] = "whirlpool_mint_unbound"
        return body
    if parsed["a_to_b"] != direction:
        body["error_category"] = "direction_unbound"
        return body
    if not _limit_ok(direction, parsed["sqrt_price_limit"], solana_whirlpool.sqrt_price(pool_data)):
        body["error_category"] = "sqrt_price_limit"
        return body
    source, dest = _owner_accounts(decoded.static_keys, instruction.account_indexes, direction)
    try:
        solana_whirlpool.check_swap_accounts(
            decoded.static_keys,
            instruction.account_indexes,
            phantom_transport.REQUIRED_WALLET,
            source,
            dest,
            solana_tx.NATIVE_MINT,
            solana_tx.USDC_MINT,
            direction,
            accounts,
        )
    except solana_whirlpool.WhirlpoolError as error:
        body["error_category"] = error.category
        return body
    body["spend_verified"] = True
    body["slippage_verified"] = True
    body["wallet_verified"] = True
    body["byte_checks_passed"] = True
    return body


def _direction(pool_data: bytes) -> bool:
    fields = solana_whirlpool.pool_fields(pool_data or b"")
    if fields is None:
        raise ValueError("pool")
    mint_a, _vault_a, mint_b, _vault_b = fields
    if mint_a == solana_tx.NATIVE_MINT and mint_b == solana_tx.USDC_MINT:
        return True
    if mint_a == solana_tx.USDC_MINT and mint_b == solana_tx.NATIVE_MINT:
        return False
    raise ValueError("mints")


def _require_limit(a_to_b: bool, limit: int, current: int | None) -> None:
    if not _limit_ok(a_to_b, limit, current):
        raise ValueError("sqrt price limit is not usable")


def _limit_ok(a_to_b: bool, limit: int, current: int | None) -> bool:
    if current is None or not MIN_SQRT_PRICE < current < MAX_SQRT_PRICE:
        return False
    if a_to_b:
        return MIN_SQRT_PRICE < limit < current
    return current < limit < MAX_SQRT_PRICE


def _owner_accounts(keys: list[str], indexes: list[int], a_to_b: bool) -> tuple[str, str]:
    owner_a = keys[indexes[3]]
    owner_b = keys[indexes[5]]
    if a_to_b:
        return owner_a, owner_b
    return owner_b, owner_a
