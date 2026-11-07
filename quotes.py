"""PAPER quote requests. This process does not call Phantom or sign anything.

A request stores an immutable pair and the exact buy-tool arguments for a
later execute:false call. A posted result is a manual import until a bridge
is actually connected.
"""

from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException

import db

LABEL = "PAPER · manually imported Phantom quote"
PROVIDER = "phantom"
PROVENANCE = "manual_import"
CHAIN = "solana:mainnet"
_ID = re.compile(r"^[A-Za-z0-9._-]{1,40}$")
_QUOTE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
_MINT = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_DECIMAL = re.compile(r"^(?:0|[1-9]\d*)(?:\.\d+)?$")
_REQUEST_KEYS = {
    "id", "sellChainId", "buyChainId", "sellTokenMint", "buyTokenMint",
    "sellTokenIsNative", "buyTokenIsNative", "amount", "amountUnit",
    "sellTokenDecimals", "buyTokenDecimals", "slippageTolerance", "exactOut",
}
_ADDRESS_KEYS = {"addressType", "address"}
_WALLET_KEYS = {"addresses"}
_QUOTE_KEYS = {"quoteRequest", "quoteResponse"}
_TOOL_KEYS = {
    "sellChainId", "buyChainId", "buyTokenMint", "buyTokenIsNative", "sellTokenMint",
    "sellTokenIsNative", "amount", "amountUnit", "buyTokenDecimals", "sellTokenDecimals",
    "slippageTolerance", "exactOut", "autoSlippage", "base64EncodedTx", "execute",
    "taker", "derivationIndex",
}
_FORBIDDEN = {
    "execute", "execution", "signature", "rawtransaction", "transaction",
    "signedtransaction", "secret", "session", "stamper", "privatekey",
    "broadcast", "signed", "transactions",
}
_RESULT_KEYS = {"id", "wallet_addresses", "quote"}


def create_request(body: dict) -> dict:
    spec = _request_spec(body)
    existing = _row(spec["id"])
    if existing:
        if existing["request_json"] != spec["request_json"]:
            raise HTTPException(status_code=409, detail="quote id is already bound")
        return _view(existing)
    now = time.time()
    db.execute(
        """
        INSERT INTO quotes (
            id, status, label, provider, provenance, wallet, amount, amount_unit,
            sell_chain, buy_chain, sell_native, buy_native, sell_mint, buy_mint,
            sell_decimals, buy_decimals, exact_out, slippage, expiry, out_amount,
            provider_quote_id, failure, request_json, created_at, updated_at
        ) VALUES (
            ?, 'pending', ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL,
            NULL, NULL, ?, ?, ?
        )
        """,
        (
            spec["id"], LABEL, PROVIDER, PROVENANCE, spec["amount"], spec["amount_unit"],
            spec["sell_chain"], spec["buy_chain"], spec["sell_native"], spec["buy_native"],
            spec["sell_mint"], spec["buy_mint"], spec["sell_decimals"], spec["buy_decimals"],
            spec["exact_out"], spec["slippage"], spec["request_json"], now, now,
        ),
    )
    return _view(_row(spec["id"]))


def record_result(body: dict) -> dict:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    _exact(body, _RESULT_KEYS)
    quote_id = body["id"]
    row = _row(quote_id) if isinstance(quote_id, str) else None
    if row is None:
        raise HTTPException(status_code=404, detail="unknown quote")
    wallet = _solana_wallet(body["wallet_addresses"])
    quote = body["quote"]
    _exact(quote, _QUOTE_KEYS)
    request = quote["quoteRequest"]
    response = quote["quoteResponse"]
    if not isinstance(request, dict) or not isinstance(response, dict):
        raise HTTPException(status_code=400, detail="quote must contain request and response objects")
    _reject_tree(response)
    stored = json.loads(row["request_json"])
    _matches_request(request, stored, wallet)
    provider_quote_id = _provider_quote_id(response)
    out_amount = _quoted_out(response)
    _bound_slippage(response, stored)
    _expire_row(row)
    row = _row(quote_id)
    if row["status"] != "pending":
        if row["wallet"] != wallet or row["provider_quote_id"] != provider_quote_id:
            raise HTTPException(status_code=409, detail="quote id is already bound")
        return _view(row)
    expiry = _expiry(_expiry_value(response))
    now = time.time()
    db.execute(
        """
        UPDATE quotes
        SET status = 'quoted', wallet = ?, out_amount = ?, expiry = ?, provider_quote_id = ?,
            failure = NULL, updated_at = ?
        WHERE id = ? AND status = 'pending'
        """,
        (wallet, out_amount, expiry, provider_quote_id, now, quote_id),
    )
    return _view(_row(quote_id))


def listed() -> list[dict]:
    rows = db.query("SELECT * FROM quotes ORDER BY created_at DESC LIMIT 20")
    for row in rows:
        _expire_row(row)
    rows = db.query("SELECT * FROM quotes ORDER BY created_at DESC LIMIT 20")
    return [_view(row) for row in rows]


def _request_spec(body: dict) -> dict:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    _allow(body, _REQUEST_KEYS)
    quote_id = body.get("id")
    if not isinstance(quote_id, str) or _ID.fullmatch(quote_id) is None:
        raise HTTPException(status_code=400, detail="id must be 1 to 40 safe characters")
    sell_chain = _chain(body.get("sellChainId"))
    buy_chain = _chain(body.get("buyChainId"))
    sell_native, sell_mint = _asset(body, "sell")
    buy_native, buy_mint = _asset(body, "buy")
    if sell_native and buy_native:
        raise HTTPException(status_code=400, detail="sell and buy cannot both be native")
    if sell_mint and buy_mint and sell_mint == buy_mint:
        raise HTTPException(status_code=400, detail="sell and buy must be different assets")
    amount = _amount(body.get("amount"))
    unit = body.get("amountUnit")
    if unit not in {"ui", "base"}:
        raise HTTPException(status_code=400, detail="amountUnit must be ui or base")
    exact_out = body.get("exactOut", False)
    if exact_out is not False:
        raise HTTPException(status_code=400, detail="exactOut must be false")
    sell_decimals = _decimals(body.get("sellTokenDecimals"), required=unit == "ui", native=sell_native)
    buy_decimals = _decimals(body.get("buyTokenDecimals"), required=False, native=buy_native)
    slippage = _slippage(body.get("slippageTolerance"))
    tool = {
        "sellChainId": sell_chain,
        "buyChainId": buy_chain,
        "amount": amount,
        "amountUnit": unit,
        "sellTokenIsNative": sell_native,
        "buyTokenIsNative": buy_native,
        "exactOut": False,
        "slippageTolerance": _json_number(slippage),
        "autoSlippage": False,
        "base64EncodedTx": False,
        "execute": False,
        "derivationIndex": 0,
    }
    if sell_mint:
        tool["sellTokenMint"] = sell_mint
    if buy_mint:
        tool["buyTokenMint"] = buy_mint
    if sell_decimals is not None:
        tool["sellTokenDecimals"] = sell_decimals
    if buy_decimals is not None:
        tool["buyTokenDecimals"] = buy_decimals
    return {
        "id": quote_id,
        "amount": amount,
        "amount_unit": unit,
        "sell_chain": sell_chain,
        "buy_chain": buy_chain,
        "sell_native": 1 if sell_native else 0,
        "buy_native": 1 if buy_native else 0,
        "sell_mint": sell_mint,
        "buy_mint": buy_mint,
        "sell_decimals": sell_decimals,
        "buy_decimals": buy_decimals,
        "exact_out": 0,
        "slippage": float(slippage),
        "request_json": _canonical_json(tool),
        "tool": tool,
    }


def _view(row) -> dict:
    status, usable = _status_on_read(row)
    tool = json.loads(row["request_json"])
    return {
        "id": row["id"],
        "status": status,
        "usable": usable,
        "label": LABEL,
        "provenance": PROVENANCE,
        "bridge": "not_connected",
        "provider": PROVIDER,
        "provider_quote_id": row["provider_quote_id"],
        "wallet": row["wallet"],
        "amount": row["amount"],
        "amount_unit": row["amount_unit"],
        "sell_chain": row["sell_chain"],
        "buy_chain": row["buy_chain"],
        "sell_native": bool(row["sell_native"]),
        "buy_native": bool(row["buy_native"]),
        "sell_mint": row["sell_mint"],
        "buy_mint": row["buy_mint"],
        "sell_decimals": row["sell_decimals"],
        "buy_decimals": row["buy_decimals"],
        "slippage": row["slippage"],
        "expiry": row["expiry"],
        "out_amount": row["out_amount"],
        "failure": row["failure"],
        "execute": False,
        "tool": "buy",
        "tool_args": tool,
        "wallet_tool": "wallet_addresses",
        "wallet_tool_args": {"derivationIndex": 0},
    }


def _status_on_read(row) -> tuple[str, bool]:
    status = row["status"]
    expiry = row["expiry"]
    if status == "quoted" and (expiry is None or float(expiry) <= time.time()):
        return "expired", False
    if status == "quoted":
        return "quoted", True
    return status, False


def _expire_row(row) -> None:
    status, _usable = _status_on_read(row)
    if status == "expired" and row["status"] != "expired":
        db.execute(
            "UPDATE quotes SET status = 'expired', updated_at = ? WHERE id = ? AND status = 'quoted'",
            (time.time(), row["id"]),
        )


def _row(quote_id: str):
    rows = db.query("SELECT * FROM quotes WHERE id = ?", (quote_id,))
    return rows[0] if rows else None


def _chain(value) -> str:
    if value != CHAIN:
        raise HTTPException(status_code=400, detail="only solana:mainnet quotes are accepted")
    return value


def _asset(body: dict, side: str) -> tuple[bool, str | None]:
    native_key = side + "TokenIsNative"
    mint_key = side + "TokenMint"
    native = body.get(native_key, False)
    mint = body.get(mint_key)
    if not isinstance(native, bool):
        raise HTTPException(status_code=400, detail=native_key + " must be a boolean")
    if native and mint is not None:
        raise HTTPException(status_code=400, detail=side + " asset must be native or a mint")
    if native:
        return True, None
    if not isinstance(mint, str) or _MINT.fullmatch(mint) is None:
        raise HTTPException(status_code=400, detail=side + " mint must be a Solana address")
    return False, mint


def _amount(value) -> str:
    if not isinstance(value, str) or _DECIMAL.fullmatch(value) is None:
        raise HTTPException(status_code=400, detail="amount must be a decimal string")
    number = Decimal(value)
    if not number.is_finite() or number <= 0:
        raise HTTPException(status_code=400, detail="amount must be greater than 0")
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _decimals(value, *, required: bool, native: bool):
    if value is None:
        if required and native:
            return 9
        if required:
            raise HTTPException(status_code=400, detail="decimals are required for ui amounts")
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 18:
        raise HTTPException(status_code=400, detail="decimals must be an integer from 0 to 18")
    if native and value != 9:
        raise HTTPException(status_code=400, detail="native SOL ui amounts use 9 decimals")
    return value


def _slippage(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or isinstance(value, bool):
        raise HTTPException(status_code=400, detail="slippageTolerance must be 0 to 100")
    if not math.isfinite(float(value)) or not 0 <= float(value) <= 100:
        raise HTTPException(status_code=400, detail="slippageTolerance must be 0 to 100")
    return float(value)


def _json_number(value: float):
    if float(value).is_integer():
        return int(value)
    return float(value)


def _expiry_value(response: dict):
    found = _response_values(response, ("expiry", "expiresAt", "expires_at", "validUntil", "valid_until"))
    if not found:
        raise HTTPException(status_code=400, detail="quote response has no expiry")
    stamps = [_parse_expiry(value) for value in found]
    if any(abs(stamp - stamps[0]) > 1 for stamp in stamps):
        raise HTTPException(status_code=400, detail="quote expiries disagree")
    return stamps[0]


def _parse_expiry(value) -> float:
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed_at = datetime.fromisoformat(text)
            if parsed_at.tzinfo is None:
                parsed_at = parsed_at.replace(tzinfo=timezone.utc)
            parsed = parsed_at.timestamp()
        except ValueError:
            raise HTTPException(status_code=400, detail="expiry must be a unix time within one day")
        value = parsed
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise HTTPException(status_code=400, detail="expiry must be a unix time within one day")
    stamp = float(value)
    if stamp > 10_000_000_000:
        stamp = stamp / 1000
    return stamp


def _expiry(value) -> float:
    stamp = value if isinstance(value, float) else _parse_expiry(value)
    now = time.time()
    if not now < stamp <= now + 86400:
        raise HTTPException(status_code=400, detail="expiry must be a unix time within one day")
    return stamp


def _quoted_out(response: dict) -> str:
    found = _response_values(response, ("outAmount", "buyAmount", "out_amount", "amountOut", "toAmount"))
    if not found:
        raise HTTPException(status_code=400, detail="quote response has no buy amount")
    amounts = [_response_amount(value) for value in found]
    if any(amount != amounts[0] for amount in amounts):
        raise HTTPException(status_code=400, detail="quote amounts disagree")
    return amounts[0]


def _response_amount(value) -> str:
    if isinstance(value, bool):
        raise HTTPException(status_code=400, detail="amount must be a decimal string")
    if isinstance(value, float):
        if not math.isfinite(value) or value <= 0:
            raise HTTPException(status_code=400, detail="amount must be greater than 0")
        return _amount(format(value, "f"))
    if isinstance(value, int):
        if value <= 0:
            raise HTTPException(status_code=400, detail="amount must be greater than 0")
        return str(value)
    return _amount(value)


def _provider_quote_id(response: dict) -> str | None:
    present = "quoteId" in response or "quote_id" in response or isinstance(response.get("id"), str)
    raw = None
    if "quoteId" in response:
        raw = response["quoteId"]
    elif "quote_id" in response:
        raw = response["quote_id"]
    elif isinstance(response.get("id"), str):
        raw = response["id"]
    else:
        nested = _response_values(response, ("quoteId", "quote_id"))
        present = bool(nested)
        raw = nested[0] if nested else None
    if not present:
        return None
    if not isinstance(raw, str) or _QUOTE_ID.fullmatch(raw) is None:
        raise HTTPException(status_code=400, detail="provider quote id is not usable")
    return raw


def _response_values(response: dict, names: tuple[str, ...]) -> list:
    found = [response[name] for name in names if name in response]
    if found:
        return found
    for value in response.values():
        if isinstance(value, dict):
            for name in names:
                if name in value:
                    found.append(value[name])
    return found


def _bound_slippage(response: dict, tool: dict) -> None:
    found = _response_values(response, ("slippageTolerance",))
    for value in found:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise HTTPException(status_code=400, detail="quote does not match the request")
        if _json_number(float(value)) != tool["slippageTolerance"]:
            raise HTTPException(status_code=400, detail="quote does not match the request")


def _matches_request(request: dict, stored: dict, wallet: str) -> None:
    if not isinstance(request, dict) or set(request) - _TOOL_KEYS:
        raise HTTPException(status_code=400, detail="unexpected field")
    for key, value in stored.items():
        if key not in request or _canonical_json(request[key]) != _canonical_json(value):
            raise HTTPException(status_code=400, detail="quote does not match the request")
    if request.get("execute") is not False:
        raise HTTPException(status_code=400, detail="quotes do not execute")
    if request.get("base64EncodedTx") not in (None, False):
        raise HTTPException(status_code=400, detail="quotes do not execute")
    taker = request.get("taker")
    if taker is not None and taker != wallet:
        raise HTTPException(status_code=400, detail="quote wallet does not match wallet_addresses")


def _reject_tree(value) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "").replace("_", "")
            if normalized in _FORBIDDEN:
                raise HTTPException(status_code=400, detail="execution fields are refused")
            _reject_tree(item)
    elif isinstance(value, list):
        for item in value:
            _reject_tree(item)


def _solana_wallet(payload: dict) -> str:
    _exact(payload, _WALLET_KEYS)
    rows = payload.get("addresses")
    if not isinstance(rows, list) or len(rows) != 1:
        raise HTTPException(status_code=400, detail="wallet_addresses must contain one Solana address")
    _exact(rows[0], _ADDRESS_KEYS)
    if rows[0].get("addressType") != "solana":
        raise HTTPException(status_code=400, detail="wallet_addresses must contain one Solana address")
    address = rows[0].get("address")
    if not isinstance(address, str) or _MINT.fullmatch(address) is None:
        raise HTTPException(status_code=400, detail="wallet_addresses must contain one Solana address")
    return address


def _allow(body: dict, allowed: set[str]) -> None:
    if not isinstance(body, dict) or set(body) - allowed:
        raise HTTPException(status_code=400, detail="unexpected field")


def _exact(body: dict, allowed: set[str]) -> None:
    if not isinstance(body, dict) or set(body) != allowed:
        raise HTTPException(status_code=400, detail="unexpected field")


def _canonical_json(value) -> str:
    return json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"))


def _jsonable(value):
    if isinstance(value, bool) or value is None or isinstance(value, str) or isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise HTTPException(status_code=400, detail="unexpected field")
        return _json_number(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    raise HTTPException(status_code=400, detail="unexpected field")
