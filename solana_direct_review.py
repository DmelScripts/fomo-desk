"""Read-only, short-lived review of one 0.01 SOL -> USDC readiness sample.

This is not the $8 trading workflow. It never signs, submits, reserves funds or
enables agents. RPC URLs, wallet, pool and amount are server-owned constants.
The review seals the exact snapshot and transaction together; a slot supplied
by a caller cannot turn an offline quote into this server-generated evidence.

Solana RPC references: getMultipleAccounts, getLatestBlockhash,
getFeeForMessage, getMinimumBalanceForRentExemption, isBlockhashValid and simulateTransaction at
https://solana.com/docs/rpc/http/.
minContextSlot is a lower bound, not a lock on a bank or future market prices.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import math
import secrets
import time

import phantom_transport
import solana_direct_swap as direct
import solana_inspect
import solana_tx
import solana_whirlpool as whirlpool
import solana_whirlpool_quote as quote_math

MAX_REVIEW_AGE_S = 15
SLIPPAGE_BPS = 100
COMMITMENT = "confirmed"
_KEY = secrets.token_bytes(32)  # Process-local integrity key; never persisted.
_METHODS = {"getGenesisHash", "getAccountInfo", "getMultipleAccounts", "getLatestBlockhash", "getFeeForMessage", "getMinimumBalanceForRentExemption", "isBlockhashValid", "simulateTransaction"}


class ReviewError(Exception):
    def __init__(self, category: str):
        self.category = category
        super().__init__(category)


class ReadOnlyRpc:
    """An allowlisted RPC client; injection is labeled test evidence explicitly."""
    def __init__(self, transport=None):
        self.source = "pinned_mainnet_rpc" if transport is None else "injected_test_rpc"
        self._transport = transport or self._network

    @staticmethod
    def _network(payload):
        import httpx
        response = httpx.post(solana_inspect.MAINNET_RPC, json=payload, timeout=10)
        response.raise_for_status()
        return response.json()

    def call(self, method: str, params: list):
        if method not in _METHODS:
            raise ReviewError("rpc_method_forbidden")
        try:
            response = self._transport({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        except Exception as error:
            raise ReviewError("rpc_unavailable") from error
        if not isinstance(response, dict) or response.get("error") or "result" not in response:
            raise ReviewError("rpc_result_invalid")
        return response["result"]


@dataclass(frozen=True)
class Account:
    address: str
    owner: str | None
    data: bytes | None
    exists: bool = True


@dataclass(frozen=True)
class Review:
    transaction_b64: str
    accounts: tuple[Account, ...]
    snapshot_slot: int
    blockhash_slot: int
    blockhash: str
    last_valid_block_height: int
    token_est_out: int
    token_min_out: int
    trade_fee: int
    network_fee_lamports: int
    fee_context_slot: int
    token_account_rent_lamports: int | None
    created_at: float
    rpc_source: str
    simulation_slot: int | None
    seal: str

    def report(self) -> dict:
        _check_local(self, self.transaction_b64)
        missing = sum(not account.exists for account in self.accounts[-2:])
        return {
            **status(),
            "review_valid": True,
            "snapshot_source": self.rpc_source,
            "rpc_endpoint": solana_inspect.MAINNET_RPC if self.rpc_source == "pinned_mainnet_rpc" else None,
            "commitment": COMMITMENT,
            "snapshot_bound": True,
            "transaction_bound": True,
            "minimum_output_bound": True,
            "snapshot_sha256": _snapshot_digest(self.accounts),
            "transaction_sha256": _digest(self.transaction_b64),
            "message_sha256": hashlib.sha256(_message(self.transaction_b64)).hexdigest(),
            "context_slot": self.snapshot_slot,
            "blockhash_slot": self.blockhash_slot,
            "blockhash": self.blockhash,
            "last_valid_block_height": self.last_valid_block_height,
            "blockhash_valid_when_checked": True,
            "slippage_bps": SLIPPAGE_BPS,
            "estimated_output_base_units": self.token_est_out,
            "minimum_output_base_units": self.token_min_out,
            "pool_trade_fee_input_base_units": self.trade_fee,
            "network_fee_estimate_lamports": self.network_fee_lamports,
            "fee_context_slot": self.fee_context_slot,
            "token_account_creation_plan_verified": True,
            "missing_wallet_token_account_count": missing,
            "existing_wallet_token_account_count": 2 - missing,
            "token_account_rent_estimate_lamports": (self.token_account_rent_lamports or 0) * missing,
            "rent_estimate_context_bound": False,
            "simulation_status": "checked_unsigned" if self.simulation_slot is not None else "not_run",
            "simulation_slot": self.simulation_slot,
            "valid_for_seconds": MAX_REVIEW_AGE_S,
            "review_age_seconds": round(time.monotonic() - self.created_at, 3),
        }


def status() -> dict:
    return {
        "route": "direct_whirlpool_readiness_sample",
        "market": direct.MARKET,
        "input_lamports": direct.INPUT_LAMPORTS,
        "pool": direct.SOL_USDC_POOL,
        "scope": "0.01_SOL_readiness_sample_not_8_USD_trade",
        "reservation_bound": False,
        "trading_intent_bound": False,
        "network_fees_verified": False,
        "account_creation_execution_verified": False,
        "future_market_state_guaranteed": False,
        "live_ready": False,
        "execution_allowed": False,
        "signer_called": False,
        "send": False,
        "review_valid": False,
    }


def _slot(response) -> int:
    slot = response.get("context", {}).get("slot") if isinstance(response, dict) and isinstance(response.get("context"), dict) else None
    if type(slot) is not int or slot <= 0:
        raise ReviewError("rpc_context_invalid")
    return slot


def _record(address: str, value, *, allow_absent: bool = False) -> Account:
    if value is None and allow_absent:
        # Preserve RPC null; missing bytes must never look like an existing ATA.
        return Account(address, None, None, False)
    if value is None:
        raise ReviewError("account_missing")
    if not isinstance(value, dict) or value.get("executable") is not False:
        raise ReviewError("account_invalid")
    data = value.get("data")
    owner = value.get("owner")
    if not isinstance(data, list) or len(data) != 2 or data[1] != "base64" or not isinstance(data[0], str) or len(data[0]) > 20000:
        raise ReviewError("account_invalid")
    try:
        if not isinstance(owner, str) or solana_tx.b58encode(solana_tx.b58decode(owner)) != owner:
            raise ValueError("owner")
        raw = base64.b64decode(data[0], validate=True)
    except (ValueError, TypeError) as error:
        raise ReviewError("account_invalid") from error
    return Account(address, owner, raw)


def _pool(account: Account) -> dict:
    state = whirlpool.pool_state(account.data)
    fields = whirlpool.pool_fields(account.data)
    if account.owner != whirlpool.PROGRAM_ID or state is None or fields is None:
        raise ReviewError("whirlpool_pool_unbound")
    if fields[0] != solana_tx.NATIVE_MINT or fields[2] != solana_tx.USDC_MINT or state["tick_spacing"] != 8:
        raise ReviewError("whirlpool_pool_unbound")
    if state["adaptive_fee"]:
        raise ReviewError("adaptive_fee_unsupported")
    return state


def _addresses(account: Account) -> list[str]:
    state = _pool(account)
    _, vault_a, _, vault_b = whirlpool.pool_fields(account.data)
    start = whirlpool.start_tick_index(state["tick_current_index"], state["tick_spacing"])
    ticks = [whirlpool.tick_array_address(account.address, start - i * 88 * state["tick_spacing"]) for i in range(3)]
    wallet = phantom_transport.REQUIRED_WALLET
    return [account.address, vault_a, vault_b, *ticks, whirlpool.oracle_address(account.address),
            solana_tx.associated_token_address(wallet, solana_tx.NATIVE_MINT, solana_tx.TOKEN_PROGRAM),
            solana_tx.associated_token_address(wallet, solana_tx.USDC_MINT, solana_tx.TOKEN_PROGRAM)]


def _snapshot(rpc: ReadOnlyRpc) -> tuple[tuple[Account, ...], int]:
    initial = rpc.call("getAccountInfo", [direct.SOL_USDC_POOL, {"encoding": "base64", "commitment": COMMITMENT}])
    minimum_slot = _slot(initial)
    pool = _record(direct.SOL_USDC_POOL, initial.get("value"))
    for _ in range(2):
        addresses = _addresses(pool)
        batch = rpc.call("getMultipleAccounts", [addresses, {"encoding": "base64", "commitment": COMMITMENT, "minContextSlot": minimum_slot}])
        slot = _slot(batch)
        values = batch.get("value")
        if slot < minimum_slot or not isinstance(values, list) or len(values) != len(addresses):
            raise ReviewError("snapshot_context_invalid")
        new_pool = _record(direct.SOL_USDC_POOL, values[0])
        if _addresses(new_pool) != addresses:
            pool, minimum_slot = new_pool, slot
            continue
        accounts = tuple(_record(address, value, allow_absent=index >= 6) for index, (address, value) in enumerate(zip(addresses, values)))
        return accounts, slot
    raise ReviewError("pool_moved_during_snapshot")


def prepare(rpc: ReadOnlyRpc | None = None, *, simulate: bool = False) -> Review:
    """Review the unsigned sample, including proven-missing ATA creation plans.

    No account is created and no reservation exists. A successful byte review
    is not a balance, account-creation execution or paid-fee verification.
    """
    if type(simulate) is not bool:
        raise ReviewError("simulation_option_invalid")
    client = rpc or ReadOnlyRpc()
    started = time.monotonic()
    if client.call("getGenesisHash", []) != solana_inspect.MAINNET_GENESIS:
        raise ReviewError("wrong_network")
    accounts, slot = _snapshot(client)
    block = client.call("getLatestBlockhash", [{"commitment": COMMITMENT, "minContextSlot": slot}])
    block_slot = _slot(block)
    value = block.get("value")
    if block_slot < slot or not isinstance(value, dict):
        raise ReviewError("blockhash_context_invalid")
    blockhash, height = value.get("blockhash"), value.get("lastValidBlockHeight")
    try:
        if not isinstance(blockhash, str) or solana_tx.b58encode(solana_tx.b58decode(blockhash)) != blockhash or type(height) is not int or height <= 0:
            raise ValueError("blockhash")
    except ValueError as error:
        raise ReviewError("blockhash_invalid") from error
    records = {item.address: (item.owner, item.data) if item.exists else None for item in accounts}
    if not accounts[6].exists:
        # Orca accepts a nonexistent classic-pool oracle as a default system
        # account. Keep its actual null result in the sealed snapshot above.
        records[accounts[6].address] = (solana_tx.SYSTEM_PROGRAM, b"")
    pool_data = accounts[0].data
    limit = direct.MIN_SQRT_PRICE + 1
    try:
        transaction = direct.build(direct.SOL_USDC_POOL, pool_data, records, SLIPPAGE_BPS, limit, blockhash, slot)
        checked = direct.inspect(transaction, records, SLIPPAGE_BPS, slot)
        if checked.get("byte_checks_passed") is not True:
            raise ReviewError(checked.get("error_category") or "transaction_unbound")
        quote = quote_math.quote_exact_in(_pool(accounts[0]), direct._quote_arrays(direct.SOL_USDC_POOL, pool_data, records, True), direct.INPUT_LAMPORTS, True, SLIPPAGE_BPS, limit, slot)
    except (ValueError, whirlpool.WhirlpoolError, quote_math.QuoteError) as error:
        raise ReviewError(getattr(error, "category", "transaction_unbound")) from error
    fees = client.call("getFeeForMessage", [base64.b64encode(_message(transaction)).decode(), {"commitment": COMMITMENT, "minContextSlot": block_slot}])
    fee_slot = _slot(fees)
    fee = fees.get("value")
    if fee_slot < block_slot or type(fee) is not int or not 0 <= fee < 2**64:
        raise ReviewError("network_fee_unavailable")
    missing = sum(not account.exists for account in accounts[-2:])
    rent = None
    if missing:
        # This RPC returns no context slot. It is only a funding estimate for
        # the verified legacy SPL ATA creation plan, never a paid fee claim.
        rent = client.call("getMinimumBalanceForRentExemption", [165, {"commitment": COMMITMENT}])
        if type(rent) is not int or not 0 < rent < 2**64 or rent * missing >= 2**64:
            raise ReviewError("rent_estimate_unavailable")
    review = Review(transaction, accounts, slot, block_slot, blockhash, height,
                    quote["token_est_out"], quote["token_min_out"], quote["trade_fee"], fee, fee_slot, rent,
                    started, client.source, None, "")
    review = _seal(review)
    result = validate(review, rpc=client, simulate=simulate)
    if simulate:
        from dataclasses import replace
        review = _seal(replace(review, simulation_slot=result["simulation_slot"], seal=""))
    return review


def validate(review: Review, tx_b64: str | None = None, rpc: ReadOnlyRpc | None = None, *, simulate: bool = False) -> dict:
    """Recheck this exact process-local review; never grants trading authority."""
    if type(simulate) is not bool:
        raise ReviewError("simulation_option_invalid")
    transaction = review.transaction_b64 if tx_b64 is None and isinstance(review, Review) else tx_b64
    _check_local(review, transaction)
    client = rpc or ReadOnlyRpc()
    if client.source != review.rpc_source:
        raise ReviewError("rpc_source_changed")
    if client.call("getGenesisHash", []) != solana_inspect.MAINNET_GENESIS:
        raise ReviewError("wrong_network")
    minimum_slot = max(review.snapshot_slot, review.blockhash_slot, review.fee_context_slot)
    validity = client.call("isBlockhashValid", [review.blockhash, {"commitment": COMMITMENT, "minContextSlot": minimum_slot}])
    if _slot(validity) < minimum_slot or validity.get("value") is not True:
        raise ReviewError("blockhash_invalid")
    simulation_slot = None
    if simulate:
        simulation = client.call("simulateTransaction", [transaction, {"encoding": "base64", "commitment": COMMITMENT, "minContextSlot": _slot(validity), "sigVerify": False, "replaceRecentBlockhash": False}])
        simulation_slot = _slot(simulation)
        result = simulation.get("value")
        if simulation_slot < _slot(validity) or not isinstance(result, dict) or "err" not in result or result["err"] is not None or result.get("replacementBlockhash") is not None:
            raise ReviewError("simulation_failed")
    _check_local(review, transaction)
    report = review.report()
    report["simulation_slot"] = simulation_slot if simulate else review.simulation_slot
    report["simulation_status"] = "checked_unsigned" if report["simulation_slot"] is not None else "not_run"
    return report


def _digest(transaction: str) -> str:
    try:
        raw = base64.b64decode(transaction, validate=True)
        if not raw:
            raise ValueError("empty")
    except (ValueError, TypeError) as error:
        raise ReviewError("transaction_invalid") from error
    return hashlib.sha256(raw).hexdigest()


def _message(transaction: str) -> bytes:
    raw = base64.b64decode(transaction, validate=True)
    solana_tx.decode_transaction(raw)
    count, offset = solana_tx._compact(raw, 0)
    return raw[offset + 64 * count:]


def _snapshot_digest(accounts: tuple[Account, ...]) -> str:
    body = [(item.address, item.owner, item.exists, base64.b64encode(item.data).decode() if item.data is not None else None) for item in accounts]
    return hashlib.sha256(json.dumps(body, separators=(",", ":")).encode()).hexdigest()


def _signature(review: Review) -> str:
    body = [_digest(review.transaction_b64), _snapshot_digest(review.accounts), review.snapshot_slot,
            review.blockhash_slot, review.blockhash, review.last_valid_block_height,
            review.token_est_out, review.token_min_out, review.trade_fee, review.network_fee_lamports,
            review.fee_context_slot, review.token_account_rent_lamports, review.created_at,
            review.rpc_source, review.simulation_slot]
    return hmac.new(_KEY, json.dumps(body, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()


def _seal(review: Review) -> Review:
    from dataclasses import replace
    return replace(review, seal=_signature(review))


def _check_local(review: Review, transaction: str) -> None:
    if not isinstance(review, Review) or type(review.created_at) not in (float, int) or not math.isfinite(review.created_at):
        raise ReviewError("review_invalid")
    try:
        if not hmac.compare_digest(review.seal, _signature(review)):
            raise ReviewError("review_invalid")
    except (TypeError, AttributeError) as error:
        raise ReviewError("review_invalid") from error
    if _digest(transaction) != _digest(review.transaction_b64):
        raise ReviewError("review_bytes_changed")
    age = time.monotonic() - review.created_at
    if not 0 <= age <= MAX_REVIEW_AGE_S:
        raise ReviewError("review_expired")
