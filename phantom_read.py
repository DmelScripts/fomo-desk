"""Read-only Phantom checks. They do not sign, send, or change the allowlist.

Balance uses `phantom wallet balances --networks solana --format json`.
A quote uses `phantom buy` with `--execute false` and `--base64-encoded-tx false`.
Neither call is made unless an authenticated route asks for it.
"""

from __future__ import annotations

import logging
import os
import subprocess

import phantom_auth
import phantom_transport

log = logging.getLogger("phantom_read")

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6
SELL_AMOUNT = "10000000"
_MAINNET = {"solana:101", "solana:mainnet", "solana:mainnet-beta"}
_DROP = {
    "transactiondata", "rawtransaction", "signature", "execution", "signedtransaction",
    "session", "stamper", "privatekey", "secret", "clientsecret", "refreshtoken",
    "accesstoken", "logouri",
}
_EXPIRY = {"expiry", "expiresAt", "expires_at", "validUntil", "valid_until"}


def balance() -> dict:
    text, code = _run(["wallet", "balances", "--networks", "solana", "--format", "json"])
    return _balance(text, code)


def quote() -> dict:
    text, code = _run([
        "buy",
        "--sell-chain-id", "solana:mainnet",
        "--buy-chain-id", "solana:mainnet",
        "--sell-token-is-native", "true",
        "--buy-token-mint", USDC,
        "--amount", SELL_AMOUNT,
        "--amount-unit", "base",
        "--slippage-tolerance", "1",
        "--exact-out", "false",
        "--auto-slippage", "false",
        "--base64-encoded-tx", "false",
        "--execute", "false",
        "--taker", phantom_transport.REQUIRED_WALLET,
        "--wallet-id", phantom_transport.REQUIRED_WALLET_ID,
        "--format", "json",
    ])
    return _quote(text, code)


def _run(args: list[str]) -> tuple[str, int]:
    if "--execute" in args and args[args.index("--execute") + 1] != "false":
        raise RuntimeError("phantom package is not available")
    binary = phantom_auth._binary()
    home = phantom_auth._home()
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    try:
        done = subprocess.run(
            [binary, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=phantom_auth._child_env(home, binary),
            cwd=str(home),
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        log.info("phantom read %s timeout", args[0])
        return "", 1
    text = (done.stdout + done.stderr).decode("utf-8", "replace")[: phantom_auth.MAX_CAPTURE]
    log.info("phantom read %s %s", args[0], "ok" if done.returncode == 0 else "error")
    return text, done.returncode


def _balance(text: str, code: int) -> dict:
    base = _base()
    if phantom_auth._auth_text(text):
        base["status"] = "auth_error"
        return base
    if code != 0:
        base["status"] = "unable_to_verify"
        return base
    body, shape = phantom_auth._payload(text)
    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
        base["status"] = "unable_to_verify"
        base["verify"] = {"error_category": "unable_to_verify", "shape": shape, "fields": {"items": "missing"}}
        return base
    own = []
    other = False
    for item in body["items"]:
        if not isinstance(item, dict):
            continue
        cleaned, _omitted = _strip(item)
        if cleaned.get("spamStatus") == "SPAM":
            continue
        addresses = []
        for row in cleaned.get("queriedWalletBalances") or []:
            if isinstance(row, dict) and isinstance(row.get("address"), str):
                addresses.append(row["address"])
        matched = next(
            (
                row.get("quantityString")
                for row in (cleaned.get("queriedWalletBalances") or [])
                if isinstance(row, dict) and row.get("address") == phantom_transport.REQUIRED_WALLET
            ),
            None,
        )
        if isinstance(matched, str) and matched:
            own.append({
                "symbol": cleaned.get("symbol") if isinstance(cleaned.get("symbol"), str) else None,
                "total_quantity": matched[:40],
                "spam_status": cleaned.get("spamStatus") if isinstance(cleaned.get("spamStatus"), str) else None,
            })
        elif addresses:
            other = True
    if not own and other:
        base["status"] = "mismatch"
        return base
    if not own:
        base["status"] = "unable_to_verify"
        base["verify"] = {"error_category": "unable_to_verify", "shape": shape, "fields": {"items": "array", "solana": "missing"}}
        return base
    base["status"] = "observed"
    base["items"] = own[:20]
    return base


def _quote(text: str, code: int) -> dict:
    base = _base()
    base["executable"] = False
    base["usable"] = False
    if phantom_auth._auth_text(text):
        base["status"] = "auth_error"
        return base
    if code != 0:
        base["status"] = "unable_to_verify"
        return base
    body, shape = phantom_auth._payload(text)
    if not isinstance(body, dict):
        base["status"] = "unable_to_verify"
        base["verify"] = {"error_category": "unable_to_verify", "shape": shape, "fields": {"quoteRequest": "missing"}}
        return base
    if _has_material(body):
        base["status"] = "blocked"
        base["transaction_material_omitted"] = True
        return base
    cleaned, omitted = _strip(body)
    request = cleaned.get("quoteRequest") if isinstance(cleaned.get("quoteRequest"), dict) else {}
    response = cleaned.get("quoteResponse") if isinstance(cleaned.get("quoteResponse"), dict) else {}
    if request.get("execute") is True or request.get("base64EncodedTx") is True:
        base["status"] = "blocked"
        base["transaction_material_omitted"] = True
        return base
    wallet_id = request.get("walletId")
    if isinstance(wallet_id, str) and wallet_id != phantom_transport.REQUIRED_WALLET_ID:
        base["status"] = "mismatch"
        return base
    taker = _taker(request) or _taker(response)
    if taker and taker != phantom_transport.REQUIRED_WALLET:
        base["status"] = "mismatch"
        base["observed_wallet"] = taker
        return base
    if taker is None:
        base["status"] = "unable_to_verify"
        base["verify"] = {"error_category": "unable_to_verify", "shape": shape, "fields": {"taker": "missing"}}
        return base
    response_taker = _taker(response)
    if response_taker and response_taker != phantom_transport.REQUIRED_WALLET:
        base["status"] = "mismatch"
        base["observed_wallet"] = response_taker
        return base
    bound, decimals = _bound_quote(request, response)
    if not bound:
        base["status"] = "unable_to_verify"
        base["verify"] = {"error_category": "unable_to_verify", "shape": shape, "fields": {"binding": "rejected"}}
        return base
    expiry = _find_expiry(response)
    quote_id = _find_quote_id(cleaned.get("quoteResponse"))
    base["status"] = "observed"
    base["provider_expiry_unknown"] = expiry is None
    base["provider_expiry"] = expiry
    base["quote_id"] = quote_id
    base["transaction_material_omitted"] = omitted
    base["provider"] = "phantom"
    base["chain"] = "solana:101"
    base["sell"] = "native"
    base["buy_mint"] = USDC
    base["sell_amount"] = SELL_AMOUNT
    base["buy_decimals"] = decimals
    return base


def _base() -> dict:
    return {
        "send": False,
        "executable": False,
        "live_ready": False,
        "usable": False,
        "wallet_id": phantom_transport.REQUIRED_WALLET_ID,
        "wallet": phantom_transport.REQUIRED_WALLET,
    }


def _has_material(value) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            folded = str(key).lower().replace("-", "").replace("_", "")
            if folded in {"signature", "execution", "signedtransaction"}:
                return True
            if _has_material(item):
                return True
    elif isinstance(value, list):
        return any(_has_material(item) for item in value)
    return False


def _strip(value):
    omitted = False
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            if str(key).lower().replace("-", "").replace("_", "") in _DROP:
                omitted = True
                continue
            child, child_omitted = _strip(item)
            omitted = omitted or child_omitted
            clean[key] = child
        return clean, omitted
    if isinstance(value, list):
        rows = []
        for item in value:
            child, child_omitted = _strip(item)
            omitted = omitted or child_omitted
            rows.append(child)
        return rows, omitted
    return value, False


def _mainnet(chain) -> bool:
    return isinstance(chain, str) and chain.lower() in _MAINNET


def _native_sol(token) -> bool:
    return (
        isinstance(token, dict)
        and token.get("resourceType") == "nativeToken"
        and str(token.get("slip44")) == "501"
        and _mainnet(token.get("chainId"))
    )


def _usdc(token):
    if not isinstance(token, dict) or token.get("address") != USDC or not _mainnet(token.get("chainId")):
        return False, None
    decimals = token.get("decimals")
    if decimals is None:
        return True, None
    if isinstance(decimals, bool) or decimals != USDC_DECIMALS:
        return False, None
    return True, USDC_DECIMALS


def _amount(value) -> bool:
    return value == SELL_AMOUNT or (isinstance(value, int) and not isinstance(value, bool) and value == int(SELL_AMOUNT))


def _bound_quote(request: dict, response: dict):
    buy_ok, decimals = _usdc(request.get("buyToken"))
    if not buy_ok or not _native_sol(request.get("sellToken")) or not _amount(request.get("sellAmount")):
        return False, None
    response_buy = response.get("buyToken")
    if response_buy is not None:
        response_ok, response_decimals = _usdc(response_buy)
        if not response_ok:
            return False, None
        if response_decimals is not None:
            decimals = response_decimals
    quoted = response.get("quotes")
    if isinstance(quoted, list) and quoted and isinstance(quoted[0], dict) and "sellAmount" in quoted[0]:
        if not _amount(quoted[0].get("sellAmount")):
            return False, None
    return True, decimals


def _taker(request) -> str | None:
    if not isinstance(request, dict):
        return None
    taker = request.get("taker")
    if isinstance(taker, str) and phantom_auth._valid_solana(taker):
        return taker
    if isinstance(taker, dict) and phantom_auth._valid_solana(taker.get("address")):
        return taker["address"]
    return None


def _find_expiry(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _EXPIRY and isinstance(item, (int, float)) and not isinstance(item, bool):
                return item
            found = _find_expiry(item)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_expiry(item)
            if found is not None:
                return found
    return None


def _find_quote_id(value):
    if isinstance(value, dict):
        quote_id = value.get("quoteId")
        if isinstance(quote_id, str) and quote_id and len(quote_id) <= 80:
            return quote_id
        for item in value.values():
            found = _find_quote_id(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_quote_id(item)
            if found:
                return found
    return None
