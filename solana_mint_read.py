"""SPL mint-account reader. Mint and freeze authority only.

The base Mint account is 82 bytes: a 4-byte option, a 32-byte authority, an
8-byte supply, decimals, the initialized flag, then the freeze option. An
option of 0 means that authority is absent. This does not identify the
developer, a burned LP token, a bundle, or a rug. The network stays closed
unless the caller passes a transport. getAccountInfo is the only method.
"""

from __future__ import annotations

import base64

import solana_tx

MINT_LEN = 82
SOURCE = "solana_rpc_mint_account"
NOT_PROVEN = (
    "lp_burned_or_locked",
    "dev_holding_pct",
    "dev_rugs",
    "bundled_launch_pct",
    "unique_buyers",
    "smart_wallets",
    "holder_growth",
)


def parse_account(value: dict, mint: str) -> dict:
    refused = {"ok": False, "source": SOURCE, "mint": mint, "not_proven": list(NOT_PROVEN), "send": False}
    if not isinstance(value, dict):
        return refused | {"reason": "account_missing"}
    owner = value.get("owner")
    if owner not in {solana_tx.TOKEN_PROGRAM, solana_tx.TOKEN_2022}:
        return refused | {"reason": "mint_owner"}
    data = value.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], str) or data[1:] not in ([], ["base64"]):
        return refused | {"reason": "mint_data"}
    try:
        raw = base64.b64decode(data[0], validate=True)
    except Exception:
        return refused | {"reason": "mint_data"}
    if len(raw) < MINT_LEN or raw[45] != 1:
        return refused | {"reason": "mint_layout"}
    mint_option = int.from_bytes(raw[0:4], "little")
    freeze_option = int.from_bytes(raw[46:50], "little")
    if mint_option not in {0, 1} or freeze_option not in {0, 1}:
        return refused | {"reason": "mint_layout"}
    return {
        "ok": True,
        "reason": None,
        "source": SOURCE,
        "mint": mint,
        "owner": owner,
        "mint_authority_revoked": mint_option == 0,
        "freeze_authority_revoked": freeze_option == 0,
        "not_proven": list(NOT_PROVEN),
        "send": False,
        "live_ready": False,
    }
