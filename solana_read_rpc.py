"""Read-only Solana JSON-RPC for the canonical SOL/USDC pool.

Reads stay off until solana_read_enabled is set and rpc_url is the official
mainnet endpoint. The client allows documented read methods only. It does not
send a transaction, follow a redirect, or attach a cookie, token, or session.
"""

from __future__ import annotations

import time

import db
import solana_direct_swap
import solana_inspect
import solana_tx

ALLOWED_URL = solana_inspect.MAINNET_RPC
MAINNET_GENESIS = solana_inspect.MAINNET_GENESIS
READ_KEY = "solana_read_enabled"
MAX_CALLS = 9
MAX_BODY = 2_000_000
TIMEOUT_S = 8.0
METHODS = frozenset({
    "getGenesisHash",
    "getMultipleAccounts",
    "getMinimumBalanceForRentExemption",
    "getLatestBlockhash",
    "getFeeForMessage",
    "isBlockhashValid",
    "getBlockHeight",
    "simulateTransaction",
    "getSignatureStatuses",
    "getTransaction",
})
# Tests may install a transport. Production leaves this unset and uses HTTP.
TRANSPORT = None


class RpcError(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class ReadRpc:
    """One budget of read calls. A refused method never reaches the transport."""

    def __init__(self, transport):
        self._transport = transport
        self.methods: list[str] = []
        self.params: list = []

    @property
    def calls(self) -> int:
        return len(self.methods)

    def call(self, method: str, params: list):
        if method not in METHODS or method == "sendTransaction":
            raise RpcError("method_refused")
        if len(self.methods) >= MAX_CALLS:
            raise RpcError("call_budget")
        self.methods.append(method)
        self.params.append(params)
        try:
            envelope = self._transport({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        except RpcError:
            raise
        except Exception as exc:
            raise RpcError("transport") from exc
        if not isinstance(envelope, dict) or envelope.get("error") is not None or "result" not in envelope:
            raise RpcError("rpc_result")
        return envelope["result"]


def reads_enabled() -> bool:
    """Off unless this database explicitly enabled reads on the official URL."""
    rows = db.query("SELECT value FROM phantom_control WHERE key = ?", (READ_KEY,))
    if not rows or rows[0]["value"] != "1":
        return False
    return db.get_setting("rpc_url") == ALLOWED_URL


def enable_reads() -> None:
    """Record the flag. The URL still has to be the official mainnet endpoint."""
    if db.get_setting("rpc_url") != ALLOWED_URL:
        raise RpcError("endpoint")
    db.execute(
        """
        INSERT INTO phantom_control (key, value, updated_at) VALUES (?, '1', ?)
        ON CONFLICT(key) DO UPDATE SET value = '1', updated_at = excluded.updated_at
        """,
        (READ_KEY, time.time()),
    )


def configured_reader() -> ReadRpc | None:
    """The reader the protected routes use. None while the feature is off."""
    if not reads_enabled():
        return None
    return ReadRpc(TRANSPORT or http_transport)


def http_transport(payload: dict) -> dict:
    """POST the official endpoint once. Redirects and cookies are not used."""
    import httpx
    if not isinstance(payload, dict) or payload.get("method") not in METHODS:
        raise RpcError("method_refused")
    try:
        response = httpx.post(
            ALLOWED_URL,
            json=payload,
            timeout=httpx.Timeout(TIMEOUT_S, connect=4.0),
            follow_redirects=False,
            headers={"content-type": "application/json", "accept": "application/json"},
        )
    except httpx.TimeoutException as exc:
        raise RpcError("timeout") from exc
    except httpx.HTTPError as exc:
        raise RpcError("transport") from exc
    if response.is_redirect or response.status_code in {301, 302, 303, 307, 308}:
        raise RpcError("redirect")
    if response.status_code != 200:
        raise RpcError("http_status")
    if len(response.content) > MAX_BODY:
        raise RpcError("body")
    try:
        body = response.json()
    except ValueError as exc:
        raise RpcError("rpc_result") from exc
    return body


def load_bundle(rpc: ReadRpc, expected_slot: int | None = None) -> dict:
    """Genesis, one account batch, rent, and the latest blockhash. No retry."""
    failed = {"ok": False, "fetched": False, "sign": False, "broadcastable": False, "runtime_ready": False, "retry": False}
    fetched_at = time.time()
    try:
        genesis = rpc.call("getGenesisHash", [])
    except RpcError as exc:
        return failed | {"reason": exc.reason}
    if genesis != MAINNET_GENESIS:
        return failed | {"reason": "genesis"}
    addresses = solana_direct_swap.batch_addresses()
    if len(addresses) > 12:
        return failed | {"reason": "batch_size"}
    try:
        fetched = solana_direct_swap.fetch_state(rpc)
    except RpcError as exc:
        return failed | {"reason": exc.reason}
    if not fetched.get("ok"):
        fetched["retry"] = False
        return fetched
    if expected_slot is not None and fetched["context_slot"] != expected_slot:
        return failed | {
            "reason": "slot_mismatch",
            "fetched": True,
            "stale": True,
            "context_slot": fetched["context_slot"],
        }
    slot = fetched["context_slot"]
    try:
        rent = rpc.call("getMinimumBalanceForRentExemption", [165, {"commitment": "confirmed"}])
        block = rpc.call("getLatestBlockhash", [{"commitment": "confirmed", "minContextSlot": slot}])
    except RpcError as exc:
        return failed | {"reason": exc.reason, "context_slot": slot}
    parsed = _blockhash(block, slot)
    if parsed is None or not isinstance(rent, int) or isinstance(rent, bool) or rent <= 0:
        return failed | {"reason": "blockhash" if parsed is None else "rent", "context_slot": slot}
    fetched.update(parsed)
    fetched["rent_lamports"] = rent
    fetched["genesis"] = genesis
    fetched["fetched_at"] = fetched_at
    fetched["slot_is_ttl"] = False
    return fetched


def finish_read(rpc: ReadRpc, message: bytes, blockhash: str, slot: int) -> dict:
    """Fee, validity, and block height for the message just built. No retry."""
    encoded = __import__("base64").b64encode(message).decode("ascii")
    config = {"commitment": "confirmed", "minContextSlot": slot}
    try:
        fee = rpc.call("getFeeForMessage", [encoded, config])
        valid = rpc.call("isBlockhashValid", [blockhash, config])
        height = rpc.call("getBlockHeight", [config])
    except RpcError as exc:
        return {"ok": False, "reason": exc.reason}
    fee_lamports = fee.get("value") if isinstance(fee, dict) else None
    valid_bit = valid.get("value") if isinstance(valid, dict) else None
    fee_slot = _context_slot(fee)
    valid_slot = _context_slot(valid)
    if not isinstance(fee_lamports, int) or isinstance(fee_lamports, bool) or fee_lamports < 0:
        return {"ok": False, "reason": "fee_unavailable"}
    if valid_bit is not True:
        return {"ok": False, "reason": "blockhash_invalid"}
    if not isinstance(height, int) or isinstance(height, bool) or height <= 0:
        return {"ok": False, "reason": "block_height"}
    if (fee_slot is not None and fee_slot < slot) or (valid_slot is not None and valid_slot < slot):
        return {"ok": False, "reason": "slot_binding"}
    return {
        "ok": True,
        "network_fee_lamports": fee_lamports,
        "blockhash_valid": True,
        "block_height": height,
    }


def simulate(rpc: ReadRpc, tx_b64: str, slot: int) -> dict:
    """Unsigned simulation. sigVerify stays false and the blockhash is not replaced."""
    try:
        result = rpc.call("simulateTransaction", [tx_b64, {
            "encoding": "base64",
            "commitment": "confirmed",
            "sigVerify": False,
            "replaceRecentBlockhash": False,
            "minContextSlot": slot,
        }])
    except RpcError as exc:
        return {"simulated": False, "err": exc.reason, "sign": False, "broadcastable": False}
    value = result.get("value") if isinstance(result, dict) else None
    if not isinstance(value, dict):
        return {"simulated": False, "err": "simulation_shape", "sign": False, "broadcastable": False}
    err = value.get("err")
    return {
        "simulated": err is None,
        "err": err,
        "units": value.get("unitsConsumed") if isinstance(value.get("unitsConsumed"), int) else None,
        "sign": False,
        "broadcastable": False,
        "runtime_ready": False,
    }


def read_receipt(rpc: ReadRpc, signature: str) -> dict | None:
    """Bind a receipt to the chain payload. A finalized flag by itself is not enough."""
    try:
        status = rpc.call("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
        transaction = rpc.call("getTransaction", [signature, {
            "encoding": "base64",
            "commitment": "confirmed",
            "maxSupportedTransactionVersion": 0,
        }])
    except RpcError:
        return None
    return _bind_receipt(signature, status, transaction)


def _bind_receipt(signature: str, status, transaction) -> dict | None:
    row = None
    if isinstance(status, dict) and isinstance(status.get("value"), list) and status["value"]:
        row = status["value"][0]
    if not isinstance(row, dict) or not isinstance(transaction, dict):
        return None
    if row.get("err") is not None or transaction.get("meta", {}).get("err") is not None:
        return {"err": row.get("err") if row.get("err") is not None else transaction.get("meta", {}).get("err"), "confirmation_status": row.get("confirmationStatus")}
    encoded = transaction.get("transaction")
    if not isinstance(encoded, list) or not encoded or not isinstance(encoded[0], str):
        return None
    try:
        message = solana_tx.message_bytes(encoded[0])
        import hashlib
        digest = hashlib.sha256(message).hexdigest()
        found = solana_tx.first_signature(encoded[0])
        decoded = solana_tx.decode_transaction(__import__("base64").b64decode(encoded[0], validate=True))
    except (ValueError, Exception):
        return None
    if found != signature:
        return None
    meta = transaction.get("meta") if isinstance(transaction.get("meta"), dict) else {}
    fee = meta.get("fee")
    delta = _usdc_delta(meta, decoded)
    slot = transaction.get("slot")
    if row.get("slot") != slot:
        return None
    return {
        "signature": found,
        "network": "solana:mainnet",
        "wallet": decoded.fee_payer,
        "message_sha256": digest,
        "acquired_raw": delta,
        "network_fee_lamports": fee if isinstance(fee, int) and not isinstance(fee, bool) else None,
        "err": None,
        "confirmation_status": row.get("confirmationStatus"),
        "slot": slot if isinstance(slot, int) and not isinstance(slot, bool) else None,
        "block_height": row.get("blockHeight") if isinstance(row.get("blockHeight"), int) else None,
        "blockhash": decoded.blockhash,
    }


def _usdc_delta(meta: dict, decoded) -> int | None:
    """Post minus pre for the wallet's USDC. The post balance itself is not the fill."""
    pre = _token_amount(meta.get("preTokenBalances"), decoded, solana_tx.USDC_MINT)
    post = _token_amount(meta.get("postTokenBalances"), decoded, solana_tx.USDC_MINT)
    if post is None:
        return None
    return post - (pre or 0)


def _token_amount(rows, decoded, mint: str) -> int | None:
    if not isinstance(rows, list):
        return None
    owner = decoded.fee_payer
    for row in rows:
        if not isinstance(row, dict) or row.get("mint") != mint:
            continue
        if row.get("owner") not in (None, owner):
            continue
        amount = row.get("uiTokenAmount", {}).get("amount") if isinstance(row.get("uiTokenAmount"), dict) else None
        if isinstance(amount, str) and amount.isdigit():
            return int(amount)
    return None


def _blockhash(result, account_slot: int) -> dict | None:
    if not isinstance(result, dict):
        return None
    value = result.get("value")
    if not isinstance(value, dict):
        return None
    blockhash = value.get("blockhash")
    last = value.get("lastValidBlockHeight")
    slot = _context_slot(result)
    if not isinstance(blockhash, str) or blockhash == solana_direct_swap.PLACEHOLDER_BLOCKHASH:
        return None
    if not isinstance(last, int) or isinstance(last, bool) or last <= 0:
        return None
    if slot is not None and slot < account_slot:
        return None
    return {
        "blockhash": blockhash,
        "last_valid_block_height": last,
        "blockhash_slot": slot,
        "placeholder": False,
    }


def _context_slot(result) -> int | None:
    if not isinstance(result, dict):
        return None
    context = result.get("context")
    slot = context.get("slot") if isinstance(context, dict) else None
    if isinstance(slot, bool) or not isinstance(slot, int):
        return None
    return slot
