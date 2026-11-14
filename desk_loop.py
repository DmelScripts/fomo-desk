"""One server-owned pass of the six-role desk.

The loop arms only when the owner has authorized it and RugCheck, Helius, and
Solana Tracker are configured in this process. Arming does not sign or send.
Per-token reads still fail closed when that mint's data is missing.

Scout reads DexScreener's documented token-pairs response. Filter reads the
mint account from the public RPC. LP lock, rug history, holder growth,
bundles, and dev holding are required only through the RugCheck, Helius, and
Solana Tracker live gates. Thành removed the StalkChain readiness gate on
2026-11-11 at 22:23 Asia/Saigon. Unique buyers and smart wallets stay scored 0.
Caller booleans are not accepted here.

Hermes latest prices require a Pyth API key since 26 August 2026. The keyless
feed list is metadata, not a price, and this module does not call it.
https://docs.pyth.network/llms-price-feeds-core.txt
https://docs.pyth.network/api-reference/hermes/price-feeds
"""

from __future__ import annotations

# Production authorization is a settings row, default absent. Readiness is not met.
_runtime = {
    "running": False,
    "started": False,
    "passes": 0,
    "faults": 0,
    "server_loop": False,
    "loop_started_at": None,
    "last_pass_started_at": None,
    "last_pass_ended_at": None,
    "last_error": None,
    "last_pass": {
        "candidates": 0,
        "filter_rejects": {},
        "analyst_scored": 0,
        "analyst_passed": 0,
        "sims_attempted": 0,
        "sims_ok": 0,
        "sims_failed": 0,
        "pool_checks": 0,
        "paper_entries": 0,
    },
}
_EVAL_KEEP = 200

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
    """Provider configuration for the loop. A missing per-token read is not one of these.

    LP comes from the wired RugCheck adapter. Rug history also needs the Helius
    key. Holder growth needs the Solana Tracker key. StalkChain is not a
    readiness input. The live gates stay on their own observed reads.
    """
    reasons = []
    import owner_slippage
    if owner_slippage.ceiling_bps() != owner_slippage.BPS:
        reasons.append("owner_slippage_unapproved")
    providers = _scheduler_providers()
    if not providers["lp"]:
        reasons.append("lp_provider_unready")
    if not providers["rug_history"]:
        reasons.append("rug_history_provider_unready")
    if not providers["holder_growth"]:
        reasons.append("holder_growth_provider_unready")
    if db_setting("memecoin_entry_halt") == "1":
        reasons.append("entry_halt")
    if not providers["helius_key"]:
        reasons.append("helius_api_key_missing")
    if not providers["tracker_key"]:
        reasons.append("solana_tracker_api_key_missing")
    return reasons


def _scheduler_providers() -> dict:
    """Which readers this process can call. A key is not a per-token proof."""
    import cross_source
    import desk_providers
    wired = cross_source.fetcher_is(desk_providers.production_fetch)
    tracker_key = _process_has_env("SOLANA_TRACKER_API_KEY")
    helius_key = _process_has_env("HELIUS_API_KEY")
    rugcheck = wired and desk_providers.configured().get("rugcheck") is True
    return {
        "lp": rugcheck,
        "rug_history": rugcheck and helius_key,
        "holder_growth": wired and tracker_key,
        "helius_key": helius_key,
        "tracker_key": tracker_key,
    }


def _process_has_env(name: str) -> bool:
    """Whether this process has a non-empty value. The value is not returned."""
    import os
    value = os.environ.get(name)
    return isinstance(value, str) and bool(value.strip())


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
        "server_loop": _runtime["server_loop"] is True,
        "scheduled": _runtime["server_loop"] is True,
        "loop_started_at": _runtime["loop_started_at"],
        "last_pass_started_at": _runtime["last_pass_started_at"],
        "last_pass_ended_at": _runtime["last_pass_ended_at"],
        "last_error": _runtime["last_error"],
        "last_pass": _pass_snapshot(),
        "send": False,
        "sign": False,
        "live_ready": False,
        "agents_enabled": False,
    }


def _pass_snapshot() -> dict:
    body = dict(_runtime["last_pass"])
    rejects = body.get("filter_rejects")
    body["filter_rejects"] = dict(rejects) if isinstance(rejects, dict) else {}
    return body


def configure_observe_logs() -> None:
    """Send loop and provider lines to stdout. Uvicorn does not attach those loggers."""
    import logging
    import sys
    formatter = logging.Formatter("%(levelname)s %(name)s %(message)s")
    for name in ("desk.loop", "desk.provider"):
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        if any(getattr(item, "_desk_stdout", False) for item in logger.handlers):
            continue
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(logging.INFO)
        handler.setFormatter(formatter)
        handler._desk_stdout = True
        logger.addHandler(handler)


def note_provider(provider: str, reason: str) -> None:
    """One INFO line for a provider failure. The reason is a code, never a URL or a key."""
    import logging
    if not isinstance(provider, str) or not isinstance(reason, str) or reason == "":
        return
    if "://" in reason or "api-key" in reason.lower() or any(char.isspace() for char in reason):
        reason = "redacted"
    if len(reason) > 80:
        reason = reason[:80]
    logging.getLogger("desk.provider").info("provider %s %s", provider, reason)


def begin_server_loop(now: float) -> None:
    _runtime["server_loop"] = True
    _runtime["started"] = True
    _runtime["running"] = False
    if _runtime["loop_started_at"] is None:
        _runtime["loop_started_at"] = float(now)


def end_server_loop() -> None:
    _runtime["server_loop"] = False
    _runtime["started"] = False
    _runtime["running"] = False


def begin_pass(now: float) -> None:
    _runtime["running"] = True
    _runtime["last_pass_started_at"] = float(now)


def end_pass(now: float, result, error: str | None) -> None:
    """Record one discovery pass. This does not trade, sign, or send."""
    import logging
    _runtime["running"] = False
    _runtime["last_pass_ended_at"] = float(now)
    _runtime["passes"] = int(_runtime["passes"]) + 1
    if isinstance(error, str) and error != "":
        _runtime["faults"] = int(_runtime["faults"]) + 1
        _runtime["last_error"] = error if "://" not in error and "api-key" not in error.lower() else "redacted"
    else:
        _runtime["last_error"] = None
    counts = _pass_counts(result)
    _runtime["last_pass"] = counts
    _store_evaluations(result, float(now))
    logging.getLogger("desk.loop").info(
        "pass candidates=%s filter_rejects=%s analyst_scored=%s analyst_passed=%s sims_attempted=%s sims_ok=%s sims_failed=%s pool_checks=%s paper_entries=%s error=%s",
        counts["candidates"],
        counts["filter_rejects"],
        counts["analyst_scored"],
        counts["analyst_passed"],
        counts["sims_attempted"],
        counts["sims_ok"],
        counts["sims_failed"],
        counts["pool_checks"],
        counts["paper_entries"],
        _runtime["last_error"],
    )


_FILTER_RULES = (
    ("liquidity_vs_mcap", {"liquidity_below_25pct_mcap", "liquidity_ratio", "liquidity_source_missing", "pool_missing", "pool_ambiguous"}),
    ("lp_burned_or_locked", {"lp"}),
    ("dev_rug_history", {"dev_rug_history", "dev_rug_history_unverified"}),
    ("holder_growth", {"holder_growth_unproven", "holder_growth_unknown"}),
    ("dev_holding", {"dev_holding", "creator_conflict"}),
    ("bundles", {"bundle", "bundle_unverified"}),
)


def _pass_counts(result) -> dict:
    counts = {
        "candidates": 0,
        "filter_rejects": {},
        "analyst_scored": 0,
        "analyst_passed": 0,
        "sims_attempted": 0,
        "sims_ok": 0,
        "sims_failed": 0,
        "pool_checks": 0,
        "paper_entries": 0,
    }
    if not isinstance(result, dict):
        return counts
    candidates = result.get("candidates")
    if type(candidates) is int and not isinstance(candidates, bool):
        counts["candidates"] = candidates
    screened = result.get("screened") if isinstance(result.get("screened"), list) else []
    rejects = {}
    filter_codes = {code for _name, codes in _FILTER_RULES for code in codes}
    for item in screened:
        if not isinstance(item, dict):
            continue
        stage = item.get("stage") if isinstance(item.get("stage"), str) else ""
        reason = item.get("reason") if isinstance(item.get("reason"), str) else None
        if isinstance(reason, str) and (reason in filter_codes or stage in {"filter", "rejected"}):
            rejects[reason] = rejects.get(reason, 0) + 1
        if stage == "analyst" or item.get("score_measured") is True or item.get("score") is not None:
            counts["analyst_scored"] += 1
            if reason is None:
                counts["analyst_passed"] += 1
        quote = item.get("quote") if isinstance(item.get("quote"), dict) else {}
        if quote.get("simulate") is True:
            counts["sims_attempted"] += 1
            if quote.get("ok") is True:
                counts["sims_ok"] += 1
            else:
                counts["sims_failed"] += 1
        elif quote.get("attempted") is True:
            counts["pool_checks"] += 1
        if stage in {"protected", "filled"} or item.get("paper_entry") is True:
            counts["paper_entries"] += 1
    counts["filter_rejects"] = rejects
    return counts


def _safe_code(value) -> str | None:
    if not isinstance(value, str) or value == "":
        return None
    if "://" in value or "api-key" in value.lower() or any(char.isspace() for char in value):
        return "redacted"
    return value[:80]


def _rule_status(reason: str | None, stage: str | None) -> dict:
    passed_filter = reason is None and stage in {"filter_passed", "analyst", "protected", "filled"}
    view = {}
    for name, codes in _FILTER_RULES:
        if passed_filter:
            view[name] = "pass"
        elif isinstance(reason, str) and reason in codes:
            view[name] = "fail"
        else:
            view[name] = "not_evaluated"
    return view


def _evaluation(item: dict, now: float) -> dict:
    stage = item.get("stage") if isinstance(item.get("stage"), str) else None
    reason = _safe_code(item.get("reason")) if isinstance(item.get("reason"), str) else None
    symbol = item.get("symbol") if isinstance(item.get("symbol"), str) and 0 < len(item["symbol"]) <= 32 else None
    quote = item.get("quote") if isinstance(item.get("quote"), dict) else {}
    simulated = quote.get("attempted") is True or quote.get("simulate") is True
    sim_reason = quote.get("cause") if isinstance(quote.get("cause"), str) else quote.get("reason") if isinstance(quote.get("reason"), str) else None
    score = item.get("score")
    if type(score) not in {int, float, str} or isinstance(score, bool):
        score = None
    breakdown = {}
    raw_breakdown = item.get("breakdown") if isinstance(item.get("breakdown"), dict) else {}
    for key, value in raw_breakdown.items():
        if isinstance(key, str) and len(key) <= 40 and type(value) in {int, float, str} and not isinstance(value, bool):
            breakdown[key] = value if type(value) is not str else value[:40]
    provider_errors = []
    if isinstance(reason, str):
        for name in ("helius", "rugcheck", "solana_tracker", "jupiter", "simulate"):
            if name in reason:
                provider_errors.append({"provider": name, "reason": reason})
                break
    return {
        "mint": item.get("mint") if isinstance(item.get("mint"), str) else None,
        "symbol": symbol,
        "time": now,
        "stage": stage,
        "passed": reason is None and stage in {"filter_passed", "analyst", "protected", "filled"},
        "reason": reason,
        "filter": _rule_status(reason, stage),
        "analyst": {
            "scored": item.get("score_measured") is True or score is not None,
            "passed": reason is None and stage in {"analyst", "protected", "filled"},
            "score": None if score is None else str(score)[:32],
            "breakdown": breakdown,
        },
        "provider_errors": provider_errors,
        "simulation": {
            "attempted": simulated,
            "ok": quote.get("ok") is True if simulated else None,
            "reason": _safe_code(sim_reason) if isinstance(sim_reason, str) else None,
        },
        "paper_entry": stage in {"protected", "filled"} or item.get("paper_entry") is True,
        "pair_address": item.get("pair_address") if isinstance(item.get("pair_address"), str) else None,
        "liquidity_usd": item.get("liquidity_usd") if isinstance(item.get("liquidity_usd"), str) else None,
        "candidate_pools": item.get("candidate_pools") if type(item.get("candidate_pools")) is int and not isinstance(item.get("candidate_pools"), bool) else None,
        "send": False,
        "sign": False,
    }


def _store_evaluations(result, now: float) -> None:
    import json
    import db
    screened = result.get("screened") if isinstance(result, dict) and isinstance(result.get("screened"), list) else []
    rows = [_evaluation(item, now) for item in screened if isinstance(item, dict)]
    if not rows:
        return

    def work(conn):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memecoin_evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mint TEXT,
                symbol TEXT,
                evaluated_at REAL NOT NULL,
                stage TEXT,
                passed INTEGER NOT NULL,
                reason TEXT,
                body_json TEXT NOT NULL
            )
            """
        )
        for row in rows:
            conn.execute(
                """
                INSERT INTO memecoin_evaluations (mint, symbol, evaluated_at, stage, passed, reason, body_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["mint"], row["symbol"], row["time"], row["stage"],
                    1 if row["passed"] is True else 0, row["reason"],
                    json.dumps(row, sort_keys=True, default=str),
                ),
            )
        conn.execute(
            """
            DELETE FROM memecoin_evaluations WHERE id NOT IN (
                SELECT id FROM memecoin_evaluations ORDER BY id DESC LIMIT ?
            )
            """,
            (_EVAL_KEEP,),
        )

    try:
        db.exclusive(work)
    except Exception:
        return


def recent_evaluations(limit: int = 50) -> dict:
    """Newest evaluations first. Bounded. No secrets and no signing."""
    import json
    import db
    if isinstance(limit, bool) or type(limit) is not int or limit < 1:
        limit = 50
    limit = min(limit, 100)
    shown = {"rows": [], "limit": limit, "send": False, "sign": False, "live_ready": False}
    try:
        rows = db.query(
            "SELECT body_json FROM memecoin_evaluations ORDER BY id DESC LIMIT ?",
            (limit,),
        )
    except Exception:
        return shown
    for row in rows:
        try:
            body = json.loads(row["body_json"])
        except Exception:
            continue
        if isinstance(body, dict):
            body["send"] = False
            body["sign"] = False
            shown["rows"].append(body)
    return shown


def run_discovery(transport, now: float) -> dict:
    """Pick one mint from the documented boost list, then hand it to the six stages.

    A caller does not name the mint. The default process does not call this.
    """
    stopped = scheduler_status() | {"ok": False, "send": False, "live_ready": False}
    if transport is None:
        return stopped | {"reason": "network_disabled", "mint": None, "reservations": []}
    reservations = _supervise_reservations(transport, now)
    positions = _supervise_open(transport, now)
    try:
        response = transport({"kind": "http", "url": dexscreener_public.BOOSTS_URL})
    except Exception:
        _halt_entries()
        return stopped | {"reason": "discovery_fault", "entry_halt": True, "mint": None, "positions": positions, "reservations": reservations}
    if not isinstance(response, dict) or response.get("status") != 200:
        return stopped | {"reason": "dexscreener_status", "mint": None, "positions": positions, "reservations": reservations}
    selected = dexscreener_public.parse_boost_mints(response.get("body"))
    if selected.get("ok") is not True:
        return stopped | {"reason": selected.get("reason"), "source": selected.get("source"), "mint": None, "caller_named_mint": False, "kills": [], "positions": positions, "reservations": reservations}
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
            "source_fields": result.get("source_fields"),
            "capability": result.get("capability"),
            "stalkchain": result.get("stalkchain"),
            "observed": result.get("observed"),
            "score_measured": False,
            "fixture_is_mainnet_proof": False,
            "pair_address": result.get("pair_address"),
            "liquidity_usd": result.get("liquidity_usd") if isinstance(result.get("liquidity_usd"), str) else None,
            "candidate_pools": result.get("candidate_pools") if type(result.get("candidate_pools")) is int else None,
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
        "reservations": reservations,
        "ranked": [item.get("mint") for item in ranked],
        "score_measured": False,
        "holder_growth_unproven": True,
    }


def _fetch_lp_locked_pct(transport, mint: str) -> dict:
    """Keyless RugCheck summary. A missing or unparsed body is not 100 percent locked."""
    import public_filter_docs
    url = "https://api.rugcheck.xyz/v1/tokens/" + mint + "/report/summary"
    try:
        response = transport({"kind": "http", "method": "GET", "url": url})
    except Exception:
        return {"ok": False, "reason": "lp", "fetched": False}
    body = response.get("body") if isinstance(response, dict) and response.get("status") == 200 else None
    parsed = public_filter_docs.parse_lp_locked_pct(body if isinstance(body, dict) else {})
    return parsed | {"fetched": isinstance(response, dict)}


def _read_observed(transport, mint: str) -> dict:
    """An explicit observed-MCP fixture. A REST body is not this channel."""
    import stalkchain_observed
    blank = {"supplied": False, "live_ready": False, "score_measured": False, "fixture_is_mainnet_proof": False, "rest_envelope_verified": False}
    try:
        response = transport({"kind": "observed_mcp", "mint": mint})
    except Exception:
        return blank | {"reason": "not_supplied"}
    if not isinstance(response, dict):
        return blank | {"reason": "not_supplied"}
    body = response.get("body")
    if not isinstance(body, dict) or body.get("kind") != stalkchain_observed.KIND:
        return blank | {"reason": "not_supplied"}
    mapped = stalkchain_observed.map_fixture(body, mint)
    return blank | mapped | {"supplied": True}


def collect_holder_count(mint: str, reading: dict) -> dict:
    """Write a holder count only from a same-source series this desk can verify.

    StalkChain has not published the holder-count field. DexScreener transaction
    counts are not holders. Nothing is invented and nothing is purchased.
    """
    if not isinstance(reading, dict):
        return {"ok": False, "written": False, "reason": "holder_count_source_unpublished", "score_measured": False}
    if reading.get("source") == "solana_tracker_holders_chart":
        import public_filter_docs
        parsed = public_filter_docs.parse_holder_chart(reading.get("body"), mint)
        if parsed.get("ok") is not True:
            return {"ok": False, "written": False, "booked": False, "reason": "holder_growth_unknown", "score_measured": False}
        return parsed | {"written": False, "booked": False}
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
    return memecoin_desk._num(value)


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
        return stopped | {
            "reason": parsed.get("reason"),
            "source": dexscreener_public.SOURCE,
            "stage": "scout",
            "fdv_used_as_market_cap": False,
            "pair_address": parsed.get("pair_address"),
            "liquidity_usd": parsed.get("liquidity_usd") if isinstance(parsed.get("liquidity_usd"), str) else None,
            "candidate_pools": parsed.get("candidate_pools") if type(parsed.get("candidate_pools")) is int else None,
        }
    _observe(mint, dexscreener_public.SOURCE, "market_cap_usd", parsed["scout"]["market_cap_usd"], now)
    _observe(mint, dexscreener_public.SOURCE, "liquidity_usd", parsed["scout"]["liquidity_usd"], now)
    opened = memecoin_desk.scout(parsed["scout"], now=now)
    if opened.get("ok") is not True:
        quote = _screen_quote(transport, now, mint, parsed.get("evidence") or {})
        return stopped | {
            "reason": opened.get("reason"),
            "stage": opened.get("stage") if isinstance(opened.get("stage"), str) and opened.get("stage") != "" else "scout",
            "case_id": opened.get("case_id"),
            "quote": quote,
            "jupiter_called": quote.get("jupiter_called") is True,
            "score_measured": False,
            "quote_expiry_proven": False,
            "sources_are_live_mcp": False,
            "pair_address": opened.get("pair_address"),
            "liquidity_usd": opened.get("liquidity_usd") if isinstance(opened.get("liquidity_usd"), str) else None,
            "candidate_pools": opened.get("candidate_pools") if type(opened.get("candidate_pools")) is int else None,
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
    observed = _read_observed(transport, mint)
    onchain = {"source": solana_mint_read.SOURCE, "fetched_at": now}
    if account.get("ok") is True:
        onchain["mint_authority_revoked"] = account["mint_authority_revoked"]
        onchain["freeze_authority_revoked"] = account["freeze_authority_revoked"]
    source_reason = coverage.get("reason")
    source_blocks_lp = source_reason in {
        "stalkchain_credentials_missing", "stalkchain_transport_missing", "stalkchain_transport",
        "host_refused", "method_refused", "tool_refused", "body_refused", "redirect_refused",
        "stalkchain_unauthorized", "stalkchain_credit_limit", "stalkchain_status",
        "envelope", "mint_mismatch", "response_fields_unpublished",
    }
    authorities_clear = account.get("ok") is True and account.get("mint_authority_revoked") is True and account.get("freeze_authority_revoked") is True
    if authorities_clear:
        locked = _fetch_lp_locked_pct(transport, mint)
        if locked.get("ok") is not True:
            return stopped | {
                "reason": "lp", "stage": "filter", "case_id": opened["case_id"],
                "lp_fetched": locked.get("fetched") is True, "lp_source": "rugcheck_summary",
                "rest_envelope_verified": False, "live_ready": False, "send": False,
            }
        onchain["lpLockedPct"] = 100
    if authorities_clear and source_blocks_lp:
        reported = source_reason
        stage = opened.get("stage")
    else:
        filtered = memecoin_desk.filter_case(opened["case_id"], onchain, now=now)
        reported = filtered.get("reason") or "features_unpublished"
        stage = filtered.get("stage")
    blocked = {"attempted": False, "jupiter_called": False, "score_measured": False, "reason": reported, "send": False, "live_ready": False, "sources_are_live_mcp": False}
    return stopped | {
        "reason": reported,
        "capability": coverage.get("capability"),
        "stage": stage,
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
        "source_fields": coverage.get("fields"),
        "observed": observed,
        "fixture_is_mainnet_proof": False,
        "analyst": memecoin_desk.analyst_decision(),
    }


_AGGREGATOR_ROUTERS = {"metis", "dflow", "okx"}
_RFQ_ROUTERS = {"jupiterz"}


def _rpc_result(response):
    body = response.get("body") if isinstance(response, dict) else None
    if not isinstance(body, dict):
        return None
    return body.get("result")


def read_message_validity(transport, blockhash: str, now: float) -> dict:
    """Confirm the exact message hash. The latest blockhash is not a substitute.

    isBlockhashValid and getBlockHeight use commitment confirmed. getLatestBlockhash
    is recorded beside them and is not paired with this message unless the hashes match.
    No owner wall-clock TTL is applied.
    """
    import solana_inspect
    import solana_tx
    refused = {
        "ok": False, "quote_expiry_proven": False, "price_freshness_proven": False,
        "inclusion_window_proven": False, "block_height_missing": True,
        "slot_is_block_height": False, "internal_time_limit": None,
        "internal_time_limit_is_owner_approved": False,
    }
    if transport is None or not isinstance(blockhash, str) or blockhash == "":
        return refused | {"reason": "blockhash_missing"}
    try:
        raw = solana_tx.b58decode(blockhash)
    except Exception:
        return refused | {"reason": "blockhash_malformed"}
    if len(raw) != 32:
        return refused | {"reason": "blockhash_malformed"}
    if isinstance(now, bool) or not isinstance(now, (int, float)):
        return refused | {"reason": "block_clock_future"}
    try:
        genesis = transport({"kind": "rpc", "method": "getGenesisHash"})
        tip_response = transport({"kind": "rpc", "method": "getSlot"})
    except Exception:
        return refused | {"reason": "blockhash_validity_missing"}
    if _rpc_result(genesis) != solana_inspect.MAINNET_GENESIS:
        return refused | {"reason": "genesis"}
    tip = _rpc_result(tip_response)
    if isinstance(tip, bool) or not isinstance(tip, int) or tip <= 0:
        return refused | {"reason": "context_malformed"}
    try:
        valid = transport({"kind": "rpc", "method": "isBlockhashValid", "blockhash": blockhash, "minContextSlot": tip})
        height = transport({"kind": "rpc", "method": "getBlockHeight", "minContextSlot": tip})
        latest = transport({"kind": "rpc", "method": "getLatestBlockhash", "minContextSlot": tip})
    except Exception:
        return refused | {"reason": "blockhash_validity_missing"}
    validity = _rpc_result(valid)
    if not isinstance(validity, dict) or "value" not in validity:
        return refused | {"reason": "blockhash_validity_missing"}
    context_slot, context_reason = _rpc_context_slot(validity, tip)
    if context_reason is not None:
        return refused | {"reason": context_reason}
    bit = validity.get("value")
    if not isinstance(bit, bool):
        return refused | {"reason": "blockhash_validity_malformed"}
    current = _rpc_result(height)
    if isinstance(current, bool) or not isinstance(current, int) or current <= 0:
        return refused | {"reason": "block_height_malformed", "blockhash_valid": bit, "context_slot": context_slot}
    latest_result = _rpc_result(latest)
    latest_slot, latest_reason = _rpc_context_slot(latest_result, tip)
    if latest_reason is not None:
        return refused | {"reason": latest_reason, "blockhash_valid": bit}
    latest_value = latest_result.get("value") if isinstance(latest_result, dict) else None
    latest_hash = latest_value.get("blockhash") if isinstance(latest_value, dict) else None
    latest_last = latest_value.get("lastValidBlockHeight") if isinstance(latest_value, dict) else None
    observed = {
        "block_height": current, "blockhash_valid": bit, "context_slot": context_slot, "tip_slot": tip,
        "slot_is_block_height": False, "latest_blockhash": latest_hash,
        "latest_context_slot": latest_slot,
        "latest_last_valid_block_height": latest_last if isinstance(latest_last, int) and not isinstance(latest_last, bool) else None,
        "latest_blockhash_paired": latest_hash == blockhash,
        "latest_blockhash_provenance": "rpc_getLatestBlockhash",
        "blockhash_validity_source": "isBlockhashValid",
        "observed_at": float(now), "genesis": solana_inspect.MAINNET_GENESIS,
    }
    if bit is not True:
        return refused | observed | {"reason": "blockhash_invalid", "block_height_missing": False}
    return refused | observed | {"ok": True, "reason": None, "block_height_missing": False}


def _rpc_context_slot(result, tip: int) -> tuple:
    """The RPC context slot has to be a fresh integer against the server tip. It is not a block height."""
    import jupiter_route_v2
    if not isinstance(result, dict) or not isinstance(result.get("context"), dict) or "slot" not in result["context"]:
        return None, "context_missing"
    slot = result["context"].get("slot")
    if isinstance(slot, bool) or not isinstance(slot, int) or slot <= 0:
        return None, "context_malformed"
    if slot < tip:
        return slot, "context_stale"
    if slot > tip + jupiter_route_v2.SLOT_LAG_MAX:
        return slot, "context_future"
    return slot, None


def _bind_provider_window(window: dict, order: dict, message_blockhash: str, now: float) -> dict:
    """Provider fields stay the pair for this order. A newer RPC hash is not that pair."""
    blank = {"quote_expiry_proven": False, "price_freshness_proven": False, "inclusion_window_proven": False}
    if window.get("ok") is not True:
        return window | blank
    if not isinstance(order, dict):
        return window | blank | {"ok": False, "reason": "route_expiry_missing", "provider_quote_expiry_published": False}
    if order.get("provider_expiry_malformed") is True:
        return window | blank | {"ok": False, "reason": "block_height_malformed", "provider_quote_expiry_published": False}
    if not isinstance(order.get("transaction_b64"), str) or order.get("transaction_b64") == "":
        return window | blank | {"ok": False, "reason": "provider_transaction_unpublished", "provider_quote_expiry_published": order.get("provider_expiry_field") is not None}
    if not isinstance(message_blockhash, str) or message_blockhash == "":
        return window | blank | {"ok": False, "reason": "blockhash_malformed"}
    router = order.get("router")
    provider_height = order.get("last_valid_block_height")
    expire_at = order.get("expire_at")
    if router in _AGGREGATOR_ROUTERS:
        if provider_height is None:
            return window | blank | {"ok": False, "reason": "route_expiry_missing", "router": router, "provider_quote_expiry_published": False}
    elif router in _RFQ_ROUTERS:
        if expire_at is None:
            return window | blank | {"ok": False, "reason": "route_expiry_missing", "router": router, "provider_quote_expiry_published": False}
    else:
        return window | blank | {"ok": False, "reason": "route_expiry_unspecified", "router": router if isinstance(router, str) else None}
    if type(expire_at) is int and type(now) is not bool and (type(now) is int or type(now) is float) and float(now) > float(expire_at):
        return window | blank | {"ok": False, "reason": "quote_expired", "router": router, "expire_at": expire_at, "provider_quote_expiry_published": True, "expire_at_enforced": True}
    if isinstance(provider_height, int) and not isinstance(provider_height, bool) and window.get("block_height") > provider_height:
        return window | blank | {
            "ok": False, "reason": "blockhash_expired", "router": router,
            "last_valid_block_height": provider_height, "last_valid_provenance": "jupiter_order",
            "provider_quote_expiry_published": True, "block_height_missing": False,
        }
    inclusion = isinstance(provider_height, int) and not isinstance(provider_height, bool) and window.get("block_height") <= provider_height
    return window | {
        "ok": True, "bind_ok": True, "reason": None, "router": router,
        "blockhash": message_blockhash, "blockhash_provenance": "jupiter_order.transaction",
        "last_valid_block_height": provider_height, "last_valid_provenance": "jupiter_order" if provider_height is not None else None,
        "expire_at": expire_at, "expire_at_enforced": expire_at is not None,
        "request_id": order.get("request_id"),
        "provider_expiry_field": order.get("provider_expiry_field"),
        "provider_quote_expiry_published": order.get("provider_expiry_field") is not None,
        "inclusion_window_proven": inclusion, "height_proof": inclusion,
        "blockhash_still_valid": True, "price_freshness_proven": False,
        "quote_expiry_proven": False, "gate_complete": False,
        "quote_expiry_source": None,
        "inclusion_source": "jupiter_order.lastValidBlockHeight+getBlockHeight+isBlockhashValid" if inclusion else "isBlockhashValid",
    }


def _current_deposit(transport, decoded, blockhash: str) -> dict:
    """The deposit on this exact message. A stored preflight attempt is not read."""
    try:
        tip_response = transport({"method": "getSlot"})
    except Exception:
        return _deposit_blank(deposit_reason="deposit_transport")
    tip_body = tip_response.get("body") if isinstance(tip_response, dict) else None
    tip = tip_body.get("result") if isinstance(tip_body, dict) else None
    if isinstance(tip, bool) or not isinstance(tip, int) or tip <= 0:
        return _deposit_blank(deposit_reason="deposit_context_missing")
    return _deposit_hold(transport, decoded, tip, blockhash)


def _deposit_permission(deposit: dict, *, operation: str = "unimplemented") -> dict:
    """A deposit observation is not permission to sign or send.

    Unsafe, malformed, unpriced, and unpinned results all fail closed. An
    operation label is not evidence and cannot grant permission.
    """
    reason = deposit.get("deposit_reason") if isinstance(deposit, dict) else None
    ready = (
        isinstance(deposit, dict)
        and deposit.get("rent_bound_proven") is True
        and deposit.get("ticket_limit_proven") is True
        and deposit.get("allocation_proven") is True
        and reason is None
    )
    label = operation if isinstance(operation, str) and operation != "" else "unimplemented"
    return {
        "execution_eligible": ready is True,
        "sign": False,
        "send": False,
        "operation": label,
        "operation_grants_permission": False,
        "execution_blocker": None if ready else (reason or "deposit_unobserved"),
        "deposit_safety_refusal": ready is not True and reason != "deposit_hold_unpinned",
    }


def _u64_text(value):
    """Canonical decimal for 1 .. 2**64-1. A float is not a lamport amount."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 1 or value >= 2**64:
        return None
    return str(value)


def _u64_parse(value):
    if isinstance(value, bool) or isinstance(value, float):
        return None
    if isinstance(value, int):
        if 1 <= value <= 2**63 - 1:
            return value
        return None
    if isinstance(value, str) and value.isascii() and value.isdigit() and not value.startswith("0"):
        if len(value) > 20:
            return None
        number = int(value)
        if 1 <= number < 2**64 and str(number) == value:
            return number
    return None


def _liability_rows(message_sha256: str) -> list[tuple[str, int, int]]:
    """Account, exact lamports, and row order from both storages."""
    import db
    found = []
    for row in db.query(
        "SELECT account, observed_lamports, id FROM memecoin_deposit_observations WHERE message_sha256 = ? ORDER BY id",
        (message_sha256,),
    ):
        amount = _u64_parse(row["observed_lamports"])
        if amount is None:
            continue
        found.append((row["account"], amount, int(row["id"])))
    for row in db.query(
        "SELECT account, observed_exact, id FROM memecoin_deposit_exact WHERE message_sha256 = ? ORDER BY id",
        (message_sha256,),
    ):
        amount = _u64_parse(row["observed_exact"])
        if amount is None:
            continue
        found.append((row["account"], amount, 10**12 + int(row["id"])))
    return found


def deposit_liability(message_sha256: str) -> dict:
    """Potential lamports already observed for this message. This is not a debit or a refund."""
    import memecoin_desk
    memecoin_desk._ensure()
    rows = _liability_rows(message_sha256)
    by_account = {}
    latest = None
    latest_order = -1
    for account, amount, order in rows:
        by_account[account] = max(by_account.get(account, 0), amount)
        if order >= latest_order:
            latest = amount
            latest_order = order
    return {
        "potential_lamports": sum(by_account.values()) if by_account else 0,
        "latest_observed_lamports": latest,
        "observation_count": len(rows),
        "account_count": len(by_account),
        "landed_lamports": None,
        "refunded_lamports": None,
        "funds_spent": False,
        "rent_bound_proven": False,
        "permission_from_ledger": False,
        "refund_credited": False,
        "liability_kind": None if not rows else "potential",
    }


def record_deposit_liability(case_id: str, message_sha256: str, transport, now: float) -> dict:
    """Read the frozen message and the current rent. A caller line is not an argument.

    The transport has to be callable. One account read produces the observation
    that is stored and returned. This does not call the execution gate.
    """
    import base64
    import db
    import hashlib
    import memecoin_desk
    import sqlite3
    import solana_tx
    quiet = _deposit_blank(deposit_reason="deposit_source", recorded=False)
    if not callable(transport):
        return quiet
    if not isinstance(message_sha256, str) or message_sha256 == "":
        return _deposit_blank(deposit_reason="deposit_unobserved", recorded=False)
    memecoin_desk._ensure()
    rows = db.query("SELECT * FROM memecoin_reservations WHERE message_sha256 = ?", (message_sha256,))
    if len(rows) != 1 or rows[0]["case_id"] != case_id:
        return _deposit_blank(deposit_reason="deposit_unobserved", recorded=False)
    proof = rows[0]
    try:
        decoded = solana_tx.decode_transaction(base64.b64decode(proof["message_b64"], validate=True))
        again = hashlib.sha256(solana_tx.message_bytes(proof["message_b64"])).hexdigest()
    except Exception:
        return _deposit_blank(deposit_reason="deposit_unobserved", recorded=False)
    if again != message_sha256 or decoded.blockhash != proof["blockhash"] or proof["side"] not in {"entry", "exit"}:
        return _deposit_blank(deposit_reason="deposit_unobserved", recorded=False)
    plan = _classic_deposit_plan(decoded)
    hold = _current_deposit(transport, decoded, proof["blockhash"])
    lines = hold.get("deposit_lines")
    public = {key: value for key, value in hold.items() if key != "deposit_lines"}
    if hold.get("deposit_observed") is not True or hold.get("deposit_reason") != "deposit_hold_unpinned":
        return public | deposit_liability(message_sha256) | _ledger_fields(message_sha256) | {"recorded": False}
    if plan.get("ok") is not True or not isinstance(lines, list) or not lines:
        return _deposit_blank(deposit_reason="deposit_unobserved", recorded=False) | _ledger_fields(message_sha256)
    allowed = {item["address"]: item for item in plan["creates"]}
    checked = []
    for line in lines:
        if not isinstance(line, dict):
            return _deposit_blank(deposit_reason="deposit_unobserved", recorded=False) | _ledger_fields(message_sha256)
        created = allowed.get(line.get("account"))
        text = _u64_text(line.get("rent_lamports"))
        if (
            created is None
            or created.get("mint") != line.get("mint")
            or created.get("payer") != line.get("payer")
            or created.get("space") != 165
            or line.get("space") != 165
            or line.get("observed_lamports") != line.get("rent_lamports")
            or text is None
            or not isinstance(line.get("context_slot"), int)
            or isinstance(line.get("context_slot"), bool)
            or line["context_slot"] <= 0
        ):
            return _deposit_blank(deposit_reason="deposit_unobserved", recorded=False) | _ledger_fields(message_sha256)
        checked.append((created, line, text))
    if sum(int(text) for _created, _line, text in checked) != hold.get("deposit_lamports"):
        return _deposit_blank(deposit_reason="deposit_unobserved", recorded=False) | _ledger_fields(message_sha256)

    def work(conn):
        for created, line, text in checked:
            prior = conn.execute(
                """
                SELECT 1 FROM memecoin_deposit_exact
                WHERE message_sha256 = ? AND account = ? AND context_slot = ? AND rent_exact = ? AND deposit_reason = ?
                """,
                (message_sha256, created["address"], line["context_slot"], text, "deposit_hold_unpinned"),
            ).fetchone()
            if prior is not None:
                continue
            legacy = conn.execute(
                """
                SELECT observed_lamports FROM memecoin_deposit_observations
                WHERE message_sha256 = ? AND account = ? AND context_slot = ? AND deposit_reason = ?
                """,
                (message_sha256, created["address"], line["context_slot"], "deposit_hold_unpinned"),
            ).fetchone()
            if legacy is not None and _u64_text(legacy["observed_lamports"]) == text:
                continue
            conn.execute(
                """
                INSERT INTO memecoin_deposit_exact (
                    message_sha256, case_id, side, account, mint, payer, context_slot,
                    rent_exact, observed_exact, space, blockhash, deposit_reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 165, ?, 'deposit_hold_unpinned', ?)
                """,
                (
                    message_sha256, case_id, proof["side"], created["address"], created["mint"], created["payer"],
                    line["context_slot"], text, text, proof["blockhash"], float(now),
                ),
            )

    try:
        db.exclusive(work)
    except sqlite3.IntegrityError as exc:
        if "deposit_liability" not in str(exc):
            raise
    reported = public | _ledger_fields(message_sha256) | {"recorded": True}
    reported["deposit_lines"] = None
    reported.update(value_deposit_potential(message_sha256, transport, now))
    return reported


def _exact_ratio(numerator: int, places: int) -> str:
    """numerator / 10**places as a decimal string. No binary float and no context precision."""
    if isinstance(numerator, bool) or not isinstance(numerator, int) or isinstance(places, bool) or not isinstance(places, int):
        raise ValueError("ratio")
    if places < 0:
        raise ValueError("ratio")
    sign = "-" if numerator < 0 else ""
    remainder = -numerator if numerator < 0 else numerator
    scale = 10 ** places
    whole, frac = divmod(remainder, scale)
    if frac == 0:
        return sign + str(whole)
    digits = f"{frac:0{places}d}".rstrip("0")
    return sign + f"{whole}.{digits}"


def deposit_usd_bounds(lamports: int, price: int, conf: int, expo: int) -> dict | None:
    """Exact floor and ceiling for lamports at the existing price-plus-or-minus-confidence rate.

    The rate is (price ± confidence) * 10**exponent / 10**9. The exponent is the
    one the sponsored account already allows, -12 through 0. This is not a future bound.
    """
    if isinstance(lamports, bool) or isinstance(price, bool) or isinstance(conf, bool) or isinstance(expo, bool):
        return None
    if not isinstance(lamports, int) or not isinstance(price, int) or not isinstance(conf, int) or not isinstance(expo, int):
        return None
    if lamports < 1 or price <= 0 or conf < 0 or expo < -12 or expo > 0:
        return None
    if price - conf <= 0:
        return None
    places = 9 - expo
    ceiling_num = lamports * (price + conf)
    floor_num = lamports * (price - conf)
    unit_places = -expo
    unit_ceiling = price + conf
    unit_floor = price - conf
    if expo > 0:
        return None
    return {
        "ceiling": _exact_ratio(ceiling_num, places),
        "floor": _exact_ratio(floor_num, places),
        "upper": _exact_ratio(unit_ceiling, unit_places),
        "lower": _exact_ratio(unit_floor, unit_places),
    }


def value_deposit_potential(message_sha256: str, transport, now: float) -> dict:
    """Price this message's historical lamport maximum with one current SOL/USD read.

    The quantity is the stored maximum. The price is the read taken now. A caller
    price is not accepted. Distinct messages are not added together.
    """
    import pyth_sponsored
    quiet = {
        "deposit_potential_usd_ceiling": None,
        "deposit_potential_usd_floor": None,
        "deposit_valued_lamports": None,
        "deposit_price_used": False,
        "deposit_valuation_reason": "no_lamport_liability",
        "deposit_oracle_fresh": False,
        "deposit_oracle_source": None,
        "deposit_oracle_feed_id": None,
        "deposit_oracle_account": None,
        "deposit_oracle_price": None,
        "deposit_oracle_conf": None,
        "deposit_oracle_expo": None,
        "deposit_oracle_publish_time": None,
        "deposit_oracle_observed_at": None,
        "deposit_oracle_age_s": None,
        "deposit_oracle_slot": None,
        "deposit_oracle_upper": None,
        "deposit_oracle_lower": None,
        "deposit_price_is_future_guarantee": False,
    }
    ledger = deposit_liability(message_sha256)
    quantity = ledger["potential_lamports"]
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0:
        return quiet | {"deposit_valuation_reason": "deposit_unobserved"}
    if quantity == 0:
        return quiet
    if not callable(transport):
        return quiet | {"deposit_valuation_reason": "deposit_source", "deposit_valued_lamports": quantity}
    try:
        read = pyth_sponsored.read_accounts(transport, now)
    except Exception:
        return quiet | {"deposit_valuation_reason": "rpc_transport", "deposit_valued_lamports": quantity}
    sol = read.get("sol") if isinstance(read, dict) else None
    if not isinstance(sol, dict) or sol.get("fresh") is not True or sol.get("used") is not True:
        reason = sol.get("reason") if isinstance(sol, dict) and sol.get("reason") else (read.get("reason") if isinstance(read, dict) else None)
        return quiet | {
            "deposit_valuation_reason": reason or "sol_usd_unpriced",
            "deposit_valued_lamports": quantity,
            "deposit_oracle_source": "pyth_sponsored",
            "deposit_oracle_feed_id": pyth_sponsored.SOL_FEED,
            "deposit_oracle_account": pyth_sponsored.SOL_ACCOUNT,
            "deposit_oracle_slot": read.get("slot") if isinstance(read, dict) else None,
        }
    price = sol.get("price")
    conf = sol.get("conf")
    expo = sol.get("expo")
    if isinstance(price, bool) or isinstance(conf, bool) or isinstance(expo, bool):
        return quiet | {"deposit_valuation_reason": "valuation_scale", "deposit_valued_lamports": quantity}
    if not isinstance(price, int) or not isinstance(conf, int) or not isinstance(expo, int):
        return quiet | {"deposit_valuation_reason": "valuation_scale", "deposit_valued_lamports": quantity}
    if conf < 0 or expo < -12 or expo > 0:
        return quiet | {"deposit_valuation_reason": "valuation_scale", "deposit_valued_lamports": quantity}
    executable = read.get("account_executable")
    if not isinstance(executable, list) or len(executable) != 2 or any(flag is not False for flag in executable):
        return quiet | {"deposit_valuation_reason": "price_account_executable", "deposit_valued_lamports": quantity}
    bounds = deposit_usd_bounds(quantity, price, conf, expo)
    if bounds is None:
        return quiet | {"deposit_valuation_reason": "valuation_not_positive", "deposit_valued_lamports": quantity}
    return quiet | {
        "deposit_potential_usd_ceiling": bounds["ceiling"],
        "deposit_potential_usd_floor": bounds["floor"],
        "deposit_valued_lamports": quantity,
        "deposit_price_used": True,
        "deposit_valuation_reason": None,
        "deposit_oracle_fresh": True,
        "deposit_oracle_source": "pyth_sponsored",
        "deposit_oracle_feed_id": pyth_sponsored.SOL_FEED,
        "deposit_oracle_account": pyth_sponsored.SOL_ACCOUNT,
        "deposit_oracle_price": price,
        "deposit_oracle_conf": conf,
        "deposit_oracle_expo": expo,
        "deposit_oracle_publish_time": sol.get("publish_time"),
        "deposit_oracle_observed_at": read.get("observed_at"),
        "deposit_oracle_slot": read.get("slot"),
        "deposit_oracle_upper": bounds["upper"],
        "deposit_oracle_lower": bounds["lower"],
        "deposit_price_is_future_guarantee": False,
    }


def _snapshot_deposit_rows(conn) -> dict:
    """One transaction's view of both deposit tables and the identity rows they name."""
    exact = list(conn.execute(
        """
        SELECT message_sha256, case_id, side, account, observed_exact AS amount, 'exact' AS storage
        FROM memecoin_deposit_exact
        """
    ))
    legacy = list(conn.execute(
        """
        SELECT message_sha256, case_id, side, account, observed_lamports AS amount, 'legacy' AS storage
        FROM memecoin_deposit_observations
        """
    ))
    proofs = {
        row["message_sha256"]: row for row in conn.execute(
            "SELECT message_sha256, case_id, side, ledger, ledger_provenance FROM memecoin_reservations"
        )
    }
    cases = {
        row["id"]: row for row in conn.execute(
            "SELECT id, ledger, ledger_provenance FROM memecoin_cases"
        )
    }
    unknown = {
        (row["message_sha256"], row["case_id"]) for row in conn.execute(
            "SELECT message_sha256, case_id FROM memecoin_reservation_signatures WHERE kind = 'unknown'"
        )
    }
    return {"exact": exact, "legacy": legacy, "proofs": proofs, "cases": cases, "unknown": unknown}


def _message_potential(rows: list[dict]) -> dict | None:
    """Per-account maximum for one message. Repeated proofs and the two storages count once."""
    if not rows:
        return None
    case_ids = {row["case_id"] for row in rows}
    sides = {row["side"] for row in rows}
    if len(case_ids) != 1 or len(sides) != 1:
        return {
            "case_id": None, "side": None, "accounts": {}, "potential_lamports": 0,
            "observation_count": len(rows), "quarantine_reason": "identity_conflict",
        }
    accounts = {}
    for row in rows:
        amount = _u64_parse(row["amount"])
        if amount is None:
            return {
                "case_id": next(iter(case_ids)), "side": next(iter(sides)), "accounts": {},
                "potential_lamports": 0, "observation_count": len(rows),
                "quarantine_reason": "amount_unproven",
            }
        current = accounts.get(row["account"])
        accounts[row["account"]] = amount if current is None else max(current, amount)
    return {
        "case_id": next(iter(case_ids)),
        "side": next(iter(sides)),
        "accounts": accounts,
        "potential_lamports": sum(accounts.values()),
        "observation_count": len(rows),
        "quarantine_reason": None,
    }


def diagnose_deposit_potential(transport, now: float) -> dict:
    """Diagnostic gross of recorded deposit maxima. Not an open-liability or a cap.

    The rows are read in one transaction. One oracle read, if the quantities are
    nonzero, prices that frozen total. A missing refund reconciliation means the
    set is not complete. The same account in two messages stays in both.
    """
    import db
    import memecoin_desk
    import pyth_sponsored
    memecoin_desk._ensure()
    snap = db.exclusive(_snapshot_deposit_rows)
    grouped = {}
    for row in list(snap["exact"]) + list(snap["legacy"]):
        grouped.setdefault(row["message_sha256"], []).append(row)
    messages = []
    included = 0
    quarantined = 0
    by_account = {}
    for digest, rows in sorted(grouped.items()):
        built = _message_potential(rows)
        if built is None:
            continue
        proof = snap["proofs"].get(digest)
        case = None if built["case_id"] is None else snap["cases"].get(built["case_id"])
        reason = built["quarantine_reason"]
        if reason is None and proof is None:
            reason = "reservation_missing"
        elif reason is None and (
            proof["ledger"] != "live" or proof["ledger_provenance"] != "server"
            or proof["case_id"] != built["case_id"] or proof["side"] != built["side"]
        ):
            reason = "identity_unproven"
        elif reason is None and (case is None or case["ledger"] != "live" or case["ledger_provenance"] != "server"):
            reason = "identity_unproven"
        signature_unknown = (digest, built["case_id"]) in snap["unknown"]
        item = {
            "message_sha256": digest,
            "case_id": built["case_id"],
            "side": built["side"],
            "potential_lamports": built["potential_lamports"],
            "accounts": [{"account": key, "max_lamports": built["accounts"][key]} for key in sorted(built["accounts"])],
            "observation_count": built["observation_count"],
            "signature_unknown": signature_unknown,
            "included": reason is None,
            "quarantine_reason": reason,
            "refund_verified": False,
        }
        messages.append(item)
        bucket = included if reason is None else 0
        if reason is None:
            included += built["potential_lamports"]
        else:
            quarantined += built["potential_lamports"]
        del bucket
        for key, amount in built["accounts"].items():
            by_account.setdefault(key, []).append({"message_sha256": digest, "max_lamports": amount, "included": reason is None})
    shared = []
    for account, uses in sorted(by_account.items()):
        if len(uses) < 2:
            continue
        shared.append({
            "account": account,
            "messages": uses,
            "gross_lamports": sum(item["max_lamports"] for item in uses),
            "unique_seen_lamports": max(item["max_lamports"] for item in uses),
            "spent_twice": False,
            "shared_liability_released": False,
        })
    report = {
        "diagnostic": True,
        "completeness": False,
        "scope": "recorded_observations",
        "open_liability_reconciled": False,
        "message_count": len(messages),
        "included_message_count": sum(1 for item in messages if item["included"]),
        "messages": messages,
        "gross_lamports": included,
        "quarantined_lamports": quarantined,
        "quarantine_is_not_zero": quarantined > 0,
        "shared_accounts": shared,
        "shared_ambiguity": len(shared) > 0,
        "gross_counts_each_message": True,
        "unique_account_liability_released": False,
        "cap_proven": False,
        "ticket_limit_proven": False,
        "allocation_proven": False,
        "rent_bound_proven": False,
        "execution_eligible": False,
        "price_freshness_proven": False,
        "preflight_ready": False,
        "gate_complete": False,
        "funds_spent": False,
        "refund_credited": False,
        "added_to_usd_hold": False,
        "gross_usd_ceiling": None,
        "gross_usd_floor": None,
        "deposit_price_used": False,
        "deposit_valuation_reason": "no_lamport_liability" if included == 0 and quarantined == 0 else None,
        "deposit_oracle_price": None,
        "deposit_oracle_conf": None,
        "deposit_oracle_expo": None,
        "deposit_oracle_publish_time": None,
        "deposit_oracle_slot": None,
        "deposit_price_is_future_guarantee": False,
    }
    if included == 0 and quarantined == 0:
        return report
    if not callable(transport):
        report["deposit_valuation_reason"] = "deposit_source"
        return report
    try:
        read = pyth_sponsored.read_accounts(transport, now)
    except Exception:
        report["deposit_valuation_reason"] = "rpc_transport"
        return report
    if not isinstance(read, dict) or read.get("fetched") is not True or read.get("reason") in {"rpc_transport", "rpc_status", "rpc_shape"}:
        report["deposit_valuation_reason"] = read.get("reason") if isinstance(read, dict) and read.get("reason") else "rpc_transport"
        return report
    sol = read.get("sol")
    executable = read.get("account_executable")
    if not isinstance(executable, list) or len(executable) != 2 or any(flag is not False for flag in executable):
        report["deposit_valuation_reason"] = "price_account_executable"
        return report
    if not isinstance(sol, dict) or sol.get("fresh") is not True or sol.get("used") is not True:
        reason = sol.get("reason") if isinstance(sol, dict) and sol.get("reason") else (read.get("reason") if isinstance(read, dict) else None)
        report["deposit_valuation_reason"] = reason or "sol_usd_unpriced"
        return report
    price, conf, expo = sol.get("price"), sol.get("conf"), sol.get("expo")
    if included == 0:
        report["deposit_oracle_price"] = price
        report["deposit_oracle_conf"] = conf
        report["deposit_oracle_expo"] = expo
        report["deposit_oracle_publish_time"] = sol.get("publish_time")
        report["deposit_oracle_slot"] = read.get("slot")
        report["deposit_valuation_reason"] = "included_quantity_empty"
        report["deposit_price_used"] = False
        if quarantined > 0:
            held = deposit_usd_bounds(quarantined, price, conf, expo)
            report["quarantined_usd_ceiling"] = None if held is None else held["ceiling"]
            report["quarantined_usd_floor"] = None if held is None else held["floor"]
        return report
    priced = deposit_usd_bounds(included, price, conf, expo)
    if priced is None:
        report["deposit_valuation_reason"] = "valuation_not_positive"
        return report
    for item in messages:
        if not item["included"] or item["potential_lamports"] <= 0:
            continue
        item_price = deposit_usd_bounds(item["potential_lamports"], price, conf, expo)
        item["usd_ceiling"] = None if item_price is None else item_price["ceiling"]
        item["usd_floor"] = None if item_price is None else item_price["floor"]
    report.update({
        "gross_usd_ceiling": priced["ceiling"],
        "gross_usd_floor": priced["floor"],
        "deposit_price_used": True,
        "deposit_valuation_reason": None,
        "deposit_oracle_price": price,
        "deposit_oracle_conf": conf,
        "deposit_oracle_expo": expo,
        "deposit_oracle_publish_time": sol.get("publish_time"),
        "deposit_oracle_slot": read.get("slot"),
        "valued_lamports": included,
    })
    return report


def _ledger_fields(message_sha256: str) -> dict:
    ledger = deposit_liability(message_sha256)
    return {
        "deposit_potential_lamports": ledger["potential_lamports"],
        "deposit_latest_observed_lamports": ledger["latest_observed_lamports"],
        "deposit_observation_count": ledger["observation_count"],
        "deposit_landed_lamports": None,
        "deposit_refunded_lamports": None,
        "deposit_funds_spent": False,
        "deposit_permission_from_ledger": False,
    }


def execution_permission(case_id: str, message_sha256: str, transport, now: float, *, operation: str = "unimplemented") -> dict:
    """Re-read the exact message. Caller flags are not accepted."""
    gate = execution_gate(case_id, message_sha256, transport, now)
    refused = {
        "execution_eligible": False, "sign": False, "send": False,
        "operation": operation if isinstance(operation, str) and operation != "" else "unimplemented",
        "operation_grants_permission": False, "holds": True, "resend": False,
    }
    if gate.get("reservation_valid") is not True:
        return gate | refused
    permitted = gate | _deposit_permission(gate, operation=refused["operation"]) | {"reservation_valid": True, "holds": True, "resend": False}
    pre_sign = approach_signer(case_id, message_sha256, transport, now)
    return permitted | {"pre_sign": pre_sign, "sign": False, "send": False}


def execution_gate(case_id: str, message_sha256: str, transport, now: float) -> dict:
    """Reservation validity for one exact message. That is not permission to sign.

    The snapshot is not read. A failed proof does not rewrite the stored message
    or release the hold. Deposit evidence is read now and is not a stored attempt.
    """
    import hashlib
    import memecoin_desk
    import solana_tx
    base = {
        "execution_eligible": False, "sign": False, "send": False, "simulate": False,
        "operation": "unimplemented", "price_freshness_proven": False, "quote_expiry_proven": False,
        "inclusion_window_proven": False, "height_proof": False, "gate_complete": False,
        "expire_at_enforced": False, "holds": True, "resend": False, "rewritten": False,
        "requoted": False, "live_ready": False, "internal_time_limit": None,
        "reservation_valid": False, "execution_blocker": None,
        "operation_grants_permission": False, "deposit_safety_refusal": False,
    }
    if not isinstance(message_sha256, str) or message_sha256 == "":
        return base | {"reason": "message_mismatch", "case_id": case_id}
    memecoin_desk._ensure()
    import db
    rows = db.query("SELECT * FROM memecoin_reservations WHERE message_sha256 = ?", (message_sha256,))
    if len(rows) != 1 or rows[0]["case_id"] != case_id:
        owned = db.query("SELECT message_sha256 FROM memecoin_reservations WHERE case_id = ?", (case_id,))
        return base | {"reason": "message_mismatch" if owned or rows else "reservation_proof_missing", "case_id": case_id, "message_sha256": message_sha256}
    proof = rows[0]
    identity = {
        "case_id": case_id, "message_sha256": message_sha256, "side": proof["side"],
        "pool": proof["pool"], "intent_id": proof["intent_id"], "router": proof["router"],
        "reservation_id": proof["message_sha256"],
    }
    try:
        import base64
        decoded = solana_tx.decode_transaction(base64.b64decode(proof["message_b64"], validate=True))
        again = hashlib.sha256(solana_tx.message_bytes(proof["message_b64"])).hexdigest()
    except Exception:
        return base | identity | {"reason": "reservation_proof_malformed"}
    embedded = decoded.blockhash
    if again != proof["message_sha256"] or again != message_sha256 or embedded != proof["blockhash"] or proof["side"] not in {"entry", "exit"} or proof["case_id"] != case_id:
        return base | identity | {"reason": "reservation_proof_mismatch", "blockhash": embedded}
    if decoded.required_signatures != 1:
        return base | identity | {"reason": "extra_signer", "holds": True, "resend": False}
    agreement = db.exclusive(lambda conn: memecoin_desk._identity_agreement(conn, proof))
    if agreement != "match":
        return base | identity | {
            "reason": "reservation_identity_mismatch" if agreement == "mismatch" else "reservation_identity_missing",
            "holds": True, "resend": False,
        }
    router = proof["router"]
    stored_last = proof["last_valid_block_height"]
    expire_at = proof["expire_at"]
    if router in _AGGREGATOR_ROUTERS and (isinstance(stored_last, bool) or not isinstance(stored_last, int) or stored_last <= 0):
        return base | identity | {"reason": "reservation_proof_missing", "blockhash": embedded}
    if router in _RFQ_ROUTERS and (isinstance(expire_at, bool) or not isinstance(expire_at, int) or expire_at <= 0):
        return base | identity | {"reason": "reservation_proof_missing", "blockhash": embedded}
    if router not in _AGGREGATOR_ROUTERS | _RFQ_ROUTERS:
        return base | identity | {"reason": "reservation_proof_missing", "blockhash": embedded}
    if proof["side"] == "exit":
        intent = memecoin_desk._matching_intent(case_id)
        if intent is None or intent["pool"] != proof["pool"] or (proof["intent_id"] not in (None, intent["id"])):
            return base | identity | {"reason": "intent_unbound", "blockhash": embedded}
    status = db.exclusive(lambda conn: memecoin_desk._reservation_status(conn, proof))
    if status in {"active", "unknown"} and db.exclusive(lambda conn: memecoin_desk._open_reservation_count(conn, case_id, proof["side"])) > 1:
        return base | identity | {"reason": "reservation_ambiguous", "holds": True, "resend": False, "active": False}
    if status == "consumed":
        return base | identity | {"reason": "reservation_consumed", "blockhash": embedded, "active": False}
    if status == "failed":
        return base | identity | {"reason": "reservation_failed", "blockhash": embedded, "active": False}
    if status == "unknown":
        return base | identity | {"reason": "reservation_unknown", "blockhash": embedded, "active": True}
    validity = read_message_validity(transport, embedded, now)
    if validity.get("ok") is not True:
        return base | identity | {"reason": validity.get("reason") or "blockhash_validity_missing", "blockhash_valid": validity.get("blockhash_valid")}
    expired = isinstance(stored_last, int) and not isinstance(stored_last, bool) and validity.get("block_height") > stored_last
    quote_past = type(expire_at) is int and (type(now) is int or type(now) is float) and float(now) > float(expire_at)
    if expired or quote_past:
        return base | identity | {
            "reason": "quote_expired" if quote_past and not expired else "blockhash_expired",
            "blockhash_valid": validity.get("blockhash_valid"),
        }
    height_proof = isinstance(stored_last, int) and not isinstance(stored_last, bool) and validity.get("block_height") <= stored_last
    reservation_valid = height_proof if router in _AGGREGATOR_ROUTERS else True
    window = {
        "reservation_valid": reservation_valid,
        "reason": None if reservation_valid else "height_proof_missing",
        "blockhash": embedded,
        "last_valid_block_height": stored_last, "last_valid_provenance": proof["last_valid_provenance"],
        "block_height": validity.get("block_height"), "latest_blockhash": validity.get("latest_blockhash"),
        "latest_blockhash_paired": validity.get("latest_blockhash_paired") is True,
        "inclusion_window_proven": height_proof, "height_proof": height_proof,
        "blockhash_still_valid": True, "expire_at_enforced": isinstance(expire_at, int) and not isinstance(expire_at, bool),
        "blockhash_validity_source": "isBlockhashValid", "context_slot": validity.get("context_slot"),
    }
    if reservation_valid is not True:
        return base | identity | window
    observed = record_deposit_liability(case_id, message_sha256, transport, now)
    return base | identity | window | observed | _deposit_permission(observed)


def recheck_bound(case_id: str, message_sha256: str, transport, now: float) -> dict:
    """The supervision recheck. It does not replace the reserved message."""
    return execution_gate(case_id, message_sha256, transport, now)


def _preflight_report(**extra) -> dict:
    base = {
        "attempted": False, "simulate": False, "sign": False, "send": False,
        "simulation_clear": False, "preflight_ready": False, "gate_complete": False,
        "price_freshness_proven": False, "slippage_proven_by_simulation": False,
        "fee_usd_proven": False, "fee_lamports_match": False, "fee_composition_proven": False,
        "rent_bound_proven": False, "rent_unproven": True, "ticket_limit_proven": False,
        "account_validated": False, "observed_lamport_delta": None,
        "network_fee_arithmetic_match": False, "requested_priority_arithmetic_match": False,
        "rent_constant_arithmetic_match": False, "requested_compute_unit_limit": None,
        "requested_micro_lamports_per_cu": None, "requested_priority_lamports": None,
        "requested_priority_formula": None, "priority_fee_observed": False,
        "priority_uses_consumed_units": False, "base_fee_proven": False,
        "fee_for_message_observed": False, "fee_for_message_lamports": None,
        "fee_for_message_reason": None, "charged_fee_proven": False,
        "instruction_slippage_within_cap": False, "instruction_slippage_bps": None,
        "quoted_out_amount": None, "candidate_min_out": None, "api_sample_min_out": None,
        "min_out_bound_proven": False, "program_rounding_proven": False,
        "network_fee_instruction_proven": False, "network_lamports_instruction": None,
        "dex_fee_proven": False, "dex_fee_rate_observed": False, "dex_fee_rate": None,
        "dex_fee_estimate_raw": None, "dex_fee_estimate_proven": False,
        "dex_fee_one_shot_raw": None, "dex_fee_one_shot_is_bound": False,
        "dex_fee_quote_raw": None, "dex_fee_quote_partial": False,
        "dex_fee_quote_current_state": False, "dex_fee_quote_is_inclusion_bound": False,
        "dex_fee_input_ceiling_raw": None, "dex_fee_input_ceiling_unit": None,
        "dex_fee_input_ceiling_proven": False, "dex_fee_input_ceiling_tight": False,
        "dex_fee_input_ceiling_is_inclusion_bound": False, "dex_fee_step_count_is_maximum": False,
        "dex_fee_charged_proven": False, "adaptive_fee_unsupported": False,
        "input_transfer_fee_unsupported": False, "pool_fee_reason": None,
        "combined_fee_usd_proven": False, "allocation_proven": False,
        "deposit_observed": False, "deposit_lamports": None, "deposit_space": None,
        "deposit_account_count": None, "deposit_rent_each": None,
        "deposit_principal_lamports": None, "deposit_refund_credited": False,
        "deposit_counts_as_fee": False, "preexisting_close_refused": False,
        "deposit_reason": None,
        "lookup_unresolved": False, "instruction_unproven": True, "economic_reason": None,
        "sig_verify": False, "replace_recent_blockhash": False, "message_rewritten": False,
        "holds": True, "resend": False, "operation": "unimplemented", "reason": None,
    }
    return base | extra


def _decoded_account_data(data):
    """Decode an RPC account data pair. The encoding tag is not the payload."""
    import base64
    if isinstance(data, (str, bytes)) or not isinstance(data, (list, tuple)) or len(data) != 2:
        return None
    payload, encoding = data
    if isinstance(payload, bool) or not isinstance(payload, str) or encoding != "base64":
        return None
    try:
        return base64.b64decode(payload, validate=True)
    except Exception:
        return None


def _system_account_lamports(value):
    """Lamports for a plain system-owned wallet, after the data actually decodes.

    RPC data is [payload, "base64"]. The payload has to use the standard
    alphabet and padding. A normal fee-payer wallet has empty data. Non-empty
    data belongs to another system layout, such as a nonce, and is not read as
    this wallet. The lamport number is not fee composition or rent proof.
    """
    import solana_tx
    if not isinstance(value, dict):
        return None
    owner = value.get("owner")
    if not isinstance(owner, str) or owner != solana_tx.SYSTEM_PROGRAM:
        return None
    if value.get("executable") is not False:
        return None
    if _decoded_account_data(value.get("data")) != b"":
        return None
    lamports = value.get("lamports")
    if isinstance(lamports, bool) or not isinstance(lamports, int) or lamports < 0:
        return None
    return lamports


def _account_lamports(response, tip: int):
    body = response.get("body") if isinstance(response, dict) else None
    result = body.get("result") if isinstance(body, dict) else None
    if not isinstance(result, dict):
        return None
    context = result.get("context") if isinstance(result.get("context"), dict) else None
    slot = None if context is None else context.get("slot")
    if isinstance(slot, bool) or not isinstance(slot, int) or slot < tip or slot > tip + __import__("jupiter_route_v2").SLOT_LAG_MAX:
        return None
    return _system_account_lamports(result.get("value"))


def _simulation_result(response, tip: int) -> dict:
    import jupiter_route_v2
    body = response.get("body") if isinstance(response, dict) else None
    result = body.get("result") if isinstance(body, dict) else None
    if not isinstance(result, dict):
        return {"ok": False, "reason": "simulation_malformed"}
    context = result.get("context")
    if not isinstance(context, dict) or "slot" not in context:
        return {"ok": False, "reason": "simulation_context_missing"}
    slot = context.get("slot")
    if isinstance(slot, bool) or not isinstance(slot, int) or slot <= 0:
        return {"ok": False, "reason": "simulation_context_malformed"}
    if slot < tip or slot > tip + jupiter_route_v2.SLOT_LAG_MAX:
        return {"ok": False, "reason": "simulation_context_stale" if slot < tip else "simulation_context_future"}
    value = result.get("value")
    if not isinstance(value, dict) or "err" not in value:
        return {"ok": False, "reason": "simulation_malformed"}
    units = value.get("unitsConsumed")
    if isinstance(units, bool) or not isinstance(units, int) or units < 0:
        return {"ok": False, "reason": "simulation_malformed"}
    if value.get("err") is not None:
        return {"ok": False, "reason": "simulation_failed", "slot": slot, "err_clear": False}
    return {"ok": True, "reason": None, "slot": slot, "accounts": value.get("accounts"), "units": units, "err_clear": True}


def _exact_message_economics(decoded) -> dict:
    """Slippage and network fee read from the reserved message bytes.

    The route instruction carries slippage_bps and a quoted output. That is a
    bound inside the transaction, not a simulation result and not a fresh price.
    The two integer floors agree only when the division has no remainder. The
    IDL does not name which floor the program compares, so a disagreement stays
    unproven. The compute-budget fee is not the DEX fee, the deposit, or USD.
    """
    import jupiter_route_v2
    import owner_slippage
    import solana_jupiter
    import solana_tx
    blank = {
        "instruction_slippage_within_cap": False, "instruction_slippage_bps": None,
        "quoted_out_amount": None, "candidate_min_out": None, "api_sample_min_out": None,
        "min_out_bound_proven": False, "program_rounding_proven": False,
        "slippage_proven_by_simulation": False, "price_freshness_proven": False,
        "execution_guarantee": False, "network_fee_instruction_proven": False,
        "network_lamports_instruction": None, "dex_fee_proven": False,
        "combined_fee_usd_proven": False, "ticket_limit_proven": False,
        "allocation_proven": False, "rent_bound_proven": False,
        "lookup_unresolved": False, "instruction_unproven": True,
        "economic_reason": "instruction_unproven",
        "requested_compute_unit_limit": None, "requested_micro_lamports_per_cu": None,
        "requested_priority_lamports": None, "requested_priority_formula": None,
        "priority_fee_observed": False, "priority_uses_consumed_units": False,
        "base_fee_proven": False,
    }
    if getattr(decoded, "lookups", None):
        return blank | {"lookup_unresolved": True, "economic_reason": "lookup_unresolved"}
    if getattr(decoded, "required_signatures", None) != 1:
        return blank | {"economic_reason": "extra_signer"}
    routes = []
    limits = []
    prices = []
    for item in decoded.instructions:
        try:
            program = decoded.static_keys[item.program_index]
        except (IndexError, TypeError):
            return blank | {"economic_reason": "instruction_unsupported"}
        data = bytes(item.data)
        if program == solana_jupiter.PROGRAM_ID:
            routes.append(data)
        elif program == solana_tx.COMPUTE_BUDGET and len(data) == 5 and data[0] == 2:
            limits.append(data)
        elif program == solana_tx.COMPUTE_BUDGET and len(data) == 9 and data[0] == 3:
            prices.append(data)
        else:
            return blank | {"economic_reason": "instruction_unsupported"}
    if len(routes) != 1 or len(limits) != 1 or len(prices) != 1:
        return blank | {"economic_reason": "instruction_unsupported"}
    priced = jupiter_route_v2.network_fee(limits + prices, decoded.required_signatures)
    if priced.get("ok") is not True:
        return blank | {"economic_reason": priced.get("reason") or "instruction_unsupported"}
    parsed = jupiter_route_v2.parse(routes[0])
    found = blank | {
        "network_fee_instruction_proven": False,
        "network_lamports_instruction": None,
        "base_fee_proven": False,
        "priority_fee_observed": False,
        "priority_uses_consumed_units": False,
        "requested_priority_formula": priced.get("requested_priority_formula"),
        "requested_compute_unit_limit": priced.get("requested_compute_unit_limit"),
        "requested_micro_lamports_per_cu": priced.get("requested_micro_lamports_per_cu"),
        "requested_priority_lamports": priced.get("requested_priority_lamports"),
    }
    if parsed.get("ok") is not True or parsed.get("hop") != "Whirlpool" or parsed.get("bytes_consumed") is not True:
        return found | {"economic_reason": parsed.get("reason") or "instruction_unproven"}
    bps = parsed.get("slippage_bps")
    quoted = parsed.get("quoted_out_amount")
    floors = None
    if not isinstance(bps, bool) and not isinstance(quoted, bool):
        floors = jupiter_route_v2.candidate_floors(quoted, bps)
    agreed = floors is not None and floors[0] == floors[1]
    ceiling = owner_slippage.ceiling_bps()
    within = (
        ceiling == owner_slippage.BPS
        and isinstance(bps, int) and not isinstance(bps, bool) and 0 <= bps <= owner_slippage.BPS
        and parsed.get("platform_fee_bps") == 0 and parsed.get("positive_slippage_bps") == 0
    )
    if parsed.get("platform_fee_bps") != 0 or parsed.get("positive_slippage_bps") != 0:
        reason = "platform_fee_unpriced"
    elif isinstance(bps, int) and not isinstance(bps, bool) and bps > owner_slippage.BPS:
        reason = "slippage_exceeds"
    elif ceiling != owner_slippage.BPS:
        reason = "slippage_unapproved"
    else:
        reason = "fee_composition_unproven"
    return found | {
        "instruction_unproven": False,
        "instruction_slippage_bps": bps,
        "instruction_slippage_within_cap": within,
        "quoted_out_amount": quoted,
        "candidate_min_out": None if floors is None else floors[0],
        "api_sample_min_out": None if floors is None else floors[1],
        "min_out_bound_proven": bool(agreed and within),
        "program_rounding_proven": False,
        "economic_reason": reason,
    }


def _fee_for_message(transport, message: bytes, tip: int) -> dict:
    """Unsigned getFeeForMessage for this message. The value is not meta.fee."""
    import base64
    import jupiter_route_v2
    blank = {
        "fee_for_message_observed": False, "fee_for_message_lamports": None,
        "charged_fee_proven": False, "fee_for_message_reason": "fee_for_message_unobserved",
    }
    encoded = base64.b64encode(message).decode("ascii")
    try:
        response = transport({
            "method": "getFeeForMessage",
            "message": encoded,
            "commitment": "confirmed",
            "minContextSlot": tip,
        })
    except Exception:
        return blank | {"fee_for_message_reason": "fee_transport"}
    body = response.get("body") if isinstance(response, dict) else None
    result = body.get("result") if isinstance(body, dict) else None
    if not isinstance(result, dict):
        return blank | {"fee_for_message_reason": "fee_for_message_malformed"}
    echoed = result.get("message", body.get("message"))
    if echoed is not None and echoed != encoded:
        return blank | {"fee_for_message_reason": "fee_for_message_message_mismatch"}
    context = result.get("context")
    if not isinstance(context, dict) or "slot" not in context:
        return blank | {"fee_for_message_reason": "fee_for_message_context_missing"}
    slot = context.get("slot")
    if isinstance(slot, bool) or not isinstance(slot, int) or slot <= 0:
        return blank | {"fee_for_message_reason": "fee_for_message_context_malformed"}
    if slot < tip or slot > tip + jupiter_route_v2.SLOT_LAG_MAX:
        reason = "fee_for_message_context_stale" if slot < tip else "fee_for_message_context_future"
        return blank | {"fee_for_message_reason": reason}
    if "value" not in result:
        return blank | {"fee_for_message_reason": "fee_for_message_malformed"}
    value = result.get("value")
    if value is None:
        return blank | {"fee_for_message_reason": "fee_for_message_unavailable"}
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value >= 2**64:
        return blank | {"fee_for_message_reason": "fee_for_message_malformed"}
    return {
        "fee_for_message_observed": True, "fee_for_message_lamports": value,
        "charged_fee_proven": False, "fee_for_message_reason": None,
    }


def _hop_pool(decoded) -> dict | None:
    """The whirlpool hop named by the reserved message. A caller pool is not used."""
    import jupiter_route_v2
    import memecoin_exit
    import solana_jupiter
    import solana_whirlpool
    route = memecoin_exit._route(decoded)
    if route is None:
        return None
    parsed = None
    for instruction in decoded.instructions:
        if decoded.static_keys[instruction.program_index] == solana_jupiter.PROGRAM_ID:
            parsed = jupiter_route_v2.parse(bytes(instruction.data))
            break
    if not isinstance(parsed, dict) or not isinstance(parsed.get("a_to_b"), bool):
        return None
    keys = route["keys"]
    cursor = 7
    if cursor >= len(keys) or keys[cursor] != jupiter_route_v2.EVENT:
        cursor += 1
    pool_index = cursor + 5
    if pool_index + 4 >= len(keys) or pool_index < 3:
        return None
    if keys[pool_index - 3] != solana_whirlpool.PROGRAM_ID:
        return None
    amount = route.get("in_amount")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0 or amount >= 2**64:
        return None
    return {
        "pool": keys[pool_index],
        "token_program": keys[pool_index - 2],
        "vault_a": keys[pool_index + 2],
        "vault_b": keys[pool_index + 4],
        "source_mint": route["source_mint"],
        "destination_mint": route["destination_mint"],
        "in_amount": amount,
        "a_to_b": parsed["a_to_b"],
        "tick_arrays": [keys[pool_index + 5], keys[pool_index + 6], keys[pool_index + 7]],
    }


def _current_account(transport, purpose: str, address: str, tip: int, blockhash: str):
    """One confirmed account. The context slot is the read's slot, not inclusion."""
    import jupiter_route_v2
    try:
        response = transport({
            "method": "getAccountInfo", "purpose": purpose, "account": address,
            "commitment": "confirmed", "minContextSlot": tip, "blockhash": blockhash,
        })
    except Exception:
        return None, None, "transport"
    body = response.get("body") if isinstance(response, dict) else None
    result = body.get("result") if isinstance(body, dict) else None
    if not isinstance(result, dict):
        return None, None, "malformed"
    echoed = result.get("blockhash", body.get("blockhash") if isinstance(body, dict) else None)
    if echoed is not None and echoed != blockhash:
        return None, None, "hash"
    context = result.get("context")
    if not isinstance(context, dict) or "slot" not in context:
        return None, None, "context_missing"
    slot = context.get("slot")
    if isinstance(slot, bool) or not isinstance(slot, int) or slot <= 0:
        return None, None, "context_malformed"
    if slot < tip or slot > tip + jupiter_route_v2.SLOT_LAG_MAX:
        return None, None, "context_stale" if slot < tip else "context_future"
    value = result.get("value")
    if value is None:
        return None, slot, "missing"
    if not isinstance(value, dict) or value.get("executable") is not False:
        return None, slot, "executable"
    return value, slot, None


def _message_tick_arrays(transport, hop: dict, state: dict, tip: int, blockhash: str, pool_slot: int):
    """The three tick arrays named by the frozen message, checked against their PDAs."""
    import solana_whirlpool
    spacing = state["tick_spacing"]
    current = state["tick_current_index"]
    if not isinstance(spacing, int) or spacing <= 0 or not isinstance(current, int):
        return None, "tick_array_spacing"
    base = solana_whirlpool.start_tick_index(current, spacing)
    step = solana_whirlpool.TICK_ARRAY_SIZE * spacing
    starts = [base - i * step for i in range(3)] if hop["a_to_b"] else [base + i * step for i in range(3)]
    named = hop.get("tick_arrays")
    if not isinstance(named, list) or len(named) != 3:
        return None, "tick_array_missing"
    expected = [solana_whirlpool.tick_array_address(hop["pool"], start) for start in starts]
    if named != expected:
        return None, "tick_array_pda"
    if not (starts[0] <= current < starts[0] + step):
        return None, "tick_array_coverage"
    parsed = []
    for address, start in zip(named, starts):
        value, slot, reason = _current_account(transport, "whirlpool_tick_array", address, tip, blockhash)
        if reason is not None:
            return None, "tick_array_" + reason
        if slot != pool_slot:
            return None, "tick_array_context"
        if value.get("owner") != solana_whirlpool.PROGRAM_ID:
            return None, "tick_array_owner"
        raw = _decoded_account_data(value.get("data"))
        fields = None if raw is None else solana_whirlpool.tick_array_state(raw)
        if fields is None or fields["start_tick_index"] != start or fields["pool"] != hop["pool"]:
            return None, "tick_array_layout"
        offset = 12
        for _ in range(solana_whirlpool.TICK_ARRAY_SIZE):
            if raw[offset] not in (0, 1):
                return None, "tick_array_layout"
            offset += 113
        parsed.append(fields)
    return parsed, None


def _exact_in_ceiling(hop: dict) -> dict:
    """Fee <= the message input on a successful classic exact-in.

    The inequality holds for every successful exact-in of this program.
    Tightness does not. The step that meets the input has zero output, and
    swap.rs rejects that outcome when the threshold is positive. The route
    bytes do not carry that threshold, so this does not claim equality.
    """
    import solana_tx
    import solana_whirlpool_quote
    unit = "lamports" if hop["source_mint"] == solana_tx.NATIVE_MINT else "token_raw"
    return {
        "dex_fee_input_ceiling_raw": solana_whirlpool_quote.exact_in_input_ceiling(hop["in_amount"]),
        "dex_fee_input_ceiling_unit": unit,
        "dex_fee_input_ceiling_proven": True,
        "dex_fee_input_ceiling_tight": False,
        "dex_fee_input_ceiling_is_inclusion_bound": True,
        "dex_fee_step_count_is_maximum": False,
        "dex_fee_ceiling_program": "orca-so/whirlpools@2509ad9d9894a38c922e6e84a6a3a9de5e9ccd2d",
        "dex_fee_static_rate_max": solana_whirlpool_quote.MAX_STATIC_FEE_RATE,
    }


FEE_ESTIMATE_LABEL = "estimate, not guaranteed"


def _message_network_estimate(decoded) -> dict:
    """Base fee, requested priority, and any non-wrap system transfer. An estimate."""
    import jupiter_route_v2
    import phantom_transport
    import solana_tx
    refused = {"ok": False}
    compute = []
    for instruction in decoded.instructions:
        try:
            program = decoded.static_keys[instruction.program_index]
        except (IndexError, TypeError):
            return refused | {"reason": "network_unparsed"}
        if program == solana_tx.COMPUTE_BUDGET:
            compute.append(bytes(instruction.data))
    network = jupiter_route_v2.network_fee(compute, decoded.required_signatures)
    if network.get("ok") is not True:
        return refused | {"reason": network.get("reason") or "network_unparsed"}
    try:
        wrap = solana_tx.associated_token_address(phantom_transport.REQUIRED_WALLET, solana_tx.NATIVE_MINT, solana_tx.TOKEN_PROGRAM)
    except Exception:
        return refused | {"reason": "network_unparsed"}
    tip = 0
    for instruction in decoded.instructions:
        try:
            program = decoded.static_keys[instruction.program_index]
            keys = [decoded.static_keys[item] for item in instruction.account_indexes]
        except (IndexError, TypeError):
            return refused | {"reason": "network_unparsed"}
        if program != solana_tx.SYSTEM_PROGRAM:
            continue
        moved = solana_tx.system_transfer(bytes(instruction.data))
        if moved is None:
            return refused | {"reason": "network_unparsed"}
        if len(keys) != 2 or keys[0] != phantom_transport.REQUIRED_WALLET:
            return refused | {"reason": "network_unparsed"}
        if keys[1] == wrap:
            continue
        tip += moved
    base = jupiter_route_v2.BASE_FEE_LAMPORTS * decoded.required_signatures
    priority = network["requested_priority_lamports"]
    return {
        "ok": True,
        "base_fee_lamports": base,
        "priority_lamports": priority,
        "tip_lamports": tip,
        "network_lamports": base + priority + tip,
        "base_fee_is_protocol_constant": True,
    }


def _raw_to_usd(amount: int, unit: str, sol: dict, sqrt_price: int, token_is_mint_a: bool):
    """One unit family to USD. Token raw is not added to lamports."""
    import jupiter_route_v2
    import pyth_sponsored
    if unit == "lamports":
        return pyth_sponsored.lamports_usd(amount, sol)
    if unit != "token_raw":
        return None
    lamports = jupiter_route_v2.token_raw_to_lamports(amount, sqrt_price, token_is_mint_a)
    if lamports is None:
        return None
    return pyth_sponsored.lamports_usd(lamports, sol)


def fee_estimate_before_sign(case_id: str, message_sha256: str, transport, now: float, attempt: int) -> dict:
    """Re-read the current tick-step fee and the message network fee before a signature.

    The dollar figure is an estimate. It is not a guaranteed cap, and a pass
    does not sign. Each attempt reads the pool again. Nothing is cached.
    """
    import base64
    import memecoin_desk
    import pyth_sponsored
    import solana_tx
    body = {
        "sign": False, "send": False, "simulate": False, "execution_eligible": False,
        "preflight_ready": False, "gate_complete": False, "combined_fee_usd_proven": False,
        "fee_estimate_is_hard_cap": False, "fee_estimate_label": FEE_ESTIMATE_LABEL,
        "fee_estimate_skip": True, "attempt": attempt, "case_id": case_id,
        "message_sha256": message_sha256, "signer_implemented": False,
    }
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        return body | {"reason": "attempt_unbound"}
    if isinstance(now, bool) or not isinstance(now, (int, float)):
        return body | {"reason": "quote_stale"}
    memecoin_desk._ensure()
    import db
    rows = db.query("SELECT * FROM memecoin_reservations WHERE message_sha256 = ?", (message_sha256,))
    if len(rows) != 1 or rows[0]["case_id"] != case_id or rows[0]["side"] not in {"entry", "exit"}:
        return _log_fee_estimate(body | {"reason": "reservation_missing"}, now)
    proof = rows[0]
    try:
        decoded = solana_tx.decode_transaction(base64.b64decode(proof["message_b64"], validate=True))
    except Exception:
        return _log_fee_estimate(body | {"reason": "message", "side": proof["side"]}, now)
    hop = _hop_pool(decoded)
    if hop is None or hop["pool"] != proof["pool"]:
        return _log_fee_estimate(body | {"reason": "pool_unbound", "side": proof["side"]}, now)
    try:
        tip_response = transport({"method": "getSlot"})
    except Exception:
        return _log_fee_estimate(body | {"reason": "quote_stale", "side": proof["side"]}, now)
    tip_body = tip_response.get("body") if isinstance(tip_response, dict) else None
    tip = tip_body.get("result") if isinstance(tip_body, dict) else None
    if isinstance(tip, bool) or not isinstance(tip, int) or tip <= 0:
        return _log_fee_estimate(body | {"reason": "quote_stale", "side": proof["side"]}, now)
    quoted = _reserved_pool_fee(transport, {"pool": proof["pool"], "blockhash": proof["blockhash"]}, decoded, tip)
    network = _message_network_estimate(decoded)
    prices = pyth_sponsored.read_accounts(transport, now)
    sol = prices.get("sol") if isinstance(prices, dict) else None
    detail = body | {
        "side": proof["side"],
        "dex_fee_quote_raw": quoted.get("dex_fee_quote_raw"),
        "dex_fee_quote_unit": quoted.get("dex_fee_input_ceiling_unit"),
        "dex_fee_quote_current_state": quoted.get("dex_fee_quote_current_state") is True,
        "dex_fee_quote_is_inclusion_bound": False,
        "adaptive_fee_unsupported": quoted.get("adaptive_fee_unsupported") is True,
        "base_fee_lamports": network.get("base_fee_lamports"),
        "priority_lamports": network.get("priority_lamports"),
        "tip_lamports": network.get("tip_lamports"),
        "network_lamports": network.get("network_lamports"),
    }
    if quoted.get("adaptive_fee_unsupported") is True or hop["token_program"] != solana_tx.TOKEN_PROGRAM:
        return _log_fee_estimate(detail | {"reason": "adaptive_fee_unsupported" if quoted.get("adaptive_fee_unsupported") else "pool_unsupported"}, now)
    reason = quoted.get("pool_fee_reason")
    if quoted.get("dex_fee_quote_current_state") is not True or not isinstance(quoted.get("dex_fee_quote_raw"), int):
        stale = reason in {"pool_context_stale", "pool_context_future", "tick_array_context", "pool_context_missing"}
        return _log_fee_estimate(detail | {"reason": "quote_stale" if stale else (reason or "quote_missing")}, now)
    if quoted.get("dex_fee_quote_partial") is True:
        return _log_fee_estimate(detail | {"reason": "quote_partial"}, now)
    if network.get("ok") is not True:
        return _log_fee_estimate(detail | {"reason": network.get("reason") or "network_unparsed"}, now)
    if not isinstance(sol, dict) or sol.get("fresh") is not True or sol.get("used") is not True:
        missing = prices.get("reason") if isinstance(prices, dict) else None
        return _log_fee_estimate(detail | {"reason": missing or "usd_unpriced"}, now)
    sqrt_price = quoted.get("pool_sqrt_price")
    read_slot = quoted.get("pool_read_slot")
    if not isinstance(sqrt_price, int) or isinstance(sqrt_price, bool) or not isinstance(read_slot, int) or isinstance(read_slot, bool) or read_slot < tip:
        return _log_fee_estimate(detail | {"reason": "quote_missing"}, now)
    unit = "lamports" if hop["source_mint"] == solana_tx.NATIVE_MINT else "token_raw"
    if quoted.get("dex_fee_input_ceiling_unit") != unit:
        return _log_fee_estimate(detail | {"reason": "fee_unit"}, now)
    token_is_a = quoted.get("pool_source_is_mint_a") is True
    dex_usd = _raw_to_usd(quoted["dex_fee_quote_raw"], unit, sol, sqrt_price, token_is_a)
    network_usd = _raw_to_usd(network["network_lamports"], "lamports", sol, sqrt_price, token_is_a)
    if dex_usd is None or network_usd is None:
        return _log_fee_estimate(detail | {"reason": "usd_unpriced"}, now)
    total = dex_usd + network_usd
    detail = detail | {
        "dex_fee_usd": format(dex_usd, "f"),
        "network_fee_usd": format(network_usd, "f"),
        "estimated_fee_usd": format(total, "f"),
        "dex_fee_quote_unit": unit,
        "price_source": "pyth_sponsored_sol_usd",
        "token_converted_through_pool_sqrt": unit == "token_raw",
        "sol_price": sol.get("price"),
        "sol_conf": sol.get("conf"),
        "sol_expo": sol.get("expo"),
        "pool_sqrt_price": sqrt_price,
        "pool_read_slot": read_slot,
        "pool_snapshot_sha256": quoted.get("pool_snapshot_sha256"),
    }
    if total > memecoin_desk.MAX_FEE:
        return _log_fee_estimate(detail | {"reason": "fee_estimate_above_0.05"}, now)
    return _log_fee_estimate(detail | {"fee_estimate_skip": False, "reason": "signer_unimplemented"}, now)


def _log_fee_estimate(body: dict, now: float) -> dict:
    import memecoin_desk
    import db
    evidence = {
        "label": FEE_ESTIMATE_LABEL,
        "guaranteed_cap": False,
        "hard_cap": False,
        "attempt": body.get("attempt"),
        "side": body.get("side"),
        "reason": body.get("reason"),
        "estimated_fee_usd": body.get("estimated_fee_usd"),
        "dex_fee_usd": body.get("dex_fee_usd"),
        "network_fee_usd": body.get("network_fee_usd"),
        "dex_fee_quote_raw": body.get("dex_fee_quote_raw"),
        "dex_fee_quote_unit": body.get("dex_fee_quote_unit"),
        "base_fee_lamports": body.get("base_fee_lamports"),
        "priority_lamports": body.get("priority_lamports"),
        "tip_lamports": body.get("tip_lamports"),
        "network_lamports": body.get("network_lamports"),
        "sol_price": body.get("sol_price"),
        "sol_conf": body.get("sol_conf"),
        "sol_expo": body.get("sol_expo"),
        "pool_sqrt_price": body.get("pool_sqrt_price"),
        "pool_read_slot": body.get("pool_read_slot"),
        "pool_snapshot_sha256": body.get("pool_snapshot_sha256"),
        "message_sha256": body.get("message_sha256"),
        "sign": False,
    }
    case_id = body.get("case_id")
    try:
        row = memecoin_desk._case(case_id) if isinstance(case_id, str) else None
        mint = None if row is None else row["mint"]
        verdict = "skip" if body.get("fee_estimate_skip") is not False else "pass"
        db.exclusive(lambda conn: memecoin_desk._log(
            conn, case_id, mint, "fee_estimate", verdict, body.get("reason"), FEE_ESTIMATE_LABEL, evidence, float(now),
        ))
    except Exception:
        pass
    return body


def _safe_landed_fee_log(case_id: str, signature: str, transport, now: float) -> None:
    try:
        log_landed_fee_against_estimate(case_id, signature, transport, now)
    except Exception:
        return


def log_landed_fee_against_estimate(case_id: str, signature: str, transport, now: float) -> dict:
    """Compare the landed fee with the estimate in the estimate's own units.

    A later SOL/USD print is not used. The pool sqrt price from a later read
    is not used. transport is unused so a price move cannot set the flag.
    """
    import memecoin_desk
    import db
    del transport
    blank = {
        "logged": False, "label": FEE_ESTIMATE_LABEL, "guaranteed_cap": False,
        "hard_cap": False, "overrun": None, "sign": False,
    }
    memecoin_desk._ensure()
    fees = db.query("SELECT * FROM memecoin_landed_swap_fee WHERE signature = ?", (signature,))
    if len(fees) != 1 or fees[0]["case_id"] != case_id:
        return blank | {"reason": "landed_fee_missing"}
    landed = fees[0]
    logs = db.query(
        "SELECT evidence_json FROM memecoin_log WHERE case_id = ? AND stage = 'fee_estimate' AND verdict = 'pass' ORDER BY id DESC",
        (case_id,),
    )
    estimate = None
    for row in logs:
        try:
            parsed = __import__("json").loads(row["evidence_json"])
        except Exception:
            continue
        if parsed.get("message_sha256") == landed["message_sha256"] and isinstance(parsed.get("dex_fee_quote_raw"), int):
            estimate = parsed
            break
    if estimate is None:
        return blank | {"reason": "estimate_missing"}
    try:
        landed_dex = int(landed["lp_fee"]) + int(landed["protocol_fee"])
        landed_network = int(landed["network_fee_lamports"])
        estimate_dex = int(estimate["dex_fee_quote_raw"])
        estimate_network = int(estimate["network_lamports"])
    except (KeyError, TypeError, ValueError):
        return blank | {"reason": "landed_fee_missing"}
    if landed["fee_unit"] != estimate.get("dex_fee_quote_unit"):
        evidence = {
            "label": FEE_ESTIMATE_LABEL, "guaranteed_cap": False, "hard_cap": False,
            "overrun": None, "reason": "unit_mismatch",
            "estimated_fee_usd": estimate.get("estimated_fee_usd"),
            "estimate_dex_raw": estimate_dex, "landed_dex_raw": landed_dex,
            "message_sha256": landed["message_sha256"], "signature": signature,
        }
        _write_overrun_log(case_id, evidence, now)
        return blank | evidence | {"logged": True}
    overrun = landed_dex > estimate_dex or landed_network > estimate_network
    evidence = {
        "label": FEE_ESTIMATE_LABEL, "guaranteed_cap": False, "hard_cap": False,
        "estimated_fee_usd": estimate.get("estimated_fee_usd"),
        "estimate_dex_raw": estimate_dex, "estimate_dex_unit": estimate.get("dex_fee_quote_unit"),
        "estimate_network_lamports": estimate_network,
        "landed_dex_raw": landed_dex, "landed_fee_unit": landed["fee_unit"],
        "landed_network_lamports": landed_network,
        "frozen_sol_price": estimate.get("sol_price"),
        "frozen_pool_sqrt_price": estimate.get("pool_sqrt_price"),
        "overrun": overrun, "repriced": False,
        "message_sha256": landed["message_sha256"], "signature": signature,
    }
    _write_overrun_log(case_id, evidence, now)
    return blank | evidence | {"logged": True, "reason": "landed_above_estimate" if overrun else None}


def _next_fee_attempt(message_sha256: str) -> int:
    import db
    import json
    try:
        rows = db.query("SELECT evidence_json FROM memecoin_log WHERE stage = 'fee_estimate'")
    except Exception:
        return 1
    seen = 0
    for row in rows:
        try:
            parsed = json.loads(row["evidence_json"])
        except Exception:
            continue
        if parsed.get("message_sha256") == message_sha256 and parsed.get("attempt") is not None:
            seen += 1
    return seen + 1


def _server_auth(mock_auth) -> dict:
    """The pinned server wallet. A mock session is not a Phantom login."""
    import phantom_transport
    pinned = phantom_transport.status()
    body = {
        "ok": False, "network_called": False, "live": False,
        "wallet_id": phantom_transport.REQUIRED_WALLET_ID,
        "wallet": phantom_transport.REQUIRED_WALLET,
        "auth": pinned.get("auth"), "transport": pinned.get("transport"),
    }
    if mock_auth is None:
        return body | {"reason": "phantom_auth_not_started"}
    if not isinstance(mock_auth, dict) or mock_auth.get("mock") is not True:
        return body | {"reason": "live_auth_refused"}
    if mock_auth.get("wallet_id") != phantom_transport.REQUIRED_WALLET_ID or mock_auth.get("wallet") != phantom_transport.REQUIRED_WALLET:
        return body | {"reason": "wallet_mismatch"}
    return body | {"ok": True, "mock": True, "reason": None}


def _entry_room(case_id: str, reserved) -> bool:
    """Other holds plus this reservation's own reserved dollars, not a fresh $8."""
    import memecoin_desk
    def work(conn):
        return memecoin_desk._room_for(conn, case_id, reserved)
    import db
    return db.exclusive(work) is True


def _gates_after_fee(case_id: str, message_sha256: str) -> dict:
    """Reservation, ticket, loss halt, and slippage. No network read."""
    import base64
    import memecoin_desk
    import owner_slippage
    import solana_jupiter
    import solana_tx
    import jupiter_route_v2
    import db
    rows = db.query("SELECT * FROM memecoin_reservations WHERE message_sha256 = ?", (message_sha256,))
    if len(rows) != 1 or rows[0]["case_id"] != case_id:
        return {"ok": False, "reason": "reservation_missing"}
    proof = rows[0]
    notes = db.query(
        "SELECT kind FROM memecoin_reservation_signatures WHERE message_sha256 = ? AND case_id = ?",
        (message_sha256, case_id),
    )
    if any(item["kind"] == "unknown" for item in notes):
        return {"ok": False, "reason": "unknown_signature_hold"}
    status = db.exclusive(lambda conn: memecoin_desk._reservation_status(conn, proof))
    if status == "unknown":
        return {"ok": False, "reason": "unknown_signature_hold"}
    if status != "active":
        return {"ok": False, "reason": "reservation_closed"}
    side = proof["side"]
    if side == "entry":
        if db_setting("memecoin_entry_halt") == "1":
            return {"ok": False, "reason": "entry_halt"}
        if memecoin_desk.loss_totals().get("live_halts_new_entries") is True:
            return {"ok": False, "reason": "loss_halt"}
        case = memecoin_desk._case(case_id)
        reserved = None if case is None else memecoin_desk._reserved_amount(__import__("json").loads(case["snapshot_json"]).get("reserved_usd"))
        if reserved is None or reserved > memecoin_desk.TICKET:
            return {"ok": False, "reason": "ticket_unpriced" if reserved is None else "ticket"}
        if not _entry_room(case_id, reserved):
            return {"ok": False, "reason": "allocation"}
    try:
        decoded = solana_tx.decode_transaction(base64.b64decode(proof["message_b64"], validate=True))
    except Exception:
        return {"ok": False, "reason": "slippage_unparsed"}
    parsed = None
    for instruction in decoded.instructions:
        if decoded.static_keys[instruction.program_index] == solana_jupiter.PROGRAM_ID:
            parsed = jupiter_route_v2.parse(bytes(instruction.data))
            break
    if not isinstance(parsed, dict) or parsed.get("ok") is not True:
        return {"ok": False, "reason": "slippage_unparsed"}
    bps = parsed.get("slippage_bps")
    if isinstance(bps, bool) or not isinstance(bps, int) or bps < 0 or bps > owner_slippage.BPS:
        return {"ok": False, "reason": "slippage_exceeds"}
    if owner_slippage.ceiling_bps() != owner_slippage.BPS:
        return {"ok": False, "reason": "slippage_unapproved"}
    return {"ok": True, "reason": None, "side": side, "slippage_bps": bps}


# Only a 165-byte classic token account has a rent bound. The lamport figure is
# the fixture getMinimumBalanceForRentExemption value for that size, not a
# second guess. The older formula produced 2_039_280; the pinned read is
# solana_tx.TOKEN_ACCOUNT_RENT. Space above 165, or lamports above the table, exceeds.
SIGNER_RENT_SPACE = 165


def _signer_rent_bound(decoded, transport, wallet: str, wsol: str, position_ata: str | None, position_mint: str | None) -> dict:
    """Count every create at the fixture rent minimum. A close of an existing account is refused."""
    import solana_tx
    unknown = {"ok": False, "reason": "rent_unknown"}
    recognized = {wsol}
    if isinstance(position_ata, str) and position_ata != "":
        recognized.add(position_ata)
    creates = []
    closes = []
    seen = set()
    for instruction in decoded.instructions:
        try:
            program = decoded.static_keys[instruction.program_index]
            keys = [decoded.static_keys[item] for item in instruction.account_indexes]
        except (IndexError, TypeError):
            return unknown
        data = bytes(instruction.data)
        if program == solana_tx.ASSOCIATED_TOKEN and data in {bytes([0]), bytes([1])} and len(keys) == 6:
            payer, ata, owner, mint, system, token_program = keys
            if payer != wallet or owner != wallet or system != solana_tx.SYSTEM_PROGRAM or token_program != solana_tx.TOKEN_PROGRAM:
                return unknown
            try:
                expected = solana_tx.associated_token_address(owner, mint, token_program)
            except Exception:
                return unknown
            if ata != expected or ata in seen:
                return unknown
            seen.add(ata)
            creates.append({"account": ata, "mint": mint, "explicit": None})
            continue
        if program == solana_tx.SYSTEM_PROGRAM:
            created = solana_tx.system_create(data)
            if created is None or len(keys) < 2 or keys[0] != wallet:
                continue
            lamports, space, owner = created
            if isinstance(space, bool) or not isinstance(space, int) or isinstance(lamports, bool) or not isinstance(lamports, int):
                return unknown
            if space > SIGNER_RENT_SPACE:
                return {"ok": False, "reason": "rent_exceeds"}
            if space != SIGNER_RENT_SPACE or owner != solana_tx.TOKEN_PROGRAM:
                return unknown
            account = keys[1]
            if account not in recognized or account in seen:
                return unknown
            if account == wsol:
                mint = solana_tx.NATIVE_MINT
            elif account == position_ata and isinstance(position_mint, str) and position_mint != "":
                mint = position_mint
            else:
                return unknown
            seen.add(account)
            creates.append({"account": account, "mint": mint, "explicit": lamports})
            continue
        if program == solana_tx.TOKEN_PROGRAM and data == bytes([9]) and keys:
            closes.append(keys[0])
    if not callable(transport):
        return unknown
    try:
        tip_response = transport({"method": "getSlot"})
    except Exception:
        return unknown
    tip_body = tip_response.get("body") if isinstance(tip_response, dict) else None
    tip = tip_body.get("result") if isinstance(tip_body, dict) else None
    if isinstance(tip, bool) or not isinstance(tip, int) or tip <= 0:
        return unknown
    minimum = None
    if creates:
        priced = _read_rent_exemption(transport, SIGNER_RENT_SPACE, tip)
        if priced.get("ok") is not True or isinstance(priced.get("lamports"), bool) or not isinstance(priced.get("lamports"), int) or priced["lamports"] < 1:
            return unknown
        minimum = priced["lamports"]
    def presence(account: str, mint: str) -> str:
        try:
            response = transport({
                "method": "getAccountInfo", "purpose": "token_deposit", "account": account,
                "commitment": "confirmed", "minContextSlot": tip, "blockhash": decoded.blockhash,
            })
        except Exception:
            return "unknown"
        body = response.get("body") if isinstance(response, dict) else None
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, dict) or "value" not in result:
            return "unknown"
        viewed = _deposit_view(result.get("value"), mint, wallet)
        if viewed.get("kind") in {"absent", "present"}:
            return viewed["kind"]
        return "unknown"

    total = 0
    open_total = 0
    for item in creates:
        explicit = item["explicit"]
        if explicit is not None:
            if explicit > minimum:
                return {"ok": False, "reason": "rent_exceeds"}
            if explicit != minimum:
                return unknown
        found = presence(item["account"], item["mint"])
        if found == "absent":
            total += minimum
            if item["account"] not in closes:
                open_total += minimum
        elif found == "present":
            if explicit is not None:
                return unknown
        else:
            return unknown
    for account in closes:
        if account not in recognized and account not in seen:
            return unknown
        mint = solana_tx.NATIVE_MINT if account == wsol else None
        if mint is None and account == position_ata and isinstance(position_mint, str) and position_mint != "":
            mint = position_mint
        if mint is None:
            for item in creates:
                if item["account"] == account:
                    mint = item["mint"]
        if not isinstance(mint, str):
            return unknown
        found = presence(account, mint)
        if found == "present":
            return {"ok": False, "reason": "preexisting_close_refused"}
        if found != "absent":
            return unknown
    return {"ok": True, "lamports": total, "open_lamports": open_total}


def _message_deposit_lamports(decoded, transport) -> dict:
    """Rent for ATA creates named by the message. No create means no new deposit."""
    import memecoin_desk
    import solana_tx
    creates = memecoin_desk._ata_creates(decoded)
    if creates is None:
        return {"ok": False, "reason": "deposit_unknown"}
    if creates == 0:
        return {"ok": True, "lamports": 0}
    if not callable(transport):
        return {"ok": False, "reason": "deposit_unknown"}
    try:
        tip_response = transport({"method": "getSlot"})
    except Exception:
        return {"ok": False, "reason": "deposit_unknown"}
    tip_body = tip_response.get("body") if isinstance(tip_response, dict) else None
    tip = tip_body.get("result") if isinstance(tip_body, dict) else None
    if isinstance(tip, bool) or not isinstance(tip, int) or tip <= 0:
        return {"ok": False, "reason": "deposit_unknown"}
    priced = _read_rent_exemption(transport, solana_tx.TOKEN_ACCOUNT_SPACE, tip)
    if priced.get("ok") is not True or not isinstance(priced.get("lamports"), int):
        return {"ok": False, "reason": "deposit_unknown"}
    wallet = __import__("phantom_transport").REQUIRED_WALLET
    held = 0
    seen = set()
    for instruction in decoded.instructions:
        try:
            program = decoded.static_keys[instruction.program_index]
            keys = [decoded.static_keys[item] for item in instruction.account_indexes]
        except (IndexError, TypeError):
            return {"ok": False, "reason": "deposit_unknown"}
        if program != solana_tx.ASSOCIATED_TOKEN or bytes(instruction.data) != bytes([1]):
            continue
        if len(keys) != 6:
            return {"ok": False, "reason": "deposit_unknown"}
        payer, ata, owner, mint, system, token_program = keys
        if payer != wallet or owner != wallet or system != solana_tx.SYSTEM_PROGRAM or token_program != solana_tx.TOKEN_PROGRAM:
            return {"ok": False, "reason": "deposit_unknown"}
        if ata in seen:
            return {"ok": False, "reason": "deposit_unknown"}
        seen.add(ata)
        found = _read_deposit_account(transport, ata, mint, wallet, tip, decoded.blockhash)
        if found["kind"] == "absent":
            held += priced["lamports"]
        elif found["kind"] != "present":
            return {"ok": False, "reason": "deposit_unknown"}
    if len(seen) != creates:
        return {"ok": False, "reason": "deposit_unknown"}
    return {"ok": True, "lamports": held}


def _payer_token_ix(decoded, data: bytes, keys: list, wallet: str, wsol: str, position_ata: str | None, side: str, remaining) -> dict:
    """Token instructions the payer signs. Anything else is refused."""
    import solana_tx
    refused = {"ok": False, "reason": "unparseable_instruction"}
    if data == bytes([17]):
        if keys == [wsol]:
            return {"ok": True, "wsol": 0, "position": 0}
        return refused
    if data == bytes([9]):
        if keys == [wsol, wallet, wallet]:
            return {"ok": True, "wsol": 0, "position": 0}
        if side == "exit" and position_ata is not None and keys == [position_ata, wallet, wallet]:
            return {"ok": True, "wsol": 0, "position": 0}
        return refused
    amount = solana_tx.token_transfer(data)
    authority = keys[2] if amount is not None and len(keys) >= 3 else None
    source = keys[0] if amount is not None and len(keys) >= 3 else None
    checked = None
    if amount is None:
        checked = solana_tx.transfer_checked(data)
        if checked is not None and len(keys) >= 4:
            amount = checked[0]
            source = keys[0]
            authority = keys[3]
            if keys[1] != solana_tx.NATIVE_MINT and not (side == "exit" and source == position_ata):
                return refused
        else:
            return refused
    signers = set(decoded.static_keys[:decoded.required_signatures])
    if authority not in signers:
        return refused
    if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
        return refused
    if source == wsol and authority == wallet:
        return {"ok": True, "wsol": amount, "position": 0}
    if side == "exit" and position_ata is not None and source == position_ata and authority == wallet:
        if isinstance(remaining, bool) or not isinstance(remaining, int) or remaining < 0 or amount > remaining:
            return refused
        return {"ok": True, "wsol": 0, "position": amount}
    return refused


def _message_outflow(decoded, transport, case: dict | None, side: str) -> dict:
    """Every lamport and wSOL unit that leaves the payer, plus the rent of new ATAs.

    Compute-budget limit and price are returned for the fee check and are not
    exposure. A token movement the payer signs is refused unless it is the
    payer's wSOL or the position sold on an exit.
    """
    import jupiter_route_v2
    import phantom_transport
    import solana_jupiter
    import solana_tx
    refused = {"ok": False}
    if getattr(decoded, "lookups", None) or decoded.required_signatures != 1:
        return refused | {"reason": "unparseable_instruction"}
    wallet = phantom_transport.REQUIRED_WALLET
    if decoded.fee_payer != wallet or decoded.static_keys[:1] != [wallet]:
        return refused | {"reason": "unparseable_instruction"}
    try:
        wsol = solana_tx.associated_token_address(wallet, solana_tx.NATIVE_MINT, solana_tx.TOKEN_PROGRAM)
    except Exception:
        return refused | {"reason": "unparseable_instruction"}
    position_mint = None if case is None else case["mint"]
    position_ata = None
    if isinstance(position_mint, str) and position_mint != "":
        try:
            position_ata = solana_tx.associated_token_address(wallet, position_mint, solana_tx.TOKEN_PROGRAM)
        except Exception:
            return refused | {"reason": "unparseable_instruction"}
    snap = {} if case is None else __import__("memecoin_desk")._snapshot(case)
    remaining = snap.get("remaining_raw")
    lamport_out = 0
    wsol_out = 0
    position_out = 0
    route_sol = 0
    routes = 0
    cu_limit = None
    cu_price = None
    for instruction in decoded.instructions:
        try:
            program = decoded.static_keys[instruction.program_index]
            keys = [decoded.static_keys[item] for item in instruction.account_indexes]
        except (IndexError, TypeError):
            return refused | {"reason": "unparseable_instruction"}
        data = bytes(instruction.data)
        if program == solana_tx.COMPUTE_BUDGET:
            parsed = solana_tx.compute_budget_payload(data)
            if parsed is None:
                return refused | {"reason": "unparseable_instruction"}
            tag, payload, _length = parsed
            if tag == 2:
                if cu_limit is not None:
                    return refused | {"reason": "unparseable_instruction"}
                cu_limit = int.from_bytes(payload, "little")
                continue
            if tag == 3:
                if cu_price is not None:
                    return refused | {"reason": "unparseable_instruction"}
                cu_price = int.from_bytes(payload, "little")
                continue
            return refused | {"reason": "unparseable_instruction"}
        if program == solana_tx.SYSTEM_PROGRAM:
            moved = solana_tx.system_transfer(data)
            if moved is not None and len(keys) == 2:
                if keys[0] == wallet and keys[1] != wsol:
                    lamport_out += moved
                elif keys[0] in decoded.static_keys[:decoded.required_signatures] and keys[0] != wallet:
                    return refused | {"reason": "unparseable_instruction"}
                continue
            created = solana_tx.system_create(data)
            if created is not None and len(keys) >= 2 and keys[0] == wallet:
                continue
            return refused | {"reason": "unparseable_instruction"}
        if program == solana_tx.ASSOCIATED_TOKEN:
            if data not in {bytes([0]), bytes([1])} or len(keys) != 6 or keys[0] != wallet:
                return refused | {"reason": "unparseable_instruction"}
            continue
        if program == solana_tx.TOKEN_PROGRAM:
            allowed = _payer_token_ix(decoded, data, keys, wallet, wsol, position_ata, side, remaining)
            if allowed.get("ok") is not True:
                return refused
            wsol_out += allowed["wsol"]
            position_out += allowed["position"]
            continue
        if program == solana_jupiter.PROGRAM_ID:
            verified = _verified_hop(decoded, instruction, transport, wsol, position_ata, side)
            if verified.get("ok") is not True:
                return refused | {"reason": verified.get("reason") or "unparseable_instruction"}
            routes += 1
            route_sol += verified["route_sol"]
            position_out += verified["position"]
            continue
        return refused | {"reason": "unparseable_instruction"}
    if routes != 1 or cu_limit is None or cu_price is None or cu_limit <= 0 or cu_limit > 1_400_000 or cu_price < 0:
        return refused | {"reason": "unparseable_instruction"}
    if isinstance(remaining, bool) or not isinstance(remaining, int) or position_out > remaining:
        if position_out:
            return refused | {"reason": "unparseable_instruction"}
    deposit = _signer_rent_bound(decoded, transport, wallet, wsol, position_ata, position_mint)
    if deposit.get("ok") is not True:
        return refused | {"reason": deposit.get("reason") or "rent_unknown"}
    return {
        "ok": True,
        "lamports": lamport_out + route_sol + wsol_out + deposit["lamports"],
        "open_rent_lamports": deposit.get("open_lamports") or 0,
        "cu_limit": cu_limit,
        "cu_price": cu_price,
        "rent_bounded": True,
    }


def _route_account_bytes(transport, address: str, blockhash: str, fallback: tuple[str, bytes] | None) -> tuple[str, bytes] | None:
    """One hop account. A canonical payer ATA may be filled from its address when the read is empty."""
    if callable(transport):
        try:
            response = transport({
                "method": "getAccountInfo", "account": address, "commitment": "confirmed", "blockhash": blockhash,
            })
        except Exception:
            response = None
        body = response.get("body") if isinstance(response, dict) else None
        result = body.get("result") if isinstance(body, dict) else None
        value = result.get("value") if isinstance(result, dict) else None
        if isinstance(value, dict):
            raw = _decoded_account_data(value.get("data"))
            owner = value.get("owner")
            if isinstance(owner, str) and raw is not None:
                return owner, raw
    return fallback


def _verified_hop(decoded, instruction, transport, wsol: str, position_ata: str | None, side: str) -> dict:
    """The whirlpool hop on this message. The debit is the hop input account, not index 1 alone."""
    import jupiter_route_v2
    import phantom_transport
    import solana_jupiter
    import solana_tx
    import solana_whirlpool
    refused = {"ok": False, "reason": "unparseable_instruction"}
    data = bytes(instruction.data)
    found = jupiter_route_v2.parse(data)
    if found.get("ok") is not True or isinstance(found.get("in_amount"), bool) or not isinstance(found.get("in_amount"), int):
        return refused
    if found.get("a_to_b") is not True and found.get("a_to_b") is not False:
        return refused
    try:
        account_keys = [decoded.static_keys[item] for item in instruction.account_indexes]
    except (IndexError, TypeError):
        return refused
    wallet = phantom_transport.REQUIRED_WALLET
    buying = side == "entry"
    if side not in {"entry", "exit"} or not isinstance(position_ata, str):
        return refused
    want_source_mint = solana_tx.NATIVE_MINT if buying else None
    want_dest_mint = None if buying else solana_tx.NATIVE_MINT
    # The case mint is the position. Buy spends wSOL into that ATA. Exit spends that ATA into wSOL.
    if buying:
        want_source_ata, want_dest_ata = wsol, position_ata
    else:
        want_source_ata, want_dest_ata = position_ata, wsol
    if len(account_keys) < 10 or account_keys[0] != wallet:
        return refused
    if account_keys[5] != solana_tx.TOKEN_PROGRAM or account_keys[6] != solana_tx.TOKEN_PROGRAM:
        return refused
    cursor = 7
    if account_keys[cursor] != jupiter_route_v2.EVENT:
        if account_keys[cursor] != solana_jupiter.PROGRAM_ID:
            return refused
        cursor += 1
    if cursor + 1 >= len(account_keys) or account_keys[cursor] != jupiter_route_v2.EVENT or account_keys[cursor + 1] != solana_jupiter.PROGRAM_ID:
        return refused
    remaining = list(range(cursor + 2, len(account_keys)))
    if len(remaining) != 12 or account_keys[remaining[0]] != solana_whirlpool.PROGRAM_ID:
        return refused
    source_mint = account_keys[3]
    dest_mint = account_keys[4]
    if buying:
        if source_mint != solana_tx.NATIVE_MINT:
            return refused
        want_source_mint = solana_tx.NATIVE_MINT
        want_dest_mint = dest_mint
        if dest_mint == solana_tx.NATIVE_MINT or position_ata != solana_tx.associated_token_address(wallet, dest_mint, solana_tx.TOKEN_PROGRAM):
            return refused
    else:
        if dest_mint != solana_tx.NATIVE_MINT:
            return refused
        want_source_mint = source_mint
        want_dest_mint = solana_tx.NATIVE_MINT
        if source_mint == solana_tx.NATIVE_MINT or position_ata != solana_tx.associated_token_address(wallet, source_mint, solana_tx.TOKEN_PROGRAM):
            return refused
    if account_keys[1] != want_source_ata or account_keys[2] != want_dest_ata:
        return refused
    created = set()
    for other in decoded.instructions:
        try:
            program = decoded.static_keys[other.program_index]
            okeys = [decoded.static_keys[item] for item in other.account_indexes]
        except (IndexError, TypeError):
            return refused
        if program == solana_tx.ASSOCIATED_TOKEN and bytes(other.data) == bytes([1]) and len(okeys) == 6 and okeys[0] == wallet:
            created.add(okeys[1])
    hop = remaining[1:]
    needed = {account_keys[hop[index]] for index in (2, 3, 4, 5, 6, 7, 8, 9)}
    needed.update((account_keys[1], account_keys[2]))
    observed = {}
    for address in needed:
        fallback = None
        if address == wsol:
            fallback = (solana_tx.TOKEN_PROGRAM, solana_whirlpool.reference_token_account(solana_tx.NATIVE_MINT, wallet))
        elif address == position_ata:
            mint = dest_mint if buying else source_mint
            fallback = (solana_tx.TOKEN_PROGRAM, solana_whirlpool.reference_token_account(mint, wallet))
        record = _route_account_bytes(transport, address, decoded.blockhash, fallback if address in {wsol, position_ata} else None)
        if record is None and address in created and address in {wsol, position_ata}:
            record = fallback
        if record is None:
            return refused
        observed[address] = record
    try:
        solana_whirlpool.check_swap_accounts(
            account_keys, remaining[1:], wallet, account_keys[1], account_keys[2],
            source_mint, dest_mint, found["a_to_b"] is True, observed, created,
        )
    except solana_whirlpool.WhirlpoolError:
        return refused
    hop = remaining[1:]
    input_account = account_keys[hop[3]] if found["a_to_b"] is True else account_keys[hop[5]]
    output_account = account_keys[hop[5]] if found["a_to_b"] is True else account_keys[hop[3]]
    if input_account != account_keys[1] or input_account != want_source_ata:
        return refused
    if output_account != want_dest_ata or account_keys[hop[1]] != wallet:
        return refused
    if want_source_mint != source_mint or want_dest_mint != dest_mint:
        return refused
    if input_account == wsol:
        return {"ok": True, "route_sol": found["in_amount"], "position": 0}
    if input_account == position_ata:
        return {"ok": True, "route_sol": 0, "position": found["in_amount"]}
    return refused


# A reserved quote older than this is expired. The blockhash dies at lastValidBlockHeight.
SIGNER_QUOTE_MAX_AGE_S = 20


def _signer_sol_price(transport, now: float) -> dict:
    """The signer reads the dollar rate itself from a Hermes-shaped body. No network."""
    import pyth_sponsored
    if not callable(transport):
        return {"ok": False, "reason": "price_unknown"}
    try:
        response = transport({"method": "hermes_price", "kind": "hermes"})
    except Exception:
        return {"ok": False, "reason": "price_unknown"}
    status = response.get("status") if isinstance(response, dict) else None
    if status is not None and status != 200:
        return {"ok": False, "reason": "price_unknown"}
    body = response.get("body") if isinstance(response, dict) else None
    if not isinstance(body, dict):
        return {"ok": False, "reason": "price_unknown"}
    try:
        return pyth_sponsored.assess_signer_price(body, now)
    except (OverflowError, ValueError, ArithmeticError):
        return {"ok": False, "reason": "price_unknown"}


def _result_field(response, name: str | None = None):
    body = response.get("body") if isinstance(response, dict) else None
    result = body.get("result") if isinstance(body, dict) else None
    if name is None:
        return result
    return result.get(name) if isinstance(result, dict) else None


def _signer_quote_and_blockhash(proof, decoded, transport, now: float) -> dict:
    """Quote age and the message blockhash. The height comes from the injected fixture provider."""
    import math
    import pyth_sponsored
    clock = pyth_sponsored.finite_clock(now)
    if clock is None:
        return {"ok": False, "reason": "quote_expired"}
    created = pyth_sponsored.finite_number(proof["created_at"])
    if created is None:
        return {"ok": False, "reason": "quote_expired"}
    age = clock - float(created)
    if not (0 <= age <= SIGNER_QUOTE_MAX_AGE_S):
        return {"ok": False, "reason": "quote_expired"}
    stored = proof["blockhash"]
    message_hash = getattr(decoded, "blockhash", None)
    if not isinstance(stored, str) or stored == "" or not isinstance(message_hash, str) or message_hash == "":
        return {"ok": False, "reason": "blockhash_unknown"}
    last_valid = proof["last_valid_block_height"]
    if type(last_valid) is not int or last_valid <= 0:
        return {"ok": False, "reason": "blockhash_unknown"}
    if not callable(transport):
        return {"ok": False, "reason": "blockhash_unknown"}
    try:
        height_response = transport({"method": "getBlockHeight", "commitment": "confirmed"})
        valid_response = transport({"method": "isBlockhashValid", "blockhash": message_hash, "commitment": "confirmed"})
    except Exception:
        return {"ok": False, "reason": "blockhash_unknown"}
    if message_hash != stored:
        return {"ok": False, "reason": "blockhash_mismatch"}
    height = _result_field(height_response)
    valid = _result_field(valid_response, "value")
    if valid is None:
        valid = _result_field(valid_response)
    if type(height) is not int or not isinstance(valid, bool):
        return {"ok": False, "reason": "blockhash_unknown"}
    if height >= 0 and height < last_valid and valid is True:
        return {"ok": True}
    if height >= last_valid or valid is not True:
        return {"ok": False, "reason": "blockhash_expired"}
    return {"ok": False, "reason": "blockhash_unknown"}


def _live_trade_proofs(case, proof, moment) -> str | None:
    """LIVE signing needs a fresh Hermes price and Jupiter quote for this mint only."""
    import live_gate_proof
    import solana_tx
    traded = None if case is None else case["mint"]
    native = solana_tx.NATIVE_MINT
    try:
        side = proof["side"]
    except Exception:
        side = None
    if side == "exit":
        input_mint, output_mint = traded, native
    else:
        input_mint, output_mint = native, traded
    if not live_gate_proof.closed("price_freshness_proven", traded, now=moment):
        return "price_unknown"
    if not live_gate_proof.quote_ready(traded, moment, input_mint=input_mint, output_mint=output_mint):
        return "quote_expired"
    return None


def _requote_guard(decoded, proof, moment) -> str | None:
    """A second owned-TLS quote. A move past 50 bps, or a stand-in body, refuses the sign."""
    import json
    import urllib.request
    import jupiter_route_v2
    import jupiter_swap_v2
    import live_gate_proof
    import solana_jupiter
    import solana_tx
    route = None
    for item in decoded.instructions:
        program = decoded.static_keys[item.program_index]
        if program == solana_jupiter.PROGRAM_ID and route is None:
            route = bytes(item.data)
    parsed = jupiter_route_v2.parse(route or b"")
    quoted = parsed.get("quoted_out_amount") if parsed.get("ok") is True else None
    amount = parsed.get("in_amount") if parsed.get("ok") is True else None
    if type(quoted) is not int or isinstance(quoted, bool) or quoted <= 0:
        return "quote_unproven"
    if type(amount) is not int or isinstance(amount, bool) or amount <= 0:
        return "quote_unproven"
    try:
        side = proof["side"]
    except Exception:
        side = None
    mint = None
    try:
        import memecoin_desk
        case = memecoin_desk._case(proof["case_id"])
        mint = None if case is None else case["mint"]
    except Exception:
        mint = None
    if not isinstance(mint, str) or mint == "":
        return "quote_unproven"
    native = solana_tx.NATIVE_MINT
    if side == "exit":
        left, right = mint, native
    else:
        left, right = native, mint
    if not live_gate_proof.quote_ready(mint, moment, input_mint=left, output_mint=right):
        return "quote_expired"
    url = jupiter_swap_v2.order_url(left, right, str(amount))
    try:
        status, raw, proved = live_gate_proof.exchange(
            urllib.request.Request(url, headers={"accept": "application/json"}),
            8,
            mint,
        )
    except Exception:
        return "quote_unproven"
    if proved is not True or type(status) is not int or status != 200:
        return "quote_unproven"
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception:
        return "quote_unproven"
    out_raw = body.get("outAmount") if isinstance(body, dict) else None
    if type(out_raw) is not str or not out_raw.isdigit():
        return "quote_unproven"
    minimum = jupiter_route_v2.minimum_out(quoted, 50)
    if minimum is None or int(out_raw) < minimum:
        return "quote_moved"
    return None


def _signer_capital(message_sha256: str, now, transport) -> dict:
    """Price the whole-message outflow. A reduce-only parse does not set the capital flags."""
    import base64
    import db
    import time
    import memecoin_desk
    import pyth_sponsored
    import solana_tx
    refused = {"ok": False, "ticket_limit_proven": False, "allocation_proven": False}
    rows = db.query("SELECT * FROM memecoin_reservations WHERE message_sha256 = ?", (message_sha256,))
    if len(rows) != 1:
        return refused | {"reason": "exposure_unknown"}
    proof = rows[0]
    try:
        decoded = solana_tx.decode_transaction(base64.b64decode(proof["message_b64"], validate=True))
    except Exception:
        return refused | {"reason": "exposure_unknown"}
    case = memecoin_desk._case(proof["case_id"])
    outflow = _message_outflow(decoded, transport, case, proof["side"] if isinstance(proof["side"], str) else "")
    if outflow.get("ok") is not True:
        return refused | {"reason": outflow.get("reason") or "unparseable_instruction"}
    if now is None:
        moment = time.time()
    else:
        moment = pyth_sponsored.finite_clock(now)
    if moment is None:
        return refused | {"reason": "price_unknown"}
    priced = _signer_sol_price(transport, moment)
    if priced.get("ok") is not True:
        return refused | {"reason": priced.get("reason") or "price_unknown"}
    expiry = _signer_quote_and_blockhash(proof, decoded, transport, moment)
    if expiry.get("ok") is not True:
        if expiry.get("reason") == "blockhash_expired" and db.get_setting("mode", "PAPER") == "LIVE":
            _requote_guard(decoded, proof, moment if moment is not None else time.time())
            live_simulate(proof["case_id"], message_sha256, moment if moment is not None else time.time())
        return refused | {"reason": expiry.get("reason") or "blockhash_unknown"}
    sol = {"fresh": True, "used": True, "price": priced["price"], "conf": priced["conf"], "expo": priced["expo"], "reason": None}
    import jupiter_route_v2
    from decimal import Decimal
    priority = (outflow["cu_price"] * outflow["cu_limit"] + 999_999) // 1_000_000
    fee_usd = pyth_sponsored.lamports_usd(priority + jupiter_route_v2.BASE_FEE_LAMPORTS, sol)
    if fee_usd is None:
        return refused | {"reason": "price_unknown"}
    if fee_usd > memecoin_desk.MAX_FEE:
        return refused | {"reason": "fee_cap"}
    fresh = {"price_freshness_proven": True, "rent_bound_proven": False}
    if outflow["lamports"] == 0:
        return {"ok": True, "reason": None, "ticket_limit_proven": False, "allocation_proven": False, "reduce_only": True} | fresh
    usd = pyth_sponsored.lamports_usd(outflow["lamports"], sol)
    if usd is None:
        return refused | {"reason": "price_unknown"}
    case = memecoin_desk._case(proof["case_id"])
    snap = memecoin_desk._snapshot(case) if case is not None else {}
    if "reserved_usd" not in snap:
        return refused | {"reason": "exposure_unknown"}
    reserved = memecoin_desk._reserved_amount(snap.get("reserved_usd"))
    if reserved is None:
        return refused | {"reason": "exposure_unknown"}
    open_rent = outflow.get("open_rent_lamports") or 0
    if type(open_rent) is not int or isinstance(open_rent, bool) or open_rent < 0:
        open_rent = 0
    rent_usd = pyth_sponsored.lamports_usd(open_rent, sol) if open_rent else Decimal("0")
    if rent_usd is None:
        return refused | {"reason": "price_unknown"}
    priced = usd
    if reserved > priced:
        usd = reserved
    if usd > memecoin_desk.TICKET:
        return refused | {"reason": "ticket"}
    # A reserved ticket does not include rent that this message locks in a new ATA.
    # capital_room still enforces the $8 ticket on the trade itself.
    room = db.exclusive(lambda conn: memecoin_desk.capital_room(conn, proof["case_id"], usd, now=moment))
    if room.get("ok") is not True:
        return refused | {"reason": room.get("reason") or "allocation"}
    if reserved >= priced and rent_usd > 0:
        held = Decimal(room["held_usd"])
        if held + usd + rent_usd > memecoin_desk.ALLOCATION:
            return refused | {"reason": "allocation"}
    return {
        "ok": True, "reason": None, "ticket_limit_proven": True, "allocation_proven": True,
        "price_freshness_proven": True, "rent_bound_proven": outflow.get("rent_bounded") is True, "reduce_only": False,
    }


def mock_phantom_sign(message_sha256: str, auth: dict, now: float | None = None, transport=None) -> dict:
    """A labeled stand-in. It repeats the ticket and allocation checks and does not call Phantom."""
    import hashlib
    blank = {
        "signed": False, "mock": True, "network_called": False, "live": False,
        "ticket_limit_proven": False, "allocation_proven": False, "price_freshness_proven": False, "rent_bound_proven": False,
    }
    if not isinstance(auth, dict) or auth.get("ok") is not True or auth.get("mock") is not True or auth.get("network_called") is not False:
        return blank | {"reason": "auth_refused"}
    capital = _signer_capital(message_sha256, now, transport)
    if capital.get("ok") is not True:
        return blank | {"reason": capital.get("reason") or "exposure_unknown"}
    digest = hashlib.sha256(b"mock-sign:" + str(message_sha256).encode()).hexdigest()
    return {
        "signed": True, "mock": True, "network_called": False, "live": False,
        "signature": "mock:" + digest[:32],
        "wallet_id": auth.get("wallet_id"), "wallet": auth.get("wallet"),
        "reason": None,
        "ticket_limit_proven": capital.get("ticket_limit_proven") is True,
        "allocation_proven": capital.get("allocation_proven") is True,
        "price_freshness_proven": capital.get("price_freshness_proven") is True,
        "rent_bound_proven": capital.get("rent_bound_proven") is True,
    }


def mock_phantom_send(signed: dict) -> dict:
    """Accepts only a mock signature and does not broadcast it."""
    if not isinstance(signed, dict) or signed.get("signed") is not True or signed.get("mock") is not True or signed.get("network_called") is not False:
        return {"sent": False, "accepted": False, "mock": True, "network_called": False, "broadcast": False, "reason": "unsigned"}
    return {
        "sent": False, "accepted": True, "mock": True, "network_called": False,
        "broadcast": False, "reason": "mock_not_broadcast", "signature": signed.get("signature"),
    }


def simulation_allowed() -> bool:
    """LIVE with the agents on, or an explicit PAPER dry-run. The dry-run never signs."""
    import os
    if os.environ.get("DESK_SIM_DRY_RUN") == "1":
        return True
    import db
    return db.get_setting("mode", "PAPER") == "LIVE" and db.get_setting("agents_enabled", "0") == "1"


def live_simulate(case_id: str, message_sha256: str, now: float) -> dict:
    """simulateTransaction through the owned TLS transport. This function does not sign."""
    import hashlib
    import json
    import os
    import urllib.request
    import db
    import live_gate_proof
    import preflight_sim
    import solana_rpc
    quiet = {
        "ok": False, "sign": False, "send": False, "preflight_ready": False,
        "dry_run": os.environ.get("DESK_SIM_DRY_RUN") == "1", "network_called": False,
    }

    def _stop(extra):
        body = quiet | extra
        if body.get("ok") is not True:
            reason = body.get("reason")
            note_provider("simulateTransaction", reason if isinstance(reason, str) else "simulation_failed")
        return body

    if not simulation_allowed():
        return _stop({"reason": "simulation_unreachable"})
    info = solana_rpc.describe()
    if info.get("ok") is not True:
        return _stop({"reason": info.get("reason") or "helius_api_key_missing"})
    try:
        rows = db.query(
            "SELECT message_b64, side FROM memecoin_reservations WHERE message_sha256 = ? AND case_id = ?",
            (message_sha256, case_id),
        )
    except Exception:
        return _stop({"reason": "simulation_message"})
    if len(rows) != 1 or not isinstance(rows[0]["message_b64"], str) or rows[0]["side"] not in {"entry", "exit"}:
        return _stop({"reason": "simulation_message"})
    encoded = rows[0]["message_b64"]
    try:
        import memecoin_desk
        import solana_tx
        case = memecoin_desk._case(case_id)
        if case is None or not isinstance(case["mint"], str):
            return _stop({"reason": "simulation_message"})
        output_mint = case["mint"] if rows[0]["side"] == "entry" else solana_tx.NATIVE_MINT
        decoded = solana_tx.decode_transaction(__import__("base64").b64decode(encoded, validate=True))
        output_account = solana_tx.associated_token_address(decoded.fee_payer, output_mint, solana_tx.TOKEN_PROGRAM)
    except Exception:
        return _stop({"reason": "simulation_message"})
    payload = preflight_sim.rpc_body(encoded, output_account)
    try:
        url = solana_rpc._wire_url()
    except solana_rpc.RpcRefused as exc:
        return _stop({"reason": exc.reason})
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json", "accept": "application/json"},
        method="POST",
    )
    del url
    try:
        status, _raw, proved = live_gate_proof.exchange(request, 8)
    except Exception:
        return _stop({"reason": "simulation_rpc", "network_called": True})
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    closed = proved is True and live_gate_proof.preflight_observed(digest=digest)
    return _stop({
        "ok": closed, "preflight_ready": closed, "network_called": True,
        "reason": None if closed else "preflight_open", "status": status,
    })


def simulate_before_sign(case_id: str, message_sha256: str, transport, now: float, estimate: dict) -> dict:
    """simulateTransaction of the stored swap. A failure does not sign."""
    import base64
    import db
    import jupiter_route_v2
    import memecoin_desk
    import preflight_sim
    import solana_jupiter
    import solana_tx
    endpoint = preflight_sim.simulation_endpoint()
    quiet = {"ok": False, "sign": False, "send": False, "preflight_ready": False, "rpc": endpoint}

    def finish(body: dict) -> dict:
        _log_simulation(case_id, body, now)
        return body

    if endpoint.get("uses_helius_key") is True and endpoint.get("helius_key_present") is not True:
        return finish(quiet | {"cause": "helius_api_key_missing"})
    rows = db.query("SELECT * FROM memecoin_reservations WHERE message_sha256 = ?", (message_sha256,))
    if len(rows) != 1 or rows[0]["case_id"] != case_id:
        return finish(quiet | {"cause": "simulation_message"})
    proof = rows[0]
    try:
        encoded = proof["message_b64"]
        decoded = solana_tx.decode_transaction(base64.b64decode(encoded, validate=True))
    except Exception:
        return finish(quiet | {"cause": "simulation_message"})
    case = memecoin_desk._case(case_id)
    if case is None or proof["side"] not in {"entry", "exit"}:
        return finish(quiet | {"cause": "simulation_message"})
    output_mint = case["mint"] if proof["side"] == "entry" else solana_tx.NATIVE_MINT
    output_account = solana_tx.associated_token_address(decoded.fee_payer, output_mint, solana_tx.TOKEN_PROGRAM)
    route = None
    compute = []
    for item in decoded.instructions:
        program = decoded.static_keys[item.program_index]
        data = bytes(item.data)
        if program == solana_tx.COMPUTE_BUDGET:
            compute.append(data)
        elif program == solana_jupiter.PROGRAM_ID and route is None:
            route = data
    parsed = jupiter_route_v2.parse(route or b"")
    quoted = parsed.get("quoted_out_amount") if parsed.get("ok") is True else None
    if type(quoted) is str and quoted.isdigit():
        quoted = int(quoted)
    if type(quoted) is not int or quoted <= 0:
        return finish(quiet | {"cause": "simulated_output_unproven"})
    priced = jupiter_route_v2.network_fee(compute, decoded.required_signatures)
    cu_price = priced.get("requested_micro_lamports_per_cu") if priced.get("ok") is True else 0
    cu_limit = priced.get("requested_compute_unit_limit") if priced.get("ok") is True else None
    if type(cu_price) is not int:
        cu_price = 0
    if type(cu_limit) is not int:
        cu_limit = None
    mint_decimals = 9 if output_mint == solana_tx.NATIVE_MINT else None
    outflow = _message_outflow(decoded, transport, case, proof["side"])
    if outflow.get("ok") is not True:
        return finish(quiet | {"cause": outflow.get("reason") or "simulated_ticket_unproven"})
    dex_raw = estimate.get("dex_fee_usd")
    dex = None if dex_raw is None else memecoin_desk._num(dex_raw)
    if dex is None or dex < 0:
        return finish(quiet | {"cause": "simulation_fee_unproven"})
    snap = memecoin_desk._snapshot(case)
    reserved = memecoin_desk._reserved_amount(snap.get("reserved_usd")) if "reserved_usd" in snap else None
    sol = _signer_sol_price(transport, now)
    if sol.get("ok") is not True:
        return finish(quiet | {"cause": sol.get("reason") or "simulation_fee_unproven"})
    leg = {"fresh": True, "used": True, "price": sol["price"], "conf": sol["conf"], "expo": sol["expo"]}
    payload = preflight_sim.rpc_body(encoded, output_account)
    request = {
        "method": "simulateTransaction",
        "purpose": "pre_sign",
        "transaction": encoded,
        "commitment": preflight_sim.COMMITMENT,
        "encoding": "base64",
        "sigVerify": False,
        "replaceRecentBlockhash": True,
        "innerInstructions": True,
        "output_mint": output_mint,
        "jsonrpc": payload,
        "rpc": {"provider": endpoint["provider"], "host": endpoint["host"]},
    }
    try:
        response = transport(request)
    except TimeoutError:
        return finish(quiet | {"cause": "simulation_timeout"})
    except Exception:
        return finish(quiet | {"cause": "simulation_rpc"})
    if not isinstance(response, dict) or response.get("status") != 200:
        return finish(quiet | {"cause": "simulation_rpc"})
    rpc = response.get("body")
    if not isinstance(rpc, dict) or rpc.get("error") is not None:
        return finish(quiet | {"cause": "simulation_rpc"})
    result = rpc.get("result")
    value = result.get("value") if isinstance(result, dict) else None
    if not isinstance(value, dict):
        return finish(quiet | {"cause": "simulation_malformed"})
    checked = preflight_sim.assess(
        value, output_mint=output_mint, owner=decoded.fee_payer, account_count=len(decoded.static_keys),
        quoted_out=quoted, sol=leg, dex_fee_usd=dex, outflow_lamports=outflow["lamports"],
        reserved_usd=reserved, cu_price=cu_price, cu_limit=cu_limit, mint_decimals=mint_decimals,
    )
    checked["rpc"] = endpoint
    checked["preflight_ready"] = checked.get("ok") is True
    checked["sign"] = False
    checked["send"] = False
    return finish(checked)


def _log_simulation(case_id: str, body: dict, now: float) -> None:
    import memecoin_desk
    import db
    if body.get("ok") is not True:
        cause = body.get("cause")
        note_provider("simulateTransaction", cause if isinstance(cause, str) else "simulation_failed")
    rpc = body.get("rpc") if isinstance(body.get("rpc"), dict) else {}
    evidence = {
        "cause": body.get("cause"),
        "ok": body.get("ok") is True,
        "fee_lamports": body.get("fee_lamports"),
        "combined_fee_usd": body.get("combined_fee_usd"),
        "output_amount": body.get("output_amount"),
        "minimum_out": body.get("minimum_out"),
        "deposit_lamports": body.get("deposit_lamports"),
        "ticket_usd": body.get("ticket_usd"),
        "units_consumed": body.get("units_consumed"),
        "rpc_provider": rpc.get("provider"),
        "rpc_host": rpc.get("host"),
        "commitment": "processed",
        "sig_verify": False,
        "replace_recent_blockhash": True,
        "sign": False,
        "send": False,
        "live_ready": False,
    }
    try:
        row = memecoin_desk._case(case_id) if isinstance(case_id, str) else None
        mint = None if row is None else row["mint"]
        verdict = "pass" if body.get("ok") is True else "fail"
        db.exclusive(lambda conn: memecoin_desk._log(
            conn, case_id, mint, "preflight", verdict, body.get("cause"), "simulateTransaction", evidence, float(now),
        ))
    except Exception:
        return


def approach_signer(case_id: str, message_sha256: str, transport, now: float, mock_auth=None) -> dict:
    """Fee, reservation, ticket, loss, and slippage, then a mock sign and send.

    A failed gate does not reach the mock signer. The mock does not touch the
    network, and it is not a live signature.
    """
    estimate = fee_estimate_before_sign(case_id, message_sha256, transport, now, _next_fee_attempt(message_sha256))
    base = {
        "sign": False, "send": False, "simulate": False, "execution_eligible": False,
        "preflight_ready": False, "gate_complete": False, "combined_fee_usd_proven": False,
        "live_ready": False, "fee_estimate_is_hard_cap": False, "fee_estimate_label": FEE_ESTIMATE_LABEL,
        "signer_implemented": True, "sender_implemented": True, "signer_is_mock": True,
        "network_called": False, "fee_estimate": estimate, "signer": None, "sender": None,
    }
    if estimate.get("fee_estimate_skip") is not False:
        return base | {"fee_estimate_blocked": True, "signer_reached": False, "reason": estimate.get("reason")}
    gates = _gates_after_fee(case_id, message_sha256)
    if gates.get("ok") is not True:
        return base | {"fee_estimate_blocked": False, "signer_reached": False, "reason": gates.get("reason")}
    auth = _server_auth(mock_auth)
    if auth.get("ok") is not True:
        return base | {"fee_estimate_blocked": False, "signer_reached": False, "reason": auth.get("reason"), "auth": auth}
    simulated = simulate_before_sign(case_id, message_sha256, transport, now, estimate)
    if simulated.get("ok") is not True:
        return base | {
            "simulate": True, "simulation": simulated, "fee_estimate_blocked": False,
            "signer_reached": False, "reason": simulated.get("cause"), "preflight_ready": False,
            "sign": False, "send": False, "live_ready": False, "execution_eligible": False,
        }
    import live_submit
    if live_submit.armed():
        import phantom_session
        lived = live_submit.submit(case_id, message_sha256, now, transport, phantom_session.sign)
        return base | {
            "fee_estimate_blocked": False,
            "signer_reached": True,
            "signer_is_mock": False,
            "signer": lived,
            "sender": lived,
            "reason": lived.get("reason"),
            "sign": lived.get("sign") is True,
            "send": lived.get("send") is True,
            "live_ready": False,
            "execution_eligible": False,
            "network_called": lived.get("network_called") is True,
            "broadcast": lived.get("broadcast") is True,
            "preflight_ready": True,
            "simulate": True,
            "simulation": simulated,
        }
    signed = mock_phantom_sign(message_sha256, auth, now, transport)
    if signed.get("signed") is not True:
        return base | {
            "fee_estimate_blocked": False, "signer_reached": True, "signer": signed, "reason": signed.get("reason"),
            "ticket_limit_proven": False, "allocation_proven": False, "price_freshness_proven": False, "rent_bound_proven": False,
        }
    sent = mock_phantom_send(signed)
    return base | {
        "fee_estimate_blocked": False, "signer_reached": True, "signer": signed, "sender": sent,
        "mock_signed": True, "broadcast": False, "reason": sent.get("reason"),
        "ticket_limit_proven": signed.get("ticket_limit_proven") is True,
        "allocation_proven": signed.get("allocation_proven") is True,
        "price_freshness_proven": signed.get("price_freshness_proven") is True,
        "rent_bound_proven": signed.get("rent_bound_proven") is True,
        "execution_eligible": False, "preflight_ready": True, "gate_complete": False, "live_ready": False,
        "simulate": True, "simulation": simulated,
    }


def _write_overrun_log(case_id: str, evidence: dict, now: float) -> None:
    import memecoin_desk
    import db
    row = memecoin_desk._case(case_id)
    mint = None if row is None else row["mint"]
    db.exclusive(lambda conn: memecoin_desk._log(
        conn, case_id, mint, "fee_estimate", "landed", evidence.get("reason"), FEE_ESTIMATE_LABEL, evidence, float(now),
    ))


def _reserved_pool_fee(transport, proof, decoded, tip: int) -> dict:
    """Static Whirlpool fee rate on the reserved hop's pool account.

    programs/whirlpool/src/math/swap_math.rs compute_swap charges each step
    separately. A step that reaches the next tick uses
    ceil(amount_in * rate / (1_000_000 - rate)). A step that does not uses
    amount_remaining - amount_in. Those amounts add across the loop in
    swap_manager.rs. The one-shot ceil(input * rate / 1_000_000) is recorded
    and is not that sum. The sum is the fee of the pool and tick arrays as
    read now. It is not a bound at inclusion and it is not the landed Traded
    event. An adaptive fee or a Token-2022 transfer fee is a different amount.
    """
    import base64
    import jupiter_route_v2
    import solana_tx
    import solana_whirlpool
    blank = {
        "dex_fee_proven": False, "dex_fee_rate_observed": False, "dex_fee_rate": None,
        "dex_fee_estimate_raw": None, "dex_fee_estimate_proven": False,
        "dex_fee_one_shot_raw": None, "dex_fee_one_shot_is_bound": False,
        "dex_fee_quote_raw": None, "dex_fee_quote_partial": False,
        "dex_fee_quote_current_state": False, "dex_fee_quote_is_inclusion_bound": False,
        "dex_fee_input_ceiling_raw": None, "dex_fee_input_ceiling_unit": None,
        "dex_fee_input_ceiling_proven": False, "dex_fee_input_ceiling_tight": False,
        "dex_fee_input_ceiling_is_inclusion_bound": False, "dex_fee_step_count_is_maximum": False,
        "dex_fee_charged_proven": False, "adaptive_fee_unsupported": False,
        "input_transfer_fee_unsupported": False, "pool_fee_reason": "pool_fee_unobserved",
    }
    hop = _hop_pool(decoded)
    reserved = proof["pool"] if isinstance(proof, dict) else None
    if hop is None or (isinstance(reserved, str) and reserved != "" and reserved != hop["pool"]):
        return blank | {"pool_fee_reason": "pool_unbound"}
    if hop["token_program"] == solana_tx.TOKEN_2022:
        return blank | {"pool_fee_reason": "token_2022_unsupported", "input_transfer_fee_unsupported": True}
    if hop["token_program"] != solana_tx.TOKEN_PROGRAM:
        return blank | {"pool_fee_reason": "pool_unbound"}
    try:
        response = transport({
            "method": "getAccountInfo",
            "purpose": "whirlpool_pool",
            "account": hop["pool"],
            "commitment": "confirmed",
            "minContextSlot": tip,
            "blockhash": proof["blockhash"],
        })
    except Exception:
        return blank | {"pool_fee_reason": "pool_transport"}
    body = response.get("body") if isinstance(response, dict) else None
    result = body.get("result") if isinstance(body, dict) else None
    if not isinstance(result, dict):
        return blank | {"pool_fee_reason": "pool_account_malformed"}
    echoed = result.get("blockhash", body.get("blockhash") if isinstance(body, dict) else None)
    if echoed is not None and echoed != proof["blockhash"]:
        return blank | {"pool_fee_reason": "pool_hash_mismatch"}
    context = result.get("context")
    if not isinstance(context, dict) or "slot" not in context:
        return blank | {"pool_fee_reason": "pool_context_missing"}
    slot = context.get("slot")
    if isinstance(slot, bool) or not isinstance(slot, int) or slot <= 0:
        return blank | {"pool_fee_reason": "pool_context_malformed"}
    if slot < tip or slot > tip + jupiter_route_v2.SLOT_LAG_MAX:
        return blank | {"pool_fee_reason": "pool_context_stale" if slot < tip else "pool_context_future"}
    value = result.get("value")
    if value is None:
        return blank | {"pool_fee_reason": "pool_account_missing"}
    if not isinstance(value, dict) or value.get("owner") != solana_whirlpool.PROGRAM_ID:
        return blank | {"pool_fee_reason": "pool_owner"}
    if value.get("executable") is not False:
        return blank | {"pool_fee_reason": "pool_executable"}
    raw = _decoded_account_data(value.get("data"))
    if raw is None or solana_whirlpool.pool_fields(raw) is None or solana_whirlpool.pool_state(raw) is None:
        return blank | {"pool_fee_reason": "pool_layout"}
    mint_a, vault_a, mint_b, vault_b = solana_whirlpool.pool_fields(raw)
    if hop["a_to_b"]:
        mint_ok = hop["source_mint"] == mint_a and hop["destination_mint"] == mint_b
    else:
        mint_ok = hop["source_mint"] == mint_b and hop["destination_mint"] == mint_a
    if not mint_ok or hop["vault_a"] != vault_a or hop["vault_b"] != vault_b:
        return blank | {"pool_fee_reason": "pool_direction"}
    if not solana_whirlpool.pool_pda_matches(hop["pool"], raw):
        return blank | {"pool_fee_reason": "pool_pda"}
    state = solana_whirlpool.pool_state(raw)
    rate = state.get("fee_rate")
    if isinstance(rate, bool) or not isinstance(rate, int) or rate < 0 or rate > 65535:
        return blank | {"pool_fee_reason": "pool_layout"}
    import hashlib
    observed = blank | {
        "dex_fee_rate_observed": True, "dex_fee_rate": rate, "dex_fee_charged_proven": False,
        "dex_fee_proven": False, "dex_fee_estimate_proven": False, "dex_fee_one_shot_is_bound": False,
        "pool_sqrt_price": state["sqrt_price"], "pool_read_slot": slot,
        "pool_snapshot_sha256": hashlib.sha256(bytes(raw)).hexdigest(),
        "pool_source_is_mint_a": hop["source_mint"] == mint_a,
    }
    if state.get("adaptive_fee") is True:
        return observed | {"adaptive_fee_unsupported": True, "pool_fee_reason": "adaptive_fee_unsupported"}
    one_shot = jupiter_route_v2.whirlpool_fee(hop["in_amount"], rate)
    if one_shot is None or one_shot >= 2**64:
        one_shot = None
    observed = observed | {"dex_fee_one_shot_raw": one_shot} | _exact_in_ceiling(hop)
    arrays, array_reason = _message_tick_arrays(transport, hop, state, tip, proof["blockhash"], slot)
    if array_reason is not None:
        return observed | {"pool_fee_reason": array_reason}
    import solana_whirlpool_quote
    try:
        computed = solana_whirlpool_quote._compute_swap(
            hop["in_amount"], 0,
            {
                "sqrt_price": state["sqrt_price"],
                "tick_current_index": state["tick_current_index"],
                "fee_rate": rate,
                "liquidity": state["liquidity"],
                "tick_spacing": state["tick_spacing"],
            },
            arrays, hop["a_to_b"], True,
        )
    except solana_whirlpool_quote.QuoteError as exc:
        return observed | {"pool_fee_reason": exc.category}
    return observed | {
        "dex_fee_quote_raw": computed["trade_fee"],
        "dex_fee_quote_partial": computed["amount_remaining"] != 0,
        "dex_fee_quote_amount_remaining": computed["amount_remaining"],
        "dex_fee_quote_steps": computed["steps"],
        "dex_fee_quote_current_state": True,
        "dex_fee_quote_is_inclusion_bound": False,
        "pool_fee_reason": "pool_fee_quote_not_inclusion_bound",
    }


def _deposit_blank(**extra) -> dict:
    return {
        "deposit_observed": False, "deposit_lamports": None, "deposit_space": None,
        "deposit_account_count": None, "deposit_rent_each": None,
        "deposit_principal_lamports": None, "deposit_refund_credited": False,
        "deposit_counts_as_fee": False, "preexisting_close_refused": False,
        "deposit_reason": "deposit_unobserved", "rent_bound_proven": False,
    } | extra


def _classic_deposit_plan(decoded) -> dict:
    """ATA creates, the wrap transfer, and closes named by this message.

    Addresses come from the instruction accounts and the associated-token PDA.
    Classic SPL token accounts are 165 bytes. That length is not a lamport rent.
    """
    import jupiter_route_v2
    import phantom_transport
    import solana_jupiter
    import solana_tx
    if getattr(decoded, "lookups", None):
        return {"ok": False, "reason": "lookup_unresolved"}
    if getattr(decoded, "required_signatures", None) != 1:
        return {"ok": False, "reason": "extra_signer"}
    wallet = phantom_transport.REQUIRED_WALLET
    try:
        wsol = solana_tx.associated_token_address(wallet, solana_tx.NATIVE_MINT, solana_tx.TOKEN_PROGRAM)
    except Exception:
        return {"ok": False, "reason": "deposit_grammar_unsupported"}
    creates = []
    closes = []
    transfer = None
    swap = None
    kinds = []
    for instruction in decoded.instructions:
        try:
            program = decoded.static_keys[instruction.program_index]
            keys = [decoded.static_keys[item] for item in instruction.account_indexes]
        except (IndexError, TypeError):
            return {"ok": False, "reason": "deposit_grammar_unsupported"}
        data = bytes(instruction.data)
        if program == solana_tx.COMPUTE_BUDGET:
            kinds.append("compute")
            continue
        if program == solana_tx.ASSOCIATED_TOKEN and data == bytes([1]) and len(keys) == 6:
            payer, ata, owner, mint, system, token_program = keys
            if token_program == solana_tx.TOKEN_2022:
                return {"ok": False, "reason": "token_2022_unsupported"}
            if payer != wallet or owner != wallet or system != solana_tx.SYSTEM_PROGRAM or token_program != solana_tx.TOKEN_PROGRAM:
                return {"ok": False, "reason": "deposit_grammar_unsupported"}
            try:
                expected = solana_tx.associated_token_address(owner, mint, token_program)
            except Exception:
                return {"ok": False, "reason": "deposit_grammar_unsupported"}
            if ata != expected or any(item["address"] == ata for item in creates):
                return {"ok": False, "reason": "deposit_grammar_unsupported"}
            creates.append({
                "address": ata, "mint": mint, "owner": owner, "payer": payer,
                "space": solana_tx.TOKEN_ACCOUNT_SPACE,
            })
            kinds.append("create")
            continue
        if program == solana_tx.SYSTEM_PROGRAM and solana_tx.system_transfer(data) is not None and len(keys) == 2:
            if transfer is not None or keys[0] != wallet or keys[1] != wsol:
                return {"ok": False, "reason": "deposit_grammar_unsupported"}
            transfer = solana_tx.system_transfer(data)
            kinds.append("transfer")
            continue
        if program == solana_tx.TOKEN_PROGRAM and data == bytes([17]) and keys == [wsol]:
            kinds.append("sync")
            continue
        if program == solana_tx.TOKEN_PROGRAM and data == bytes([9]) and keys == [wsol, wallet, wallet]:
            closes.append(wsol)
            kinds.append("close")
            continue
        if program == solana_jupiter.PROGRAM_ID:
            if swap is not None:
                return {"ok": False, "reason": "deposit_grammar_unsupported"}
            swap = instruction
            kinds.append("swap")
            continue
        return {"ok": False, "reason": "deposit_grammar_unsupported"}
    if swap is None or len(swap.account_indexes) < 5:
        return {"ok": False, "reason": "deposit_grammar_unsupported"}
    parsed = jupiter_route_v2.parse(bytes(swap.data))
    if parsed.get("ok") is not True or parsed.get("hop") != "Whirlpool":
        return {"ok": False, "reason": "deposit_grammar_unsupported"}
    try:
        source_mint = decoded.static_keys[swap.account_indexes[3]]
        dest_mint = decoded.static_keys[swap.account_indexes[4]]
    except (IndexError, TypeError):
        return {"ok": False, "reason": "deposit_grammar_unsupported"}
    allowed = {solana_tx.NATIVE_MINT, source_mint, dest_mint}
    if any(item["mint"] not in allowed for item in creates):
        return {"ok": False, "reason": "deposit_grammar_unsupported"}
    body = [kind for kind in kinds if kind != "compute"]
    while body and body[0] == "create":
        body = body[1:]
    if transfer is not None:
        if body != ["transfer", "sync", "swap", "close"] or transfer != parsed.get("in_amount"):
            return {"ok": False, "reason": "deposit_grammar_unsupported"}
    elif body != ["swap", "close"]:
        return {"ok": False, "reason": "deposit_grammar_unsupported"}
    if closes != [wsol]:
        return {"ok": False, "reason": "deposit_grammar_unsupported"}
    return {"ok": True, "creates": creates, "closes": closes, "principal": transfer, "wsol": wsol}


def _deposit_context(result, tip: int, blockhash: str) -> str | None:
    import jupiter_route_v2
    if not isinstance(result, dict):
        return "deposit_account_malformed"
    echoed = result.get("blockhash")
    if echoed is not None and echoed != blockhash:
        return "deposit_hash_mismatch"
    context = result.get("context")
    if not isinstance(context, dict) or "slot" not in context:
        return "deposit_context_missing"
    slot = context.get("slot")
    if isinstance(slot, bool) or not isinstance(slot, int) or slot <= 0:
        return "deposit_context_malformed"
    if slot < tip or slot > tip + jupiter_route_v2.SLOT_LAG_MAX:
        return "deposit_context_stale" if slot < tip else "deposit_context_future"
    return None


def _deposit_view(value, mint: str, wallet: str) -> dict:
    """One account response. Null is absent. A system-owned balance is prefunded."""
    import solana_tx
    if value is None:
        return {"kind": "absent"}
    if not isinstance(value, dict) or value.get("executable") is not False:
        return {"kind": "malformed"}
    raw = _decoded_account_data(value.get("data"))
    owner = value.get("owner")
    if owner == solana_tx.SYSTEM_PROGRAM and raw == b"":
        lamports = value.get("lamports")
        if isinstance(lamports, bool) or not isinstance(lamports, int) or lamports <= 0 or lamports >= 2**64:
            return {"kind": "malformed"}
        return {"kind": "prefunded", "lamports": lamports}
    if owner != solana_tx.TOKEN_PROGRAM or raw is None or len(raw) != solana_tx.TOKEN_ACCOUNT_SPACE:
        return {"kind": "malformed"}
    fields = solana_tx.token_account_fields(raw)
    if fields is None or fields != (mint, wallet) or raw[108] != 1:
        return {"kind": "malformed"}
    return {"kind": "present", "raw": raw}


def _read_deposit_account(transport, address: str, mint: str, wallet: str, tip: int, blockhash: str) -> dict:
    def once() -> dict:
        try:
            response = transport({
                "method": "getAccountInfo",
                "purpose": "token_deposit",
                "account": address,
                "commitment": "confirmed",
                "minContextSlot": tip,
                "blockhash": blockhash,
            })
        except Exception:
            return {"kind": "transport"}
        body = response.get("body") if isinstance(response, dict) else None
        result = body.get("result") if isinstance(body, dict) else None
        context_reason = _deposit_context(result, tip, blockhash)
        if context_reason is not None:
            return {"kind": "context", "reason": context_reason}
        viewed = _deposit_view(result.get("value"), mint, wallet)
        viewed["slot"] = result["context"]["slot"]
        return viewed

    first = once()
    if first["kind"] in {"transport", "context"}:
        return first
    second = once()
    if second["kind"] in {"transport", "context"}:
        return second
    if first["kind"] != second["kind"] or first.get("slot") != second.get("slot"):
        return {"kind": "race"}
    if first["kind"] == "present" and first.get("raw") != second.get("raw"):
        return {"kind": "race"}
    if first["kind"] == "prefunded" and first.get("lamports") != second.get("lamports"):
        return {"kind": "race"}
    return first


def _read_rent_exemption(transport, space: int, tip: int) -> dict:
    """Official getMinimumBalanceForRentExemption returns a bare u64, not a slot."""
    try:
        response = transport({
            "method": "getMinimumBalanceForRentExemption",
            "purpose": "rent_exemption",
            "size": space,
            "commitment": "confirmed",
            "minContextSlot": tip,
        })
    except Exception:
        return {"ok": False, "reason": "deposit_transport"}
    body = response.get("body") if isinstance(response, dict) else None
    value = body.get("result") if isinstance(body, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value >= 2**64:
        return {"ok": False, "reason": "deposit_rent_malformed"}
    return {"ok": True, "lamports": value}


def _deposit_hold(transport, decoded, tip: int, blockhash: str) -> dict:
    """Lamports this message adds for missing classic ATAs. A close is not a refund.

    The wrap transfer is the swap principal and is not added again. An account
    that already exists is not closed here. The rent figure is the server's
    current exemption for 165 bytes. It is not pinned to inclusion, so it is
    not rent_bound_proven and it is not a ticket or fee pass.
    """
    import phantom_transport
    import solana_tx
    plan = _classic_deposit_plan(decoded)
    if plan.get("ok") is not True:
        extra = {"deposit_reason": plan.get("reason") or "deposit_grammar_unsupported"}
        if plan.get("reason") == "lookup_unresolved":
            extra["lookup_unresolved"] = True
        return _deposit_blank(**extra)
    wallet = phantom_transport.REQUIRED_WALLET
    views = {}

    def view(address: str, mint: str) -> dict:
        if address not in views:
            views[address] = _read_deposit_account(transport, address, mint, wallet, tip, blockhash)
        return views[address]

    held = 0
    missing = 0
    rent_each = None
    lines = []
    for created in plan["creates"]:
        found = view(created["address"], created["mint"])
        if found["kind"] == "transport":
            return _deposit_blank(deposit_reason="deposit_transport", deposit_principal_lamports=plan["principal"])
        if found["kind"] == "context":
            return _deposit_blank(deposit_reason=found["reason"], deposit_principal_lamports=plan["principal"])
        if found["kind"] == "race":
            return _deposit_blank(deposit_reason="deposit_race", deposit_principal_lamports=plan["principal"])
        if found["kind"] == "prefunded":
            return _deposit_blank(deposit_reason="deposit_prefunded_unpriced", deposit_principal_lamports=plan["principal"])
        if found["kind"] == "malformed":
            return _deposit_blank(deposit_reason="deposit_account_malformed", deposit_principal_lamports=plan["principal"])
        if found["kind"] == "present" and created["address"] in plan["closes"]:
            return _deposit_blank(
                deposit_reason="preexisting_close_refused", preexisting_close_refused=True,
                deposit_principal_lamports=plan["principal"],
            )
        if found["kind"] == "absent":
            if rent_each is None:
                priced = _read_rent_exemption(transport, created["space"], tip)
                if priced.get("ok") is not True:
                    return _deposit_blank(deposit_reason=priced.get("reason"), deposit_principal_lamports=plan["principal"])
                rent_each = priced["lamports"]
            held += rent_each
            missing += 1
            lines.append({
                "account": created["address"],
                "mint": created["mint"],
                "payer": created["payer"],
                "context_slot": found["slot"],
                "rent_lamports": rent_each,
                "observed_lamports": rent_each,
                "space": created["space"],
            })
        elif found["kind"] != "present":
            return _deposit_blank(deposit_reason="deposit_account_malformed", deposit_principal_lamports=plan["principal"])
    for address in plan["closes"]:
        if any(item["address"] == address for item in plan["creates"]):
            continue
        found = view(address, solana_tx.NATIVE_MINT)
        if found["kind"] == "transport":
            return _deposit_blank(deposit_reason="deposit_transport", deposit_principal_lamports=plan["principal"])
        if found["kind"] == "context":
            return _deposit_blank(deposit_reason=found["reason"], deposit_principal_lamports=plan["principal"])
        if found["kind"] == "race":
            return _deposit_blank(deposit_reason="deposit_race", deposit_principal_lamports=plan["principal"])
        if found["kind"] == "present":
            return _deposit_blank(
                deposit_reason="preexisting_close_refused", preexisting_close_refused=True,
                deposit_principal_lamports=plan["principal"],
            )
        if found["kind"] == "prefunded":
            return _deposit_blank(deposit_reason="deposit_prefunded_unpriced", deposit_principal_lamports=plan["principal"])
        if found["kind"] != "absent":
            return _deposit_blank(deposit_reason="deposit_account_malformed", deposit_principal_lamports=plan["principal"])
        return _deposit_blank(deposit_reason="close_without_create", deposit_principal_lamports=plan["principal"])
    return _deposit_blank(
        deposit_observed=True,
        deposit_lamports=held,
        deposit_space=solana_tx.TOKEN_ACCOUNT_SPACE,
        deposit_account_count=missing,
        deposit_rent_each=rent_each,
        deposit_principal_lamports=plan["principal"],
        deposit_refund_credited=False,
        deposit_counts_as_fee=False,
        preexisting_close_refused=False,
        deposit_reason="deposit_hold_unpinned",
        rent_bound_proven=False,
        deposit_lines=lines,
    )


def preflight_reserved(case_id: str, message_sha256: str, transport, now: float) -> dict:
    """Unsigned simulateTransaction of the stored message. The bytes are not replaced.

    sigVerify is false because the message is unsigned. That is not signature
    validation. replaceRecentBlockhash is false. A stored attempt is not reused.
    """
    import base64
    import hashlib
    import db
    import jupiter_route_v2
    import memecoin_desk
    import solana_tx
    gate = execution_gate(case_id, message_sha256, transport, now)
    if gate.get("reservation_valid") is not True:
        return _preflight_report(attempted=False, reason=gate.get("reason"), holds=gate.get("holds", True), resend=False, case_id=case_id, message_sha256=message_sha256)
    rows = db.query("SELECT * FROM memecoin_reservations WHERE message_sha256 = ?", (message_sha256,))
    proof = rows[0]
    encoded = proof["message_b64"]
    try:
        raw = base64.b64decode(encoded, validate=True)
        decoded = solana_tx.decode_transaction(raw)
        again = hashlib.sha256(solana_tx.message_bytes(encoded)).hexdigest()
    except Exception:
        return _preflight_report(reason="reservation_proof_malformed", case_id=case_id, message_sha256=message_sha256)
    if again != message_sha256 or decoded.blockhash != proof["blockhash"] or decoded.fee_payer == "":
        return _preflight_report(reason="reservation_proof_mismatch", case_id=case_id, message_sha256=message_sha256)
    economics = _exact_message_economics(decoded)
    tip_response = transport({"method": "getSlot"})
    tip_body = tip_response.get("body") if isinstance(tip_response, dict) else None
    tip = tip_body.get("result") if isinstance(tip_body, dict) else None
    if isinstance(tip, bool) or not isinstance(tip, int) or tip <= 0:
        return _preflight_report(reason="simulation_context_missing", case_id=case_id, message_sha256=message_sha256)
    pool_fee = _reserved_pool_fee(transport, proof, decoded, tip)
    deposit = {
        key: gate[key] for key in (
            "deposit_observed", "deposit_lamports", "deposit_space", "deposit_account_count",
            "deposit_rent_each", "deposit_principal_lamports", "deposit_refund_credited",
            "deposit_counts_as_fee", "preexisting_close_refused", "deposit_reason", "rent_bound_proven",
        ) if key in gate
    }
    fee_quote = _fee_for_message(transport, solana_tx.message_bytes(encoded), tip) if decoded.required_signatures == 1 else {
        "fee_for_message_observed": False, "fee_for_message_lamports": None,
        "charged_fee_proven": False, "fee_for_message_reason": "extra_signer",
    }
    payer = decoded.fee_payer
    pre = _account_lamports(transport({"method": "getAccountInfo", "account": payer, "purpose": "fee_payer"}), tip)
    compute = [bytes(item.data) for item in decoded.instructions if decoded.static_keys[item.program_index] == solana_tx.COMPUTE_BUDGET]
    priced = jupiter_route_v2.network_fee(compute, decoded.required_signatures)
    requested = priced.get("requested_priority_lamports") if priced.get("ok") is True else None
    response = transport({
        "method": "simulateTransaction",
        "transaction": encoded,
        "commitment": "confirmed",
        "encoding": "base64",
        "sigVerify": False,
        "replaceRecentBlockhash": False,
        "minContextSlot": tip,
        "accounts": [payer],
    })
    parsed = _simulation_result(response, tip)
    if parsed.get("slot"):
        try:
            db.execute(
                "INSERT INTO memecoin_preflight_attempts (message_sha256, case_id, side, blockhash, context_slot, err_clear, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (message_sha256, case_id, proof["side"], proof["blockhash"], parsed["slot"], 1 if parsed.get("err_clear") else 0, now),
            )
        except Exception:
            return _preflight_report(attempted=True, simulate=True, reason="preflight_unbound", case_id=case_id, message_sha256=message_sha256, blockhash=proof["blockhash"])
    if parsed.get("ok") is not True:
        return _preflight_report(attempted=True, simulate=True, reason=parsed.get("reason"), case_id=case_id, message_sha256=message_sha256, blockhash=proof["blockhash"], context_slot=parsed.get("slot"))
    accounts = parsed.get("accounts")
    post_value = accounts[0] if isinstance(accounts, list) and accounts else None
    post = _system_account_lamports(post_value)
    account_ok = pre is not None and post is not None
    observed = None
    priority_match = False
    rent_constant = False
    if account_ok and isinstance(requested, int) and requested >= 0:
        observed = pre - post
        if observed == requested:
            priority_match = True
        elif observed == requested + solana_tx.TOKEN_ACCOUNT_RENT:
            rent_constant = True
    # A fee-payer delta still mixes the swap, wrapped SOL, rent, and any refund.
    # Matching the network fee, or that fee plus the constant token-account rent,
    # is only arithmetic. It is not fee composition and it is not rent proof.
    if economics.get("economic_reason") == "slippage_exceeds":
        reason = "slippage_exceeds"
    elif account_ok:
        reason = "fee_composition_unproven"
    else:
        reason = "simulation_account_malformed"
    return _preflight_report(
        attempted=True, simulate=True, simulation_clear=True, preflight_ready=False,
        reason=reason,
        account_validated=account_ok, observed_lamport_delta=observed,
        network_fee_arithmetic_match=False, requested_priority_arithmetic_match=priority_match,
        rent_constant_arithmetic_match=rent_constant,
        fee_lamports_match=False, fee_composition_proven=False, rent_unproven=True, fee_usd_proven=False,
        case_id=case_id, message_sha256=message_sha256, side=proof["side"], blockhash=proof["blockhash"],
        context_slot=parsed["slot"], lookups_resolved=False, signature_validated=False,
        **(fee_quote | economics | pool_fee | deposit),
    )


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
        order, build, instruction = public_market.request_quote(
            get, reviewed["est_out"], intent["amount"], intent["input_mint"], intent["output_mint"],
        )
    except Exception:
        note_provider("jupiter", "quote_transport")
        return quiet | {"attempted": True, "reason": "quote_transport", "pool": identity["pool"], "measured_est_out": reviewed["est_out"]}
    result = quiet | {
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
        "internal_time_limit": None,
        "internal_time_limit_is_owner_approved": False,
        "send": False,
        "sign": False,
        "live_ready": False,
        "effectual_entry": False,
        "reserved": False,
    }
    if instruction.get("instruction_matches_request") is not True:
        return result
    message = order.get("transaction_b64") if isinstance(order, dict) else None
    message_hash = None
    decode_failed = False
    if isinstance(message, str) and message != "":
        try:
            import solana_tx
            decoded = solana_tx.decode_transaction(__import__("base64").b64decode(message, validate=True))
            message_hash = decoded.blockhash
        except Exception:
            decode_failed = True
    if decode_failed:
        window = {"ok": False, "reason": "blockhash_malformed", "quote_expiry_proven": False, "price_freshness_proven": False, "inclusion_window_proven": False, "block_height_missing": True}
    elif isinstance(message_hash, str) and message_hash != "":
        window = read_message_validity(transport, message_hash, now)
    else:
        window = {"ok": False, "reason": "provider_transaction_unpublished", "quote_expiry_proven": False, "price_freshness_proven": False, "inclusion_window_proven": False, "block_height_missing": True}
    bound = _bind_provider_window(window, order if isinstance(order, dict) else {}, message_hash or "", now)
    if bound.get("ok") is not True:
        return result | {
            "reserve_reason": bound.get("reason"),
            "block_height_missing": bound.get("block_height_missing", True) is not False,
            "provider_quote_expiry_published": bound.get("provider_quote_expiry_published") is True,
            "provider_expiry_field": bound.get("provider_expiry_field"),
            "quote_expiry_source": bound.get("quote_expiry_source"),
        }
    if allow_synthetic or memecoin_desk.execution_mode(memecoin_desk._case(intent["case_id"]) or {}) == "synthetic":
        return result | {"reserve_reason": "synthetic_not_live", "quote_expiry_proven": False}
    fetched["snapshot"].server_chain = bound
    if intent["side"] == "exit":
        reserved = memecoin_desk.reserve_exit(intent["case_id"], message, intent["amount"], fetched["snapshot"], transport, now)
    else:
        reserved = memecoin_desk.reserve_entry(intent["case_id"], message, fetched["snapshot"], transport, now)
    gate = execution_gate(intent["case_id"], reserved.get("message_sha256"), transport, now) if reserved.get("ok") is True else {"execution_eligible": False, "operation": "unimplemented", "sign": False, "send": False, "simulate": False, "gate_complete": False, "price_freshness_proven": False}
    pre_sign = None
    if reserved.get("ok") is True and isinstance(reserved.get("message_sha256"), str):
        if intent["side"] == "exit" and isinstance(reserved.get("pre_sign"), dict):
            pre_sign = reserved["pre_sign"]
        else:
            pre_sign = approach_signer(intent["case_id"], reserved["message_sha256"], transport, now)
        if isinstance(pre_sign, dict) and pre_sign.get("fee_estimate_blocked") is True:
            gate = dict(gate) | {"sign": False, "send": False, "execution_eligible": False}
    return result | {
        "reserved": reserved.get("ok") is True,
        "reserve_reason": reserved.get("reason"),
        "quote_expiry_proven": False,
        "price_freshness_proven": False,
        "inclusion_window_proven": bound.get("inclusion_window_proven") is True and reserved.get("ok") is True,
        "height_proof": bound.get("height_proof") is True and reserved.get("ok") is True,
        "blockhash_still_valid": bound.get("blockhash_still_valid") is True and reserved.get("ok") is True,
        "gate_complete": False,
        "expire_at_enforced": bound.get("expire_at_enforced") is True and reserved.get("ok") is True,
        "quote_clock_proven": False,
        "block_height_missing": False,
        "blockhash": bound.get("blockhash"),
        "last_valid_block_height": bound.get("last_valid_block_height"),
        "last_valid_provenance": bound.get("last_valid_provenance"),
        "latest_blockhash_paired": bound.get("latest_blockhash_paired") is True,
        "block_height": bound.get("block_height"),
        "provider_quote_expiry_published": bound.get("provider_quote_expiry_published") is True,
        "provider_expiry_field": bound.get("provider_expiry_field"),
        "inclusion_source": bound.get("inclusion_source"),
        "message_sha256": reserved.get("message_sha256"),
        "idempotent": reserved.get("idempotent") is True,
        "execution_eligible": gate.get("execution_eligible") is True,
        "execution": gate,
        "pre_sign": pre_sign,
        "operation": "unimplemented",
        "resend": False,
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
            "SELECT id, stage, snapshot_json, ledger, ledger_provenance FROM memecoin_cases WHERE mint = ? AND stage NOT IN ('rejected', 'exited')",
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
    mode = memecoin_desk.execution_mode(rows[0])
    conflict = isinstance(snap.get("capital_ledger"), str) and snap.get("capital_ledger") != mode
    if mode == "synthetic" and not allow_synthetic:
        return refused | {"reason": "synthetic_not_live"}
    if mode not in {"live", "synthetic"} and stage in {"filled", "partial", "exit_intent", "protected"}:
        return refused | {"reason": "paper_not_live" if mode == "paper" else "exit_unproven"}
    if stage in {"filled", "partial", "exit_intent"}:
        if isinstance(snap.get("inflight"), dict):
            return refused | {"reason": "exit_inflight"}
        if stage == "filled":
            return refused | {"reason": "exit_hold"}
        intent_row = memecoin_desk._matching_intent(rows[0]["id"])
        if intent_row is None:
            return refused | {"reason": "exit_unproven"}
        if intent_row["pool"] != pool:
            return refused | {"reason": "pool_mismatch", "holds": True}
        left = memecoin_desk.receipt_remaining(rows[0]["id"])
        if left.get("ok") is not True or not isinstance(left.get("remaining"), int) or left["remaining"] <= 0:
            return refused | {"reason": "flat" if left.get("remaining") == 0 else left.get("reason")}
        return {"ok": True, "side": "exit", "amount": left["remaining"], "input_mint": mint, "output_mint": solana_tx.NATIVE_MINT, "a_to_b": False, "case_id": rows[0]["id"], "pool": intent_row["pool"], "intent_id": intent_row["id"], "snapshot_mode_conflict": conflict}
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
              AND ledger = 'live' AND ledger_provenance = 'server'
              AND EXISTS (
                SELECT 1 FROM memecoin_entry_receipts
                WHERE case_id = memecoin_cases.id AND failed = 0 AND qty > 0
                  AND ledger = 'live' AND ledger_provenance = 'server'
              )
            """
        )
    except Exception:
        return []


def _durable_reservation(case_id: str, side: str):
    """The unconsumed reservation for this side. A landed slice does not block the next one."""
    import db
    import memecoin_desk
    memecoin_desk._ensure()
    rows = db.query("SELECT * FROM memecoin_reservations WHERE case_id = ? AND side = ?", (case_id, side))
    for proof in rows:
        if db.exclusive(lambda conn, item=proof: memecoin_desk._reservation_status(conn, item)) in {"active", "unknown"}:
            return proof
    return None


def _supervise_reservations(transport, now: float) -> list:
    """Every stored entry and exit reservation, before discovery and without a new quote."""
    import json
    import db
    import memecoin_desk
    memecoin_desk._ensure()
    found = []
    seen = set()
    try:
        rows = db.query("SELECT * FROM memecoin_reservations")
    except Exception:
        rows = []
    for proof in rows:
        status = db.exclusive(lambda conn, item=proof: memecoin_desk._reservation_status(conn, item))
        if status in {"consumed", "failed"}:
            continue
        seen.add((proof["case_id"], proof["message_sha256"]))
        try:
            pre_sign = approach_signer(proof["case_id"], proof["message_sha256"], transport, now)
        except Exception:
            pre_sign = {"sign": False, "fee_estimate_blocked": True, "signer_reached": False, "reason": "fee_estimate_unreadable"}
        try:
            decision = execution_gate(proof["case_id"], proof["message_sha256"], transport, now)
        except Exception:
            decision = {
                "execution_eligible": False, "reason": "blockhash_validity_missing", "holds": True,
                "resend": False, "rewritten": False, "requoted": False, "sign": False, "send": False,
                "simulate": False, "operation": "unimplemented", "message_sha256": proof["message_sha256"],
                "price_freshness_proven": False, "gate_complete": False,
            }
        if decision.get("reason") == "reservation_unknown" or decision.get("reservation_valid") is not True:
            preflight = _preflight_report(attempted=False, reason=decision.get("reason"), holds=True, resend=False)
        else:
            try:
                preflight = preflight_reserved(proof["case_id"], proof["message_sha256"], transport, now)
            except Exception:
                preflight = _preflight_report(attempted=True, reason="simulation_transport", holds=True, resend=False)
        case = memecoin_desk._case(proof["case_id"])
        found.append({
            "case_id": proof["case_id"], "mint": None if case is None else case["mint"], "side": proof["side"],
            "jupiter_called": False, "requoted": False, "supervised": True, "execution": decision,
            "preflight": preflight,
            "pre_sign": pre_sign,
            "sign": False,
            "message_sha256": proof["message_sha256"],
        })
    try:
        cases = db.query("SELECT id, mint, snapshot_json FROM memecoin_cases")
    except Exception:
        cases = []
    for case in cases:
        try:
            snap = json.loads(case["snapshot_json"])
        except Exception:
            continue
        if not isinstance(snap, dict):
            continue
        for key, side in (("entry_inflight", "entry"), ("inflight", "exit")):
            flight = snap.get(key)
            if not isinstance(flight, dict):
                continue
            digest = flight.get("message_sha256")
            if (case["id"], digest) in seen:
                continue
            found.append({
                "case_id": case["id"], "mint": case["mint"], "side": side, "jupiter_called": False,
                "requoted": False, "supervised": True, "message_sha256": digest,
                "execution": {
                    "execution_eligible": False, "reason": "reservation_proof_missing", "holds": True,
                    "resend": False, "rewritten": False, "requoted": False, "sign": False, "send": False,
                    "simulate": False, "operation": "unimplemented", "message_sha256": digest,
                    "price_freshness_proven": False, "gate_complete": False, "quote_expiry_proven": False,
                },
            })
    return found


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
        if _durable_reservation(row["id"], "exit") is not None or isinstance(snap.get("inflight"), dict):
            found.append({"mint": row["mint"], "reason": "exit_inflight", "jupiter_called": False, "supervised": True, "resend": False, "holds": True, "rewritten": False, "requoted": False, "message_sha256": None if not isinstance(snap.get("inflight"), dict) else snap["inflight"].get("message_sha256")})
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
            marked = memecoin_desk.mark_open_position(row["id"], transport, now)
            if marked.get("action") not in {"stop", "trail"}:
                found.append({"mint": row["mint"], "reason": marked.get("reason") or "exit_hold", "action": marked.get("action"), "jupiter_called": False, "supervised": True, "mark_price": marked.get("mark_price")})
                continue
        elif memecoin_desk._matching_intent(row["id"]) is None:
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
            "execution": quote.get("execution") if isinstance(quote.get("execution"), dict) else {"execution_eligible": False, "operation": "unimplemented", "sign": False, "send": False, "simulate": False, "gate_complete": False},
        })
    return found


def synthetic_screen(mint: str, features: dict, transport, now: float) -> dict:
    """Test-only handoff. It does not reserve live capital and the live round does not call it."""
    quiet = {"ok": False, "send": False, "sign": False, "live_ready": False, "live_capital": False, "ledger": "synthetic", "jupiter_called": False}
    if isinstance(features, dict) and (features.get("ledger") == "live" or features.get("live_capital") is True or features.get("live") is True):
        return quiet | {"reason": "live_refused", "signer_reached": False}
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
            return quiet | {
                "reason": analyzed.get("reason"), "stage": analyzed.get("stage"),
                "sign": False, "send": False, "signer_reached": False,
            }
    elif opened.get("reason") != "already_open":
        return quiet | {"reason": opened.get("reason")}
    return quiet | {
        "ok": False, "reason": "synthetic_stops_before_signer",
        "sign": False, "send": False, "signer_reached": False, "live_ready": False,
    }


def _halt_entries() -> None:
    import db
    try:
        db.set_setting("memecoin_entry_halt", "1", "scheduler_fault")
    except Exception:
        return


def _typed_chain_err(err) -> str | None:
    import json
    if err is None:
        return "null"
    if isinstance(err, str) and err != "":
        return json.dumps(err)
    if isinstance(err, dict):
        return json.dumps(err, sort_keys=True, separators=(",", ":"))
    return None


def _typed_chain_fee(fee) -> int | None:
    if isinstance(fee, bool) or not isinstance(fee, int) or fee < 0 or fee >= 2**63:
        return None
    return fee


def _lamport_balances_typed(meta: dict, count: int) -> bool:
    for name in ("preBalances", "postBalances"):
        values = meta.get(name)
        if not isinstance(values, list) or len(values) != count:
            return False
        if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in values):
            return False
    return True


def _token_balances_typed(meta: dict, count: int) -> bool:
    for name in ("preTokenBalances", "postTokenBalances"):
        if name not in meta:
            continue
        rows = meta.get(name)
        if not isinstance(rows, list):
            return False
        for item in rows:
            if not isinstance(item, dict):
                return False
            index = item.get("accountIndex")
            mint = item.get("mint")
            owner = item.get("owner")
            amount = item.get("uiTokenAmount")
            if isinstance(index, bool) or not isinstance(index, int) or index < 0 or index >= count:
                return False
            if not isinstance(mint, str) or mint == "" or not isinstance(owner, str) or owner == "":
                return False
            if not isinstance(amount, dict):
                return False
            raw = amount.get("amount")
            decimals = amount.get("decimals")
            if not isinstance(raw, str) or not raw.isdigit():
                return False
            if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 18:
                return False
    return True


def _classic_layout(decoded) -> bool:
    import solana_jupiter
    import solana_tx
    if decoded.version != "legacy" or decoded.lookups or decoded.required_signatures != 1:
        return False
    allowed = {solana_tx.COMPUTE_BUDGET, solana_tx.SYSTEM_PROGRAM, solana_tx.ASSOCIATED_TOKEN, solana_tx.TOKEN_PROGRAM, solana_jupiter.PROGRAM_ID}
    try:
        programs = [decoded.static_keys[item.program_index] for item in decoded.instructions]
    except (IndexError, TypeError):
        return False
    return all(program in allowed for program in programs)


def _landed_hold(case_id: str, signature, reason: str, now: float) -> dict:
    import db
    import memecoin_desk
    if isinstance(case_id, str) and case_id != "":
        def note(conn):
            conn.execute(
                "INSERT INTO memecoin_landed_notes (signature, case_id, reason, created_at) VALUES (?, ?, ?, ?)",
                (signature if isinstance(signature, str) else None, case_id, reason, float(now)),
            )
        try:
            memecoin_desk._ensure()
            db.exclusive(note)
        except Exception:
            pass
    return {
        "ok": False, "recorded": False, "idempotent": False, "reason": reason,
        "holds": True, "resend": False, "send": False, "sign": False, "live_ready": False,
        "fill_proven": False, "deposit_proven": False, "refund_proven": False, "quantity_proven": False,
        "execution_eligible": False, "ticket_limit_proven": False, "allocation_proven": False, "rent_bound_proven": False,
    }


def record_landed_evidence(case_id: str, signature: str, transport, now: float) -> dict:
    """Bind one known signature to its reserved message. A caller receipt is not an argument.

    This records the landed transaction and its network fee. It does not record a
    fill, release a hold, or authorize a close. Token quantity stays unproven until
    the mint decimals and the reserved token account are proven from account bytes.
    """
    import base64
    import db
    import hashlib
    import memecoin_desk
    import phantom_transport
    import solana_inspect
    import solana_tx
    import sqlite3
    if not callable(transport):
        return _landed_hold(case_id if isinstance(case_id, str) else "", signature, "receipt_source", now)
    if not isinstance(signature, str) or signature == "" or not isinstance(case_id, str) or case_id == "":
        return _landed_hold(case_id if isinstance(case_id, str) else "", signature, "receipt_unknown", now)
    memecoin_desk._ensure()
    try:
        genesis = transport({"method": "getGenesisHash"})
        landed = transport({"method": "getTransaction", "signature": signature, "commitment": "confirmed"})
        status = transport({"method": "getSignatureStatuses", "signature": signature, "commitment": "confirmed"})
    except Exception:
        return _landed_hold(case_id, signature, "receipt_unknown", now)
    genesis_body = genesis.get("body") if isinstance(genesis, dict) else None
    if not isinstance(genesis_body, dict) or genesis_body.get("result") != solana_inspect.MAINNET_GENESIS:
        return _landed_hold(case_id, signature, "genesis", now)
    pulled = landed.get("body") if isinstance(landed, dict) and landed.get("status") == 200 else None
    result = pulled.get("result") if isinstance(pulled, dict) else None
    if result is None:
        return _landed_hold(case_id, signature, "receipt_unconfirmed", now)
    if not isinstance(result, dict) or not isinstance(result.get("meta"), dict):
        return _landed_hold(case_id, signature, "receipt_unknown", now)
    meta = result["meta"]
    err = _typed_chain_err(meta.get("err"))
    fee = _typed_chain_fee(meta.get("fee"))
    encoded = result.get("transaction")
    slot = result.get("slot")
    if err is None or fee is None or not isinstance(encoded, list) or not encoded or not isinstance(encoded[0], str):
        return _landed_hold(case_id, signature, "receipt_unproven", now)
    if isinstance(slot, bool) or not isinstance(slot, int) or slot <= 0:
        return _landed_hold(case_id, signature, "slot_unproven", now)
    status_body = status.get("body") if isinstance(status, dict) and status.get("status") == 200 else None
    status_result = status_body.get("result") if isinstance(status_body, dict) else None
    values = status_result.get("value") if isinstance(status_result, dict) else None
    state = values[0] if isinstance(values, list) and len(values) == 1 else None
    if not isinstance(state, dict):
        return _landed_hold(case_id, signature, "receipt_unproven", now)
    confirmation = state.get("confirmationStatus")
    if confirmation not in {"confirmed", "finalized"} or state.get("slot") != slot or _typed_chain_err(state.get("err")) != err:
        return _landed_hold(case_id, signature, "receipt_unproven", now)
    try:
        tx = encoded[0]
        if solana_tx.first_signature(tx) != signature:
            return _landed_hold(case_id, signature, "signature_mismatch", now)
        message = solana_tx.message_bytes(tx)
        digest = hashlib.sha256(message).hexdigest()
        decoded = solana_tx.decode_transaction(base64.b64decode(tx, validate=True))
    except Exception:
        return _landed_hold(case_id, signature, "receipt_unproven", now)
    if decoded.fee_payer != phantom_transport.REQUIRED_WALLET:
        return _landed_hold(case_id, signature, "wallet_unbound", now)
    proofs = db.query("SELECT * FROM memecoin_reservations WHERE message_sha256 = ? AND case_id = ?", (digest, case_id))
    if len(proofs) != 1:
        return _landed_hold(case_id, signature, "message_mismatch", now)
    proof = proofs[0]
    try:
        stored = solana_tx.message_bytes(proof["message_b64"])
    except Exception:
        return _landed_hold(case_id, signature, "message_mismatch", now)
    if stored != message or proof["side"] not in {"entry", "exit"}:
        return _landed_hold(case_id, signature, "message_mismatch", now)
    if not _lamport_balances_typed(meta, len(decoded.static_keys)) or not _token_balances_typed(meta, len(decoded.static_keys)):
        return _landed_hold(case_id, signature, "balances_untyped", now)
    layout = 1 if _classic_layout(decoded) else 0
    existing = db.query(
        "SELECT * FROM memecoin_landed_evidence WHERE signature = ? AND message_sha256 = ?",
        (signature, digest),
    )
    if existing:
        row = existing[0]
        same = (
            row["case_id"] == case_id and row["side"] == proof["side"] and row["slot"] == slot
            and row["confirmation_status"] == confirmation and row["network_fee_lamports"] == fee
            and row["meta_err"] == err and row["wallet"] == decoded.fee_payer and row["layout_supported"] == layout
        )
        if not same:
            return _landed_hold(case_id, signature, "evidence_conflict", now)
        return {
            "ok": True, "recorded": True, "idempotent": True, "reason": None, "signature": signature,
            "case_id": case_id, "side": proof["side"], "message_sha256": digest, "slot": slot,
            "confirmation_status": confirmation, "network_fee_lamports": fee, "meta_err": err,
            "layout_supported": layout == 1, "fill_proven": False, "deposit_proven": False,
            "refund_proven": False, "quantity_proven": False, "holds": True, "resend": False,
            "send": False, "sign": False, "live_ready": False, "execution_eligible": False,
            "ticket_limit_proven": False, "allocation_proven": False, "rent_bound_proven": False,
            "failed": err != "null",
        }

    def work(conn):
        conn.execute(
            """
            INSERT INTO memecoin_landed_evidence (
                signature, message_sha256, case_id, side, slot, confirmation_status,
                network_fee_lamports, meta_err, wallet, layout_supported, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (signature, digest, case_id, proof["side"], slot, confirmation, fee, err, decoded.fee_payer, layout, float(now)),
        )
        conn.execute(
            "INSERT INTO memecoin_landed_notes (signature, case_id, reason, created_at) VALUES (?, ?, 'landed', ?)",
            (signature, case_id, float(now)),
        )

    try:
        db.exclusive(work)
    except sqlite3.IntegrityError as exc:
        if "landed_evidence" not in str(exc):
            raise
        again = db.query("SELECT signature FROM memecoin_landed_evidence WHERE signature = ? AND message_sha256 = ?", (signature, digest))
        if not again:
            return _landed_hold(case_id, signature, "evidence_conflict", now)
    return {
        "ok": True, "recorded": True, "idempotent": False, "reason": None, "signature": signature,
        "case_id": case_id, "side": proof["side"], "message_sha256": digest, "slot": slot,
        "confirmation_status": confirmation, "network_fee_lamports": fee, "meta_err": err,
        "layout_supported": layout == 1, "fill_proven": False, "deposit_proven": False,
        "refund_proven": False, "quantity_proven": False, "holds": True, "resend": False,
        "send": False, "sign": False, "live_ready": False, "execution_eligible": False,
        "ticket_limit_proven": False, "allocation_proven": False, "rent_bound_proven": False,
        "failed": err != "null", "next_missing": "mint_decimals_and_reserved_token_account",
    }


def _u64_canon(value) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value >= 2**64:
        return None
    return str(value)


def _account_context(result, tip: int) -> tuple[int | None, str | None]:
    import jupiter_route_v2
    if not isinstance(result, dict):
        return None, "quantity_context_missing"
    context = result.get("context")
    if not isinstance(context, dict) or "slot" not in context:
        return None, "quantity_context_missing"
    slot = context.get("slot")
    if isinstance(slot, bool) or not isinstance(slot, int) or slot <= 0:
        return None, "quantity_context_malformed"
    if slot < tip or slot > tip + jupiter_route_v2.SLOT_LAG_MAX:
        return None, "quantity_context_stale" if slot < tip else "quantity_context_future"
    return slot, None


def _read_current_account(transport, address: str, purpose: str, tip: int) -> tuple[dict | None, str | None, int | None]:
    try:
        response = transport({
            "method": "getAccountInfo", "purpose": purpose, "account": address,
            "commitment": "confirmed", "minContextSlot": tip,
        })
    except Exception:
        return None, "quantity_transport", None
    body = response.get("body") if isinstance(response, dict) else None
    result = body.get("result") if isinstance(body, dict) else None
    slot, reason = _account_context(result, tip)
    if reason is not None:
        return None, reason, None
    value = result.get("value")
    if value is None:
        return None, "quantity_account_missing", slot
    if not isinstance(value, dict) or value.get("executable") is not False:
        return None, "quantity_account_executable", slot
    return value, None, slot


def _classic_mint_decimals(value: dict) -> tuple[int | None, str | None]:
    import solana_tx
    if value.get("owner") == solana_tx.TOKEN_2022:
        return None, "token_2022_unsupported"
    if value.get("owner") != solana_tx.TOKEN_PROGRAM:
        return None, "mint_owner"
    raw = _decoded_account_data(value.get("data"))
    if raw is None or len(raw) != 82 or raw[45] != 1:
        return None, "mint_layout"
    decimals = raw[44]
    if not 0 <= decimals <= 18:
        return None, "mint_decimals"
    return decimals, None


def _classic_token_identity(value: dict, mint: str, wallet: str) -> str | None:
    import solana_tx
    if value.get("owner") != solana_tx.TOKEN_PROGRAM:
        return "token_owner"
    raw = _decoded_account_data(value.get("data"))
    if raw is None or len(raw) != solana_tx.TOKEN_ACCOUNT_SPACE or raw[108] != 1:
        return "token_layout"
    if solana_tx.b58encode(raw[0:32]) != mint or solana_tx.b58encode(raw[32:64]) != wallet:
        return "token_identity"
    return None


def _historical_token_row(rows, index: int) -> tuple[dict | None, str | None]:
    if rows is None:
        return None, "historical_missing"
    hits = []
    if not isinstance(rows, list):
        return None, "historical_untyped"
    for item in rows:
        if isinstance(item, dict) and item.get("accountIndex") == index:
            hits.append(item)
    if len(hits) > 1:
        return None, "historical_duplicate"
    if len(hits) == 0:
        return None, "historical_missing"
    item = hits[0]
    shown = item.get("uiTokenAmount")
    raw = shown.get("amount") if isinstance(shown, dict) else None
    decimals = shown.get("decimals") if isinstance(shown, dict) else None
    if not isinstance(raw, str) or not raw.isdigit() or (len(raw) > 1 and raw.startswith("0")):
        return None, "historical_amount"
    if isinstance(decimals, bool) or not isinstance(decimals, int):
        return None, "historical_decimals"
    try:
        amount = int(raw)
    except Exception:
        return None, "historical_amount"
    if amount < 0 or amount >= 2**64:
        return None, "historical_amount"
    if not isinstance(item.get("mint"), str) or not isinstance(item.get("owner"), str):
        return None, "historical_untyped"
    return {"amount": amount, "decimals": decimals, "mint": item["mint"], "owner": item["owner"]}, None


def record_landed_quantity(case_id: str, signature: str, transport, now: float) -> dict:
    """Quantity from the landed token metadata and the current mint and token account.

    The current account read proves the mint decimals and the account identity now.
    It is not the balance at the landed slot. minContextSlot does not select the
    account as it existed at that slot. A missing historical pre or post balance
    is not zero. A failed transaction is not a fill. The historical body has to be
    the same signature, message, success, fee, and slot as the stored evidence.
    """
    import db
    import memecoin_desk
    import phantom_transport
    import solana_tx
    import sqlite3
    evidence = record_landed_evidence(case_id, signature, transport, now)
    if evidence.get("recorded") is not True:
        return evidence | {"quantity_proven": False, "quantity_recorded": False}
    if evidence.get("failed") is True or evidence.get("meta_err") not in (None, "null"):
        return evidence | {"quantity_proven": False, "quantity_recorded": False, "quantity_reason": "failed_not_a_fill", "holds": True, "resend": False}
    if evidence.get("layout_supported") is not True:
        return evidence | {"quantity_proven": False, "quantity_recorded": False, "quantity_reason": "layout_unsupported", "holds": True, "resend": False}
    try:
        landed = transport({"method": "getTransaction", "signature": signature, "commitment": "confirmed"})
        tip_response = transport({"method": "getSlot"})
    except Exception:
        return evidence | {"quantity_proven": False, "quantity_recorded": False, "quantity_reason": "quantity_transport", "holds": True, "resend": False}
    body = landed.get("body") if isinstance(landed, dict) else None
    result = body.get("result") if isinstance(body, dict) else None
    meta = result.get("meta") if isinstance(result, dict) else None
    encoded = result.get("transaction") if isinstance(result, dict) else None
    if not isinstance(meta, dict) or not isinstance(encoded, list) or not encoded:
        return evidence | {"quantity_proven": False, "quantity_recorded": False, "quantity_reason": "historical_missing", "holds": True, "resend": False}
    bound = _evidence_body(signature, case_id, result)
    if bound is None:
        return evidence | {"quantity_proven": False, "quantity_recorded": False, "quantity_reason": "quantity_identity", "holds": True, "resend": False}
    decoded = bound["decoded"]
    meta = bound["meta"]
    import memecoin_exit
    route = memecoin_exit._route(decoded)
    case = db.query("SELECT mint FROM memecoin_cases WHERE id = ?", (case_id,))
    case_mint = case[0]["mint"] if case else None
    if route is None or not isinstance(case_mint, str):
        return evidence | {"quantity_proven": False, "quantity_recorded": False, "quantity_reason": "route_unbound", "holds": True, "resend": False}
    wallet = phantom_transport.REQUIRED_WALLET
    if evidence["side"] == "entry":
        account = route["keys"][2]
        mint = route["destination_mint"]
        direction = "bought"
        if route["source_mint"] != solana_tx.NATIVE_MINT or mint != case_mint:
            return evidence | {"quantity_proven": False, "quantity_recorded": False, "quantity_reason": "route_unbound", "holds": True, "resend": False}
    else:
        account = route["keys"][1]
        mint = route["source_mint"]
        direction = "sold"
        if route["destination_mint"] != solana_tx.NATIVE_MINT or mint != case_mint:
            return evidence | {"quantity_proven": False, "quantity_recorded": False, "quantity_reason": "route_unbound", "holds": True, "resend": False}
    try:
        canonical = solana_tx.associated_token_address(wallet, mint, solana_tx.TOKEN_PROGRAM)
    except Exception:
        return evidence | {"quantity_proven": False, "quantity_recorded": False, "quantity_reason": "route_unbound", "holds": True, "resend": False}
    if account != canonical or account not in decoded.static_keys:
        return evidence | {"quantity_proven": False, "quantity_recorded": False, "quantity_reason": "route_unbound", "holds": True, "resend": False}
    index = decoded.static_keys.index(account)
    pre, pre_reason = _historical_token_row(meta.get("preTokenBalances"), index)
    post, post_reason = _historical_token_row(meta.get("postTokenBalances"), index)
    if pre is None or post is None:
        return evidence | {"quantity_proven": False, "quantity_recorded": False, "quantity_reason": pre_reason if pre is None else post_reason, "holds": True, "resend": False}
    if pre["mint"] != mint or post["mint"] != mint or pre["owner"] != wallet or post["owner"] != wallet:
        return evidence | {"quantity_proven": False, "quantity_recorded": False, "quantity_reason": "historical_identity", "holds": True, "resend": False}
    if pre["decimals"] != post["decimals"]:
        return evidence | {"quantity_proven": False, "quantity_recorded": False, "quantity_reason": "historical_decimals", "holds": True, "resend": False}
    tip_body = tip_response.get("body") if isinstance(tip_response, dict) else None
    tip = tip_body.get("result") if isinstance(tip_body, dict) else None
    if isinstance(tip, bool) or not isinstance(tip, int) or tip <= 0:
        return evidence | {"quantity_proven": False, "quantity_recorded": False, "quantity_reason": "quantity_context_missing", "holds": True, "resend": False}
    mint_value, mint_reason, _mint_slot = _read_current_account(transport, mint, "mint_decimals", tip)
    if mint_reason is not None:
        return evidence | {"quantity_proven": False, "quantity_recorded": False, "quantity_reason": mint_reason, "holds": True, "resend": False, "current_is_not_landed_balance": True}
    decimals, decimal_reason = _classic_mint_decimals(mint_value)
    if decimal_reason is not None or decimals != pre["decimals"]:
        return evidence | {"quantity_proven": False, "quantity_recorded": False, "quantity_reason": decimal_reason or "mint_decimals", "holds": True, "resend": False, "current_is_not_landed_balance": True}
    token_value, token_reason, _token_slot = _read_current_account(transport, account, "token_account", tip)
    if token_reason is not None:
        return evidence | {"quantity_proven": False, "quantity_recorded": False, "quantity_reason": token_reason, "holds": True, "resend": False, "current_is_not_landed_balance": True}
    identity = _classic_token_identity(token_value, mint, wallet)
    if identity is not None:
        return evidence | {"quantity_proven": False, "quantity_recorded": False, "quantity_reason": identity, "holds": True, "resend": False, "current_is_not_landed_balance": True}
    if direction == "bought":
        if post["amount"] < pre["amount"]:
            return evidence | {"quantity_proven": False, "quantity_recorded": False, "quantity_reason": "quantity_sign", "holds": True, "resend": False}
        delta = post["amount"] - pre["amount"]
    else:
        if pre["amount"] < post["amount"]:
            return evidence | {"quantity_proven": False, "quantity_recorded": False, "quantity_reason": "quantity_sign", "holds": True, "resend": False}
        delta = pre["amount"] - post["amount"]
    pre_text, post_text, qty_text = _u64_canon(pre["amount"]), _u64_canon(post["amount"]), _u64_canon(delta)
    if pre_text is None or post_text is None or qty_text is None:
        return evidence | {"quantity_proven": False, "quantity_recorded": False, "quantity_reason": "historical_amount", "holds": True, "resend": False}
    prior = db.query(
        "SELECT * FROM memecoin_landed_quantity WHERE signature = ? AND message_sha256 = ? AND account = ?",
        (signature, evidence["message_sha256"], account),
    )
    if prior:
        row = prior[0]
        same = row["quantity_raw"] == qty_text and row["pre_amount"] == pre_text and row["post_amount"] == post_text and row["decimals"] == decimals and row["direction"] == direction
        return evidence | {
            "quantity_proven": same, "quantity_recorded": same, "idempotent": same,
            "quantity_reason": None if same else "quantity_conflict",
            "quantity_raw": qty_text if same else None, "direction": direction if same else None,
            "decimals": decimals if same else None, "holds": True, "resend": False,
            "fill_proven": False, "cost_basis_proven": False, "refund_proven": False,
            "current_is_not_landed_balance": True,
        }

    def work(conn):
        conn.execute(
            """
            INSERT INTO memecoin_landed_quantity (
                signature, message_sha256, case_id, side, account, mint, wallet, decimals,
                pre_amount, post_amount, quantity_raw, direction, landed_slot, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signature, evidence["message_sha256"], case_id, evidence["side"], account, mint, wallet, decimals,
                pre_text, post_text, qty_text, direction, evidence["slot"], float(now),
            ),
        )

    try:
        db.exclusive(work)
    except sqlite3.IntegrityError as exc:
        if "landed_quantity" not in str(exc):
            raise
        return evidence | {"quantity_proven": False, "quantity_recorded": False, "quantity_reason": "quantity_conflict", "holds": True, "resend": False}
    return evidence | {
        "quantity_proven": True, "quantity_recorded": True, "idempotent": False, "quantity_reason": None,
        "quantity_raw": qty_text, "direction": direction, "decimals": decimals, "account": account, "mint": mint,
        "pre_amount": pre_text, "post_amount": post_text, "holds": True, "resend": False,
        "fill_proven": False, "cost_basis_proven": False, "refund_proven": False, "deposit_proven": False,
        "execution_eligible": False, "ticket_limit_proven": False, "allocation_proven": False,
        "current_is_not_landed_balance": True,
        "next_missing": "authoritative_receipt_lineage",
    }


def _evidence_body(signature: str, case_id: str, result):
    """The historical transaction body is the stored evidence, or it is not usable.

    Signature, message bytes, success, fee, and slot all have to be that row.
    A later response cannot borrow an earlier message hash.
    """
    import base64
    import hashlib
    import db
    import phantom_transport
    import solana_tx
    if not isinstance(result, dict):
        return None
    meta = result.get("meta")
    encoded = result.get("transaction")
    slot = result.get("slot")
    if not isinstance(meta, dict) or not isinstance(encoded, list) or not encoded or not isinstance(encoded[0], str):
        return None
    err = _typed_chain_err(meta.get("err"))
    fee = _typed_chain_fee(meta.get("fee"))
    if err != "null" or fee is None or isinstance(slot, bool) or not isinstance(slot, int) or slot <= 0:
        return None
    try:
        raw_tx = encoded[0]
        if solana_tx.first_signature(raw_tx) != signature:
            return None
        message = solana_tx.message_bytes(raw_tx)
        digest = hashlib.sha256(message).hexdigest()
        decoded = solana_tx.decode_transaction(base64.b64decode(raw_tx, validate=True))
    except Exception:
        return None
    if not _classic_layout(decoded) or decoded.fee_payer != phantom_transport.REQUIRED_WALLET:
        return None
    rows = db.query(
        "SELECT * FROM memecoin_landed_evidence WHERE signature = ? AND message_sha256 = ?",
        (signature, digest),
    )
    if len(rows) != 1:
        return None
    proof = rows[0]
    if proof["case_id"] != case_id or proof["meta_err"] != "null" or proof["layout_supported"] != 1:
        return None
    if proof["network_fee_lamports"] != fee or proof["slot"] != slot or proof["wallet"] != decoded.fee_payer:
        return None
    reserved = db.query(
        "SELECT message_b64 FROM memecoin_reservations WHERE message_sha256 = ? AND case_id = ?",
        (digest, case_id),
    )
    if len(reserved) != 1:
        return None
    try:
        if solana_tx.message_bytes(reserved[0]["message_b64"]) != message:
            return None
    except Exception:
        return None
    return {"decoded": decoded, "meta": meta, "digest": digest, "fee": fee, "slot": slot, "evidence": proof}


def _quantity_matches_body(observed, decoded, meta, slot: int) -> bool:
    """Stored pre/post metadata has to be this body's token row, not an earlier one."""
    account = observed["account"]
    if observed["landed_slot"] != slot or account not in decoded.static_keys:
        return False
    index = decoded.static_keys.index(account)
    pre, _pre_reason = _historical_token_row(meta.get("preTokenBalances"), index)
    post, _post_reason = _historical_token_row(meta.get("postTokenBalances"), index)
    if pre is None or post is None:
        return False
    return (
        _u64_canon(pre["amount"]) == observed["pre_amount"]
        and _u64_canon(post["amount"]) == observed["post_amount"]
        and pre["mint"] == post["mint"] == observed["mint"]
        and pre["owner"] == post["owner"] == observed["wallet"]
        and pre["decimals"] == post["decimals"] == observed["decimals"]
    )


def _flow_gap(base: dict, reason: str) -> dict:
    """A missing swap leg stays incomplete. It is not a receipt and not a cost."""
    return base | {
        "flow_recorded": False, "flow_proven": False, "flow_reason": reason,
        "receipt_booked": False, "composition_complete": False, "price_proven": False,
        "dex_fee_separated": False, "dex_fee_lamports": None, "network_fee_added_to_input": False,
        "dex_fee_embedded_in_input": True, "one_shot_fee_used": False, "combined_fee_cap_proven": False,
        "wrap_principal_netted": False, "rent_netted": False, "payer_delta_used": False,
        "pool_bytes_at_landed_slot": False, "pool_bytes_fetched": False, "pool_pda_rederived": False,
        "account_read_is_historical": False, "vault_binding": None,
        "hold_released": False, "reservation_consumed": False,
        "cost_basis_proven": False, "refund_proven": False, "fill_proven": False,
        "holds": True, "resend": False, "execution_eligible": False,
    }


def _swap_legs(decoded):
    """User accounts and the route's vault positions. Pool bytes are not in the transaction."""
    import jupiter_route_v2
    import solana_jupiter
    import solana_whirlpool
    import solana_tx
    swaps = []
    for index, instruction in enumerate(decoded.instructions):
        if decoded.static_keys[instruction.program_index] == solana_jupiter.PROGRAM_ID:
            swaps.append((index, instruction))
    if len(swaps) != 1:
        return None
    index, instruction = swaps[0]
    parsed = jupiter_route_v2.parse(bytes(instruction.data))
    if parsed.get("hop") != "Whirlpool" or parsed.get("a_to_b") not in (True, False):
        return None
    try:
        accounts = []
        for item in instruction.account_indexes:
            signer, writable = decoded.account_flags(item)
            accounts.append({"pubkey": decoded.static_keys[item], "isSigner": signer, "isWritable": writable})
    except Exception:
        return None
    bound = jupiter_route_v2._fixed_accounts(accounts)
    remaining = bound.get("remaining") if bound.get("ok") is True else None
    if not isinstance(remaining, list) or len(remaining) != 12:
        return None
    hop = [accounts[item]["pubkey"] for item in remaining]
    if hop[0] != solana_whirlpool.PROGRAM_ID or hop[1] != solana_tx.TOKEN_PROGRAM or hop[2] != bound["user"]:
        return None
    if parsed["a_to_b"] is True:
        user_in, vault_in, user_out, vault_out = hop[4], hop[5], hop[6], hop[7]
    else:
        user_in, vault_in, user_out, vault_out = hop[6], hop[7], hop[4], hop[5]
    if user_in != bound["source"] or user_out != bound["destination"]:
        return None
    if len({user_in, user_out, vault_in, vault_out, hop[3]}) != 5:
        return None
    return {
        "index": index, "pool": hop[3], "user": bound["user"],
        "source": bound["source"], "destination": bound["destination"],
        "source_mint": bound["source_mint"], "destination_mint": bound["destination_mint"],
        "vault_in": vault_in, "vault_out": vault_out,
        "a_to_b": parsed["a_to_b"] is True,
        "swap_accounts": hop[1:],
    }


def _classic_inner_transfer(item, decoded):
    """A classic SPL Transfer inside the swap. TransferChecked and Token-2022 stay unparsed."""
    import solana_tx
    if not isinstance(item, dict):
        return None
    program_index = item.get("programIdIndex")
    accounts = item.get("accounts")
    data = item.get("data")
    if isinstance(program_index, bool) or not isinstance(program_index, int):
        return None
    if program_index < 0 or program_index >= len(decoded.static_keys):
        return None
    program = decoded.static_keys[program_index]
    if program != solana_tx.TOKEN_PROGRAM:
        return program
    if not isinstance(accounts, list) or len(accounts) != 3 or not isinstance(data, str):
        return None
    try:
        raw = solana_tx.b58decode(data, 9)
    except Exception:
        return None
    if len(raw) != 9 or raw[0] != 3 or solana_tx.b58encode(raw) != data:
        return None
    amount = int.from_bytes(raw[1:9], "little")
    if amount <= 0 or amount >= 2**64:
        return None
    keys = []
    for index in accounts:
        if isinstance(index, bool) or not isinstance(index, int) or index < 0 or index >= len(decoded.static_keys):
            return None
        keys.append(decoded.static_keys[index])
    return {"source": keys[0], "destination": keys[1], "authority": keys[2], "amount": amount}


# Anchor discriminator for pub fn swap. swap_v2 and two_hop_swap are different instructions.
# Source: orca-so/whirlpools@2509ad9d9894a38c922e6e84a6a3a9de5e9ccd2d
# programs/whirlpool/src/lib.rs declare_id and pub fn swap,
# programs/whirlpool/src/instructions/swap.rs vault constraints,
# programs/whirlpool/src/util/swap_utils.rs perform_swap.
_WHIRLPOOL_SWAP_DISC = bytes.fromhex("f8c69e91e17587c8")
_WHIRLPOOL_SWAP_V2_DISC = bytes.fromhex("2b04ed0b1ac91e62")
_WHIRLPOOL_TWO_HOP_DISC = bytes.fromhex("c360ed6c44a2dbe6")
_WHIRLPOOL_SWAP_SOURCE = "orca-so/whirlpools@2509ad9d9894a38c922e6e84a6a3a9de5e9ccd2d"
_VAULT_BINDING = "whirlpool_swap_constraint"
# Anchor event discriminator for Traded in programs/whirlpool/src/events.rs at that commit.
# swap.rs emits it after perform_swap. lp_fee is this swap's fee minus its protocol cut.
_TRADED_DISC = bytes.fromhex("e1ca49af932ba096")
_TRADED_LEN = 121


def _b58_exact(data: str, size: int):
    import solana_tx
    try:
        raw = solana_tx.b58decode(data, size)
    except Exception:
        return None
    if len(raw) != size or solana_tx.b58encode(raw) != data:
        return None
    return raw


def _inner_program(item, decoded):
    if not isinstance(item, dict):
        return None
    index = item.get("programIdIndex")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0 or index >= len(decoded.static_keys):
        return None
    return decoded.static_keys[index]


def _stack_height(item):
    height = item.get("stackHeight") if isinstance(item, dict) else None
    if isinstance(height, bool) or not isinstance(height, int) or height < 2:
        return None
    return height


def _whirlpool_swap_data(raw: bytes):
    """Canonical exact-in swap. A different discriminator is not this instruction."""
    if len(raw) != 42:
        return None, "whirlpool_discriminator"
    disc = raw[:8]
    if disc in {_WHIRLPOOL_SWAP_V2_DISC, _WHIRLPOOL_TWO_HOP_DISC}:
        return None, "whirlpool_variant"
    if disc != _WHIRLPOOL_SWAP_DISC:
        return None, "whirlpool_discriminator"
    amount = int.from_bytes(raw[8:16], "little")
    specified = raw[40]
    direction = raw[41]
    if specified not in (0, 1) or direction not in (0, 1) or amount <= 0:
        return None, "whirlpool_amount"
    if specified != 1:
        return None, "whirlpool_amount"
    return {"amount": amount, "a_to_b": direction == 1}, None


def _decode_whirlpool_swap(item, decoded):
    """The published 11-account swap. Extra accounts and swap_v2 stay unsupported."""
    import solana_tx
    import solana_whirlpool
    if _inner_program(item, decoded) != solana_whirlpool.PROGRAM_ID:
        return None, "whirlpool_program"
    accounts = item.get("accounts")
    data = item.get("data")
    if not isinstance(accounts, list) or len(accounts) != 11 or not isinstance(data, str):
        return None, "whirlpool_accounts"
    raw = _b58_exact(data, 42)
    if raw is None:
        return None, "whirlpool_discriminator"
    parsed, reason = _whirlpool_swap_data(raw)
    if parsed is None:
        return None, reason
    keys = []
    for index in accounts:
        if isinstance(index, bool) or not isinstance(index, int) or index < 0 or index >= len(decoded.static_keys):
            return None, "whirlpool_accounts"
        keys.append(decoded.static_keys[index])
    if keys[0] != solana_tx.TOKEN_PROGRAM:
        return None, "whirlpool_program"
    parsed["accounts"] = keys
    parsed["height"] = _stack_height(item)
    return parsed, None


def _attributed_transfers(meta, decoded, legs):
    """The two SPL transfers have to be the children of one canonical Whirlpool swap.

    A successful swap applies the pool's vault-field constraints to the accounts
    in that instruction. This does not fetch the pool account and does not rederive
    its PDA.     A transfer that is not nested under that swap is not attributed to it.
    """
    import solana_tx
    import solana_whirlpool
    groups = meta.get("innerInstructions")
    if groups is None:
        return None, "inner_transfers_missing"
    if not isinstance(groups, list):
        return None, "inner_transfers_untyped"
    rows = None
    for group in groups:
        if not isinstance(group, dict):
            return None, "inner_transfers_untyped"
        outer = group.get("index")
        instructions = group.get("instructions")
        if isinstance(outer, bool) or not isinstance(outer, int) or not isinstance(instructions, list):
            return None, "inner_transfers_untyped"
        for item in instructions:
            program = _inner_program(item, decoded)
            if program is None:
                return None, "inner_transfers_untyped"
            if program == solana_tx.TOKEN_2022:
                return None, "token_2022_unsupported"
            if program == solana_tx.SYSTEM_PROGRAM:
                return None, "inner_economic_unsupported"
            if program == solana_tx.TOKEN_PROGRAM and outer != legs["index"]:
                return None, "inner_transfer_outside_swap"
        if outer == legs["index"]:
            if rows is not None:
                return None, "whirlpool_swap_count"
            rows = instructions
    if rows is None:
        return None, "whirlpool_swap_missing"
    hops = [index for index, item in enumerate(rows) if _inner_program(item, decoded) == solana_whirlpool.PROGRAM_ID]
    if len(hops) == 0:
        return None, "whirlpool_swap_missing"
    if len(hops) != 1:
        return None, "whirlpool_swap_count"
    hop_index = hops[0]
    swap, reason = _decode_whirlpool_swap(rows[hop_index], decoded)
    if swap is None:
        return None, reason
    if swap["height"] is None or swap["accounts"] != legs["swap_accounts"] or swap["a_to_b"] is not legs["a_to_b"]:
        return None, "whirlpool_nesting" if swap["height"] is None else ("whirlpool_direction" if swap["a_to_b"] is not legs["a_to_b"] else "whirlpool_accounts")
    parent = swap["height"]
    children = []
    for item in rows[hop_index + 1:]:
        if _inner_program(item, decoded) == solana_whirlpool.PROGRAM_ID:
            return None, "whirlpool_swap_count"
        height = _stack_height(item)
        if height is None:
            return None, "whirlpool_nesting"
        if height <= parent:
            break
        if height != parent + 1:
            return None, "whirlpool_nesting"
        children.append(item)
    for item in rows[:hop_index]:
        if _inner_program(item, decoded) == solana_tx.TOKEN_PROGRAM:
            return None, "whirlpool_nesting"
    if len(children) != 2:
        return None, "inner_transfer_count" if len(children) > 2 else "whirlpool_nesting"
    transfers = []
    for item in children:
        if _inner_program(item, decoded) != solana_tx.TOKEN_PROGRAM:
            return None, "inner_economic_unsupported"
        parsed = _classic_inner_transfer(item, decoded)
        if not isinstance(parsed, dict):
            return None, "inner_transfer_unsupported"
        transfers.append(parsed)
    owner_a, vault_a, owner_b, vault_b = swap["accounts"][3], swap["accounts"][4], swap["accounts"][5], swap["accounts"][6]
    pool = swap["accounts"][2]
    user = swap["accounts"][1]
    if swap["a_to_b"]:
        expect_in = (owner_a, vault_a, user)
        expect_out = (vault_b, owner_b, pool)
    else:
        expect_in = (owner_b, vault_b, user)
        expect_out = (vault_a, owner_a, pool)
    got_in = (transfers[0]["source"], transfers[0]["destination"], transfers[0]["authority"])
    got_out = (transfers[1]["source"], transfers[1]["destination"], transfers[1]["authority"])
    if got_in != expect_in or got_out != expect_out:
        return None, "whirlpool_transfer"
    if transfers[0]["amount"] != swap["amount"]:
        return None, "whirlpool_amount"
    if transfers[0]["source"] != legs["source"] or transfers[0]["destination"] != legs["vault_in"]:
        return None, "whirlpool_transfer"
    if transfers[1]["source"] != legs["vault_out"] or transfers[1]["destination"] != legs["destination"]:
        return None, "whirlpool_transfer"
    return {"input": transfers[0], "output": transfers[1], "swap": swap}, None


def record_landed_flow(case_id: str, signature: str, transport, now: float) -> dict:
    """Join one historical transaction body to the landed quantity and network fee.

    The body that supplies the transfers has to be the stored signature, message,
    success, fee, and slot. Its token pre/post metadata has to be the stored
    quantity. One canonical Whirlpool swap, nested above those two transfers,
    supplies the vault binding: that instruction's published constraints require
    the vault accounts to be the pool's vault fields at execution. The pool
    account is not fetched and its PDA is not rederived. The gross source
    transfer is the native input and already includes the pool fee. The fee is
    not added again and is not taken from the one-shot formula. The separate
    combined-fee cap stays unproven. No receipt is booked.
    """
    import db
    import solana_tx
    import sqlite3
    quantity = record_landed_quantity(case_id, signature, transport, now)
    if quantity.get("quantity_recorded") is not True:
        return _flow_gap(quantity, quantity.get("quantity_reason") or quantity.get("reason") or "quantity_required")
    try:
        landed = transport({"method": "getTransaction", "signature": signature, "commitment": "confirmed"})
    except Exception:
        return _flow_gap(quantity, "flow_transport")
    body = landed.get("body") if isinstance(landed, dict) else None
    result = body.get("result") if isinstance(body, dict) else None
    bound = _evidence_body(signature, case_id, result)
    if bound is None:
        return _flow_gap(quantity, "flow_identity")
    decoded = bound["decoded"]
    meta = bound["meta"]
    import phantom_transport
    wallet = phantom_transport.REQUIRED_WALLET
    stored = db.query(
        "SELECT * FROM memecoin_landed_quantity WHERE signature = ? AND message_sha256 = ?",
        (signature, bound["digest"]),
    )
    if len(stored) != 1 or not _quantity_matches_body(stored[0], decoded, meta, bound["slot"]):
        return _flow_gap(quantity, "flow_identity")
    observed = stored[0]
    legs = _swap_legs(decoded)
    if legs is None or legs["user"] != wallet:
        return _flow_gap(quantity, "route_unbound")
    if observed["side"] == "entry":
        if legs["source_mint"] != solana_tx.NATIVE_MINT or legs["destination_mint"] != observed["mint"]:
            return _flow_gap(quantity, "route_unbound")
    elif legs["destination_mint"] != solana_tx.NATIVE_MINT or legs["source_mint"] != observed["mint"]:
        return _flow_gap(quantity, "route_unbound")
    found, reason = _attributed_transfers(meta, decoded, legs)
    if found is None:
        return _flow_gap(quantity, reason)
    input_text = _u64_canon(found["input"]["amount"])
    output_text = _u64_canon(found["output"]["amount"])
    if input_text is None or output_text is None:
        return _flow_gap(quantity, "inner_amount")
    if observed["side"] == "entry":
        if output_text != observed["quantity_raw"]:
            return _flow_gap(quantity, "quantity_mismatch")
        proceeds = None
        direction = "bought"
    else:
        if input_text != observed["quantity_raw"]:
            return _flow_gap(quantity, "quantity_mismatch")
        proceeds = output_text
        direction = "sold"
    if direction != observed["direction"]:
        return _flow_gap(quantity, "quantity_mismatch")
    prior = db.query("SELECT * FROM memecoin_landed_flow WHERE signature = ?", (signature,))
    if prior:
        row = prior[0]
        same = (
            row["message_sha256"] == bound["digest"] and row["case_id"] == case_id
            and row["input_raw"] == input_text and row["output_raw"] == output_text
            and row["proceeds_raw"] == proceeds and row["network_fee_lamports"] == bound["fee"]
            and row["landed_slot"] == bound["slot"] and row["vault_binding"] == _VAULT_BINDING
        )
        return _flow_gap(quantity, None if same else "flow_conflict") | {
            "flow_recorded": same, "flow_proven": same, "idempotent": same,
            "input_raw": input_text if same else None, "output_raw": output_text if same else None,
            "proceeds_raw": proceeds if same else None,
            "dex_fee_embedded_in_input": True,
            "vault_binding": _VAULT_BINDING if same else None,
            "vault_binding_source": _WHIRLPOOL_SWAP_SOURCE if same else None,
            "pool_bytes_fetched": False, "pool_pda_rederived": False,
        }
    fee = bound["fee"]

    def work(conn):
        conn.execute(
            """
            INSERT INTO memecoin_landed_flow (
                signature, message_sha256, case_id, side, direction, input_mint, output_mint,
                input_raw, output_raw, proceeds_raw, network_fee_lamports, landed_slot,
                vault_binding, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signature, bound["digest"], case_id, observed["side"], direction,
                legs["source_mint"], legs["destination_mint"], input_text, output_text, proceeds,
                fee, bound["slot"], _VAULT_BINDING, float(now),
            ),
        )

    try:
        db.exclusive(work)
    except sqlite3.IntegrityError as exc:
        if "landed_flow" not in str(exc):
            raise
        raced = db.query("SELECT * FROM memecoin_landed_flow WHERE signature = ?", (signature,))
        if raced and raced[0]["message_sha256"] == bound["digest"] and raced[0]["input_raw"] == input_text and raced[0]["output_raw"] == output_text and raced[0]["proceeds_raw"] == proceeds:
            return _flow_gap(quantity, None) | {
                "flow_recorded": True, "flow_proven": True, "idempotent": True,
                "input_raw": input_text, "output_raw": output_text, "proceeds_raw": proceeds,
                "network_fee_lamports": fee, "dex_fee_embedded_in_input": True,
                "vault_binding": _VAULT_BINDING, "vault_binding_source": _WHIRLPOOL_SWAP_SOURCE,
                "pool_bytes_fetched": False, "pool_pda_rederived": False,
            }
        return _flow_gap(quantity, "flow_conflict")
    return _flow_gap(quantity, None) | {
        "flow_recorded": True, "flow_proven": True, "idempotent": False, "flow_reason": None,
        "input_raw": input_text, "output_raw": output_text, "proceeds_raw": proceeds,
        "input_mint": legs["source_mint"], "output_mint": legs["destination_mint"],
        "network_fee_lamports": fee, "dex_fee_embedded_in_input": True,
        "vault_binding": _VAULT_BINDING, "vault_binding_source": _WHIRLPOOL_SWAP_SOURCE,
        "next_missing": "actual_combined_fee_proof",
    }


def _fee_gap(base: dict, reason: str) -> dict:
    """A missing or unbound Traded event is not a fee and not a cap."""
    return base | {
        "fee_recorded": False, "fee_proven": False, "fee_reason": reason,
        "fee_added_to_input": False, "fee_inside_input": True,
        "network_fee_added_to_dex_fee": False, "combined_fee_cap_proven": False,
        "one_shot_fee_used": False, "usd_proven": False,
        "pretrade_fee_bound_from_event": False, "dex_fee_lamports": None,
        "receipt_booked": False, "refund_proven": False, "fill_proven": False,
        "hold_released": False, "reservation_consumed": False,
        "execution_eligible": False, "holds": True, "resend": False,
    }


def _decode_traded(payload: str):
    """One exact Traded body. A short or long payload is not this event."""
    import base64
    import solana_tx
    try:
        raw = base64.b64decode(payload, validate=True)
    except Exception:
        return None, "event_malformed"
    if len(raw) != _TRADED_LEN or raw[:8] != _TRADED_DISC:
        if len(raw) >= 8 and raw[:8] == _TRADED_DISC:
            return None, "event_malformed"
        return None, None
    direction = raw[40]
    if direction not in (0, 1):
        return None, "event_malformed"
    def u64(start):
        return int.from_bytes(raw[start:start + 8], "little")
    def u128(start):
        return int.from_bytes(raw[start:start + 16], "little")
    return {
        "pool": solana_tx.b58encode(raw[8:40]),
        "a_to_b": direction == 1,
        "pre_sqrt_price": u128(41),
        "post_sqrt_price": u128(57),
        "input_amount": u64(73),
        "output_amount": u64(81),
        "input_transfer_fee": u64(89),
        "output_transfer_fee": u64(97),
        "lp_fee": u64(105),
        "protocol_fee": u64(113),
    }, None


def _traded_from_logs(logs, whirlpool_program: str):
    """Program data belongs to the program currently invoked.

    A Program log line is not that payload, even when its text says Program data.
    A Traded discriminator emitted by any other program is rejected.
    """
    import re
    if logs is None:
        return None, "event_log_missing"
    if not isinstance(logs, list) or not logs:
        return None, "event_log_missing"
    invoke = re.compile(r"^Program (\S+) invoke \[(\d+)\]$")
    success = re.compile(r"^Program (\S+) success$")
    failed = re.compile(r"^Program (\S+) failed\b")
    consumed = re.compile(r"^Program (\S+) consumed \d+ of \d+ compute units$")
    data = re.compile(r"^Program data: ([A-Za-z0-9+/=]+)$")
    stack = []
    found = []
    whirlpool_invokes = 0
    for line in logs:
        if not isinstance(line, str):
            return None, "event_log_untyped"
        if line == "Log truncated":
            return None, "event_log_truncated"
        if line.startswith("Program log:") or line.startswith("Program return:"):
            continue
        matched = data.match(line)
        if line.startswith("Program data:"):
            if matched is None or not stack:
                return None, "event_malformed" if stack else "event_log_unbalanced"
            parsed, reason = _decode_traded(matched.group(1))
            if reason is not None:
                return None, reason
            if stack[-1] != whirlpool_program:
                if parsed is not None:
                    return None, "event_foreign_program"
                continue
            if parsed is None:
                return None, "event_ambiguous"
            found.append(parsed)
            continue
        matched = invoke.match(line)
        if matched:
            depth = int(matched.group(2))
            if depth != len(stack) + 1:
                return None, "event_log_unbalanced"
            if matched.group(1) == whirlpool_program:
                if depth < 2:
                    return None, "event_invocation"
                whirlpool_invokes += 1
            stack.append(matched.group(1))
            continue
        matched = success.match(line)
        if matched:
            if not stack or stack[-1] != matched.group(1):
                return None, "event_log_unbalanced"
            stack.pop()
            continue
        if failed.match(line):
            return None, "event_log_failed"
        matched = consumed.match(line)
        if matched:
            if not stack or stack[-1] != matched.group(1):
                return None, "event_log_unbalanced"
            continue
        return None, "event_log_untyped"
    if stack:
        return None, "event_log_unbalanced"
    if whirlpool_invokes != 1:
        return None, "event_absent" if whirlpool_invokes == 0 else "event_ambiguous"
    if len(found) == 0:
        return None, "event_absent"
    if len(found) != 1:
        return None, "event_ambiguous"
    return found[0], None


def record_landed_swap_fee(case_id: str, signature: str, transport, now: float) -> dict:
    """The Traded fee from the same body as the canonical swap.

    lp_fee and protocol_fee are this swap's input-token fee. They are already
    inside the gross input. An entry's input is wSOL, so the unit is lamports.
    An exit's input is the token, so the unit stays token raw units and is not
    added to the network fee. No USD figure and no cap is derived.
    """
    import db
    import solana_tx
    import solana_whirlpool
    import sqlite3
    flow = record_landed_flow(case_id, signature, transport, now)
    if flow.get("flow_recorded") is not True:
        return _fee_gap(flow, flow.get("flow_reason") or flow.get("quantity_reason") or flow.get("reason") or "flow_required")
    try:
        landed = transport({"method": "getTransaction", "signature": signature, "commitment": "confirmed"})
    except Exception:
        return _fee_gap(flow, "fee_transport")
    body = landed.get("body") if isinstance(landed, dict) else None
    result = body.get("result") if isinstance(body, dict) else None
    bound = _evidence_body(signature, case_id, result)
    if bound is None:
        return _fee_gap(flow, "flow_identity")
    decoded = bound["decoded"]
    meta = bound["meta"]
    stored_qty = db.query(
        "SELECT * FROM memecoin_landed_quantity WHERE signature = ? AND message_sha256 = ?",
        (signature, bound["digest"]),
    )
    if len(stored_qty) != 1 or not _quantity_matches_body(stored_qty[0], decoded, meta, bound["slot"]):
        return _fee_gap(flow, "flow_identity")
    legs = _swap_legs(decoded)
    if legs is None:
        return _fee_gap(flow, "route_unbound")
    found, reason = _attributed_transfers(meta, decoded, legs)
    if found is None:
        return _fee_gap(flow, reason)
    input_text = _u64_canon(found["input"]["amount"])
    output_text = _u64_canon(found["output"]["amount"])
    recorded = db.query("SELECT * FROM memecoin_landed_flow WHERE signature = ?", (signature,))
    if len(recorded) != 1:
        return _fee_gap(flow, "flow_required")
    row = recorded[0]
    if row["message_sha256"] != bound["digest"] or row["case_id"] != case_id or row["input_raw"] != input_text or row["output_raw"] != output_text or row["network_fee_lamports"] != bound["fee"] or row["landed_slot"] != bound["slot"] or row["vault_binding"] != _VAULT_BINDING:
        return _fee_gap(flow, "flow_identity")
    event, event_reason = _traded_from_logs(meta.get("logMessages"), solana_whirlpool.PROGRAM_ID)
    if event is None:
        return _fee_gap(flow, event_reason)
    pool = found["swap"]["accounts"][2]
    if event["pool"] != pool or event["a_to_b"] is not found["swap"]["a_to_b"]:
        return _fee_gap(flow, "event_mismatch")
    if event["input_amount"] != found["input"]["amount"] or event["output_amount"] != found["output"]["amount"]:
        return _fee_gap(flow, "event_mismatch")
    if event["input_transfer_fee"] != 0 or event["output_transfer_fee"] != 0:
        return _fee_gap(flow, "event_transfer_fee")
    if event["lp_fee"] + event["protocol_fee"] > event["input_amount"]:
        return _fee_gap(flow, "event_fee_exceeds_input")
    unit = "lamports" if row["input_mint"] == solana_tx.NATIVE_MINT else "token_raw"
    if (row["side"] == "entry" and unit != "lamports") or (row["side"] == "exit" and unit != "token_raw"):
        return _fee_gap(flow, "event_unit")
    lp_text, protocol_text = _u64_canon(event["lp_fee"]), _u64_canon(event["protocol_fee"])
    pre_text, post_text = str(event["pre_sqrt_price"]), str(event["post_sqrt_price"])
    if lp_text is None or protocol_text is None:
        return _fee_gap(flow, "event_malformed")
    prior = db.query("SELECT * FROM memecoin_landed_swap_fee WHERE signature = ?", (signature,))
    same_fields = None
    if prior:
        seen = prior[0]
        same_fields = (
            seen["message_sha256"] == bound["digest"] and seen["lp_fee"] == lp_text
            and seen["protocol_fee"] == protocol_text and seen["fee_unit"] == unit
            and seen["pool"] == pool and seen["input_raw"] == input_text and seen["output_raw"] == output_text
            and seen["a_to_b"] == (1 if event["a_to_b"] else 0)
            and seen["pre_sqrt_price"] == pre_text and seen["post_sqrt_price"] == post_text
            and seen["network_fee_lamports"] == bound["fee"]
        )
        if same_fields:
            _safe_landed_fee_log(case_id, signature, transport, now)
        return _fee_gap(flow, None if same_fields else "fee_conflict") | {
            "fee_recorded": bool(same_fields), "fee_proven": bool(same_fields), "idempotent": bool(same_fields),
            "lp_fee": lp_text if same_fields else None, "protocol_fee": protocol_text if same_fields else None,
            "fee_unit": unit if same_fields else None, "network_fee_lamports": bound["fee"],
        }

    def work(conn):
        conn.execute(
            """
            INSERT INTO memecoin_landed_swap_fee (
                signature, message_sha256, case_id, side, pool, a_to_b, input_raw, output_raw,
                lp_fee, protocol_fee, fee_unit, network_fee_lamports, pre_sqrt_price, post_sqrt_price, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signature, bound["digest"], case_id, row["side"], pool, 1 if event["a_to_b"] else 0,
                input_text, output_text, lp_text, protocol_text, unit, bound["fee"], pre_text, post_text, float(now),
            ),
        )

    try:
        db.exclusive(work)
    except sqlite3.IntegrityError as exc:
        if "landed_swap_fee" not in str(exc):
            raise
        raced = db.query("SELECT * FROM memecoin_landed_swap_fee WHERE signature = ?", (signature,))
        if raced and raced[0]["lp_fee"] == lp_text and raced[0]["protocol_fee"] == protocol_text and raced[0]["fee_unit"] == unit:
            _safe_landed_fee_log(case_id, signature, transport, now)
            return _fee_gap(flow, None) | {
                "fee_recorded": True, "fee_proven": True, "idempotent": True,
                "lp_fee": lp_text, "protocol_fee": protocol_text, "fee_unit": unit,
                "network_fee_lamports": bound["fee"],
            }
        return _fee_gap(flow, "fee_conflict")
    _safe_landed_fee_log(case_id, signature, transport, now)
    return _fee_gap(flow, None) | {
        "fee_recorded": True, "fee_proven": True, "idempotent": False, "fee_reason": None,
        "lp_fee": lp_text, "protocol_fee": protocol_text, "fee_unit": unit,
        "input_raw": input_text, "output_raw": output_text, "pool": pool,
        "network_fee_lamports": bound["fee"],
        "event_source": _WHIRLPOOL_SWAP_SOURCE + " programs/whirlpool/src/events.rs Traded",
        "next_missing": "landed_fee_valuation",
    }


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


def _confirmed(request: dict) -> dict:
    """Confirmed read. minContextSlot is the server tip, not a historical account slot."""
    config = {"commitment": "confirmed"}
    slot = request.get("minContextSlot")
    if isinstance(slot, int) and not isinstance(slot, bool) and slot > 0:
        config["minContextSlot"] = slot
    return config


def _open_no_redirect(req):
    """One POST. A redirect is returned, not followed, so the bearer stays on the first host."""
    import urllib.error
    import urllib.request
    opener = urllib.request.build_opener(stalkchain_adapter._NoRedirect)
    try:
        with opener.open(req, timeout=8) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        try:
            payload = exc.read()
        except Exception:
            payload = b""
        return exc.code, payload


def _paper_dry_run() -> bool:
    """PAPER plus the explicit dry-run flag. LIVE never uses this path."""
    import os
    if os.environ.get("DESK_SIM_DRY_RUN") != "1":
        return False
    try:
        import db
        return db.get_setting("mode", "PAPER") != "LIVE"
    except Exception:
        return False


def _paper_dry_simulate(request: dict) -> dict:
    """simulateTransaction only. The body is built here so it cannot be a send."""
    if not _paper_dry_run():
        raise RuntimeError("method_refused")
    transaction = request.get("transaction")
    if not isinstance(transaction, str) or transaction.strip() == "" or len(transaction) > 20000:
        raise RuntimeError("method_refused")
    if request.get("sigVerify") is True:
        raise RuntimeError("method_refused")
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "simulateTransaction",
        "params": [transaction, {
            "encoding": "base64",
            "sigVerify": False,
            "replaceRecentBlockhash": True,
            "commitment": "processed",
        }],
    }
    import solana_rpc
    try:
        return solana_rpc.post(payload)
    except solana_rpc.RpcRefused as exc:
        note_provider("simulateTransaction", exc.reason if isinstance(exc.reason, str) else "rpc_refused")
        raise RuntimeError(exc.reason) from None


def public_reader(request: dict) -> dict:
    """Documented public reads. StalkChain is one POST to a known tool, not an open URL.

    PAPER with DESK_SIM_DRY_RUN=1 may simulate. Sending stays refused.
    """
    import json
    import os
    import urllib.request
    if not isinstance(request, dict):
        raise RuntimeError("host_refused")
    if request.get("method") in {"sendTransaction", "signTransaction"}:
        raise RuntimeError("method_refused")
    if request.get("method") == "simulateTransaction":
        return _paper_dry_simulate(request)
    if request.get("method") == "getFeeForMessage" or request.get("purpose") in {"whirlpool_pool", "whirlpool_tick_array", "token_deposit", "rent_exemption", "mint_decimals", "token_account"}:
        raise RuntimeError("method_refused")
    url = request.get("url")
    if isinstance(url, str) and url.startswith("https://data.stalkchain.com"):
        if request.get("method") != "POST":
            raise RuntimeError("method_refused")
        prefix = stalkchain_adapter.URL
        if not url.startswith(prefix) or url[len(prefix):] not in stalkchain_adapter.TOOLS:
            raise RuntimeError("tool_refused")
        body = request.get("json")
        token = body.get("token") if isinstance(body, dict) else None
        if not isinstance(body, dict) or set(body) != {"token"} or not stalkchain_adapter.canonical_mint(token):
            raise RuntimeError("body_refused")
        secret = os.environ.get(stalkchain_adapter.ENV, "").strip()
        if secret == "":
            raise RuntimeError("stalkchain_credentials_missing")
        payload = json.dumps({"token": token}, separators=(",", ":")).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "authorization": "Bearer " + secret,
            },
            method="POST",
        )
        status, raw = _open_no_redirect(req)
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else None
        except Exception:
            parsed = None
        return {"status": status, "body": parsed}
    method = request.get("method")
    if request.get("kind") == "rpc" or method in {"getAccountInfo", "getMultipleAccounts", "getMinimumBalanceForRentExemption", "getSlot", "getTransaction", "getSignatureStatuses", "getGenesisHash", "getLatestBlockhash", "getBlockHeight", "isBlockhashValid"}:
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
        elif method == "getLatestBlockhash":
            params = [_confirmed(request)]
        elif method == "getBlockHeight":
            params = [_confirmed(request)]
        elif method == "isBlockhashValid":
            blockhash = request.get("blockhash")
            if not isinstance(blockhash, str) or blockhash == "":
                raise RuntimeError("method_refused")
            params = [blockhash, _confirmed(request)]
        else:
            raise RuntimeError("method_refused")
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        import solana_rpc
        try:
            posted = solana_rpc.post(payload)
        except solana_rpc.RpcRefused as exc:
            note_provider("helius", exc.reason if isinstance(exc.reason, str) else "rpc_refused")
            raise RuntimeError(exc.reason) from None
        return posted
    else:
        rugcheck_summary = isinstance(url, str) and url.startswith("https://api.rugcheck.xyz/v1/tokens/") and url.endswith("/report/summary")
        if not isinstance(url, str) or not (url.startswith("https://api.dexscreener.com/") or url.startswith("https://api.jup.ag/swap/v2/") or rugcheck_summary):
            raise RuntimeError("host_refused")
        if url.startswith("https://api.jup.ag/") and ("/execute" in url or "/submit" in url):
            raise RuntimeError("host_refused")
        req = urllib.request.Request(url, headers={"accept": "application/json"})
    import live_gate_proof
    status, raw, _proved = live_gate_proof.exchange(req, 8)
    parsed = json.loads(raw.decode())
    if isinstance(url, str) and status != 200:
        if url.startswith("https://api.jup.ag/"):
            note_provider("jupiter", "http_status")
        elif url.startswith("https://api.rugcheck.xyz/"):
            note_provider("rugcheck", "http_status")
    return {"status": status, "body": parsed}


def _readiness_blocks(pyth: dict) -> list[str]:
    import owner_slippage
    blocks = []
    if owner_slippage.ceiling_bps() != owner_slippage.BPS:
        blocks.append("owner_slippage_unapproved")
    blocks.append(pyth["reason"] or "pyth_sponsored_not_fresh")
    blocks.append("hermes_not_called")
    if not _process_has_env("HELIUS_API_KEY"):
        blocks.append("helius_api_key_missing")
    if not _process_has_env("SOLANA_TRACKER_API_KEY"):
        blocks.append("solana_tracker_api_key_missing")
    return blocks


def _remaining_live_gates_body(mint, shut, quote_fn, preflight_fn, submit_fn) -> list[dict]:
    """What still sits between this branch and a genuine live send. None of these are armed.

    Thành removed stalkchain_rest_envelope on 2026-11-11 at 22:23 Asia/Saigon.
    LP, rug history, holder growth, bundles, and dev holding stay on the
    RugCheck, Helius, and Solana Tracker gates below. A proof is for one mint.
    """
    return [
        {
            "gate": "lp_lock_live",
            "proves": "RugCheck lpBurn is 100, or lpLockedPct is 100, or both are present and sum to exactly 100",
            "owner": False,
            "needs_live_read": True,
            "closed": shut("lp_lock_live", mint),
        },
        {
            "gate": "rug_history_live",
            "proves": "Helius getAssetsByCreator plus a fetched RugCheck report rugged boolean for every past mint",
            "owner": True,
            "owner_action": "set HELIUS_API_KEY",
            "needs_live_read": True,
            "closed": shut("rug_history_live", mint),
        },
        {
            "gate": "holder_growth_live",
            "proves": "a fetched Solana Tracker holder chart with two increasing points",
            "owner": True,
            "owner_action": "set SOLANA_TRACKER_API_KEY",
            "needs_live_read": True,
            "closed": shut("holder_growth_live", mint),
        },
        {
            "gate": "bundle_unique_buyers_smart_wallets_dev_holding",
            "proves": "Solana Tracker totalInitialPercentage at most 10, dev holding at most 3 percent, unique buyers and smart wallets scored 0",
            "owner": False,
            "needs_live_read": True,
            "closed": shut("bundle_unique_buyers_smart_wallets_dev_holding", mint),
        },
        {
            "gate": "price_freshness_proven",
            "proves": "a Hermes SOL/USD observation inside the 30 second window for this mint",
            "owner": False,
            "needs_live_read": True,
            "closed": shut("price_freshness_proven", mint),
        },
        {
            "gate": "quote_expiry",
            "proves": "a Jupiter quote this process read over TLS, newer than the max age (default 10s), re-checked against 50 bps before signing",
            "owner": False,
            "needs_live_read": True,
            "closed": quote_fn(mint),
        },
        {
            "gate": "preflight_ready",
            "proves": "simulateTransaction of the exact unsigned message on Helius, with no error, funds, fee, or slippage miss",
            "owner": False,
            "needs_live_read": True,
            "closed": preflight_fn(mint),
        },
        {
            "gate": "ticket_limit_proven",
            "proves": "every signer path enforces this reservation, including deposits, at most $8",
            "owner": False,
            "needs_live_read": False,
            "closed": True,
        },
        {
            "gate": "allocation_proven",
            "proves": "every signer path enforces other holds plus this reservation at most $100",
            "owner": False,
            "needs_live_read": False,
            "closed": True,
        },
        {
            "gate": "phantom_server_auth",
            "proves": "a fresh allowlisted identity from /api/phantom/login/check for wallet 7AFJbqsX2UeK7ZW5M1gbXWjiKCai2CJWHpCzXe7woC7t",
            "owner": True,
            "owner_action": "open the approval URL, enter the user code, and approve before it expires",
            "needs_live_read": False,
            "closed": _phantom_server_auth_closed(),
        },
        {
            "gate": "real_submit",
            "proves": "this process read a confirmed Helius fill for the signed message, with one owner and mint row",
            "owner": True,
            "owner_action": "approve the Phantom session before any real signature",
            "needs_live_read": True,
            "closed": submit_fn(mint),
        },
        {
            "gate": "deploy_env",
            "proves": "this process has HELIUS_API_KEY and SOLANA_TRACKER_API_KEY",
            "owner": True,
            "owner_action": "set both variables on the deploy",
            "needs_live_read": False,
            "closed": _process_has_env("HELIUS_API_KEY") and _process_has_env("SOLANA_TRACKER_API_KEY"),
        },
    ]


def _bind_remaining_live_gates():
    """Capture the proof objects once. Later lookups of those names are checked, not trusted."""
    import live_gate_proof as proof
    check = proof.attributes_intact
    read = proof.closed
    quote_read = proof.quote_ready
    preflight_read = proof.preflight_observed
    submit_read = proof.real_submit_observed
    mod = proof

    def remaining_live_gates(mint: str | None = None) -> list[dict]:
        intact = (
            mod.closed is read
            and mod.quote_ready is quote_read
            and mod.preflight_observed is preflight_read
            and mod.real_submit_observed is submit_read
            and mod.attributes_intact is check
            and check()
        )

        def shut(name, mint_arg):
            if not intact:
                return False
            return read(name, mint_arg)

        def quote(mint_arg):
            if not intact:
                return False
            return quote_read(mint_arg)

        def preflight(mint_arg):
            if not intact:
                return False
            return preflight_read(mint_arg)

        def submit(mint_arg):
            if not intact:
                return False
            return submit_read(mint_arg)

        return _remaining_live_gates_body(mint, shut, quote, preflight, submit)

    return remaining_live_gates


remaining_live_gates = _bind_remaining_live_gates()


def readiness() -> dict:
    """Presence and status for this process. Not a Render inventory and not a secret."""
    pyth = pyth_sponsored.status()
    catalog = usdc_valuation.keyless_catalog_is_not_a_price()
    return {
        "coded": True,
        "deployed": False,
        "this_process_only": True,
        "server_loop": _runtime["server_loop"] is True,
        "run_once_scheduled": False,
        "send": False,
        "sign": False,
        "live_ready": False,
        "stalkchain_env_present": stalkchain_adapter.this_process_has_key(),
        "helius_env_present": _process_has_env("HELIUS_API_KEY"),
        "solana_tracker_env_present": _process_has_env("SOLANA_TRACKER_API_KEY"),
        "remaining_live_gates": remaining_live_gates(),
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
        "removed_live_gates": [
            {
                "gate": "stalkchain_rest_envelope",
                "owner": "Thành",
                "decided_at": "2026-11-11 22:23 Asia/Saigon",
                "decision": "removed from the required LIVE gates",
                "covered_by": "RugCheck, Helius, and Solana Tracker live gates only",
                "facts": ["lp_lock", "rug_history", "holder_growth", "bundles", "dev_holding"],
            }
        ],
        "slippage_policy": _slippage_policy(),
        "source_fields": stalkchain_adapter.field_requirements(),
        "observation_files_required": list(stalkchain_adapter.OBSERVATION_FILES),
        "analyst_formula": memecoin_desk.analyst_decision(),
        "fixture_is_mainnet_proof": False,
        "providers": _provider_status(),
        "execution_eligible": False,
        "preflight_ready": False,
        "gate_complete": False,
    }


def _live_gate(name: str, mint: str | None) -> bool:
    import live_gate_proof
    return live_gate_proof.closed(name, mint)


def _phantom_server_auth_closed() -> bool:
    import phantom_auth
    return phantom_auth.server_auth_closed()


def _provider_status() -> dict:
    """Which readers this process can call. Booleans only. Never a key."""
    import desk_providers
    return desk_providers.configured()


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


