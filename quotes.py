"""PAPER quote requests. This process does not call Phantom or sign anything.

A quote is recorded only after the owner posts a result that contains no
execution. execute is never stored as true.
"""

from __future__ import annotations

import re
import time

from fastapi import HTTPException

import db

LABEL = "PAPER · quote only"
PROVIDER = "phantom"
CHAIN = "solana:mainnet"
_ID = re.compile(r"^[A-Za-z0-9._-]{1,40}$")
_AMOUNT = re.compile(r"^[0-9]+(\.[0-9]+)?$")
_MINT = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_WALLET = _MINT
_FORBIDDEN = {
    "execute", "execution", "signature", "rawtransaction", "raw_transaction",
    "confirmed", "transaction", "signedtransaction", "signed_transaction",
    "secret", "session", "stamper", "privatekey", "private_key", "broadcast",
    "sign", "transfer",
}


def create_request(body: dict) -> dict:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    _reject_forbidden(body)
    if _truthy(body.get("execute")):
        raise HTTPException(status_code=400, detail="quotes do not execute")
    quote_id = body.get("id")
    if not isinstance(quote_id, str) or _ID.fullmatch(quote_id) is None:
        raise HTTPException(status_code=400, detail="id must be 1 to 40 safe characters")
    amount = _amount(body.get("amount"))
    chain = body.get("chain") or CHAIN
    if chain != CHAIN:
        raise HTTPException(status_code=400, detail="only solana:mainnet quotes are accepted")
    native = _truthy(body.get("native"))
    mint = body.get("mint")
    if native:
        mint = None
    elif not isinstance(mint, str) or _MINT.fullmatch(mint) is None:
        raise HTTPException(status_code=400, detail="mint must be a Solana address, or set native true")
    slippage = _slippage(body.get("slippage"))
    existing = _row(quote_id)
    if existing:
        return _public(existing) | {"bridge": "not_connected", "execute": False}
    now = time.time()
    db.execute(
        """
        INSERT INTO quotes (
            id, status, label, provider, wallet, amount, mint, chain, slippage,
            expiry, out_amount, failure, created_at, updated_at
        ) VALUES (?, 'pending', ?, ?, NULL, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)
        """,
        (quote_id, LABEL, PROVIDER, amount, mint, chain, slippage, now, now),
    )
    return _public(_row(quote_id)) | {"bridge": "not_connected", "execute": False}


def record_result(body: dict) -> dict:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    _reject_forbidden(body)
    if _truthy(body.get("execute")):
        raise HTTPException(status_code=400, detail="quotes do not execute")
    quote_id = body.get("id")
    row = _row(quote_id) if isinstance(quote_id, str) else None
    if row is None:
        raise HTTPException(status_code=404, detail="unknown quote")
    if row["status"] != "pending":
        return _public(row) | {"execute": False}
    failure = body.get("failure")
    if failure is not None:
        if failure not in {"no_quote", "rejected", "expired", "provider"}:
            raise HTTPException(status_code=400, detail="unknown failure")
        db.execute(
            "UPDATE quotes SET status = 'failed', failure = ?, updated_at = ? WHERE id = ?",
            (failure, time.time(), quote_id),
        )
        return _public(_row(quote_id)) | {"execute": False}
    wallet = body.get("wallet")
    if not isinstance(wallet, str) or _WALLET.fullmatch(wallet) is None:
        raise HTTPException(status_code=400, detail="wallet must be a Solana address")
    out_amount = _amount(body.get("out_amount"))
    expiry = body.get("expiry")
    now = time.time()
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not (now < float(expiry) <= now + 86400):
        raise HTTPException(status_code=400, detail="expiry must be a unix time within one day")
    slippage = _slippage(body.get("slippage"))
    if body.get("chain") not in (None, CHAIN) or body.get("provider") not in (None, PROVIDER):
        raise HTTPException(status_code=400, detail="provider and chain are fixed")
    db.execute(
        """
        UPDATE quotes
        SET status = 'quoted', wallet = ?, out_amount = ?, expiry = ?, slippage = COALESCE(?, slippage),
            failure = NULL, updated_at = ?
        WHERE id = ? AND status = 'pending'
        """,
        (wallet, out_amount, float(expiry), slippage, now, quote_id),
    )
    return _public(_row(quote_id)) | {"execute": False}


def listed() -> list[dict]:
    rows = db.query(
        """
        SELECT id, status, label, provider, wallet, amount, mint, chain, slippage,
               expiry, out_amount, failure
        FROM quotes ORDER BY created_at DESC LIMIT 20
        """
    )
    return [_public(row) for row in rows]


def _row(quote_id: str):
    rows = db.query("SELECT * FROM quotes WHERE id = ?", (quote_id,))
    return rows[0] if rows else None


def _public(row) -> dict:
    return {
        "id": row["id"],
        "status": row["status"],
        "label": LABEL,
        "provider": PROVIDER,
        "wallet": row["wallet"],
        "amount": row["amount"],
        "mint": row["mint"],
        "chain": row["chain"],
        "slippage": row["slippage"],
        "expiry": row["expiry"],
        "out_amount": row["out_amount"],
        "failure": row["failure"],
        "execute": False,
    }


def _amount(value) -> str:
    if isinstance(value, bool) or isinstance(value, float):
        raise HTTPException(status_code=400, detail="amount must be a decimal string")
    text = str(value) if isinstance(value, int) else value
    if not isinstance(text, str) or _AMOUNT.fullmatch(text) is None or text in {"0", "0.0"}:
        raise HTTPException(status_code=400, detail="amount must be a positive decimal")
    if text.startswith("0") and not text.startswith("0."):
        raise HTTPException(status_code=400, detail="amount must be a positive decimal")
    return text


def _slippage(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HTTPException(status_code=400, detail="slippage must be 0 to 100")
    if not 0 <= float(value) <= 100:
        raise HTTPException(status_code=400, detail="slippage must be 0 to 100")
    return float(value)


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return value is True


def _reject_forbidden(body: dict) -> None:
    for key in body:
        if str(key).lower().replace("-", "_") in _FORBIDDEN:
            raise HTTPException(status_code=400, detail="execution fields are refused")
