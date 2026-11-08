"""One server-owned pass of the six-role desk. It is not armed and it does not send.

Scout reads DexScreener's documented token-pairs response. Filter reads the
mint account from the public RPC. LP lock expiry, rug history, holder growth,
bundles, unique buyers, and smart wallets come only from the documented
StalkChain tool route. With no owner key, those facts stay unproven and the
case stops. Caller booleans are not accepted here.

Hermes latest prices require a Pyth API key since 26 August 2026. The keyless
feed list is metadata, not a price, and this module does not call it.
https://docs.pyth.network/llms-price-feeds-core.txt
https://docs.pyth.network/api-reference/hermes/price-feeds
"""

from __future__ import annotations

import os

import dexscreener_public
import memecoin_desk
import solana_mint_read

STALK_URL = "https://data.stalkchain.com/api/v1/tools/"
STALK_ENV = "STALKCHAIN_API_KEY"
UNPROVEN = (
    "lp_burned_or_locked",
    "lp_lock_expiry",
    "dev_holding_pct",
    "dev_rugs",
    "bundled_launch_pct",
    "holder_growth",
    "unique_buyers",
    "smart_wallets",
)


def stalk_coverage() -> dict:
    """The adapter. A missing key does not become a clean developer or a burned LP."""
    key = os.environ.get(STALK_ENV)
    present = isinstance(key, str) and bool(key.strip())
    if not present:
        return {
            "ok": False,
            "called": False,
            "reason": "stalkchain_credentials_missing",
            "unproven": list(UNPROVEN),
            "url": STALK_URL,
            "credential_env": STALK_ENV,
            "live_ready": False,
        }
    return {
        "ok": False,
        "called": False,
        "reason": "stalkchain_not_invoked",
        "unproven": list(UNPROVEN),
        "url": STALK_URL,
        "credential_env": STALK_ENV,
        "live_ready": False,
    }


def run_once(mint: str, transport, now: float) -> dict:
    """Fetch, then hand off. No loop is started and no flag is turned on."""
    stopped = {"ok": False, "send": False, "sign": False, "live_ready": False, "armed": False, "mint": mint}
    if transport is None:
        return stopped | {"reason": "network_disabled", "stage": "scout"}
    try:
        response = transport({"kind": "http", "url": dexscreener_public.pairs_url(mint)})
    except Exception:
        return stopped | {"reason": "dexscreener_transport", "stage": "scout"}
    if not isinstance(response, dict) or response.get("status") != 200 or not isinstance(response.get("body"), list):
        return stopped | {"reason": "dexscreener_status", "stage": "scout"}
    parsed = dexscreener_public.parse_pairs(response["body"], mint, now)
    if parsed.get("ok") is not True:
        memecoin_desk.scout({"mint": mint, "source": "other"}, now=now)
        return stopped | {"reason": parsed.get("reason"), "stage": "scout", "fdv_used_as_market_cap": False}
    opened = memecoin_desk.scout(parsed["scout"], now=now)
    if opened.get("ok") is not True:
        return stopped | {"reason": opened.get("reason"), "stage": opened.get("stage"), "case_id": opened.get("case_id")}
    try:
        rpc = transport({"kind": "rpc", "method": "getAccountInfo", "mint": mint})
    except Exception:
        return stopped | {"reason": "mint_transport", "stage": "filter", "case_id": opened["case_id"]}
    account = {"ok": False, "reason": "account_missing"}
    if isinstance(rpc, dict) and rpc.get("status") == 200 and isinstance(rpc.get("body"), dict):
        value = (rpc["body"].get("result") or {}).get("value") if isinstance(rpc["body"].get("result"), dict) else None
        if isinstance(value, dict):
            account = solana_mint_read.parse_account(value, mint)
    coverage = stalk_coverage()
    onchain = {"source": solana_mint_read.SOURCE, "fetched_at": now}
    if account.get("ok") is True:
        onchain["mint_authority_revoked"] = account["mint_authority_revoked"]
        onchain["freeze_authority_revoked"] = account["freeze_authority_revoked"]
    filtered = memecoin_desk.filter_case(opened["case_id"], onchain, now=now)
    return {
        "ok": False,
        "reason": filtered.get("reason") or coverage["reason"],
        "stage": filtered.get("stage"),
        "case_id": opened["case_id"],
        "mint": mint,
        "stalkchain": coverage,
        "mint_account_ok": account.get("ok") is True,
        "score_measured": False,
        "send": False,
        "sign": False,
        "live_ready": False,
        "armed": False,
        "usd_live": False,
    }
