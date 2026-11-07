"""Guarded Phantom sign-and-send adapter. It is not mounted and not enabled.

The concrete bridge is the pinned `phantom solana send --confirmed true`
command, using the binary and HOME the desk already uses for that session.
submit() does not start it while fee, aggregate, slippage, or route checks
fail. Those checks stay false. No session file is copied.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import subprocess
import time

import db
import exit_policy
import phantom_auth
import phantom_runner
import phantom_transport
import risk_limits
import solana_inspect
import solana_tx

_REVIEW = "send_review:"
_LEDGER = "send_ledger"
_CONSENT = "live_trading_consent"
_STATEMENT = "Finish everything then enable LIVE trade."
SWAP_BLOCKERS = (
    "Jupiter does not publish the route_v2 remaining-account list for Swap::Whirlpool. The observed build had 12 accounts after the 10 fixed route accounts; Orca's swap struct has 11, and no Jupiter source identifies the extra account.",
    "Jupiter does not publish how route_v2 slippage_bps and quoted_out_amount are enforced as the Whirlpool swap other_amount_threshold.",
)


NETWORK = "solana:mainnet"
# @phantom/cli 3.0.0 normalizes the request alias before returning networkId.
# src/actions/send-solana-transaction.ts -> src/utils/network.ts.
CLI_MAINNET_NETWORK = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
_SIGNATURE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{64,88}$")
_BASE64 = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
# A review is an ephemeral local binding, never a promise of chain freshness.
REVIEW_MAX_AGE_S = 120


def addresses_argv(binary: str) -> list[str]:
    """Same read command the desk already uses to identify the server session."""
    return [binary, "wallet", "addresses", "--format", "json"]


def send_argv(binary: str, transaction: str) -> list[str]:
    """Sign-and-broadcast as separate arguments. There is no stdin transaction."""
    if not isinstance(binary, str) or not binary or any(char in binary for char in "\n\r\x00") or binary.startswith("-"):
        raise ValueError("binary")
    if not isinstance(transaction, str) or not _BASE64.fullmatch(transaction):
        raise ValueError("transaction")
    return [
        binary,
        "solana",
        "send",
        "--transaction",
        transaction,
        "--network-id",
        NETWORK,
        "--wallet-id",
        phantom_transport.REQUIRED_WALLET_ID,
        "--confirmed",
        "true",
        "--format",
        "json",
    ]


def _spawn(argv: list[str], env: dict, timeout: float):
    """One list invocation. stdin is closed and the shell is not used."""
    if not isinstance(argv, list) or not all(isinstance(part, str) for part in argv):
        raise ValueError("argv")
    done = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=env.get("HOME"),
        timeout=timeout,
        check=False,
        shell=False,
    )
    return done.returncode, (done.stdout + done.stderr).decode("utf-8", "replace")[:8000]


def _signer_enabled() -> bool:
    """No production activation exists until route and policy proofs are wired."""
    return False


def _production_runner(argv: list[str], _env: dict, timeout: float):
    # Enforce the disabled state at the process boundary as well as submit().
    # Calling the lower-level helper must not bypass the readiness decision.
    if not _signer_enabled():
        raise PermissionError("production signer disabled")
    binary = phantom_auth._binary()
    home = phantom_auth._home()
    if not argv or argv[0] != binary:
        raise RuntimeError("binary mismatch")
    return _spawn(argv, phantom_auth._child_env(home, binary), timeout)


def _identity(text: str) -> dict:
    if phantom_auth._auth_text(text) or "user_code" in text or "approval_url" in text:
        return {"ok": False, "reason": "auth_expired"}
    start, end = text.find("{"), text.rfind("}")
    try:
        body = json.loads(text[start : end + 1]) if start >= 0 and end > start else None
    except json.JSONDecodeError:
        body = None
    rows = body.get("addresses") if isinstance(body, dict) else None
    solana = None
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("addressType") == "solana":
                solana = row.get("address")
                break
    matched = (
        isinstance(body, dict)
        and body.get("walletId") == phantom_transport.REQUIRED_WALLET_ID
        and solana == phantom_transport.REQUIRED_WALLET
    )
    return {"ok": matched, "reason": None if matched else "wallet_mismatch"}


def _signature(text: str) -> str | None:
    start, end = text.find("{"), text.rfind("}")
    try:
        body = json.loads(text[start : end + 1]) if start >= 0 and end > start else None
    except json.JSONDecodeError:
        return None
    if not isinstance(body, dict):
        return None
    if body.get("networkId") != CLI_MAINNET_NETWORK or body.get("account") != phantom_transport.REQUIRED_WALLET:
        return None
    signature = body.get("signature")
    if isinstance(signature, str) and _SIGNATURE.fullmatch(signature):
        import solana_tx
        try:
            decoded = solana_tx.b58decode(signature, 64)
        except ValueError:
            return None
        if solana_tx.b58encode(decoded) == signature:
            return signature
    return None


def _claim(intent_id: str, digest: str, expected_review: dict, message_info: dict) -> bool:
    """One transaction moves sim_open to submit_unknown and records the digest."""

    def work(conn):
        row = conn.execute(
            "SELECT * FROM phantom_reservations WHERE intent_id = ?",
            (intent_id,),
        ).fetchone()
        if not _reservation_valid(row):
            return False
        halt = conn.execute("SELECT value FROM phantom_control WHERE key = 'halt'").fetchone()
        mode = conn.execute("SELECT value FROM settings WHERE key = 'mode'").fetchone()
        if halt is None or halt["value"] != "0" or mode is None or mode["value"] != "PAPER":
            return False
        stored_review = conn.execute(
            "SELECT value FROM phantom_control WHERE key = ?", (_REVIEW + intent_id,)
        ).fetchone()
        try:
            review = json.loads(stored_review["value"]) if stored_review is not None else None
        except (json.JSONDecodeError, TypeError):
            return False
        if review != expected_review or not _review_matches(review, digest, row):
            return False
        current = conn.execute("SELECT value FROM phantom_control WHERE key = ?", (_LEDGER,)).fetchone()
        ledger = {}
        if current is not None:
            try:
                ledger = json.loads(current["value"])
            except json.JSONDecodeError:
                return False
        if not isinstance(ledger, dict) or intent_id in ledger:
            return False
        now = time.time()
        updated = conn.execute(
            "UPDATE phantom_reservations SET status = 'submit_unknown', updated_at = ? WHERE intent_id = ? AND status = 'sim_open'",
            (now, intent_id),
        )
        if updated.rowcount != 1:
            return False
        ledger[intent_id] = {
            "sha256": digest, "status": "submit_unknown",
            "message_sha256": message_info["message_sha256"],
            "blockhash": message_info["blockhash"],
            "wallet": phantom_transport.REQUIRED_WALLET,
            "wallet_id": phantom_transport.REQUIRED_WALLET_ID,
            "network": NETWORK, "attempted_at": now,
            "reservation_sha256": _reservation_digest({**dict(row), "status": "submit_unknown", "updated_at": now}),
        }
        conn.execute(
            """
            INSERT INTO phantom_control (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (_LEDGER, json.dumps(ledger, separators=(",", ":"), sort_keys=True), now),
        )
        return True

    return db.exclusive(work) is True


def send_after_identity(intent_id: str, tx_b64: str, runner, *, binary: str = "phantom", timeout: float = 30) -> dict:
    """Verify the current server wallet, then broadcast the reviewed bytes once.

    The addresses command runs before the send command. A mismatch, an auth
    error, or a timeout of that read does not start the send. A timeout or
    ambiguous send result stays submit_unknown and is not retried.
    """
    if runner is _production_runner and not _signer_enabled():
        return {"status": "refused", "reason": "signer_disabled", "send": False, "resend": False, "subprocess": []}
    if not isinstance(intent_id, str) or not intent_id:
        return {"status": "refused", "reason": "intent", "send": False, "resend": False, "subprocess": []}
    try:
        raw = _bytes(tx_b64)
        message_info = _message_info(raw)
        if any(message_info["signatures"][0]):
            raise ValueError("transaction must be unsigned")
    except Exception:
        return {"status": "refused", "reason": "bytes", "send": False, "resend": False, "subprocess": []}
    digest = hashlib.sha256(raw).hexdigest()
    review = _read(_REVIEW + intent_id)
    reservation = _reservation(intent_id)
    if (
        not _reservation_valid(reservation)
        or not _review_matches(review, digest, reservation)
        or phantom_runner.halted()
        or db.get_setting("mode", "PAPER") != "PAPER"
        or intent_id in _ledger()
    ):
        return {"status": "refused", "reason": "not_claimable", "send": False, "resend": False, "subprocess": []}
    env = phantom_auth._child_env(phantom_auth._home(), binary if binary.startswith("/") else None)
    seen = []
    try:
        code, text = runner(addresses_argv(binary), env, timeout)
    except (subprocess.TimeoutExpired, OSError):
        return {"status": "refused", "reason": "auth_expired", "send": False, "resend": False, "subprocess": ["addresses"]}
    seen.append("addresses")
    identity = _identity(text)
    if code != 0 or not identity["ok"]:
        reason = "auth_expired" if code != 0 or identity["reason"] == "auth_expired" else "wallet_mismatch"
        return {"status": "refused", "reason": reason, "send": False, "resend": False, "subprocess": seen}
    if not _claim(intent_id, digest, review, message_info):
        return {"status": "refused", "reason": "reservation_lost", "send": False, "resend": False, "subprocess": seen}
    try:
        code, text = runner(send_argv(binary, base64.b64encode(raw).decode("ascii")), env, timeout)
    except (subprocess.TimeoutExpired, OSError):
        return {"status": "submit_unknown", "reason": "timeout", "send": False, "resend": False, "subprocess": seen + ["send"]}
    seen.append("send")
    signature = _signature(text) if code == 0 else None
    if signature is None:
        return {"status": "submit_unknown", "reason": "ambiguous", "send": False, "resend": False, "subprocess": seen}
    try:
        persisted = _record_receipt(intent_id, digest, signature)
    except Exception:
        # A persistence error after broadcast must never make this retryable.
        persisted = False
    return {
        "status": "submitted" if persisted else "submit_unknown",
        "reason": None if persisted else "receipt_persistence_failed",
        "send": True, "signature": signature, "resend": False,
        "subprocess": seen, "receipt_persisted": persisted, "confirmed": False,
    }


def _record_receipt(intent_id: str, digest: str, signature: str) -> bool:
    """Store the CLI receipt, never a fill or a released reservation."""
    def work(conn):
        row = conn.execute("SELECT * FROM phantom_reservations WHERE intent_id = ?", (intent_id,)).fetchone()
        stored = conn.execute("SELECT value FROM phantom_control WHERE key = ?", (_LEDGER,)).fetchone()
        if row is None or row["status"] != "submit_unknown" or stored is None:
            return False
        ledger = json.loads(stored["value"])
        record = ledger.get(intent_id) if isinstance(ledger, dict) else None
        if (not isinstance(record, dict) or record.get("sha256") != digest or record.get("signature") is not None
            or record.get("reservation_sha256") != _reservation_digest(row)):
            return False
        record.update({"signature": signature, "status": "submitted_unconfirmed", "receipt_at": time.time()})
        conn.execute("UPDATE phantom_control SET value = ?, updated_at = ? WHERE key = ?",
                     (json.dumps(ledger, separators=(",", ":"), sort_keys=True), time.time(), _LEDGER))
        return True
    return db.exclusive(work) is True


def _message_info(raw: bytes) -> dict:
    """Decode the exact message independently of its mutable signature bytes."""
    decoded = solana_tx.decode_transaction(raw)
    if decoded.fee_payer != phantom_transport.REQUIRED_WALLET or decoded.signers != [phantom_transport.REQUIRED_WALLET]:
        raise ValueError("transaction wallet is not bound")
    count, start = solana_tx._compact(raw, 0)
    signatures, offset = solana_tx._take(raw, start, count * 64)
    message = raw[offset:]
    return {"decoded": decoded, "message": message,
            "message_sha256": hashlib.sha256(message).hexdigest(),
            "blockhash": decoded.blockhash,
            "signatures": [signatures[index:index + 64] for index in range(0, len(signatures), 64)]}


def status() -> dict:
    return {
        "wired": False,
        "signing": "cli_solana_send_behind_gates",
        "send": "disabled",
        "complete": False,
        "automatic_invocation": False,
        "live_ready": False,
        "executable": False,
        "fee_policy": "not_approved",
        "aggregate_loss_policy": False,
    }


def activation_status() -> dict:
    """Machine-checkable gate for enabling the five agents and LIVE later.

    Ready is false while any criterion fails. This function changes nothing.
    """
    consent = _consent()
    policy = exit_policy.view()
    limits = risk_limits.view()
    checks = {
        "mode_still_paper": db.get_setting("mode", "PAPER") == "PAPER",
        "agents_still_off": db.get_setting("agents_enabled", "0") == "0",
        "five_agents_named": db.AGENT_NAMES == ("scanner", "auditor", "analyst", "executor", "risk"),
        "wallet_allowlist": phantom_transport.REQUIRED_WALLET == "7AFJbqsX2UeK7ZW5M1gbXWjiKCai2CJWHpCzXe7woC7t",
        "exit_tuple_effective": policy["effective"] is True and policy["rules"] == exit_policy._RULES,
        "exit_is_not_live_consent": policy["live_consent"] is False,
        "exit_is_not_aggregate": policy["aggregate_loss_policy"] is False and policy["loss_policy"] == "not_approved",
        "live_request_recorded": consent is not None,
        "live_request_not_applied": consent is None or consent.get("applied") is False,
        "fee_policy_approved": limits["approved"],
        "aggregate_loss_policy_approved": limits["approved"],
        "actual_fees_verified": False,
        "live_ledger_verified": False,
        "swap_path_verified": False,
        "signer_enabled": _signer_enabled(),
    }
    return {
        "ready": all(checks.values()),
        "agents": list(db.AGENT_NAMES),
        "checks": checks,
        "unmet": [name for name, passed in checks.items() if not passed],
        "swap_blockers": list(SWAP_BLOCKERS),
        "fee_policy": "configured_unverified" if limits["approved"] else "not_approved",
        "aggregate_loss_policy": limits["approved"],
        "risk_limits": limits,
        "live_ready": False,
    }


def remember_review(intent_id: str, tx_b64: str) -> dict:
    """Store the reviewed unsigned-byte hash. This does not send."""
    if not isinstance(intent_id, str) or not intent_id:
        raise ValueError("intent is not usable")
    raw = _bytes(tx_b64)
    reservation = _reservation(intent_id)
    payload = {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "wallet": phantom_transport.REQUIRED_WALLET,
        "network": NETWORK,
        "reviewed_at": time.time(),
        "reservation_sha256": _reservation_digest(reservation),
    }
    _write(_REVIEW + intent_id, payload)
    return payload


def submit(intent_id: str, tx_b64: str, *, runner=None, rpc=None) -> dict:
    """Start the CLI bridge only when decide() has no unmet check. It never does."""
    if not isinstance(intent_id, str) or not isinstance(tx_b64, str):
        raise TypeError("string inputs required")
    decision = decide(intent_id, tx_b64, rpc)
    decision["signature"] = None
    decision["signer_called"] = False
    decision["send"] = False
    decision["executable"] = False
    decision["live_ready"] = False
    decision["subprocess"] = []
    if decision["unmet"]:
        return decision
    bridge_runner = _production_runner if runner is None else runner
    binary = phantom_auth._binary() if runner is None else "phantom"
    result = send_after_identity(intent_id, tx_b64, bridge_runner, binary=binary)
    decision["signer_called"] = "send" in result.get("subprocess", [])
    decision["send"] = result.get("send") is True
    decision["subprocess"] = list(result.get("subprocess", []))
    decision["signature"] = result.get("signature")
    decision["bridge"] = result.get("status")
    return decision


def decide(intent_id: str, tx_b64: str, rpc=None) -> dict:
    limits = risk_limits.view()
    checks = {
        "intent_id": isinstance(intent_id, str) and bool(intent_id),
        "mode_paper_until_activation": db.get_setting("mode", "PAPER") == "PAPER",
        "kill_switch_open": phantom_runner.halted() is False,
        "signer_enabled": _signer_enabled(),
        "exit_tuple_effective": exit_policy.view()["effective"] is True,
        "live_request_recorded": _consent() is not None,
        "fee_policy_approved": limits["approved"],
        "aggregate_loss_policy_approved": limits["approved"],
        "actual_fees_verified": False,
        "live_ledger_verified": False,
        "reservation_open": False,
        "unknown_submit_clear": False,
        "idempotent": False,
        "reviewed_binding": False,
        "wallet_network": False,
        "inspection_chain_ok": False,
        "spend_verified": False,
        "slippage_verified": False,
        "fee_unbound_clear": False,
        "blockhash_checked": False,
    }
    reservation = _reservation(intent_id) if checks["intent_id"] else None
    if reservation is not None and reservation["status"] == "sim_open":
        checks["reservation_open"] = _reservation_valid(reservation)
        checks["unknown_submit_clear"] = True
    elif reservation is not None and reservation["status"] == "submit_unknown":
        checks["unknown_submit_clear"] = False
    elif reservation is None:
        checks["unknown_submit_clear"] = False
    else:
        checks["unknown_submit_clear"] = reservation["status"] != "submit_unknown"
    ledger = _ledger()
    checks["idempotent"] = checks["intent_id"] and intent_id not in ledger
    review = _read(_REVIEW + intent_id) if checks["intent_id"] else None
    raw = b""
    try:
        raw = _bytes(tx_b64)
    except Exception:
        raw = b""
    digest = hashlib.sha256(raw).hexdigest() if raw else ""
    checks["reviewed_binding"] = _review_matches(review, digest, reservation)
    inspected = None
    if rpc is not None and raw:
        inspected = solana_inspect.inspect(base64.b64encode(raw).decode("ascii"), rpc)
        checks["inspection_chain_ok"] = inspected.get("chain_ok") is True
        checks["spend_verified"] = inspected.get("spend_verified") is True
        checks["slippage_verified"] = inspected.get("slippage_verified") is True
        checks["fee_unbound_clear"] = inspected.get("error_category") != "fee_unbound" and _price_is_zero(raw)
        checks["blockhash_checked"] = inspected.get("blockhash_status") == "checked" and inspected.get("freshness_guarantee") is False
        checks["wallet_network"] = inspected.get("fee_payer") == phantom_transport.REQUIRED_WALLET
    unmet = [name for name, passed in checks.items() if not passed]
    return {
        "checks": checks,
        "unmet": unmet,
        "effects_allowed": not unmet,
        "inspection_category": None if inspected is None else inspected.get("error_category"),
        "fee_policy": "configured_unverified" if limits["approved"] else "not_approved",
        "aggregate_loss_policy": limits["approved"],
        "risk_limits": limits,
        "swap_blockers": list(SWAP_BLOCKERS),
    }


def reconciliation_rpc(*, transport=None):
    """Pinned mainnet client restricted to the three receipt-review reads.

    Construction performs no I/O. Tests inject an envelope transport; production
    callers cannot provide a URL, redirects or signing/broadcast RPC methods.
    """
    def request(payload):
        import httpx
        deadline = time.monotonic() + 15.0
        with httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=False, trust_env=False) as client:
            with client.stream("POST", solana_inspect.MAINNET_RPC, json=payload) as response:
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if time.monotonic() > deadline:
                        raise solana_inspect.RpcError("receipt response timed out")
                    if len(body) > 262144:
                        raise solana_inspect.RpcError("receipt response exceeds limit")
        return json.loads(body)

    class ReceiptRpcClient:
        def call(self, method, params):
            if method not in {"getGenesisHash", "getSignatureStatuses", "getTransaction"} or not isinstance(params, list):
                raise solana_inspect.RpcError("receipt RPC method is not allowed")
            envelope = (request if transport is None else transport)(
                {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            )
            if (not isinstance(envelope, dict) or envelope.get("jsonrpc") != "2.0"
                or type(envelope.get("id")) is not int or envelope["id"] != 1
                or envelope.get("error") is not None or "result" not in envelope):
                raise solana_inspect.RpcError("receipt RPC envelope is not usable")
            result = envelope["result"]
            if method == "getGenesisHash":
                if not isinstance(result, str) or not result:
                    raise solana_inspect.RpcError("receipt genesis is not usable")
            elif not isinstance(result, dict) and not (method == "getTransaction" and result is None):
                raise solana_inspect.RpcError("receipt RPC result is not usable")
            return result

    return ReceiptRpcClient()


def reconcile(intent_id: str, *, rpc=None, commitment: str = "confirmed") -> dict:
    """Read-only chain evidence. Neither a receipt nor this observation is a fill.

    The full reservation remains held, including after verified chain evidence,
    until production position/loss bookkeeping consumes that evidence atomically.
    Missing or conflicting responses never release capacity or permit a resend.
    """
    base = {"send": False, "complete": False, "confirmation": None,
            "executable": False, "live_ready": False, "live_ledger_verified": False,
            "fill_verified": False, "reservation_retained": True, "resend": False}
    record = _ledger().get(intent_id) if isinstance(intent_id, str) else None
    if not isinstance(record, dict):
        return {**base, "status": "not_run", "reason": "recorded_send_missing"}
    signature = record.get("signature")
    if (not isinstance(signature, str) or not _SIGNATURE.fullmatch(signature)
        or record.get("wallet") != phantom_transport.REQUIRED_WALLET
        or record.get("wallet_id") != phantom_transport.REQUIRED_WALLET_ID
        or record.get("network") != NETWORK
        or not isinstance(record.get("message_sha256"), str)):
        return {**base, "status": "refused", "reason": "receipt_binding_missing"}
    if rpc is None:
        return {**base, "status": "refused", "reason": "rpc_required"}
    if commitment not in {"confirmed", "finalized"}:
        return {**base, "status": "refused", "reason": "commitment_invalid"}
    try:
        if rpc.call("getGenesisHash", []) != solana_inspect.MAINNET_GENESIS:
            raise ValueError("network_mismatch")
        statuses = rpc.call("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
        value = statuses.get("value") if isinstance(statuses, dict) else None
        context = statuses.get("context") if isinstance(statuses, dict) else None
        if not isinstance(context, dict) or not _integer(context.get("slot")) or not isinstance(value, list) or len(value) != 1:
            raise ValueError("status_invalid")
        state = value[0]
        if state is None:
            return {**base, "status": "pending", "reason": "signature_not_found"}
        if not isinstance(state, dict) or "err" not in state or not _integer(state.get("slot")) or state["slot"] > context["slot"]:
            raise ValueError("status_invalid")
        accepted = {"finalized"} if commitment == "finalized" else {"confirmed", "finalized"}
        if state.get("confirmationStatus") not in accepted:
            return {**base, "status": "pending", "reason": "confirmation_pending"}
        transaction = rpc.call("getTransaction", [signature, {"encoding": "base64", "commitment": commitment, "maxSupportedTransactionVersion": 0}])
        if transaction is None:
            return {**base, "status": "pending", "reason": "transaction_not_found"}
        evidence = _chain_evidence(transaction, state, record)
    except ValueError as error:
        return {**base, "status": "refused", "reason": str(error)}
    except Exception:
        return {**base, "status": "pending", "reason": "rpc_unavailable"}
    try:
        recorded = _record_observation(intent_id, record, evidence)
    except Exception:
        recorded = False
    if not recorded:
        return {**base, "status": "refused", "reason": "receipt_changed"}
    return {**base, "status": "chain_confirmed" if evidence["transaction_succeeded"] else "chain_failed",
            "confirmation": evidence["confirmation"], "receipt_verified": True,
            "actual_network_fee_verified": True, "actual_total_fee_verified": False,
            "evidence": evidence}


def _integer(value, *, maximum: int = (1 << 64) - 1) -> bool:
    return type(value) is int and 0 <= value <= maximum


def _chain_evidence(transaction: dict, state: dict, receipt: dict) -> dict:
    if not isinstance(transaction, dict) or not _integer(transaction.get("slot")) or transaction.get("slot") != state["slot"]:
        raise ValueError("transaction_slot_mismatch")
    encoded = transaction.get("transaction")
    if not isinstance(encoded, list) or len(encoded) != 2 or encoded[1] != "base64":
        raise ValueError("transaction_bytes_missing")
    try:
        raw = _bytes(encoded[0])
        info = _message_info(raw)
    except Exception as error:
        raise ValueError("transaction_bytes_invalid") from error
    if info["message_sha256"] != receipt["message_sha256"] or info["blockhash"] != receipt.get("blockhash"):
        raise ValueError("transaction_message_mismatch")
    signed = info["signatures"][0]
    if solana_tx.b58encode(signed) != receipt["signature"]:
        raise ValueError("transaction_signature_mismatch")
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(solana_tx.b58decode(phantom_transport.REQUIRED_WALLET)).verify(signed, info["message"])
    except Exception as error:
        raise ValueError("transaction_signature_invalid") from error
    decoded = info["decoded"]
    if transaction.get("version") != ("legacy" if decoded.version == "legacy" else 0):
        raise ValueError("transaction_version_mismatch")
    meta = transaction.get("meta")
    if not isinstance(meta, dict) or "err" not in meta or meta["err"] != state["err"]:
        raise ValueError("transaction_status_mismatch")
    if meta["err"] is not None and not isinstance(meta["err"], dict):
        raise ValueError("transaction_error_invalid")
    keys = list(decoded.static_keys)
    if decoded.lookups:
        loaded = meta.get("loadedAddresses")
        if not isinstance(loaded, dict):
            raise ValueError("loaded_addresses_missing")
        for field, wanted in (("writable", sum(len(item.writable) for item in decoded.lookups)),
                              ("readonly", sum(len(item.readonly) for item in decoded.lookups))):
            addresses = loaded.get(field)
            if not isinstance(addresses, list) or len(addresses) != wanted:
                raise ValueError("loaded_addresses_invalid")
            for address in addresses:
                _public_key(address)
            keys.extend(addresses)
    if len(set(keys)) != len(keys):
        raise ValueError("transaction_accounts_ambiguous")
    before, after, fee = meta.get("preBalances"), meta.get("postBalances"), meta.get("fee")
    if (not isinstance(before, list) or not isinstance(after, list) or len(before) != len(keys) or len(after) != len(keys)
        or not _integer(fee) or any(not _integer(value) for value in before + after)
        or sum(before) - sum(after) != fee):
        raise ValueError("transaction_balances_invalid")
    if meta["err"] is not None and (before[0] - after[0] != fee or before[1:] != after[1:]):
        raise ValueError("failed_transaction_balances_invalid")
    token_before = _token_balances(meta.get("preTokenBalances"), keys)
    token_after = _token_balances(meta.get("postTokenBalances"), keys)
    if (solana_tx.TOKEN_PROGRAM in keys or solana_tx.TOKEN_2022 in keys) and not (token_before or token_after):
        raise ValueError("token_balances_missing")
    token_deltas = []
    for index in sorted(set(token_before) | set(token_after)):
        old, new = token_before.get(index), token_after.get(index)
        identity = old or new
        if old and new and old[:4] != new[:4]:
            raise ValueError("token_balance_identity_changed")
        delta = (new[4] if new else 0) - (old[4] if old else 0)
        if meta["err"] is not None and delta:
            raise ValueError("failed_transaction_token_changes")
        if identity[1] == phantom_transport.REQUIRED_WALLET:
            token_deltas.append({"account": keys[index], "mint": identity[0], "decimals": identity[2], "delta_base_units": str(delta)})
    return {"confirmation": state["confirmationStatus"], "slot": state["slot"],
            "signature": receipt["signature"], "message_sha256": receipt["message_sha256"],
            "wallet": phantom_transport.REQUIRED_WALLET, "network": NETWORK,
            "transaction_succeeded": meta["err"] is None,
            "network_fee_lamports": fee, "wallet_lamport_delta": after[0] - before[0],
            "wallet_token_deltas": token_deltas}


def _public_key(address) -> None:
    try:
        if solana_tx.b58encode(solana_tx.b58decode(address)) != address:
            raise ValueError("key")
    except Exception as error:
        raise ValueError("balance_public_key_invalid") from error


def _token_balances(rows, keys: list[str]) -> dict:
    if not isinstance(rows, list):
        raise ValueError("token_balance_metadata_missing")
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("token_balance_invalid")
        index = row.get("accountIndex")
        amount = row.get("uiTokenAmount")
        if (not _integer(index, maximum=len(keys) - 1) or index in result or not isinstance(amount, dict)
            or not _integer(amount.get("decimals"), maximum=255)):
            raise ValueError("token_balance_invalid")
        _public_key(row.get("mint"))
        _public_key(row.get("owner"))
        value = amount.get("amount")
        if not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]{0,19}", value) or int(value) >= 1 << 64:
            raise ValueError("token_balance_invalid")
        if row.get("programId") not in {solana_tx.TOKEN_PROGRAM, solana_tx.TOKEN_2022}:
            raise ValueError("token_program_invalid")
        result[index] = (row["mint"], row["owner"], amount["decimals"], row["programId"], int(value))
    return result


def _record_observation(intent_id: str, expected: dict, evidence: dict) -> bool:
    def work(conn):
        stored = conn.execute("SELECT value FROM phantom_control WHERE key = ?", (_LEDGER,)).fetchone()
        row = conn.execute("SELECT * FROM phantom_reservations WHERE intent_id = ?", (intent_id,)).fetchone()
        if stored is None or row is None or row["status"] != "submit_unknown":
            return False
        if row["wallet"] != phantom_transport.REQUIRED_WALLET or row["wallet_id"] != phantom_transport.REQUIRED_WALLET_ID or row["chain"] != NETWORK:
            return False
        ledger = json.loads(stored["value"])
        if (not isinstance(ledger, dict) or ledger.get(intent_id) != expected
            or expected.get("reservation_sha256") != _reservation_digest(row)):
            return False
        ledger[intent_id]["chain_observation"] = evidence
        ledger[intent_id]["status"] = "chain_confirmed" if evidence["transaction_succeeded"] else "chain_failed"
        conn.execute("UPDATE phantom_control SET value = ?, updated_at = ? WHERE key = ?",
                     (json.dumps(ledger, separators=(",", ":"), sort_keys=True), time.time(), _LEDGER))
        return True
    return db.exclusive(work) is True


def _consent():
    row = _read(_CONSENT)
    if not isinstance(row, dict):
        return None
    if row.get("live_trading_requested") is not True or row.get("applied") is not False:
        return None
    if row.get("agents_requested") != list(db.AGENT_NAMES):
        return None
    if row.get("fee_policy") != "not_approved" or row.get("aggregate_loss_policy") is not False:
        return None
    if row.get("statement") != _STATEMENT:
        return None
    return row


def _reservation(intent_id: str):
    rows = db.query("SELECT * FROM phantom_reservations WHERE intent_id = ?", (intent_id,))
    return rows[0] if rows else None


def _ledger() -> dict:
    row = _read(_LEDGER)
    return row if isinstance(row, dict) else {}


def _money(text):
    from decimal import Decimal, InvalidOperation
    try:
        amount = Decimal(text)
        if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal("0.01")):
            return None
    except (InvalidOperation, TypeError, ValueError):
        return None
    return amount


def _reservation_valid(row) -> bool:
    if row is None:
        return False
    reserved = _money(row["reserved_usd"])
    fee = _money(row["fee_usd"])
    return (
        row["status"] == "sim_open"
        and row["wallet"] == phantom_transport.REQUIRED_WALLET
        and row["wallet_id"] == phantom_transport.REQUIRED_WALLET_ID
        and row["chain"] == NETWORK
        and row["amount_usd"] == "8.00"
        and reserved is not None and fee is not None
        and reserved == phantom_runner.SIZE + fee
        and reserved <= phantom_runner.BANKROLL
    )


def _reservation_digest(row) -> str | None:
    if row is None:
        return None
    return hashlib.sha256(json.dumps(dict(row), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _review_matches(review, digest: str, reservation) -> bool:
    if not isinstance(review, dict) or reservation is None or not digest:
        return False
    reviewed_at = review.get("reviewed_at")
    return (
        review.get("sha256") == digest
        and review.get("wallet") == phantom_transport.REQUIRED_WALLET
        and review.get("network") == NETWORK
        and review.get("reservation_sha256") == _reservation_digest(reservation)
        and type(reviewed_at) in (int, float) and math.isfinite(reviewed_at)
        and 0 <= time.time() - reviewed_at <= REVIEW_MAX_AGE_S
    )


def _price_is_zero(raw: bytes) -> bool:
    import solana_tx
    try:
        decoded = solana_tx.decode_transaction(raw)
    except ValueError:
        return False
    for instruction in decoded.instructions:
        program = decoded.static_keys[instruction.program_index] if instruction.program_index < len(decoded.static_keys) else ""
        if program != solana_tx.COMPUTE_BUDGET:
            continue
        parsed = solana_tx.compute_budget_payload(instruction.data)
        if parsed and parsed[0] == 3 and int.from_bytes(parsed[1], "little") != 0:
            return False
    return True


def _bytes(tx_b64: str) -> bytes:
    if not isinstance(tx_b64, str):
        raise ValueError("transaction is not usable")
    raw = base64.b64decode(tx_b64, validate=True)
    if not raw:
        raise ValueError("transaction is not usable")
    return raw


def _read(key: str):
    rows = db.query("SELECT value FROM phantom_control WHERE key = ?", (key,))
    if not rows:
        return None
    try:
        return json.loads(rows[0]["value"])
    except json.JSONDecodeError:
        return None


def _write(key: str, payload: dict) -> None:
    db.execute(
        """
        INSERT INTO phantom_control (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, json.dumps(payload, separators=(",", ":"), sort_keys=True), time.time()),
    )
