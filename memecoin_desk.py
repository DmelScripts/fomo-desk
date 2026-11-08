"""Six-stage memecoin desk. A caller cannot mark a stage passed.

Scout reads a DexScreener-shaped snapshot and does not treat it as mint, freeze,
LP, developer, or bundle proof. Filter facts need their own source. Analyst
weights are engineering defaults, not approved risk numbers. The approved risk
numbers are the 6% stop, the 12% trail arm, the 12% drop from the peak, and
liquidity of at least 25% of market cap.

The trail does not lock a profit. A peak that only just arms, times a 12% drop,
is 0.9856 of entry. Entry is not sent. A fixture route is not a live provider.
DexScreener and the on-chain readers are not called from here. The older
3-hour exit was not restated for this desk, so it is not applied. The paper
desk still uses its own exit tuple.
"""

from __future__ import annotations

import hashlib
import json
import time
from decimal import Decimal, InvalidOperation

import db
import phantom_transport
import raydium_cpmm
import solana_tx

ALLOCATION = Decimal("100.00")
TICKET = Decimal("8.00")
MAX_FEE = Decimal("0.05")
MAX_LOSS = Decimal("20.00")
STOP = Decimal("6")
TRAIL_ARM = Decimal("12")
TRAIL_DROP = Decimal("12")
LIQ_OF_MCAP = Decimal("0.25")
DEV_HOLDING_MAX = Decimal("3")
BUNDLE_MAX = Decimal("10")
ALREADY_UP_1H = Decimal("80")
# Engineering defaults. They are not the approved stop, trail, fee, or loss numbers.
WEIGHTS = {
    "momentum": Decimal("20"),
    "unique_buyers": Decimal("20"),
    "buy_sell_ratio": Decimal("15"),
    "holder_growth": Decimal("15"),
    "liq_mcap": Decimal("15"),
    "smart_wallets": Decimal("15"),
}
COMPONENTS = tuple(WEIGHTS)
FRESH_S = 30


def analyst_decision() -> dict:
    """The score floor is coded. The component weights are not an owner rule."""
    return {
        "score_floor": 70,
        "weights": {key: format(value, "f") for key, value in WEIGHTS.items()},
        "weights_are": "engineering_default",
        "owner_approved": False,
        "approved_numbers": {
            "stop_pct": format(STOP, "f"),
            "trail_arm_pct": format(TRAIL_ARM, "f"),
            "trail_drop_pct": format(TRAIL_DROP, "f"),
            "liq_of_mcap": format(LIQ_OF_MCAP, "f"),
            "ticket_usd": format(TICKET, "f"),
            "max_fee_usd": format(MAX_FEE, "f"),
            "allocation_usd": format(ALLOCATION, "f"),
            "max_loss_usd": format(MAX_LOSS, "f"),
            "slippage_bps": 50,
        },
        "decision_needed": "Component weights are engineering defaults. A score of 70 from those weights is not an owner-approved formula.",
        "required_inputs": [
            {
                "name": name,
                "weight": format(WEIGHTS[name], "f"),
                "value_range": "0..100",
                "max_age_s": FRESH_S,
                "rejected_sources": ["dexscreener", "dexscreener_txns", "transaction_count", "dexscreener_boost", "profile"],
            }
            for name in COMPONENTS
        ],
    }
MIN_AGE_S = 30 * 60
MAX_AGE_S = 72 * 3600
MIN_MCAP = Decimal("300000")
MAX_MCAP = Decimal("20000000")


def scout(pair: dict, now: float | None = None) -> dict:
    """Age and market cap from one DexScreener snapshot. FDV is not market cap."""
    moment = time.time() if now is None else now
    _ensure()
    if not isinstance(pair, dict):
        return _kill(None, None, "scout", "pair_missing", None)
    mint = pair.get("mint")
    if not isinstance(mint, str) or len(mint) < 32:
        return _kill(None, mint if isinstance(mint, str) else None, "scout", "mint", "dexscreener")
    if pair.get("source") != "dexscreener":
        return _kill(None, mint, "scout", "source", pair.get("source") if isinstance(pair.get("source"), str) else None)
    if pair.get("market_cap_basis") not in (None, "market_cap"):
        return _kill(None, mint, "scout", "market_cap_basis", "dexscreener")
    fetched = _num(pair.get("fetched_at"))
    if fetched is None or abs(Decimal(str(moment)) - fetched) > FRESH_S:
        return _kill(None, mint, "scout", "snapshot_stale", "dexscreener")
    created_ms = pair.get("pair_created_at")
    if isinstance(created_ms, bool) or not isinstance(created_ms, int) or created_ms < 10**12:
        return _kill(None, mint, "scout", "pair_created_at", "dexscreener")
    age = fetched - (Decimal(created_ms) / Decimal(1000))
    if age < MIN_AGE_S or age > MAX_AGE_S:
        return _kill(None, mint, "scout", "age", "dexscreener")
    if pair.get("market_cap_usd") is None:
        return _kill(None, mint, "scout", "market_cap_missing", "dexscreener")
    mcap = _money(pair.get("market_cap_usd"))
    if mcap is None or mcap < MIN_MCAP or mcap > MAX_MCAP:
        return _kill(None, mint, "scout", "market_cap", "dexscreener")
    if pair.get("liquidity_usd") is None:
        return _kill(None, mint, "scout", "liquidity_missing", "dexscreener")
    liq = _money(pair.get("liquidity_usd"))
    if liq is None:
        return _kill(None, mint, "scout", "liquidity", "dexscreener")
    price = _money(pair.get("price_usd"))
    change = _num(pair.get("price_change_h1"))
    if price is None or change is None:
        return _kill(None, mint, "scout", "price", "dexscreener")
    snapshot = {
        "source": "dexscreener",
        "fetched_at": str(fetched),
        "pair_created_at": created_ms,
        "market_cap_usd": format(mcap, "f"),
        "market_cap_basis": "market_cap",
        "fdv_used_as_market_cap": False,
        "liquidity_usd": format(liq, "f"),
        "price_usd": format(price, "f"),
        "price_change_h1": str(change),
    }

    def work(conn):
        _ensure_conn(conn)
        open_row = conn.execute(
            "SELECT id, stage FROM memecoin_cases WHERE mint = ? AND stage NOT IN ('rejected', 'exited')",
            (mint,),
        ).fetchone()
        if open_row is not None:
            return {"ok": False, "reason": "already_open", "case_id": open_row["id"], "stage": open_row["stage"], "mint": mint}
        case_id = mint
        suffix = 0
        while conn.execute("SELECT id FROM memecoin_cases WHERE id = ?", (case_id,)).fetchone() is not None:
            suffix += 1
            case_id = mint + ":" + str(suffix)
        conn.execute(
            "INSERT INTO memecoin_cases (id, mint, stage, snapshot_json, created_at, updated_at) VALUES (?, ?, 'scout_passed', ?, ?, ?)",
            (case_id, mint, json.dumps(snapshot, sort_keys=True), moment, moment),
        )
        _log(conn, case_id, mint, "scout", "pass", None, "dexscreener", snapshot, moment)
        return {"ok": True, "reason": None, "case_id": case_id, "stage": "scout_passed", "mint": mint}

    return db.exclusive(work)


def filter_case(case_id: str, onchain: dict, now: float | None = None) -> dict:
    """On-chain facts. DexScreener cannot satisfy them. Liquidity uses the scout snapshot."""
    moment = time.time() if now is None else now
    row = _case(case_id)
    if row is None or row["stage"] != "scout_passed":
        return _kill(case_id, None, "filter", "stage", None)
    snap = _snapshot(row)
    source = onchain.get("source") if isinstance(onchain, dict) and isinstance(onchain.get("source"), str) else None
    if not isinstance(onchain, dict) or source in (None, "dexscreener"):
        return _fail(row, "filter", "onchain_source", source=source)
    def refuse(reason: str) -> dict:
        return _fail(row, "filter", reason, source=source)

    fetched = _num(onchain.get("fetched_at"))
    if fetched is None or abs(Decimal(str(moment)) - fetched) > FRESH_S:
        return refuse("onchain_stale")
    if onchain.get("mint_authority_revoked") is not True:
        return refuse("mint_authority")
    if onchain.get("freeze_authority_revoked") is not True:
        return refuse("freeze_authority")
    if onchain.get("lp_burned_or_locked") is not True:
        return refuse("lp")
    dev = _num(onchain.get("dev_holding_pct"))
    if dev is None:
        return refuse("dev_holding_missing")
    if dev > DEV_HOLDING_MAX:
        return refuse("dev_holding")
    rugs = onchain.get("dev_rugs")
    if not isinstance(rugs, dict) or rugs.get("status") != "checked" or not isinstance(rugs.get("source"), str) or rugs.get("source") == "dexscreener":
        return refuse("dev_rug_history_unproven")
    if isinstance(rugs.get("rugs"), bool) or not isinstance(rugs.get("rugs"), int) or isinstance(rugs.get("tokens_seen"), bool) or not isinstance(rugs.get("tokens_seen"), int):
        return refuse("dev_rug_history_unproven")
    if rugs["rugs"] < 0 or rugs["tokens_seen"] < 0:
        return refuse("dev_rug_history_unproven")
    if rugs["rugs"] > 0:
        return refuse("dev_rug_history")
    bundled = _num(onchain.get("bundled_launch_pct"))
    if bundled is None:
        return refuse("bundle_missing")
    if bundled > BUNDLE_MAX:
        return refuse("bundle")
    change = _num(snap.get("price_change_h1"))
    if change is None or change >= ALREADY_UP_1H:
        return refuse("already_up_1h")
    mcap = _money(snap.get("market_cap_usd"))
    liq = _money(snap.get("liquidity_usd"))
    if mcap is None or liq is None or mcap <= 0:
        return refuse("liquidity_ratio")
    if liq / mcap < LIQ_OF_MCAP:
        return refuse("liquidity_below_25pct_mcap")
    evidence = {"source": onchain.get("source"), "liq_of_mcap": format(liq / mcap, "f"), "market_cap_basis": "market_cap"}
    return _advance(row, "filter_passed", "filter", evidence)


def analyze(case_id: str, components: dict, now: float | None = None) -> dict:
    """Score only measured components. A missing one is a reject, not a zero."""
    moment = time.time() if now is None else now
    row = _case(case_id)
    if row is None or row["stage"] != "filter_passed":
        return _kill(case_id, None, "analyst", "stage", None)
    if not isinstance(components, dict):
        return _fail(row, "analyst", "components_missing")
    weighted = Decimal("0")
    evidence = {"weights": {key: format(value, "f") for key, value in WEIGHTS.items()}, "weights_are": "engineering_default"}
    for name in COMPONENTS:
        item = components.get(name)
        if not isinstance(item, dict):
            return _fail(row, "analyst", name + "_missing")
        source = item.get("source")
        logged = source if isinstance(source, str) else None
        if logged is None or logged in {"dexscreener", "dexscreener_txns", "transaction_count", "dexscreener_boost", "profile"}:
            return _fail(row, "analyst", name + "_unproven", source=logged)
        fetched = _num(item.get("fetched_at"))
        if fetched is None or abs(Decimal(str(moment)) - fetched) > FRESH_S:
            return _fail(row, "analyst", name + "_stale", source=logged)
        value = _num(item.get("value"))
        if value is None or value < 0 or value > 100:
            return _fail(row, "analyst", name, source=logged)
        weighted += WEIGHTS[name] * value
        evidence[name] = {"value": str(value), "source": source}
    score = weighted / Decimal("100")
    evidence["score"] = format(score, "f")
    if score < 70:
        return _fail(row, "analyst", "score", evidence)
    snap = _snapshot(row)
    snap["score"] = format(score, "f")
    snap["analyst"] = evidence
    return _advance(row, "analyst_passed", "analyst", evidence, snap)


def set_entry_amount(case_id: str, amount: int) -> dict:
    """A caller amount cannot replace the size protect() already bound."""
    row = _case(case_id)
    if row is None or row["stage"] != "protected":
        return {"ok": False, "reason": "stage", "case_id": case_id, "send": False, "live_ready": False}
    snap = _snapshot(row)
    stored = snap.get("entry_amount_raw")
    if isinstance(stored, int) and not isinstance(stored, bool) and stored > 0:
        return {
            "ok": False, "reason": "amount_locked", "case_id": case_id, "amount": stored,
            "send": False, "live_ready": False,
        }
    return {"ok": False, "reason": "server_amount_required", "case_id": case_id, "send": False, "live_ready": False}


def _entry_lamports(sol_leg: dict, rent_lamports: int | None = None) -> dict:
    """Largest raw input whose rent, input, and fee ceiling stay inside $8.

    $8 is the maximum. The search stops at the largest integer that still fits.
    It does not invent a price. Rent must be the proved lamport sum, not a guess.
    """
    import pyth_sponsored
    refused = {"ok": False, "reason": "sol_usd_unpriced"}
    if rent_lamports is None:
        rent = solana_tx.TOKEN_ACCOUNT_RENT
        rent_proved = False
    elif isinstance(rent_lamports, bool) or not isinstance(rent_lamports, int) or rent_lamports < 0:
        return refused | {"reason": "rent_unpriced"}
    else:
        rent = rent_lamports
        rent_proved = True
    room = TICKET - MAX_FEE
    if room <= 0:
        return refused | {"reason": "ticket"}
    if pyth_sponsored.lamports_usd(1, sol_leg) is None:
        return refused
    lo, hi = rent + 1, 2_000_000_000
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        priced = pyth_sponsored.lamports_usd(mid, sol_leg)
        if priced is not None and priced <= room:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if best is None:
        return refused | {"reason": "rent_exceeds_ticket"}
    raw = best - rent
    if raw < 1:
        return refused | {"reason": "rent_exceeds_ticket"}
    exposure = pyth_sponsored.lamports_usd(raw + rent, sol_leg)
    if exposure is None or exposure + MAX_FEE > TICKET:
        return refused | {"reason": "ticket"}
    return {
        "ok": True,
        "lamports": raw,
        "rent_lamports": rent,
        "rent_proved": rent_proved,
        "exposure_usd": format(exposure, "f"),
        "fee_usd": format(MAX_FEE, "f"),
        "total_usd": format(exposure + MAX_FEE, "f"),
    }


def _balance_class(item) -> str:
    """Paper and synthetic are not live. A live-looking row without server provenance is unresolved."""
    if item["ledger"] in {"paper", "synthetic"}:
        return "skip"
    if item["ledger"] != "live" or item["ledger_provenance"] != "server":
        return "ambiguous"
    return "live"


def receipt_remaining(case_id: str) -> dict:
    """Size a live exit from server-proven live receipts.

    A paper or synthetic row is not live quantity, so it does not close a
    proven live position and it does not add to it. A live-looking row that
    is not server-proven can be an unaccounted buy or sale. It is not ignored
    and it is not counted. Sizing stays closed until a new server-proven
    receipt accounts for that row. The lineage trigger rejects editing the
    old row into server provenance. The hold is unchanged either way.
    """
    refused = {"ok": False, "reason": "exit_receipt_unproven", "remaining": None}

    def work(conn):
        _ensure_conn(conn)
        bought_rows = conn.execute(
            "SELECT signature, qty, ledger, ledger_provenance FROM memecoin_entry_receipts WHERE case_id = ? AND failed = 0",
            (case_id,),
        ).fetchall()
        sold_rows = conn.execute(
            "SELECT signature, qty, ledger, ledger_provenance FROM memecoin_exit_receipts WHERE case_id = ?",
            (case_id,),
        ).fetchall()
        if not bought_rows:
            return refused
        bought = 0
        seen = set()
        live_buys = 0
        for item in bought_rows:
            if item["signature"] in seen:
                continue
            seen.add(item["signature"])
            kind = _balance_class(item)
            if kind == "ambiguous":
                return refused | {"reason": "provenance_unproven"}
            if kind == "skip":
                continue
            live_buys += 1
            qty = item["qty"]
            if isinstance(qty, bool) or not isinstance(qty, int) or qty < 0:
                return refused
            bought += qty
        if live_buys == 0:
            return refused
        sold = 0
        exited = set()
        for item in sold_rows:
            if item["signature"] in exited:
                continue
            exited.add(item["signature"])
            kind = _balance_class(item)
            if kind == "ambiguous":
                return refused | {"reason": "provenance_unproven"}
            if kind == "skip":
                continue
            qty = item["qty"]
            if isinstance(qty, bool) or not isinstance(qty, int) or qty < 0:
                return refused | {"reason": "exit_qty_unproven"}
            sold += qty
        left = bought - sold
        if left < 0:
            return refused | {"reason": "exit_qty_exceeds_entry", "remaining": None}
        return {"ok": True, "reason": None, "remaining": left, "bought": bought, "sold": sold}

    return db.exclusive(work)


def refresh_entry_intent(case_id: str, sized: dict, now: float) -> dict:
    """Replace a quotable size that no longer fits. An inflight message is not edited."""
    row = _case(case_id)
    refused = {"ok": False, "send": False, "sign": False, "live_ready": False, "case_id": case_id}
    if row is None or row["stage"] != "protected":
        return refused | {"reason": "stage"}
    if sized.get("ok") is not True:
        return refused | {"reason": sized.get("reason") or "sol_usd_unpriced", "jupiter_called": False}

    def work(conn):
        current = conn.execute("SELECT stage, snapshot_json FROM memecoin_cases WHERE id = ?", (row["id"],)).fetchone()
        if current is None or current["stage"] != "protected":
            return refused | {"reason": "stage"}
        snap = json.loads(current["snapshot_json"])
        if isinstance(snap.get("entry_inflight"), dict):
            return refused | {"reason": "intent_inflight", "amount": snap.get("entry_amount_raw")}
        previous = snap.get("entry_amount_raw")
        version = snap.get("entry_intent_version")
        version = version if isinstance(version, int) and not isinstance(version, bool) and version > 0 else 0
        fresh = sized["lamports"]
        if previous == fresh and snap.get("entry_rent_lamports") == sized["rent_lamports"] and version > 0:
            return {"ok": True, "amount": fresh, "version": version, "rebuilt": False, "case_id": row["id"], "send": False, "live_ready": False}
        held = _held_conn(conn)
        old_reserved = _reserved_amount(snap.get("reserved_usd")) or Decimal("0")
        new_reserved = _money(sized["total_usd"]) or TICKET
        if held - old_reserved + new_reserved > ALLOCATION:
            return refused | {"reason": "allocation"}
        snap["entry_amount_raw"] = fresh
        snap["entry_intent_version"] = version + 1
        snap["entry_amount_previous"] = previous
        snap["entry_amount_source"] = "server_ticket"
        snap["entry_amount_locked"] = True
        snap["entry_rent_lamports"] = sized["rent_lamports"]
        snap["entry_rent_proved"] = sized.get("rent_proved") is True
        snap["entry_exposure_usd"] = sized["exposure_usd"]
        snap["entry_fee_usd"] = sized["fee_usd"]
        snap["entry_total_usd"] = sized["total_usd"]
        snap["reserved_usd"] = sized["total_usd"]
        snap["entry_intent_rebuilt_at"] = now
        conn.execute(
            "UPDATE memecoin_cases SET snapshot_json = ?, updated_at = ? WHERE id = ? AND stage = 'protected'",
            (json.dumps(snap, sort_keys=True), now, row["id"]),
        )
        return {"ok": True, "amount": fresh, "version": version + 1, "rebuilt": previous != fresh, "previous": previous, "case_id": row["id"], "send": False, "live_ready": False}

    return db.exclusive(work)


def protect(case_id: str, canonical_pool: bytes | None = None, quote: dict | None = None, now: float | None = None, rent_proof: dict | None = None, ledger: str | None = None) -> dict:
    """Persist the stop and the 12/12 trail before an entry can become eligible."""
    row = _case(case_id)
    if row is None or row["stage"] != "analyst_passed":
        return _kill(case_id, None, "executor", "stage", None)
    snap = _snapshot(row)
    price = _money(snap.get("price_usd"))
    if price is None:
        return _fail(row, "executor", "price")
    stop_px = price * (Decimal("100") - STOP) / Decimal("100")
    arm_px = price * (Decimal("100") + TRAIL_ARM) / Decimal("100")
    floor_px = arm_px * (Decimal("100") - TRAIL_DROP) / Decimal("100")
    snap.update({
        "stop_pct": format(STOP, "f"),
        "trail_arm_pct": format(TRAIL_ARM, "f"),
        "trail_drop_pct": format(TRAIL_DROP, "f"),
        "stop_price_usd": format(stop_px, "f"),
        "trail_arm_price_usd": format(arm_px, "f"),
        "trail_floor_if_armed_at_threshold": format(floor_px, "f"),
        "trail_breakeven_guaranteed": False,
        "entry_price_usd": format(price, "f"),
        "ticket_usd": format(TICKET, "f"),
        "notional_usd": format(_notional(canonical_pool, quote), "f") if _notional(canonical_pool, quote) is not None else None,
        "deposit_usd": format(_deposit(canonical_pool, quote), "f") if _deposit(canonical_pool, quote) is not None else None,
        "reserved_usd": format(TICKET, "f") if _deposit(canonical_pool, quote) is not None else None,
        "reserved_includes_deposit": _deposit(canonical_pool, quote) is not None,
        "fee_cap_usd": format(MAX_FEE, "f"),
        "live_ready": False,
        "send": False,
    })
    evidence = {
        "stop_price_usd": snap["stop_price_usd"],
        "trail_arm_price_usd": snap["trail_arm_price_usd"],
        "trail_floor_if_armed_at_threshold": snap["trail_floor_if_armed_at_threshold"],
        "trail_breakeven_guaranteed": False,
        "deposit_usd": snap.get("deposit_usd"),
        "reserved_includes_deposit": snap.get("reserved_includes_deposit"),
    }
    moment = time.time() if now is None else now
    import pyth_sponsored
    if isinstance(rent_proof, dict) and rent_proof.get("ok") is True and isinstance(rent_proof.get("lamports"), int) and rent_proof["lamports"] > 0:
        deposit = pyth_sponsored.lamports_usd(rent_proof["lamports"], pyth_sponsored.sol_leg(moment))
        if deposit is not None and deposit < TICKET:
            snap["deposit_usd"] = format(deposit, "f")
            snap["reserved_includes_deposit"] = True
            snap["rent_account_count"] = rent_proof.get("count")
            snap["rent_proof"] = "getMinimumBalanceForRentExemption"
            snap["rent_slot"] = rent_proof.get("slot")
            snap["rent_tip_slot"] = rent_proof.get("tip_slot")
            snap["rent_genesis"] = rent_proof.get("genesis")
            snap["mint_program"] = rent_proof.get("mint_program")
    if snap.get("reserved_includes_deposit") is not True:
        return {"ok": False, "reason": "deposit_unpriced", "case_id": row["id"], "stage": row["stage"], "mint": row["mint"], "send": False, "live_ready": False}
    rent_for_size = None
    if isinstance(rent_proof, dict) and rent_proof.get("ok") is True and isinstance(rent_proof.get("lamports"), int):
        rent_for_size = rent_proof["lamports"]
    sized = _entry_lamports(pyth_sponsored.sol_leg(moment), rent_for_size)
    snap["sources_are_live_mcp"] = False
    snap["readiness_not_implied"] = True
    snap["wallet"] = phantom_transport.REQUIRED_WALLET
    snap["allocation_usd"] = format(ALLOCATION, "f")
    snap["policy_bps"] = 50
    snap["entry_amount_is_a_fill"] = False
    snap["send"] = False
    snap["live_ready"] = False
    stored_ledger = ledger if ledger in {"synthetic", "paper"} else "live"
    snap["capital_ledger"] = stored_ledger
    snap["live_capital"] = stored_ledger == "live"
    if sized.get("ok") is True:
        snap["entry_amount_raw"] = sized["lamports"]
        snap["entry_intent_version"] = 1
        snap["entry_amount_source"] = "server_ticket"
        snap["entry_amount_locked"] = True
        snap["entry_rent_proved"] = sized.get("rent_proved") is True
        snap["entry_rent_lamports"] = sized["rent_lamports"]
        snap["entry_exposure_usd"] = sized["exposure_usd"]
        snap["entry_fee_usd"] = sized["fee_usd"]
        snap["entry_total_usd"] = sized["total_usd"]
        total = _money(sized["total_usd"])
        if total is not None and total <= TICKET:
            snap["reserved_usd"] = sized["total_usd"]
    else:
        snap["entry_intent_reason"] = sized.get("reason")
    evidence["entry_amount_raw"] = snap.get("entry_amount_raw")
    evidence["entry_intent_reason"] = snap.get("entry_intent_reason")
    evidence["sources_are_live_mcp"] = False

    def work(conn):
        current = conn.execute("SELECT stage FROM memecoin_cases WHERE id = ?", (row["id"],)).fetchone()
        if current is None or current["stage"] != "analyst_passed":
            return {"ok": False, "reason": "stage", "case_id": row["id"], "stage": None if current is None else current["stage"]}
        if _halted_conn(conn):
            _log(conn, row["id"], row["mint"], "executor", "kill", "kill_switch", None, {"reason": "kill_switch"}, moment)
            return {"ok": False, "reason": "kill_switch", "case_id": row["id"], "stage": "analyst_passed", "mint": row["mint"]}
        if conn.execute("SELECT value FROM settings WHERE key = 'memecoin_entry_halt'").fetchone() is not None and conn.execute("SELECT value FROM settings WHERE key = 'memecoin_entry_halt'").fetchone()["value"] == "1":
            conn.execute("UPDATE memecoin_cases SET stage = 'rejected', updated_at = ? WHERE id = ?", (moment, row["id"]))
            _log(conn, row["id"], row["mint"], "executor", "kill", "entry_halt", None, {"reason": "entry_halt"}, moment)
            return {"ok": False, "reason": "entry_halt", "case_id": row["id"], "stage": "rejected", "mint": row["mint"]}
        if _loss_conn(conn) >= MAX_LOSS:
            conn.execute("UPDATE memecoin_cases SET stage = 'rejected', updated_at = ? WHERE id = ?", (moment, row["id"]))
            _log(conn, row["id"], row["mint"], "executor", "kill", "stop_new_entries", None, {"reason": "stop_new_entries"}, moment)
            return {"ok": False, "reason": "stop_new_entries", "case_id": row["id"], "stage": "rejected", "mint": row["mint"]}
        if _held_conn(conn) + TICKET > ALLOCATION:
            conn.execute("UPDATE memecoin_cases SET stage = 'rejected', updated_at = ? WHERE id = ?", (moment, row["id"]))
            _log(conn, row["id"], row["mint"], "executor", "kill", "allocation", None, {"reason": "allocation"}, moment)
            return {"ok": False, "reason": "allocation", "case_id": row["id"], "stage": "rejected", "mint": row["mint"]}
        conn.execute(
            "UPDATE memecoin_cases SET stage = 'protected', snapshot_json = ?, ledger = ?, ledger_provenance = 'server', updated_at = ? WHERE id = ? AND stage = 'analyst_passed'",
            (json.dumps(snap, sort_keys=True), stored_ledger, moment, row["id"]),
        )
        _log(conn, row["id"], row["mint"], "executor", "pass", None, None, evidence, moment)
        return {"ok": True, "reason": None, "case_id": row["id"], "stage": "protected", "mint": row["mint"]}

    return db.exclusive(work)


def offer_fixture_route(case_id: str, tx_b64: str, *, amount_in: int, minimum_out: int, fee_usd: str) -> dict:
    """A test route. It is not a live pool and it does not send."""
    row = _case(case_id)
    if row is None or row["stage"] != "protected":
        return _kill(case_id, None, "executor", "stage", None)
    snap = _snapshot(row)
    if "stop_price_usd" not in snap or "trail_drop_pct" not in snap:
        return _fail(row, "executor", "policy_not_persisted")
    fee = _money(fee_usd)
    if fee is None:
        return _fail(row, "executor", "fee_unpriced")
    if fee > MAX_FEE:
        return _fail(row, "executor", "fee_cap")
    checked = raydium_cpmm.inspect_buy(
        tx_b64, payer=phantom_transport.REQUIRED_WALLET, output_mint=row["mint"], amount_in=amount_in, minimum_out=minimum_out,
    )
    if checked.get("ok") is not True:
        return _fail(row, "executor", checked.get("reason") or "buy_route", {"decoder": checked.get("decoder"), "fixture": True})
    snap["route"] = {
        "fixture": True,
        "live_provider": False,
        "message_sha256": hashlib.sha256(solana_tx.message_bytes(tx_b64)).hexdigest(),
        "amount_in": amount_in,
        "minimum_out": minimum_out,
        "fee_usd": format(fee, "f"),
        "decoder": checked["decoder"],
    }
    snap["send"] = False
    snap["live_ready"] = False
    return _advance(row, "entry_ready", "executor", snap["route"], snap)


def harness_fill(case_id: str, receipt: dict) -> dict:
    """Book one fixture fill. Only a case created as paper can take this path."""
    row = _case(case_id)
    if row is None:
        return _kill(case_id, None, "executor", "stage", None)
    if execution_mode(row) != "paper":
        return _note(row, "executor", "paper_not_live")
    if row["stage"] != "entry_ready":
        return _kill(case_id, None, "executor", "stage", None)
    snap = _snapshot(row)
    route = snap.get("route") if isinstance(snap.get("route"), dict) else {}
    if route.get("fixture") is not True or route.get("live_provider") is True:
        return _fail(row, "executor", "route_not_fixture")
    if not isinstance(receipt, dict) or receipt.get("message_sha256") != route.get("message_sha256"):
        return _fail(row, "executor", "message_unbound")
    tokens = receipt.get("tokens_out")
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < int(route["minimum_out"]):
        return _fail(row, "executor", "fill_below_min")
    fee = _money(route.get("fee_usd"))
    snap["fill"] = {
        "fixture": True,
        "live_proof": False,
        "tokens_out": tokens,
        "cost_usd": snap["ticket_usd"],
        "fee_usd": format(fee, "f"),
        "fee_counted_once": True,
        "fee_usd_is_live": False,
        "qty_is_live": False,
    }
    snap["entry_funding"] = {
        "proven": False,
        "source": "paper_harness",
        "live_proof": False,
        "fee_usd_is_live": False,
        "qty_is_live": False,
        "accounts": None,
    }
    snap["remaining_raw"] = tokens
    snap["live_ledger"] = False
    snap["peak_price_usd"] = snap["entry_price_usd"]
    snap["trail_armed"] = False
    return _advance(row, "filled", "executor", snap["fill"], snap)


def reserve_entry(case_id: str, message_b64: str, snapshot, price_transport=None, now: float | None = None) -> dict:
    """Reserve one reviewed buy message. This does not send, and a test slippage number is not owner consent."""
    import base64
    import jupiter_route_v2
    import memecoin_exit
    import pyth_sponsored
    import solana_jupiter
    row = _case(case_id)
    if row is None or row["stage"] != "protected":
        return _kill(case_id, None, "executor", "stage", None)
    foreign = _foreign_ledger(row, "executor")
    if foreign is not None:
        return foreign
    snap = _snapshot(row)
    if "stop_price_usd" not in snap or "trail_drop_pct" not in snap or snap.get("reserved_includes_deposit") is not True:
        return _note(row, "executor", "policy_not_persisted")
    if not isinstance(snapshot, jupiter_route_v2.Snapshot):
        return _note(row, "executor", "snapshot_unbound")
    moment = time.time() if now is None else now
    prices = pyth_sponsored.public_fetch(moment) if price_transport is None else pyth_sponsored.read_accounts(price_transport, moment)
    sol = prices.get("sol") if isinstance(prices, dict) else None
    if not isinstance(sol, dict) or prices.get("sol_used") is not True or sol.get("fresh") is not True:
        return _note(row, "executor", prices.get("reason") if isinstance(prices, dict) and prices.get("reason") else "usd_source_unbound")
    try:
        digest = hashlib.sha256(solana_tx.message_bytes(message_b64)).hexdigest()
        decoded = solana_tx.decode_transaction(base64.b64decode(message_b64, validate=True))
    except Exception:
        return _note(row, "executor", "message")
    if decoded.static_keys[0] != phantom_transport.REQUIRED_WALLET or decoded.required_signatures != 1:
        return _note(row, "executor", "wallet")
    route = memecoin_exit._route(decoded)
    if route is None or route["destination_mint"] != row["mint"] or route["source_mint"] != solana_tx.NATIVE_MINT:
        return _note(row, "executor", "entry_mint")
    pool_key, tick_keys = _hop_keys(route)
    if pool_key is None:
        return _note(row, "executor", "pool_unbound")
    record = snapshot.accounts.get(pool_key)
    if not isinstance(record, tuple) or record[0] != __import__("solana_whirlpool").PROGRAM_ID:
        return _note(row, "executor", "pool_unbound")
    state = __import__("solana_whirlpool").pool_state(record[1])
    fields = __import__("solana_whirlpool").pool_fields(record[1])
    if state is None or fields is None or {fields[0], fields[2]} != {solana_tx.NATIVE_MINT, row["mint"]}:
        return _note(row, "executor", "pool_unbound")
    swap_data = None
    for instruction in decoded.instructions:
        if decoded.static_keys[instruction.program_index] == solana_jupiter.PROGRAM_ID:
            swap_data = bytes(instruction.data)
    parsed = jupiter_route_v2.parse(swap_data or b"")
    tick_bytes = []
    for key in tick_keys:
        tick = snapshot.accounts.get(key)
        if not isinstance(tick, tuple):
            return _note(row, "executor", "quote_unbound")
        tick_bytes.append(tick[1])
    quoted = jupiter_route_v2._amm_bound(parsed, state, parsed.get("a_to_b") is True, tick_bytes, pool_key)
    if quoted.get("ok") is not True:
        return _note(row, "executor", quoted.get("reason") or "quote_unbound")
    import owner_slippage
    slip = owner_slippage.check_route(parsed.get("slippage_bps"), parsed.get("min_out"), quoted.get("est_out"))
    if slip.get("ok") is not True:
        return _note(row, "executor", slip.get("reason") or "slippage_exceeds")
    chain = getattr(snapshot, "server_chain", None)
    if isinstance(chain, dict):
        if chain.get("bind_ok") is not True or decoded.blockhash != chain.get("blockhash"):
            reason = "blockhash_mismatch" if decoded.blockhash != chain.get("blockhash") else (chain.get("reason") or "block_height_missing")

            def refuse(conn):
                _ensure_conn(conn)
                current = conn.execute("SELECT snapshot_json FROM memecoin_cases WHERE id = ?", (row["id"],)).fetchone()
                fresh = json.loads(current["snapshot_json"]) if current is not None else {}
                existing = fresh.get("entry_inflight") if isinstance(fresh.get("entry_inflight"), dict) else None
                if isinstance(existing, dict) and existing.get("message_sha256") != digest:
                    return {"ok": False, "reason": "entry_inflight", "case_id": row["id"], "holds": True, "resend": False, "rewritten": False, "send": False, "live_ready": False}
                return {"ok": False, "reason": reason, "case_id": row["id"], "holds": True, "resend": False, "rewritten": False, "message_sha256": None if existing is None else existing.get("message_sha256"), "send": False, "live_ready": False, "quote_expiry_proven": False}

            return db.exclusive(refuse)
        clock = {
            "quote_clock_proven": False,
            "quote_expiry_proven": False,
            "inclusion_window_proven": chain.get("inclusion_window_proven") is True,
            "reason": None,
        }
    else:
        clock = memecoin_exit.quote_clock(None, None, None)
    dex = jupiter_route_v2.whirlpool_fee(route["in_amount"], state["fee_rate"])
    compute = [bytes(item.data) for item in decoded.instructions if decoded.static_keys[item.program_index] == solana_tx.COMPUTE_BUDGET]
    network = jupiter_route_v2.network_fee(compute, decoded.required_signatures)
    if dex is None or network.get("ok") is not True:
        return _note(row, "executor", "exit_dimension" if network.get("ok") is True else network.get("reason"))
    fee_usd = pyth_sponsored.lamports_usd(dex + network["network_lamports"], sol)
    exposure = pyth_sponsored.lamports_usd(route["in_amount"] + solana_tx.TOKEN_ACCOUNT_RENT, sol)
    if fee_usd is None or exposure is None:
        return _note(row, "executor", "usd_source_unbound")
    if fee_usd > MAX_FEE:
        return _note(row, "executor", "fee_cap")
    if exposure > TICKET:
        return _note(row, "executor", "exposure_cap")
    destination = route["keys"][2]
    policy = {
        "allocation_usd": format(ALLOCATION, "f"),
        "ticket_usd": format(TICKET, "f"),
        "max_fee_usd": format(MAX_FEE, "f"),
        "max_loss_usd": format(MAX_LOSS, "f"),
        "stop_pct": format(STOP, "f"),
        "trail_arm_pct": format(TRAIL_ARM, "f"),
        "trail_drop_pct": format(TRAIL_DROP, "f"),
        "source": "owner_widget_21:37",
        "max_slippage_bps": owner_slippage.BPS,
        "owner_slippage_approved": owner_slippage.ceiling_bps() == owner_slippage.BPS,
        "test_consent_is_owner_consent": False,
    }
    blocked = _effectual_blocks()
    reservation = {
        "message_sha256": digest,
        "input_lamports": route["in_amount"],
        "min_out": route["min_out"],
        "instruction_slippage_bps": parsed.get("slippage_bps"),
        "quoted_out_amount": parsed.get("quoted_out_amount"),
        "owner_min_out": slip.get("allowance_floor"),
        "mint": row["mint"],
        "wallet": phantom_transport.REQUIRED_WALLET,
        "pool": pool_key,
        "destination_account": destination,
        "destination_index": decoded.static_keys.index(destination),
        "account_keys": list(decoded.static_keys),
        "dex_fee_lamports": dex,
        "dex_fee_counted_in_input": True,
        "network_lamports": network["network_lamports"],
        "network_fee_added_once": True,
        "reserved_fee_usd": format(fee_usd, "f"),
        "exposure_usd": format(exposure, "f"),
        "deposit_lamports_assumed": solana_tx.TOKEN_ACCOUNT_RENT,
        "mint_ata_prebalance_at_reserve": "unknown",
        "sol_price": sol.get("price"),
        "sol_conf": sol.get("conf"),
        "sol_expo": sol.get("expo"),
        "snapshot_slot": snapshot.slot,
        "snapshot_tip_slot": snapshot.tip_slot,
        "snapshot_fetched_at": snapshot.fetched_at,
        "blockhash": decoded.blockhash,
        "last_valid_block_height": None if not isinstance(chain, dict) else chain.get("last_valid_block_height"),
        "last_valid_provenance": None if not isinstance(chain, dict) else chain.get("last_valid_provenance"),
        "router": None if not isinstance(chain, dict) else chain.get("router"),
        "block_height": None if not isinstance(chain, dict) else chain.get("block_height"),
        "expire_at": None if not isinstance(chain, dict) else chain.get("expire_at"),
        "inclusion_window_proven": isinstance(chain, dict) and chain.get("inclusion_window_proven") is True,
        "price_freshness_proven": False,
        "quote_observed_at": None if not isinstance(chain, dict) else chain.get("observed_at"),
        "quote_expiry_source": None if not isinstance(chain, dict) else chain.get("quote_expiry_source"),
        "provider_quote_expiry_published": isinstance(chain, dict) and chain.get("provider_quote_expiry_published") is True,
        "internal_time_limit": None,
        "dex_fee_source": "reserved_pool_state",
        "dex_fee_remeasured": False,
        "quote_expiry_rule": "getBlockHeight_le_lastValidBlockHeight" if isinstance(chain, dict) else "slot_distance_guard_not_block_height",
        "slot_distance_is_blockhash_proof": False,
        "quote_clock_proven": clock["quote_clock_proven"],
        "quote_expiry_proven": clock["quote_expiry_proven"],
        "quote_clock_reason": clock["reason"],
        "oracle_fresh": True,
        "amm_est_out": quoted.get("est_out"),
        "policy": policy,
        "cluster": "solana:mainnet",
        "status": "reserved",
        "signature": None,
        "effectual_entry": False,
        "effectual_blocked_by": blocked,
    }

    def work(conn):
        _ensure_conn(conn)
        current = conn.execute("SELECT stage, snapshot_json FROM memecoin_cases WHERE id = ?", (row["id"],)).fetchone()
        if current is None or current["stage"] != "protected":
            return {"ok": False, "reason": "stage", "case_id": row["id"], "send": False, "live_ready": False}
        fresh = json.loads(current["snapshot_json"])
        priors = conn.execute("SELECT * FROM memecoin_reservations WHERE case_id = ? AND side = 'entry'", (row["id"],)).fetchall()
        for stored in priors:
            if stored["message_sha256"] == digest:
                if _same_reservation(stored, case_id=row["id"], side="entry", digest=digest, chain=chain, pool=pool_key):
                    return _reservation_view(row["id"], reservation, idempotent=True)
                return {"ok": False, "reason": "reservation_conflict", "case_id": row["id"], "holds": True, "resend": False, "rewritten": False, "send": False, "live_ready": False}
            if _reservation_status(conn, stored) in {"active", "unknown"}:
                return {"ok": False, "reason": "entry_inflight", "case_id": row["id"], "holds": True, "resend": False, "rewritten": False, "send": False, "live_ready": False}
        existing = fresh.get("entry_inflight")
        if isinstance(existing, dict):
            if existing.get("message_sha256") == digest:
                return _reservation_view(row["id"], existing, idempotent=True)
            return {"ok": False, "reason": "entry_inflight", "case_id": row["id"], "holds": True, "resend": False, "send": False, "live_ready": False}
        if fresh.get("entry_unreconciled") is True:
            return {"ok": False, "reason": "entry_unreconciled", "case_id": row["id"], "holds": True, "resend": False, "send": False, "live_ready": False}
        if _loss_conn(conn) >= MAX_LOSS:
            return {"ok": False, "reason": "stop_new_entries", "case_id": row["id"], "send": False, "live_ready": False}
        incoming = _money(reservation.get("exposure_usd")) or TICKET
        fee_part = _money(reservation.get("reserved_fee_usd")) or Decimal("0")
        if not _room_for(conn, row["id"], incoming + fee_part):
            return {"ok": False, "reason": "allocation", "case_id": row["id"], "send": False, "live_ready": False}
        fresh["entry_inflight"] = reservation
        _store_reservation(conn, case_id=row["id"], side="entry", message_b64=message_b64, digest=digest, chain=chain, pool=pool_key, intent_id=None, ledger=_ledger_of(row), moment=moment)
        conn.execute("UPDATE memecoin_cases SET snapshot_json = ?, updated_at = ? WHERE id = ?", (json.dumps(fresh, sort_keys=True), moment, row["id"]))
        _log(conn, row["id"], row["mint"], "executor", "pass", None, "entry_reserve", {"message_sha256": digest, "effectual_entry": False}, moment)
        return _reservation_view(row["id"], reservation, idempotent=False)

    return db.exclusive(work)


def confirm_entry(case_id: str, signature: str, now: float | None = None) -> dict:
    """Read getTransaction and book the reserved buy. A caller fill document cannot do this."""
    import desk_loop
    import memecoin_exit
    import pyth_sponsored
    row = _case(case_id)
    if row is None:
        return _kill(case_id, None, "executor", "stage", None)
    snap = _snapshot(row)
    if _ledger_of(row) != "live":
        if snap.get("live_ledger") is True or snap.get("capital_ledger") == "live":
            return _quarantine(row, "ledger_lineage")
        return _foreign_ledger(row, "executor")
    duplicate = _duplicate_entry(signature)
    if duplicate is not None:
        replay = _replay_ledger(signature)
        if replay is not None:
            return replay
        return duplicate
    if row["stage"] != "protected":
        return _kill(case_id, None, "executor", "stage", None)
    inflight = snap.get("entry_inflight")
    if not isinstance(inflight, dict):
        return _hold(row, "entry_not_reserved", status="held")
    if inflight.get("status") == "unknown" and inflight.get("signature") not in (None, signature):
        return _hold(row, "no_resend", status="unknown")
    pulled = desk_loop.read_transaction(signature)
    moment = time.time() if now is None else now
    if pulled.get("reason") == "receipt_failed":
        return _book_failed_entry(row, inflight, signature, pulled, moment)
    if pulled.get("ok") is not True:
        snap["entry_inflight"]["status"] = "unknown"
        snap["entry_inflight"]["signature"] = signature

        def hold(conn):
            _ensure_conn(conn)
            _note_unknown_signature(conn, row["id"], inflight.get("message_sha256"), signature, moment)
            conn.execute(
                "UPDATE memecoin_cases SET snapshot_json = ?, updated_at = ? WHERE id = ? AND stage = 'protected'",
                (json.dumps(snap, sort_keys=True), moment, row["id"]),
            )
            return _hold_conn(conn, row, pulled.get("reason") or "receipt_unknown", status="unknown", moment=moment)

        return db.exclusive(hold)
    flight = dict(inflight)
    if pulled.get("block_time") is not None:
        flight["block_time"] = pulled.get("block_time")
    proven = memecoin_exit.match_entry(flight, signature, pulled)
    if proven.get("ok") is not True:
        if proven.get("reason") in {"unexplained_lamports", "cost_unsplit", "rent_unproven"}:
            return _book_unreconciled_entry(row, inflight, signature, pulled, proven, moment)
        return _hold(row, proven.get("reason") or "receipt_unbound", status="held", quote_clock_proven=False, quote_expiry_proven=False)
    leg = {"used": True, "fresh": True, "price": inflight.get("sol_price"), "conf": inflight.get("sol_conf"), "expo": inflight.get("sol_expo")}
    fee_usd = pyth_sponsored.lamports_usd(proven["dex_fee_lamports"] + proven["network_fee_lamports"], leg)
    cost_usd = pyth_sponsored.lamports_usd(proven["cost_basis_lamports"], leg)
    breach = fee_usd is None or fee_usd > MAX_FEE or proven["network_fee_lamports"] > int(inflight.get("network_lamports") or 0)

    def work(conn):
        _ensure_conn(conn)
        current = conn.execute("SELECT stage, snapshot_json FROM memecoin_cases WHERE id = ?", (row["id"],)).fetchone()
        if current is None or current["stage"] != "protected":
            return {"ok": False, "reason": "stage", "holds": True, "recorded": False, "resend": False, "send": False, "live_ready": False}
        fresh = json.loads(current["snapshot_json"])
        stored = fresh.get("entry_inflight") or {}
        if stored.get("message_sha256") != proven["message_sha256"]:
            return _hold_conn(conn, row, "message_mismatch", status="held", moment=moment, stage_now="protected")
        seen = conn.execute("SELECT signature FROM memecoin_entry_receipts WHERE signature = ? OR (case_id = ? AND message_sha256 = ?)", (signature, row["id"], proven["message_sha256"])).fetchone()
        if seen is not None:
            return _duplicate_entry_conn(conn, signature, row["id"])
        fresh.pop("entry_inflight", None)
        fresh["remaining_raw"] = proven["qty"]
        fresh["deposits"] = proven["rent"]
        fresh["entry_funding"] = {
            "proven": True,
            "live_proof": True,
            "source": "entry_receipt",
            "signature": signature,
            "fee_usd_is_live": True,
            "qty_is_live": True,
            "qty_source": "entry_receipt",
            "tokens_out": proven["qty"],
            "token_pre_source": proven["token_pre_source"],
            "input_lamports": proven["input_lamports"],
            "cost_basis_lamports": proven["cost_basis_lamports"],
            "cost_basis_lamports_remaining": proven["cost_basis_lamports"],
            "cost_basis_usd": None if cost_usd is None else format(cost_usd, "f"),
            "network_fee_lamports": proven["network_fee_lamports"],
            "dex_fee_lamports": proven["dex_fee_lamports"],
            "dex_fee_counted_in_input": True,
            "network_fee_added_once": True,
            "deposit_lamports": proven["deposit_lamports"],
            "fee_usd": None if fee_usd is None else format(fee_usd, "f"),
            "sol_leg": leg,
            "pnl_uses_case_price": False,
        }
        fresh["entry_funding_proven"] = True
        fresh["live_ledger"] = True
        fresh["ledger"] = "live"
        decimals = proven.get("mint_decimals")
        if isinstance(decimals, int) and not isinstance(decimals, bool) and proven["qty"] > 0 and cost_usd is not None:
            tokens = Decimal(proven["qty"]) / (Decimal(10) ** decimals)
            px = cost_usd / tokens
            fresh["discovery_price_usd"] = fresh.get("entry_price_usd")
            fresh["entry_price_usd"] = format(px, "f")
            fresh["entry_price_source"] = "entry_receipt"
            fresh["fill_basis_proven"] = True
            fresh["basis_decimals"] = decimals
            fresh["basis_qty"] = proven["qty"]
            fresh["basis_cost_lamports"] = proven["cost_basis_lamports"]
            fresh["basis_fee_treatment"] = "dex_inside_input_network_once_deposit_excluded"
            fresh["stop_price_usd"] = format(px * (Decimal("100") - STOP) / Decimal("100"), "f")
            fresh["trail_arm_price_usd"] = format(px * (Decimal("100") + TRAIL_ARM) / Decimal("100"), "f")
            fresh["trail_floor_if_armed_at_threshold"] = format(px * (Decimal("100") + TRAIL_ARM) / Decimal("100") * (Decimal("100") - TRAIL_DROP) / Decimal("100"), "f")
            fresh["peak_price_usd"] = fresh["entry_price_usd"]
            fresh["trail_armed"] = False
        else:
            fresh["fill_basis_proven"] = False
            fresh["entry_price_source"] = "discovery_not_a_fill"
        fresh["pnl_live"] = False
        fresh.pop("realized_pnl", None)
        fresh["quote_clock_proven"] = proven["quote_clock_proven"]
        fresh["quote_expiry_proven"] = proven["quote_expiry_proven"]
        _write_deposits(conn, row["id"], fresh["deposits"])
        if breach:
            _halt_entries(conn, moment)
        conn.execute(
            "INSERT INTO memecoin_entry_receipts (signature, case_id, message_sha256, slot, qty, cost_basis_lamports, network_fee_lamports, dex_fee_lamports, fee_usd, failed, ledger, pool, ledger_provenance) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 'server')",
            (signature, row["id"], proven["message_sha256"], proven["slot"], proven["qty"], proven["cost_basis_lamports"], proven["network_fee_lamports"], proven["dex_fee_lamports"], "unpriced" if fee_usd is None else format(fee_usd, "f"), _ledger_of(row), inflight.get("pool") if isinstance(inflight.get("pool"), str) else None),
        )
        conn.execute("UPDATE memecoin_cases SET stage = 'filled', snapshot_json = ?, updated_at = ? WHERE id = ?", (json.dumps(fresh, sort_keys=True), moment, row["id"]))
        _log(conn, row["id"], row["mint"], "executor", "pass" if not breach else "kill", None if not breach else "fee_policy_breach", "entry_receipt", {"signature": signature, "qty": proven["qty"]}, moment)
        return {
            "ok": not breach,
            "recorded": True,
            "reason": "fee_policy_breach" if breach else None,
            "case_id": row["id"],
            "stage": "filled",
            "status": "breach" if breach else "booked",
            "qty": proven["qty"],
            "qty_source": "entry_receipt",
            "remaining_raw": proven["qty"],
            "token_pre_source": proven["token_pre_source"],
            "cost_basis_lamports": proven["cost_basis_lamports"],
            "cost_basis_usd": fresh["entry_funding"]["cost_basis_usd"],
            "input_lamports": proven["input_lamports"],
            "network_fee_lamports": proven["network_fee_lamports"],
            "dex_fee_lamports": proven["dex_fee_lamports"],
            "dex_fee_counted_in_input": True,
            "network_fee_added_once": True,
            "deposit_lamports": proven["deposit_lamports"],
            "fee_usd": fresh["entry_funding"]["fee_usd"],
            "proceeds": None,
            "quote_clock_proven": False,
            "quote_expiry_proven": False,
            "quote_clock_reason": proven["quote_clock_reason"],
            "slot_distance_is_blockhash_proof": False,
            "fee_policy_breach": breach,
            "entry_halt": breach,
            "entry_funding_proven": True,
            "effectual_entry": False,
            "owner_slippage_approved": __import__("owner_slippage").ceiling_bps() == 50,
            "holds": True,
            "send": False,
            "sign": False,
            "live_ready": False,
            "resend": False,
        }

    return db.exclusive(work)


def record_exit_receipt(case_id: str, body: dict, price_transport=None, now: float | None = None) -> dict:
    """A caller document is not a receipt."""
    row = _case(case_id)
    if row is None:
        return _kill(case_id, None, "risk", "stage", None)
    return _note(row, "risk", "receipt_unbound")


def reserve_exit(case_id: str, message_b64: str, qty: int, snapshot, price_transport=None, now: float | None = None) -> dict:
    """Claim one reduce-only slice of the remaining tokens. A second claim waits."""
    import jupiter_route_v2
    import memecoin_exit
    import pyth_sponsored
    row = _case(case_id)
    if row is None or row["stage"] not in {"filled", "partial", "exit_intent"}:
        return _kill(case_id, None, "risk", "stage", None)
    mode = execution_mode(row)
    if mode == "unknown":
        return _quarantine(row, "ledger_unknown")
    if mode == "synthetic":
        return _note(row, "risk", "synthetic_not_live")
    paper = mode == "paper"
    if mode == "live" and (_live_entry_receipt(row["id"]) is None or _matching_intent(row["id"]) is None):
        return _note(row, "risk", "exit_unproven")
    if mode not in {"live", "paper"}:
        return _note(row, "risk", "exit_unproven")
    if not isinstance(snapshot, jupiter_route_v2.Snapshot):
        return _note(row, "risk", "snapshot_unbound")
    if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
        return _note(row, "risk", "quantity")
    moment = time.time() if now is None else now
    try:
        digest = hashlib.sha256(solana_tx.message_bytes(message_b64)).hexdigest()
        decoded = solana_tx.decode_transaction(__import__("base64").b64decode(message_b64, validate=True))
    except Exception:
        return _note(row, "risk", "message")
    route = memecoin_exit._route(decoded)
    if route is None or route["source_mint"] != row["mint"] or route["in_amount"] != qty:
        return _note(row, "risk", "exit_mint")
    if not paper:
        intent_now = _matching_intent(row["id"])
        cursor_pool = 7
        if route["keys"][cursor_pool] != jupiter_route_v2.EVENT:
            cursor_pool += 1
        if intent_now is None or route["keys"][cursor_pool + 5] != intent_now["pool"]:
            return _note(row, "risk", "pool_mismatch")
    prices = pyth_sponsored.public_fetch(moment) if price_transport is None else pyth_sponsored.read_accounts(price_transport, moment)
    sol = prices.get("sol") if isinstance(prices, dict) else None
    if not isinstance(sol, dict) or prices.get("sol_used") is not True or sol.get("fresh") is not True:
        return _note(row, "risk", prices.get("reason") if isinstance(prices, dict) and prices.get("reason") else "usd_source_unbound")
    cursor = 7
    if route["keys"][cursor] != jupiter_route_v2.EVENT:
        cursor += 1
    pool_key = route["keys"][cursor + 5]
    record = snapshot.accounts.get(pool_key)
    if not isinstance(record, tuple) or record[0] != __import__("solana_whirlpool").PROGRAM_ID:
        return _note(row, "risk", "pool_unbound")
    state = __import__("solana_whirlpool").pool_state(record[1])
    fields = __import__("solana_whirlpool").pool_fields(record[1])
    if state is None or fields is None or row["mint"] not in {fields[0], fields[2]}:
        return _note(row, "risk", "pool_unbound")
    token_is_a = fields[0] == row["mint"]
    dex_raw = jupiter_route_v2.whirlpool_fee(qty, state["fee_rate"])
    dex_lamports = None if dex_raw is None else jupiter_route_v2.token_raw_to_lamports(dex_raw, state["sqrt_price"], token_is_a)
    notional = jupiter_route_v2.token_raw_to_lamports(qty, state["sqrt_price"], token_is_a)
    compute = [bytes(item.data) for item in decoded.instructions if decoded.static_keys[item.program_index] == solana_tx.COMPUTE_BUDGET]
    network = jupiter_route_v2.network_fee(compute, decoded.required_signatures)
    if dex_lamports is None or notional is None or network.get("ok") is not True:
        return _note(row, "risk", "exit_dimension" if network.get("ok") is True else network.get("reason"))
    fee_usd = pyth_sponsored.lamports_usd(dex_lamports + network["network_lamports"], sol)
    exposure = pyth_sponsored.lamports_usd(notional, sol)
    if fee_usd is None or exposure is None:
        return _note(row, "risk", "usd_source_unbound")
    if fee_usd > MAX_FEE:
        return _note(row, "risk", "fee_cap")
    if exposure > TICKET:
        return _note(row, "risk", "exposure_cap")
    import owner_slippage
    import solana_jupiter
    pool_for_quote, tick_keys = _hop_keys(route)
    swap_data = b""
    for instruction in decoded.instructions:
        if decoded.static_keys[instruction.program_index] == solana_jupiter.PROGRAM_ID:
            swap_data = bytes(instruction.data)
    parsed = jupiter_route_v2.parse(swap_data)
    tick_bytes = []
    for key in tick_keys:
        tick = snapshot.accounts.get(key)
        if isinstance(tick, tuple):
            tick_bytes.append(tick[1])
    quoted = jupiter_route_v2._amm_bound(parsed, state, parsed.get("a_to_b") is True, tick_bytes, pool_key if pool_for_quote is None else pool_for_quote)
    est = quoted.get("est_out") if isinstance(quoted, dict) else None
    slip = owner_slippage.check_route(parsed.get("slippage_bps"), parsed.get("min_out"), est)
    if slip.get("ok") is not True:
        return _note(row, "risk", slip.get("reason") or "slippage_exceeds")
    snap = _snapshot(row)

    def work(conn):
        _ensure_conn(conn)
        current = conn.execute("SELECT stage, snapshot_json FROM memecoin_cases WHERE id = ?", (row["id"],)).fetchone()
        if current is None or current["stage"] not in {"filled", "partial", "exit_intent"}:
            return {"ok": False, "reason": "stage", "case_id": row["id"]}
        fresh = json.loads(current["snapshot_json"])
        chain = getattr(snapshot, "server_chain", None)
        if isinstance(chain, dict) and (chain.get("bind_ok") is not True or decoded.blockhash != chain.get("blockhash")):
            if fresh.get("inflight"):
                return {"ok": False, "reason": "exit_inflight", "case_id": row["id"], "resend": False, "holds": True, "rewritten": False}
            return {"ok": False, "reason": "blockhash_mismatch" if decoded.blockhash != chain.get("blockhash") else (chain.get("reason") or "block_height_missing"), "case_id": row["id"], "holds": True, "resend": False, "rewritten": False, "send": False}
        priors = conn.execute("SELECT * FROM memecoin_reservations WHERE case_id = ? AND side = 'exit'", (row["id"],)).fetchall()
        for stored in priors:
            if stored["message_sha256"] == digest:
                if _same_reservation(stored, case_id=row["id"], side="exit", digest=digest, chain=chain, pool=pool_key):
                    return {"ok": True, "reason": None, "case_id": row["id"], "qty": qty, "message_sha256": digest, "idempotent": True, "resend": False, "rewritten": False, "send": False, "quote_expiry_proven": False, "inclusion_window_proven": isinstance(chain, dict) and chain.get("inclusion_window_proven") is True}
                return {"ok": False, "reason": "reservation_conflict", "case_id": row["id"], "resend": False, "holds": True, "rewritten": False}
            if _reservation_status(conn, stored) in {"active", "unknown"}:
                return {"ok": False, "reason": "exit_inflight", "case_id": row["id"], "resend": False, "holds": True, "rewritten": False}
        if fresh.get("inflight"):
            return {"ok": False, "reason": "exit_inflight", "case_id": row["id"], "resend": False, "holds": True, "rewritten": False}
        remaining = fresh.get("remaining_raw")
        if not isinstance(remaining, int) or remaining < qty:
            return {"ok": False, "reason": "quantity", "case_id": row["id"]}
        intent = conn.execute("SELECT id, message_sha256 FROM memecoin_risk_intents WHERE case_id = ?", (row["id"],)).fetchone()
        fresh["inflight"] = {
            "message_sha256": digest,
            "intent_id": None if intent is None else intent["id"],
            "qty": qty,
            "pool": pool_key,
            "mint": row["mint"],
            "min_out": route["min_out"],
            "instruction_slippage_bps": parsed.get("slippage_bps"),
            "quoted_out_amount": parsed.get("quoted_out_amount"),
            "amm_est_out": est,
            "dex_fee_lamports": dex_lamports,
            "network_lamports": network["network_lamports"],
            "reserved_fee_usd": format(fee_usd, "f"),
            "sol_price": sol.get("price"),
            "sol_conf": sol.get("conf"),
            "sol_expo": sol.get("expo"),
            "snapshot_slot": snapshot.slot,
            "snapshot_tip_slot": snapshot.tip_slot,
            "snapshot_fetched_at": snapshot.fetched_at,
            "blockhash": decoded.blockhash,
            "last_valid_block_height": None if not isinstance(chain, dict) else chain.get("last_valid_block_height"),
            "last_valid_provenance": None if not isinstance(chain, dict) else chain.get("last_valid_provenance"),
            "router": None if not isinstance(chain, dict) else chain.get("router"),
            "block_height": None if not isinstance(chain, dict) else chain.get("block_height"),
            "expire_at": None if not isinstance(chain, dict) else chain.get("expire_at"),
            "inclusion_window_proven": isinstance(chain, dict) and chain.get("inclusion_window_proven") is True,
            "price_freshness_proven": False,
            "quote_observed_at": None if not isinstance(chain, dict) else chain.get("observed_at"),
            "quote_expiry_source": None if not isinstance(chain, dict) else chain.get("quote_expiry_source"),
            "provider_quote_expiry_published": isinstance(chain, dict) and chain.get("provider_quote_expiry_published") is True,
            "internal_time_limit": None,
            "dex_fee_source": "reserved_pool_state",
            "dex_fee_remeasured": False,
            "quote_expiry_rule": "getBlockHeight_le_lastValidBlockHeight" if isinstance(chain, dict) else "slot_distance_guard_not_block_height",
            "slot_distance_is_blockhash_proof": False,
            "quote_clock_proven": False,
            "quote_expiry_proven": False,
            "inclusion_window_proven": isinstance(chain, dict) and chain.get("inclusion_window_proven") is True,
            "price_freshness_proven": False,
            "status": "reserved",
            "signature": None,
        }
        if intent is not None and intent["message_sha256"] is None:
            conn.execute(
                "UPDATE memecoin_risk_intents SET message_sha256 = ? WHERE id = ? AND message_sha256 IS NULL",
                (digest, intent["id"]),
            )
        _store_reservation(conn, case_id=row["id"], side="exit", message_b64=message_b64, digest=digest, chain=chain, pool=pool_key, intent_id=None if intent is None else intent["id"], ledger=_ledger_of(row), moment=moment)
        conn.execute(
            "UPDATE memecoin_cases SET snapshot_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(fresh, sort_keys=True), moment, row["id"]),
        )
        _log(conn, row["id"], row["mint"], "risk", "pass", None, "exit_reserve", {"qty": qty, "message_sha256": digest, "intent_id": None if intent is None else intent["id"]}, moment)
        return {
            "ok": True, "reason": None, "case_id": row["id"], "qty": qty, "message_sha256": digest,
            "instruction_slippage_bps": parsed.get("slippage_bps"), "quoted_out_amount": parsed.get("quoted_out_amount"),
            "min_out": route["min_out"], "amm_est_out": est,
            "quote_clock_proven": False,
            "quote_expiry_proven": False,
            "inclusion_window_proven": isinstance(chain, dict) and chain.get("inclusion_window_proven") is True,
            "price_freshness_proven": False,
            "blockhash": decoded.blockhash,
            "last_valid_block_height": None if not isinstance(chain, dict) else chain.get("last_valid_block_height"),
            "block_height": None if not isinstance(chain, dict) else chain.get("block_height"),
            "live_ready": False, "send": False, "resend": False, "rewritten": False,
        }

    return db.exclusive(work)


def confirm_exit(case_id: str, signature: str, now: float | None = None) -> dict:
    """Read getTransaction through the server reader and book only the landed reserved slice."""
    import desk_loop
    import memecoin_exit
    import pyth_sponsored
    duplicate = _duplicate_exit(signature)
    if duplicate is not None:
        return duplicate
    row = _case(case_id)
    if row is None or row["stage"] not in {"filled", "partial", "exit_intent"}:
        return _kill(case_id, None, "risk", "stage", None)
    snap = _snapshot(row)
    mode = execution_mode(row)
    if mode == "unknown":
        return _quarantine(row, "ledger_unknown")
    if mode == "synthetic":
        return _note(row, "risk", "synthetic_not_live")
    if mode == "live" and (_live_entry_receipt(row["id"]) is None or _matching_intent(row["id"]) is None):
        return _note(row, "risk", "exit_unproven")
    if mode not in {"live", "paper"}:
        return _note(row, "risk", "exit_unproven")
    inflight = snap.get("inflight")
    if not isinstance(inflight, dict):
        return _hold(row, "exit_not_reserved", status="held")
    if inflight.get("status") == "unknown" and inflight.get("signature") not in (None, signature):
        return _hold(row, "no_resend", status="unknown")
    pulled = desk_loop.read_transaction(signature)
    moment = time.time() if now is None else now
    if pulled.get("ok") is not True:
        snap["inflight"]["status"] = "unknown"
        snap["inflight"]["signature"] = signature

        def hold(conn):
            _ensure_conn(conn)
            _note_unknown_signature(conn, row["id"], inflight.get("message_sha256"), signature, moment)
            conn.execute(
                "UPDATE memecoin_cases SET snapshot_json = ?, updated_at = ? WHERE id = ? AND stage = ?",
                (json.dumps(snap, sort_keys=True), moment, row["id"], row["stage"]),
            )
            return _hold_conn(conn, row, pulled.get("reason") or "receipt_unknown", status="unknown", moment=moment)

        return db.exclusive(hold)
    flight = dict(inflight)
    if pulled.get("block_time") is not None:
        flight["block_time"] = pulled.get("block_time")
    proven = memecoin_exit.match_landed(flight, signature, pulled)
    if proven.get("ok") is not True:
        return _hold(
            row, proven.get("reason") or "receipt_unbound", status="held",
            snapshot_slot=proven.get("snapshot_slot"), slot=proven.get("slot"),
            slot_distance=proven.get("slot_distance"), slot_distance_cap=proven.get("slot_distance_cap"),
            slot_distance_is_blockhash_proof=False, blockhash_expiry_needs_block_height=True,
            quote_clock_proven=False, quote_expiry_proven=False,
            blockhash_rechecked=False, dex_fee_remeasured=False,
        )
    qty = inflight["qty"]

    def work(conn):
        _ensure_conn(conn)
        current = conn.execute("SELECT stage, snapshot_json FROM memecoin_cases WHERE id = ?", (row["id"],)).fetchone()
        if current is None or current["stage"] not in {"filled", "partial", "exit_intent"}:
            return {"ok": False, "reason": "stage", "case_id": row["id"], "holds": True, "resend": False, "recorded": False, "status": "held"}
        fresh = json.loads(current["snapshot_json"])
        stored = fresh.get("inflight") or {}
        if stored.get("message_sha256") != proven["message_sha256"]:
            return _hold_conn(conn, row, "message_mismatch", status="held", moment=moment, stage_now=current["stage"])
        seen = conn.execute("SELECT signature FROM memecoin_exit_receipts WHERE signature = ? OR (case_id = ? AND message_sha256 = ?)", (signature, row["id"], proven["message_sha256"])).fetchone()
        if seen is not None:
            return _duplicate_conn(conn, signature, row["id"])
        remaining = int(fresh.get("remaining_raw") or 0) - qty
        if remaining < 0:
            return _hold_conn(conn, row, "quantity", status="held", moment=moment, stage_now=current["stage"])
        dex_lamports = stored.get("dex_fee_lamports")
        if isinstance(dex_lamports, bool) or not isinstance(dex_lamports, int) or dex_lamports < 0 or dex_lamports != proven.get("dex_fee_lamports"):
            return _hold_conn(conn, row, "dex_fee_unbound", status="held", moment=moment, stage_now=current["stage"])
        leg = {"used": True, "fresh": True, "price": stored.get("sol_price"), "conf": stored.get("sol_conf"), "expo": stored.get("sol_expo")}
        actual_network = int(proven["network_fee_lamports"])
        fee_usd = pyth_sponsored.lamports_usd(dex_lamports + actual_network, leg)
        breach = fee_usd is None or fee_usd > MAX_FEE or actual_network > int(stored.get("network_lamports") or 0)
        rent = proven.get("rent") if isinstance(proven.get("rent"), dict) else {}
        fresh["remaining_raw"] = remaining
        fresh.pop("inflight", None)
        fresh["deposits"] = _remember_funding(fresh.get("deposits"), rent)
        receipts = list(fresh.get("live_receipts") or [])
        entry_live = _entry_live(conn, fresh)
        receipt = {
            "signature": signature,
            "qty": qty,
            "sol_proceeds": proven["sol_proceeds"],
            "network_fee_lamports": actual_network,
            "dex_fee_lamports": dex_lamports,
            "dex_fee_source": "reserved_pool_state",
            "dex_fee_remeasured": False,
            "fee_usd": None if fee_usd is None else format(fee_usd, "f"),
            "slot": proven["slot"],
            "snapshot_slot": proven.get("snapshot_slot"),
            "snapshot_not_after_landed": proven.get("snapshot_not_after_landed") is True,
            "slot_distance": proven.get("slot_distance"),
            "blockhash_rechecked": False,
            "slot_distance_cap": proven.get("slot_distance_cap"),
            "slot_distance_is_blockhash_proof": False,
            "pool_matches_reservation": proven.get("pool_matches_reservation") is True,
            "quote_clock_proven": False,
            "wsol_rent_net_lamports": (rent.get("wsol") or {}).get("net_lamports"),
            "wsol_rent_net_proven": (rent.get("wsol") or {}).get("net_proven") is True,
            "mint_ata_rent_lamports": (rent.get("mint_ata") or {}).get("rent_lamports"),
            "mint_refund_proven": False,
            "fee_policy_breach": breach,
            "exit_chain_proven": True,
            "entry_funding_proven": entry_live,
            "live_ledger": False,
        }
        receipts.append(receipt)
        fresh["live_receipts"] = receipts
        fresh["exit_chain_proven"] = True
        fresh["entry_funding_proven"] = entry_live
        fresh["live_ledger"] = False
        fresh["pnl_live"] = False
        fresh["fill_fixture_is_not_live"] = not entry_live
        stage = "exited" if remaining == 0 else "partial"
        if remaining == 0:
            _keep_unrefunded_deposit(fresh, leg)
        else:
            fresh["deposit_refund_proven"] = False
        if entry_live:
            _apply_live_pnl(fresh, qty, remaining + qty, proven["sol_proceeds"], leg, fee_usd)
        else:
            fresh.pop("realized_pnl", None)
            fresh["pnl_live"] = False
        _write_deposits(conn, row["id"], fresh.get("deposits") or {})
        if breach:
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES ('memecoin_entry_halt', '1', ?) ON CONFLICT(key) DO UPDATE SET value = '1', updated_at = excluded.updated_at",
                (moment,),
            )
        conn.execute(
            "INSERT INTO memecoin_exit_receipts (signature, case_id, message_sha256, slot, fee_usd, snapshot_slot, provenance_json, qty, ledger, ledger_provenance) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'server')",
            (
                signature, row["id"], proven["message_sha256"], proven["slot"],
                "unpriced" if fee_usd is None else format(fee_usd, "f"),
                proven.get("snapshot_slot"),
                json.dumps({"dex_fee_source": "reserved_pool_state", "dex_fee_remeasured": False, "blockhash_rechecked": False, "quote_clock_proven": False, "pool": proven.get("pool")}, sort_keys=True),
                qty, _ledger_of(row),
            ),
        )
        conn.execute(
            "UPDATE memecoin_cases SET stage = ?, snapshot_json = ?, updated_at = ? WHERE id = ?",
            (stage, json.dumps(fresh, sort_keys=True), moment, row["id"]),
        )
        _log(conn, row["id"], row["mint"], "risk", "pass" if not breach else "kill", None if not breach else "fee_policy_breach", "exit_receipt", receipt, moment)
        holds = remaining > 0 or fresh.get("deposit_refund_proven") is not True
        return {
            "ok": not breach,
            "recorded": True,
            "reason": "fee_policy_breach" if breach else None,
            "case_id": row["id"],
            "stage": stage,
            "status": "breach" if breach else "booked",
            "remaining_raw": remaining,
            "reserved_usd": fresh.get("reserved_usd"),
            "sol_proceeds": proven["sol_proceeds"],
            "fee_usd": None if fee_usd is None else format(fee_usd, "f"),
            "network_fee_lamports": actual_network,
            "dex_fee_lamports": dex_lamports,
            "dex_fee_source": "reserved_pool_state",
            "dex_fee_remeasured": False,
            "snapshot_slot": proven.get("snapshot_slot"),
            "slot": proven["slot"],
            "snapshot_not_after_landed": True,
            "slot_distance": proven.get("slot_distance"),
            "blockhash_rechecked": False,
            "slot_distance_cap": proven.get("slot_distance_cap"),
            "slot_distance_is_blockhash_proof": False,
            "blockhash_expiry_needs_block_height": True,
            "pool_matches_reservation": True,
            "quote_clock_proven": False,
            "quote_expiry_proven": False,
            "wsol_rent_net_lamports": receipt["wsol_rent_net_lamports"],
            "wsol_rent_net_proven": receipt["wsol_rent_net_proven"],
            "position_rent_lamports": receipt["mint_ata_rent_lamports"],
            "mint_refund_proven": False,
            "fee_policy_breach": breach,
            "entry_halt": breach,
            "entry_funding_proven": False if not entry_live else True,
            "exit_chain_proven": True,
            "pnl_live": fresh.get("pnl_live") is True,
            "pnl_source": fresh.get("pnl_source"),
            "realized_pnl": fresh.get("realized_pnl"),
            "live_ledger": fresh.get("pnl_live") is True,
            "holds": holds,
            "send": False,
            "live_ready": False,
            "resend": False,
        }

    return db.exclusive(work)


def confirm_deposit_refund(case_id: str, signature: str, now: float | None = None) -> dict:
    """Release only the recorded mint-account rent after that exact balance reaches the wallet."""
    import desk_loop
    import memecoin_exit
    import pyth_sponsored
    row = _case(case_id)
    if row is None:
        return _kill(case_id, None, "risk", "stage", None)
    mode = execution_mode(row)
    if mode not in {"live", "paper"}:
        return _hold(row, "ledger_unknown" if mode == "unknown" else "synthetic_not_live", status="held")
    snap = _snapshot(row)
    if row["stage"] == "paper_exited":
        return _hold(row, "paper_not_live", status="held")
    if snap.get("deposit_refund_proven") is True:
        return _hold(row, "deposit_duplicate", status="duplicate", holds=False)
    remaining = snap.get("remaining_raw")
    if not isinstance(remaining, int) or remaining > 0:
        return _hold(row, "tokens_unsold", status="held")
    if not isinstance(snap.get("deposits"), dict):
        return _hold(row, "deposit_unobserved", status="held")
    mint = snap.get("deposits", {}).get("mint_ata")
    if not isinstance(mint, dict) or mint.get("preexisting") is True:
        return _hold(row, "preexisting_account" if isinstance(mint, dict) and mint.get("preexisting") is True else "deposit_unobserved", status="held")
    if mint.get("bot_funded") is not True:
        return _hold(row, "deposit_unobserved", status="held")
    duplicate = _refund_duplicate(signature)
    if duplicate is not None:
        return duplicate
    pulled = desk_loop.read_transaction(signature)
    moment = time.time() if now is None else now
    if pulled.get("reason") == "receipt_failed":
        failed = memecoin_exit.match_failed_refund(mint, signature, pulled)
        if failed.get("attributable") is not True:
            return _hold(row, failed.get("reason") or "receipt_unknown", status="unknown" if failed.get("reason") in {"receipt_unknown", "receipt_unconfirmed"} else "held")
        return _record_refund(row, failed, released=False, moment=moment)
    proven = memecoin_exit.match_refund(mint, signature, pulled)
    if proven.get("ok") is not True:
        return _hold(row, proven.get("reason") or "refund_balance", status="unknown" if proven.get("reason") in {"receipt_unknown", "receipt_unconfirmed"} else "held")

    def work(conn):
        _ensure_conn(conn)
        current = conn.execute("SELECT stage, snapshot_json FROM memecoin_cases WHERE id = ?", (row["id"],)).fetchone()
        if current is None:
            return {"ok": False, "reason": "stage", "holds": True, "recorded": False, "resend": False}
        fresh = json.loads(current["snapshot_json"])
        if fresh.get("deposit_refund_proven") is True:
            return _hold_conn(conn, row, "deposit_duplicate", status="duplicate", moment=moment, stage_now=current["stage"], holds=False)
        body = fresh.get("deposits") if isinstance(fresh.get("deposits"), dict) else {}
        account = (body.get("mint_ata") or {}).get("account")
        rent = (body.get("mint_ata") or {}).get("rent_lamports")
        if account != proven["account"] or rent != proven["rent_lamports"]:
            return _hold_conn(conn, row, "refund_balance", status="held", moment=moment, stage_now=current["stage"])
        fresh["deposit_refund_proven"] = True
        fresh["reserved_usd"] = "0.00"
        fresh["deposits"]["mint_ata"]["refund_proven"] = True
        fresh["deposits"]["mint_ata"]["refund_signature"] = signature
        fresh["deposits"]["mint_ata"]["refund_slot"] = proven["slot"]
        fee_lamports = proven.get("network_fee_lamports")
        leg = (fresh.get("entry_funding") or {}).get("sol_leg") if isinstance(fresh.get("entry_funding"), dict) else None
        if not isinstance(leg, dict):
            leg = fresh.get("sol_leg") if isinstance(fresh.get("sol_leg"), dict) else None
        refund_fee = pyth_sponsored.lamports_usd(fee_lamports, leg) if isinstance(fee_lamports, int) and isinstance(leg, dict) else None
        events = list(fresh.get("live_events") or [])
        events.append({
            "kind": "deposit_refund",
            "signature": signature,
            "rent_lamports": rent,
            "network_fee_lamports": fee_lamports,
            "fee_usd": None if refund_fee is None else format(refund_fee, "f"),
            "trading_pnl_unchanged": True,
        })
        fresh["live_events"] = events
        breach = refund_fee is not None and refund_fee > MAX_FEE
        if breach:
            _halt_entries(conn, moment)
        conn.execute(
            "INSERT INTO memecoin_refunds (signature, case_id, rent_lamports, network_fee_lamports, fee_usd, released, ledger, ledger_provenance) VALUES (?, ?, ?, ?, ?, 1, ?, 'server')",
            (signature, row["id"], rent, 0 if not isinstance(fee_lamports, int) else fee_lamports, "unpriced" if refund_fee is None else format(refund_fee, "f"), _ledger_of(row)),
        )
        conn.execute(
            "UPDATE memecoin_deposits SET refunded = 1, refund_signature = ? WHERE case_id = ? AND kind = 'mint_ata' AND account = ?",
            (signature, row["id"], account),
        )
        conn.execute(
            "UPDATE memecoin_cases SET snapshot_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(fresh, sort_keys=True), moment, row["id"]),
        )
        _log(conn, row["id"], row["mint"], "risk", "pass" if not breach else "kill", None if not breach else "fee_policy_breach", "deposit_refund", {"account": account, "rent_lamports": rent, "signature": signature, "network_fee_lamports": fee_lamports, "wallet": proven["wallet"]}, moment)
        return {
            "ok": not breach,
            "reason": "fee_policy_breach" if breach else None,
            "recorded": True,
            "refunded": True,
            "holds": False,
            "resend": False,
            "status": "refunded",
            "case_id": row["id"],
            "stage": current["stage"],
            "remaining_raw": fresh.get("remaining_raw"),
            "reserved_usd": "0.00",
            "rent_lamports": rent,
            "account": account,
            "wallet": proven["wallet"],
            "network_fee_lamports": proven.get("network_fee_lamports"),
            "refund_fee_usd": None if refund_fee is None else format(refund_fee, "f"),
            "entry_halt": breach,
            "fee_policy_breach": breach,
            "realized_pnl": fresh.get("realized_pnl"),
            "pnl_live": fresh.get("pnl_live") is True,
            "pnl_source": fresh.get("pnl_source"),
            "entry_funding_proven": (fresh.get("entry_funding") or {}).get("source") == "entry_receipt" if isinstance(fresh.get("entry_funding"), dict) else False,
            "send": False,
            "live_ready": False,
        }

    return db.exclusive(work)


def _refund_duplicate(signature: str) -> dict | None:
    _ensure()
    try:
        rows = db.query("SELECT case_id, network_fee_lamports, fee_usd, released FROM memecoin_refunds WHERE signature = ?", (signature,))
    except Exception:
        return None
    if not rows:
        return None
    row = rows[0]
    case = _case(row["case_id"])
    snap = {} if case is None else _snapshot(case)
    released = row["released"] == 1
    return {
        "ok": False,
        "reason": "deposit_duplicate",
        "already_recorded": True,
        "recorded": False,
        "refunded": released,
        "holds": not released and snap.get("deposit_refund_proven") is not True,
        "resend": False,
        "network_fee_lamports": row["network_fee_lamports"],
        "refund_fee_usd": row["fee_usd"],
        "realized_pnl": snap.get("realized_pnl"),
        "pnl_live": snap.get("pnl_live") is True,
        "reserved_usd": snap.get("reserved_usd"),
        "case_id": row["case_id"],
        "stage": None if case is None else case["stage"],
        "status": "duplicate",
        "send": False,
        "live_ready": False,
    }


def _record_refund(row, proven: dict, *, released: bool, moment: float) -> dict:
    """Append one close fee. A failed close does not release the deposit or erase trading pnl."""
    import pyth_sponsored
    signature = proven["signature"]

    def work(conn):
        _ensure_conn(conn)
        current = conn.execute("SELECT stage, snapshot_json FROM memecoin_cases WHERE id = ?", (row["id"],)).fetchone()
        fresh = {} if current is None else json.loads(current["snapshot_json"])
        seen = conn.execute("SELECT signature, fee_usd, released FROM memecoin_refunds WHERE signature = ?", (signature,)).fetchone()
        if seen is not None:
            return {
                "ok": False, "reason": "deposit_duplicate", "already_recorded": True, "recorded": False,
                "refunded": seen["released"] == 1, "holds": seen["released"] != 1, "resend": False,
                "refund_fee_usd": seen["fee_usd"], "realized_pnl": fresh.get("realized_pnl"),
                "pnl_live": fresh.get("pnl_live") is True, "send": False, "live_ready": False, "case_id": row["id"],
            }
        trading = fresh.get("realized_pnl")
        fee_lamports = proven.get("network_fee_lamports")
        leg = (fresh.get("entry_funding") or {}).get("sol_leg") if isinstance(fresh.get("entry_funding"), dict) else None
        refund_fee = pyth_sponsored.lamports_usd(fee_lamports, leg) if isinstance(fee_lamports, int) and isinstance(leg, dict) else None
        breach = refund_fee is not None and refund_fee > MAX_FEE
        anomaly = proven.get("anomaly") is True
        if breach or anomaly:
            _halt_entries(conn, moment)
        events = list(fresh.get("live_events") or [])
        events.append({
            "kind": "deposit_refund_failed" if not released else "deposit_refund",
            "signature": signature,
            "rent_lamports": proven.get("rent_lamports"),
            "network_fee_lamports": fee_lamports,
            "fee_usd": None if refund_fee is None else format(refund_fee, "f"),
            "released": released,
            "trading_pnl_unchanged": True,
        })
        fresh["live_events"] = events
        fresh["realized_pnl"] = trading
        conn.execute(
            "INSERT INTO memecoin_refunds (signature, case_id, rent_lamports, network_fee_lamports, fee_usd, released, ledger, ledger_provenance) VALUES (?, ?, ?, ?, ?, ?, ?, 'server')",
            (signature, row["id"], proven.get("rent_lamports") or 0, fee_lamports if isinstance(fee_lamports, int) else 0, "unpriced" if refund_fee is None else format(refund_fee, "f"), 1 if released else 0, _ledger_of(row)),
        )
        conn.execute(
            "UPDATE memecoin_cases SET snapshot_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(fresh, sort_keys=True), moment, row["id"]),
        )
        _log(conn, row["id"], row["mint"], "risk", "kill", "fee_policy_breach" if breach else "receipt_failed", "deposit_refund", {"signature": signature, "network_fee_lamports": fee_lamports, "released": released}, moment)
        return {
            "ok": False,
            "reason": "fee_policy_breach" if breach else "receipt_failed",
            "recorded": True,
            "refunded": False,
            "holds": True,
            "resend": False,
            "ordinary_revert": proven.get("ordinary") is True and not breach,
            "entry_halt": breach or anomaly,
            "fee_policy_breach": breach,
            "network_fee_lamports": fee_lamports,
            "refund_fee_usd": None if refund_fee is None else format(refund_fee, "f"),
            "realized_pnl": trading,
            "pnl_live": fresh.get("pnl_live") is True,
            "reserved_usd": fresh.get("reserved_usd"),
            "case_id": row["id"],
            "stage": None if current is None else current["stage"],
            "status": "failed",
            "send": False,
            "live_ready": False,
        }

    return db.exclusive(work)


def mark(case_id: str, price_usd: str, *, source: str, fetched_at: float, now: float | None = None) -> dict:
    """Apply the persisted stop and trail. A new loss total does not block this."""
    moment = time.time() if now is None else now
    row = _case(case_id)
    if row is None or row["stage"] != "filled":
        return _kill(case_id, None, "risk", "stage", None)
    mode = execution_mode(row)
    if mode == "live":
        return _note(row, "risk", "mark_source")
    if mode == "unknown":
        return _quarantine(row, "ledger_unknown")
    if mode != "paper":
        return _note(row, "risk", "synthetic_not_live")
    snap = _snapshot(row)
    if not isinstance(source, str) or source == "":
        return _note(row, "risk", "mark_source")
    fetched = _num(fetched_at)
    if fetched is None or abs(Decimal(str(moment)) - fetched) > FRESH_S:
        return _note(row, "risk", "mark_stale")
    price = _money(price_usd)
    if price is None:
        return _note(row, "risk", "mark_price")
    snap = _snapshot(row)
    entry = _money(snap.get("entry_price_usd"))
    stop_px = _money(snap.get("stop_price_usd"))
    peak = _money(snap.get("peak_price_usd")) or entry
    if entry is None or stop_px is None or peak is None:
        return _fail(row, "risk", "policy_not_persisted")
    if price > peak:
        peak = price
    snap["peak_price_usd"] = format(peak, "f")
    arm = entry * (Decimal("100") + TRAIL_ARM) / Decimal("100")
    armed = peak >= arm
    snap["trail_armed"] = armed
    drop_line = peak * (Decimal("100") - TRAIL_DROP) / Decimal("100") if armed else None
    action = "hold"
    if price <= stop_px:
        action = "stop"
    elif armed and drop_line is not None and price <= drop_line:
        action = "trail"
    evidence = {
        "action": action,
        "price_usd": format(price, "f"),
        "source": source,
        "stop_price_usd": format(stop_px, "f"),
        "trail_armed": armed,
        "trail_line_usd": None if drop_line is None else format(drop_line, "f"),
        "trail_breakeven_guaranteed": False,
        "loss_stop_blocks_exit": False,
    }
    if action != "hold":
        snap["live_ledger"] = False
        notional = _money(snap.get("notional_usd")) or TICKET
        entry_fee = _money((snap.get("fill") or {}).get("fee_usd")) or Decimal("0")
        projection = (notional * price / entry) - notional - entry_fee
        snap["paper_exit"] = {
            "action": action,
            "mark_price_usd": format(price, "f"),
            "paper_realized_pnl": format(projection, "f"),
            "counts_toward_live_loss": False,
            "ledger": "paper",
            "exit_fee_unpriced": True,
            "live_ledger": False,
            "receipt_proceeds": None,
            "reason": "paper_projection_is_not_a_fill",
        }
        target = "paper_exited"
        evidence = evidence | {"live_ledger": False, "receipt_proceeds": None}
    else:
        target = "filled"

    def work(conn):
        current = conn.execute("SELECT stage FROM memecoin_cases WHERE id = ?", (row["id"],)).fetchone()
        if current is None or current["stage"] != "filled":
            return {"ok": False, "reason": "stage", "case_id": row["id"], "stage": None if current is None else current["stage"]}
        if _halted_conn(conn):
            _log(conn, row["id"], row["mint"], "risk", "kill", "kill_switch", None, {"reason": "kill_switch"}, moment)
            return {"ok": False, "reason": "kill_switch", "case_id": row["id"], "stage": "filled", "mint": row["mint"]}
        conn.execute(
            "UPDATE memecoin_cases SET stage = ?, snapshot_json = ?, updated_at = ? WHERE id = ? AND stage = 'filled'",
            (target, json.dumps(snap, sort_keys=True), moment, row["id"]),
        )
        _log(conn, row["id"], row["mint"], "risk", "pass", None, source, evidence, moment)
        return {"ok": True, "reason": None, "case_id": row["id"], "stage": target, "mint": row["mint"], "action": action}

    return db.exclusive(work)


def measure_pool_price(pool_data: bytes, mint: str, decimals: int, sol_leg: dict) -> dict:
    """Arithmetic only. The result is not a mark and cannot open an exit."""
    import jupiter_route_v2
    import pyth_sponsored
    refused = {"ok": False, "trusted": False, "reason": "untrusted_measurement"}
    if not isinstance(pool_data, (bytes, bytearray)) or not isinstance(sol_leg, dict):
        return refused
    if isinstance(decimals, bool) or not isinstance(decimals, int) or decimals < 0 or decimals > 18:
        return refused | {"reason": "mint_decimals"}
    fields = __import__("solana_whirlpool").pool_fields(bytes(pool_data))
    sqrt_price = __import__("solana_whirlpool").sqrt_price(bytes(pool_data))
    if fields is None or sqrt_price is None or mint not in {fields[0], fields[2]}:
        return refused | {"reason": "mark_mint"}
    lamports = jupiter_route_v2.token_raw_to_lamports(10 ** decimals, sqrt_price, fields[0] == mint)
    usd = None if lamports is None else pyth_sponsored.lamports_usd(lamports, sol_leg)
    if usd is None or usd <= 0:
        return refused | {"reason": "mark_price"}
    return {"ok": True, "trusted": False, "reason": "untrusted_measurement", "price": format(usd, "f"), "mint": mint}


def read_bound_price(case_id: str, pool_data: bytes, pool: str, sol_leg: dict, decimals: int, slot: int, tip_slot: int, now: float) -> dict:
    """Caller bytes and a caller clock are not a trusted mark."""
    row = _case(case_id)
    mint = row["mint"] if row is not None else ""
    measured = measure_pool_price(pool_data, mint, decimals, sol_leg if isinstance(sol_leg, dict) else {})
    measured["trusted"] = False
    measured["reason"] = "untrusted_measurement"
    measured["pool"] = pool
    measured["slot"] = slot
    measured["tip_slot"] = tip_slot
    measured["now"] = now
    return measured


def mark_open_position(case_id: str, transport, now: float) -> dict:
    """Read the chain and persist the exit in one call. A price object is not an argument."""
    import base64
    import jupiter_route_v2
    import pyth_sponsored
    import solana_inspect
    import solana_mint_read
    import solana_whirlpool
    moment = now
    if not callable(transport):
        return {"ok": False, "reason": "mark_source", "case_id": case_id, "send": False, "live_ready": False, "recorded": False}
    row = _case(case_id)
    if row is None or row["stage"] != "filled" or _ledger_of(row) != "live":
        return {"ok": False, "reason": "stage", "case_id": case_id, "send": False, "live_ready": False, "recorded": False}
    snap = _snapshot(row)
    if isinstance(snap.get("fill"), dict) and snap["fill"].get("fixture") is True:
        return {"ok": False, "reason": "paper_not_live", "case_id": case_id, "send": False, "live_ready": False, "recorded": False}
    receipt = _live_entry_receipt(case_id)
    if receipt is None or not isinstance(receipt["pool"], str):
        return {"ok": False, "reason": "basis_unproven", "case_id": case_id, "send": False, "live_ready": False, "recorded": False}
    entry = _money(snap.get("entry_price_usd"))
    stop_px = _money(snap.get("stop_price_usd"))
    if entry is None or stop_px is None or snap.get("entry_price_source") != "entry_receipt":
        return {"ok": False, "reason": "basis_unproven", "case_id": case_id, "send": False, "live_ready": False, "recorded": False}
    try:
        genesis = transport({"kind": "rpc", "method": "getGenesisHash"})
    except Exception:
        return {"ok": False, "reason": "genesis", "case_id": case_id, "send": False, "recorded": False}
    genesis_body = genesis.get("body") if isinstance(genesis, dict) else None
    if not isinstance(genesis_body, dict) or genesis_body.get("result") != solana_inspect.MAINNET_GENESIS:
        return {"ok": False, "reason": "genesis", "case_id": case_id, "send": False, "recorded": False}
    pool = receipt["pool"]
    first = jupiter_route_v2.read_snapshot(transport, [pool], now)
    if first.get("ok") is not True:
        return {"ok": False, "reason": first.get("reason") or "exit_mark_unpriced", "case_id": case_id, "send": False, "recorded": False}
    record = first["snapshot"].accounts.get(pool)
    if not isinstance(record, tuple) or record[0] != solana_whirlpool.PROGRAM_ID or not solana_whirlpool.pool_pda_matches(pool, record[1]):
        return {"ok": False, "reason": "whirlpool_pool_unbound" if not isinstance(record, tuple) or record[0] != solana_whirlpool.PROGRAM_ID else "whirlpool_pda_unbound", "case_id": case_id, "send": False, "recorded": False}
    fields = solana_whirlpool.pool_fields(record[1])
    if fields is None or row["mint"] not in {fields[0], fields[2]}:
        return {"ok": False, "reason": "mark_mint", "case_id": case_id, "send": False, "recorded": False}
    oracle = solana_whirlpool.oracle_address(pool)
    second = jupiter_route_v2.read_snapshot(transport, [fields[1], fields[3], oracle], now)
    if second.get("ok") is not True:
        return {"ok": False, "reason": second.get("reason") or "whirlpool_vault_unbound", "case_id": case_id, "send": False, "recorded": False}
    accounts = second["snapshot"].accounts
    for vault, mint in ((fields[1], fields[0]), (fields[3], fields[2])):
        vault_account = accounts.get(vault)
        if not isinstance(vault_account, tuple) or vault_account[0] != solana_tx.TOKEN_PROGRAM or not solana_whirlpool.vault_authority_is_pool(vault_account[1], mint, pool):
            return {"ok": False, "reason": "whirlpool_vault_unbound", "case_id": case_id, "send": False, "recorded": False}
    oracle_account = accounts.get(oracle)
    if not isinstance(oracle_account, tuple) or oracle_account[0] != solana_whirlpool.PROGRAM_ID or not solana_whirlpool.oracle_matches(pool, oracle_account[1]):
        return {"ok": False, "reason": "whirlpool_oracle_unbound", "case_id": case_id, "send": False, "recorded": False}
    try:
        mint_info = transport({"kind": "rpc", "method": "getAccountInfo", "mint": row["mint"]})
    except Exception:
        return {"ok": False, "reason": "mint_decimals", "case_id": case_id, "send": False, "recorded": False}
    mint_body = mint_info.get("body") if isinstance(mint_info, dict) else None
    mint_value = ((mint_body or {}).get("result") or {}).get("value") if isinstance(mint_body, dict) else None
    if not isinstance(mint_value, dict) or mint_value.get("owner") != solana_tx.TOKEN_PROGRAM:
        return {"ok": False, "reason": "mint_decimals", "case_id": case_id, "send": False, "recorded": False}
    if solana_mint_read.parse_account(mint_value, row["mint"]).get("ok") is not True:
        return {"ok": False, "reason": "mint_decimals", "case_id": case_id, "send": False, "recorded": False}
    try:
        mint_raw = base64.b64decode(mint_value["data"][0], validate=True)
    except Exception:
        return {"ok": False, "reason": "mint_decimals", "case_id": case_id, "send": False, "recorded": False}
    if len(mint_raw) < 82:
        return {"ok": False, "reason": "mint_decimals", "case_id": case_id, "send": False, "recorded": False}
    decimals = mint_raw[44]
    sol = pyth_sponsored.read_accounts(transport, now)
    leg = sol.get("sol") if isinstance(sol, dict) and sol.get("fresh") is True else None
    if not isinstance(leg, dict) or leg.get("fresh") is not True:
        return {"ok": False, "reason": sol.get("reason") if isinstance(sol, dict) else "sol_usd_unpriced", "case_id": case_id, "send": False, "recorded": False}
    measured = measure_pool_price(record[1], row["mint"], decimals, leg)
    if measured.get("ok") is not True:
        return {"ok": False, "reason": measured.get("reason"), "case_id": case_id, "send": False, "recorded": False}
    price = Decimal(measured["price"])
    slot = first["snapshot"].slot
    peak = _money(snap.get("peak_price_usd")) or entry
    if price > peak:
        peak = price
    snap["peak_price_usd"] = format(peak, "f")
    arm = entry * (Decimal("100") + TRAIL_ARM) / Decimal("100")
    armed = peak >= arm
    snap["trail_armed"] = armed
    drop_line = peak * (Decimal("100") - TRAIL_DROP) / Decimal("100") if armed else None
    action = "hold"
    if price <= stop_px:
        action = "stop"
    elif armed and drop_line is not None and price <= drop_line:
        action = "trail"
    target = "filled"
    if action != "hold":
        snap["exit_intent"] = {
            "action": action,
            "source": "server_pool_pyth",
            "live": True,
            "mark_price_usd": format(price, "f"),
            "pool": pool,
            "slot": slot,
            "fetched_at": moment,
            "decimals": decimals,
            "entry_price_usd": format(entry, "f"),
            "stop_price_usd": format(stop_px, "f"),
        }
        snap["live_ledger"] = True
        target = "exit_intent"

    def work(conn):
        _ensure_conn(conn)
        current = conn.execute("SELECT stage, ledger, snapshot_json FROM memecoin_cases WHERE id = ?", (row["id"],)).fetchone()
        if current is None or current["stage"] != "filled" or current["ledger"] != "live":
            return {"ok": False, "reason": "stage", "case_id": row["id"], "stage": None if current is None else current["stage"]}
        if _halted_conn(conn):
            return {"ok": False, "reason": "kill_switch", "case_id": row["id"], "stage": "filled", "mint": row["mint"]}
        receipt = conn.execute(
            "SELECT signature, qty, cost_basis_lamports, ledger, pool, ledger_provenance FROM memecoin_entry_receipts WHERE case_id = ? AND failed = 0",
            (row["id"],),
        ).fetchone()
        if receipt is None or receipt["ledger"] != "live" or receipt["pool"] != pool:
            return {"ok": False, "reason": "basis_unproven", "case_id": row["id"], "stage": "filled"}
        intent_id = None
        if action != "hold":
            evidence = {
                "action": action, "pool": pool, "slot": slot,
                "mark_price_usd": format(price, "f"), "entry_price_usd": format(entry, "f"),
                "stop_price_usd": format(stop_px, "f"), "entry_signature": receipt["signature"],
                "qty": receipt["qty"], "basis_cost_lamports": receipt["cost_basis_lamports"],
                "decimals": decimals,
            }
            intent_id = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
            existing = conn.execute("SELECT id FROM memecoin_risk_intents WHERE case_id = ?", (row["id"],)).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO memecoin_risk_intents (
                        id, case_id, ledger, ledger_provenance, entry_signature, action, pool, slot, mark_price_usd,
                        entry_price_usd, stop_price_usd, qty, basis_cost_lamports, basis_decimals,
                        evidence_json, created_at
                    ) VALUES (?, ?, 'live', 'server', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        intent_id, row["id"], receipt["signature"], action, pool, slot,
                        format(price, "f"), format(entry, "f"), format(stop_px, "f"), receipt["qty"],
                        receipt["cost_basis_lamports"], decimals, json.dumps(evidence, sort_keys=True), moment,
                    ),
                )
            else:
                intent_id = existing["id"]
            snap["risk_intent_id"] = intent_id
        conn.execute(
            "UPDATE memecoin_cases SET stage = ?, snapshot_json = ?, updated_at = ? WHERE id = ? AND stage = 'filled' AND ledger = 'live'",
            (target, json.dumps(snap, sort_keys=True), moment, row["id"]),
        )
        _log(conn, row["id"], row["mint"], "risk", "pass", None, "server_pool_pyth", {"action": action, "price_usd": format(price, "f"), "pool": pool, "slot": slot, "intent_id": intent_id}, moment)
        return {"ok": True, "reason": None, "case_id": row["id"], "stage": target, "mint": row["mint"], "action": action, "intent_id": intent_id, "mark_price": format(price, "f"), "recorded": action != "hold"}

    return db.exclusive(work)


def record_jupiter_block(case_id: str, assessment: dict) -> dict:
    """A Jupiter quote does not open or close a position."""
    row = _case(case_id)
    if row is None or row["stage"] not in {"protected", "filled"}:
        return _kill(case_id, None, "executor", "stage", None)
    name = "executor" if row["stage"] == "protected" else "risk"
    if not isinstance(assessment, dict) or assessment.get("execution_allowed") is not False or assessment.get("min_out_proven") is not False:
        return _note(row, name, "jupiter_assessment")
    return _note(row, name, "jupiter_instruction_unproven")


def view(case_id: str) -> dict | None:
    row = _case(case_id)
    if row is None:
        return None
    return {"case_id": row["id"], "mint": row["mint"], "stage": row["stage"], "snapshot": _snapshot(row)}


def log_for(case_id: str) -> list[dict]:
    _ensure()
    rows = db.query("SELECT stage, verdict, reason, source, ts, evidence_json FROM memecoin_log WHERE case_id = ? ORDER BY id", (case_id,))
    return [
        {"stage": row["stage"], "verdict": row["verdict"], "reason": row["reason"], "source": row["source"], "ts": row["ts"]}
        for row in rows
    ]


def _advance(row, stage: str, name: str, evidence: dict, snap: dict | None = None, ledger: str | None = None) -> dict:
    moment = time.time()
    body = _snapshot(row) if snap is None else snap

    def work(conn):
        current = conn.execute("SELECT stage FROM memecoin_cases WHERE id = ?", (row["id"],)).fetchone()
        if current is None or current["stage"] != row["stage"]:
            return {"ok": False, "reason": "stage", "case_id": row["id"], "stage": None if current is None else current["stage"]}
        if ledger in _LEDGERS:
            conn.execute(
                "UPDATE memecoin_cases SET stage = ?, snapshot_json = ?, ledger = ?, ledger_provenance = 'server', updated_at = ? WHERE id = ? AND stage = ?",
                (stage, json.dumps(body, sort_keys=True), ledger, moment, row["id"], row["stage"]),
            )
        else:
            conn.execute(
                "UPDATE memecoin_cases SET stage = ?, snapshot_json = ?, updated_at = ? WHERE id = ? AND stage = ?",
                (stage, json.dumps(body, sort_keys=True), moment, row["id"], row["stage"]),
            )
        _log(conn, row["id"], row["mint"], name, "pass", None, evidence.get("source") if isinstance(evidence, dict) else None, evidence, moment)
        return {"ok": True, "reason": None, "case_id": row["id"], "stage": stage, "mint": row["mint"]}

    return db.exclusive(work)


def _fail(row, stage: str, reason: str, evidence: dict | None = None, source: str | None = None) -> dict:
    moment = time.time()

    def work(conn):
        conn.execute(
            "UPDATE memecoin_cases SET stage = 'rejected', updated_at = ? WHERE id = ? AND stage = ?",
            (moment, row["id"], row["stage"]),
        )
        _log(conn, row["id"], row["mint"], stage, "kill", reason, source, evidence or {"reason": reason}, moment)
        return {"ok": False, "reason": reason, "case_id": row["id"], "stage": "rejected", "mint": row["mint"]}

    return db.exclusive(work)


def _kill(case_id, mint, stage: str, reason: str, source) -> dict:
    moment = time.time()
    _ensure()

    def work(conn):
        _ensure_conn(conn)
        _log(conn, case_id, mint, stage, "kill", reason, source if isinstance(source, str) else None, {"reason": reason}, moment)
        current = None
        if isinstance(case_id, str):
            stored = conn.execute("SELECT stage FROM memecoin_cases WHERE id = ?", (case_id,)).fetchone()
            current = None if stored is None else stored["stage"]
        return {"ok": False, "reason": reason, "case_id": case_id, "stage": current, "mint": mint}

    return db.exclusive(work)


def _note(row, stage: str, reason: str) -> dict:
    """Log a block that leaves the case where it is. Halt is one of these."""
    moment = time.time()

    def work(conn):
        current = conn.execute("SELECT stage FROM memecoin_cases WHERE id = ?", (row["id"],)).fetchone()
        stage_now = None if current is None else current["stage"]
        _log(conn, row["id"], row["mint"], stage, "kill", reason, None, {"reason": reason}, moment)
        return {"ok": False, "reason": reason, "case_id": row["id"], "stage": stage_now, "mint": row["mint"]}

    return db.exclusive(work)


_OPEN_CAPITAL = ("protected", "entry_ready", "filled", "partial", "exit_intent", "paper_exited")


_LEDGERS = {"live", "synthetic", "paper"}


def _ledger_of(row) -> str:
    """A name counts only with server provenance. A legacy default is unknown."""
    try:
        value = row["ledger"]
    except Exception:
        return "unknown"
    try:
        provenance = row["ledger_provenance"]
    except Exception:
        provenance = None
    if provenance == "server" and value in _LEDGERS:
        return value
    return "unknown"


def execution_mode(row) -> str:
    """Live, paper, or synthetic comes only from the column and server provenance."""
    return _ledger_of(row)


_LEGACY_LEDGER_DEFAULT = False


def _foreign_ledger(row, stage_name: str) -> dict | None:
    kind = _ledger_of(row)
    if kind == "live":
        return None
    if kind == "unknown":
        return _quarantine(row, "ledger_unknown")
    return _note(row, stage_name, "synthetic_not_live" if kind == "synthetic" else "paper_not_live")


def _quarantine(row, reason: str) -> dict:
    """A forged live flag on a non-live case. No receipt is written and the chain is not read."""
    moment = time.time()

    def work(conn):
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES ('memecoin_integrity_halt', '1', ?) ON CONFLICT(key) DO UPDATE SET value = '1', updated_at = excluded.updated_at",
            (moment,),
        )
        _halt_entries(conn, moment)
        _log(conn, row["id"], row["mint"], "risk", "kill", reason, "ledger", {"reason": reason, "quarantine": True}, moment)
        return {"ok": False, "reason": reason, "quarantine": True, "case_id": row["id"], "stage": row["stage"], "send": False, "live_ready": False, "recorded": False}

    return db.exclusive(work)


def _live_entry_receipt(case_id: str):
    _ensure()
    rows = db.query(
        "SELECT signature, qty, cost_basis_lamports, ledger, pool, ledger_provenance FROM memecoin_entry_receipts WHERE case_id = ? AND failed = 0",
        (case_id,),
    )
    if len(rows) != 1 or rows[0]["ledger"] != "live" or rows[0]["ledger_provenance"] != "server":
        return None
    case = db.query("SELECT id, ledger, ledger_provenance FROM memecoin_cases WHERE id = ?", (case_id,))
    if len(case) != 1 or case[0]["ledger"] != "live" or case[0]["ledger_provenance"] != "server":
        return None
    return rows[0]


def _matching_intent(case_id: str):
    """The server mark row. Snapshot flags are not consulted."""
    _ensure()
    rows = db.query("SELECT * FROM memecoin_risk_intents WHERE case_id = ?", (case_id,))
    receipt = _live_entry_receipt(case_id)
    if len(rows) != 1 or receipt is None:
        return None
    intent = rows[0]
    if intent["ledger"] != "live" or intent["ledger_provenance"] != "server" or intent["entry_signature"] != receipt["signature"]:
        return None
    if intent["case_id"] != case_id:
        return None
    if intent["basis_cost_lamports"] != receipt["cost_basis_lamports"] or intent["pool"] != receipt["pool"]:
        return None
    if not isinstance(intent["qty"], int) or intent["qty"] < 1:
        return None
    return intent


def _replay_ledger(signature: str) -> dict | None:
    _ensure()
    rows = db.query("SELECT case_id, ledger FROM memecoin_entry_receipts WHERE signature = ?", (signature,))
    if not rows:
        return None
    kind = rows[0]["ledger"]
    if kind == "live":
        return None
    return {"ok": False, "reason": "cross_ledger_replay", "case_id": rows[0]["case_id"], "send": False, "live_ready": False, "recorded": False}


def _room_for(conn, case_id: str, incoming: Decimal) -> bool:
    """True when this live ticket still fits. A book already at $100 refuses another ticket."""
    return _held_conn(conn, exclude_id=case_id) + incoming <= ALLOCATION


def _held_conn(conn, exclude_id=None) -> Decimal:
    """Full ticket stays reserved while tokens or an unrefunded deposit remain."""
    total = Decimal("0")
    for item in conn.execute("SELECT id, stage, snapshot_json, ledger, ledger_provenance FROM memecoin_cases"):
        if exclude_id is not None and item["id"] == exclude_id:
            continue
        if item["ledger"] == "synthetic" and item["ledger_provenance"] == "server":
            continue
        body = json.loads(item["snapshot_json"])
        remaining = body.get("remaining_raw")
        deposit_open = body.get("deposit_refund_proven") is not True and (isinstance(remaining, int) and remaining == 0 and (body.get("deposits") or body.get("live_receipts")))
        failed_hold = item["stage"] in {"entry_failed", "entry_unreconciled"} and (_reserved_amount(body.get("reserved_usd")) or Decimal("0")) > 0
        unresolved = (isinstance(remaining, int) and remaining > 0) or isinstance(body.get("inflight"), dict) or isinstance(body.get("entry_inflight"), dict) or deposit_open or failed_hold
        if item["stage"] not in _OPEN_CAPITAL and not unresolved:
            continue
        if item["stage"] == "exited" and not unresolved:
            continue
        reserved = _reserved_amount(body.get("reserved_usd"))
        total += TICKET if reserved is None else reserved
    return total


def _reserved_amount(value) -> Decimal | None:
    """A stored zero is released capital. A missing figure is still the whole ticket."""
    if value is None:
        return None
    number = _num(value)
    if number is None or not number.is_finite() or number < 0:
        return None
    return number


def _entry_live(conn, snap: dict) -> bool:
    """A harness flag is not an entry receipt. The signature has to be in the entry table."""
    funding = snap.get("entry_funding") if isinstance(snap.get("entry_funding"), dict) else {}
    signature = funding.get("signature")
    if funding.get("source") != "entry_receipt" or funding.get("proven") is not True or not isinstance(signature, str):
        return False
    row = conn.execute("SELECT signature, qty FROM memecoin_entry_receipts WHERE signature = ? AND failed = 0", (signature,)).fetchone()
    return row is not None and row["qty"] == funding.get("tokens_out")


def _remember_funding(prior, rent: dict) -> dict:
    """A later slice must not relabel an account this desk already funded as a user's account."""
    body = dict(rent) if isinstance(rent, dict) else {}
    previous = prior if isinstance(prior, dict) else {}
    for name in ("mint_ata", "position_account", "wsol"):
        old = previous.get(name) if isinstance(previous.get(name), dict) else {}
        new = body.get(name) if isinstance(body.get(name), dict) else {}
        if old.get("bot_funded") is True and new:
            new = dict(new)
            new["bot_funded"] = True
            new["preexisting"] = False
            new["funder"] = old.get("funder")
            new["provenance"] = old.get("provenance")
            body[name] = new
    return body


def _keep_unrefunded_deposit(fresh: dict, leg: dict) -> None:
    """Token quantity zero does not refund a mint account the exit left funded."""
    import pyth_sponsored
    fresh["deposit_refund_proven"] = False
    mint = (fresh.get("deposits") or {}).get("mint_ata") if isinstance(fresh.get("deposits"), dict) else None
    if not isinstance(mint, dict) or mint.get("observed") is not True or mint.get("preexisting") is True:
        fresh["deposit_hold_reason"] = "preexisting_mint_account" if isinstance(mint, dict) and mint.get("preexisting") is True else "mint_ata_unobserved"
        return
    rent = mint.get("rent_lamports")
    if isinstance(rent, bool) or not isinstance(rent, int) or rent <= 0 or mint.get("bot_funded") is not True:
        fresh["deposit_hold_reason"] = "mint_rent_unproven"
        return
    usd = pyth_sponsored.lamports_usd(rent, leg)
    if usd is None:
        fresh["deposit_hold_reason"] = "deposit_unpriced"
        return
    current = _reserved_amount(fresh.get("reserved_usd"))
    if current is None:
        current = TICKET
    fresh["reserved_usd"] = format(usd if usd <= current else current, "f")
    fresh["deposit_hold_lamports"] = rent
    fresh["deposit_hold_reason"] = "mint_ata_still_funded"


def _write_deposits(conn, case_id: str, deposits: dict) -> None:
    for name in ("wsol", "mint_ata", "position_account"):
        row = deposits.get(name) if isinstance(deposits.get(name), dict) else None
        if row is None or not isinstance(row.get("account"), str):
            continue
        conn.execute(
            """
            INSERT INTO memecoin_deposits (case_id, account, kind, rent_lamports, funder, provenance, preexisting, bot_funded, refund_signature, refunded)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0)
            ON CONFLICT(case_id, kind) DO UPDATE SET
                account = excluded.account,
                rent_lamports = excluded.rent_lamports,
                funder = excluded.funder,
                provenance = excluded.provenance,
                preexisting = excluded.preexisting,
                bot_funded = excluded.bot_funded
            """,
            (
                case_id, row["account"], name, row.get("rent_lamports"), row.get("funder"), row.get("provenance") or "unproven",
                1 if row.get("preexisting") is True else 0,
                None if row.get("bot_funded") is None else (1 if row.get("bot_funded") is True else 0),
            ),
        )


def _apply_live_pnl(fresh: dict, sold: int, before: int, proceeds_lamports: int, exit_leg: dict, exit_fee) -> None:
    """Realized pnl from the entry receipt's remaining lamport basis and the exit receipt. The case price is not used."""
    import pyth_sponsored
    funding = fresh.get("entry_funding") if isinstance(fresh.get("entry_funding"), dict) else None
    basis = None if funding is None else funding.get("cost_basis_lamports_remaining")
    if funding is None or isinstance(basis, bool) or not isinstance(basis, int) or basis < 0 or before <= 0 or sold > before:
        fresh["pnl_live"] = False
        return
    consumed = basis * sold // before
    funding["cost_basis_lamports_remaining"] = basis - consumed
    entry_leg = funding.get("sol_leg")
    cost = pyth_sponsored.lamports_usd(consumed, entry_leg) if isinstance(entry_leg, dict) else None
    proceeds = pyth_sponsored.lamports_usd(proceeds_lamports, exit_leg)
    fee = exit_fee if isinstance(exit_fee, Decimal) else None
    if cost is None or proceeds is None or fee is None:
        fresh["pnl_live"] = False
        return
    prior = _num(fresh.get("realized_pnl")) or Decimal("0")
    fresh["realized_pnl"] = format(prior + proceeds - cost - fee, "f")
    fresh["pnl_live"] = True
    fresh["pnl_source"] = "entry_receipt_and_exit_receipt"
    fresh["pnl_uses_case_price"] = False


def _hop_keys(route: dict):
    keys = route.get("keys") if isinstance(route.get("keys"), list) else []
    if len(keys) < 16:
        return None, []
    import jupiter_route_v2
    cursor = 7
    if keys[cursor] != jupiter_route_v2.EVENT:
        cursor += 1
    base = cursor + 2
    if base + 10 >= len(keys):
        return None, []
    return keys[base + 3], [keys[base + item] for item in (8, 9, 10)]


def _effectual_blocks() -> list[str]:
    import desk_loop
    reasons = [item for item in desk_loop._ready_reasons() if item != "entry_halt"]
    reasons.append("quote_expiry_unproven")
    return reasons


def _reservation_view(case_id: str, reservation: dict, *, idempotent: bool) -> dict:
    return {
        "ok": True,
        "reason": None,
        "case_id": case_id,
        "idempotent": idempotent,
        "message_sha256": reservation.get("message_sha256"),
        "input_lamports": reservation.get("input_lamports"),
        "instruction_slippage_bps": reservation.get("instruction_slippage_bps"),
        "quoted_out_amount": reservation.get("quoted_out_amount"),
        "min_out": reservation.get("min_out"),
        "owner_min_out": reservation.get("owner_min_out"),
        "amm_est_out": reservation.get("amm_est_out"),
        "max_slippage_bps": (reservation.get("policy") or {}).get("max_slippage_bps"),
        "exposure_usd": reservation.get("exposure_usd"),
        "reserved_fee_usd": reservation.get("reserved_fee_usd"),
        "wallet": reservation.get("wallet"),
        "mint": reservation.get("mint"),
        "owner_slippage_approved": (reservation.get("policy") or {}).get("owner_slippage_approved") is True,
        "test_consent_is_owner_consent": False,
        "effectual_entry": False,
        "effectual_blocked_by": reservation.get("effectual_blocked_by"),
        "quote_clock_proven": reservation.get("quote_clock_proven") is True,
        "quote_expiry_proven": reservation.get("quote_expiry_proven") is True,
        "blockhash": reservation.get("blockhash"),
        "last_valid_block_height": reservation.get("last_valid_block_height"),
        "block_height": reservation.get("block_height"),
        "oracle_fresh": reservation.get("oracle_fresh") is True,
        "slot_distance_is_blockhash_proof": False,
        "send": False,
        "sign": False,
        "live_ready": False,
        "resend": False,
    }


def _duplicate_entry(signature: str) -> dict | None:
    _ensure()
    rows = db.query("SELECT case_id FROM memecoin_entry_receipts WHERE signature = ?", (signature,))
    if not rows:
        return None
    return db.exclusive(lambda conn: _duplicate_entry_conn(conn, signature, rows[0]["case_id"]))


def _duplicate_entry_conn(conn, signature: str, case_id: str) -> dict:
    row = conn.execute("SELECT stage, snapshot_json FROM memecoin_cases WHERE id = ?", (case_id,)).fetchone()
    snap = {} if row is None else json.loads(row["snapshot_json"])
    remaining = snap.get("remaining_raw")
    holds = (isinstance(remaining, int) and remaining > 0) or snap.get("deposit_refund_proven") is not True
    return {
        "ok": False,
        "reason": "entry_duplicate",
        "already_recorded": True,
        "recorded": False,
        "holds": holds,
        "resend": False,
        "remaining_raw": remaining,
        "reserved_usd": snap.get("reserved_usd"),
        "stage": None if row is None else row["stage"],
        "case_id": case_id,
        "status": "duplicate",
        "signature": signature,
        "send": False,
        "sign": False,
        "live_ready": False,
        "proceeds": None,
    }


def _halt_entries(conn, moment: float) -> None:
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ('memecoin_entry_halt', '1', ?) ON CONFLICT(key) DO UPDATE SET value = '1', updated_at = excluded.updated_at",
        (moment,),
    )


def _book_failed_entry(row, inflight: dict, signature: str, pulled: dict, moment: float) -> dict:
    """A reverted buy still spent its network fee. That fee is a live loss, not a token position."""
    import base64
    import pyth_sponsored
    transaction = pulled.get("transaction")
    if not isinstance(transaction, str):
        return _hold(row, "receipt_failed", status="held")
    try:
        landed = solana_tx.first_signature(transaction)
        digest = hashlib.sha256(solana_tx.message_bytes(transaction)).hexdigest()
        decoded = solana_tx.decode_transaction(base64.b64decode(transaction, validate=True))
    except Exception:
        return _hold(row, "receipt_unsigned", status="held")
    if landed != signature:
        return _hold(row, "signature_mismatch", status="held")
    if digest != inflight.get("message_sha256"):
        return _hold(row, "message_mismatch", status="held")
    fee = pulled.get("network_fee_lamports")
    if isinstance(fee, bool) or not isinstance(fee, int) or fee < 0:
        return _hold(row, "network_fee_missing", status="held")
    meta = pulled.get("meta") if isinstance(pulled.get("meta"), dict) else {}
    changed = _failed_balances_changed(decoded, meta, fee)
    leg = {"used": True, "fresh": True, "price": inflight.get("sol_price"), "conf": inflight.get("sol_conf"), "expo": inflight.get("sol_expo")}
    fee_usd = pyth_sponsored.lamports_usd(fee, leg)

    def work(conn):
        _ensure_conn(conn)
        seen = conn.execute("SELECT signature FROM memecoin_entry_receipts WHERE signature = ?", (signature,)).fetchone()
        if seen is not None:
            return _duplicate_entry_conn(conn, signature, row["id"])
        current = conn.execute("SELECT snapshot_json FROM memecoin_cases WHERE id = ?", (row["id"],)).fetchone()
        fresh = {} if current is None else json.loads(current["snapshot_json"])
        fresh.pop("entry_inflight", None)
        fresh["entry_funding"] = {
            "proven": True,
            "live_proof": True,
            "source": "entry_receipt",
            "signature": signature,
            "failed": True,
            "qty_is_live": False,
            "tokens_out": 0,
            "fee_usd": None if fee_usd is None else format(fee_usd, "f"),
            "network_fee_lamports": fee,
        }
        fresh["remaining_raw"] = 0
        fresh["pnl_live"] = True
        fresh["pnl_source"] = "failed_entry_network_fee"
        fresh["pnl_uses_case_price"] = False
        fresh["realized_pnl"] = "0.00" if fee_usd is None else format(-fee_usd, "f")
        fresh["proceeds"] = None
        ordinary = (not changed) and fee_usd is not None and fee_usd <= MAX_FEE
        if changed:
            fresh["deposit_refund_proven"] = False
            fresh["failed_balance_unproven"] = True
        else:
            fresh["reserved_usd"] = "0.00"
            fresh["deposit_refund_proven"] = True
            fresh["failed_balance_unproven"] = False
        halt_this = not ordinary
        if halt_this:
            _halt_entries(conn, moment)
        conn.execute(
            "INSERT INTO memecoin_entry_receipts (signature, case_id, message_sha256, slot, qty, cost_basis_lamports, network_fee_lamports, dex_fee_lamports, fee_usd, failed, ledger, ledger_provenance) VALUES (?, ?, ?, ?, 0, 0, ?, 0, ?, 1, ?, 'server')",
            (signature, row["id"], digest, int(pulled.get("slot") or 0), fee, "unpriced" if fee_usd is None else format(fee_usd, "f"), _ledger_of(row)),
        )
        conn.execute(
            "UPDATE memecoin_cases SET stage = 'entry_failed', snapshot_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(fresh, sort_keys=True), moment, row["id"]),
        )
        _log(conn, row["id"], row["mint"], "executor", "kill", "receipt_failed", "entry_receipt", {"signature": signature, "network_fee_lamports": fee, "proceeds": None}, moment)
        return {
            "ok": False,
            "recorded": True,
            "reason": "failed_balance_unproven" if changed else "receipt_failed",
            "case_id": row["id"],
            "stage": "entry_failed",
            "status": "failed",
            "qty": 0,
            "proceeds": None,
            "network_fee_lamports": fee,
            "fee_usd": fresh["entry_funding"]["fee_usd"],
            "realized_pnl": fresh["realized_pnl"],
            "pnl_live": True,
            "entry_halt": halt_this,
            "ordinary_revert": ordinary,
            "holds": changed,
            "reserved_usd": fresh.get("reserved_usd"),
            "send": False,
            "sign": False,
            "live_ready": False,
            "resend": False,
            "effectual_entry": False,
        }

    return db.exclusive(work)


def _book_unreconciled_entry(row, inflight: dict, signature: str, pulled: dict, proven: dict, moment: float) -> dict:
    """A landed buy whose lamports do not balance stays held. The fee already paid is not dropped."""
    import pyth_sponsored
    fee = proven.get("network_fee_lamports")
    if not isinstance(fee, int) or isinstance(fee, bool):
        meta = pulled.get("meta") if isinstance(pulled.get("meta"), dict) else {}
        fee = meta.get("fee") if isinstance(meta.get("fee"), int) and not isinstance(meta.get("fee"), bool) else None
    spent = proven.get("wallet_spent_lamports")
    leg = {"used": True, "fresh": True, "price": inflight.get("sol_price"), "conf": inflight.get("sol_conf"), "expo": inflight.get("sol_expo")}
    incurred = spent if isinstance(spent, int) and spent >= 0 else fee
    incurred_usd = pyth_sponsored.lamports_usd(incurred, leg) if isinstance(incurred, int) else None

    def work(conn):
        _ensure_conn(conn)
        seen = conn.execute("SELECT signature FROM memecoin_entry_receipts WHERE signature = ?", (signature,)).fetchone()
        if seen is not None:
            return _duplicate_entry_conn(conn, signature, row["id"])
        current = conn.execute("SELECT snapshot_json FROM memecoin_cases WHERE id = ?", (row["id"],)).fetchone()
        fresh = {} if current is None else json.loads(current["snapshot_json"])
        fresh.pop("entry_inflight", None)
        fresh["entry_unreconciled"] = True
        fresh["entry_funding"] = {
            "proven": True,
            "live_proof": True,
            "source": "entry_receipt",
            "signature": signature,
            "failed": False,
            "anomaly": True,
            "qty_is_live": False,
            "tokens_out": 0,
            "network_fee_lamports": fee,
            "wallet_spent_lamports": spent,
            "fee_usd": None if incurred_usd is None else format(incurred_usd, "f"),
        }
        fresh["pnl_live"] = True
        fresh["pnl_source"] = "unexplained_lamports"
        fresh["pnl_uses_case_price"] = False
        fresh["realized_pnl"] = "0.00" if incurred_usd is None else format(-incurred_usd, "f")
        fresh["proceeds"] = None
        fresh["deposit_refund_proven"] = False
        events = list(fresh.get("live_events") or [])
        events.append({"kind": "unexplained_lamports", "signature": signature, "accounts": proven.get("accounts"), "network_fee_lamports": fee, "wallet_spent_lamports": spent})
        fresh["live_events"] = events
        _halt_entries(conn, moment)
        conn.execute(
            "INSERT INTO memecoin_entry_receipts (signature, case_id, message_sha256, slot, qty, cost_basis_lamports, network_fee_lamports, dex_fee_lamports, fee_usd, failed, ledger, ledger_provenance) VALUES (?, ?, ?, ?, 0, ?, ?, 0, ?, 2, ?, 'server')",
            (
                signature, row["id"], inflight.get("message_sha256"), int(pulled.get("slot") or 0),
                0 if not isinstance(spent, int) else spent,
                0 if not isinstance(fee, int) else fee,
                "unpriced" if incurred_usd is None else format(incurred_usd, "f"),
                _ledger_of(row),
            ),
        )
        conn.execute(
            "UPDATE memecoin_cases SET stage = 'entry_unreconciled', snapshot_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(fresh, sort_keys=True), moment, row["id"]),
        )
        _log(conn, row["id"], row["mint"], "executor", "kill", proven.get("reason"), "entry_receipt", {"signature": signature, "holds": True, "proceeds": None}, moment)
        return {
            "ok": False,
            "recorded": True,
            "reason": proven.get("reason"),
            "case_id": row["id"],
            "stage": "entry_unreconciled",
            "status": "held",
            "holds": True,
            "resend": False,
            "qty": None,
            "proceeds": None,
            "deposit_lamports": None,
            "network_fee_lamports": fee,
            "wallet_spent_lamports": spent,
            "realized_pnl": fresh["realized_pnl"],
            "pnl_live": True,
            "entry_halt": True,
            "accounts": proven.get("accounts"),
            "reserved_usd": fresh.get("reserved_usd"),
            "send": False,
            "sign": False,
            "live_ready": False,
        }

    return db.exclusive(work)


def _failed_balances_changed(decoded, meta: dict, fee: int) -> bool:
    """A reverted transaction is reconciled only when the wallet paid exactly the fee and every other account is unchanged."""
    import memecoin_exit
    deltas = memecoin_exit._account_deltas(decoded, meta)
    if deltas is None:
        return True
    for item in deltas:
        if item["index"] == 0:
            if item["delta"] != -fee:
                return True
            continue
        if item["delta"] != 0:
            return True
    pre = meta.get("preTokenBalances") if isinstance(meta.get("preTokenBalances"), list) else []
    post = meta.get("postTokenBalances") if isinstance(meta.get("postTokenBalances"), list) else []
    return pre != post


def _duplicate_exit(signature: str) -> dict | None:
    _ensure()
    rows = db.query("SELECT case_id FROM memecoin_exit_receipts WHERE signature = ?", (signature,))
    if not rows:
        return None
    return db.exclusive(lambda conn: _duplicate_conn(conn, signature, rows[0]["case_id"]))


def _duplicate_conn(conn, signature: str, case_id: str) -> dict:
    row = conn.execute("SELECT stage, snapshot_json FROM memecoin_cases WHERE id = ?", (case_id,)).fetchone()
    snap = {} if row is None else json.loads(row["snapshot_json"])
    remaining = snap.get("remaining_raw")
    holds = (isinstance(remaining, int) and remaining > 0) or snap.get("deposit_refund_proven") is not True
    return {
        "ok": False,
        "reason": "exit_duplicate",
        "already_recorded": True,
        "recorded": False,
        "holds": holds,
        "resend": False,
        "remaining_raw": remaining,
        "reserved_usd": snap.get("reserved_usd"),
        "stage": None if row is None else row["stage"],
        "case_id": case_id,
        "status": "duplicate",
        "signature": signature,
        "send": False,
        "live_ready": False,
    }


def _hold(row, reason: str, *, status: str, holds: bool = True, **extra) -> dict:
    moment = time.time()

    def work(conn):
        return _hold_conn(conn, row, reason, status=status, moment=moment, holds=holds, **extra)

    return db.exclusive(work)


def _hold_conn(conn, row, reason: str, *, status: str, moment: float, stage_now: str | None = None, holds: bool = True, **extra) -> dict:
    current = conn.execute("SELECT stage, snapshot_json FROM memecoin_cases WHERE id = ?", (row["id"],)).fetchone()
    snap = {} if current is None else json.loads(current["snapshot_json"])
    stage = current["stage"] if stage_now is None and current is not None else stage_now
    _log(conn, row["id"], row["mint"], "risk", "kill", reason, None, {"reason": reason, "holds": holds, "resend": False, "status": status}, moment)
    return {
        "ok": False,
        "reason": reason,
        "holds": holds,
        "resend": False,
        "recorded": False,
        "remaining_raw": snap.get("remaining_raw"),
        "reserved_usd": snap.get("reserved_usd"),
        "case_id": row["id"],
        "stage": stage if stage is not None else (None if current is None else current["stage"]),
        "mint": row["mint"],
        "status": status,
        "send": False,
        "live_ready": False,
        **{key: value for key, value in extra.items() if value is not None},
    }


def loss_totals() -> dict:
    """Paper projections and confirmed chain losses. The $20 halt reads only the live figure."""
    def read(conn):
        live = _loss_conn(conn)
        paper = _paper_loss_conn(conn)
        return {
            "live_loss_usd": format(live.quantize(Decimal("0.01")), "f"),
            "paper_loss_usd": format(paper.quantize(Decimal("0.01")), "f"),
            "live_includes_paper": False,
            "limit_usd": format(MAX_LOSS, "f"),
            "live_halts_new_entries": live >= MAX_LOSS,
        }
    _ensure()
    return db.exclusive(read)


def _loss_conn(conn) -> Decimal:
    """Confirmed chain losses only. A paper projection is not in this sum."""
    total = Decimal("0")
    for item in conn.execute("SELECT stage, snapshot_json, ledger, ledger_provenance FROM memecoin_cases"):
        if item["ledger"] != "live" or item["ledger_provenance"] != "server":
            continue
        body = json.loads(item["snapshot_json"])
        funding = body.get("entry_funding") if isinstance(body.get("entry_funding"), dict) else {}
        signature = funding.get("signature")
        if not isinstance(signature, str):
            continue
        if item["stage"] == "entry_failed":
            proof = conn.execute("SELECT failed, ledger, ledger_provenance FROM memecoin_entry_receipts WHERE signature = ?", (signature,)).fetchone()
            if proof is None or proof["failed"] != 1 or proof["ledger"] != "live" or proof["ledger_provenance"] != "server":
                continue
        elif body.get("pnl_live") is True:
            proof = conn.execute("SELECT failed, ledger, ledger_provenance FROM memecoin_entry_receipts WHERE signature = ?", (signature,)).fetchone()
            if proof is None or proof["failed"] == 1 or proof["ledger"] != "live" or proof["ledger_provenance"] != "server":
                continue
        else:
            continue
        realized = _num(body.get("realized_pnl"))
        if realized is not None and realized < 0:
            total -= realized
    for refund in conn.execute("SELECT fee_usd, ledger, ledger_provenance FROM memecoin_refunds"):
        if refund["ledger"] != "live" or refund["ledger_provenance"] != "server":
            continue
        fee = _num(refund["fee_usd"])
        if fee is not None and fee > 0:
            total += fee
    return total


def _paper_loss_conn(conn) -> Decimal:
    total = Decimal("0")
    for item in conn.execute("SELECT snapshot_json FROM memecoin_cases WHERE stage = 'paper_exited'"):
        body = json.loads(item["snapshot_json"])
        projected = _num((body.get("paper_exit") or {}).get("paper_realized_pnl"))
        if projected is not None and projected < 0:
            total -= projected
    return total


def _halted_conn(conn) -> bool:
    item = conn.execute("SELECT value FROM phantom_control WHERE key = 'halt'").fetchone()
    if item is None:
        return True
    return item["value"] != "0"


def _case(case_id: str):
    _ensure()
    rows = db.query("SELECT * FROM memecoin_cases WHERE id = ?", (case_id,))
    return rows[0] if rows else None


def _snapshot(row) -> dict:
    try:
        body = json.loads(row["snapshot_json"])
    except (TypeError, json.JSONDecodeError):
        return {}
    return body if isinstance(body, dict) else {}


def _deposit(canonical_pool, quote) -> Decimal | None:
    if not isinstance(canonical_pool, (bytes, bytearray)) or not isinstance(quote, dict):
        return None
    if quote.get("usable") is not True or quote.get("origin") not in {"test_harness", "hermes"}:
        return None
    import jupiter_route_v2
    import usdc_valuation
    sol_micro = jupiter_route_v2.sol_micro_from_pool(canonical_pool)
    if sol_micro is None:
        return None
    micro = jupiter_route_v2.lamports_to_micro_usdc(1_488_440 * 2, sol_micro)
    return None if micro is None else usdc_valuation.usd_ceiling(micro, quote)


def _notional(canonical_pool, quote) -> Decimal | None:
    deposit = _deposit(canonical_pool, quote)
    if deposit is None or deposit >= TICKET:
        return None
    return TICKET - deposit


def _log(conn, case_id, mint, stage: str, verdict: str, reason, source, evidence, moment: float) -> None:
    cursor = conn.execute(
        "INSERT INTO memecoin_log (ts, case_id, mint, stage, verdict, reason, source, evidence_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (moment, case_id, mint, stage, verdict, reason, source, json.dumps(evidence, sort_keys=True)),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("log_failed")


def _ensure() -> None:
    db.exclusive(_ensure_conn)


def _ensure_conn(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memecoin_cases (
            id TEXT PRIMARY KEY,
            mint TEXT NOT NULL,
            stage TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memecoin_risk_intents (
            id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL UNIQUE,
            ledger TEXT NOT NULL,
            entry_signature TEXT NOT NULL,
            action TEXT NOT NULL,
            pool TEXT NOT NULL,
            slot INTEGER NOT NULL,
            mark_price_usd TEXT NOT NULL,
            entry_price_usd TEXT NOT NULL,
            stop_price_usd TEXT NOT NULL,
            qty INTEGER NOT NULL,
            basis_cost_lamports INTEGER NOT NULL,
            basis_decimals INTEGER NOT NULL,
            evidence_json TEXT NOT NULL,
            message_sha256 TEXT,
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memecoin_refunds (
            signature TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            rent_lamports INTEGER NOT NULL,
            network_fee_lamports INTEGER NOT NULL,
            fee_usd TEXT NOT NULL,
            released INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memecoin_entry_receipts (
            signature TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            message_sha256 TEXT NOT NULL,
            slot INTEGER NOT NULL,
            qty INTEGER NOT NULL,
            cost_basis_lamports INTEGER NOT NULL,
            network_fee_lamports INTEGER NOT NULL,
            dex_fee_lamports INTEGER NOT NULL,
            fee_usd TEXT NOT NULL,
            failed INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memecoin_exit_receipts (
            signature TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            message_sha256 TEXT NOT NULL,
            slot INTEGER NOT NULL,
            fee_usd TEXT NOT NULL,
            snapshot_slot INTEGER,
            provenance_json TEXT
        )
        """
    )
    for statement in (
        "ALTER TABLE memecoin_exit_receipts ADD COLUMN snapshot_slot INTEGER",
        "ALTER TABLE memecoin_exit_receipts ADD COLUMN provenance_json TEXT",
        "ALTER TABLE memecoin_exit_receipts ADD COLUMN qty INTEGER",
        "ALTER TABLE memecoin_cases ADD COLUMN ledger TEXT",
        "ALTER TABLE memecoin_entry_receipts ADD COLUMN ledger TEXT",
        "ALTER TABLE memecoin_exit_receipts ADD COLUMN ledger TEXT",
        "ALTER TABLE memecoin_refunds ADD COLUMN ledger TEXT",
        "ALTER TABLE memecoin_entry_receipts ADD COLUMN pool TEXT",
        "ALTER TABLE memecoin_cases ADD COLUMN ledger_provenance TEXT",
        "ALTER TABLE memecoin_entry_receipts ADD COLUMN ledger_provenance TEXT",
        "ALTER TABLE memecoin_exit_receipts ADD COLUMN ledger_provenance TEXT",
        "ALTER TABLE memecoin_refunds ADD COLUMN ledger_provenance TEXT",
        "ALTER TABLE memecoin_risk_intents ADD COLUMN ledger_provenance TEXT",
    ):
        try:
            conn.execute(statement)
        except Exception:
            pass
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memecoin_deposits (
            case_id TEXT NOT NULL,
            account TEXT NOT NULL,
            kind TEXT NOT NULL,
            rent_lamports INTEGER,
            funder TEXT,
            provenance TEXT NOT NULL,
            preexisting INTEGER NOT NULL,
            bot_funded INTEGER,
            refund_signature TEXT,
            refunded INTEGER NOT NULL,
            PRIMARY KEY (case_id, kind)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memecoin_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            case_id TEXT,
            mint TEXT,
            stage TEXT NOT NULL,
            verdict TEXT NOT NULL,
            reason TEXT,
            source TEXT,
            evidence_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memecoin_reservations (
            message_sha256 TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            side TEXT NOT NULL,
            message_b64 TEXT NOT NULL,
            router TEXT NOT NULL,
            blockhash TEXT NOT NULL,
            last_valid_block_height INTEGER,
            last_valid_provenance TEXT,
            expire_at INTEGER,
            pool TEXT,
            intent_id TEXT,
            request_id TEXT,
            ledger TEXT NOT NULL,
            ledger_provenance TEXT NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memecoin_preflight_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_sha256 TEXT NOT NULL,
            case_id TEXT NOT NULL,
            side TEXT NOT NULL,
            blockhash TEXT NOT NULL,
            context_slot INTEGER NOT NULL,
            err_clear INTEGER NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute("DROP INDEX IF EXISTS memecoin_reservations_case_side")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memecoin_reservation_signatures (
            signature TEXT PRIMARY KEY,
            message_sha256 TEXT NOT NULL,
            case_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )
    _install_lineage_triggers(conn)
    _classify_ledgers(conn)


_LINEAGE_TABLES = ("memecoin_entry_receipts", "memecoin_exit_receipts", "memecoin_refunds", "memecoin_risk_intents")
_LINEAGE_TRIGGER_REVISION = "14"


def _replace_lineage_triggers(conn) -> None:
    """Drop and recreate lineage triggers on this connection. The caller commits both."""
    for table in _LINEAGE_TABLES:
        conn.execute(f"DROP TRIGGER IF EXISTS {table}_ledger")
        conn.execute(
            f"""
            CREATE TRIGGER {table}_ledger
            BEFORE INSERT ON {table}
            FOR EACH ROW
            WHEN NEW.ledger IS NULL OR NEW.ledger NOT IN ('live', 'synthetic', 'paper')
              OR NEW.ledger_provenance IS NULL OR NEW.ledger_provenance NOT IN ('server', 'legacy_default')
            BEGIN
                SELECT RAISE(ABORT, 'ledger_provenance');
            END
            """
        )
        conn.execute(f"DROP TRIGGER IF EXISTS {table}_provenance_update")
        conn.execute(
            f"""
            CREATE TRIGGER {table}_provenance_update
            BEFORE UPDATE OF ledger, ledger_provenance, case_id ON {table}
            FOR EACH ROW
            WHEN (OLD.ledger_provenance IS NOT NULL AND (NEW.ledger_provenance IS NOT OLD.ledger_provenance OR NEW.ledger IS NOT OLD.ledger OR NEW.case_id IS NOT OLD.case_id))
              OR (NEW.ledger_provenance IS NOT NULL AND NEW.ledger_provenance NOT IN ('server', 'legacy_default'))
            BEGIN
                SELECT RAISE(ABORT, 'ledger_tamper');
            END
            """
        )
    conn.execute("DROP TRIGGER IF EXISTS memecoin_cases_lineage")
    conn.execute(
        """
        CREATE TRIGGER memecoin_cases_lineage
        BEFORE UPDATE OF ledger, ledger_provenance ON memecoin_cases
        FOR EACH ROW
        WHEN (OLD.ledger_provenance IS NOT NULL AND (NEW.ledger_provenance IS NOT OLD.ledger_provenance OR NEW.ledger IS NOT OLD.ledger))
          OR (NEW.ledger_provenance IS NOT NULL AND NEW.ledger_provenance NOT IN ('server', 'legacy_default'))
        BEGIN
            SELECT RAISE(ABORT, 'ledger_tamper');
        END
        """
    )


_IDENTITY_FIELDS = (
    "case_id", "side", "message_b64", "router", "blockhash",
    "last_valid_block_height", "last_valid_provenance", "expire_at",
    "pool", "intent_id", "ledger", "ledger_provenance",
)


def _identity_agreement(conn, proof) -> str:
    """The frozen first proof must still be the reservation row."""
    row = conn.execute(
        "SELECT * FROM memecoin_reservation_identity WHERE message_sha256 = ?",
        (proof["message_sha256"],),
    ).fetchone()
    if row is None:
        return "missing"
    for field in _IDENTITY_FIELDS:
        if row[field] != proof[field]:
            return "mismatch"
    return "match"


def _open_reservation_count(conn, case_id: str, side: str) -> int:
    rows = conn.execute(
        "SELECT * FROM memecoin_reservations WHERE case_id = ? AND side = ?",
        (case_id, side),
    ).fetchall()
    return sum(1 for item in rows if _reservation_status(conn, item) in {"active", "unknown"})


def _reservation_status(conn, proof) -> str:
    """A live server receipt of this side and message is the only thing that ends a live reservation."""
    case = conn.execute(
        "SELECT ledger, ledger_provenance FROM memecoin_cases WHERE id = ?",
        (proof["case_id"],),
    ).fetchone()
    if case is None or case["ledger"] != "live" or case["ledger_provenance"] != "server":
        return "active"
    if proof["ledger"] != "live" or proof["ledger_provenance"] != "server":
        return "active"
    if proof["side"] == "entry":
        rows = conn.execute(
            "SELECT failed, ledger, ledger_provenance FROM memecoin_entry_receipts WHERE case_id = ? AND message_sha256 = ?",
            (proof["case_id"], proof["message_sha256"]),
        ).fetchall()
    elif proof["side"] == "exit":
        rows = conn.execute(
            "SELECT 0 AS failed, ledger, ledger_provenance FROM memecoin_exit_receipts WHERE case_id = ? AND message_sha256 = ?",
            (proof["case_id"], proof["message_sha256"]),
        ).fetchall()
    else:
        return "active"
    live = [item for item in rows if item["ledger"] == "live" and item["ledger_provenance"] == "server"]
    if any(item["failed"] == 0 for item in live):
        return "consumed"
    if any(item["failed"] != 0 for item in live):
        return "failed"
    note = conn.execute(
        "SELECT kind FROM memecoin_reservation_signatures WHERE message_sha256 = ? AND case_id = ?",
        (proof["message_sha256"], proof["case_id"]),
    ).fetchone()
    if note is not None and note["kind"] == "unknown":
        return "unknown"
    return "active"


def _same_reservation(existing, *, case_id: str, side: str, digest: str, chain, pool) -> bool:
    if existing["case_id"] != case_id or existing["side"] != side or existing["message_sha256"] != digest:
        return False
    if existing["blockhash"] != (None if not isinstance(chain, dict) else chain.get("blockhash")):
        return False
    if existing["router"] != (None if not isinstance(chain, dict) else chain.get("router")):
        return False
    if existing["pool"] != pool:
        return False
    if existing["last_valid_block_height"] != (None if not isinstance(chain, dict) else chain.get("last_valid_block_height")):
        return False
    if existing["expire_at"] != (None if not isinstance(chain, dict) else chain.get("expire_at")):
        return False
    return True


def _note_unknown_signature(conn, case_id: str, message_sha256: str, signature: str, moment: float) -> None:
    if not isinstance(signature, str) or signature == "" or not isinstance(message_sha256, str):
        return
    conn.execute(
        "INSERT OR IGNORE INTO memecoin_reservation_signatures (signature, message_sha256, case_id, kind, created_at) VALUES (?, ?, ?, 'unknown', ?)",
        (signature, message_sha256, case_id, moment),
    )


def _store_reservation(conn, *, case_id: str, side: str, message_b64: str, digest: str, chain, pool, intent_id, ledger: str, moment: float) -> None:
    """Write the proof once. The snapshot copy is not this record."""
    if not isinstance(chain, dict) or chain.get("bind_ok") is not True:
        return
    conn.execute(
        """
        INSERT INTO memecoin_reservations (
            message_sha256, case_id, side, message_b64, router, blockhash,
            last_valid_block_height, last_valid_provenance, expire_at, pool, intent_id, request_id,
            ledger, ledger_provenance, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'server', ?)
        """,
        (
            digest, case_id, side, message_b64, chain.get("router"), chain.get("blockhash"),
            chain.get("last_valid_block_height"), chain.get("last_valid_provenance"), chain.get("expire_at"),
            pool, intent_id, chain.get("request_id"), ledger, moment,
        ),
    )


def _repair_active_slots(conn) -> None:
    """Rebuild the open-slot projection from proofs and validated receipts.

    The slot is not a second copy of the proof. Zero open proofs release it,
    one open proof stores that hash, and several stay ambiguous. An unknown
    signature counts as open. A paper receipt and the snapshot do not.
    """
    rows = list(conn.execute("SELECT * FROM memecoin_reservations"))
    grouped = {}
    for row in rows:
        grouped.setdefault((row["case_id"], row["side"]), []).append(row)
    keys = set(grouped)
    for table in ("memecoin_reservation_active", "memecoin_reservation_active_pin"):
        for row in conn.execute(f"SELECT case_id, side FROM {table}"):
            keys.add((row["case_id"], row["side"]))
    desired = {}
    for case_id, side in keys:
        open_rows = [item for item in grouped.get((case_id, side), []) if _reservation_status(conn, item) in {"active", "unknown"}]
        if len(open_rows) == 1:
            desired[(case_id, side)] = open_rows[0]["message_sha256"]
        elif len(open_rows) > 1:
            desired[(case_id, side)] = "ambiguous"
    conn.execute("DELETE FROM memecoin_reservation_active")
    conn.execute("DELETE FROM memecoin_reservation_active_pin")
    for (case_id, side), message in desired.items():
        conn.execute(
            "INSERT INTO memecoin_reservation_active (case_id, side, message_sha256) VALUES (?, ?, ?)",
            (case_id, side, message),
        )
        conn.execute(
            "INSERT INTO memecoin_reservation_active_pin (case_id, side, message_sha256) VALUES (?, ?, ?)",
            (case_id, side, message),
        )


def _replace_reservation_triggers(conn) -> None:
    """The first proof for a message hash sticks. A later insert of that hash cannot replace it."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memecoin_reservation_identity (
            message_sha256 TEXT PRIMARY KEY,
            case_id TEXT NOT NULL,
            side TEXT NOT NULL,
            message_b64 TEXT NOT NULL,
            router TEXT NOT NULL,
            blockhash TEXT NOT NULL,
            last_valid_block_height INTEGER,
            last_valid_provenance TEXT,
            expire_at INTEGER,
            pool TEXT,
            intent_id TEXT,
            ledger TEXT NOT NULL,
            ledger_provenance TEXT NOT NULL
        )
        """
    )
    identity_columns = {row[1] for row in conn.execute("PRAGMA table_info(memecoin_reservation_identity)")}
    if "last_valid_provenance" not in identity_columns:
        conn.execute("ALTER TABLE memecoin_reservation_identity ADD COLUMN last_valid_provenance TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memecoin_reservation_anchor (
            message_sha256 TEXT PRIMARY KEY
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memecoin_reservation_pin (
            message_sha256 TEXT PRIMARY KEY
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memecoin_reservation_active (
            case_id TEXT NOT NULL,
            side TEXT NOT NULL,
            message_sha256 TEXT NOT NULL,
            PRIMARY KEY (case_id, side)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memecoin_reservation_active_pin (
            case_id TEXT NOT NULL,
            side TEXT NOT NULL,
            message_sha256 TEXT NOT NULL,
            PRIMARY KEY (case_id, side)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO memecoin_reservation_identity (
            message_sha256, case_id, side, message_b64, router, blockhash,
            last_valid_block_height, last_valid_provenance, expire_at, pool, intent_id, ledger, ledger_provenance
        )
        SELECT message_sha256, case_id, side, message_b64, router, blockhash,
               last_valid_block_height, last_valid_provenance, expire_at, pool, intent_id, ledger, ledger_provenance
        FROM memecoin_reservations
        WHERE message_sha256 NOT IN (SELECT message_sha256 FROM memecoin_reservation_identity)
        """
    )
    conn.execute("DROP TRIGGER IF EXISTS memecoin_reservation_identity_update")
    conn.execute(
        """
        UPDATE memecoin_reservation_identity
        SET last_valid_provenance = (
            SELECT last_valid_provenance FROM memecoin_reservations
            WHERE memecoin_reservations.message_sha256 = memecoin_reservation_identity.message_sha256
        )
        WHERE last_valid_provenance IS NULL
          AND message_sha256 IN (
            SELECT message_sha256 FROM memecoin_reservations
            WHERE last_valid_provenance IS NOT NULL
          )
        """
    )
    conn.execute(
        """
        INSERT INTO memecoin_reservation_anchor (message_sha256)
        SELECT message_sha256 FROM memecoin_reservation_identity
        WHERE message_sha256 NOT IN (SELECT message_sha256 FROM memecoin_reservation_anchor)
        """
    )
    conn.execute(
        """
        INSERT INTO memecoin_reservation_pin (message_sha256)
        SELECT message_sha256 FROM memecoin_reservation_anchor
        WHERE message_sha256 NOT IN (SELECT message_sha256 FROM memecoin_reservation_pin)
        """
    )
    conn.execute("DROP TRIGGER IF EXISTS memecoin_reservations_proof")
    conn.execute(
        """
        CREATE TRIGGER memecoin_reservations_proof
        BEFORE INSERT ON memecoin_reservations
        FOR EACH ROW
        WHEN NEW.ledger NOT IN ('live', 'paper')
          OR NEW.ledger_provenance != 'server'
          OR NEW.side NOT IN ('entry', 'exit')
          OR NEW.router NOT IN ('metis', 'dflow', 'okx', 'jupiterz')
          OR NEW.message_sha256 IS NULL OR NEW.message_b64 IS NULL OR NEW.blockhash IS NULL OR NEW.case_id IS NULL
          OR (NEW.router IN ('metis', 'dflow', 'okx') AND (NEW.last_valid_block_height IS NULL OR NEW.last_valid_block_height <= 0))
          OR (NEW.router = 'jupiterz' AND (NEW.expire_at IS NULL OR NEW.expire_at <= 0))
        BEGIN
            SELECT RAISE(ABORT, 'reservation_proof');
        END
        """
    )
    conn.execute("DROP TRIGGER IF EXISTS memecoin_reservations_active_guard")
    conn.execute(
        """
        CREATE TRIGGER memecoin_reservations_active_guard
        BEFORE INSERT ON memecoin_reservations
        FOR EACH ROW
        WHEN EXISTS (
            SELECT 1 FROM memecoin_reservation_active
            WHERE case_id = NEW.case_id AND side = NEW.side
        )
        BEGIN
            SELECT RAISE(ABORT, 'reservation_active');
        END
        """
    )
    conn.execute("DROP TRIGGER IF EXISTS memecoin_reservations_identity_guard")
    conn.execute(
        """
        CREATE TRIGGER memecoin_reservations_identity_guard
        BEFORE INSERT ON memecoin_reservations
        FOR EACH ROW
        WHEN EXISTS (
            SELECT 1 FROM memecoin_reservation_identity
            WHERE message_sha256 = NEW.message_sha256
        )
        BEGIN
            SELECT RAISE(ABORT, 'reservation_immutable');
        END
        """
    )
    conn.execute("DROP TRIGGER IF EXISTS memecoin_reservation_identity_guard")
    conn.execute(
        """
        CREATE TRIGGER memecoin_reservation_identity_guard
        BEFORE INSERT ON memecoin_reservation_identity
        FOR EACH ROW
        WHEN EXISTS (
            SELECT 1 FROM memecoin_reservation_anchor
            WHERE message_sha256 = NEW.message_sha256
        )
        OR EXISTS (
            SELECT 1 FROM memecoin_reservations
            WHERE message_sha256 = NEW.message_sha256
              AND (
                case_id IS NOT NEW.case_id OR side IS NOT NEW.side OR message_b64 IS NOT NEW.message_b64
                OR router IS NOT NEW.router OR blockhash IS NOT NEW.blockhash
                OR last_valid_block_height IS NOT NEW.last_valid_block_height
                OR last_valid_provenance IS NOT NEW.last_valid_provenance
                OR expire_at IS NOT NEW.expire_at OR pool IS NOT NEW.pool OR intent_id IS NOT NEW.intent_id
                OR ledger IS NOT NEW.ledger OR ledger_provenance IS NOT NEW.ledger_provenance
              )
        )
        BEGIN
            SELECT RAISE(ABORT, 'reservation_immutable');
        END
        """
    )
    conn.execute("DROP TRIGGER IF EXISTS memecoin_reservation_anchor_guard")
    conn.execute(
        """
        CREATE TRIGGER memecoin_reservation_anchor_guard
        BEFORE INSERT ON memecoin_reservation_anchor
        FOR EACH ROW
        WHEN EXISTS (
            SELECT 1 FROM memecoin_reservation_pin
            WHERE message_sha256 = NEW.message_sha256
        )
        BEGIN
            SELECT RAISE(ABORT, 'reservation_immutable');
        END
        """
    )
    conn.execute("DROP TRIGGER IF EXISTS memecoin_reservations_identity_record")
    conn.execute(
        """
        CREATE TRIGGER memecoin_reservations_identity_record
        AFTER INSERT ON memecoin_reservations
        FOR EACH ROW
        BEGIN
            INSERT INTO memecoin_reservation_identity (
                message_sha256, case_id, side, message_b64, router, blockhash,
                last_valid_block_height, last_valid_provenance, expire_at, pool, intent_id, ledger, ledger_provenance
            ) VALUES (
                NEW.message_sha256, NEW.case_id, NEW.side, NEW.message_b64, NEW.router, NEW.blockhash,
                NEW.last_valid_block_height, NEW.last_valid_provenance, NEW.expire_at, NEW.pool, NEW.intent_id,
                NEW.ledger, NEW.ledger_provenance
            );
            INSERT INTO memecoin_reservation_anchor (message_sha256) VALUES (NEW.message_sha256);
            INSERT INTO memecoin_reservation_pin (message_sha256) VALUES (NEW.message_sha256);
            INSERT INTO memecoin_reservation_active (case_id, side, message_sha256)
            VALUES (NEW.case_id, NEW.side, NEW.message_sha256);
            INSERT INTO memecoin_reservation_active_pin (case_id, side, message_sha256)
            VALUES (NEW.case_id, NEW.side, NEW.message_sha256);
        END
        """
    )
    conn.execute("DROP TRIGGER IF EXISTS memecoin_reservation_identity_update")
    conn.execute(
        """
        CREATE TRIGGER memecoin_reservation_identity_update
        BEFORE UPDATE ON memecoin_reservation_identity
        BEGIN
            SELECT RAISE(ABORT, 'reservation_immutable');
        END
        """
    )
    conn.execute("DROP TRIGGER IF EXISTS memecoin_reservation_identity_delete")
    conn.execute(
        """
        CREATE TRIGGER memecoin_reservation_identity_delete
        BEFORE DELETE ON memecoin_reservation_identity
        BEGIN
            SELECT RAISE(ABORT, 'reservation_immutable');
        END
        """
    )
    for name, table in (
        ("memecoin_reservation_anchor_update", "memecoin_reservation_anchor"),
        ("memecoin_reservation_anchor_delete", "memecoin_reservation_anchor"),
        ("memecoin_reservation_pin_update", "memecoin_reservation_pin"),
        ("memecoin_reservation_pin_delete", "memecoin_reservation_pin"),
        ("memecoin_reservation_active_update", "memecoin_reservation_active"),
        ("memecoin_reservation_active_pin_update", "memecoin_reservation_active_pin"),
    ):
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        kind = "UPDATE" if name.endswith("_update") else "DELETE"
        conn.execute(
            f"""
            CREATE TRIGGER {name}
            BEFORE {kind} ON {table}
            BEGIN
                SELECT RAISE(ABORT, 'reservation_immutable');
            END
            """
        )
    for name in (
        "memecoin_reservation_active_delete",
        "memecoin_reservation_active_insert",
        "memecoin_reservation_active_pin_delete",
        "memecoin_reservation_active_pin_guard",
        "memecoin_entry_receipts_release_active",
        "memecoin_exit_receipts_release_active",
    ):
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    _repair_active_slots(conn)
    conn.execute(
        """
        CREATE TRIGGER memecoin_reservation_active_insert
        BEFORE INSERT ON memecoin_reservation_active
        FOR EACH ROW
        WHEN EXISTS (
            SELECT 1 FROM memecoin_reservation_active_pin
            WHERE case_id = NEW.case_id AND side = NEW.side
        )
        BEGIN
            SELECT RAISE(ABORT, 'reservation_active');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER memecoin_reservation_active_pin_guard
        BEFORE INSERT ON memecoin_reservation_active_pin
        FOR EACH ROW
        WHEN EXISTS (
            SELECT 1 FROM memecoin_reservation_active_pin
            WHERE case_id = NEW.case_id AND side = NEW.side
        )
        OR EXISTS (
            SELECT 1 FROM memecoin_reservation_active
            WHERE case_id = NEW.case_id AND side = NEW.side
              AND message_sha256 IS NOT NEW.message_sha256
        )
        BEGIN
            SELECT RAISE(ABORT, 'reservation_active');
        END
        """
    )
    for table in ("memecoin_reservation_active", "memecoin_reservation_active_pin"):
        trigger = "memecoin_reservation_active_delete" if table.endswith("active") else "memecoin_reservation_active_pin_delete"
        conn.execute(
            f"""
            CREATE TRIGGER {trigger}
            BEFORE DELETE ON {table}
            FOR EACH ROW
            WHEN NOT EXISTS (
                SELECT 1 FROM memecoin_entry_receipts e
                JOIN memecoin_reservations r ON r.message_sha256 = OLD.message_sha256
                JOIN memecoin_cases c ON c.id = OLD.case_id
                WHERE OLD.side = 'entry' AND e.case_id = OLD.case_id AND e.message_sha256 = OLD.message_sha256
                  AND e.ledger = 'live' AND e.ledger_provenance = 'server'
                  AND r.ledger = 'live' AND r.ledger_provenance = 'server'
                  AND c.ledger = 'live' AND c.ledger_provenance = 'server'
            ) AND NOT EXISTS (
                SELECT 1 FROM memecoin_exit_receipts e
                JOIN memecoin_reservations r ON r.message_sha256 = OLD.message_sha256
                JOIN memecoin_cases c ON c.id = OLD.case_id
                WHERE OLD.side = 'exit' AND e.case_id = OLD.case_id AND e.message_sha256 = OLD.message_sha256
                  AND e.ledger = 'live' AND e.ledger_provenance = 'server'
                  AND r.ledger = 'live' AND r.ledger_provenance = 'server'
                  AND c.ledger = 'live' AND c.ledger_provenance = 'server'
            )
            BEGIN
                SELECT RAISE(ABORT, 'reservation_active');
            END
            """
        )
    conn.execute(
        """
        CREATE TRIGGER memecoin_entry_receipts_release_active
        AFTER INSERT ON memecoin_entry_receipts
        FOR EACH ROW
        WHEN NEW.ledger = 'live' AND NEW.ledger_provenance = 'server'
        BEGIN
            DELETE FROM memecoin_reservation_active
            WHERE case_id = NEW.case_id AND side = 'entry' AND message_sha256 = NEW.message_sha256;
            DELETE FROM memecoin_reservation_active_pin
            WHERE case_id = NEW.case_id AND side = 'entry' AND message_sha256 = NEW.message_sha256;
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER memecoin_exit_receipts_release_active
        AFTER INSERT ON memecoin_exit_receipts
        FOR EACH ROW
        WHEN NEW.ledger = 'live' AND NEW.ledger_provenance = 'server'
        BEGIN
            DELETE FROM memecoin_reservation_active
            WHERE case_id = NEW.case_id AND side = 'exit' AND message_sha256 = NEW.message_sha256;
            DELETE FROM memecoin_reservation_active_pin
            WHERE case_id = NEW.case_id AND side = 'exit' AND message_sha256 = NEW.message_sha256;
        END
        """
    )
    conn.execute("DROP TRIGGER IF EXISTS memecoin_reservations_immutable")
    conn.execute(
        """
        CREATE TRIGGER memecoin_reservations_immutable
        BEFORE UPDATE ON memecoin_reservations
        BEGIN
            SELECT RAISE(ABORT, 'reservation_immutable');
        END
        """
    )
    conn.execute("DROP TRIGGER IF EXISTS memecoin_reservations_delete")
    conn.execute(
        """
        CREATE TRIGGER memecoin_reservations_delete
        BEFORE DELETE ON memecoin_reservations
        BEGIN
            SELECT RAISE(ABORT, 'reservation_immutable');
        END
        """
    )
    conn.execute("DROP TRIGGER IF EXISTS memecoin_reservation_signatures_insert")
    conn.execute(
        """
        CREATE TRIGGER memecoin_reservation_signatures_insert
        BEFORE INSERT ON memecoin_reservation_signatures
        FOR EACH ROW
        WHEN NEW.kind != 'unknown' OR NEW.signature IS NULL OR NEW.message_sha256 IS NULL OR NEW.case_id IS NULL
        BEGIN
            SELECT RAISE(ABORT, 'reservation_signature');
        END
        """
    )
    conn.execute("DROP TRIGGER IF EXISTS memecoin_reservation_signatures_update")
    conn.execute(
        """
        CREATE TRIGGER memecoin_reservation_signatures_update
        BEFORE UPDATE ON memecoin_reservation_signatures
        BEGIN
            SELECT RAISE(ABORT, 'reservation_immutable');
        END
        """
    )
    conn.execute("DROP TRIGGER IF EXISTS memecoin_reservation_signatures_delete")
    conn.execute(
        """
        CREATE TRIGGER memecoin_reservation_signatures_delete
        BEFORE DELETE ON memecoin_reservation_signatures
        BEGIN
            SELECT RAISE(ABORT, 'reservation_immutable');
        END
        """
    )
    _install_preflight_audit(conn)


def _install_preflight_audit(conn) -> None:
    """An attempt is bound to the reservation that was simulated. It cannot be retargeted."""
    conn.execute("DROP TRIGGER IF EXISTS memecoin_preflight_attempts_insert")
    conn.execute(
        """
        CREATE TRIGGER memecoin_preflight_attempts_insert
        BEFORE INSERT ON memecoin_preflight_attempts
        FOR EACH ROW
        WHEN NOT EXISTS (
            SELECT 1 FROM memecoin_reservations
            WHERE message_sha256 = NEW.message_sha256
              AND case_id = NEW.case_id
              AND side = NEW.side
              AND blockhash = NEW.blockhash
        )
        OR NEW.context_slot IS NULL OR NEW.context_slot <= 0
        OR NEW.err_clear NOT IN (0, 1)
        BEGIN
            SELECT RAISE(ABORT, 'preflight_unbound');
        END
        """
    )
    conn.execute("DROP TRIGGER IF EXISTS memecoin_preflight_attempts_update")
    conn.execute(
        """
        CREATE TRIGGER memecoin_preflight_attempts_update
        BEFORE UPDATE ON memecoin_preflight_attempts
        BEGIN
            SELECT RAISE(ABORT, 'preflight_unbound');
        END
        """
    )
    conn.execute("DROP TRIGGER IF EXISTS memecoin_preflight_attempts_delete")
    conn.execute(
        """
        CREATE TRIGGER memecoin_preflight_attempts_delete
        BEFORE DELETE ON memecoin_preflight_attempts
        BEGIN
            SELECT RAISE(ABORT, 'preflight_unbound');
        END
        """
    )


def _install_lineage_triggers(conn) -> None:
    """Replace predecessor trigger definitions and record the revision in one transaction."""
    _replace_lineage_triggers(conn)
    _replace_reservation_triggers(conn)
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ('memecoin_lineage_trigger', ?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (_LINEAGE_TRIGGER_REVISION, time.time()),
    )


def _legacy_live_default(conn, table: str) -> bool:
    try:
        info = list(conn.execute(f"PRAGMA table_info({table})"))
    except Exception:
        return False
    for column in info:
        name = column[1]
        default = column[4]
        if name == "ledger" and default is not None and "live" in str(default).lower():
            return True
    return False


def _classify_ledgers(conn) -> None:
    """Stamp rows that never received server provenance. A named ledger without provenance stays quarantined."""
    global _LEGACY_LEDGER_DEFAULT
    tables = ("memecoin_cases", "memecoin_entry_receipts", "memecoin_exit_receipts", "memecoin_refunds")
    legacy = any(_legacy_live_default(conn, table) for table in tables)
    _LEGACY_LEDGER_DEFAULT = legacy
    seen = conn.execute("SELECT value FROM settings WHERE key = 'memecoin_ledger_migration'").fetchone()
    if seen is not None and seen[0] == "13":
        return
    for table in tables:
        if _legacy_live_default(conn, table):
            conn.execute(
                f"UPDATE {table} SET ledger_provenance = 'legacy_default' WHERE ledger_provenance IS NULL"
            )
        else:
            conn.execute(
                f"UPDATE {table} SET ledger_provenance = 'legacy_default' WHERE ledger_provenance IS NULL AND ledger IS NOT NULL"
            )
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ('memecoin_ledger_migration', '13', ?) ON CONFLICT(key) DO UPDATE SET value = '13', updated_at = excluded.updated_at",
        (time.time(),),
    )


def _money(value) -> Decimal | None:
    number = _num(value)
    if number is None or not number.is_finite() or number <= 0:
        return None
    return number


def _num(value) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    return number
