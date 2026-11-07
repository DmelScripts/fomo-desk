"""Decode a Solana transaction far enough to inspect it. Bytes are not returned.

The layout is the versioned message used by solana-web3.js. Lookup-table
addresses start at byte 56 of the table account. A blockhash is 32 bytes in
the message and is reported as base58 for isBlockhashValid.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ASSOCIATED_TOKEN = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
NATIVE_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
ALLOWED_PROGRAMS = {SYSTEM_PROGRAM, COMPUTE_BUDGET, TOKEN_PROGRAM, TOKEN_2022, ASSOCIATED_TOKEN}
BOUND_MINTS = {NATIVE_MINT, USDC_MINT}
_LOOKUP_META = 56
_ACTIVE_SLOT = (1 << 64) - 1


@dataclass
class Instruction:
    program_index: int
    account_indexes: list[int]
    data: bytes
    program_id: str = ""
    parsed: dict | None = None


@dataclass
class Lookup:
    table: str
    writable: list[int]
    readonly: list[int]


@dataclass
class Decoded:
    version: str
    fee_payer: str
    signers: list[str]
    static_keys: list[str]
    blockhash: str
    instructions: list[Instruction]
    lookups: list[Lookup] = field(default_factory=list)
    resolved_keys: list[str] = field(default_factory=list)


def b58encode(data: bytes) -> str:
    zeros = 0
    for byte in data:
        if byte == 0:
            zeros += 1
        else:
            break
    number = int.from_bytes(data, "big")
    chars = []
    while number:
        number, rem = divmod(number, 58)
        chars.append(_ALPHABET[rem])
    return ("1" * zeros) + "".join(reversed(chars))


def b58decode(text: str, size: int = 32) -> bytes:
    if not isinstance(text, str) or not text or any(char not in _ALPHABET for char in text):
        raise ValueError("base58 is not usable")
    number = 0
    for char in text:
        number = number * 58 + _ALPHABET.index(char)
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    pad = 0
    for char in text:
        if char != "1":
            break
        pad += 1
    out = b"\x00" * pad + raw
    if len(out) > size:
        raise ValueError("base58 is not usable")
    return b"\x00" * (size - len(out)) + out


def decode_transaction(raw: bytes) -> Decoded:
    if not isinstance(raw, bytes) or len(raw) < 65:
        raise ValueError("transaction is not usable")
    count, index = _compact(raw, 0)
    if count < 1 or count > 8:
        raise ValueError("transaction is not usable")
    index += 64 * count
    if index >= len(raw):
        raise ValueError("transaction is not usable")
    message = raw[index:]
    version = "legacy"
    cursor = 0
    if message[0] & 0x80:
        version_byte = message[0] & 0x7F
        if version_byte != 0:
            raise ValueError("transaction version is not usable")
        version = "v0"
        cursor = 1
    if cursor + 3 > len(message):
        raise ValueError("transaction is not usable")
    required = message[cursor]
    readonly_signed = message[cursor + 1]
    readonly_unsigned = message[cursor + 2]
    cursor += 3
    key_count, cursor = _compact(message, cursor)
    if required < 1 or key_count < required or readonly_signed > required:
        raise ValueError("transaction is not usable")
    keys = []
    for _ in range(key_count):
        keys.append(b58encode(message[cursor:cursor + 32]))
        cursor += 32
    blockhash = b58encode(message[cursor:cursor + 32])
    cursor += 32
    ix_count, cursor = _compact(message, cursor)
    if ix_count < 1 or ix_count > 16:
        raise ValueError("transaction is not usable")
    instructions = []
    for _ in range(ix_count):
        program_index = message[cursor]
        cursor += 1
        account_count, cursor = _compact(message, cursor)
        accounts = list(message[cursor:cursor + account_count])
        cursor += account_count
        data_len, cursor = _compact(message, cursor)
        data = message[cursor:cursor + data_len]
        cursor += data_len
        instructions.append(Instruction(program_index, accounts, data))
    lookups = []
    if version == "v0":
        lookup_count, cursor = _compact(message, cursor)
        for _ in range(lookup_count):
            table = b58encode(message[cursor:cursor + 32])
            cursor += 32
            writable_count, cursor = _compact(message, cursor)
            writable = list(message[cursor:cursor + writable_count])
            cursor += writable_count
            readonly_count, cursor = _compact(message, cursor)
            readonly = list(message[cursor:cursor + readonly_count])
            cursor += readonly_count
            lookups.append(Lookup(table, writable, readonly))
    if cursor != len(message):
        raise ValueError("transaction is not usable")
    signers = keys[:required]
    return Decoded(version, keys[0], signers, keys, blockhash, instructions, lookups, list(keys))


def lookup_addresses(data: bytes) -> list[str]:
    if len(data) < _LOOKUP_META or int.from_bytes(data[:4], "little") != 1:
        raise ValueError("lookup table is not usable")
    if int.from_bytes(data[4:12], "little") != _ACTIVE_SLOT:
        raise ValueError("lookup table is not active")
    body = data[_LOOKUP_META:]
    if len(body) % 32:
        raise ValueError("lookup table is not usable")
    return [b58encode(body[offset:offset + 32]) for offset in range(0, len(body), 32)]


def system_transfer(data: bytes) -> int | None:
    if len(data) != 12 or int.from_bytes(data[:4], "little") != 2:
        return None
    return int.from_bytes(data[4:12], "little")


# Rent-exempt minimum for one 165-byte token account.
# getMinimumBalanceForRentExemption(165) returned 1_488_440 at minContextSlot
# 466540548. The older formula result was 2_039_280 and is not the current bound.
TOKEN_ACCOUNT_SPACE = 165
TOKEN_ACCOUNT_RENT = 1_488_440


def system_create(data: bytes) -> tuple[int, int, str] | None:
    """System CreateAccount: u32 tag 0, lamports, space, owner pubkey."""
    if len(data) != 52 or int.from_bytes(data[:4], "little") != 0:
        return None
    lamports = int.from_bytes(data[4:12], "little")
    space = int.from_bytes(data[12:20], "little")
    if lamports >= 1 << 63:
        return None
    return lamports, space, b58encode(data[20:52])


def transfer_checked(data: bytes) -> tuple[int, int] | None:
    if len(data) != 10 or data[0] != 12:
        return None
    return int.from_bytes(data[1:9], "little"), data[9]


def token_account_fields(data: bytes) -> tuple[str, str] | None:
    """SPL token account: mint at byte 0, owner at byte 32. Shorter data is not an account."""
    if len(data) < 165:
        return None
    return b58encode(data[0:32]), b58encode(data[32:64])


def token_transfer(data: bytes) -> int | None:
    """SPL Transfer names an amount and no mint. Tag 3, then a u64."""
    if len(data) != 9 or data[0] != 3:
        return None
    return int.from_bytes(data[1:9], "little")


def compute_budget_payload(data: bytes) -> tuple[int, bytes, int] | None:
    """Return tag, payload, and total length for a documented compute-budget encoding.

    web3.js writes a u8 discriminant. The Rust bincode encoding writes a u32
    discriminant. Payload widths are the same: deprecated request-units is two
    u32s, heap and compute limit and loaded-account limit are one u32, and the
    price is one u64.
    """
    short = {0: 8, 1: 4, 2: 4, 3: 8, 4: 4}
    if data and data[0] in short and len(data) == 1 + short[data[0]]:
        return data[0], data[1:], len(data)
    if len(data) >= 4:
        tag = int.from_bytes(data[:4], "little")
        payload = data[4:]
        if tag in short and len(payload) == short[tag]:
            return tag, payload, len(data)
    return None


def find_program_address(seeds: list[bytes], program: bytes) -> tuple[bytes, int]:
    """Solana findProgramAddress. Off-curve check matches the web3.js PDA rule."""
    for bump in range(255, -1, -1):
        digest = hashlib.sha256(b"".join(seeds) + bytes([bump]) + program + b"ProgramDerivedAddress").digest()
        if not _point_on_curve(digest):
            return digest, bump
    raise ValueError("program address is not usable")


def associated_token_address(owner: str, mint: str, token_program: str) -> str:
    """SPL associated-token PDA: [owner, token program, mint]."""
    derived, _bump = find_program_address(
        [b58decode(owner), b58decode(token_program), b58decode(mint)],
        b58decode(ASSOCIATED_TOKEN),
    )
    return b58encode(derived)


def _point_on_curve(point: bytes) -> bool:
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


def _compact(data: bytes, index: int) -> tuple[int, int]:
    value = 0
    shift = 0
    for _ in range(3):
        if index >= len(data):
            raise ValueError("transaction is not usable")
        byte = data[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            return value, index
        shift += 7
    raise ValueError("transaction is not usable")


def encode_v0_transfer_lookup(fee_payer: str, table: str, lamports: int, blockhash: str) -> str:
    """Unsigned v0 transfer whose destination is lookup index 0."""
    payer = b58decode(fee_payer)
    table_key = b58decode(table)
    block = b58decode(blockhash)
    program = b"\x00" * 32
    data = (2).to_bytes(4, "little") + int(lamports).to_bytes(8, "little")
    message = bytes([0x80, 1, 0, 1, 2]) + payer + program + block
    message += bytes([1, 1, 2, 0, 2, len(data)]) + data
    message += bytes([1]) + table_key + bytes([1, 0, 0])
    raw = bytes([1]) + (b"\x00" * 64) + message
    return base64.b64encode(raw).decode("ascii")


def message_bytes(tx_b64: str) -> bytes:
    """The message after the signature block. This is what getFeeForMessage prices."""
    _count, index, raw = _signed(tx_b64)
    return raw[index:]


def first_signature(tx_b64: str) -> str:
    """The fee payer signature. An unsigned transaction does not have one."""
    count, index, raw = _signed(tx_b64)
    if count < 1 or index < 64:
        raise ValueError("transaction is not usable")
    signature = raw[index - 64 * count:index - 64 * count + 64]
    if signature == b"\x00" * 64:
        raise ValueError("transaction is unsigned")
    return b58encode(signature)


def _signed(tx_b64: str) -> tuple[int, int, bytes]:
    raw = base64.b64decode(tx_b64, validate=True)
    count, index = _compact(raw, 0)
    if count < 1 or count > 8:
        raise ValueError("transaction is not usable")
    start = index
    index += 64 * count
    if index >= len(raw) or start != index - 64 * count:
        raise ValueError("transaction is not usable")
    return count, index, raw


def encode_legacy(fee_payer: str, keys: list[str], readonly_unsigned: int, instructions: list[tuple[int, list[int], bytes]], blockhash: str, required: int = 1) -> str:
    """Unsigned legacy transaction. The first required keys are the signers."""
    if not keys or keys[0] != fee_payer or not 1 <= required <= len(keys) or len(keys) > 64 or len(instructions) > 16:
        raise ValueError("transaction is not usable")
    raw_keys = b"".join(b58decode(key) for key in keys)
    body = bytes([required, 0, readonly_unsigned, len(keys)]) + raw_keys + b58decode(blockhash)
    encoded = bytes([len(instructions)])
    for program_index, accounts, data in instructions:
        if len(accounts) > 64 or len(data) > 255:
            raise ValueError("transaction is not usable")
        encoded += bytes([program_index, len(accounts), *accounts, len(data)]) + data
    raw = bytes([required]) + (b"\x00" * (64 * required)) + body + encoded
    return base64.b64encode(raw).decode("ascii")


def encode_legacy_transfer(fee_payer: str, dest: str, lamports: int, blockhash: str) -> str:
    """Unsigned legacy transfer used by tests. The returned text is not stored."""
    payer = b58decode(fee_payer)
    target = b58decode(dest)
    block = b58decode(blockhash)
    program = b"\x00" * 32
    data = (2).to_bytes(4, "little") + int(lamports).to_bytes(8, "little")
    message = bytes([1, 0, 1, 3]) + payer + target + program + block + bytes([1, 2, 2, 0, 1, len(data)]) + data
    raw = bytes([1]) + (b"\x00" * 64) + message
    return base64.b64encode(raw).decode("ascii")
