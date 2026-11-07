"""Read-only Solana inspection. It does not sign, send, or store a transaction.

Public RPC methods used here are isBlockhashValid, simulateTransaction,
getSignatureStatuses, getTransaction, and getAccountInfo. A valid blockhash
at a reported slot is not a provider expiry and not a slippage guarantee.

Phantom `solana send` with confirmed false calls Phantom's simulation service
and returns pending_confirmation without signAndSendTransaction. confirmed
true calls signAndSendTransaction. That preview is not this RPC check and is
not execution intent. This module does not call the CLI.
"""

from __future__ import annotations

import base64

import phantom_transport
import solana_tx

_METHODS = {
    "getGenesisHash",
    "isBlockhashValid",
    "simulateTransaction",
    "getSignatureStatuses",
    "getTransaction",
    "getAccountInfo",
}
MAINNET_RPC = "https://api.mainnet-beta.solana.com"
MAINNET_GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
_COMMITMENTS = {"confirmed", "finalized"}
_MAX_LAMPORTS = 10_000_000


class RpcError(RuntimeError):
    pass


class RpcClient:
    """JSON-RPC caller. Tests pass a transport. Nothing calls the network on import."""

    def __init__(self, transport):
        self._transport = transport

    def call(self, method: str, params: list) -> dict:
        if method not in _METHODS:
            raise RpcError("rpc method is not allowed")
        envelope = self._transport({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if not isinstance(envelope, dict) or envelope.get("error") or "result" not in envelope:
            raise RpcError("rpc result is not usable")
        result = envelope["result"]
        if method == "getGenesisHash":
            if not isinstance(result, str) or not result:
                raise RpcError("rpc result is not usable")
            return result
        if not isinstance(result, dict):
            raise RpcError("rpc result is not usable")
        return result


def mainnet_rpc() -> RpcClient:
    """Fixed public mainnet endpoint. The URL is not taken from the request."""
    def transport(payload):
        import httpx
        response = httpx.post(MAINNET_RPC, json=payload, timeout=20.0)
        response.raise_for_status()
        return response.json()
    return RpcClient(transport)


def inspect(tx_b64: str, rpc: RpcClient, commitment: str = "confirmed", max_lamports: int = _MAX_LAMPORTS) -> dict:
    if commitment not in _COMMITMENTS or not isinstance(max_lamports, int) or isinstance(max_lamports, bool):
        raise ValueError("inspection bounds are not usable")
    if not isinstance(tx_b64, str) or len(tx_b64) > 20000:
        raise ValueError("transaction is not usable")
    try:
        raw = base64.b64decode(tx_b64, validate=True)
        decoded = solana_tx.decode_transaction(raw)
        keys = _resolve(decoded, rpc, commitment)
        program_ids = _programs(decoded, keys)
        debited = _debits(decoded, keys)
    except GateError as exc:
        return _rejected(exc.reason, exc.category, exc.program_ids)
    except (ValueError, RpcError) as exc:
        return _rejected(str(exc), "not_usable")
    if decoded.fee_payer != phantom_transport.REQUIRED_WALLET:
        return _rejected("fee payer is not the allowlisted wallet", "wallet_unbound", program_ids)
    if debited is None or debited > max_lamports:
        return _rejected("debit is not bound", "amount_unbound", program_ids)
    try:
        validity = rpc.call("isBlockhashValid", [decoded.blockhash, {"commitment": commitment}])
        slot = _slot(validity)
        if validity.get("value") is not True:
            return _rejected("blockhash is not valid", "blockhash_invalid", program_ids)
        simulated = rpc.call("simulateTransaction", [tx_b64, {
            "encoding": "base64",
            "commitment": commitment,
            "sigVerify": False,
            "replaceRecentBlockhash": False,
        }])
        sim_slot = _slot(simulated)
        value = simulated.get("value")
        if not isinstance(value, dict) or value.get("err") is not None:
            return _rejected("simulation failed", "simulation_failed", program_ids)
        if sim_slot < slot:
            return _rejected("slot is inconsistent", "slot_inconsistent", program_ids)
    except GateError as exc:
        return _rejected(exc.reason, exc.category, exc.program_ids)
    except (ValueError, RpcError) as exc:
        return _rejected(str(exc), "not_usable", program_ids)
    return {
        "stage": "chain_checked",
        "chain_ok": True,
        "version": decoded.version,
        "fee_payer": decoded.fee_payer,
        "blockhash": decoded.blockhash,
        "commitment": commitment,
        "blockhash_slot": slot,
        "simulation_slot": sim_slot,
        "debited_lamports": debited,
        "program_ids": sorted({item.program_id for item in decoded.instructions}),
        "slippage_verified": False,
        "provider_expiry": None,
        "freshness_guarantee": False,
        "execution_allowed": False,
        "send": False,
        "executable": False,
        "live_ready": False,
        "error_category": None,
    }


def preflight_transaction(tx_b64: str, rpc: RpcClient) -> dict:
    """Check mainnet identity, then inspect. A valid blockhash is not an expiry."""
    try:
        genesis = rpc.call("getGenesisHash", [])
    except (ValueError, RpcError):
        return _rejected("genesis is not usable", "wrong_network")
    if genesis != MAINNET_GENESIS:
        return _rejected("genesis is not mainnet", "wrong_network")
    result = inspect(tx_b64, rpc)
    result["genesis_ok"] = True
    result["mainnet"] = True
    result["freshness_guarantee"] = False
    result["provider_expiry"] = None
    result["slippage_verified"] = False
    result["execution_allowed"] = False
    result["send"] = False
    result["executable"] = False
    result["live_ready"] = False
    return result


def signature_status(rpc: RpcClient, signature: str, commitment: str = "confirmed") -> dict:
    if commitment not in _COMMITMENTS or _exact_b58(signature, 64) is None:
        raise ValueError("signature is not usable")
    result = rpc.call("getSignatureStatuses", [[signature], {"searchTransactionHistory": False}])
    value = result.get("value")
    row = value[0] if isinstance(value, list) and value and isinstance(value[0], dict) else None
    return {
        "method": "getSignatureStatuses",
        "confirmation_status": None if row is None else row.get("confirmationStatus"),
        "err": None if row is None else row.get("err"),
        "slot": _slot(result) if row else None,
        "send": False,
        "executable": False,
    }


def _exact_b58(text: str, size: int) -> bytes | None:
    try:
        raw = solana_tx.b58decode(text, size)
    except ValueError:
        return None
    if solana_tx.b58encode(raw) != text:
        return None
    return raw


def _resolve(decoded: solana_tx.Decoded, rpc: RpcClient, commitment: str) -> list[str]:
    keys = list(decoded.static_keys)
    for lookup in decoded.lookups:
        account = rpc.call("getAccountInfo", [lookup.table, {"encoding": "base64", "commitment": commitment}])
        value = account.get("value")
        if not isinstance(value, dict):
            raise ValueError("lookup table is not usable")
        data = value.get("data")
        if not isinstance(data, list) or len(data) < 1 or not isinstance(data[0], str):
            raise ValueError("lookup table is not usable")
        addresses = solana_tx.lookup_addresses(base64.b64decode(data[0], validate=True))
        for index in lookup.writable + lookup.readonly:
            if index >= len(addresses):
                raise ValueError("lookup table is not usable")
        keys.extend(addresses[index] for index in lookup.writable)
        keys.extend(addresses[index] for index in lookup.readonly)
    decoded.resolved_keys = keys
    for instruction in decoded.instructions:
        if instruction.program_index >= len(keys) or any(index >= len(keys) for index in instruction.account_indexes):
            raise ValueError("transaction is not usable")
        instruction.program_id = keys[instruction.program_index]
    return keys


def _programs(decoded: solana_tx.Decoded, keys: list[str]) -> list[str]:
    program_ids = []
    for instruction in decoded.instructions:
        program = keys[instruction.program_index]
        instruction.program_id = program
        program_ids.append(program)
        if program not in solana_tx.ALLOWED_PROGRAMS:
            raise GateError("program is not verified", "unverified_program", program_ids)
        if program in {solana_tx.TOKEN_PROGRAM, solana_tx.TOKEN_2022} and solana_tx.transfer_checked(instruction.data) is None:
            raise GateError("token instruction is not verified", "unverified_program", program_ids)
        if program == solana_tx.SYSTEM_PROGRAM and solana_tx.system_transfer(instruction.data) is None:
            raise GateError("system instruction is not verified", "unverified_program", program_ids)
    return sorted(set(program_ids))


def _debits(decoded: solana_tx.Decoded, keys: list[str]) -> int | None:
    total = 0
    found = False
    usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    for instruction in decoded.instructions:
        if instruction.program_id == solana_tx.SYSTEM_PROGRAM:
            lamports = solana_tx.system_transfer(instruction.data)
            source = keys[instruction.account_indexes[0]]
            if lamports is None or source != phantom_transport.REQUIRED_WALLET:
                continue
            total += lamports
            found = True
        if instruction.program_id in {solana_tx.TOKEN_PROGRAM, solana_tx.TOKEN_2022}:
            parsed = solana_tx.transfer_checked(instruction.data)
            if parsed is None or len(instruction.account_indexes) < 4:
                raise ValueError("token instruction is not verified")
            mint = keys[instruction.account_indexes[1]]
            owner = keys[instruction.account_indexes[3]]
            if mint != usdc or owner != phantom_transport.REQUIRED_WALLET:
                raise ValueError("token movement is not bound")
            if parsed[0] <= 0:
                raise ValueError("token movement is not bound")
    if not found:
        return None
    return total


def _slot(result: dict) -> int:
    context = result.get("context")
    if not isinstance(context, dict) or isinstance(context.get("slot"), bool) or not isinstance(context.get("slot"), int):
        raise ValueError("rpc slot is not usable")
    return context["slot"]


class GateError(Exception):
    def __init__(self, reason: str, category: str, program_ids: list[str] | None = None):
        self.reason = reason
        self.category = category
        self.program_ids = program_ids or []


def _rejected(reason: str, category: str = "not_usable", program_ids: list[str] | None = None) -> dict:
    return {
        "stage": "rejected",
        "chain_ok": False,
        "reason": reason,
        "error_category": category,
        "program_ids": sorted(set(program_ids or [])),
        "slippage_verified": False,
        "provider_expiry": None,
        "freshness_guarantee": False,
        "execution_allowed": False,
        "send": False,
        "executable": False,
        "live_ready": False,
    }
