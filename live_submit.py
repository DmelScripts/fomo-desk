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
_CONFIRM_S = 8.0
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
    # real_submit closes only after a confirmed fill, so the first send cannot require it.
    return all(
        isinstance(item, dict) and (item.get("closed") is True or item.get("gate") == "real_submit")
        for item in gates
    )


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
    exiting = proof["side"] == "exit"
    if capital.get("reduce_only") is True and exiting:
        pass
    elif capital.get("ticket_limit_proven") is not True:
        return refused | {"reason": "ticket"}
    elif capital.get("allocation_proven") is not True:
        return refused | {"reason": "allocation"}
    totals = memecoin_desk.loss_totals()
    if totals.get("live_halts_new_entries") is True and not exiting:
        return refused | {"reason": "stop_new_entries"}
    if not callable(signer):
        return refused | {"reason": "phantom_session_missing"}
    session_ready = getattr(signer, "ready", None)
    if callable(session_ready) and session_ready() is not True:
        return refused | {"reason": "phantom_session_missing"}
    if db.get_setting("mode", "PAPER") == "LIVE":
        import time as _time
        moment = _time.time()
        missing = desk_loop._live_trade_proofs(memecoin_desk._case(case_id), proof, moment)
        if missing is not None:
            return refused | {"reason": missing}
        moved = desk_loop._requote_guard(decoded, proof, moment)
        if moved is not None:
            return refused | {"reason": moved}
        simulated = desk_loop.live_simulate(case_id, message_sha256, moment)
        if simulated.get("preflight_ready") is not True:
            return refused | {"reason": simulated.get("reason") or "preflight_open", "sign": False, "send": False}
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
        signed_decoded = solana_tx.decode_transaction(__import__("base64").b64decode(signed_tx, validate=True))
    except Exception:
        _release(message_sha256)
        return refused | {"reason": "signature_refused"}
    if signed_decoded.fee_payer != phantom_transport.REQUIRED_WALLET:
        _release(message_sha256)
        return refused | {"reason": "signer_mismatch"}
    if hashlib.sha256(reserved).digest() != hashlib.sha256(produced).digest() or reserved != produced or signature == "":
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


def _status_value(posted: dict):
    if posted.get("ok") is not True or not isinstance(posted.get("body"), dict):
        return None
    result = posted["body"].get("result")
    values = result.get("value") if isinstance(result, dict) else None
    if not isinstance(values, list) or not values:
        return None
    return values[0]


def _status_rpc(signature: str) -> dict:
    return _rpc({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignatureStatuses",
        "params": [[signature], {"searchTransactionHistory": True}],
    })


def _poll_status(signature: str) -> dict:
    """Poll until confirmed. A test transport is one read. The owned path stops at the deadline."""
    import time
    import solana_rpc
    deadline = time.monotonic() + _CONFIRM_S
    while True:
        posted = _status_rpc(signature)
        if solana_rpc.TRANSPORT is not None:
            return posted
        if posted.get("ok") is not True:
            return posted
        value = _status_value(posted)
        if isinstance(value, dict) and (value.get("err") is not None or value.get("confirmationStatus") in _CONFIRMED):
            return posted
        if time.monotonic() >= deadline:
            posted["timed_out"] = True
            return posted
        time.sleep(0.25)


def _pull_fill(signature: str) -> dict | None:
    posted = _rpc({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [signature, {"encoding": "base64", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}],
    })
    if posted.get("ok") is not True or not isinstance(posted.get("body"), dict):
        return None
    result = posted["body"].get("result")
    import live_gate_proof
    if not live_gate_proof._fill_matches(result):
        return None
    summary = live_gate_proof._fill_summary(result)
    return summary if isinstance(summary, dict) else None


def _reconcile(case_id: str, message_sha256: str, signature: str, status: str, now: float, fallback: str | None = None) -> dict:
    refused = _blank() | {"sign": True, "signature": signature}
    if status == "confirmed":
        return _confirmed(signature)
    posted = _poll_status(signature)
    if posted.get("reason") == "rpc_transport":
        return refused | {"reason": "rpc_transport"}
    value = _status_value(posted)
    if isinstance(value, dict) and value.get("err") is not None:
        return refused | {"reason": "confirmation_err"}
    confirmed = isinstance(value, dict) and value.get("err") is None and value.get("confirmationStatus") in _CONFIRMED
    if posted.get("ok") is not True or posted.get("timed_out") is True or (not confirmed and status == "unknown"):
        _mark_unknown(message_sha256, signature)
        if posted.get("ok") is not True:
            posted = _status_rpc(signature)
            value = _status_value(posted)
            confirmed = isinstance(value, dict) and value.get("err") is None and value.get("confirmationStatus") in _CONFIRMED
        if not confirmed:
            summary = _pull_fill(signature)
            if summary is None:
                return refused | {"reason": "open_liability"}
            return _commit(case_id, message_sha256, signature, value if isinstance(value, dict) else {}, now, summary)
    if not confirmed:
        return refused | {"reason": "confirmation_pending"}
    summary = _pull_fill(signature)
    return _commit(case_id, message_sha256, signature, value, now, summary)


def _commit(case_id: str, message_sha256: str, signature: str, value: dict, now: float, summary) -> dict:
    refused = _blank() | {"sign": True, "signature": signature}
    evidence = {
        "message_sha256": message_sha256,
        "signature": signature,
        "slot": value.get("slot") if isinstance(value, dict) else None,
        "confirmation_status": value.get("confirmationStatus") if isinstance(value, dict) else "confirmed",
        "err": None,
        "wallet": phantom_transport.REQUIRED_WALLET,
    }
    if isinstance(summary, dict):
        evidence["output_delta"] = summary.get("output_delta")
        evidence["fee_lamports"] = summary.get("fee_lamports")
        evidence["mint"] = summary.get("mint")
    if _leaks(evidence):
        return refused | {"reason": "receipt_unrecorded"}
    case = memecoin_desk._case(case_id)
    mint = None if case is None else case["mint"]
    try:
        wrote = db.exclusive(lambda conn: _finish(conn, case_id, mint, message_sha256, signature, evidence, now, summary))
    except Exception:
        return refused | {"reason": "receipt_unrecorded"}
    if wrote is False:
        return _confirmed(signature)
    return _confirmed(signature)


def _mark_unknown(message_sha256: str, signature: str) -> None:
    db.execute(
        "UPDATE live_submit SET status = 'unknown' WHERE message_sha256 = ? AND signature = ?",
        (message_sha256, signature),
    )


def _finish(conn, case_id: str, mint, message_sha256: str, signature: str, evidence: dict, now: float, summary=None) -> bool:
    cursor = conn.execute(
        """
        UPDATE live_submit SET status = 'confirmed'
        WHERE message_sha256 = ? AND signature = ? AND status IN ('inflight', 'unknown')
        """,
        (message_sha256, signature),
    )
    if cursor.rowcount != 1:
        return False
    memecoin_desk._log(conn, case_id, mint, "live_submit", "pass", None, "sendTransaction", evidence, float(now))
    if isinstance(summary, dict):
        _book_fill(conn, case_id, mint, message_sha256, signature, evidence, now, summary)
    return True


def _book_fill(conn, case_id: str, mint, message_sha256: str, signature: str, evidence: dict, now: float, summary: dict) -> None:
    """Actual output, fee, and slippage. A breach halts new entries and stays in the book."""
    import json
    from decimal import Decimal
    import live_gate_proof
    import pyth_sponsored
    delta = summary.get("output_delta")
    fee_lamports = summary.get("fee_lamports")
    output_mint = summary.get("mint")
    if type(delta) is not int or isinstance(delta, bool) or type(fee_lamports) is not int or isinstance(fee_lamports, bool):
        return
    reservation = conn.execute(
        "SELECT side, message_b64 FROM memecoin_reservations WHERE message_sha256 = ?",
        (message_sha256,),
    ).fetchone()
    side = reservation["side"] if reservation is not None else "entry"
    route = {}
    if reservation is not None:
        try:
            decoded = solana_tx.decode_transaction(__import__("base64").b64decode(reservation["message_b64"], validate=True))
            route = _route(decoded)
        except Exception:
            route = {}
    quoted = route.get("quoted_out_amount") if route.get("ok") is True else None
    amount_in = route.get("in_amount") if route.get("ok") is True else None
    leg = live_gate_proof.fresh_sol_leg(now)
    fee_usd = pyth_sponsored.lamports_usd(fee_lamports, leg) if isinstance(leg, dict) else None
    slippage_bps = None
    if type(quoted) is int and not isinstance(quoted, bool) and quoted > 0 and delta >= 0:
        if delta >= quoted:
            slippage_bps = 0
        else:
            slippage_bps = int((Decimal(quoted - delta) * Decimal(10_000) / Decimal(quoted)).to_integral_value(rounding="ROUND_CEILING"))
    halt = fee_usd is None or fee_usd > memecoin_desk.MAX_FEE or (slippage_bps is not None and slippage_bps > owner_slippage.BPS)
    receipt = {
        "signature": signature,
        "side": side,
        "mint": output_mint,
        "amount_in": amount_in,
        "amount_out": delta,
        "quoted_out": quoted,
        "fee_lamports": fee_lamports,
        "fee_usd": None if fee_usd is None else format(fee_usd, "f"),
        "slippage_bps": slippage_bps,
        "halt_new_entries": halt,
    }
    if _leaks(receipt):
        raise RuntimeError("receipt_unrecorded")
    memecoin_desk._log(conn, case_id, mint, "live_fill", "fail" if halt else "pass", None, "getTransaction", receipt, float(now))
    slot = evidence.get("slot")
    if type(slot) is not int or isinstance(slot, bool) or slot < 0:
        slot = 0
    qty = delta if delta > 0 else 0
    basis = amount_in if type(amount_in) is int and not isinstance(amount_in, bool) and amount_in > 0 else 0
    fee_text = "0.00" if fee_usd is None else format(fee_usd, "f")
    case_ledger = conn.execute("SELECT ledger, ledger_provenance FROM memecoin_cases WHERE id = ?", (case_id,)).fetchone()
    booked = conn.execute(
        "SELECT ledger, ledger_provenance FROM memecoin_reservations WHERE message_sha256 = ?",
        (message_sha256,),
    ).fetchone()
    live_book = (
        case_ledger is not None and booked is not None
        and case_ledger["ledger"] == "live" and case_ledger["ledger_provenance"] == "server"
        and booked["ledger"] == "live" and booked["ledger_provenance"] == "server"
    )
    if live_book and side == "exit":
        conn.execute(
            """
            INSERT OR IGNORE INTO memecoin_exit_receipts (
                signature, case_id, message_sha256, slot, fee_usd, qty, ledger, ledger_provenance
            ) VALUES (?, ?, ?, ?, ?, ?, 'live', 'server')
            """,
            (signature, case_id, message_sha256, slot, fee_text, qty),
        )
    elif live_book:
        conn.execute(
            """
            INSERT OR IGNORE INTO memecoin_entry_receipts (
                signature, case_id, message_sha256, slot, qty, cost_basis_lamports, network_fee_lamports,
                dex_fee_lamports, fee_usd, failed, ledger, ledger_provenance
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 0, 'live', 'server')
            """,
            (signature, case_id, message_sha256, slot, qty, basis, fee_lamports, fee_text),
        )
    _apply_book(conn, case_id, side, signature, delta, basis, fee_usd, leg, now)
    if halt:
        memecoin_desk._log(
            conn, case_id, mint, "live_halt_review", "fail", None, "getTransaction",
            {"fee_usd": receipt["fee_usd"], "slippage_bps": slippage_bps}, float(now),
        )
        conn.execute(
            """
            INSERT INTO settings (key, value, updated_at) VALUES ('live_halt_review', '1', ?)
            ON CONFLICT(key) DO UPDATE SET value = '1', updated_at = excluded.updated_at
            """,
            (float(now),),
        )


def _apply_book(conn, case_id: str, side: str, signature: str, delta: int, basis: int, fee_usd, leg, now: float) -> None:
    """Realized P&L and the reserved dollars the $100 cap reads."""
    import json
    from decimal import Decimal
    import pyth_sponsored
    row = conn.execute("SELECT snapshot_json FROM memecoin_cases WHERE id = ?", (case_id,)).fetchone()
    if row is None:
        return
    body = json.loads(row["snapshot_json"])
    if not isinstance(body, dict):
        body = {}
    if side == "entry" and isinstance(leg, dict) and fee_usd is not None and basis > 0:
        spent = pyth_sponsored.lamports_usd(basis, leg)
        if spent is not None:
            body["reserved_usd"] = format((spent + fee_usd).quantize(Decimal("0.01")), "f")
        body["entry_funding"] = {
            "signature": signature,
            "source": "entry_receipt",
            "proven": True,
            "tokens_out": delta,
            "fee_usd": format(fee_usd, "f"),
        }
    if side == "exit" and isinstance(leg, dict):
        proceeds = pyth_sponsored.lamports_usd(delta, leg) if delta > 0 else None
        prior = conn.execute(
            "SELECT cost_basis_lamports, fee_usd FROM memecoin_entry_receipts WHERE case_id = ? AND failed = 0",
            (case_id,),
        ).fetchone()
        if proceeds is not None and prior is not None:
            cost = pyth_sponsored.lamports_usd(prior["cost_basis_lamports"], leg)
            try:
                prior_fee = Decimal(prior["fee_usd"])
            except Exception:
                prior_fee = None
            if cost is not None and prior_fee is not None and fee_usd is not None:
                realized = (proceeds - cost - prior_fee - fee_usd).quantize(Decimal("0.01"))
                body["realized_pnl"] = format(realized, "f")
                body["pnl_live"] = True
    conn.execute(
        "UPDATE memecoin_cases SET snapshot_json = ?, updated_at = ? WHERE id = ?",
        (json.dumps(body, sort_keys=True), float(now), case_id),
    )


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
                    INSERT INTO live_submit (message_sha256, idempotency_key, signature, case_id, status, created_at)
                    VALUES (?, ?, NULL, ?, 'inflight', ?)
                    """,
                    (message_sha256, message_sha256, case_id, float(now)),
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
            idempotency_key TEXT NOT NULL,
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
        return {"ok": False, "reason": reason, "proved": False}
    import json
    import urllib.request
    import live_gate_proof
    import solana_rpc
    if solana_rpc.TRANSPORT is not None:
        try:
            posted = solana_rpc.post(payload)
        except solana_rpc.RpcRefused as exc:
            return {"ok": False, "reason": exc.reason if exc.reason in {"rpc_timeout", "rpc_transport"} else "rpc_transport", "proved": False}
        except TimeoutError:
            return {"ok": False, "reason": "rpc_timeout", "proved": False}
        except Exception:
            return {"ok": False, "reason": "rpc_transport", "proved": False}
        if not isinstance(posted, dict) or posted.get("status") != 200 or not isinstance(posted.get("body"), dict):
            return {"ok": False, "reason": "send_refused", "proved": False}
        if posted["body"].get("error") or _leaks(posted["body"]):
            return {"ok": False, "reason": "send_refused", "proved": False}
        return {"ok": True, "body": posted["body"], "proved": False}
    try:
        url = solana_rpc._wire_url()
    except solana_rpc.RpcRefused as exc:
        return {"ok": False, "reason": exc.reason, "proved": False}
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json", "accept": "application/json"},
        method="POST",
    )
    del url
    try:
        status, raw, proved = live_gate_proof.exchange(request, 8)
        parsed = json.loads(raw.decode("utf-8"))
    except TimeoutError:
        return {"ok": False, "reason": "rpc_timeout", "proved": False}
    except Exception:
        return {"ok": False, "reason": "rpc_transport", "proved": False}
    if status != 200 or not isinstance(parsed, dict) or parsed.get("error") or _leaks(parsed):
        return {"ok": False, "reason": "send_refused", "proved": False}
    return {"ok": True, "body": parsed, "proved": proved is True}


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
