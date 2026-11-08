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

# The scheduler is implemented and unarmed. Nothing in this process starts it.
ARMED = False
STARTED = False

import dexscreener_public
import memecoin_desk
import pyth_sponsored
import solana_mint_read
import stalkchain_adapter
import usdc_valuation


def scheduler_status() -> dict:
    """The loop exists and is not started. Agents and LIVE are not turned on here."""
    return {
        "armed": False,
        "started": False,
        "server_loop": False,
        "scheduled": False,
        "send": False,
        "sign": False,
        "live_ready": False,
        "agents_enabled": False,
    }


def run_discovery(transport, now: float, stalk_transport=None) -> dict:
    """Pick one mint from the documented boost list, then hand it to the six stages.

    A caller does not name the mint. The default process does not call this.
    """
    stopped = scheduler_status() | {"ok": False}
    if ARMED or STARTED:
        return stopped | {"reason": "scheduler_refusing_to_start"}
    if transport is None:
        return stopped | {"reason": "network_disabled", "mint": None}
    try:
        response = transport({"kind": "http", "url": dexscreener_public.BOOSTS_URL})
    except Exception:
        return stopped | {"reason": "dexscreener_transport", "mint": None}
    if not isinstance(response, dict) or response.get("status") != 200:
        return stopped | {"reason": "dexscreener_status", "mint": None}
    selected = dexscreener_public.parse_boost_mint(response.get("body"))
    if selected.get("ok") is not True:
        return stopped | {"reason": selected.get("reason"), "mint": None, "caller_named_mint": False}
    result = run_once(selected["mint"], transport, now, stalk_transport)
    result["caller_named_mint"] = False
    result["discovered_mint"] = selected["mint"]
    result["armed"] = False
    result["started"] = False
    result["scheduled"] = False
    return result


def holder_growth(mint: str) -> dict:
    """Growth only from this desk's own holder-count rows. One row is not a series."""
    import db
    conn = db.connect()
    try:
        _observations(conn)
        conn.commit()
        rows = list(conn.execute(
            "SELECT value, fetched_at FROM memecoin_observations WHERE mint = ? AND source = ? AND metric = ? ORDER BY fetched_at",
            (mint, "desk_holder_count", "holder_count"),
        ))
    finally:
        conn.close()
    if len(rows) < 2:
        return {"ok": False, "reason": "holder_growth_unproven", "points": len(rows), "score_measured": False, "cross_provider": False}
    first = Decimal_from(rows[0]["value"])
    last = Decimal_from(rows[-1]["value"])
    if first is None or last is None:
        return {"ok": False, "reason": "holder_growth_unproven", "points": len(rows), "score_measured": False}
    return {"ok": True, "reason": None, "points": len(rows), "delta": format(last - first, "f"), "score_measured": False, "cross_provider": False}


def Decimal_from(value):
    from decimal import Decimal, InvalidOperation
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _observations(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memecoin_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mint TEXT NOT NULL,
            source TEXT NOT NULL,
            metric TEXT NOT NULL,
            value TEXT NOT NULL,
            fetched_at REAL NOT NULL
        )
        """
    )


def _observe(mint: str, source: str, metric: str, value: str, fetched_at: float) -> None:
    import db

    def work(conn):
        _observations(conn)
        conn.execute(
            "INSERT INTO memecoin_observations (mint, source, metric, value, fetched_at) VALUES (?, ?, ?, ?, ?)",
            (mint, source, metric, value, fetched_at),
        )

    db.exclusive(work)


def run_once(mint: str, transport, now: float, stalk_transport=None) -> dict:
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
    _observe(mint, dexscreener_public.SOURCE, "market_cap_usd", parsed["scout"]["market_cap_usd"], now)
    _observe(mint, dexscreener_public.SOURCE, "liquidity_usd", parsed["scout"]["liquidity_usd"], now)
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
    coverage = stalkchain_adapter.read_mint(mint, stalk_transport)
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
        "holder_growth": holder_growth(mint),
        "caller_named_mint": True,
        "send": False,
        "sign": False,
        "live_ready": False,
        "armed": False,
        "server_loop": False,
        "scheduled": False,
        "usd_live": False,
        "onchain_pyth": pyth_sponsored.status(),
    }


def readiness() -> dict:
    """Presence and status for this process. Not a Render inventory and not a secret."""
    pyth = pyth_sponsored.status()
    catalog = usdc_valuation.keyless_catalog_is_not_a_price()
    return {
        "coded": True,
        "deployed": False,
        "this_process_only": True,
        "server_loop": False,
        "run_once_scheduled": False,
        "send": False,
        "sign": False,
        "live_ready": False,
        "stalkchain_env_present": stalkchain_adapter.this_process_has_key(),
        "pyth_env_present": usdc_valuation.this_process_has_key(),
        "render_presence": "unknown",
        "hermes_price_requires_key": True,
        "keyless_catalog_is_price": False,
        "keyless_catalog_reason": catalog["reason"],
        "onchain_pyth": pyth["reason"],
        "onchain_pyth_configured": pyth["configured"],
        "onchain_pyth_valid": pyth["valid"],
        "onchain_pyth_fresh": pyth["fresh"],
        "onchain_pyth_live": False,
        "onchain_pyth_age_s": pyth["age_s"],
        "onchain_pyth_account": pyth["account"],
        "onchain_pyth_publish_time": pyth["publish_time"],
        "onchain_pyth_observed_at": pyth["observed_at"],
        "onchain_pyth_observation_age_s": pyth["observation_age_s"],
        "onchain_pyth_usable": pyth["usable"],
        "onchain_pyth_stale_observation": pyth["stale_observation"],
        "onchain_pyth_valuation": pyth["valuation"],
        "onchain_pyth_usdc_used": False,
        "onchain_pyth_available": pyth["usable"] is True,
        "blocked_because": [
            "stalkchain_response_fields_for_lp_rug_growth_are_unpublished",
            "owner_slippage_unapproved",
            pyth["reason"] or "pyth_sponsored_not_fresh",
            "hermes_not_called",
        ],
    }


