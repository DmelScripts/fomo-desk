"""Server-owned buy and exit for a scanner mint.

The classic SOL/USDC Whirlpool path is a diagnostic hedge, not this buy.
Jupiter route_v2 is not a strict decoder and is not enabled. A mint this
module cannot decode fails closed. No runner is started.
"""

from __future__ import annotations

import phantom_transport

SOL_USDC_POOL = "7qbRF6YsyGuLUVs6Y1q64bdVrfe4ZcUUz1JRdoVNUJnm"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
NATIVE = "So11111111111111111111111111111111111111112"
DIAGNOSTIC = {SOL_USDC_POOL, USDC, NATIVE}

# There is no documented strict buy decoder for a scanner mint in this process.
DECODER = None

REMAINING = (
    "route_v2 Whirlpool is decoded from the JUP6 IDL. The minimum is quoted_out_amount minus the instruction slippage. Other hops do not publish remaining accounts. A build without SetComputeUnitLimit cannot price the network fee.",
    "A Jupiter API key from https://developers.jup.ag/portal raises the keyless 0.5 rps limit. This process does not attach a key. The key is not a signer.",
    "PYTH_API_KEY for the documented Hermes USDC/USD latest-price route. This process does not call Hermes, and a test quote is not that credential.",
    "solana_read_enabled and rpc_url https://api.mainnet-beta.solana.com for accounts a proven decoder names. A mint account proves mint and freeze authority only.",
    "Developer holding, past rugs, and launch bundles are not on the public mint account or DexScreener. The credentialed StalkChain reads were not called.",
    "A Phantom session for wallet 7AFJbqsX2UeK7ZW5M1gbXWjiKCai2CJWHpCzXe7woC7t, id eba28e4f-6b99-4543-af72-c7d1b008ce4e. A phone connection to a different wallet is not this session.",
)


def supported_buy(mint: str) -> dict:
    """Refuse a token buy that cannot be strictly decoded. This does not send."""
    return _decision(mint, "buy")


def supported_exit(mint: str) -> dict:
    """Refuse a token exit that cannot be strictly decoded. The loss stop is not the reason."""
    body = _decision(mint, "exit")
    body["loss_stop_blocks_exit"] = False
    return body


def _decision(mint: str, side: str) -> dict:
    reason = "unsupported_buy_route" if side == "buy" else "unsupported_exit_route"
    if not isinstance(mint, str) or not mint:
        reason = "mint"
    elif mint in DIAGNOSTIC:
        reason = "diagnostic_is_not_a_token_buy"
    return {
        "ok": False,
        "reason": reason,
        "side": side,
        "mint": mint if isinstance(mint, str) else None,
        "send": False,
        "sign": False,
        "resend": False,
        "live_ready": False,
        "signer_enabled": False,
        "effectful": False,
        "decoder": DECODER,
        "jupiter_route_v2": "not_enabled",
        "jupiter_swap_v2": "refused",
        "mobile_phantom_is_server_auth": False,
        "diagnostic_sol_usdc_is_not_this_buy": True,
        "wallet": phantom_transport.REQUIRED_WALLET,
        "wallet_id": phantom_transport.REQUIRED_WALLET_ID,
        "allocation_usd": "100.00",
        "ticket_usd": "8.00",
        "max_fee_usd": "0.05",
        "max_loss_usd": "20.00",
        "loss_stops": "new_entries",
        "exit_rules": "6/20/12/3h unless 5",
        "remaining": list(REMAINING),
    }
