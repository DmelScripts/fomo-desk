"""Server-owned SOL/USDC diagnostic lifecycle. Not a token-buy strategy.

One tick advances the canonical Whirlpool position: reserve, prove, claim,
entry receipt, risk, exit validation, exit receipt, net close. The process
does not start this loop. Real signing stays off. Caller bodies cannot supply
bytes, prices, or receipt proof.

Scanner, auditor, and analyst do not qualify this pool. The position is a
SOL to USDC round trip used as a diagnostic, not a user-token buy.
"""

from __future__ import annotations

import hashlib
import json
import time

import db
import execution_transport
import phantom_runner
import phantom_transport
import solana_direct_swap
import solana_read_rpc
import solana_tx
import spend_policy
import usdc_valuation

INTENT = "diagnostic-sol-usdc"
_STASH = "diagnostic_tx:"
_OPEN = {"sim_open", "submit_unknown", "confirmed", "exit_unknown"}

# Tests install a fake runner. None means the real CLI is not called.
RUNNER = None
# Tests install a clock. Production uses the process clock.
CLOCK = None

SCOPE = {
    "diagnostic": True,
    "market": "SOL->USDC",
    "scope": "canonical_whirlpool_diagnostic",
    "strategy": None,
    "scanner_qualified": False,
    "auditor_qualified": False,
    "analyst_qualified": False,
    "executor_qualified": False,
    "user_token_buy": False,
    "hedge_is_not_a_token_buy": True,
    "signer_enabled": False,
    "effectful": False,
    "live_ready": False,
    "mounted": True,
    "service_loop_armed": False,
}


def service_tick() -> dict:
    """One server step. This is not scheduled while agents and LIVE are off."""
    row = _row()
    if row is None:
        result = _open()
    elif row["status"] == "sim_open":
        result = _spawn(row)
    elif row["status"] == "submit_unknown":
        result = _confirm_entry(row)
    elif row["status"] == "confirmed":
        result = _risk(row)
    elif row["status"] == "exit_unknown":
        result = _close(row)
    elif row["status"] == "sim_closed":
        result = {"ok": True, "stage": "closed", "resend": False, "send": False}
    else:
        result = {"ok": False, "stage": row["status"], "reason": "held", "resend": False, "send": False}
    return _public(result)


def view() -> dict:
    """Public state. The unsigned transaction is not included."""
    row = _row()
    body = dict(SCOPE)
    body["price"] = _price_view()
    body["runner_configured"] = RUNNER is not None
    body["intent"] = None if row is None else _intent_view(row)
    body["stage"] = "idle" if row is None else row["status"]
    return body


def _open() -> dict:
    blocked = _preconditions()
    if blocked:
        return blocked
    existing = _row()
    if existing is not None:
        return {"ok": True, "stage": existing["status"], "reason": "existing", "send": False, "resend": False}
    reader = solana_read_rpc.configured_reader()
    if reader is None:
        return _stop("reads_disabled")
    try:
        saved = execution_transport.reserve_from_reader(INTENT, reader, keep_tx=True)
    except solana_read_rpc.RpcError as exc:
        return _stop(exc.reason)
    tx = saved.pop("_server_tx", None)
    if saved.get("created") is not True or not isinstance(tx, str):
        return _stop(saved.get("reason") or "reserve", **{key: saved.get(key) for key in ("fee_cap_ok", "exposure_cap_ok")})
    _stash(tx)
    execution_transport.remember_review(INTENT, tx)
    stored = _stored(_row())
    stored.update({
        "policy_stamp": spend_policy.policy_stamp(),
        "last_valid_block_height": saved.get("last_valid_block_height"),
        "diagnostic": True,
        "scope": SCOPE["scope"],
        "strategy": None,
        "user_token_buy": False,
    })
    _write_result(stored)
    return {"ok": True, "stage": "sim_open", "reason": "reserved", "send": False, "resend": False, "reserved_usd": saved.get("reserved_usd")}


def _spawn(row) -> dict:
    blocked = _preconditions()
    if blocked:
        return blocked
    if row["intent_id"] in execution_transport._ledger():
        return _stop("not_claimable", resend=False)
    tx = _peek()
    if not isinstance(tx, str) or not tx:
        return _stop("server_tx_missing", resend=False)
    stored = _stored(row)
    reader = solana_read_rpc.configured_reader()
    if reader is None:
        return _stop("reads_disabled")
    try:
        bundle = solana_read_rpc.load_bundle(reader)
    except solana_read_rpc.RpcError as exc:
        return _stop(exc.reason)
    if not bundle.get("ok"):
        return _stop(bundle.get("reason") or "read_failed")
    accounts = _grammar_accounts(bundle)
    grammar = solana_direct_swap.prove_entry_tx(tx, stored, accounts, wsol_exists=bundle.get("wsol_exists") is True)
    proof = {
        "recomputed": True,
        "grammar": grammar is None,
        "spend_verified": grammar is None,
        "slippage_verified": grammar is None,
        "blockhash_checked": False,
        "signer_enabled": False,
        "effectful": False,
    }
    if grammar:
        return _stop(grammar, sign_proof=proof)
    if _stamp_changed(stored):
        return _stop("policy_changed", sign_proof=proof)
    try:
        tail = solana_read_rpc.finish_read(reader, solana_tx.message_bytes(tx), stored.get("blockhash"), bundle["context_slot"])
    except solana_read_rpc.RpcError as exc:
        return _stop(exc.reason, sign_proof=proof)
    if not tail.get("ok"):
        return _stop(tail.get("reason") or "blockhash", sign_proof=proof)
    height = tail.get("block_height")
    last_valid = stored.get("last_valid_block_height")
    if not isinstance(height, int) or not isinstance(last_valid, int) or height > last_valid:
        return _stop("blockhash_expired", sign_proof=proof)
    proof["blockhash_checked"] = True
    sent = execution_transport.send_after_identity(INTENT, tx, RUNNER, binary="phantom")
    if sent.get("status") != "sim_open":
        _drop_stash()
    sent_public = {
        "ok": sent.get("status") in {"submitted", "submit_unknown"},
        "stage": "submit_unknown" if sent.get("status") in {"submitted", "submit_unknown"} else sent.get("status"),
        "reason": sent.get("reason"),
        "send": sent.get("send") is True,
        "resend": False,
        "subprocess": sent.get("subprocess") or [],
        "signature": sent.get("signature"),
        "sign_proof": proof,
        "cli_transport": sent.get("cli_transport"),
        "stdin_transaction": False,
    }
    if sent.get("status") == "submitted" and sent.get("signature"):
        _merge({"signature": sent["signature"], "resend": False})
    return sent_public


def _confirm_entry(row) -> dict:
    stored = _stored(row)
    signature = stored.get("signature")
    if not isinstance(signature, str) or not signature:
        return _stop("signature_missing", resend=False)
    reader = solana_read_rpc.configured_reader()
    if reader is None:
        return _stop("status_unavailable", resend=False)
    proof = solana_read_rpc.read_receipt(reader, signature)
    if not isinstance(proof, dict) or proof.get("err") is not None:
        return _stop("status_rejected", resend=False)
    height = proof.get("block_height")
    if isinstance(height, bool) or not isinstance(height, int) or height <= 0:
        return _stop("block_height_missing", resend=False)
    confirmed = phantom_runner.apply_chain_proof(INTENT, signature, height)
    return {
        "ok": confirmed.get("confirmed") is True,
        "stage": "confirmed" if confirmed.get("confirmed") is True else "submit_unknown",
        "reason": None if confirmed.get("confirmed") is True else confirmed.get("reason"),
        "send": False,
        "resend": False,
        "acquired_raw": confirmed.get("acquired_raw"),
        "realized_pnl": None,
    }


def _risk(row) -> dict:
    blocked = _preconditions()
    if blocked:
        return blocked
    stored = _stored(row)
    reader = solana_read_rpc.configured_reader()
    if reader is None:
        return _stop("reads_disabled", resend=False)
    try:
        bundle = solana_read_rpc.load_bundle(reader)
    except solana_read_rpc.RpcError as exc:
        return _stop(exc.reason, resend=False)
    if not bundle.get("ok"):
        return _stop(bundle.get("reason") or "read_failed", resend=False)
    mark = solana_direct_swap.marginal_micro_usdc_per_sol(bundle["sqrt_price"])
    signal = execution_transport.assess_direct_exit(stored, mark, _now())
    stored["peak_price_micro"] = signal.get("peak_price_micro")
    stored["trail_armed"] = signal.get("trail_armed") is True
    stored["exit_intent"] = signal.get("action")
    stored["mark_price_micro"] = mark
    _write_result(stored)
    if signal["action"] not in {"stop", "trail", "timeout"}:
        return {
            "ok": True,
            "stage": "confirmed",
            "action": signal["action"],
            "reason": signal.get("reason"),
            "send": False,
            "resend": False,
            "loss_stop_blocks_exit": False,
            "mark_price_micro": mark,
        }
    if stored.get("exit_claimed") is True:
        return _stop("not_claimable", resend=False, action=signal["action"])
    planned = execution_transport.plan_acquired_exit(INTENT, solana_read_rpc.configured_reader(), mark, RUNNER, _now())
    if planned.get("ok") is not True:
        return _stop(planned.get("reason") or "exit", send=False, resend=False, action=signal["action"], loss_stop_blocks_exit=False)
    rebuilt = _rebuild_exit(mark)
    if rebuilt.get("ok") is not True:
        return _stop(rebuilt.get("reason") or "message_unbound", resend=False, action=signal["action"])
    if not _claim_exit():
        return _stop("not_claimable", resend=False)
    sent = _send_held(rebuilt["tx"])
    _drop_stash()
    return {
        "ok": sent.get("ok") is True,
        "stage": "exit_unknown",
        "action": signal["action"],
        "reason": sent.get("reason"),
        "send": sent.get("send") is True,
        "resend": False,
        "loss_stop_blocks_exit": False,
        "signature": sent.get("signature"),
    }


def _close(row) -> dict:
    stored = _stored(row)
    signature = stored.get("exit_signature") or stored.get("signature")
    # The entry signature stays on the row. The exit signature is exit_signature.
    signature = stored.get("exit_signature")
    if not isinstance(signature, str) or not signature:
        return _stop("signature_missing", resend=False, stage="exit_unknown")
    reader = solana_read_rpc.configured_reader()
    if reader is None:
        return _stop("status_unavailable", resend=False)
    proof = solana_read_rpc.read_receipt(reader, signature)
    if not isinstance(proof, dict) or proof.get("err") is not None:
        return _stop("status_rejected", resend=False)
    height = proof.get("block_height")
    if isinstance(height, bool) or not isinstance(height, int) or height <= 0:
        return _stop("block_height_missing", resend=False)
    booked = execution_transport.book_exit_close(INTENT, signature, height)
    return {
        "ok": booked.get("booked") is True,
        "stage": "sim_closed" if booked.get("booked") is True else "exit_unknown",
        "reason": booked.get("reason"),
        "send": False,
        "resend": False,
        "realized_pnl": booked.get("realized_pnl"),
        "reserved_usd": booked.get("reserved_usd"),
    }


def _rebuild_exit(mark: int) -> dict:
    row = _row()
    stored = _stored(row)
    reader = solana_read_rpc.configured_reader()
    if reader is None:
        return {"ok": False, "reason": "reads_disabled"}
    try:
        bundle = solana_read_rpc.load_bundle(reader)
    except solana_read_rpc.RpcError as exc:
        return {"ok": False, "reason": exc.reason}
    if not bundle.get("ok"):
        return {"ok": False, "reason": bundle.get("reason") or "read_failed"}
    try:
        tx, _quote = solana_direct_swap.build_exit(
            bundle["pool"], bundle["pool_data"], bundle["accounts"], solana_direct_swap.SOL_USDC_SNAPSHOT["specified_slippage_bps"],
            bundle["blockhash"], bundle["context_slot"], stored["acquired_raw"], stored.get("wallet_usdc_raw"),
            minimum=stored.get("entry_token_min_out"), estimated=stored.get("entry_token_est_out"),
            ata_balance=stored.get("ata_balance"),
        )
    except (ValueError, solana_direct_swap.solana_whirlpool_quote.QuoteError):
        return {"ok": False, "reason": "exit_quote"}
    digest = hashlib.sha256(solana_tx.message_bytes(tx)).hexdigest()
    if digest != stored.get("exit_message_sha256"):
        return {"ok": False, "reason": "message_unbound"}
    return {"ok": True, "tx": tx, "mark": mark}


def _claim_exit() -> bool:
    def work(conn):
        row = conn.execute("SELECT status, result_json FROM phantom_reservations WHERE intent_id = ?", (INTENT,)).fetchone()
        if row is None or row["status"] != "confirmed":
            return False
        try:
            stored = json.loads(row["result_json"] or "{}")
        except json.JSONDecodeError:
            stored = {}
        if not isinstance(stored, dict) or stored.get("exit_claimed") is True:
            return False
        if _stamp_changed(stored):
            return False
        if phantom_runner.halted():
            return False
        stored["exit_claimed"] = True
        stored["resend"] = False
        updated = conn.execute(
            "UPDATE phantom_reservations SET status = 'exit_unknown', result_json = ?, updated_at = ? WHERE intent_id = ? AND status = 'confirmed'",
            (json.dumps(stored, separators=(",", ":"), sort_keys=True), time.time(), INTENT),
        )
        return updated.rowcount == 1

    return db.exclusive(work) is True


def _send_held(tx: str) -> dict:
    if RUNNER is None:
        return {"ok": False, "reason": "runner_missing", "send": False}
    try:
        code, text = RUNNER(execution_transport.addresses_argv("phantom"), {}, 30)
    except (OSError, TimeoutError):
        return {"ok": False, "reason": "auth_expired", "send": False}
    identity = execution_transport._identity(text)
    if code != 0 or identity.get("ok") is not True:
        return {"ok": False, "reason": "wallet_mismatch" if code == 0 else "auth_expired", "send": False}
    try:
        code, text = RUNNER(execution_transport.send_argv("phantom", tx), {}, 30)
    except (OSError, TimeoutError):
        return {"ok": False, "reason": "timeout", "send": False}
    signature = execution_transport._signature(text) if code == 0 else None
    if signature is None:
        return {"ok": False, "reason": "ambiguous", "send": False}
    _merge({"exit_signature": signature, "resend": False})
    return {"ok": True, "send": True, "signature": signature, "reason": None}


def _preconditions() -> dict | None:
    if db.get_setting("mode", "PAPER") != "PAPER":
        return _stop("live_locked")
    if db.get_setting("agents_enabled", "0") == "1":
        return _stop("agents_on")
    if phantom_runner.halted():
        return _stop("kill_switch")
    if not spend_policy.fee_view()["approved"] or not spend_policy.aggregate_view()["approved"]:
        return _stop("policy_unrecorded")
    _source, reason = _lifecycle_price()
    if reason:
        return _stop(reason)
    if RUNNER is None:
        return _stop("runner_missing")
    return None


def _lifecycle_price() -> tuple[dict | None, str | None]:
    """Harness quotes can run the diagnostic. set_quote and a missing Hermes key cannot."""
    source = usdc_valuation.current()
    if source.get("origin") == "test_harness" and source.get("usable") is True and source.get("authenticated_live") is not True:
        return source, None
    if source.get("origin") == "injected_quote":
        return None, "injected_quote_not_live"
    auth = usdc_valuation.authenticated_read()
    if auth.get("reason") == "credentials_missing" or not usdc_valuation.enabled():
        return None, auth.get("reason") or source.get("reason") or "credentials_missing"
    return None, auth.get("reason") or source.get("reason") or "credentials_missing"


def _price_view() -> dict:
    source = usdc_valuation.current()
    auth = usdc_valuation.authenticated_read() if usdc_valuation.enabled() else {"reason": "usd_source_unapproved", "authenticated_live": False}
    return {
        "origin": source.get("origin"),
        "live_provider": False,
        "authenticated_live": False,
        "usable": source.get("usable") is True and source.get("origin") == "test_harness",
        "authenticated_reason": auth.get("reason"),
        "credential_env": usdc_valuation.CREDENTIAL_ENV,
    }


def _stamp_changed(stored: dict) -> bool:
    stamp = stored.get("policy_stamp")
    if not isinstance(stamp, dict):
        return True
    current = spend_policy.policy_stamp()
    return json.dumps(stamp, sort_keys=True) != json.dumps(current, sort_keys=True)


def _grammar_accounts(bundle: dict) -> dict:
    accounts = dict(bundle.get("accounts") or {})
    if bundle.get("wsol_exists") is not True:
        wsol = solana_tx.associated_token_address(
            phantom_transport.REQUIRED_WALLET, solana_tx.NATIVE_MINT, solana_tx.TOKEN_PROGRAM,
        )
        accounts.pop(wsol, None)
    return accounts


def _now() -> float:
    if CLOCK is None:
        return time.time()
    return float(CLOCK())


def _row():
    rows = db.query("SELECT * FROM phantom_reservations WHERE intent_id = ?", (INTENT,))
    return rows[0] if rows else None


def _stored(row) -> dict:
    if row is None or not row["result_json"]:
        return {}
    try:
        body = json.loads(row["result_json"])
    except json.JSONDecodeError:
        return {}
    return body if isinstance(body, dict) else {}


def _write_result(stored: dict) -> None:
    db.execute(
        "UPDATE phantom_reservations SET result_json = ?, updated_at = ? WHERE intent_id = ?",
        (json.dumps(stored, separators=(",", ":"), sort_keys=True), time.time(), INTENT),
    )


def _merge(extra: dict) -> None:
    row = _row()
    stored = _stored(row)
    stored.update(extra)
    _write_result(stored)


def _stash(tx: str) -> None:
    db.execute(
        """
        INSERT INTO phantom_control (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (_STASH + INTENT, tx, time.time()),
    )


def _peek() -> str | None:
    rows = db.query("SELECT value FROM phantom_control WHERE key = ?", (_STASH + INTENT,))
    if not rows:
        return None
    return rows[0]["value"]


def _drop_stash() -> None:
    db.execute("DELETE FROM phantom_control WHERE key = ?", (_STASH + INTENT,))


def _intent_view(row) -> dict:
    stored = _stored(row)
    return {
        "intent_id": row["intent_id"],
        "status": row["status"],
        "reserved_usd": row["reserved_usd"],
        "realized_pnl": row["realized_pnl"],
        "signature": stored.get("signature"),
        "exit_signature": stored.get("exit_signature"),
        "entry_lamports": stored.get("entry_lamports"),
        "entry_token_min_out": stored.get("entry_token_min_out"),
        "entry_token_est_out": stored.get("entry_token_est_out"),
        "network_fee_lamports": stored.get("network_fee_lamports"),
        "message_sha256": stored.get("message_sha256"),
        "acquired_raw": stored.get("acquired_raw"),
        "wallet_usdc_raw": stored.get("wallet_usdc_raw"),
        "pool_micro_per_sol": stored.get("pool_micro_per_sol"),
        "entry_price_micro": stored.get("entry_price_micro"),
        "opened_at": stored.get("opened_at"),
        "exit_intent": stored.get("exit_intent"),
        "exit_message_sha256": stored.get("exit_message_sha256"),
        "context_slot": stored.get("context_slot"),
        "scope": stored.get("scope") or SCOPE["scope"],
        "strategy": None,
        "user_token_buy": False,
    }


def _stop(reason: str, **extra) -> dict:
    body = {"ok": False, "reason": reason, "send": False, "resend": False, "sign": False, "effectful": False}
    body.update(extra)
    return body


def _public(result: dict) -> dict:
    body = dict(SCOPE)
    body.update(result)
    body.pop("_server_tx", None)
    body["sign"] = False
    body["effectful"] = False
    body["live_ready"] = False
    body["live_provider"] = False
    body["signer_enabled"] = False
    for key in list(body):
        if "transaction" in key or key.endswith("_tx"):
            body.pop(key, None)
    return body
