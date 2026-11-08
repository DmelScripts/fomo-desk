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


def run_discovery(transport, now: float) -> dict:
    """Pick one mint from the documented boost list, then hand it to the six stages.

    A caller does not name the mint. The default process does not call this.
    """
    stopped = scheduler_status() | {"ok": False, "send": False, "live_ready": False}
    if transport is None:
        return stopped | {"reason": "network_disabled", "mint": None}
    positions = _supervise_open(transport, now)
    try:
        response = transport({"kind": "http", "url": dexscreener_public.BOOSTS_URL})
    except Exception:
        _halt_entries()
        return stopped | {"reason": "discovery_fault", "entry_halt": True, "mint": None, "positions": positions}
    if not isinstance(response, dict) or response.get("status") != 200:
        return stopped | {"reason": "dexscreener_status", "mint": None, "positions": positions}
    selected = dexscreener_public.parse_boost_mints(response.get("body"))
    if selected.get("ok") is not True:
        return stopped | {"reason": selected.get("reason"), "source": selected.get("source"), "mint": None, "caller_named_mint": False, "kills": [], "positions": positions}
    kills = list(selected.get("skipped") or [])
    ranked = []
    screened = []
    supervised = {item.get("mint") for item in positions}
    for mint in selected["mints"]:
        if mint in supervised:
            continue
        result = run_once(mint, transport, now)
        screened.append({
            "mint": mint,
            "reason": result.get("reason"),
            "stage": result.get("stage"),
            "quote": result.get("quote"),
            "jupiter_called": result.get("jupiter_called") is True,
        })
        if result.get("reason"):
            kills.append(screened[-1] | {"source": result.get("source") or dexscreener_public.SOURCE})
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
        "screened": screened,
        "positions": positions,
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
    last = None
    try:
        for _ in range(rounds):
            try:
                last = run_discovery(transport, now)
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
        "discovery": last,
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
        return stopped | {"reason": parsed.get("reason"), "source": dexscreener_public.SOURCE, "stage": "scout", "fdv_used_as_market_cap": False}
    _observe(mint, dexscreener_public.SOURCE, "market_cap_usd", parsed["scout"]["market_cap_usd"], now)
    _observe(mint, dexscreener_public.SOURCE, "liquidity_usd", parsed["scout"]["liquidity_usd"], now)
    opened = memecoin_desk.scout(parsed["scout"], now=now)
    if opened.get("ok") is not True:
        quote = _screen_quote(transport, now, mint, parsed.get("evidence") or {})
        return stopped | {
            "reason": opened.get("reason"), "stage": opened.get("stage"), "case_id": opened.get("case_id"),
            "quote": quote, "jupiter_called": quote.get("jupiter_called") is True, "score_measured": False,
            "quote_expiry_proven": False, "sources_are_live_mcp": False,
        }
    try:
        rpc = transport({"kind": "rpc", "method": "getAccountInfo", "mint": mint})
    except Exception:
        return stopped | {"reason": "mint_transport", "stage": "filter", "case_id": opened["case_id"]}
    account = {"ok": False, "reason": "account_missing"}
    if isinstance(rpc, dict) and rpc.get("status") == 200 and isinstance(rpc.get("body"), dict):
        value = (rpc["body"].get("result") or {}).get("value") if isinstance(rpc["body"].get("result"), dict) else None
        if isinstance(value, dict):
            account = solana_mint_read.parse_account(value, mint)
    coverage = stalkchain_adapter.read_mint(mint, transport)
    onchain = {"source": solana_mint_read.SOURCE, "fetched_at": now}
    if account.get("ok") is True:
        onchain["mint_authority_revoked"] = account["mint_authority_revoked"]
        onchain["freeze_authority_revoked"] = account["freeze_authority_revoked"]
    filtered = memecoin_desk.filter_case(opened["case_id"], onchain, now=now)
    blocked = {"attempted": False, "jupiter_called": False, "score_measured": False, "reason": filtered.get("reason") or "features_unpublished", "send": False, "live_ready": False, "sources_are_live_mcp": False}
    return stopped | {
        "reason": filtered.get("reason") or "features_unpublished",
        "stage": filtered.get("stage"),
        "case_id": opened["case_id"],
        "stalkchain": coverage,
        "mint_account_ok": account.get("ok") is True,
        "quote": blocked,
        "jupiter_called": False,
        "score_measured": False,
        "quote_expiry_proven": False,
        "effectual_entry": False,
        "sources_are_live_mcp": False,
        "usd_live": False,
        "holder_growth": holder_growth(mint),
        "caller_named_mint": True,
    }


def _screen_quote(transport, now: float, mint: str, evidence: dict, allow_synthetic: bool = False) -> dict:
    """Read-only pool review. Authorization and the owner record are required first.

    A missing policy does not fetch the pool or Jupiter. An unsupported pair,
    a wrong owner, or a stale slot stops before Jupiter. An open position is
    left where it is.
    """
    quiet = {"attempted": False, "jupiter_called": False, "send": False, "sign": False, "live_ready": False, "quote_expiry_proven": False, "effectual_entry": False, "score_measured": False}
    if not _authorized():
        return quiet | {"reason": "scheduler_unauthorized"}
    import owner_slippage
    if owner_slippage.ceiling_bps() != owner_slippage.BPS:
        return quiet | {"reason": "owner_slippage_unapproved"}
    identity = dexscreener_public.supported_pool(evidence)
    if identity.get("ok") is not True:
        return quiet | {"attempted": True, "reason": identity.get("reason"), "dex_id": identity.get("dex_id")}
    intent = _quote_intent(mint, identity["pool"], allow_synthetic=allow_synthetic)
    if intent.get("ok") is not True:
        return quiet | {"attempted": True, "reason": intent.get("reason"), "pool": identity["pool"]}
    if intent["side"] == "entry":
        sol = _fresh_sol(transport, now)
        leg = sol.get("sol") if isinstance(sol, dict) and sol.get("fresh") is True else None
        if not isinstance(leg, dict):
            return quiet | {"attempted": True, "reason": (sol or {}).get("reason") if isinstance(sol, dict) else "sol_usd_unpriced", "pool": identity["pool"]}
        rent = _account_rent(transport, mint)
        if rent.get("ok") is not True:
            return quiet | {"attempted": True, "reason": rent.get("reason"), "pool": identity["pool"]}
        sized = memecoin_desk._entry_lamports(leg, rent["lamports"])
        if sized.get("ok") is not True:
            return quiet | {"attempted": True, "reason": sized.get("reason"), "pool": identity["pool"]}
        refreshed = memecoin_desk.refresh_entry_intent(intent["case_id"], sized, now)
        if refreshed.get("ok") is not True:
            return quiet | {"attempted": True, "reason": refreshed.get("reason"), "pool": identity["pool"], "previous_amount": refreshed.get("amount")}
        intent = intent | {"amount": refreshed["amount"], "version": refreshed.get("version")}
    import jupiter_route_v2
    try:
        first = jupiter_route_v2.read_snapshot(transport, [identity["pool"]], now)
    except Exception:
        return quiet | {"attempted": True, "reason": "snapshot_transport", "pool": identity["pool"]}
    if first.get("ok") is not True:
        return quiet | {"attempted": True, "reason": first.get("reason"), "pool": identity["pool"]}
    pool_record = first["snapshot"].accounts.get(identity["pool"])
    if not isinstance(pool_record, tuple) or pool_record[0] != __import__("solana_whirlpool").PROGRAM_ID:
        return quiet | {"attempted": True, "reason": "whirlpool_pool_unbound", "pool": identity["pool"], "slot": first["snapshot"].slot}
    fields = __import__("solana_whirlpool").pool_fields(pool_record[1])
    if fields is None or intent["input_mint"] not in {fields[0], fields[2]}:
        return quiet | {"attempted": True, "reason": "whirlpool_mint_unbound", "pool": identity["pool"]}
    keys = jupiter_route_v2.snapshot_keys(pool_record[1], identity["pool"], fields[0] == intent["input_mint"])
    if keys is None:
        return quiet | {"attempted": True, "reason": "whirlpool_pool_unbound", "pool": identity["pool"]}
    try:
        fetched = jupiter_route_v2.read_snapshot(transport, keys, now)
    except Exception:
        return quiet | {"attempted": True, "reason": "snapshot_transport", "pool": identity["pool"]}
    if fetched.get("ok") is not True:
        return quiet | {"attempted": True, "reason": fetched.get("reason"), "pool": identity["pool"]}
    reviewed = jupiter_route_v2.review_snapshot(
        fetched["snapshot"], identity["pool"], intent["input_mint"], intent["output_mint"], intent["amount"], now,
    )
    if reviewed.get("ok") is not True:
        return quiet | {"attempted": True, "reason": reviewed.get("reason"), "pool": identity["pool"], "slot": fetched["snapshot"].slot, "tip_slot": fetched["snapshot"].tip_slot}
    import public_market

    def get(url: str):
        response = transport({"kind": "http", "url": url})
        if not isinstance(response, dict) or response.get("status") != 200 or not isinstance(response.get("body"), (dict, list)):
            raise public_market.PublicStop("http_status")
        return response["body"]

    try:
        _order, build, instruction = public_market.request_quote(
            get, reviewed["est_out"], intent["amount"], intent["input_mint"], intent["output_mint"],
        )
    except Exception:
        return quiet | {"attempted": True, "reason": "quote_transport", "pool": identity["pool"], "measured_est_out": reviewed["est_out"]}
    return quiet | {
        "attempted": True,
        "reason": None if instruction.get("instruction_matches_request") is True else build.get("reason"),
        "jupiter_called": instruction.get("jupiter_called") is True,
        "pool": identity["pool"],
        "amount": intent["amount"],
        "side": intent["side"],
        "measured_est_out": reviewed["est_out"],
        "requested_slippage_bps": instruction.get("requested_slippage_bps"),
        "instruction_matches_request": instruction.get("instruction_matches_request") is True,
        "instruction_sha256": instruction.get("instruction_sha256"),
        "rewritten": False,
        "slot": reviewed["slot"],
        "tip_slot": reviewed["tip_slot"],
        "context_slot": reviewed["context_slot"],
        "quote_expiry_proven": False,
        "block_height_missing": True,
        "oracle_price_proven": False,
        "pyth_is_separate": True,
        "slot_is_block_height": False,
        "send": False,
        "sign": False,
        "live_ready": False,
        "effectual_entry": False,
    }


def _fresh_sol(transport, now: float) -> dict:
    """Read the sponsored SOL/USD accounts through this round's transport."""
    import pyth_sponsored
    try:
        return pyth_sponsored.read_accounts(transport, now)
    except Exception:
        return {"fresh": False, "reason": "sol_usd_unpriced"}


def _slot_fresh(context_slot, tip_slot) -> bool:
    import jupiter_route_v2
    if isinstance(context_slot, bool) or isinstance(tip_slot, bool):
        return False
    if not isinstance(context_slot, int) or not isinstance(tip_slot, int):
        return False
    lag = tip_slot - context_slot
    return 0 <= lag <= jupiter_route_v2.SLOT_LAG_MAX


def _context_slot(response) -> int | None:
    body = response.get("body") if isinstance(response, dict) else None
    result = body.get("result") if isinstance(body, dict) else None
    context = result.get("context") if isinstance(result, dict) else None
    slot = context.get("slot") if isinstance(context, dict) else None
    if isinstance(slot, bool) or not isinstance(slot, int):
        return None
    return slot


def _classic_ata(item, mint: str, wallet: str) -> bool:
    """Exact 165-byte initialized SPL account for this mint and wallet."""
    import base64
    import solana_tx
    if not isinstance(item, dict) or item.get("owner") != solana_tx.TOKEN_PROGRAM:
        return False
    data = item.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], str) or data[1:] not in ([], ["base64"]):
        return False
    try:
        raw = base64.b64decode(data[0], validate=True)
    except Exception:
        return False
    if len(raw) != solana_tx.TOKEN_ACCOUNT_SPACE or raw[108] != 1:
        return False
    if solana_tx.b58encode(raw[0:32]) != mint or solana_tx.b58encode(raw[32:64]) != wallet:
        return False
    return True


def _account_rent(transport, mint: str) -> dict:
    """Rent for the classic wSOL and mint accounts this entry would create.

    The mint program is read first. Token-2022 is refused. A null account is
    counted. Any other account is not treated as already created. The context
    slot has to sit inside the current tip window, and the genesis has to be
    mainnet.
    """
    import phantom_transport
    import solana_inspect
    import solana_tx
    refused = {"ok": False, "reason": "rent_unpriced"}
    try:
        genesis = transport({"kind": "rpc", "method": "getGenesisHash"})
        tip = transport({"kind": "rpc", "method": "getSlot"})
        mint_info = transport({"kind": "rpc", "method": "getAccountInfo", "mint": mint})
        exemption = transport({"kind": "rpc", "method": "getMinimumBalanceForRentExemption", "size": solana_tx.TOKEN_ACCOUNT_SPACE})
    except Exception:
        return refused
    genesis_body = genesis.get("body") if isinstance(genesis, dict) else None
    if not isinstance(genesis_body, dict) or genesis_body.get("result") != solana_inspect.MAINNET_GENESIS:
        return refused | {"reason": "genesis"}
    tip_body = tip.get("body") if isinstance(tip, dict) else None
    tip_slot = tip_body.get("result") if isinstance(tip_body, dict) else None
    if isinstance(tip_slot, bool) or not isinstance(tip_slot, int) or tip_slot < 0:
        return refused
    mint_body = mint_info.get("body") if isinstance(mint_info, dict) else None
    mint_result = mint_body.get("result") if isinstance(mint_body, dict) else None
    mint_value = mint_result.get("value") if isinstance(mint_result, dict) else None
    if not isinstance(mint_value, dict):
        return refused | {"reason": "mint_program_unproven"}
    program = mint_value.get("owner")
    if program == solana_tx.TOKEN_2022:
        return {"ok": False, "reason": "token_2022_unsupported", "lamports": None}
    if program != solana_tx.TOKEN_PROGRAM:
        return refused | {"reason": "mint_program_unproven"}
    parsed = solana_mint_read.parse_account(mint_value, mint)
    if parsed.get("ok") is not True:
        return refused | {"reason": "mint_program_unproven"}
    if not _slot_fresh(_context_slot(mint_info), tip_slot):
        return refused | {"reason": "rent_slot_stale"}
    exempt_body = exemption.get("body") if isinstance(exemption, dict) else None
    each = exempt_body.get("result") if isinstance(exempt_body, dict) else None
    if isinstance(each, bool) or not isinstance(each, int) or each < 1:
        return refused
    wallet = phantom_transport.REQUIRED_WALLET
    keys = [
        (solana_tx.associated_token_address(wallet, solana_tx.NATIVE_MINT, solana_tx.TOKEN_PROGRAM), solana_tx.NATIVE_MINT),
        (solana_tx.associated_token_address(wallet, mint, program), mint),
    ]
    try:
        listed = transport({"kind": "rpc", "method": "getMultipleAccounts", "accounts": [item[0] for item in keys]})
    except Exception:
        return refused
    if not _slot_fresh(_context_slot(listed), tip_slot):
        return refused | {"reason": "rent_slot_stale"}
    listed_body = listed.get("body") if isinstance(listed, dict) else None
    result = listed_body.get("result") if isinstance(listed_body, dict) else None
    value = result.get("value") if isinstance(result, dict) else None
    if not isinstance(value, list) or len(value) != len(keys):
        return refused
    count = 0
    for (address, expected_mint), item in zip(keys, value):
        if item is None:
            count += 1
            continue
        if not _classic_ata(item, expected_mint, wallet):
            return refused | {"reason": "rent_account_unbound", "account": address}
    return {
        "ok": True,
        "lamports": each * count,
        "each": each,
        "count": count,
        "space": solana_tx.TOKEN_ACCOUNT_SPACE,
        "slot": _context_slot(listed),
        "tip_slot": tip_slot,
        "genesis": solana_inspect.MAINNET_GENESIS,
        "mint_program": program,
    }


def _quote_intent(mint: str, pool: str, allow_synthetic: bool = False) -> dict:
    """Quantity from an open protected entry or a filled exit. No invented size."""
    import db
    import solana_tx
    refused = {"ok": False, "reason": "intent_unbound"}
    try:
        rows = db.query(
            "SELECT id, stage, snapshot_json FROM memecoin_cases WHERE mint = ? AND stage NOT IN ('rejected', 'exited')",
            (mint,),
        )
    except Exception:
        return refused
    if len(rows) != 1:
        return refused
    import json
    try:
        snap = json.loads(rows[0]["snapshot_json"])
    except Exception:
        return refused
    if not isinstance(snap, dict):
        return refused
    stage = rows[0]["stage"]
    synthetic = snap.get("capital_ledger") == "synthetic"
    if synthetic and not allow_synthetic:
        return refused | {"reason": "synthetic_not_live"}
    if stage in {"filled", "partial", "exit_intent"}:
        if isinstance(snap.get("inflight"), dict):
            return refused | {"reason": "exit_inflight"}
        if stage == "filled":
            return refused | {"reason": "exit_hold"}
        intent_row = snap.get("exit_intent") if isinstance(snap.get("exit_intent"), dict) else {}
        if intent_row.get("source") != "server_pool_pyth" or intent_row.get("live") is not True:
            return refused | {"reason": "exit_unproven"}
        left = memecoin_desk.receipt_remaining(rows[0]["id"])
        if left.get("ok") is not True or not isinstance(left.get("remaining"), int) or left["remaining"] <= 0:
            return refused | {"reason": "flat" if left.get("remaining") == 0 else left.get("reason")}
        return {"ok": True, "side": "exit", "amount": left["remaining"], "input_mint": mint, "output_mint": solana_tx.NATIVE_MINT, "a_to_b": False, "case_id": rows[0]["id"], "pool": pool}
    amount = snap.get("entry_amount_raw")
    if stage == "protected" and isinstance(amount, int) and not isinstance(amount, bool) and amount > 0:
        return {"ok": True, "side": "entry", "amount": amount, "input_mint": solana_tx.NATIVE_MINT, "output_mint": mint, "a_to_b": True, "case_id": rows[0]["id"], "pool": pool}
    return refused


def _open_positions() -> list:
    import db
    try:
        memecoin_desk.loss_totals()
        return db.query(
            """
            SELECT id, mint, stage, snapshot_json, ledger FROM memecoin_cases
            WHERE stage IN ('filled', 'partial', 'exit_intent')
              AND ledger = 'live'
              AND EXISTS (
                SELECT 1 FROM memecoin_entry_receipts
                WHERE case_id = memecoin_cases.id AND failed = 0 AND qty > 0
              )
            """
        )
    except Exception:
        return []


def _bound_mark(row, snap: dict, transport, now: float, evidence: dict):
    """Pool sqrt price times a fresh SOL leg. The pair's priceUsd is not used."""
    import base64
    import pyth_sponsored
    pool = evidence.get("pair_address") if isinstance(evidence, dict) else None
    if not isinstance(pool, str):
        pool = snap.get("pool")
    if not isinstance(pool, str):
        return {"ok": False, "reason": "exit_mark_unpriced"}
    try:
        listed = transport({"kind": "rpc", "method": "getMultipleAccounts", "accounts": [pool]})
        mint_info = transport({"kind": "rpc", "method": "getAccountInfo", "mint": row["mint"]})
        tip = transport({"kind": "rpc", "method": "getSlot"})
    except Exception:
        return {"ok": False, "reason": "exit_mark_unpriced"}
    body = listed.get("body") if isinstance(listed, dict) else None
    result = body.get("result") if isinstance(body, dict) else None
    value = result.get("value") if isinstance(result, dict) else None
    slot = _context_slot(listed)
    tip_body = tip.get("body") if isinstance(tip, dict) else None
    tip_slot = tip_body.get("result") if isinstance(tip_body, dict) else None
    if not isinstance(value, list) or not value or not isinstance(value[0], dict):
        return {"ok": False, "reason": "exit_mark_unpriced"}
    data = value[0].get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], str):
        return {"ok": False, "reason": "exit_mark_unpriced"}
    try:
        raw = base64.b64decode(data[0], validate=True)
    except Exception:
        return {"ok": False, "reason": "exit_mark_unpriced"}
    mint_body = mint_info.get("body") if isinstance(mint_info, dict) else None
    mint_value = ((mint_body or {}).get("result") or {}).get("value") if isinstance(mint_body, dict) else None
    parsed = solana_mint_read.parse_account(mint_value, row["mint"]) if isinstance(mint_value, dict) else {"ok": False}
    if parsed.get("ok") is not True:
        return {"ok": False, "reason": "mint_decimals"}
    account = mint_value.get("data")
    try:
        mint_raw = base64.b64decode(account[0], validate=True)
    except Exception:
        return {"ok": False, "reason": "mint_decimals"}
    if len(mint_raw) < 45:
        return {"ok": False, "reason": "mint_decimals"}
    decimals = mint_raw[44]
    sol = _fresh_sol(transport, now)
    leg = sol.get("sol") if isinstance(sol, dict) and sol.get("fresh") is True else None
    if not isinstance(leg, dict):
        return {"ok": False, "reason": "sol_usd_unpriced"}
    bound = memecoin_desk.read_bound_price(row["id"], raw, pool, leg, decimals, slot if slot is not None else -1, tip_slot if isinstance(tip_slot, int) else -1, now)
    if isinstance(bound, memecoin_desk.BoundPrice):
        return bound
    return bound if isinstance(bound, dict) else {"ok": False, "reason": "exit_mark_unpriced"}


def _supervise_open(transport, now: float) -> list:
    """Mark and, when the stop or trail has fired, quote each open receipt position.

    This does not read the boost list and it does not apply the new-entry halt.
    """
    import json
    found = []
    for row in _open_positions():
        try:
            snap = json.loads(row["snapshot_json"])
        except Exception:
            found.append({"mint": row["mint"], "reason": "snapshot", "jupiter_called": False, "supervised": True})
            continue
        if isinstance(snap.get("inflight"), dict):
            found.append({"mint": row["mint"], "reason": "exit_inflight", "jupiter_called": False, "supervised": True})
            continue
        balance = memecoin_desk.receipt_remaining(row["id"])
        if balance.get("remaining") == 0:
            found.append({"mint": row["mint"], "reason": "flat", "jupiter_called": False, "supervised": True})
            continue
        if balance.get("ok") is not True:
            found.append({"mint": row["mint"], "reason": balance.get("reason"), "jupiter_called": False, "supervised": True})
            continue
        try:
            response = transport({"kind": "http", "url": dexscreener_public.pairs_url(row["mint"])})
        except Exception:
            found.append({"mint": row["mint"], "reason": "exit_mark_unpriced", "jupiter_called": False, "supervised": True})
            continue
        parsed = dexscreener_public.parse_pairs(response.get("body") if isinstance(response, dict) else None, row["mint"], now)
        if parsed.get("ok") is not True:
            found.append({"mint": row["mint"], "reason": "exit_mark_unpriced", "jupiter_called": False, "supervised": True})
            continue
        intent = snap.get("exit_intent") if isinstance(snap.get("exit_intent"), dict) else {}
        if row["stage"] == "filled":
            bound = _bound_mark(row, snap, transport, now, parsed.get("evidence") or {})
            if not isinstance(bound, memecoin_desk.BoundPrice):
                found.append({"mint": row["mint"], "reason": bound.get("reason") if isinstance(bound, dict) else "exit_mark_unpriced", "jupiter_called": False, "supervised": True})
                continue
            marked = memecoin_desk.apply_live_mark(row["id"], bound, now)
            if marked.get("action") not in {"stop", "trail"}:
                found.append({"mint": row["mint"], "reason": marked.get("reason") or "exit_hold", "action": marked.get("action"), "jupiter_called": False, "supervised": True, "mark_price": format(bound.price, "f")})
                continue
        elif intent.get("source") != "server_pool_pyth" or intent.get("live") is not True:
            found.append({"mint": row["mint"], "reason": "exit_unproven", "jupiter_called": False, "supervised": True})
            continue
        quote = _screen_quote(transport, now, row["mint"], parsed.get("evidence") or {})
        found.append({
            "mint": row["mint"],
            "reason": quote.get("reason"),
            "quote": quote,
            "jupiter_called": quote.get("jupiter_called") is True,
            "supervised": True,
            "side": quote.get("side"),
        })
    return found


def synthetic_screen(mint: str, features: dict, transport, now: float) -> dict:
    """Test-only handoff. It does not reserve live capital and the live round does not call it."""
    quiet = {"ok": False, "send": False, "sign": False, "live_ready": False, "live_capital": False, "ledger": "synthetic", "jupiter_called": False}
    if not isinstance(features, dict) or features.get("mint") != mint or not isinstance(mint, str):
        return quiet | {"reason": "synthetic_mint"}
    if features.get("source") != "synthetic_test_adapter":
        return quiet | {"reason": "synthetic_provenance"}
    fetched = features.get("fetched_at")
    if isinstance(fetched, bool) or not isinstance(fetched, (int, float)) or abs(float(now) - float(fetched)) > 30:
        return quiet | {"reason": "synthetic_timestamp"}
    onchain = features.get("onchain")
    components = features.get("components")
    if not isinstance(onchain, dict) or onchain.get("source") != "synthetic_test_adapter":
        return quiet | {"reason": "synthetic_types"}
    if not isinstance(components, dict) or any(name not in components for name in memecoin_desk.COMPONENTS):
        return quiet | {"reason": "synthetic_types"}
    try:
        response = transport({"kind": "http", "url": dexscreener_public.pairs_url(mint)})
    except Exception:
        return quiet | {"reason": "dexscreener_transport"}
    parsed = dexscreener_public.parse_pairs(response.get("body") if isinstance(response, dict) else None, mint, now)
    if parsed.get("ok") is not True:
        return quiet | {"reason": parsed.get("reason")}
    opened = memecoin_desk.scout(parsed["scout"], now=now)
    if opened.get("ok") is True:
        filtered = memecoin_desk.filter_case(opened["case_id"], onchain, now=now)
        if filtered.get("ok") is not True:
            return quiet | {"reason": filtered.get("reason"), "stage": filtered.get("stage")}
        analyzed = memecoin_desk.analyze(opened["case_id"], components, now=now)
        if analyzed.get("ok") is not True:
            return quiet | {"reason": analyzed.get("reason"), "stage": analyzed.get("stage")}
        proof = _account_rent(transport, mint)
        if proof.get("ok") is not True:
            return quiet | {"reason": proof.get("reason")}
        _fresh_sol(transport, now)
        protected = memecoin_desk.protect(opened["case_id"], now=now, rent_proof=proof, ledger="synthetic")
        if protected.get("ok") is not True:
            return quiet | {"reason": protected.get("reason"), "stage": protected.get("stage")}
    elif opened.get("reason") != "already_open":
        return quiet | {"reason": opened.get("reason")}
    quote = _screen_quote(transport, now, mint, parsed.get("evidence") or {}, allow_synthetic=True)
    stored = memecoin_desk.view(mint)
    live = stored is None or (stored.get("snapshot") or {}).get("live_capital") is not False
    return quiet | {
        "ok": quote.get("jupiter_called") is True,
        "reason": quote.get("reason"),
        "quote": quote,
        "jupiter_called": quote.get("jupiter_called") is True,
        "stage": None if stored is None else stored.get("stage"),
        "live_capital": False,
        "counted_as_live": live,
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
    if request.get("kind") == "rpc" or method in {"getAccountInfo", "getMultipleAccounts", "getMinimumBalanceForRentExemption", "getSlot", "getTransaction", "getSignatureStatuses", "getGenesisHash"}:
        if method == "getAccountInfo":
            params = [request.get("mint"), {"encoding": "base64", "commitment": "confirmed"}]
        elif method == "getMultipleAccounts":
            accounts = request.get("accounts")
            if not isinstance(accounts, list) or not accounts or not all(isinstance(item, str) for item in accounts):
                raise RuntimeError("method_refused")
            params = [accounts, {"encoding": "base64", "commitment": "confirmed"}]
        elif method == "getMinimumBalanceForRentExemption":
            size = request.get("size")
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise RuntimeError("method_refused")
            params = [size]
        elif method == "getSlot":
            params = [{"commitment": "confirmed"}]
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
        if not isinstance(url, str) or not (url.startswith("https://api.dexscreener.com/") or url.startswith("https://api.jup.ag/swap/v2/")):
            raise RuntimeError("host_refused")
        if url.startswith("https://api.jup.ag/") and ("/execute" in url or "/submit" in url):
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


