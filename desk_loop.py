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
    screened = []
    for mint in selected["mints"]:
        result = run_once(mint, transport, now, stalk_transport)
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


def run_supervised(transport, now: float, *, rounds: int = 1, stalk_transport=None) -> dict:
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
                last = run_discovery(transport, now, stalk_transport)
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
    coverage = stalkchain_adapter.read_mint(mint, stalk_transport if stalk_transport is not None else transport)
    onchain = {"source": "synthetic_test_adapter" if coverage.get("synthetic_fields") else solana_mint_read.SOURCE, "fetched_at": now}
    if not isinstance(onchain["source"], str):
        onchain["source"] = solana_mint_read.SOURCE
    if account.get("ok") is True:
        onchain["mint_authority_revoked"] = account["mint_authority_revoked"]
        onchain["freeze_authority_revoked"] = account["freeze_authority_revoked"]
    for key, value in (coverage.get("proven") or {}).items():
        onchain[key] = value
    filtered = memecoin_desk.filter_case(opened["case_id"], onchain, now=now)
    if filtered.get("ok") is not True:
        blocked = {"attempted": False, "jupiter_called": False, "score_measured": False, "reason": filtered.get("reason"), "send": False, "live_ready": False, "sources_are_live_mcp": False}
        return stopped | {
            "reason": filtered.get("reason"), "stage": filtered.get("stage"), "case_id": opened["case_id"],
            "stalkchain": coverage, "mint_account_ok": account.get("ok") is True,
            "quote": blocked, "jupiter_called": False, "score_measured": False, "quote_expiry_proven": False,
            "effectual_entry": False, "sources_are_live_mcp": False, "usd_live": False,
        }
    components = coverage.get("components") if isinstance(coverage.get("components"), dict) else {}
    missing = [name for name in memecoin_desk.COMPONENTS if not isinstance(components.get(name), dict)]
    if missing:
        blocked = {"attempted": False, "jupiter_called": False, "score_measured": False, "reason": missing[0] + "_unproven", "send": False, "live_ready": False}
        return stopped | {"reason": missing[0] + "_unproven", "stage": "filter_passed", "case_id": opened["case_id"], "quote": blocked, "jupiter_called": False, "score_measured": False, "effectual_entry": False}
    analyzed = memecoin_desk.analyze(opened["case_id"], components, now=now)
    if analyzed.get("ok") is not True:
        blocked = {"attempted": False, "jupiter_called": False, "score_measured": False, "reason": analyzed.get("reason"), "send": False}
        return stopped | {"reason": analyzed.get("reason"), "stage": analyzed.get("stage"), "case_id": opened["case_id"], "quote": blocked, "jupiter_called": False, "score_measured": False, "effectual_entry": False}
    proof = _account_rent(transport, mint)
    if proof.get("ok") is not True:
        blocked = {"attempted": False, "jupiter_called": False, "reason": proof.get("reason"), "score_measured": False, "send": False}
        return stopped | {"reason": proof.get("reason"), "stage": "analyst_passed", "case_id": opened["case_id"], "quote": blocked, "jupiter_called": False, "score_measured": False, "effectual_entry": False}
    _fresh_sol(transport, now)
    protected = memecoin_desk.protect(opened["case_id"], now=now, rent_proof=proof)
    if protected.get("ok") is not True:
        blocked = {"attempted": False, "jupiter_called": False, "reason": protected.get("reason"), "score_measured": False, "send": False}
        return stopped | {"reason": protected.get("reason"), "stage": protected.get("stage"), "case_id": opened["case_id"], "quote": blocked, "jupiter_called": False, "score_measured": False, "effectual_entry": False}
    quote = _screen_quote(transport, now, mint, parsed.get("evidence") or {})
    return {
        "ok": False,
        "reason": quote.get("reason"),
        "stage": "protected" if quote.get("jupiter_called") is True else protected.get("stage"),
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
        "quote": quote,
        "jupiter_called": quote.get("jupiter_called") is True,
        "quote_expiry_proven": False,
        "effectual_entry": False,
    }


def _screen_quote(transport, now: float, mint: str, evidence: dict) -> dict:
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
    intent = _quote_intent(mint, identity["pool"])
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


def _account_rent(transport, mint: str) -> dict:
    """Rent-exemption lamports for the token accounts this entry would create.

    A missing read is a refusal. An account that is absent is counted. An
    account that is already a token account is not counted again.
    """
    import phantom_transport
    import solana_tx
    refused = {"ok": False, "reason": "rent_unpriced"}
    try:
        response = transport({"kind": "rpc", "method": "getMinimumBalanceForRentExemption", "size": solana_tx.TOKEN_ACCOUNT_SPACE})
    except Exception:
        return refused
    body = response.get("body") if isinstance(response, dict) else None
    each = body.get("result") if isinstance(body, dict) else None
    if isinstance(each, bool) or not isinstance(each, int) or each < 1:
        return refused
    wallet = phantom_transport.REQUIRED_WALLET
    keys = [
        solana_tx.associated_token_address(wallet, solana_tx.NATIVE_MINT, solana_tx.TOKEN_PROGRAM),
        solana_tx.associated_token_address(wallet, mint, solana_tx.TOKEN_PROGRAM),
    ]
    try:
        listed = transport({"kind": "rpc", "method": "getMultipleAccounts", "accounts": keys})
    except Exception:
        return refused
    listed_body = listed.get("body") if isinstance(listed, dict) else None
    result = listed_body.get("result") if isinstance(listed_body, dict) else None
    value = result.get("value") if isinstance(result, dict) else None
    if not isinstance(value, list) or len(value) != len(keys):
        return refused
    count = 0
    for item in value:
        if item is None:
            count += 1
            continue
        if isinstance(item, dict) and item.get("owner") == solana_tx.TOKEN_PROGRAM:
            continue
        return refused
    return {"ok": True, "lamports": each * count, "each": each, "count": count, "space": solana_tx.TOKEN_ACCOUNT_SPACE}


def _receipt_remaining(case_id: str, snap: dict) -> int | None:
    """Token quantity still held, from the entry receipt minus booked exit slices."""
    import db
    funding = snap.get("entry_funding") if isinstance(snap.get("entry_funding"), dict) else {}
    if funding.get("source") != "entry_receipt" or funding.get("proven") is not True:
        return None
    signature = funding.get("signature")
    if not isinstance(signature, str):
        return None
    try:
        rows = db.query(
            "SELECT qty FROM memecoin_entry_receipts WHERE signature = ? AND case_id = ? AND failed = 0",
            (signature, case_id),
        )
    except Exception:
        return None
    if len(rows) != 1 or isinstance(rows[0]["qty"], bool) or not isinstance(rows[0]["qty"], int):
        return None
    sold = 0
    for item in snap.get("live_receipts") or []:
        if isinstance(item, dict) and item.get("exit_chain_proven") is True and isinstance(item.get("qty"), int) and not isinstance(item.get("qty"), bool):
            sold += item["qty"]
    left = rows[0]["qty"] - sold
    return left if left > 0 else None


def _quote_intent(mint: str, pool: str) -> dict:
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
    if stage in {"filled", "partial"}:
        left = _receipt_remaining(rows[0]["id"], snap)
        if left is None:
            return refused | {"reason": "exit_receipt_unproven"}
        return {"ok": True, "side": "exit", "amount": left, "input_mint": mint, "output_mint": solana_tx.NATIVE_MINT, "a_to_b": False, "case_id": rows[0]["id"], "pool": pool}
    amount = snap.get("entry_amount_raw")
    if stage == "protected" and isinstance(amount, int) and not isinstance(amount, bool) and amount > 0:
        return {"ok": True, "side": "entry", "amount": amount, "input_mint": solana_tx.NATIVE_MINT, "output_mint": mint, "a_to_b": True, "case_id": rows[0]["id"], "pool": pool}
    return refused


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


