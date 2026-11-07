"""Optional read-only parser for Jupiter's documented Metis /build response.

This is not the production quote path. Phantom buy stays the default and this
module is not mounted on a route. It does not call Jupiter, an RPC, or a signer.
A transport can be injected by a test. The pinned request is keyless.

lastValidBlockHeight is chain metadata from blockhashWithMetadata. It is not a
provider expiry and it is not a freshness guarantee. Dynamic DEX accounts on
the swap instruction stay unverified.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass

import phantom_transport
import solana_inspect
import solana_jupiter
import solana_tx

ENDPOINT = "https://api.jup.ag/swap/v2/build"
_AMOUNT = "10000000"
_SLIPPAGE_BPS = 100
_REQUIRED = {
    "inputMint", "outputMint", "inAmount", "outAmount", "otherAmountThreshold",
    "swapMode", "slippageBps", "priceImpactPct", "routePlan",
    "computeBudgetInstructions", "setupInstructions", "swapInstruction",
    "cleanupInstruction", "otherInstructions", "tipInstruction",
    "addressesByLookupTableAddress", "blockhashWithMetadata", "transactionVersion",
}
_OPTIONAL = {"computeUnitPrice"}
_IMPACT = re.compile(r"-?\d+(\.\d+)?")
_MAX_COMPUTE_UNITS = 1_400_000
_KNOWN_PROGRAMS = solana_tx.ALLOWED_PROGRAMS | {solana_jupiter.PROGRAM_ID}


class BuildError(Exception):
    def __init__(self, reason: str, category: str, details: dict | None = None):
        self.reason = reason
        self.category = category
        self.details = details or {}


@dataclass
class _Ix:
    program_id: str
    accounts: list[tuple[str, bool, bool]]
    data: bytes


def pinned_request() -> dict:
    """The only request this adapter is allowed to describe. Nothing is sent."""
    query = (
        f"inputMint={solana_tx.NATIVE_MINT}"
        f"&outputMint={solana_tx.USDC_MINT}"
        f"&amount={_AMOUNT}"
        f"&taker={phantom_transport.REQUIRED_WALLET}"
        f"&slippageBps={_SLIPPAGE_BPS}"
    )
    return {
        "method": "GET",
        "url": f"{ENDPOINT}?{query}",
        "headers": {"accept": "application/json"},
        "api_key": None,
        "network": "not_called",
    }


def exchange(transport) -> dict:
    """Parse one injected response. A missing transport cannot open the network."""
    if transport is None:
        raise BuildError("jupiter build network is not enabled", "network_disabled")
    request = pinned_request()
    response = transport(request)
    status = getattr(response, "status", None)
    final_url = getattr(response, "url", None)
    if status in {301, 302, 303, 307, 308} or final_url != request["url"]:
        raise BuildError("jupiter build response redirected", "redirect")
    if status != 200 or not isinstance(getattr(response, "body", None), dict):
        raise BuildError("jupiter build response is not usable", "malformed")
    return evaluate(response.body, getattr(response, "rpc", None))


def evaluate(body: dict, rpc) -> dict:
    parsed = parse_build(body)
    transaction = assemble(parsed)
    if rpc is None:
        inspected = {
            "stage": "assembled",
            "chain_ok": False,
            "blockhash_status": "not_run",
            "simulation_status": "not_run",
            "program_ids": sorted({item.program_id for item in parsed["instructions"]}),
            "debited_lamports": None,
            "rent_lamports": None,
            "slippage_verified": False,
            "spend_verified": False,
            "intent_verified": False,
        }
    else:
        inspected = solana_inspect.inspect(transaction, rpc)
    quoted = inspected.get("quoted_out_amount")
    if inspected.get("chain_ok") is True and (
        str(quoted) != parsed["out_amount"] or inspected.get("slippage_bps") != parsed["slippage_bps"]
    ):
        inspected = solana_inspect._rejected(
            "jupiter build amounts do not match the swap instruction",
            "route_mismatch",
            inspected.get("program_ids"),
        )
    inspected["provider"] = "jupiter_metis_build"
    inspected["production_provider"] = "phantom"
    inspected["route_wired"] = False
    inspected["last_valid_block_height"] = parsed["last_valid_block_height"]
    inspected["last_valid_block_height_is_provider_expiry"] = False
    inspected["provider_expiry"] = None
    inspected["freshness_guarantee"] = False
    inspected["other_amount_threshold"] = parsed["other_amount_threshold"]
    inspected["price_impact_pct"] = parsed["price_impact_pct"]
    inspected["price_impact_verified"] = False
    inspected["blockhash_fetched_at"] = parsed["blockhash_fetched_at"]
    inspected.update(_public_diagnostics(parsed))
    inspected["min_output_formula_ok"] = parsed["threshold_matches_floor"] or parsed["threshold_matches_ceil"]
    inspected["min_output_verified"] = False
    inspected["freshness_guarantee"] = False
    inspected["dex_accounts_verified"] = False
    inspected["slippage_verified"] = False
    inspected["spend_verified"] = False
    inspected["intent_verified"] = False
    inspected["execution_allowed"] = False
    inspected["send"] = False
    inspected["executable"] = False
    inspected["live_ready"] = False
    inspected["transaction_returned"] = False
    return inspected


def parse_build(body: dict) -> dict:
    if not isinstance(body, dict) or set(body) - _OPTIONAL != _REQUIRED:
        raise BuildError("jupiter build fields are not the documented set", "malformed")
    version = body.get("transactionVersion", 0)
    if version not in {0, "0"}:
        raise BuildError("jupiter build transaction version is not v0", "transaction_version_unsupported")
    if body.get("inputMint") != solana_tx.NATIVE_MINT or body.get("outputMint") != solana_tx.USDC_MINT:
        raise BuildError("jupiter build mints are not native SOL to USDC", "route_mismatch")
    if body.get("inAmount") != _AMOUNT or body.get("swapMode") != "ExactIn":
        raise BuildError("jupiter build amount is not 0.01 SOL exact in", "amount_mismatch")
    out_amount = _positive_integer(body.get("outAmount"), "outAmount")
    threshold = _positive_integer(body.get("otherAmountThreshold"), "otherAmountThreshold")
    slippage = body.get("slippageBps")
    if isinstance(slippage, bool) or not isinstance(slippage, int) or not 0 <= slippage <= _SLIPPAGE_BPS:
        raise BuildError("jupiter build slippage is outside the 1 percent field", "slippage_mismatch")
    floor = out_amount * (10_000 - slippage) // 10_000
    if threshold > out_amount or threshold < floor:
        raise BuildError("jupiter build minimum output does not match the slippage field", "slippage_mismatch")
    if "computeUnitPrice" in body and version in {0, "0"}:
        raise BuildError("jupiter build v0 response included computeUnitPrice", "malformed")
    if not isinstance(body.get("priceImpactPct"), str) or _IMPACT.fullmatch(body["priceImpactPct"]) is None or len(body["priceImpactPct"]) > 32:
        raise BuildError("jupiter build price impact is malformed", "malformed")
    if body.get("tipInstruction") is not None:
        raise BuildError("jupiter build included a tip", "tip_unbound")
    _route(body.get("routePlan"))
    instructions = []
    for group in (
        body.get("computeBudgetInstructions"),
        body.get("setupInstructions"),
        [body.get("swapInstruction")],
        [] if body.get("cleanupInstruction") is None else [body.get("cleanupInstruction")],
        body.get("otherInstructions"),
    ):
        if not isinstance(group, list):
            raise BuildError("jupiter build instructions are malformed", "malformed")
        instructions.extend(_instruction(item) for item in group)
    tables = _tables(body.get("addressesByLookupTableAddress"))
    blockhash, height, fetched_at = _blockhash(body.get("blockhashWithMetadata"))
    diagnostics = _public_diagnostics({
        "instructions": instructions,
        "out_amount": str(out_amount),
        "other_amount_threshold": str(threshold),
        "slippage_bps": slippage,
        "last_valid_block_height": height,
        "blockhash_fetched_at": fetched_at,
    })
    problem = _swap_problem(instructions)
    if problem is not None:
        reason, category = problem
        raise BuildError(reason, category, diagnostics)
    return {
        "instructions": instructions,
        "tables": tables,
        "blockhash": blockhash,
        "last_valid_block_height": height,
        "out_amount": str(out_amount),
        "other_amount_threshold": str(threshold),
        "slippage_bps": slippage,
        "price_impact_pct": body["priceImpactPct"],
        "blockhash_fetched_at": fetched_at,
        **diagnostics,
    }


def assemble(parsed: dict) -> str:
    """Unsigned v0 message. Account order follows web3.js CompiledKeys."""
    meta: dict[str, dict] = {}

    def touch(key: str) -> dict:
        item = meta.get(key)
        if item is None:
            item = {"signer": False, "writable": False, "invoked": False}
            meta[key] = item
        return item

    payer = touch(phantom_transport.REQUIRED_WALLET)
    payer["signer"] = True
    payer["writable"] = True
    for instruction in parsed["instructions"]:
        touch(instruction.program_id)["invoked"] = True
        for pubkey, signer, writable in instruction.accounts:
            if signer and pubkey != phantom_transport.REQUIRED_WALLET:
                raise BuildError("jupiter build asks for another signer", "wallet_mismatch")
            item = touch(pubkey)
            item["signer"] = item["signer"] or signer
            item["writable"] = item["writable"] or writable
    lookups = []
    drained_writable: list[str] = []
    drained_readonly: list[str] = []
    for table, addresses in parsed["tables"]:
        writable = _drain(meta, addresses, lambda item: not item["signer"] and not item["invoked"] and item["writable"])
        readonly = _drain(meta, addresses, lambda item: not item["signer"] and not item["invoked"] and not item["writable"])
        if writable[0] or readonly[0]:
            lookups.append((table, writable[0], readonly[0]))
            drained_writable.extend(writable[1])
            drained_readonly.extend(readonly[1])
    ordered_meta = list(meta.items())
    writable_signers = [key for key, item in ordered_meta if item["signer"] and item["writable"]]
    readonly_signers = [key for key, item in ordered_meta if item["signer"] and not item["writable"]]
    writable_rest = [key for key, item in ordered_meta if not item["signer"] and item["writable"]]
    readonly_rest = [key for key, item in ordered_meta if not item["signer"] and not item["writable"]]
    if writable_signers != [phantom_transport.REQUIRED_WALLET] or readonly_signers:
        raise BuildError("jupiter build fee payer is not the allowlisted wallet", "wallet_mismatch")
    static = writable_signers + readonly_signers + writable_rest + readonly_rest
    ordered = static + drained_writable + drained_readonly
    header = bytes([
        len(writable_signers) + len(readonly_signers),
        len(readonly_signers),
        len(readonly_rest),
    ])
    message = bytes([0x80]) + header + _compact(len(static))
    message += b"".join(solana_tx.b58decode(key) for key in static)
    message += solana_tx.b58decode(parsed["blockhash"])
    message += _compact(len(parsed["instructions"]))
    for instruction in parsed["instructions"]:
        try:
            program_index = ordered.index(instruction.program_id)
            accounts = [ordered.index(pubkey) for pubkey, _signer, _writable in instruction.accounts]
        except ValueError:
            raise BuildError("jupiter build account was not compiled", "malformed")
        message += bytes([program_index]) + _compact(len(accounts)) + bytes(accounts)
        message += _compact(len(instruction.data)) + instruction.data
    message += _compact(len(lookups))
    for table, writable, readonly in lookups:
        message += solana_tx.b58decode(table) + _compact(len(writable)) + bytes(writable)
        message += _compact(len(readonly)) + bytes(readonly)
    raw = bytes([1]) + (b"\x00" * 64) + message
    if len(raw) > 1232:
        raise BuildError("jupiter build transaction is too large", "malformed")
    return base64.b64encode(raw).decode("ascii")


def _drain(meta: dict, addresses: list[str], predicate) -> tuple[list[int], list[str]]:
    indexes = []
    keys = []
    for key, item in list(meta.items()):
        if predicate(item) and key in addresses:
            indexes.append(addresses.index(key))
            keys.append(key)
            del meta[key]
    return indexes, keys


def _swap_problem(instructions: list[_Ix]) -> tuple[str, str] | None:
    swaps = [item for item in instructions if item.program_id == solana_jupiter.PROGRAM_ID]
    if len(swaps) != 1:
        return "jupiter build swap program is not JUP6", "route_mismatch"
    data = swaps[0].data
    if len(data) >= 8 and data[:8] == solana_jupiter.ROUTE_V2:
        return (
            "jupiter build swap is route_v2 and the published Jupiter IDL does not include its layout",
            "jupiter_route_v2_unsupported",
        )
    if len(data) < 8 or data[:8] != solana_jupiter._SHARED:
        return (
            "jupiter build swap instruction is not shared_accounts_route",
            "jupiter_instruction_unsupported",
        )
    return None


def _public_diagnostics(parsed: dict) -> dict:
    """Fee and threshold arithmetic. Neither number is spend approval or a fill guarantee."""
    out_amount = int(parsed["out_amount"])
    slippage = parsed["slippage_bps"]
    threshold = int(parsed["other_amount_threshold"])
    kept = 10_000 - slippage
    floor = out_amount * kept // 10_000
    ceil = (out_amount * kept + 9_999) // 10_000
    fee = _fee_budget(parsed["instructions"])
    return {
        "integer_floor_threshold": str(floor),
        "integer_ceil_threshold": str(ceil),
        "threshold_matches_floor": threshold == floor,
        "threshold_matches_ceil": threshold == ceil,
        "threshold_rounding_documented": False,
        "min_output_verified": False,
        "fee_policy": "not_approved",
        "fee_authorized": False,
        "freshness_basis": "last_valid_block_height",
        "freshness_guarantee": False,
        "provider_expiry": None,
        "dex_accounts_verified": False,
        **fee,
    }


def _fee_budget(instructions: list[_Ix]) -> dict:
    """ceil(price_micro_lamports * compute_unit_limit / 1_000_000).

    Jupiter's build response carries the price and not the limit. The limit is
    chosen after simulation, so a missing limit uses the documented 1,400,000
    maximum only as an upper bound. A non-zero price stays unauthorized.
    """
    price = None
    limit = None
    for instruction in instructions:
        if instruction.program_id != solana_tx.COMPUTE_BUDGET:
            continue
        parsed = solana_tx.compute_budget_payload(instruction.data)
        if parsed is None:
            continue
        kind, payload, _length = parsed
        if kind == 3 and price is None:
            price = int.from_bytes(payload, "little")
        elif kind == 2 and limit is None:
            limit = int.from_bytes(payload, "little")
    if price is None:
        return {
            "price_micro_lamports": None,
            "compute_unit_limit": limit,
            "fee_lamports_ceil": None,
            "fee_basis": "price_absent",
        }
    units = limit if limit is not None else _MAX_COMPUTE_UNITS
    return {
        "price_micro_lamports": price,
        "compute_unit_limit": limit,
        "fee_lamports_ceil": (price * units + 999_999) // 1_000_000,
        "fee_basis": "instruction_limit" if limit is not None else "max_compute_unit_limit",
    }


def _route(plan) -> None:
    if not isinstance(plan, list) or not 1 <= len(plan) <= 16:
        raise BuildError("jupiter build route plan is not usable", "route_mismatch")
    mints = []
    for step in plan:
        if not isinstance(step, dict) or set(step) - {"usdValue"} != {"swapInfo", "percent", "bps"}:
            raise BuildError("jupiter build route plan is not usable", "route_mismatch")
        info = step.get("swapInfo")
        percent = step.get("percent")
        bps = step.get("bps")
        if not isinstance(info, dict) or set(info) != {"ammKey", "label", "inputMint", "outputMint", "inAmount", "outAmount"}:
            raise BuildError("jupiter build route plan is not usable", "route_mismatch")
        if isinstance(percent, bool) or not isinstance(percent, int) or not 1 <= percent <= 100:
            raise BuildError("jupiter build route plan is not usable", "route_mismatch")
        if isinstance(bps, bool) or not isinstance(bps, int) or bps != percent * 100:
            raise BuildError("jupiter build route plan is not usable", "route_mismatch")
        if "usdValue" in step and (isinstance(step["usdValue"], bool) or not isinstance(step["usdValue"], (int, float))):
            raise BuildError("jupiter build route plan is not usable", "route_mismatch")
        mints.append((info.get("inputMint"), info.get("outputMint")))
    if mints[0][0] != solana_tx.NATIVE_MINT or mints[-1][1] != solana_tx.USDC_MINT:
        raise BuildError("jupiter build route does not sell native SOL for USDC", "route_mismatch")


def _instruction(value) -> _Ix:
    if not isinstance(value, dict):
        raise BuildError("jupiter build instruction is malformed", "malformed")
    program_id = value.get("programId")
    accounts = value.get("accounts")
    data = value.get("data")
    if program_id not in _KNOWN_PROGRAMS:
        raise BuildError("jupiter build program is not a documented route", "unknown_router")
    if not isinstance(accounts, list) or not isinstance(data, str):
        raise BuildError("jupiter build instruction is malformed", "malformed")
    parsed_accounts = []
    for account in accounts:
        if not isinstance(account, dict):
            raise BuildError("jupiter build instruction is malformed", "malformed")
        pubkey = account.get("pubkey")
        signer = account.get("isSigner")
        writable = account.get("isWritable")
        if not isinstance(pubkey, str) or not isinstance(signer, bool) or not isinstance(writable, bool):
            raise BuildError("jupiter build instruction is malformed", "malformed")
        try:
            solana_tx.b58decode(pubkey)
        except ValueError:
            raise BuildError("jupiter build instruction is malformed", "malformed")
        if signer and pubkey != phantom_transport.REQUIRED_WALLET:
            raise BuildError("jupiter build asks for another signer", "wallet_mismatch")
        parsed_accounts.append((pubkey, signer, writable))
    try:
        raw = base64.b64decode(data, validate=True)
    except Exception:
        raise BuildError("jupiter build instruction is malformed", "malformed")
    return _Ix(program_id, parsed_accounts, raw)


def _tables(value) -> list[tuple[str, list[str]]]:
    if value is None:
        return []
    if not isinstance(value, dict):
        raise BuildError("jupiter build lookup tables are malformed", "malformed")
    tables = []
    for table, addresses in value.items():
        if not isinstance(addresses, list) or len(addresses) > 256:
            raise BuildError("jupiter build lookup tables are malformed", "malformed")
        try:
            solana_tx.b58decode(table)
            for address in addresses:
                solana_tx.b58decode(address)
        except ValueError:
            raise BuildError("jupiter build lookup tables are malformed", "malformed")
        tables.append((table, list(addresses)))
    return tables


def _blockhash(value) -> tuple[str, int, dict]:
    if not isinstance(value, dict) or set(value) != {"blockhash", "lastValidBlockHeight", "fetchedAt"}:
        raise BuildError("jupiter build blockhash is malformed", "malformed")
    raw = value.get("blockhash")
    height = value.get("lastValidBlockHeight")
    fetched = value.get("fetchedAt")
    if not isinstance(raw, list) or len(raw) != 32 or any(isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 255 for item in raw):
        raise BuildError("jupiter build blockhash is malformed", "malformed")
    if isinstance(height, bool) or not isinstance(height, int) or height < 1:
        raise BuildError("jupiter build blockhash height is malformed", "malformed")
    if not isinstance(fetched, dict) or set(fetched) != {"secs_since_epoch", "nanos_since_epoch"}:
        raise BuildError("jupiter build blockhash fetch time is malformed", "malformed")
    secs = fetched.get("secs_since_epoch")
    nanos = fetched.get("nanos_since_epoch")
    if isinstance(secs, bool) or not isinstance(secs, int) or secs < 0:
        raise BuildError("jupiter build blockhash fetch time is malformed", "malformed")
    if isinstance(nanos, bool) or not isinstance(nanos, int) or not 0 <= nanos < 1_000_000_000:
        raise BuildError("jupiter build blockhash fetch time is malformed", "malformed")
    return solana_tx.b58encode(bytes(raw)), height, {"secs_since_epoch": secs, "nanos_since_epoch": nanos}


def _positive_integer(value, name: str) -> int:
    if not isinstance(value, str) or not value.isdigit() or value.startswith("0"):
        raise BuildError(f"jupiter build {name} is malformed", "malformed")
    number = int(value)
    if number < 1 or number >= 1 << 64:
        raise BuildError(f"jupiter build {name} is malformed", "malformed")
    return number


def _compact(value: int) -> bytes:
    if value < 0 or value > 0xFFFF:
        raise BuildError("jupiter build transaction is malformed", "malformed")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)
