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


def assess(value: dict, *, output_mint: str, owner: str, account_count: int, quoted_out: int, sol: dict, dex_fee_usd: Decimal, outflow_lamports: int, reserved_usd: Decimal | None, cu_price: int, cu_limit: int | None, mint_decimals: int | None) -> dict:
    """Fail closed on the simulated fee, output, and ticket. No network."""
    import jupiter_route_v2
    import memecoin_desk
    import pyth_sponsored
    if not isinstance(value, dict) or "err" not in value:
        return _fail("simulation_malformed")
    if value.get("err") is not None:
        return _fail("simulation_err")
    fee_lamports = _simulated_fee(value, cu_price, cu_limit)
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
    output = _output_delta(value, output_mint, owner, mint_decimals)
    if output is None:
        return _fail("simulated_output_unproven", minimum_out=minimum)
    if output < minimum:
        return _fail("simulated_output_below_min", output_amount=output, minimum_out=minimum)
    deposit = _new_account_lamports(value, account_count)
    if deposit is None:
        return _fail("simulation_ticket_unproven")
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


def _simulated_fee(value: dict, cu_price: int, cu_limit: int | None) -> int | None:
    """Network lamports charged against the $0.05 cap.

    A compute-unit price uses the larger of unitsConsumed and the transaction's
    compute-unit limit, so a missing consumption figure still prices the full
    requested priority. A present unitsConsumed that is not an exact int of 0
    or more, including bool, is unproven. The RPC fee field is kept when it is
    higher.
    """
    import jupiter_route_v2
    if type(cu_price) is not int or cu_price < 0:
        return None
    if type(cu_limit) is not int or cu_limit < 0:
        cu_limit = None
    units = None
    if cu_price > 0 and "unitsConsumed" in value:
        raw = value.get("unitsConsumed")
        if type(raw) is not int or raw < 0:
            return None
        units = raw
    bound = None
    if cu_price > 0:
        if units is None and cu_limit is None:
            return None
        bound = cu_limit if units is None else units if cu_limit is None else max(units, cu_limit)
    found = []
    fee = value.get("fee")
    if type(fee) is int and fee >= 0:
        found.append(fee)
    if bound is not None:
        found.append(jupiter_route_v2.BASE_FEE_LAMPORTS + (bound * cu_price + 999_999) // 1_000_000)
    if not found:
        return None
    return max(found)


def _output_delta(value: dict, mint: str, owner: str, mint_decimals: int | None) -> int | None:
    """Post minus pre for this owner and mint. Another owner does not count."""
    if not isinstance(value.get("preTokenBalances"), list) or not isinstance(value.get("postTokenBalances"), list):
        return None
    pre = _owned_amounts(value.get("preTokenBalances"), mint, owner)
    post = _owned_amounts(value.get("postTokenBalances"), mint, owner)
    if pre is None or post is None or (not pre and not post):
        return None
    decimals = {item[1] for item in pre + post}
    if len(decimals) != 1:
        return None
    if mint_decimals is not None and decimals.pop() != mint_decimals:
        return None
    return sum(item[0] for item in post) - sum(item[0] for item in pre)


def _owned_amounts(rows, mint: str, owner: str) -> list | None:
    found = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        if row.get("mint") != mint or row.get("owner") != owner:
            continue
        ui = row.get("uiTokenAmount")
        if not isinstance(ui, dict):
            return None
        amount = ui.get("amount")
        decimals = ui.get("decimals")
        if type(amount) is not str or amount == "" or not amount.isdigit():
            return None
        if type(decimals) is not int or isinstance(decimals, bool) or decimals < 0:
            return None
        found.append((int(amount), decimals))
    return found


def _new_account_lamports(value: dict, account_count: int) -> int | None:
    """Lamports that appear in a new account. Both balance arrays are required."""
    pre = value.get("preBalances")
    post = value.get("postBalances")
    if type(account_count) is not int or isinstance(account_count, bool) or account_count < 1:
        return None
    if not isinstance(pre, list) or not isinstance(post, list):
        return None
    if len(pre) != account_count or len(post) != account_count:
        return None
    total = 0
    for index, (before, after) in enumerate(zip(pre, post)):
        if type(before) is not int or type(after) is not int or isinstance(before, bool) or isinstance(after, bool):
            return None
        if before < 0 or after < 0:
            return None
        if index == 0:
            continue
        if before == 0 and after > 0:
            total += after
    return total
