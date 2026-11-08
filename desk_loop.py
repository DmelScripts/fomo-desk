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

# Production authorization is a settings row, default absent. Readiness is not met.
_runtime = {"running": False, "started": False, "passes": 0, "faults": 0}

import dexscreener_public
import memecoin_desk
import pyth_sponsored
import solana_mint_read
import stalkchain_adapter
import usdc_valuation


def _authorized() -> bool:
    import db
    try:
        return db.get_setting("memecoin_scheduler_authorized", "0") == "1"
    except Exception:
        return False


def _ready_reasons() -> list[str]:
    """Missing evidence. A test cannot delete these by replacing this function's constant result."""
    reasons = []
    import owner_slippage
    if owner_slippage.ceiling_bps() != owner_slippage.BPS:
        reasons.append("owner_slippage_unapproved")
    if stalkchain_adapter.UNPROVEN:
        reasons.append("lp_fields_unpublished")
        reasons.append("rug_history_unpublished")
    reasons.append("holder_growth_unproven")
    if db_setting("memecoin_entry_halt") == "1":
        reasons.append("entry_halt")
    return reasons


def db_setting(key: str):
    import db
    try:
        return db.get_setting(key, "")
    except Exception:
        return ""


def _ready() -> bool:
    return _ready_reasons() == []


def scheduler_status() -> dict:
    """Report the real latch. Armed only when authorization and readiness are both true."""
    authorized = _authorized()
    ready = _ready()
    return {
        "armed": authorized and ready,
        "authorized": authorized,
        "ready": ready,
        "ready_because": [] if ready else _ready_reasons(),
        "started": _runtime["started"],
        "running": _runtime["running"],
        "passes": _runtime["passes"],
        "faults": _runtime["faults"],
        "server_loop": _runtime["started"],
        "scheduled": _runtime["started"],
        "send": False,
        "sign": False,
        "live_ready": False,
        "agents_enabled": False,
    }


def run_discovery(transport, now: float, stalk_transport=None) -> dict:
    """Pick one mint from the documented boost list, then hand it to the six stages.

    A caller does not name the mint. The default process does not call this.
    """
    stopped = scheduler_status() | {"ok": False, "send": False, "live_ready": False}
    if transport is None:
        return stopped | {"reason": "network_disabled", "mint": None}
    try:
        response = transport({"kind": "http", "url": dexscreener_public.BOOSTS_URL})
    except Exception:
        _halt_entries()
        return stopped | {"reason": "discovery_fault", "entry_halt": True, "mint": None}
    if not isinstance(response, dict) or response.get("status") != 200:
        return stopped | {"reason": "dexscreener_status", "mint": None}
    selected = dexscreener_public.parse_boost_mints(response.get("body"))
    if selected.get("ok") is not True:
        return stopped | {"reason": selected.get("reason"), "source": selected.get("source"), "mint": None, "caller_named_mint": False, "kills": []}
    kills = list(selected.get("skipped") or [])
    ranked = []
    for mint in selected["mints"]:
        result = run_once(mint, transport, now, stalk_transport)
        if result.get("reason"):
            kills.append({"mint": mint, "reason": result.get("reason"), "source": result.get("source") or dexscreener_public.SOURCE})
            continue
        ranked.append(result)
    ranked.sort(key=lambda item: item.get("liquidity_ratio") or 0, reverse=True)
    return stopped | {
        "ok": False,
        "reason": None if not kills else kills[0].get("reason"),
        "source": dexscreener_public.SOURCE,
        "caller_named_mint": False,
        "candidates": len(selected["mints"]),
        "kills": kills,
        "ranked": [item.get("mint") for item in ranked],
        "score_measured": False,
        "holder_growth_unproven": True,
    }


def collect_holder_count(mint: str, reading: dict) -> dict:
    """Write a holder count only from a same-source series this desk can verify.

    StalkChain has not published the holder-count field. DexScreener transaction
    counts are not holders. Nothing is invented and nothing is purchased.
    """
    if not isinstance(reading, dict):
        return {"ok": False, "written": False, "reason": "holder_count_source_unpublished", "score_measured": False}
    if reading.get("source") != "desk_holder_count" or "holder_count" not in reading:
        return {"ok": False, "written": False, "reason": "holder_count_source_unpublished", "score_measured": False}
    return {"ok": False, "written": False, "reason": "holder_count_source_unpublished", "score_measured": False, "mint": mint}


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


def run_supervised(transport, now: float, *, rounds: int = 1) -> dict:
    """The loop the server would start. Tests call it with a mock. Production does not."""
    if _runtime["running"]:
        return {"ok": False, "reason": "already_running", "started": True, "agents_enabled": False, "live_ready": False}
    if not isinstance(rounds, int) or isinstance(rounds, bool) or rounds < 1 or rounds > 3:
        return {"ok": False, "reason": "rounds", "agents_enabled": False}
    _runtime["running"] = True
    _runtime["started"] = True
    faults = 0
    try:
        for _ in range(rounds):
            try:
                run_discovery(transport, now)
                _runtime["passes"] += 1
            except Exception as exc:
                _runtime["faults"] += 1
                _runtime["last_fault"] = repr(exc)
                faults += 1
                _halt_entries()
    finally:
        _runtime["started"] = False
        _runtime["running"] = False
    return {
        "ok": faults == 0,
        "reason": None if faults == 0 else "discovery_fault",
        "passes": rounds - faults,
        "faults": faults,
        "last_fault": _runtime.get("last_fault"),
        "started": False,
        "agents_enabled": False,
        "live_ready": False,
        "send": False,
    }


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
        return stopped | {"reason": parsed.get("reason"), "source": dexscreener_public.SOURCE, "stage": "scout", "fdv_used_as_market_cap": False}
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


def _halt_entries() -> None:
    import db
    try:
        db.set_setting("memecoin_entry_halt", "1", "scheduler_fault")
    except Exception:
        return


def read_transaction(signature: str) -> dict:
    """Confirmed mainnet getTransaction plus the genesis hash. This is the only confirm reader."""
    import memecoin_exit
    import solana_inspect
    try:
        genesis = public_reader({"method": "getGenesisHash"})
        response = public_reader({"method": "getTransaction", "signature": signature})
    except Exception:
        return {"ok": False, "reason": "receipt_unknown", "holds": True, "resend": False}
    body = genesis.get("body") if isinstance(genesis, dict) else None
    if not isinstance(body, dict) or body.get("result") != solana_inspect.MAINNET_GENESIS:
        return {"ok": False, "reason": "genesis", "holds": True, "resend": False}
    return memecoin_exit.pull_response(response)


def public_reader(request: dict) -> dict:
    """Documented DexScreener GET or one mainnet getAccountInfo. The unarmed process does not call this."""
    import json
    import urllib.request
    if not isinstance(request, dict):
        raise RuntimeError("host_refused")
    method = request.get("method")
    if request.get("kind") == "rpc" or method in {"getAccountInfo", "getTransaction", "getSignatureStatuses", "getGenesisHash"}:
        if method == "getAccountInfo":
            params = [request.get("mint"), {"encoding": "base64", "commitment": "confirmed"}]
        elif method == "getTransaction":
            params = [request.get("signature"), {"encoding": "base64", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}]
        elif method == "getSignatureStatuses":
            params = [[request.get("signature")], {"searchTransactionHistory": True}]
        elif method == "getGenesisHash":
            params = []
        else:
            raise RuntimeError("method_refused")
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        req = urllib.request.Request("https://api.mainnet-beta.solana.com", data=json.dumps(payload).encode(), headers={"content-type": "application/json"})
    else:
        url = request.get("url")
        if not isinstance(url, str) or not url.startswith("https://api.dexscreener.com/"):
            raise RuntimeError("host_refused")
        req = urllib.request.Request(url, headers={"accept": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as response:
        return {"status": response.status, "body": json.loads(response.read().decode())}


def _readiness_blocks(pyth: dict) -> list[str]:
    import owner_slippage
    blocks = ["stalkchain_response_fields_for_lp_rug_growth_are_unpublished"]
    if owner_slippage.ceiling_bps() != owner_slippage.BPS:
        blocks.append("owner_slippage_unapproved")
    blocks.append(pyth["reason"] or "pyth_sponsored_not_fresh")
    blocks.append("hermes_not_called")
    return blocks


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
        "blocked_because": _readiness_blocks(pyth),
        "slippage_policy": _slippage_policy(),
    }


def _slippage_policy() -> dict:
    """Approved code target beside the row this process actually has. Not a deploy."""
    import owner_slippage
    try:
        body = owner_slippage.configuration()
    except Exception:
        return {
            "code_target_bps": 50,
            "applied_bps": None,
            "record_state": "unreadable",
            "approved": False,
            "deployed": False,
            "production": False,
            "live": False,
            "live_ready": False,
            "send": False,
            "sign": False,
        }
    return {
        "code_target_bps": body["code_target_bps"],
        "applied_bps": body["applied_bps"],
        "record_state": body["record_state"],
        "approved": body["approved"],
        "startup_action": body["startup_action"],
        "deployed": False,
        "production": False,
        "live": False,
        "live_ready": False,
        "send": False,
        "sign": False,
    }


