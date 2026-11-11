"""Unsigned simulateTransaction of the exact reserved swap, before any signature.

The live POST target is ``rpc_url``. The default is Helius mainnet, which
needs ``HELIUS_API_KEY``. The key is not copied into a log or a result. An
override URL is used as stored. This module does not open a socket: the desk
transport sends the JSON-RPC body. Sign and send stay mocked.
"""

from __future__ import annotations

from decimal import Decimal

import solana_rpc

COMMITMENT = "processed"


def simulation_endpoint() -> dict:
    """Which RPC a live simulate would post to. Booleans and the host only."""
    return solana_rpc.describe()


def rpc_body(transaction: str, output_account: str) -> dict:
    """The simulateTransaction body. The transaction bytes are not rewritten."""
    options = {
        "encoding": "base64",
        "sigVerify": False,
        "replaceRecentBlockhash": True,
        "commitment": COMMITMENT,
        "innerInstructions": True,
        "accounts": {"encoding": "jsonParsed", "addresses": [output_account]},
    }
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "simulateTransaction",
        "params": [transaction, options],
    }


def assess(value: dict, *, output_mint: str, quoted_out: int, sol: dict, dex_fee_usd: Decimal, outflow_lamports: int, reserved_usd: Decimal | None, cu_price: int) -> dict:
    """Fail closed on the simulated fee, output, and ticket. No network."""
    import jupiter_route_v2
    import memecoin_desk
    import pyth_sponsored
    if not isinstance(value, dict) or "err" not in value:
        return _fail("simulation_malformed")
    if value.get("err") is not None:
        return _fail("simulation_err")
    fee_lamports = _simulated_fee(value, cu_price)
    if fee_lamports is None:
        return _fail("simulation_fee_unproven")
    network = pyth_sponsored.lamports_usd(fee_lamports, sol)
    if network is None:
        return _fail("simulation_fee_unproven")
    combined = dex_fee_usd + network
    if combined > memecoin_desk.MAX_FEE:
        return _fail("simulated_fee_above_0.05", fee_lamports=fee_lamports, combined_fee_usd=format(combined, "f"))
    minimum = jupiter_route_v2.minimum_out(quoted_out, 50)
    if minimum is None:
        return _fail("simulated_output_unproven")
    output = _output_delta(value, output_mint)
    if output is None:
        return _fail("simulated_output_unproven", minimum_out=minimum)
    if output < minimum:
        return _fail("simulated_output_below_min", output_amount=output, minimum_out=minimum)
    deposit = _new_account_lamports(value)
    if deposit is None:
        return _fail("simulation_malformed")
    deposit_usd = pyth_sponsored.lamports_usd(deposit, sol) if deposit else Decimal("0.00")
    spent = pyth_sponsored.lamports_usd(outflow_lamports, sol)
    if deposit_usd is None or spent is None:
        return _fail("simulated_ticket_unproven")
    ticket = spent if reserved_usd is None or spent > reserved_usd else reserved_usd
    ticket += deposit_usd
    if ticket > memecoin_desk.TICKET:
        return _fail("simulated_ticket", ticket_usd=format(ticket, "f"), deposit_lamports=deposit)
    return {
        "ok": True,
        "cause": None,
        "fee_lamports": fee_lamports,
        "combined_fee_usd": format(combined, "f"),
        "output_amount": output,
        "minimum_out": minimum,
        "deposit_lamports": deposit,
        "ticket_usd": format(ticket, "f"),
        "units_consumed": value.get("unitsConsumed") if type(value.get("unitsConsumed")) is int else None,
    }


def _fail(cause: str, **extra) -> dict:
    return {"ok": False, "cause": cause, "sign": False, "send": False} | extra


def _simulated_fee(value: dict, cu_price: int) -> int | None:
    import jupiter_route_v2
    found = []
    fee = value.get("fee")
    if type(fee) is int and fee >= 0:
        found.append(fee)
    units = value.get("unitsConsumed")
    if type(units) is int and units >= 0 and type(cu_price) is int and cu_price >= 0:
        found.append(jupiter_route_v2.BASE_FEE_LAMPORTS + (units * cu_price + 999_999) // 1_000_000)
    if not found:
        return None
    return max(found)


def _output_delta(value: dict, mint: str) -> int | None:
    pre = _token_map(value.get("preTokenBalances"))
    post = _token_map(value.get("postTokenBalances"))
    if pre is None or post is None:
        return _parsed_account_amount(value.get("accounts"), mint)
    if mint not in post and mint not in pre:
        return _parsed_account_amount(value.get("accounts"), mint)
    return post.get(mint, 0) - pre.get(mint, 0)


def _token_map(rows) -> dict | None:
    if rows is None:
        return {}
    if not isinstance(rows, list):
        return None
    found = {}
    for row in rows:
        if not isinstance(row, dict):
            return None
        name = row.get("mint")
        ui = row.get("uiTokenAmount")
        if type(name) is not str or not isinstance(ui, dict):
            return None
        amount = ui.get("amount")
        if type(amount) is not str or amount == "" or not amount.isdigit():
            return None
        found[name] = found.get(name, 0) + int(amount)
    return found


def _parsed_account_amount(accounts, mint: str) -> int | None:
    if not isinstance(accounts, list):
        return None
    found = None
    for account in accounts:
        if not isinstance(account, dict):
            continue
        data = account.get("data")
        parsed = data.get("parsed") if isinstance(data, dict) else None
        info = parsed.get("info") if isinstance(parsed, dict) else None
        if not isinstance(info, dict) or info.get("mint") != mint:
            continue
        token = info.get("tokenAmount")
        amount = token.get("amount") if isinstance(token, dict) else None
        if type(amount) is not str or not amount.isdigit():
            return None
        found = int(amount) if found is None else found + int(amount)
    return found


def _new_account_lamports(value: dict) -> int | None:
    pre = value.get("preBalances")
    post = value.get("postBalances")
    if pre is None and post is None:
        return 0
    if not isinstance(pre, list) or not isinstance(post, list) or len(pre) != len(post):
        return None
    total = 0
    for index, (before, after) in enumerate(zip(pre, post)):
        if type(before) is not int or type(after) is not int or before < 0 or after < 0:
            return None
        if index == 0:
            continue
        if before == 0 and after > 0:
            total += after
    return total
