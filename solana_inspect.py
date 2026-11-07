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
        program_ids = _programs(decoded, keys, rpc, commitment)
        debited = _debits(decoded, keys)
    except GateError as exc:
        return _rejected(exc.reason, exc.category, exc.program_ids, exc.index, exc.tag)
    except (ValueError, RpcError) as exc:
        return _rejected(str(exc), "not_usable")
    if decoded.signers != [phantom_transport.REQUIRED_WALLET]:
        return _rejected("an extra signer is required", "extra_signer", program_ids)
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
        return _rejected(exc.reason, exc.category, exc.program_ids, exc.index, exc.tag)
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
        "spend_verified": False,
        "intent_verified": False,
        "provider_expiry": None,
        "freshness_guarantee": False,
        "execution_allowed": False,
        "send": False,
        "executable": False,
        "live_ready": False,
        "error_category": None,
        "instruction_index": None,
        "instruction_tag": None,
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
    result["spend_verified"] = False
    result["intent_verified"] = False
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


def _programs(decoded: solana_tx.Decoded, keys: list[str], rpc: RpcClient, commitment: str) -> list[str]:
    """Name every program before rejecting one instruction."""
    program_ids = []
    for instruction in decoded.instructions:
        instruction.program_id = keys[instruction.program_index]
        program_ids.append(instruction.program_id)
    for index, instruction in enumerate(decoded.instructions):
        _check_instruction(index, instruction, keys, program_ids, decoded, rpc, commitment)
    _check_fee(decoded, program_ids)
    return sorted(set(program_ids))


def _check_instruction(index: int, instruction: solana_tx.Instruction, keys: list[str], program_ids: list[str], decoded: solana_tx.Decoded, rpc: RpcClient, commitment: str) -> None:
    program = instruction.program_id
    tag = instruction.data[0] if instruction.data else None
    if program not in solana_tx.ALLOWED_PROGRAMS:
        raise GateError("program is not verified", "unverified_program", program_ids, index, tag)
    if program == solana_tx.COMPUTE_BUDGET:
        if solana_tx.compute_budget_tag(instruction.data) is None:
            raise GateError(
                f"compute budget instruction {index} tag {tag} is not verified",
                "compute_budget_unverified", program_ids, index, tag,
            )
        return
    if program == solana_tx.ASSOCIATED_TOKEN:
        _check_associated(index, instruction, keys, program_ids)
        return
    if program == solana_tx.SYSTEM_PROGRAM:
        _check_system(index, instruction, keys, program_ids, decoded, rpc, commitment)
        return
    if program in {solana_tx.TOKEN_PROGRAM, solana_tx.TOKEN_2022}:
        _check_token(index, instruction, keys, program_ids, rpc, commitment)


def _check_fee(decoded: solana_tx.Decoded, program_ids: list[str]) -> None:
    for index, instruction in enumerate(decoded.instructions):
        if instruction.program_id != solana_tx.COMPUTE_BUDGET:
            continue
        tag = instruction.data[0]
        if tag == 3 and int.from_bytes(instruction.data[1:9], "little") != 0:
            raise GateError(
                f"compute budget instruction {index} tag 3 moves lamports",
                "fee_unbound", program_ids, index, 3,
            )
        if tag == 0 and int.from_bytes(instruction.data[5:9], "little") != 0:
            raise GateError(
                f"compute budget instruction {index} tag 0 moves lamports",
                "fee_unbound", program_ids, index, 0,
            )


def _native_account(address: str, rpc: RpcClient, commitment: str, program_ids: list[str], index: int, tag: int | None) -> None:
    account = rpc.call("getAccountInfo", [address, {"encoding": "base64", "commitment": commitment}])
    value = account.get("value")
    if not isinstance(value, dict):
        raise GateError("token account was not read", "account_unread", program_ids, index, tag)
    data = value.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], str):
        raise GateError("token account was not read", "account_unread", program_ids, index, tag)
    fields = solana_tx.token_account_fields(base64.b64decode(data[0], validate=True))
    if fields is None:
        raise GateError("token account was not read", "account_unread", program_ids, index, tag)
    mint, owner = fields
    if owner != phantom_transport.REQUIRED_WALLET:
        raise GateError(
            f"token account at instruction {index} is not owned by the allowlisted wallet",
            "wallet_unbound", program_ids, index, tag,
        )
    if mint != solana_tx.NATIVE_MINT:
        raise GateError(
            f"token account at instruction {index} mint is not native SOL",
            "token_mint_unbound", program_ids, index, tag,
        )


def _initialized_native(dest: str, decoded: solana_tx.Decoded, keys: list[str]) -> bool:
    for instruction in decoded.instructions:
        if instruction.program_id not in {solana_tx.TOKEN_PROGRAM, solana_tx.TOKEN_2022}:
            continue
        data = instruction.data
        accounts = instruction.account_indexes
        if not accounts or keys[accounts[0]] != dest:
            continue
        tag = data[0] if data else None
        if tag in {16, 18} and len(data) == 33 and len(accounts) >= 2:
            owner = solana_tx.b58encode(data[1:33])
            mint = keys[accounts[1]]
            if owner == phantom_transport.REQUIRED_WALLET and mint == solana_tx.NATIVE_MINT:
                return True
        if tag == 1 and data == bytes([1]) and len(accounts) >= 3:
            if keys[accounts[2]] == phantom_transport.REQUIRED_WALLET and keys[accounts[1]] == solana_tx.NATIVE_MINT:
                return True
    return False


def _check_system(index: int, instruction: solana_tx.Instruction, keys: list[str], program_ids: list[str], decoded: solana_tx.Decoded, rpc: RpcClient, commitment: str) -> None:
    tag = int.from_bytes(instruction.data[:4], "little") if len(instruction.data) >= 4 else None
    if solana_tx.system_transfer(instruction.data) is not None:
        if len(instruction.account_indexes) < 2:
            raise GateError(
                f"system instruction {index} tag 2 is not verified",
                "system_instruction_unverified", program_ids, index, 2,
            )
        source = keys[instruction.account_indexes[0]]
        dest = keys[instruction.account_indexes[1]]
        if source != phantom_transport.REQUIRED_WALLET:
            raise GateError(
                f"system instruction {index} tag 2 source is not the allowlisted wallet",
                "wallet_unbound", program_ids, index, 2,
            )
        if dest == phantom_transport.REQUIRED_WALLET or _initialized_native(dest, decoded, keys):
            return
        _native_account(dest, rpc, commitment, program_ids, index, 2)
        return
    created = solana_tx.system_create(instruction.data)
    if created is not None:
        _lamports, owner = created
        if owner not in {solana_tx.TOKEN_PROGRAM, solana_tx.TOKEN_2022}:
            raise GateError(
                f"system instruction {index} tag 0 owner is not the token program",
                "system_owner_unbound", program_ids, index, 0,
            )
        if not instruction.account_indexes or keys[instruction.account_indexes[0]] != phantom_transport.REQUIRED_WALLET:
            raise GateError(
                f"system instruction {index} tag 0 source is not the allowlisted wallet",
                "wallet_unbound", program_ids, index, 0,
            )
        return
    raise GateError(
        f"system instruction {index} tag {tag} is not verified",
        "system_instruction_unverified", program_ids, index, tag,
    )


def _check_associated(index: int, instruction: solana_tx.Instruction, keys: list[str], program_ids: list[str]) -> None:
    data = instruction.data
    tag = data[0] if data else None
    if data not in {b"", bytes([1])}:
        raise GateError(
            f"associated token instruction {index} tag {tag} is not verified",
            "associated_token_unverified", program_ids, index, tag,
        )
    if len(instruction.account_indexes) < 4:
        raise GateError(
            f"associated token instruction {index} tag {tag} is not verified",
            "associated_token_unverified", program_ids, index, tag,
        )
    payer = keys[instruction.account_indexes[0]]
    owner = keys[instruction.account_indexes[2]]
    mint = keys[instruction.account_indexes[3]]
    if payer != phantom_transport.REQUIRED_WALLET or owner != phantom_transport.REQUIRED_WALLET:
        raise GateError(
            f"associated token instruction {index} tag {tag} owner is not the allowlisted wallet",
            "wallet_unbound", program_ids, index, tag,
        )
    if mint not in solana_tx.BOUND_MINTS:
        raise GateError(
            f"associated token instruction {index} tag {tag} mint is not bound",
            "token_mint_unbound", program_ids, index, tag,
        )


def _check_token(index: int, instruction: solana_tx.Instruction, keys: list[str], program_ids: list[str], rpc: RpcClient, commitment: str) -> None:
    data = instruction.data
    tag = data[0] if data else None
    accounts = instruction.account_indexes
    if tag == 3 and solana_tx.token_transfer(data) is not None:
        raise GateError(
            f"token instruction {index} tag 3 does not name a mint",
            "token_mint_unbound", program_ids, index, 3,
        )
    if tag == 12 and solana_tx.transfer_checked(data) is not None and len(accounts) >= 4:
        mint = keys[accounts[1]]
        owner = keys[accounts[3]]
        if owner != phantom_transport.REQUIRED_WALLET:
            raise GateError(
                f"token instruction {index} tag 12 owner is not the allowlisted wallet",
                "wallet_unbound", program_ids, index, 12,
            )
        if mint == solana_tx.USDC_MINT:
            raise GateError(
                f"token instruction {index} tag 12 debits USDC",
                "usdc_debit_not_buy", program_ids, index, 12,
            )
        if mint != solana_tx.NATIVE_MINT:
            raise GateError(
                f"token instruction {index} tag 12 mint is not bound",
                "token_mint_unbound", program_ids, index, 12,
            )
        return
    if tag == 17 and data == bytes([17]) and accounts:
        _native_account(keys[accounts[0]], rpc, commitment, program_ids, index, 17)
        return
    if tag == 9 and data == bytes([9]) and len(accounts) >= 3:
        if keys[accounts[2]] != phantom_transport.REQUIRED_WALLET:
            raise GateError(
                f"token instruction {index} tag 9 authority is not the allowlisted wallet",
                "wallet_unbound", program_ids, index, 9,
            )
        if keys[accounts[1]] != phantom_transport.REQUIRED_WALLET:
            raise GateError(
                f"token instruction {index} tag 9 destination is not the allowlisted wallet",
                "close_destination_unbound", program_ids, index, 9,
            )
        _native_account(keys[accounts[0]], rpc, commitment, program_ids, index, 9)
        return
    if tag in {16, 18} and len(data) == 33 and len(accounts) >= 2:
        owner = solana_tx.b58encode(data[1:33])
        mint = keys[accounts[1]]
        if owner != phantom_transport.REQUIRED_WALLET:
            raise GateError(
                f"token instruction {index} tag {tag} owner is not the allowlisted wallet",
                "wallet_unbound", program_ids, index, tag,
            )
        if mint not in solana_tx.BOUND_MINTS:
            raise GateError(
                f"token instruction {index} tag {tag} mint is not bound",
                "token_mint_unbound", program_ids, index, tag,
            )
        return
    if tag == 1 and data == bytes([1]) and len(accounts) >= 3:
        mint = keys[accounts[1]]
        owner = keys[accounts[2]]
        if owner != phantom_transport.REQUIRED_WALLET:
            raise GateError(
                f"token instruction {index} tag 1 owner is not the allowlisted wallet",
                "wallet_unbound", program_ids, index, 1,
            )
        if mint not in solana_tx.BOUND_MINTS:
            raise GateError(
                f"token instruction {index} tag 1 mint is not bound",
                "token_mint_unbound", program_ids, index, 1,
            )
        return
    raise GateError(
        f"token instruction {index} tag {tag} is not verified",
        "token_instruction_unverified", program_ids, index, tag,
    )


def _debits(decoded: solana_tx.Decoded, keys: list[str]) -> int | None:
    total = 0
    found = False
    for instruction in decoded.instructions:
        if instruction.program_id == solana_tx.SYSTEM_PROGRAM:
            lamports = solana_tx.system_transfer(instruction.data)
            created = solana_tx.system_create(instruction.data)
            source = keys[instruction.account_indexes[0]] if instruction.account_indexes else None
            if source != phantom_transport.REQUIRED_WALLET:
                continue
            if lamports is not None:
                total += lamports
                found = True
            elif created is not None:
                total += created[0]
                found = True
        if instruction.program_id in {solana_tx.TOKEN_PROGRAM, solana_tx.TOKEN_2022}:
            parsed = solana_tx.transfer_checked(instruction.data)
            if parsed is None or len(instruction.account_indexes) < 4:
                continue
            mint = keys[instruction.account_indexes[1]]
            owner = keys[instruction.account_indexes[3]]
            if owner == phantom_transport.REQUIRED_WALLET and mint == solana_tx.NATIVE_MINT:
                total += parsed[0]
                found = True
    if not found:
        return None
    return total


def _slot(result: dict) -> int:
    context = result.get("context")
    if not isinstance(context, dict) or isinstance(context.get("slot"), bool) or not isinstance(context.get("slot"), int):
        raise ValueError("rpc slot is not usable")
    return context["slot"]


class GateError(Exception):
    def __init__(self, reason: str, category: str, program_ids: list[str] | None = None, index: int | None = None, tag: int | None = None):
        self.reason = reason
        self.category = category
        self.program_ids = program_ids or []
        self.index = index
        self.tag = tag


def _rejected(reason: str, category: str = "not_usable", program_ids: list[str] | None = None, index: int | None = None, tag: int | None = None) -> dict:
    return {
        "stage": "rejected",
        "chain_ok": False,
        "reason": reason,
        "error_category": category,
        "instruction_index": index,
        "instruction_tag": tag,
        "program_ids": sorted(set(program_ids or [])),
        "slippage_verified": False,
        "spend_verified": False,
        "intent_verified": False,
        "provider_expiry": None,
        "freshness_guarantee": False,
        "execution_allowed": False,
        "send": False,
        "executable": False,
        "live_ready": False,
    }
