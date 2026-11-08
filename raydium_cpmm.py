"""Raydium CPMM SwapBaseInput checker. It does not build a live swap and does not send.

Account order and the two u64 arguments are the published CPMM instruction
reference: https://docs.raydium.io/products/cpmm/instructions
The discriminator is the Anchor hash of the published name SwapBaseInput.
It was not copied from an IDL byte dump. The program address is the Raydium
CP-Swap program. A passing check is not proof a mainnet pool was read.
Jupiter route_v2 is not accepted. A pump.fun bonding-curve buy is not this
instruction; its account list is versioned and is not implemented here.
"""

from __future__ import annotations

import hashlib
import base64

import solana_tx

PROGRAM_ID = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1c"
WSOL = "So11111111111111111111111111111111111111112"
DISC = hashlib.sha256(b"global:swap_base_input").digest()[:8]


def inspect_buy(tx_b64: str, *, payer: str, output_mint: str, amount_in: int, minimum_out: int) -> dict:
    """SOL in, memecoin out. Exact-in with a minimum out. Extra instructions fail."""
    return _inspect(tx_b64, payer=payer, input_mint=WSOL, output_mint=output_mint, amount_in=amount_in, minimum_out=minimum_out)


def inspect_exit(tx_b64: str, *, payer: str, input_mint: str, amount_in: int, minimum_out: int) -> dict:
    """Memecoin in, SOL out."""
    return _inspect(tx_b64, payer=payer, input_mint=input_mint, output_mint=WSOL, amount_in=amount_in, minimum_out=minimum_out)


def _inspect(tx_b64: str, *, payer: str, input_mint: str, output_mint: str, amount_in: int, minimum_out: int) -> dict:
    refused = {"ok": False, "send": False, "live_ready": False, "decoder": "raydium_cpmm_swap_base_input"}
    if isinstance(amount_in, bool) or isinstance(minimum_out, bool) or not isinstance(amount_in, int) or not isinstance(minimum_out, int):
        return refused | {"reason": "amount"}
    if amount_in <= 0 or minimum_out <= 0:
        return refused | {"reason": "amount"}
    try:
        decoded = solana_tx.decode_transaction(base64.b64decode(tx_b64, validate=True))
    except Exception:
        return refused | {"reason": "bytes"}
    if decoded.version != "legacy" or decoded.lookups or len(decoded.instructions) != 1:
        return refused | {"reason": "transaction_shape"}
    instruction = decoded.instructions[0]
    program = decoded.static_keys[instruction.program_index] if instruction.program_index < len(decoded.static_keys) else ""
    if program != PROGRAM_ID:
        return refused | {"reason": "program"}
    if instruction.data[:8] != DISC or len(instruction.data) != 24:
        return refused | {"reason": "swap_data"}
    if len(instruction.account_indexes) != 13:
        return refused | {"reason": "swap_accounts"}
    got_in = int.from_bytes(instruction.data[8:16], "little")
    got_min = int.from_bytes(instruction.data[16:24], "little")
    if got_in != amount_in or got_min != minimum_out:
        return refused | {"reason": "amount"}
    keys = [decoded.static_keys[index] for index in instruction.account_indexes]
    if keys[0] != payer:
        return refused | {"reason": "payer"}
    if keys[10] != input_mint or keys[11] != output_mint:
        return refused | {"reason": "mints"}
    try:
        signer, _writable = decoded.account_flags(instruction.account_indexes[0])
    except ValueError:
        return refused | {"reason": "privileges"}
    if not signer:
        return refused | {"reason": "privileges"}
    writable_roles = list(instruction.account_indexes[3:8]) + [instruction.account_indexes[12]]
    readonly_roles = [instruction.account_indexes[i] for i in (1, 2, 8, 9, 10, 11)]
    for index in writable_roles:
        try:
            _signer, writable = decoded.account_flags(index)
        except ValueError:
            return refused | {"reason": "privileges"}
        if not writable:
            return refused | {"reason": "privileges"}
    for index in readonly_roles:
        try:
            role_signer, writable = decoded.account_flags(index)
        except ValueError:
            return refused | {"reason": "privileges"}
        if writable or role_signer:
            return refused | {"reason": "privileges"}
    try:
        program_signer, program_writable = decoded.account_flags(instruction.program_index)
    except ValueError:
        return refused | {"reason": "privileges"}
    if program_signer or program_writable:
        return refused | {"reason": "privileges"}
    return {
        "ok": True,
        "reason": None,
        "send": False,
        "live_ready": False,
        "decoder": "raydium_cpmm_swap_base_input",
        "discriminator_source": "anchor_name_swap_base_input",
        "mainnet_route_proven": False,
        "program_id": PROGRAM_ID,
        "amount_in": got_in,
        "minimum_out": got_min,
        "input_mint": input_mint,
        "output_mint": output_mint,
        "source": "https://docs.raydium.io/products/cpmm/instructions",
    }
