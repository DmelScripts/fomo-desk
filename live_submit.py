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
        return _open_liability(case_id, message_sha256, signature, {}, now, None)
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


def _pull_result(signature: str):
    posted = _rpc({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [signature, {"encoding": "base64", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}],
    })
    if posted.get("ok") is not True or not isinstance(posted.get("body"), dict):
        return None
    result = posted["body"].get("result")
    return result if isinstance(result, dict) else None


def _observed_fill(result):
    if not isinstance(result, dict):
        return None
    import live_gate_proof
    summary = live_gate_proof._fill_summary(result)
    return summary if isinstance(summary, dict) else None


def _reference_message(message_sha256: str) -> str | None:
    rows = db.query("SELECT message_b64 FROM memecoin_reservations WHERE message_sha256 = ?", (message_sha256,))
    if len(rows) != 1 or not isinstance(rows[0]["message_b64"], str) or rows[0]["message_b64"] == "":
        return None
    return rows[0]["message_b64"]


def _accounts_rejected(result) -> bool:
    summary = _observed_fill(result)
    return isinstance(summary, dict) and summary.get("accounts_ok") is False


def _bound_result(result, signature: str, reference: str | None) -> bool:
    import live_gate_proof
    return live_gate_proof._tx_bound(result, signature, reference or "")


def _reconcile(case_id: str, message_sha256: str, signature: str, status: str, now: float, fallback: str | None = None) -> dict:
    refused = _blank() | {"sign": True, "signature": signature}
    if status == "confirmed":
        return _confirmed(signature)
    reference = _reference_message(message_sha256)
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
            result = _pull_result(signature)
            if isinstance(result, dict) and (
                not _bound_result(result, signature, reference) or _accounts_rejected(result)
            ):
                return _open_liability(case_id, message_sha256, signature, value if isinstance(value, dict) else {}, now, None)
            return refused | {"reason": "open_liability"}
    if not confirmed:
        return refused | {"reason": "confirmation_pending"}
    result = _pull_result(signature)
    if not _bound_result(result, signature, reference):
        return _open_liability(case_id, message_sha256, signature, value if isinstance(value, dict) else {}, now, None)
    summary = _observed_fill(result)
    if isinstance(summary, dict) and summary.get("positive") is True:
        return _commit(case_id, message_sha256, signature, value, now, summary)
    return _open_liability(case_id, message_sha256, signature, value if isinstance(value, dict) else {}, now, summary)


def _liability_reason(summary) -> str:
    delta = None if not isinstance(summary, dict) else summary.get("output_delta")
    if type(delta) is int and not isinstance(delta, bool) and delta <= 0:
        return "fill_anomaly"
    if isinstance(summary, dict) and summary.get("positive") is not True and type(summary.get("output_delta")) is int:
        return "fill_anomaly"
    return "fill_unmatched"


def _open_liability(case_id: str, message_sha256: str, signature: str, value: dict, now: float, summary) -> dict:
    """Confirmed on the status RPC, but not a positive fill. The claim stays unknown."""
    refused = _blank() | {"sign": True, "signature": signature, "reconciled": False}
    reason = _liability_reason(summary)
    try:
        db.exclusive(lambda conn: _book_anomaly(conn, case_id, message_sha256, signature, value, now, summary, reason))
    except Exception:
        _mark_unknown(message_sha256, signature)
        return refused | {"reason": "receipt_unrecorded"}
    return refused | {"reason": reason}


def _commit(case_id: str, message_sha256: str, signature: str, value: dict, now: float, summary) -> dict:
    refused = _blank() | {"sign": True, "signature": signature}
    if not isinstance(summary, dict) or summary.get("positive") is not True:
        return _open_liability(case_id, message_sha256, signature, value, now, summary)
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
    if not isinstance(summary, dict) or summary.get("positive") is not True:
        return False
    memecoin_desk._log(conn, case_id, mint, "live_submit", "pass", None, "sendTransaction", evidence, float(now))
    _book_fill(conn, case_id, mint, message_sha256, signature, evidence, now, summary)
    return True


def _slippage_bps(quoted, delta):
    """Basis points given up versus the quote. A missing or non-positive output is the whole quote."""
    from decimal import Decimal
    if type(quoted) is not int or isinstance(quoted, bool) or quoted <= 0:
        return None
    if type(delta) is not int or isinstance(delta, bool):
        return 10_000
    if delta >= quoted:
        return 0
    return int((Decimal(quoted - delta) * Decimal(10_000) / Decimal(quoted)).to_integral_value(rounding="ROUND_CEILING"))


def _book_anomaly(conn, case_id: str, message_sha256: str, signature: str, value: dict, now: float, summary, reason: str) -> None:
    """A confirmed status that is not a positive fill. Loss is booked and new entries stop."""
    import json
    from decimal import Decimal
    import live_gate_proof
    import pyth_sponsored
    reservation = conn.execute(
        "SELECT side, message_b64, ledger, ledger_provenance FROM memecoin_reservations WHERE message_sha256 = ?",
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
    delta = None if not isinstance(summary, dict) else summary.get("output_delta")
    if type(delta) is not int or isinstance(delta, bool):
        delta = None
    fee_lamports = None if not isinstance(summary, dict) else summary.get("fee_lamports")
    if type(fee_lamports) is not int or isinstance(fee_lamports, bool) or fee_lamports < 0:
        fee_lamports = None
    quoted = None if not isinstance(summary, dict) else summary.get("quoted_out")
    if type(quoted) is not int or isinstance(quoted, bool):
        quoted = route.get("quoted_out_amount") if route.get("ok") is True else None
    amount_in = None if not isinstance(summary, dict) else summary.get("in_amount")
    if type(amount_in) is not int or isinstance(amount_in, bool) or amount_in <= 0:
        amount_in = route.get("in_amount") if route.get("ok") is True else None
    leg = live_gate_proof.fresh_sol_leg(now)
    slippage_bps = _slippage_bps(quoted, delta)
    fee_usd = pyth_sponsored.lamports_usd(fee_lamports, leg) if isinstance(leg, dict) and type(fee_lamports) is int else None
    loss = None
    if side == "entry" and isinstance(leg, dict) and type(amount_in) is int and amount_in > 0:
        spent = pyth_sponsored.lamports_usd(amount_in, leg)
        if spent is not None:
            loss = spent if fee_usd is None else (spent + fee_usd).quantize(Decimal("0.01"))
    receipt = {
        "signature": signature,
        "side": side,
        "mint": None if not isinstance(summary, dict) else summary.get("mint"),
        "amount_in": amount_in,
        "amount_out": delta,
        "quoted_out": quoted,
        "fee_lamports": fee_lamports,
        "fee_usd": None if fee_usd is None else format(fee_usd, "f"),
        "loss_usd": None if loss is None else format(loss, "f"),
        "slippage_bps": slippage_bps,
        "anomalous": True,
        "halt_new_entries": True,
        "reason": reason,
    }
    if _leaks(receipt):
        raise RuntimeError("receipt_unrecorded")
    conn.execute(
        "UPDATE live_submit SET status = 'unknown' WHERE message_sha256 = ? AND signature = ? AND status != 'confirmed'",
        (message_sha256, signature),
    )
    already = conn.execute(
        "SELECT evidence_json FROM memecoin_log WHERE case_id = ? AND stage = 'live_fill'",
        (case_id,),
    ).fetchall()
    if any(signature in (row["evidence_json"] or "") for row in already):
        _halt_entries(conn, now)
        return
    case_row = conn.execute("SELECT mint FROM memecoin_cases WHERE id = ?", (case_id,)).fetchone()
    mint = None if case_row is None else case_row["mint"]
    memecoin_desk._log(conn, case_id, mint, "live_fill", "fail", reason, "getTransaction", receipt, float(now))
    memecoin_desk._log(
        conn, case_id, mint, "live_halt_review", "fail", reason, "getTransaction",
        {"fee_usd": receipt["fee_usd"], "slippage_bps": slippage_bps, "loss_usd": receipt["loss_usd"], "anomalous": True},
        float(now),
    )
    _halt_entries(conn, now)
    if loss is None:
        return
    case_ledger = conn.execute("SELECT ledger, ledger_provenance, snapshot_json FROM memecoin_cases WHERE id = ?", (case_id,)).fetchone()
    live_book = (
        case_ledger is not None and reservation is not None
        and case_ledger["ledger"] == "live" and case_ledger["ledger_provenance"] == "server"
        and reservation["ledger"] == "live" and reservation["ledger_provenance"] == "server"
    )
    body = {}
    if case_ledger is not None:
        try:
            body = json.loads(case_ledger["snapshot_json"])
        except Exception:
            body = {}
    if not isinstance(body, dict):
        body = {}
    body["realized_pnl"] = format(-loss, "f")
    body["pnl_live"] = True
    body["reserved_usd"] = format(loss, "f")
    body["entry_funding"] = {
        "signature": signature,
        "source": "entry_receipt",
        "proven": False,
        "anomalous": True,
        "tokens_out": 0,
        "fee_usd": format(fee_usd, "f") if fee_usd is not None else "0.00",
    }
    conn.execute(
        "UPDATE memecoin_cases SET snapshot_json = ?, stage = 'entry_failed', updated_at = ? WHERE id = ?",
        (json.dumps(body, sort_keys=True), float(now), case_id),
    )
    if not live_book or side != "entry":
        return
    slot = value.get("slot") if isinstance(value, dict) else 0
    if type(slot) is not int or isinstance(slot, bool) or slot < 0:
        slot = 0
    conn.execute(
        """
        INSERT OR IGNORE INTO memecoin_entry_receipts (
            signature, case_id, message_sha256, slot, qty, cost_basis_lamports, network_fee_lamports,
            dex_fee_lamports, fee_usd, failed, ledger, ledger_provenance
        ) VALUES (?, ?, ?, ?, 0, ?, ?, 0, ?, 1, 'live', 'server')
        """,
        (
            signature, case_id, message_sha256, slot, amount_in,
            fee_lamports if type(fee_lamports) is int else 0,
            format(fee_usd, "f") if fee_usd is not None else "0.00",
        ),
    )


def _halt_entries(conn, now: float) -> None:
    conn.execute(
        """
        INSERT INTO settings (key, value, updated_at) VALUES ('live_halt_review', '1', ?)
        ON CONFLICT(key) DO UPDATE SET value = '1', updated_at = excluded.updated_at
        """,
        (float(now),),
    )


def _book_fill(conn, case_id: str, mint, message_sha256: str, signature: str, evidence: dict, now: float, summary: dict) -> None:
    """Actual output, fee, and slippage. A breach halts new entries and stays in the book."""
    import json
    from decimal import Decimal
    import live_gate_proof
    import pyth_sponsored
    delta = summary.get("output_delta")
    fee_lamports = summary.get("fee_lamports")
    output_mint = summary.get("mint")
    if type(delta) is not int or isinstance(delta, bool):
        return
    if type(fee_lamports) is not int or isinstance(fee_lamports, bool) or fee_lamports < 0:
        fee_lamports = None
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
    quoted = summary.get("quoted_out")
    if type(quoted) is not int or isinstance(quoted, bool):
        quoted = route.get("quoted_out_amount") if route.get("ok") is True else None
    amount_in = summary.get("in_amount")
    if type(amount_in) is not int or isinstance(amount_in, bool):
        amount_in = route.get("in_amount") if route.get("ok") is True else None
    leg = live_gate_proof.fresh_sol_leg(now)
    # meta.fee is the network fee. ATA rent is recorded beside it and is not part of
    # the $0.05 cap: preflight prices rent as a deposit against the ticket, not as
    # _simulated_fee.
    fee_usd = pyth_sponsored.lamports_usd(fee_lamports, leg) if isinstance(leg, dict) and type(fee_lamports) is int else None
    slippage_bps = _slippage_bps(quoted, delta)
    halt = (
        summary.get("positive") is not True
        or fee_usd is None
        or fee_usd > memecoin_desk.MAX_FEE
        or slippage_bps is None
        or slippage_bps > owner_slippage.BPS
    )
    receipt = {
        "signature": signature,
        "side": side,
        "mint": output_mint,
        "amount_in": amount_in,
        "amount_out": delta,
        "quoted_out": quoted,
        "fee_lamports": fee_lamports,
        "fee_usd": None if fee_usd is None else format(fee_usd, "f"),
        "rent_lamports": summary.get("rent_lamports") if type(summary.get("rent_lamports")) is int else 0,
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
    _apply_book(
        conn, case_id, side, signature, delta, basis, fee_usd, leg, now,
        receipt["rent_lamports"],
        summary.get("rent_released_lamports") if type(summary.get("rent_released_lamports")) is int else 0,
    )
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


def _apply_book(conn, case_id: str, side: str, signature: str, delta: int, basis: int, fee_usd, leg, now: float, rent_lamports: int = 0, rent_released: int = 0) -> None:
    """Realized P&L and the reserved dollars the $100 cap reads.

    Open ATA rent is reserved capital, not P&L and not part of the network fee.
    """
    import json
    from decimal import Decimal
    import memecoin_desk
    import pyth_sponsored
    row = conn.execute("SELECT snapshot_json FROM memecoin_cases WHERE id = ?", (case_id,)).fetchone()
    if row is None:
        return
    body = json.loads(row["snapshot_json"])
    if not isinstance(body, dict):
        body = {}
    if type(rent_lamports) is not int or isinstance(rent_lamports, bool) or rent_lamports < 0:
        rent_lamports = 0
    if type(rent_released) is not int or isinstance(rent_released, bool) or rent_released < 0:
        rent_released = 0
    rent_usd = pyth_sponsored.lamports_usd(rent_lamports, leg) if isinstance(leg, dict) and rent_lamports else Decimal("0")
    if rent_usd is None:
        rent_usd = Decimal("0")
    if side == "entry" and isinstance(leg, dict) and fee_usd is not None and basis > 0:
        spent = pyth_sponsored.lamports_usd(basis, leg)
        if spent is not None:
            body["reserved_usd"] = format((spent + fee_usd + rent_usd).quantize(Decimal("0.01")), "f")
        body["rent_lamports"] = rent_lamports
        body["rent_usd"] = format(rent_usd, "f")
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
        stored = body.get("rent_lamports")
        if rent_released and type(stored) is int and not isinstance(stored, bool) and stored > 0:
            release_l = min(stored, rent_released)
            release_usd = pyth_sponsored.lamports_usd(release_l, leg) or Decimal("0")
            current = memecoin_desk._reserved_amount(body.get("reserved_usd")) or Decimal("0")
            body["reserved_usd"] = format(max(Decimal("0"), (current - release_usd).quantize(Decimal("0.01"))), "f")
            body["rent_lamports"] = stored - release_l
            try:
                stored_usd = Decimal(str(body.get("rent_usd") or "0"))
            except Exception:
                stored_usd = Decimal("0")
            body["rent_usd"] = format(max(Decimal("0"), (stored_usd - release_usd).quantize(Decimal("0.01"))), "f")
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
