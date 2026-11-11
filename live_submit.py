"""Live sign and send. Unreachable unless every readiness gate is closed.

The signer is the server Phantom session for the allowlisted wallet. The send
is a Helius JSON-RPC call. Tests pass both in. This module does not open a
socket by itself, and a paper run never reaches it.
"""

from __future__ import annotations

import base64
import hashlib

import db
import memecoin_desk
import owner_slippage
import phantom_transport
import solana_tx

SECRET_MARK = "api-key="


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


def submit(case_id: str, message_sha256: str, now: float, transport=None, signer=None, sender=None) -> dict:
    """Sign, send, and confirm one reserved message. Refuses while any gate is open."""
    refused = _blank()
    if not armed():
        return refused | {"reason": "live_unreachable"}
    return _exchange(case_id, message_sha256, now, transport, signer, sender)


def _exchange(case_id: str, message_sha256: str, now: float, transport, signer, sender) -> dict:
    refused = _blank()
    if not isinstance(case_id, str) or not isinstance(message_sha256, str) or message_sha256 == "":
        return refused | {"reason": "message_refused"}
    rows = db.query("SELECT * FROM memecoin_reservations WHERE message_sha256 = ?", (message_sha256,))
    if len(rows) != 1 or rows[0]["case_id"] != case_id:
        return refused | {"reason": "message_refused"}
    proof = rows[0]
    try:
        raw = base64.b64decode(proof["message_b64"], validate=True)
        decoded = solana_tx.decode_transaction(raw)
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
    if capital.get("ticket_limit_proven") is not True or capital.get("allocation_proven") is not True:
        return refused | {"reason": "ticket"}
    totals = memecoin_desk.loss_totals()
    if totals.get("live_halts_new_entries") is True:
        return refused | {"reason": "stop_new_entries"}
    if signer is None or sender is None:
        return refused | {"reason": "phantom_session_missing"}
    return _sign_send_confirm(case_id, proof["message_b64"], message_sha256, now, signer, sender)


def _sign_send_confirm(case_id: str, message_b64: str, message_sha256: str, now: float, signer, sender) -> dict:
    refused = _blank()
    try:
        signed = signer(message_b64)
    except Exception:
        return refused | {"reason": "signature_refused"}
    signed_tx = None if not isinstance(signed, dict) else signed.get("signed_tx")
    if not isinstance(signed_tx, str) or signed_tx == "":
        return refused | {"reason": "signature_refused"}
    try:
        reserved = solana_tx.message_bytes(message_b64)
        produced = solana_tx.message_bytes(signed_tx)
        signature = solana_tx.first_signature(signed_tx)
    except Exception:
        return refused | {"reason": "signature_refused"}
    if reserved != produced or signature == "":
        return refused | {"reason": "message_mismatch"}
    send_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendTransaction",
        "params": [signed_tx, {"encoding": "base64", "skipPreflight": False}],
    }
    posted = _post(sender, send_body)
    if posted.get("ok") is not True:
        return refused | {"reason": posted.get("reason") or "send_refused", "sign": True}
    result = posted["body"].get("result")
    if result != signature:
        return refused | {"reason": "send_refused", "sign": True}
    status_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignatureStatuses",
        "params": [[signature], {"searchTransactionHistory": True}],
    }
    confirmed = _post(sender, status_body)
    if confirmed.get("ok") is not True:
        return refused | {"reason": confirmed.get("reason") or "confirmation_pending", "sign": True, "signature": signature}
    value = ((confirmed["body"].get("result") or {}).get("value") or [None])[0]
    if not isinstance(value, dict) or value.get("err") is not None:
        return refused | {"reason": "confirmation_err", "sign": True, "signature": signature}
    if value.get("confirmationStatus") not in {"processed", "confirmed", "finalized"}:
        return refused | {"reason": "confirmation_pending", "sign": True, "signature": signature}
    evidence = {
        "message_sha256": message_sha256,
        "signature": signature,
        "slot": value.get("slot"),
        "confirmation_status": value.get("confirmationStatus"),
        "err": None,
        "wallet": phantom_transport.REQUIRED_WALLET,
    }
    if _leaks(evidence):
        return refused | {"reason": "receipt_unrecorded", "sign": True, "signature": signature}
    try:
        memecoin_desk._ensure()
        case = memecoin_desk._case(case_id)
        mint = None if case is None else case["mint"]
        db.exclusive(lambda conn: memecoin_desk._log(
            conn, case_id, mint, "live_submit", "pass", None, "sendTransaction", evidence, float(now),
        ))
    except Exception:
        return refused | {"reason": "receipt_unrecorded", "sign": True, "signature": signature}
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


def _post(sender, body: dict) -> dict:
    try:
        posted = sender(body)
    except Exception:
        return {"ok": False, "reason": "rpc_transport"}
    if not isinstance(posted, dict) or posted.get("status") != 200 or not isinstance(posted.get("body"), dict):
        return {"ok": False, "reason": "send_refused"}
    if posted["body"].get("error"):
        return {"ok": False, "reason": "send_refused"}
    if _leaks(posted["body"]):
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
