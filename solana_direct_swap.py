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

The quoted output is computed from the pool fee, liquidity, sqrt price, and
the tick arrays' liquidity_net fields. It is not a caller-supplied number.
A passing byte check is still not a live quote of the mainnet pool: its tick
arrays were not read. Wrapping SOL is the create, transfer, sync, and close
sequence around that swap. The route stays disabled.
"""

from __future__ import annotations

import base64
import hashlib

import phantom_transport
import solana_tx
import solana_whirlpool
import solana_whirlpool_quote

# docs.orca.so names this tick-spacing-8 SOL/USDC pool. The same address is the
# PDA ["whirlpool", mainnet config, wSOL, USDC, spacing 8] under the Whirlpool
# program. One confirmed getAccountInfo was read after that match.
SOL_USDC_POOL = "7qbRF6YsyGuLUVs6Y1q64bdVrfe4ZcUUz1JRdoVNUJnm"
SOL_USDC_CONFIG = "2LecshUwdy9xi7meFgHtFJQNSKk4KdTrcpvaB56dP2NQ"
SOL_USDC_OBSERVATION = {
    "context_slot": 466540548,
    "tick_spacing": 8,
    "fee_tier_seed": 8,
    "adaptive_fee": False,
    "fee_rate": 500,
    "protocol_fee_rate": 1300,
    "mint_a": solana_tx.NATIVE_MINT,
    "mint_b": solana_tx.USDC_MINT,
    "vault_a": "9RfZwn2Prux6QesG1Noo4HzMEBv3rPndJ2bN2Wwd6a7p",
    "vault_b": "BVNo8ftg2LkkssnWT4ZWdtoFaevnfD6ExYeramwM27pe",
    "liquidity": 576950119138,
    "sqrt_price": 5530728732878665336,
    "tick_current_index": -24093,
    "data_len": 653,
    "discriminator_ok": True,
    "tick_arrays_read": False,
}

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
        "adaptive_fee_extra_account": "classic_no_tail" if SOL_USDC_OBSERVATION["adaptive_fee"] is False else "oracle_only",
        "wrapped_sol_setup": "create_transfer_sync_close",
        "live_quote": "blocked_tick_arrays_not_read",
        "sol_usdc_pool": SOL_USDC_POOL,
        "observed_context_slot": SOL_USDC_OBSERVATION["context_slot"],
    }


def derived_sol_usdc_pool(tick_spacing: int = 8) -> tuple[str, int]:
    """PDA from the published config, mints, and tick-spacing seed. Not a guessed address."""
    derived, bump = solana_tx.find_program_address(
        [
            b"whirlpool",
            solana_tx.b58decode(SOL_USDC_CONFIG),
            solana_tx.b58decode(solana_tx.NATIVE_MINT),
            solana_tx.b58decode(solana_tx.USDC_MINT),
            int(tick_spacing).to_bytes(2, "little"),
        ],
        solana_tx.b58decode(solana_whirlpool.PROGRAM_ID),
    )
    return solana_tx.b58encode(derived), bump


def exact_in_min_out(amount_out: int, slippage_bps: int) -> int:
    """SDK floor: amount * (10_000 - bps) / 10_000."""
    try:
        return solana_whirlpool_quote.min_amount(amount_out, slippage_bps)
    except solana_whirlpool_quote.QuoteError as error:
        raise ValueError("quote is not usable") from error


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


def build(pool: str, pool_data: bytes, accounts: dict[str, tuple[str, bytes]], slippage_bps: int, sqrt_price_limit: int, blockhash: str, context_slot: int) -> str:
    """Unsigned wrapped swap. The threshold is the quote of these account bytes."""
    direction = _direction(pool_data)
    state = solana_whirlpool.pool_state(pool_data)
    if state is None:
        raise ValueError("pool is not usable")
    _require_limit(direction, sqrt_price_limit, state["sqrt_price"])
    source = solana_tx.associated_token_address(phantom_transport.REQUIRED_WALLET, solana_tx.NATIVE_MINT, solana_tx.TOKEN_PROGRAM)
    dest = solana_tx.associated_token_address(phantom_transport.REQUIRED_WALLET, solana_tx.USDC_MINT, solana_tx.TOKEN_PROGRAM)
    arrays = _quote_arrays(pool, pool_data, accounts, direction)
    quote = solana_whirlpool_quote.quote_exact_in(
        state, arrays, INPUT_LAMPORTS, direction, slippage_bps, sqrt_price_limit, context_slot,
    )
    if quote["token_in"] != INPUT_LAMPORTS:
        raise ValueError("quote does not consume the readiness amount")
    return transaction_from_fields(
        pool, pool_data, source, dest, INPUT_LAMPORTS, quote["token_min_out"], sqrt_price_limit,
        True, direction, blockhash, wrap=True,
    )


def transaction_from_fields(pool: str, pool_data: bytes, source: str, dest: str, amount: int, threshold: int, sqrt_price_limit: int, specified_input: bool, a_to_b: bool, blockhash: str, authority: str = phantom_transport.REQUIRED_WALLET, token_authority: str | None = None, copies: int = 1, extra_account: bool = False, wrap: bool = False, transfer_lamports: int | None = None) -> str:
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
    swap = [(program, accounts, data)] * copies
    if not wrap:
        return solana_tx.encode_legacy(authority, keys, len(readonly), swap, blockhash, required=len(signers))
    wsol = solana_tx.associated_token_address(authority, solana_tx.NATIVE_MINT, solana_tx.TOKEN_PROGRAM)
    usdc = solana_tx.associated_token_address(authority, solana_tx.USDC_MINT, solana_tx.TOKEN_PROGRAM)
    if source != wsol or dest != usdc:
        raise ValueError("wrapped accounts are not the wallet ATAs")
    extra_keys = [solana_tx.SYSTEM_PROGRAM, solana_tx.ASSOCIATED_TOKEN, solana_tx.NATIVE_MINT, solana_tx.USDC_MINT]
    for key in extra_keys:
        if key not in keys:
            keys.append(key)
    readonly_ids = {solana_tx.TOKEN_PROGRAM, solana_tx.SYSTEM_PROGRAM, solana_tx.ASSOCIATED_TOKEN, solana_whirlpool.PROGRAM_ID, solana_tx.NATIVE_MINT, solana_tx.USDC_MINT}
    if not extra_account:
        readonly_ids.add(oracle)
    readonly = [key for key in keys if key in readonly_ids]
    # Rebuild with the wallet and token accounts writable and programs readonly.
    signers = [authority] if signer == authority else [authority, signer]
    writable = [key for key in keys if key not in readonly and key not in signers]
    keys = [*signers, *writable, *readonly]
    def index(key: str) -> int:
        return keys.index(key)
    create = [index(authority), index(wsol), index(authority), index(solana_tx.NATIVE_MINT), index(solana_tx.SYSTEM_PROGRAM), index(solana_tx.TOKEN_PROGRAM)]
    usdc_create = [index(authority), index(usdc), index(authority), index(solana_tx.USDC_MINT), index(solana_tx.SYSTEM_PROGRAM), index(solana_tx.TOKEN_PROGRAM)]
    lamports = INPUT_LAMPORTS if transfer_lamports is None else transfer_lamports
    transfer = (2).to_bytes(4, "little") + int(lamports).to_bytes(8, "little")
    swap_accounts = [
        index(solana_tx.TOKEN_PROGRAM), index(signer), index(pool), index(owner_a), index(vault_a),
        index(owner_b), index(vault_b), *[index(tick) for tick in ticks], index(oracle),
    ]
    if extra_account:
        swap_accounts.append(index(oracle))
    instructions = [
        (index(solana_tx.ASSOCIATED_TOKEN), create, bytes([1])),
        (index(solana_tx.ASSOCIATED_TOKEN), usdc_create, bytes([1])),
        (index(solana_tx.SYSTEM_PROGRAM), [index(authority), index(wsol)], transfer),
        (index(solana_tx.TOKEN_PROGRAM), [index(wsol)], bytes([17])),
        *[(index(solana_whirlpool.PROGRAM_ID), swap_accounts, data)] * copies,
        (index(solana_tx.TOKEN_PROGRAM), [index(wsol), index(authority), index(authority)], bytes([9])),
    ]
    return solana_tx.encode_legacy(authority, keys, len(readonly), instructions, blockhash, required=len(signers))


def inspect(tx_b64: str, accounts: dict[str, tuple[str, bytes]], slippage_bps: int, context_slot: int) -> dict:
    """Read the wrapped swap. The minimum output is recomputed from the accounts."""
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
        "wrap_verified": False,
        "min_out": None,
        "context_slot": None,
        "blockhash": None,
        "error_category": None,
        "byte_checks_passed": False,
    }
    if isinstance(context_slot, bool) or not isinstance(context_slot, int) or context_slot <= 0:
        body["error_category"] = "quote_context_missing"
        return body
    try:
        raw = base64.b64decode(tx_b64, validate=True)
        decoded = solana_tx.decode_transaction(raw)
    except ValueError:
        body["error_category"] = "transaction_unusable"
        return body
    body["blockhash"] = decoded.blockhash
    body["context_slot"] = context_slot
    if decoded.fee_payer != phantom_transport.REQUIRED_WALLET:
        body["error_category"] = "transaction_shape"
        return body
    plan = _wrap_plan(decoded)
    if plan.get("error"):
        body["error_category"] = plan["error"]
        return body
    instruction = plan["swap"]
    parsed = decode_swap_data(instruction.data)
    pool = decoded.static_keys[instruction.account_indexes[2]]
    record = accounts.get(pool)
    if parsed is None or record is None:
        body["error_category"] = "swap_data_unbound" if parsed is None else "account_unread"
        return body
    pool_data = record[1]
    state = solana_whirlpool.pool_state(pool_data)
    if state is None:
        body["error_category"] = "whirlpool_pool_unbound"
        return body
    tail = _tail_ok(decoded.static_keys, instruction.account_indexes, state["adaptive_fee"])
    if tail is not None:
        body["error_category"] = tail
        return body
    body["min_out"] = parsed["other_amount_threshold"]
    if not parsed["amount_specified_is_input"] or parsed["amount"] != INPUT_LAMPORTS or plan["lamports"] != INPUT_LAMPORTS:
        body["error_category"] = "spend_unbound"
        return body
    try:
        direction = _direction(pool_data)
    except ValueError:
        body["error_category"] = "whirlpool_mint_unbound"
        return body
    if parsed["a_to_b"] != direction:
        body["error_category"] = "direction_unbound"
        return body
    if not _limit_ok(direction, parsed["sqrt_price_limit"], state["sqrt_price"]):
        body["error_category"] = "sqrt_price_limit"
        return body
    source, dest = _owner_accounts(decoded.static_keys, instruction.account_indexes, direction)
    if source != plan["wsol"] or dest != plan["usdc"]:
        body["error_category"] = "wrap_unbound"
        return body
    try:
        solana_whirlpool.check_swap_accounts(
            decoded.static_keys, instruction.account_indexes[:11], phantom_transport.REQUIRED_WALLET,
            source, dest, solana_tx.NATIVE_MINT, solana_tx.USDC_MINT, direction, accounts,
        )
        arrays = _quote_arrays(pool, pool_data, accounts, direction)
        quote = solana_whirlpool_quote.quote_exact_in(
            state, arrays, INPUT_LAMPORTS, direction, slippage_bps, parsed["sqrt_price_limit"], context_slot,
        )
    except solana_whirlpool.WhirlpoolError as error:
        body["error_category"] = error.category
        return body
    except solana_whirlpool_quote.QuoteError as error:
        body["error_category"] = error.category
        return body
    if quote["token_in"] != INPUT_LAMPORTS or quote["token_min_out"] != parsed["other_amount_threshold"]:
        body["error_category"] = "slippage_unbound"
        body["min_out"] = parsed["other_amount_threshold"]
        return body
    body["spend_verified"] = True
    body["slippage_verified"] = True
    body["wallet_verified"] = True
    body["wrap_verified"] = True
    body["output_computed_from_pool"] = True
    body["byte_checks_passed"] = True
    body["sqrt_price_before"] = quote["sqrt_price_before"]
    body["sqrt_price_after"] = quote["sqrt_price_after"]
    return body


def _quote_arrays(pool: str, pool_data: bytes, accounts: dict[str, tuple[str, bytes]], a_to_b: bool) -> list[dict]:
    state = solana_whirlpool.pool_state(pool_data)
    if state is None:
        raise solana_whirlpool_quote.QuoteError("whirlpool_pool_unbound")
    base = solana_whirlpool.start_tick_index(state["tick_current_index"], state["tick_spacing"])
    step = solana_whirlpool.TICK_ARRAY_SIZE * state["tick_spacing"]
    starts = [base - i * step for i in range(3)] if a_to_b else [base + i * step for i in range(3)]
    arrays = []
    for start in starts:
        address = solana_whirlpool.tick_array_address(pool, start)
        record = accounts.get(address)
        parsed = None if record is None else solana_whirlpool.tick_array_state(record[1])
        if parsed is None or parsed["pool"] != pool or parsed["start_tick_index"] != start:
            raise solana_whirlpool_quote.QuoteError("whirlpool_tick_array_unbound")
        arrays.append(parsed)
    return arrays


def _tail_ok(keys: list[str], indexes: list[int], adaptive: bool) -> str | None:
    """Classic pools have the 11-account swap. Adaptive pools add only their oracle."""
    if not adaptive:
        return None if len(indexes) == 11 else "adaptive_tail"
    if len(indexes) != 12 or keys[indexes[11]] != keys[indexes[10]]:
        return "adaptive_tail"
    if keys[indexes[10]] != solana_whirlpool.oracle_address(keys[indexes[2]]):
        return "whirlpool_oracle_unbound"
    return None


def _wrap_plan(decoded: solana_tx.Decoded) -> dict:
    wallet = phantom_transport.REQUIRED_WALLET
    wsol = solana_tx.associated_token_address(wallet, solana_tx.NATIVE_MINT, solana_tx.TOKEN_PROGRAM)
    usdc = solana_tx.associated_token_address(wallet, solana_tx.USDC_MINT, solana_tx.TOKEN_PROGRAM)
    creates = []
    transfer = None
    sync = False
    close = False
    swap = None
    for instruction in decoded.instructions:
        program = decoded.static_keys[instruction.program_index]
        keys = [decoded.static_keys[item] for item in instruction.account_indexes]
        if program == solana_tx.ASSOCIATED_TOKEN and instruction.data == bytes([1]) and len(keys) == 6:
            payer, ata, owner, mint, system, token_program = keys
            if payer != wallet or owner != wallet or system != solana_tx.SYSTEM_PROGRAM or token_program != solana_tx.TOKEN_PROGRAM:
                return {"error": "wrap_unbound"}
            if ata != solana_tx.associated_token_address(owner, mint, token_program):
                return {"error": "wrap_unbound"}
            if mint not in {solana_tx.NATIVE_MINT, solana_tx.USDC_MINT}:
                return {"error": "token_mint_unbound"}
            creates.append(mint)
            continue
        if program == solana_tx.SYSTEM_PROGRAM and solana_tx.system_transfer(instruction.data) is not None and len(keys) == 2:
            if transfer is not None or keys[0] != wallet or keys[1] != wsol:
                return {"error": "spend_unbound"}
            transfer = solana_tx.system_transfer(instruction.data)
            continue
        if program == solana_tx.TOKEN_PROGRAM and instruction.data == bytes([17]) and keys == [wsol]:
            sync = True
            continue
        if program == solana_tx.TOKEN_PROGRAM and instruction.data == bytes([9]) and keys == [wsol, wallet, wallet]:
            close = True
            continue
        if program == solana_whirlpool.PROGRAM_ID and swap is None:
            swap = instruction
            continue
        return {"error": "transaction_shape"}
    if creates.count(solana_tx.NATIVE_MINT) != 1 or creates.count(solana_tx.USDC_MINT) != 1:
        return {"error": "wrap_unbound"}
    if transfer is None or not sync or swap is None or not close:
        return {"error": "wrap_unbound"}
    kinds = [_kind(decoded, item) for item in decoded.instructions]
    if kinds != ["create", "create", "transfer", "sync", "swap", "close"]:
        return {"error": "wrap_unbound"}
    return {"swap": swap, "lamports": transfer, "wsol": wsol, "usdc": usdc}


def _kind(decoded: solana_tx.Decoded, instruction: solana_tx.Instruction) -> str:
    program = decoded.static_keys[instruction.program_index]
    if program == solana_tx.ASSOCIATED_TOKEN:
        return "create"
    if program == solana_tx.SYSTEM_PROGRAM:
        return "transfer"
    if program == solana_whirlpool.PROGRAM_ID:
        return "swap"
    if instruction.data == bytes([17]):
        return "sync"
    return "close"


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
