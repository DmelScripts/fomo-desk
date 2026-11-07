"""Offline record of the verified Phantom prepare and simulate commands.

`phantom buy --execute false --base64-encoded-tx true` can return transaction
bytes without signing. `phantom solana send` without confirmed, and
`phantom simulate`, can preview a transaction without broadcasting.
Neither command returns lastValidBlockHeight. No documented Phantom command
reads signature status after a send. Signing stays unimplemented.
"""

from __future__ import annotations

_FORBIDDEN = {
    "transaction", "transactiondata", "rawtransaction", "signature",
    "signedtransaction", "session", "secret",
}


def status() -> dict:
    return {
        "prepare_without_broadcast": "phantom buy --execute false --base64-encoded-tx true",
        "simulate_without_broadcast": "phantom solana send --confirmed false",
        "alternate_simulate": "phantom simulate",
        "last_valid_block_height": "not_in_buy_output",
        "signature_status_command": "absent",
        "signing": "unimplemented",
        "send": False,
        "live_ready": False,
        "executable": False,
        "held_quote_executable": False,
    }


def note_inspection(body: dict) -> dict:
    """Record a fake inspection. Raw transaction material is refused."""
    if not isinstance(body, dict):
        raise ValueError("inspection is not usable")
    folded = {str(key).lower().replace("-", "").replace("_", "") for key in body}
    if folded & _FORBIDDEN:
        raise ValueError("transaction material is not stored")
    blockhash = body.get("blockhash")
    height = body.get("last_valid_block_height")
    result = status()
    result["blockhash_present"] = isinstance(blockhash, str) and 32 <= len(blockhash) <= 88
    result["last_valid_block_height_present"] = isinstance(height, int) and not isinstance(height, bool)
    result["chain_validity"] = "height_present" if result["last_valid_block_height_present"] else "blockhash_only"
    result["wallet_bound"] = body.get("wallet_bound") is True
    result["simulated"] = body.get("simulated") == "pending_confirmation"
    result["send"] = False
    result["executable"] = False
    return result
