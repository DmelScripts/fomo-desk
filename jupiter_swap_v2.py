"""Jupiter Swap V2 assessment. It does not enable a buy or an exit.

Official sources, read for this batch:

- https://developers.jup.ag/docs/swap
  GET /swap/v2/order without taker is a keyless quote and returns
  transaction null. GET /swap/v2/build returns raw instructions. /execute
  and /submit land a signed transaction and are not called.
- https://developers.jup.ag/docs/api-reference/swap/v1/quote
  otherAmountThreshold is "Not used by /swap endpoint to build transaction".
  routePlan feeAmount and feeMint are deprecated.
- https://developers.jup.ag/docs/api-reference/swap/build
  Swap V2 SwapInfo publishes ammKey, label, mints, and amounts. It does not
  publish the swapInstruction byte layout, the hop minimum, or a DEX fee.
  The compute-budget instructions do not include a compute-unit limit.
- https://developers.jup.ag/docs/swap/migration/metis-to-build
  V2 instructions do not emit fee events.

A JSON minimum is therefore not the enforced hop minimum. route_v2 stays
disabled. The SOL/USDC diagnostic is not this buy. No API key is attached.
"""

from __future__ import annotations

import base64

import phantom_transport
import solana_jupiter
import solana_tx

ORDER_URL = "https://api.jup.ag/swap/v2/order"
BUILD_URL = "https://api.jup.ag/swap/v2/build"
PORTAL = "https://developers.jup.ag/portal"
V1_QUOTE = "https://developers.jup.ag/docs/api-reference/swap/v1/quote"
V2_BUILD = "https://developers.jup.ag/docs/api-reference/swap/build"


def order_url(input_mint: str, output_mint: str, amount: str) -> str:
    """Keyless quote. No taker, so the documented response has no transaction."""
    return f"{ORDER_URL}?inputMint={input_mint}&outputMint={output_mint}&amount={amount}"


def build_url(input_mint: str, output_mint: str, amount: str) -> str:
    """Documented instruction build. The taker is the server wallet. Nothing is signed."""
    return (
        f"{BUILD_URL}?inputMint={input_mint}&outputMint={output_mint}"
        f"&amount={amount}&taker={phantom_transport.REQUIRED_WALLET}&slippageBps=50"
    )


def assess_order(body: dict) -> dict:
    base = _refused("order")
    if not isinstance(body, dict):
        return base | {"reason": "order_shape"}
    base.update({
        "router": body.get("router") if isinstance(body.get("router"), str) else None,
        "in_amount": body.get("inAmount") if isinstance(body.get("inAmount"), str) else None,
        "out_amount": body.get("outAmount") if isinstance(body.get("outAmount"), str) else None,
        "fee_bps": body.get("feeBps") if isinstance(body.get("feeBps"), int) and not isinstance(body.get("feeBps"), bool) else None,
        "transaction_present": bool(body.get("transaction")),
        "reason": "transaction_not_decodable" if body.get("transaction") else "quote_is_not_an_instruction",
    })
    return base


def assess_build(body: dict) -> dict:
    """Look at the instruction. Do not treat the JSON minimum as enforced."""
    base = _refused("build")
    if not isinstance(body, dict):
        return base | {"reason": "build_shape"}
    threshold = body.get("otherAmountThreshold")
    plan = body.get("routePlan") if isinstance(body.get("routePlan"), list) else []
    fee_fields = []
    for step in plan:
        info = step.get("swapInfo") if isinstance(step, dict) and isinstance(step.get("swapInfo"), dict) else {}
        if "feeAmount" in info or "feeMint" in info:
            fee_fields.append(info.get("label"))
    instruction = body.get("swapInstruction") if isinstance(body.get("swapInstruction"), dict) else {}
    program = instruction.get("programId") if isinstance(instruction.get("programId"), str) else None
    accounts = instruction.get("accounts") if isinstance(instruction.get("accounts"), list) else []
    raw = b""
    if isinstance(instruction.get("data"), str):
        try:
            raw = base64.b64decode(instruction["data"], validate=True)
        except Exception:
            raw = b""
    discriminator = raw[:8].hex() if len(raw) >= 8 else None
    matches_route_v2 = raw[:8] == solana_jupiter.ROUTE_V2 if len(raw) >= 8 else False
    import jupiter_route_v2
    parsed = jupiter_route_v2.parse(raw) if raw else {"min_out_proven": False}
    quoted = str(parsed["quoted_out_amount"]) if parsed.get("quoted_out_amount") is not None else None
    base["min_out_proven"] = parsed.get("min_out_proven") is True
    base["instruction_min_out"] = None if parsed.get("min_out") is None else str(parsed["min_out"])
    base.update({
        "program_id": program,
        "program_is_jup6": program == solana_jupiter.PROGRAM_ID,
        "account_count": len(accounts),
        "data_len": len(raw),
        "discriminator_hex": discriminator,
        "matches_route_v2": matches_route_v2,
        "json_other_amount_threshold": threshold if isinstance(threshold, str) else None,
        "instruction_quoted_out": quoted,
        "json_threshold_equals_instruction_min": parsed.get("min_out") is not None and str(parsed["min_out"]) == threshold,
        "route_labels": [step.get("swapInfo", {}).get("label") for step in plan if isinstance(step, dict) and isinstance(step.get("swapInfo"), dict)],
        "deprecated_fee_fields_present": fee_fields,
        "compute_unit_limit_published": False,
        "reason": parsed.get("reason") or ("hop_accounts_unpublished" if parsed.get("min_out_proven") else "route_v2_bytes"),
    })
    return base


def _refused(kind: str) -> dict:
    return {
        "ok": False,
        "kind": kind,
        "execution_allowed": False,
        "send": False,
        "sign": False,
        "resend": False,
        "live_ready": False,
        "signer_enabled": False,
        "min_out_proven": False,
        "hop_accounts_proven": False,
        "dex_fee_proven": False,
        "network_fee_proven": False,
        "combined_fee_usd_proven": False,
        "buy_proven": False,
        "exit_proven": False,
        "diagnostic_sol_usdc_is_not_this_buy": True,
        "mobile_phantom_is_server_auth": False,
        "api_key_attached": False,
        "wallet": phantom_transport.REQUIRED_WALLET,
        "wallet_id": phantom_transport.REQUIRED_WALLET_ID,
        "sources": [V1_QUOTE, V2_BUILD],
        "portal": PORTAL,
        "input_mint_native": solana_tx.NATIVE_MINT,
    }
