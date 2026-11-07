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
import re
import subprocess
import time

import db
import exit_policy
import phantom_auth
import phantom_runner
import phantom_transport
import solana_inspect

_REVIEW = "send_review:"
_LEDGER = "send_ledger"
_CONSENT = "live_trading_consent"
_STATEMENT = "Finish everything then enable LIVE trade."
SWAP_BLOCKERS = (
    "Jupiter does not publish the route_v2 remaining-account list for Swap::Whirlpool. The observed build had 12 accounts after the 10 fixed route accounts; Orca's swap struct has 11, and no Jupiter source identifies the extra account.",
    "Jupiter does not publish how route_v2 slippage_bps and quoted_out_amount are enforced as the Whirlpool swap other_amount_threshold.",
)


NETWORK = "solana:mainnet"
_SIGNATURE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{64,88}$")
_BASE64 = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")


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


def _production_runner(argv: list[str], _env: dict, timeout: float):
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
    if body.get("networkId") != NETWORK or body.get("account") != phantom_transport.REQUIRED_WALLET:
        return None
    signature = body.get("signature")
    if isinstance(signature, str) and _SIGNATURE.fullmatch(signature):
        return signature
    return None


def _claim(intent_id: str, digest: str) -> bool:
    """One transaction moves sim_open to submit_unknown and records the digest."""

    def work(conn):
        row = conn.execute(
            "SELECT status, wallet, wallet_id, amount_usd FROM phantom_reservations WHERE intent_id = ?",
            (intent_id,),
        ).fetchone()
        if row is None or row["status"] != "sim_open":
            return False
        if (
            row["wallet"] != phantom_transport.REQUIRED_WALLET
            or row["wallet_id"] != phantom_transport.REQUIRED_WALLET_ID
            or row["amount_usd"] != "8.00"
        ):
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
        ledger[intent_id] = {"sha256": digest, "status": "submit_unknown"}
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
    if not isinstance(intent_id, str) or not intent_id:
        return {"status": "refused", "reason": "intent", "send": False, "resend": False, "subprocess": []}
    try:
        raw = _bytes(tx_b64)
    except Exception:
        return {"status": "refused", "reason": "bytes", "send": False, "resend": False, "subprocess": []}
    digest = hashlib.sha256(raw).hexdigest()
    review = _read(_REVIEW + intent_id)
    reservation = _reservation(intent_id)
    if (
        not isinstance(review, dict)
        or review.get("sha256") != digest
        or review.get("wallet") != phantom_transport.REQUIRED_WALLET
        or review.get("network") != NETWORK
        or reservation is None
        or reservation["status"] != "sim_open"
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
    if not _claim(intent_id, digest):
        return {"status": "refused", "reason": "reservation_lost", "send": False, "resend": False, "subprocess": seen}
    try:
        code, text = runner(send_argv(binary, base64.b64encode(raw).decode("ascii")), env, timeout)
    except (subprocess.TimeoutExpired, OSError):
        return {"status": "submit_unknown", "reason": "timeout", "send": False, "resend": False, "subprocess": seen + ["send"]}
    seen.append("send")
    signature = _signature(text) if code == 0 else None
    if signature is None:
        return {"status": "submit_unknown", "reason": "ambiguous", "send": False, "resend": False, "subprocess": seen}
    return {"status": "submitted", "reason": None, "send": True, "signature": signature, "resend": False, "subprocess": seen}


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
    recorded = _read(_CONSENT)
    policy = exit_policy.view()
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
        "fee_policy_approved": False,
        "aggregate_loss_policy_approved": False,
        "swap_path_verified": False,
        "signer_enabled": False,
    }
    return {
        "ready": all(checks.values()),
        "agents": list(db.AGENT_NAMES),
        "checks": checks,
        "unmet": [name for name, passed in checks.items() if not passed],
        "swap_blockers": list(SWAP_BLOCKERS),
        "fee_policy": "not_approved",
        "aggregate_loss_policy": False,
        "live_ready": False,
        "chat_conditional_live_approval": "not_a_control",
        "recorded_live_applied": isinstance(recorded, dict) and recorded.get("applied") is True,
        "live_applicable": False,
    }


def remember_review(intent_id: str, tx_b64: str) -> dict:
    """Store the reviewed unsigned-byte hash. This does not send."""
    raw = _bytes(tx_b64)
    payload = {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "wallet": phantom_transport.REQUIRED_WALLET,
        "network": "solana:mainnet",
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
    result = send_after_identity(intent_id, tx_b64, runner or _production_runner)
    decision["signer_called"] = "send" in result.get("subprocess", [])
    decision["send"] = result.get("send") is True
    decision["subprocess"] = list(result.get("subprocess", []))
    decision["signature"] = result.get("signature")
    decision["bridge"] = result.get("status")
    return decision


def decide(intent_id: str, tx_b64: str, rpc=None) -> dict:
    checks = {
        "intent_id": isinstance(intent_id, str) and bool(intent_id),
        "mode_paper_until_activation": db.get_setting("mode", "PAPER") == "PAPER",
        "kill_switch_open": phantom_runner.halted() is False,
        "exit_tuple_effective": exit_policy.view()["effective"] is True,
        "live_request_recorded": _consent() is not None,
        "fee_policy_approved": False,
        "aggregate_loss_policy_approved": False,
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
        checks["reservation_open"] = (
            reservation["wallet"] == phantom_transport.REQUIRED_WALLET
            and reservation["wallet_id"] == phantom_transport.REQUIRED_WALLET_ID
            and reservation["amount_usd"] == "8.00"
            and _money(reservation["reserved_usd"]) is not None
            and _money(reservation["reserved_usd"]) <= phantom_runner.BANKROLL
        )
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
    if review and review.get("sha256") == digest and review.get("wallet") == phantom_transport.REQUIRED_WALLET and review.get("network") == "solana:mainnet":
        checks["reviewed_binding"] = True
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
        "fee_policy": "not_approved",
        "aggregate_loss_policy": False,
        "swap_blockers": list(SWAP_BLOCKERS),
    }


def reconcile(intent_id: str) -> dict:
    """A provider status cannot confirm a send this adapter did not record."""
    if not isinstance(intent_id, str) or intent_id not in _ledger():
        return {"status": "not_run", "send": False, "complete": False, "confirmation": None, "executable": False}
    return {
        "status": "refused",
        "reason": "recorded send is not a confirmation",
        "send": False,
        "complete": False,
        "confirmation": None,
        "executable": False,
    }


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
    except (InvalidOperation, TypeError):
        return None
    if amount != amount.quantize(Decimal("0.01")):
        return None
    return amount


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
    return base64.b64decode(tx_b64, validate=True)


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
