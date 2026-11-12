"""Live sign and send. Unreachable unless every readiness gate is closed.

The signer is the server Phantom session for the allowlisted wallet. The send
and the status check go through the configured Helius RPC client. A timeout
reconciles the signature already recorded. It does not sign or send again.
"""

from __future__ import annotations

import hashlib
import threading

import db
import memecoin_desk
import owner_slippage
import phantom_transport
import solana_tx

SECRET_MARK = "api-key="
_CONFIRMED = {"confirmed", "finalized"}
_CLAIM = threading.Lock()


def armed() -> bool:
    """True only for LIVE with the agents on and every readiness gate closed."""
    if db.get_setting("mode", "PAPER") != "LIVE":
        return False
    if db.get_setting("agents_enabled", "0") != "1":
        return False
    import desk_loop
    gates = desk_loop.remaining_live_gates()
    if not isinstance(gates, list) or not gates:
        return False
    return all(isinstance(item, dict) and item.get("closed") is True for item in gates)


def submit(case_id: str, message_sha256: str, now: float, transport=None, signer=None) -> dict:
    """Sign, send, and confirm one reserved message. Refuses while any gate is open."""
    return _exchange(case_id, message_sha256, now, transport, signer)


def _exchange(case_id: str, message_sha256: str, now: float, transport, signer) -> dict:
    if not armed():
        return _blank() | {"reason": "live_unreachable"}
    return _sign_send_confirm(case_id, message_sha256, now, transport, signer)


def _sign_send_confirm(case_id: str, message_sha256: str, now: float, transport, signer) -> dict:
    """Limits, the allowlisted fee payer, then one sign and one Helius send."""
    refused = _blank()
    if not armed():
        return refused | {"reason": "live_unreachable"}
    helius = _helius_reason()
    if helius is not None:
        return refused | {"reason": helius}
    if not isinstance(case_id, str) or not isinstance(message_sha256, str) or message_sha256 == "":
        return refused | {"reason": "message_refused"}
    rows = db.query("SELECT * FROM memecoin_reservations WHERE message_sha256 = ?", (message_sha256,))
    if len(rows) != 1 or rows[0]["case_id"] != case_id:
        return refused | {"reason": "message_refused"}
    proof = rows[0]
    try:
        decoded = solana_tx.decode_transaction(__import__("base64").b64decode(proof["message_b64"], validate=True))
    except Exception:
        return refused | {"reason": "message_refused"}
    if decoded.fee_payer != phantom_transport.REQUIRED_WALLET or decoded.required_signatures != 1:
        return refused | {"reason": "wallet_mismatch"}
    route = _route(decoded)
    if route.get("slippage_bps") != owner_slippage.BPS or isinstance(route.get("slippage_bps"), bool):
        return refused | {"reason": "slippage_unproven"}
    import desk_loop
    capital = desk_loop._signer_capital(message_sha256, now, transport)
    if capital.get("ok") is not True:
        return refused | {"reason": capital.get("reason") or "exposure_unknown"}
    if capital.get("ticket_limit_proven") is not True:
        return refused | {"reason": "ticket"}
    if capital.get("allocation_proven") is not True:
        return refused | {"reason": "allocation"}
    simulated = desk_loop.live_simulate(case_id, message_sha256, now)
    if simulated.get("preflight_ready") is not True:
        return refused | {"reason": simulated.get("reason") or "preflight_open", "sign": False, "send": False}
    totals = memecoin_desk.loss_totals()
    if totals.get("live_halts_new_entries") is True:
        return refused | {"reason": "stop_new_entries"}
    if not callable(signer):
        return refused | {"reason": "phantom_session_missing"}
    session_ready = getattr(signer, "ready", None)
    if callable(session_ready) and session_ready() is not True:
        return refused | {"reason": "phantom_session_missing"}
    owned, signature, status = _claim(message_sha256, case_id, now)
    if not owned:
        if not signature:
            return refused | {"reason": "already_in_flight"}
        return _reconcile(case_id, message_sha256, signature, status, now)
    try:
        signed = signer(proof["message_b64"])
    except Exception:
        _release(message_sha256)
        return refused | {"reason": "signature_refused"}
    signed_tx = None if not isinstance(signed, dict) else signed.get("signed_tx")
    if not isinstance(signed_tx, str) or signed_tx == "":
        _release(message_sha256)
        return refused | {"reason": "signature_refused"}
    try:
        reserved = solana_tx.message_bytes(proof["message_b64"])
        produced = solana_tx.message_bytes(signed_tx)
        signature = solana_tx.first_signature(signed_tx)
    except Exception:
        _release(message_sha256)
        return refused | {"reason": "signature_refused"}
    if reserved != produced or signature == "":
        _release(message_sha256)
        return refused | {"reason": "message_mismatch"}
    _store_signature(message_sha256, signature)
    sent = _rpc({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendTransaction",
        "params": [signed_tx, {"encoding": "base64", "skipPreflight": False}],
    })
    if sent.get("ok") is not True:
        if sent.get("reason") in {"rpc_timeout", "rpc_transport"}:
            return _reconcile(case_id, message_sha256, signature, "inflight", now, fallback=sent.get("reason"))
        return refused | {"reason": sent.get("reason") or "send_refused", "sign": True, "signature": signature}
    if sent["body"].get("result") != signature:
        return refused | {"reason": "send_refused", "sign": True, "signature": signature}
    return _reconcile(case_id, message_sha256, signature, "inflight", now)


def _reconcile(case_id: str, message_sha256: str, signature: str, status: str, now: float, fallback: str | None = None) -> dict:
    refused = _blank() | {"sign": True, "signature": signature}
    if status == "confirmed":
        return _confirmed(signature)
    posted = _rpc({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignatureStatuses",
        "params": [[signature], {"searchTransactionHistory": True}],
    })
    if posted.get("ok") is not True:
        return refused | {"reason": posted.get("reason") or fallback or "rpc_transport"}
    value = ((posted["body"].get("result") or {}).get("value") or [None])[0]
    if not isinstance(value, dict) or value.get("err") is not None:
        return refused | {"reason": "confirmation_err"}
    if value.get("confirmationStatus") not in _CONFIRMED:
        return refused | {"reason": "confirmation_pending"}
    evidence = {
        "message_sha256": message_sha256,
        "signature": signature,
        "slot": value.get("slot"),
        "confirmation_status": value.get("confirmationStatus"),
        "err": None,
        "wallet": phantom_transport.REQUIRED_WALLET,
    }
    if _leaks(evidence):
        return refused | {"reason": "receipt_unrecorded"}
    case = memecoin_desk._case(case_id)
    mint = None if case is None else case["mint"]
    try:
        wrote = db.exclusive(lambda conn: _finish(conn, case_id, mint, message_sha256, signature, evidence, now))
    except Exception:
        return refused | {"reason": "receipt_unrecorded"}
    if wrote is False:
        return _confirmed(signature)
    return _confirmed(signature)


def _finish(conn, case_id: str, mint, message_sha256: str, signature: str, evidence: dict, now: float) -> bool:
    cursor = conn.execute(
        "UPDATE live_submit SET status = 'confirmed' WHERE message_sha256 = ? AND signature = ? AND status = 'inflight'",
        (message_sha256, signature),
    )
    if cursor.rowcount != 1:
        return False
    memecoin_desk._log(conn, case_id, mint, "live_submit", "pass", None, "sendTransaction", evidence, float(now))
    return True


def _confirmed(signature: str) -> dict:
    return {
        "ok": True,
        "sign": True,
        "send": True,
        "live_ready": False,
        "execution_eligible": False,
        "network_called": True,
        "broadcast": True,
        "reason": None,
        "signature": signature,
        "reconciled": True,
        "mock": False,
    }


def _claim(message_sha256: str, case_id: str, now: float):
    """One row per reservation. The caller that inserts it is the only signer."""
    with _CLAIM:
        conn = db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            _ensure_table(conn)
            row = conn.execute(
                "SELECT signature, status FROM live_submit WHERE message_sha256 = ?",
                (message_sha256,),
            ).fetchone()
            if row is not None:
                conn.commit()
                return False, row["signature"], row["status"]
            try:
                conn.execute(
                    """
                    INSERT INTO live_submit (message_sha256, signature, case_id, status, created_at)
                    VALUES (?, NULL, ?, 'inflight', ?)
                    """,
                    (message_sha256, case_id, float(now)),
                )
            except Exception:
                lost = conn.execute(
                    "SELECT signature, status FROM live_submit WHERE message_sha256 = ?",
                    (message_sha256,),
                ).fetchone()
                conn.commit()
                if lost is None:
                    raise
                return False, lost["signature"], lost["status"]
            conn.commit()
            return True, None, "inflight"
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _release(message_sha256: str) -> None:
    """Drop a claim that never reached a signature. A later call may sign."""
    try:
        db.execute(
            "DELETE FROM live_submit WHERE message_sha256 = ? AND signature IS NULL AND status = 'inflight'",
            (message_sha256,),
        )
    except Exception:
        return


def _store_signature(message_sha256: str, signature: str) -> None:
    db.execute(
        "UPDATE live_submit SET signature = ? WHERE message_sha256 = ? AND signature IS NULL",
        (signature, message_sha256),
    )


def _ensure_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS live_submit (
            message_sha256 TEXT PRIMARY KEY,
            signature TEXT,
            case_id TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )


def _helius_reason() -> str | None:
    import solana_rpc
    info = solana_rpc.describe()
    if info.get("provider") != "helius" or info.get("ok") is not True:
        return info.get("reason") or "rpc_refused"
    return None


def _rpc(payload: dict) -> dict:
    reason = _helius_reason()
    if reason is not None:
        return {"ok": False, "reason": reason}
    import solana_rpc
    try:
        posted = solana_rpc.post(payload)
    except solana_rpc.RpcRefused as exc:
        return {"ok": False, "reason": exc.reason if exc.reason in {"rpc_timeout", "rpc_transport"} else "rpc_transport"}
    except TimeoutError:
        return {"ok": False, "reason": "rpc_timeout"}
    except Exception:
        return {"ok": False, "reason": "rpc_transport"}
    if not isinstance(posted, dict) or posted.get("status") != 200 or not isinstance(posted.get("body"), dict):
        return {"ok": False, "reason": "send_refused"}
    if posted["body"].get("error") or _leaks(posted["body"]):
        return {"ok": False, "reason": "send_refused"}
    return {"ok": True, "body": posted["body"]}


def _route(decoded) -> dict:
    import jupiter_route_v2
    import solana_jupiter
    for item in decoded.instructions:
        program = decoded.static_keys[item.program_index]
        if program == solana_jupiter.PROGRAM_ID:
            parsed = jupiter_route_v2.parse(bytes(item.data))
            return parsed if isinstance(parsed, dict) else {}
    return {}


def _blank() -> dict:
    return {
        "ok": False,
        "sign": False,
        "send": False,
        "live_ready": False,
        "execution_eligible": False,
        "network_called": False,
        "broadcast": False,
        "reconciled": False,
        "mock": False,
        "signature": None,
    }


def _leaks(value) -> bool:
    if isinstance(value, str):
        return SECRET_MARK in value
    if isinstance(value, dict):
        return any(_leaks(key) or _leaks(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_leaks(item) for item in value)
    return False


def message_digest(message_b64: str) -> str:
    return hashlib.sha256(solana_tx.message_bytes(message_b64)).hexdigest()
