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
from decimal import Decimal

import db
import exit_policy
import phantom_auth
import phantom_runner
import phantom_transport
import solana_direct_swap
import solana_inspect
import spend_policy

_REVIEW = "send_review:"
_LEDGER = "send_ledger"
_CONSENT = "live_trading_consent"
_STATEMENT = "Finish everything then enable LIVE trade."
SWAP_BLOCKERS = (
    "Jupiter does not publish the route_v2 remaining-account list for Swap::Whirlpool. The observed build had 12 accounts after the 10 fixed route accounts; Orca's swap struct has 11, and no Jupiter source identifies the extra account.",
    "Jupiter does not publish how route_v2 slippage_bps and quoted_out_amount are enforced as the Whirlpool swap other_amount_threshold.",
)


NETWORK = "solana:mainnet"
STATE_RPC = None
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


def fresh_block_height() -> int | None:
    """One getBlockHeight on the configured reader. None when that read is unavailable."""
    import solana_read_rpc
    reader = solana_read_rpc.configured_reader()
    if reader is None:
        return None
    try:
        height = reader.call("getBlockHeight", [{"commitment": "confirmed"}])
    except solana_read_rpc.RpcError:
        return None
    if isinstance(height, bool) or not isinstance(height, int) or height <= 0:
        return None
    return height


def claim_after_identity(intent_id: str, digest: str, *, message_sha256: str, expect_status: str, last_valid, block_height) -> str | None:
    """Claim only after identity. None means the row moved. A reason means it did not.

    Halt, the policy stamp, the reserved message, status, and expiry are read
    inside this immediate transaction. A control write that commits after this
    transaction returns, and before the subprocess returns, is still outside it.
    """

    def work(conn):
        if _halted_conn(conn):
            return "kill_switch"
        row = conn.execute(
            "SELECT status, wallet, wallet_id, amount_usd, result_json FROM phantom_reservations WHERE intent_id = ?",
            (intent_id,),
        ).fetchone()
        if row is None or row["status"] != expect_status:
            return "not_claimable"
        if (
            row["wallet"] != phantom_transport.REQUIRED_WALLET
            or row["wallet_id"] != phantom_transport.REQUIRED_WALLET_ID
            or row["amount_usd"] != "8.00"
        ):
            return "not_claimable"
        try:
            payload = json.loads(row["result_json"]) if row["result_json"] else {}
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        stamp = payload.get("policy_stamp")
        if isinstance(stamp, dict) and _stamp_changed(conn, stamp):
            return "policy_changed"
        bound = payload.get("message_sha256") if expect_status == "sim_open" else payload.get("exit_message_sha256")
        if bound != message_sha256:
            return "message_unbound"
        if isinstance(last_valid, int) and not isinstance(last_valid, bool):
            if isinstance(block_height, bool) or not isinstance(block_height, int) or block_height <= 0:
                return "expiry_unproven"
            if block_height > last_valid:
                return "blockhash_expired"
        now = time.time()
        if expect_status == "sim_open":
            current = conn.execute("SELECT value FROM phantom_control WHERE key = ?", (_LEDGER,)).fetchone()
            ledger = {}
            if current is not None:
                try:
                    ledger = json.loads(current["value"])
                except json.JSONDecodeError:
                    return "not_claimable"
            if not isinstance(ledger, dict) or intent_id in ledger:
                return "not_claimable"
            payload["chain_submit"] = True
            payload["resubmit"] = False
            payload["confirmed"] = False
            updated = conn.execute(
                """
                UPDATE phantom_reservations
                SET status = 'submit_unknown', result_json = ?, updated_at = ?
                WHERE intent_id = ? AND status = 'sim_open'
                """,
                (json.dumps(payload, separators=(",", ":"), sort_keys=True), now, intent_id),
            )
            if updated.rowcount != 1:
                return "not_claimable"
            ledger[intent_id] = {"sha256": digest, "status": "submit_unknown"}
            conn.execute(
                """
                INSERT INTO phantom_control (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (_LEDGER, json.dumps(ledger, separators=(",", ":"), sort_keys=True), now),
            )
            return None
        if payload.get("exit_claimed") is True:
            return "not_claimable"
        payload["exit_claimed"] = True
        payload["resend"] = False
        updated = conn.execute(
            """
            UPDATE phantom_reservations
            SET status = 'exit_unknown', result_json = ?, updated_at = ?
            WHERE intent_id = ? AND status = 'confirmed'
            """,
            (json.dumps(payload, separators=(",", ":"), sort_keys=True), now, intent_id),
        )
        if updated.rowcount != 1:
            return "not_claimable"
        return None

    return db.exclusive(work)


def _stamp_changed(conn, stamp: dict) -> bool:
    fee = conn.execute("SELECT updated_at FROM phantom_control WHERE key = 'fee_policy'").fetchone()
    loss = conn.execute("SELECT updated_at FROM phantom_control WHERE key = 'aggregate_loss_policy'").fetchone()
    halt = conn.execute("SELECT value, updated_at FROM phantom_control WHERE key = 'halt'").fetchone()
    exit_row = conn.execute("SELECT updated_at FROM phantom_control WHERE key = 'exit_policy_approval'").fetchone()
    fee_at = None if fee is None else fee["updated_at"]
    loss_at = None if loss is None else loss["updated_at"]
    halt_value = None if halt is None else halt["value"]
    halt_at = None if halt is None else halt["updated_at"]
    exit_at = None if exit_row is None else exit_row["updated_at"]
    return (
        fee_at != stamp.get("fee")
        or loss_at != stamp.get("loss")
        or halt_value != stamp.get("halt")
        or halt_at != stamp.get("halt_at")
        or exit_at != stamp.get("exit")
    )


def _halted_conn(conn) -> bool:
    row = conn.execute("SELECT value FROM phantom_control WHERE key = 'halt'").fetchone()
    if row is None:
        return True
    return row["value"] != "0"


def _send_blocked(intent_id: str, raw: bytes) -> str | None:
    """Gates that must hold before any signer process. Caller bytes are not a proof."""
    if phantom_runner.halted():
        return "kill_switch"
    if db.get_setting("mode", "PAPER") != "PAPER":
        return "live_locked"
    source = spend_policy.usd_source()
    if source["approved"] is not True:
        return source.get("reason") or "usd_source_unapproved"
    if not spend_policy.fee_view()["approved"] or not spend_policy.aggregate_view()["approved"]:
        return "policy_unrecorded"
    reservation = _reservation(intent_id)
    if reservation is None or reservation["status"] != "sim_open":
        return "not_claimable"
    stored = {}
    if reservation["result_json"]:
        try:
            parsed = json.loads(reservation["result_json"])
            stored = parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            stored = {}
    import solana_tx
    try:
        message = hashlib.sha256(solana_tx.message_bytes(base64.b64encode(raw).decode("ascii"))).hexdigest()
    except ValueError:
        return "bytes"
    if stored.get("message_sha256") != message or stored.get("origin") != "direct_whirlpool":
        return "message_unbound"
    encoded = base64.b64encode(raw).decode("ascii")
    swap_reason = solana_direct_swap.prove_entry_tx(encoded, stored, None)
    if swap_reason == "account_proof":
        bundle = _spawn_accounts()
        if bundle is None:
            return "account_proof"
        swap_reason = solana_direct_swap.prove_entry_tx(
            encoded, stored, bundle.get("accounts"), wsol_exists=bundle.get("wsol_exists") is True,
        )
    if swap_reason:
        return swap_reason
    review = _read(_REVIEW + intent_id)
    digest = hashlib.sha256(raw).hexdigest()
    if not isinstance(review, dict) or review.get("sha256") != digest or review.get("wallet") != phantom_transport.REQUIRED_WALLET or review.get("network") != NETWORK:
        return "review_unbound"
    if intent_id in _ledger():
        return "not_claimable"
    return None


def _spawn_accounts() -> dict | None:
    """Trusted read used only after the message shape already matches. A missing read is not a pass."""
    import solana_read_rpc
    reader = solana_read_rpc.configured_reader()
    if reader is None:
        return None
    try:
        bundle = solana_read_rpc.load_bundle(reader)
    except solana_read_rpc.RpcError:
        return None
    if not isinstance(bundle, dict) or bundle.get("ok") is not True:
        return None
    import solana_tx
    accounts = dict(bundle.get("accounts") or {})
    if bundle.get("wsol_exists") is not True:
        wsol = solana_tx.associated_token_address(phantom_transport.REQUIRED_WALLET, solana_tx.NATIVE_MINT, solana_tx.TOKEN_PROGRAM)
        accounts.pop(wsol, None)
    bundle["accounts"] = accounts
    return bundle


def _record_signature(intent_id: str, signature: str, digest: str) -> None:
    """Keep the signature. The raw transaction is not written."""

    def work(conn):
        row = conn.execute("SELECT result_json FROM phantom_reservations WHERE intent_id = ?", (intent_id,)).fetchone()
        try:
            body = json.loads(row["result_json"]) if row and row["result_json"] else {}
        except json.JSONDecodeError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        body["signature"] = signature
        body["message_sha256"] = body.get("message_sha256")
        body["chain_submit"] = True
        body.pop("transaction", None)
        conn.execute(
            "UPDATE phantom_reservations SET result_json = ?, updated_at = ? WHERE intent_id = ?",
            (json.dumps(body, separators=(",", ":"), sort_keys=True), time.time(), intent_id),
        )
        current = conn.execute("SELECT value FROM phantom_control WHERE key = ?", (_LEDGER,)).fetchone()
        try:
            ledger = json.loads(current["value"]) if current else {}
        except json.JSONDecodeError:
            ledger = {}
        if not isinstance(ledger, dict):
            ledger = {}
        ledger[intent_id] = {"sha256": digest, "signature": signature, "status": "submit_unknown"}
        conn.execute(
            """
            INSERT INTO phantom_control (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (_LEDGER, json.dumps({key: {"sha256": value.get("sha256"), "signature": value.get("signature"), "status": value.get("status")} for key, value in ledger.items() if isinstance(value, dict)}, separators=(",", ":"), sort_keys=True), time.time()),
        )

    db.exclusive(work)


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
    blocked = _send_blocked(intent_id, raw)
    if blocked:
        return {
            "status": "refused",
            "reason": blocked,
            "send": False,
            "resend": False,
            "subprocess": [],
            "cli_transport": "argv_only",
            "stdin_transaction": False,
            "signature": None,
        }
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
    stored = {}
    if reservation["result_json"]:
        try:
            parsed = json.loads(reservation["result_json"])
            stored = parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            stored = {}
    import solana_tx
    try:
        message_sha = hashlib.sha256(solana_tx.message_bytes(base64.b64encode(raw).decode("ascii"))).hexdigest()
    except ValueError:
        return {"status": "refused", "reason": "bytes", "send": False, "resend": False, "subprocess": seen}
    last_valid = stored.get("last_valid_block_height")
    height = None
    if isinstance(last_valid, int) and not isinstance(last_valid, bool):
        height = fresh_block_height()
    refused = claim_after_identity(
        intent_id, digest, message_sha256=message_sha, expect_status="sim_open",
        last_valid=last_valid, block_height=height,
    )
    if refused:
        return {
            "status": "refused",
            "reason": refused,
            "send": False,
            "resend": False,
            "subprocess": seen,
            "cli_transport": "argv_only",
            "stdin_transaction": False,
            "signature": None,
            "post_claim_toctou": False,
        }
    try:
        code, text = runner(send_argv(binary, base64.b64encode(raw).decode("ascii")), env, timeout)
    except (subprocess.TimeoutExpired, OSError):
        return {"status": "submit_unknown", "reason": "timeout", "send": False, "resend": False, "subprocess": seen + ["send"]}
    seen.append("send")
    signature = _signature(text) if code == 0 else None
    if signature is None:
        return {"status": "submit_unknown", "reason": "ambiguous", "send": False, "resend": False, "subprocess": seen}
    _record_signature(intent_id, signature, digest)
    return {
        "status": "submitted",
        "reason": None,
        "send": True,
        "signature": signature,
        "resend": False,
        "subprocess": seen,
        "cli_transport": "argv_only",
        "stdin_transaction": False,
        "post_claim_toctou": True,
    }


def status() -> dict:
    fee = spend_policy.fee_view()
    aggregate = spend_policy.aggregate_view()
    return {
        "wired": False,
        "signing": "cli_solana_send_behind_gates",
        "send": "disabled",
        "complete": False,
        "automatic_invocation": False,
        "live_ready": False,
        "executable": False,
        "fee_policy": "approved" if fee["approved"] else "not_approved",
        "aggregate_loss_policy": aggregate["approved"],
    }


def activation_status() -> dict:
    """Machine-checkable gate for enabling the five agents and LIVE later.

    Ready is false while any criterion fails. This function changes nothing.
    """
    consent = _consent()
    recorded = _read(_CONSENT)
    policy = exit_policy.view()
    fee = spend_policy.fee_view()
    aggregate = spend_policy.aggregate_view()
    route = solana_direct_swap.route_guard()
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
        "kill_switch_open": phantom_runner.halted() is False,
        "fee_policy_approved": fee["approved"] is True,
        "aggregate_loss_policy_approved": aggregate["approved"] is True,
        "usd_source_approved": spend_policy.usd_source()["approved"] is True,
        "swap_path_verified": False,
        "snapshot_is_not_a_live_path": route["diagnostic"] is True and route["swap_path_verified"] is False,
        "signer_enabled": False,
        "unsigned_simulation_is_not_activation": True,
    }
    return {
        "ready": all(checks.values()),
        "agents": list(db.AGENT_NAMES),
        "pipeline_gaps": {
            "scanner": "not wired to the direct reserve",
            "auditor": "not wired to the direct reserve",
            "analyst": "not wired to the direct reserve",
            "executor": "PAPER on_qualified_signal only",
            "risk": "PAPER on_scheduled_exit only",
        },
        "checks": checks,
        "unmet": [name for name, passed in checks.items() if not passed],
        "swap_blockers": list(SWAP_BLOCKERS),
        "fee_policy": "approved" if fee["approved"] else "not_approved",
        "aggregate_loss_policy": aggregate["approved"],
        "route_blockers": list(route["blockers"]),
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


def prepare_readonly(slot: int | None = None, rpc=None) -> dict:
    """Read, quote, and hash. This does not simulate and does not return transaction bytes."""
    prepared, _entry, _exit, reader = _assemble_read(slot, rpc)
    del _entry, _exit
    return _public_read(prepared, reader)


def reserve_from_reader(intent_id: str, rpc, keep_tx: bool = False) -> dict:
    """Simulate before any hold. A clean simulation is not a USD approval."""
    import solana_read_rpc
    refused = {"ok": False, "sign": False, "send": False, "resubmit": False, "live_ready": False, "broadcastable": False, "runtime_ready": False, "created": False}
    if db.get_setting("mode", "PAPER") != "PAPER":
        return refused | {"reason": "live_locked", "rpc_methods": []}
    if phantom_runner.halted():
        return refused | {"reason": "kill_switch", "rpc_methods": []}
    if not spend_policy.fee_view()["approved"] or not spend_policy.aggregate_view()["approved"]:
        return refused | {"reason": "policy_unrecorded", "rpc_methods": []}
    stamp = spend_policy.policy_stamp()
    prepared, entry_tx, exit_tx, reader = _assemble_read(None, rpc)
    try:
        methods = list(getattr(reader, "methods", []))
        if not prepared.get("technical_ok"):
            return refused | {
                "reason": prepared.get("reason") or "cap",
                "fee_cap_ok": False,
                "usd_cap_ok": False,
                "rpc_methods": methods,
            }
        if not entry_tx:
            return refused | {"reason": "quote", "rpc_methods": methods}
        try:
            simulation = solana_read_rpc.simulate(reader, entry_tx, prepared["context_slot"])
        except solana_read_rpc.RpcError as exc:
            return refused | {"reason": "simulation_ambiguous", "simulation_err": exc.reason, "rpc_methods": list(reader.methods)}
        methods = list(reader.methods)
        if simulation.get("simulated") is not True or simulation.get("err") is not None:
            err = simulation.get("err")
            reason = "simulation_ambiguous" if isinstance(err, str) and err in {"timeout", "transport", "rpc_result", "call_budget"} else "simulation_failed"
            return refused | {"reason": reason, "simulation": simulation, "rpc_methods": methods}
        saved = reserve_direct(intent_id, prepared=prepared, stamp=stamp, simulation_clean=True)
        saved["simulation"] = simulation
        saved["rpc_methods"] = methods
        saved["broadcastable"] = False
        saved["runtime_ready"] = False
        saved["sign"] = False
        saved["live_ready"] = False
        if keep_tx and saved.get("created") and isinstance(entry_tx, str):
            saved["_server_tx"] = entry_tx
        return saved
    finally:
        del entry_tx, exit_tx


def _assemble_read(slot, rpc):
    import solana_read_rpc
    chosen = rpc if rpc is not None else solana_read_rpc.configured_reader()
    if chosen is None:
        return _not_ready(slot, "reads_disabled"), None, None, None
    bundle = solana_read_rpc.load_bundle(chosen, slot)
    if not bundle.get("ok"):
        prepared = _not_ready(bundle.get("context_slot", slot), bundle.get("reason") or "read_failed")
        prepared["stale"] = bundle.get("reason") == "slot_mismatch" or bundle.get("stale") is True
        prepared["fetched"] = bundle.get("fetched") is True
        return prepared, None, None, chosen
    prepared = solana_direct_swap.prepare_from_state(bundle)
    entry_tx = prepared.pop("_entry_tx", None)
    exit_tx = prepared.pop("_exit_tx", None)
    if entry_tx and prepared.get("message_sha256"):
        tail = solana_read_rpc.finish_read(
            chosen, solana_tx_message(entry_tx), prepared["blockhash"], prepared["context_slot"],
        )
        prepared = solana_direct_swap.apply_read_tail(prepared, tail)
    return prepared, entry_tx, exit_tx, chosen


def _public_read(prepared: dict, reader) -> dict:
    prepared["send"] = False
    prepared["cli"] = False
    prepared["broadcastable"] = False
    prepared["runtime_ready"] = False
    prepared["sign"] = False
    prepared["simulation"] = {"simulated": False, "sign": False, "broadcastable": False}
    prepared["rpc_methods"] = list(getattr(reader, "methods", []))
    prepared["fee_policy"] = "approved" if spend_policy.fee_view()["approved"] else "not_approved"
    prepared["aggregate_loss_policy"] = spend_policy.aggregate_view()["approved"]
    prepared["strategies_enabled"] = []
    prepared.pop("_entry_tx", None)
    prepared.pop("_exit_tx", None)
    return prepared


def _not_ready(slot, reason: str) -> dict:
    return {
        "ok": False,
        "stale": True,
        "fetched": False,
        "sign": False,
        "cli": False,
        "diagnostic": True,
        "broadcastable": False,
        "runtime_ready": False,
        "placeholder": False,
        "unsigned_tx_sha256": None,
        "context_slot": slot,
        "reason": reason,
        "strategies_enabled": [],
    }


def solana_tx_message(tx_b64: str) -> bytes:
    import solana_tx
    return solana_tx.message_bytes(tx_b64)


def submit(intent_id: str, tx_b64: str, *, runner=None, rpc=None, direct_state=None, blockhash_source=None) -> dict:
    """Start the CLI bridge only when decide() has no unmet check. It never does."""
    if not isinstance(intent_id, str) or not isinstance(tx_b64, str):
        raise TypeError("string inputs required")
    decision = decide(intent_id, tx_b64, rpc, direct_state=direct_state, blockhash_source=blockhash_source)
    decision["signature"] = None
    decision["signer_called"] = False
    decision["send"] = False
    decision["executable"] = False
    decision["live_ready"] = False
    decision["broadcastable"] = False
    decision["runtime_ready"] = False
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


def decide(intent_id: str, tx_b64: str, rpc=None, *, direct_state=None, blockhash_source=None) -> dict:
    checks = {
        "intent_id": isinstance(intent_id, str) and bool(intent_id),
        "mode_paper_until_activation": db.get_setting("mode", "PAPER") == "PAPER",
        "kill_switch_open": phantom_runner.halted() is False,
        "exit_tuple_effective": exit_policy.view()["effective"] is True,
        "live_request_recorded": _consent() is not None,
        "fee_policy_approved": spend_policy.fee_view()["approved"] is True,
        "aggregate_loss_policy_approved": spend_policy.aggregate_view()["approved"] is True,
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
        "caller_state_refused": direct_state is None and blockhash_source is None,
        "usd_source_approved": spend_policy.usd_source()["approved"] is True,
        "reserved_message": False,
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
    stored_message = None
    if reservation is not None and reservation["result_json"]:
        try:
            stored_body = json.loads(reservation["result_json"])
            stored_message = stored_body.get("message_sha256") if isinstance(stored_body, dict) else None
        except json.JSONDecodeError:
            stored_message = None
    if raw:
        import solana_tx
        try:
            message_digest = hashlib.sha256(solana_tx.message_bytes(base64.b64encode(raw).decode("ascii"))).hexdigest()
        except ValueError:
            message_digest = ""
        checks["reserved_message"] = bool(stored_message) and stored_message == message_digest
    if review and review.get("sha256") == digest and review.get("wallet") == phantom_transport.REQUIRED_WALLET and review.get("network") == "solana:mainnet" and checks["reserved_message"]:
        checks["reviewed_binding"] = True
    inspected = None
    recognized = _direct_route(raw)
    if recognized and (direct_state is not None or blockhash_source is not None):
        inspected = {"error_category": "caller_state"}
    elif recognized:
        inspected = {"error_category": "trusted_read_required"}
    elif rpc is not None and raw:
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
        "fee_policy": "approved" if spend_policy.fee_view()["approved"] else "not_approved",
        "aggregate_loss_policy": spend_policy.aggregate_view()["approved"],
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


def reserve_direct(intent_id: str, fetched: dict | None = None, prepared: dict | None = None, stamp: dict | None = None, simulation_clean: bool = False) -> dict:
    """Hold spend, fees, and rent inside one transaction. This does not sign."""
    if not isinstance(intent_id, str) or not intent_id or len(intent_id) > 80:
        raise ValueError("intent")
    refused = {"ok": False, "sign": False, "send": False, "resubmit": False, "live_ready": False, "broadcastable": False, "created": False}
    if stamp is None:
        return refused | {"reason": "stamp_missing"}
    if db.get_setting("mode", "PAPER") != "PAPER":
        return refused | {"reason": "live_locked"}
    if phantom_runner.halted():
        return refused | {"reason": "kill_switch"}
    if not spend_policy.fee_view()["approved"] or not spend_policy.aggregate_view()["approved"]:
        return refused | {"reason": "policy_unrecorded"}
    if not simulation_clean:
        return refused | {"reason": "simulation_failed"}
    if prepared is None:
        prepared = solana_direct_swap.prepare_from_state(fetched or {})
    prepared.pop("_entry_tx", None)
    prepared.pop("_exit_tx", None)
    if not prepared.get("technical_ok"):
        return refused | {"reason": prepared.get("reason") or "cap", "fee_cap_ok": False, "exposure_cap_ok": False}
    if prepared.get("fee_cap_ok") is not True or prepared.get("exposure_cap_ok") is not True or prepared.get("usd_cap_ok") is not True:
        return refused | {"reason": prepared.get("reason") or "cap", "fee_cap_ok": prepared.get("fee_cap_ok") is True, "exposure_cap_ok": prepared.get("exposure_cap_ok") is True}
    from decimal import Decimal, ROUND_CEILING
    reserved = (Decimal(prepared["exposure_micro_usdc"]) / Decimal(1_000_000)).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    fee_hold = (Decimal(prepared["fee_micro_usdc"]) / Decimal(1_000_000)).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    if reserved <= 0 or reserved > phantom_runner.BANKROLL or fee_hold > spend_policy.MAX_FEE_USD:
        return refused | {"reason": "cap"}
    payload = {
        "origin": "direct_whirlpool",
        "chain_submit": False,
        "provider": "direct_state",
        "context_slot": prepared["context_slot"],
        "network": NETWORK,
        "wallet": phantom_transport.REQUIRED_WALLET,
        "entry_token_est_out": prepared["quote_token_est_out"],
        "entry_token_min_out": prepared["quote_token_min_out"],
        "wallet_usdc_raw": prepared["wallet_usdc_raw"],
        "acquired_raw": None,
        "exit_raw": None,
        "fee_micro_usdc": prepared["fee_micro_usdc"],
        "marginal_micro_usdc": prepared.get("marginal_micro_usdc"),
        "pool_micro_per_sol": prepared.get("micro_usdc_per_sol"),
        "network_fee_micro_usdc": prepared.get("network_fee_micro_usdc"),
        "exposure_micro_usdc": prepared["exposure_micro_usdc"],
        "dex_fee_lamports": prepared.get("dex_fee_lamports"),
        "network_fee_lamports": prepared.get("network_fee_lamports"),
        "message_sha256": prepared.get("message_sha256"),
        "entry_lamports": prepared.get("entry_lamports"),
        "exit_tx_sha256": prepared.get("exit_tx_sha256"),
        "exit_plan_raw": prepared.get("exit_plan_raw"),
        "blockhash": prepared.get("blockhash"),
        "last_valid_block_height": prepared.get("last_valid_block_height"),
        "policy_stamp": stamp if isinstance(stamp, dict) else None,
        "rent_micro_usdc": prepared.get("rent_micro_usdc"),
        "unsigned_tx_sha256": prepared.get("unsigned_tx_sha256"),
        "confirmed": False,
        "live_ready": False,
        "sign": False,
        "resubmit": False,
    }

    def work(conn):
        if _halted_conn(conn):
            return refused | {"reason": "kill_switch"}
        changed = stamp is not None and _stamp_changed(conn, stamp)
        if changed:
            return refused | {"reason": "policy_changed"}
        source = spend_policy.usd_source()
        import usdc_valuation
        limits = usdc_valuation.within_limits(
            int(prepared.get("fee_micro_usdc") or 0),
            int(prepared.get("marginal_micro_usdc") or 0),
            int(prepared.get("exposure_micro_usdc") or 0),
            source,
        )
        if limits.get("ok") is not True:
            return refused | {"reason": limits.get("reason") or "cap", "fee_cap_ok": limits.get("fee_ok") is True, "exposure_cap_ok": False}
        row = conn.execute("SELECT * FROM phantom_reservations WHERE intent_id = ?", (intent_id,)).fetchone()
        if row is not None:
            return {
                "ok": row["status"] == "sim_open",
                "created": False,
                "reason": "existing",
                "status": row["status"],
                "reserved_usd": row["reserved_usd"],
                "resubmit": False,
                "sign": False,
                "send": False,
                "live_ready": False,
            }
        loss = phantom_runner.cumulative_realized_loss(conn)
        if loss >= spend_policy.MAX_CUMULATIVE_LOSS_USD:
            _insert_direct(conn, intent_id, "rejected", "0.00", fee_hold, prepared, payload | {"reason": "stop_new_entries"})
            return refused | {"reason": "stop_new_entries", "status": "rejected", "loss_usd": format(loss, "f")}
        held = Decimal("0")
        open_rows = conn.execute(
            "SELECT reserved_usd FROM phantom_reservations WHERE status IN ('reserved', 'quoted', 'sim_open', 'submit_unknown', 'confirmed', 'exit_planned', 'exit_unknown')"
        ).fetchall()
        for open_row in open_rows:
            held += Decimal(open_row["reserved_usd"] or "0")
        if held + reserved > phantom_runner.BANKROLL:
            _insert_direct(conn, intent_id, "rejected", "0.00", fee_hold, prepared, payload | {"reason": "allocation"})
            return refused | {"reason": "allocation", "status": "rejected"}
        _insert_direct(conn, intent_id, "sim_open", format(reserved, "f"), fee_hold, prepared, payload)
        return {
            "ok": True,
            "created": True,
            "status": "sim_open",
            "reason": "reserved",
            "reserved_usd": format(reserved, "f"),
            "fee_usd": format(fee_hold, "f"),
            "fee_micro_usdc": prepared["fee_micro_usdc"],
            "exposure_micro_usdc": prepared["exposure_micro_usdc"],
            "marginal_micro_usdc": prepared["marginal_micro_usdc"],
            "entry_lamports": prepared["entry_lamports"],
            "quote_token_est_out": prepared["quote_token_est_out"],
            "min_out": prepared.get("min_out"),
            "wallet_usdc_raw": prepared["wallet_usdc_raw"],
            "exit_plan_raw": prepared.get("exit_plan_raw"),
            "exit_quote_out": prepared.get("exit_quote_out"),
            "exit_closes_usdc": prepared.get("exit_closes_usdc"),
            "exit_close_raw": None,
            "exit_tx_sha256": prepared.get("exit_tx_sha256"),
            "network_fee_lamports": prepared.get("network_fee_lamports"),
            "dex_fee_lamports": prepared.get("dex_fee_lamports"),
            "unsigned_tx_sha256": prepared.get("unsigned_tx_sha256"),
            "message_sha256": prepared.get("message_sha256"),
            "context_slot": prepared["context_slot"],
            "blockhash": prepared.get("blockhash"),
            "block_height": prepared.get("block_height"),
            "last_valid_block_height": prepared.get("last_valid_block_height"),
            "placeholder": False,
            "slot_is_ttl": False,
            "fee_cap_ok": prepared.get("fee_cap_ok"),
            "exposure_cap_ok": prepared.get("exposure_cap_ok"),
            "sign": False,
            "send": False,
            "resubmit": False,
            "live_ready": False,
            "broadcastable": False,
        }

    return db.exclusive(work)


def _write_confirmed(intent_id: str, stored: dict) -> None:
    db.execute(
        "UPDATE phantom_reservations SET result_json = ?, updated_at = ? WHERE intent_id = ? AND status = 'confirmed'",
        (json.dumps(stored, separators=(",", ":"), sort_keys=True), time.time(), intent_id),
    )


def _identity_matches(runner) -> bool:
    if runner is None:
        return False
    try:
        code, text = runner(addresses_argv("phantom"), phantom_auth._child_env(phantom_auth._home(), None), 30)
    except (subprocess.TimeoutExpired, OSError, TypeError, ValueError):
        return False
    identity = _identity(text)
    return code == 0 and identity.get("ok") is True


def _combined_fee_ok(dex_micro: int, network_lamports, pool_micro: int, quote: dict) -> bool:
    """DEX micro-USDC plus the chain fee, once. This is the existing $0.05 cap."""
    import usdc_valuation
    if not isinstance(quote, dict) or quote.get("usable") is not True:
        return False
    if isinstance(dex_micro, bool) or not isinstance(dex_micro, int) or dex_micro < 0:
        return False
    net = usdc_valuation._ceil_lamports(network_lamports, pool_micro)
    if net is None:
        return False
    priced = usdc_valuation.usd_ceiling(dex_micro + net, quote)
    return priced is not None and priced <= spend_policy.MAX_FEE_USD


def assess_direct_exit(stored: dict, mark_micro: int, now: float) -> dict:
    """6% stop, arm at 20% favorable, 12% giveback, 3h unless still up at least 5%.

    The mark is micro-USDC per SOL. This unwind sold SOL, so a higher mark is adverse.
    The $20 new-entry stop is not consulted.
    """
    rules = exit_policy.view()
    if rules.get("effective") is not True or not isinstance(rules.get("rules"), dict):
        return {"action": "unavailable", "reason": "exit_rules", "loss_stop_blocks_exit": False}
    entry = stored.get("entry_price_micro")
    opened = stored.get("opened_at")
    if isinstance(mark_micro, bool) or not isinstance(mark_micro, int) or mark_micro <= 0:
        return {"action": "unavailable", "reason": "mark_missing", "loss_stop_blocks_exit": False}
    if isinstance(entry, bool) or not isinstance(entry, int) or entry <= 0:
        return {"action": "unavailable", "reason": "mark_missing", "loss_stop_blocks_exit": False}
    peak = stored.get("peak_price_micro")
    if isinstance(peak, bool) or not isinstance(peak, int) or peak <= 0:
        peak = entry
    if mark_micro < peak:
        peak = mark_micro
    armed = stored.get("trail_armed") is True or (entry - peak) * 100 >= entry * 20
    action = "hold"
    if mark_micro * 100 >= entry * 106:
        action = "stop"
    elif armed and mark_micro * 100 >= peak * 112:
        action = "trail"
    elif isinstance(opened, (int, float)) and not isinstance(opened, bool) and float(now) >= float(opened) + 3 * 3600:
        if (entry - mark_micro) * 100 < entry * 5:
            action = "timeout"
    return {
        "action": action,
        "reason": None if action == "hold" else action,
        "peak_price_micro": peak,
        "trail_armed": armed,
        "loss_stop_blocks_exit": False,
    }


def plan_acquired_exit(intent_id: str, rpc, mark_micro: int | None = None, identity=None, now: float | None = None) -> dict:
    """Fresh read, then an exit for the confirmed delta only. This does not sign.

    The loss stop does not block this. A hold from the exit rules does.
    """
    import solana_read_rpc
    import solana_tx
    import usdc_valuation
    refused = {"ok": False, "sign": False, "send": False, "executable": False}
    row = _reservation(intent_id)
    if row is None or row["status"] != "confirmed":
        return refused | {"reason": "unconfirmed"}
    try:
        stored = json.loads(row["result_json"] or "{}")
    except json.JSONDecodeError:
        stored = {}
    if not isinstance(stored, dict):
        return refused | {"reason": "unconfirmed"}
    signal = assess_direct_exit(stored, mark_micro if mark_micro is not None else 0, time.time() if now is None else now)
    stored["peak_price_micro"] = signal.get("peak_price_micro", stored.get("peak_price_micro"))
    stored["trail_armed"] = signal.get("trail_armed") is True
    if signal["action"] not in {"stop", "trail", "timeout"}:
        _write_confirmed(intent_id, stored)
        return refused | {"reason": signal.get("reason") or signal["action"], "action": signal["action"], "loss_stop_blocks_exit": False, "reserved_usd": row["reserved_usd"]}
    if not _identity_matches(identity):
        _write_confirmed(intent_id, stored)
        return refused | {"reason": "identity_unproven", "loss_stop_blocks_exit": False, "reserved_usd": row["reserved_usd"]}
    source = spend_policy.usd_source()
    if source.get("approved") is not True:
        return refused | {"reason": source.get("reason") or "usd_source_unapproved", "loss_stop_blocks_exit": False}
    bundle = solana_read_rpc.load_bundle(rpc)
    if not bundle.get("ok"):
        return refused | {"reason": bundle.get("reason") or "read_failed", "loss_stop_blocks_exit": False}
    acquired = stored.get("acquired_raw")
    choice = solana_direct_swap.exit_amount(
        acquired, stored.get("wallet_usdc_raw") if isinstance(stored.get("wallet_usdc_raw"), int) else -1,
        confirmed=True, minimum=stored.get("entry_token_min_out"), estimated=stored.get("entry_token_est_out"),
        ata_balance=stored.get("ata_balance"),
    )
    if choice["allowed"] is not True:
        return refused | {"reason": choice["reason"], "loss_stop_blocks_exit": False}
    try:
        tx, exit_quote = solana_direct_swap.build_exit(
            bundle["pool"], bundle["pool_data"], bundle["accounts"], solana_direct_swap.SOL_USDC_SNAPSHOT["specified_slippage_bps"],
            bundle["blockhash"], bundle["context_slot"], acquired, stored.get("wallet_usdc_raw"),
            minimum=stored.get("entry_token_min_out"), estimated=stored.get("entry_token_est_out"),
            ata_balance=stored.get("ata_balance"),
        )
    except (ValueError, solana_direct_swap.solana_whirlpool_quote.QuoteError):
        return refused | {"reason": "exit_quote", "loss_stop_blocks_exit": False}
    accounts = dict(bundle.get("accounts") or {})
    if bundle.get("wsol_exists") is not True:
        wsol = solana_tx.associated_token_address(phantom_transport.REQUIRED_WALLET, solana_tx.NATIVE_MINT, solana_tx.TOKEN_PROGRAM)
        accounts.pop(wsol, None)
    grammar = solana_direct_swap.prove_exit_tx(tx, acquired, exit_quote["token_min_out"], accounts)
    if grammar:
        return refused | {"reason": grammar, "loss_stop_blocks_exit": False}
    tail = solana_read_rpc.finish_read(rpc, solana_tx.message_bytes(tx), bundle["blockhash"], bundle["context_slot"])
    if not tail.get("ok"):
        return refused | {"reason": tail.get("reason") or "blockhash", "loss_stop_blocks_exit": False}
    exit_micro = solana_direct_swap.marginal_micro_usdc_per_sol(bundle["sqrt_price"])
    dex_micro = int(exit_quote.get("trade_fee") or 0)
    if not _combined_fee_ok(dex_micro, tail.get("network_fee_lamports"), exit_micro, source):
        return refused | {"reason": "fee_cap", "loss_stop_blocks_exit": False, "executable": False, "reserved_usd": row["reserved_usd"]}
    simulation = solana_read_rpc.simulate(rpc, tx, bundle["context_slot"])
    simulated = simulation.get("simulated") is True and simulation.get("err") is None
    stored["exit_tx_sha256"] = hashlib.sha256(base64.b64decode(tx)).hexdigest()
    stored["exit_message_sha256"] = hashlib.sha256(solana_tx.message_bytes(tx)).hexdigest()
    stored["exit_token_min_out"] = exit_quote["token_min_out"]
    stored["exit_dex_micro"] = dex_micro
    stored["exit_pool_micro_per_sol"] = exit_micro
    stored["exit_network_fee_estimate"] = tail.get("network_fee_lamports")
    stored["exit_blockhash"] = bundle.get("blockhash")
    stored["exit_last_valid_block_height"] = bundle.get("last_valid_block_height")
    stored["exit_valuation"] = usdc_valuation.snapshot(source)
    stored["exit_simulated"] = simulated
    stored["exit_raw"] = acquired if simulated else None
    stored["exit_action"] = signal["action"]
    stored["realized_pnl_booked"] = False
    stored["live_ready"] = False
    _write_confirmed(intent_id, stored)
    digest = stored["exit_tx_sha256"]
    del tx
    return {
        "ok": simulated,
        "reason": None if simulated else "simulation_failed",
        "action": signal["action"],
        "exit_raw": acquired if simulated else None,
        "exit_close_raw": acquired if simulated else None,
        "exit_tx_sha256": digest,
        "sign": False,
        "send": False,
        "executable": simulated,
        "realized_pnl": None,
        "reserved_usd": row["reserved_usd"],
        "loss_stop_blocks_exit": False,
        "mounted": False,
    }


def book_exit_close(intent_id: str, signature: str, block_height: int) -> dict:
    """Book one net result after a receipt proves the acquired USDC was sold.

    An unresolved submit stays a hold. The fee is inside this result once.
    """
    import solana_read_rpc
    import usdc_valuation
    refused = {"ok": False, "sign": False, "send": False, "booked": False, "realized_pnl": None}
    if not isinstance(intent_id, str) or not intent_id or not isinstance(signature, str) or not signature:
        return refused | {"reason": "intent"}
    if isinstance(block_height, bool) or not isinstance(block_height, int) or block_height <= 0:
        return refused | {"reason": "block_height"}
    reader = solana_read_rpc.configured_reader()
    if reader is None:
        return refused | {"reason": "status_unavailable"}
    proof = solana_read_rpc.read_receipt(reader, signature)
    if not isinstance(proof, dict) or proof.get("err") is not None:
        return refused | {"reason": "status_rejected"}

    def work(conn):
        row = conn.execute("SELECT * FROM phantom_reservations WHERE intent_id = ?", (intent_id,)).fetchone()
        if row is None or row["status"] not in {"confirmed", "exit_unknown"}:
            return refused | {"reason": "unconfirmed", "reserved_usd": None if row is None else row["reserved_usd"]}
        try:
            stored = json.loads(row["result_json"] or "{}")
        except json.JSONDecodeError:
            stored = {}
        if not isinstance(stored, dict) or stored.get("exit_simulated") is not True:
            return refused | {"reason": "exit_unproven", "reserved_usd": row["reserved_usd"]}
        acquired = stored.get("acquired_raw")
        if not isinstance(acquired, int) or isinstance(acquired, bool) or acquired <= 0 or stored.get("exit_raw") != acquired:
            return refused | {"reason": "exit_unproven", "reserved_usd": row["reserved_usd"]}
        if proof.get("message_sha256") != stored.get("exit_message_sha256") or proof.get("signature") != signature:
            return refused | {"reason": "message_mismatch", "reserved_usd": row["reserved_usd"]}
        if proof.get("network") != "solana:mainnet" or proof.get("wallet") != row["wallet"]:
            return refused | {"reason": "receipt_mismatch", "reserved_usd": row["reserved_usd"]}
        if proof.get("confirmation_status") != "finalized":
            return refused | {"reason": "status_rejected", "reserved_usd": row["reserved_usd"]}
        sold = proof.get("acquired_raw")
        if sold is None:
            return refused | {"reason": "pre_missing", "reserved_usd": row["reserved_usd"]}
        if sold != -acquired:
            return refused | {"reason": "delta_mismatch", "reserved_usd": row["reserved_usd"]}
        observed = proof.get("exit_swap")
        minimum = stored.get("exit_token_min_out")
        if not isinstance(observed, dict) or observed.get("a_to_b") is not False or observed.get("amount_specified_is_input") is not True or observed.get("closes_wsol") is not True:
            return refused | {"reason": "exit_semantics", "reserved_usd": row["reserved_usd"]}
        if observed.get("amount") != acquired or observed.get("min_out") != minimum:
            return refused | {"reason": "exit_semantics", "reserved_usd": row["reserved_usd"]}
        economics = solana_direct_swap.exit_economics(
            proof.get("account_keys"), proof.get("native_pre"), proof.get("native_post"), proof.get("network_fee_lamports"),
            exit_min=minimum if isinstance(minimum, int) and not isinstance(minimum, bool) else None,
        )
        if economics.get("ok") is not True:
            return refused | {"reason": economics.get("reason") or "proceeds_missing", "reserved_usd": row["reserved_usd"]}
        entry_quote = stored.get("entry_valuation")
        exit_quote = stored.get("exit_valuation")
        if not isinstance(entry_quote, dict) or not isinstance(exit_quote, dict):
            return refused | {"reason": "valuation_missing", "reserved_usd": row["reserved_usd"]}
        if not _combined_fee_ok(int(stored.get("exit_dex_micro") or 0), economics["network_fee"], int(stored.get("exit_pool_micro_per_sol") or 0), exit_quote):
            return refused | {"reason": "fee_cap", "reserved_usd": row["reserved_usd"], "executable": False}
        result = usdc_valuation.round_trip_pnl(
            int(stored.get("actual_sol_out_lamports") or 0),
            int(stored.get("actual_network_fee_lamports") or 0),
            economics["sol_proceeds"],
            economics["network_fee"],
            int(stored.get("pool_micro_per_sol") or 0),
            int(stored.get("exit_pool_micro_per_sol") or 0),
            entry_quote,
            exit_quote,
        )
        if result.get("ok") is not True:
            return refused | {"reason": result.get("reason") or "valuation_missing", "reserved_usd": row["reserved_usd"]}
        signed = result["realized_pnl"]
        stored["realized_pnl_booked"] = True
        stored["exit_close_raw"] = acquired
        stored["actual_sol_proceeds_lamports"] = economics["sol_proceeds"]
        stored["actual_exit_network_fee_lamports"] = economics["network_fee"]
        stored["rent_net_lamports"] = 0
        now = time.time()
        conn.execute(
            "UPDATE phantom_reservations SET status = 'sim_closed', reserved_usd = '0.00', realized_pnl = ?, result_json = ?, updated_at = ? WHERE intent_id = ? AND status IN ('confirmed', 'exit_unknown')",
            (signed, json.dumps(stored, separators=(",", ":"), sort_keys=True), now, intent_id),
        )
        return {
            "ok": True, "booked": True, "reason": None, "sign": False, "send": False,
            "realized_pnl": signed, "reserved_usd": "0.00", "loss_stop_guarantees_no_overshoot": False,
        }

    return db.exclusive(work)


def direct_status(intent_id: str | None = None) -> dict:
    """Read slot, network, and wallet from the row. This does not fetch or sign."""
    body = {
        "mode": db.get_setting("mode", "PAPER"),
        "agents_enabled": db.get_setting("agents_enabled", "0") == "1",
        "kill_switch": phantom_runner.halted(),
        "network": NETWORK,
        "wallet": phantom_transport.REQUIRED_WALLET,
        "live_ready": False,
        "sign": False,
        "send": False,
        "broadcastable": False,
        "strategies_enabled": [],
        "diagnostic_market": "SOL->USDC",
        "fee_policy_approved": spend_policy.fee_view()["approved"],
        "aggregate_loss_policy_approved": spend_policy.aggregate_view()["approved"],
        "exit": exit_policy.view(),
    }
    if not intent_id:
        body["intent"] = None
        return body
    rows = db.query("SELECT * FROM phantom_reservations WHERE intent_id = ?", (intent_id,))
    if not rows:
        body["intent"] = None
        return body
    row = rows[0]
    stored = {}
    if row["result_json"]:
        try:
            parsed = json.loads(row["result_json"])
            stored = parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            stored = {}
    acquired = stored.get("acquired_raw")
    wallet_raw = stored.get("wallet_usdc_raw")
    simulated = stored.get("exit_simulated") is True and stored.get("exit_raw") == acquired
    body["intent"] = {
        "intent_id": row["intent_id"],
        "status": row["status"],
        "reserved_usd": row["reserved_usd"],
        "context_slot": stored.get("context_slot"),
        "network": stored.get("network") or row["chain"],
        "wallet": row["wallet"],
        "confirmed": stored.get("confirmed") is True and row["status"] in {"confirmed", "exit_planned", "sim_closed"},
        "acquired_raw": acquired if isinstance(acquired, int) else None,
        "exit_close_raw": stored.get("exit_raw") if simulated else None,
        "executable": simulated,
        "wallet_usdc_raw": wallet_raw,
        "exit_uses_wallet_balance": False,
        "realized_pnl": row["realized_pnl"],
        "loss_stop_guarantees_no_overshoot": False,
        "sign": False,
        "resubmit": False,
    }
    return body


def _insert_direct(conn, intent_id: str, status: str, reserved: str, fee_hold, prepared: dict, payload: dict) -> None:
    from decimal import Decimal
    now = time.time()
    micro = int(prepared.get("marginal_micro_usdc") or 0)
    sol_usd = format(Decimal(micro) / Decimal(1_000_000), "f")
    pair = json.dumps({
        "origin": "direct_whirlpool",
        "sell_native": True,
        "buy_mint": phantom_runner.USDC,
        "chain": NETWORK,
    }, sort_keys=True, separators=(",", ":"))
    conn.execute(
        """
        INSERT INTO phantom_reservations (
            intent_id, status, wallet_id, wallet, chain, pair_json, amount_usd, fee_usd,
            reserved_usd, slippage, entry_quote_id, exit_quote_id, fx_sol_usd, fx_at,
            result_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, '8.00', ?, ?, 1, NULL, NULL, ?, ?, ?, ?, ?)
        """,
        (
            intent_id, status, phantom_transport.REQUIRED_WALLET_ID, phantom_transport.REQUIRED_WALLET,
            NETWORK, pair, format(fee_hold, "f"), reserved, sol_usd, now,
            json.dumps(payload, separators=(",", ":"), sort_keys=True), now, now,
        ),
    )


def _direct_route(raw: bytes) -> bool:
    if not raw:
        return False
    import solana_tx
    import solana_whirlpool
    try:
        decoded = solana_tx.decode_transaction(raw)
    except ValueError:
        return False
    return solana_whirlpool.PROGRAM_ID in decoded.static_keys


def _direct_proof(raw: bytes, direct_state, blockhash_source) -> dict:
    """Recompute the swap from account bytes. A slippage flag on the dict is ignored."""
    empty = {
        "byte_checks_passed": False,
        "spend_verified": False,
        "slippage_verified": False,
        "fee_cap_ok": False,
        "exposure_cap_ok": False,
        "wallet_verified": False,
        "blockhash_checked": False,
        "error_category": "direct_state_missing",
    }
    if not isinstance(direct_state, dict):
        return empty
    accounts = direct_state.get("accounts")
    slot = direct_state.get("context_slot")
    if not isinstance(accounts, dict) or isinstance(slot, bool) or not isinstance(slot, int):
        return empty
    proved = solana_direct_swap.prove(
        base64.b64encode(raw).decode("ascii"), accounts, slot, bool(direct_state.get("wsol_exists")),
        network_fee_lamports=direct_state.get("network_fee_lamports"),
        rent_lamports=direct_state.get("rent_lamports"),
    )
    hook = solana_direct_swap.revalidate_blockhash(proved.get("blockhash"), blockhash_source, slot)
    proved["blockhash_checked"] = hook.get("checked") is True
    proved["broadcastable"] = False
    proved["runtime_ready"] = False
    return proved


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
