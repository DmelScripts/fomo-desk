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
        taken = conn.execute("SELECT id FROM memecoin_cases WHERE id = ?", (case_id,)).fetchone()
        if taken is not None:
            case_id = mint + ":" + str(int(moment))
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
    if dev > 3:
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
    if bundled > 10:
        return refuse("bundle")
    change = _num(snap.get("price_change_h1"))
    if change is None or change >= 80:
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


def protect(case_id: str) -> dict:
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
        "reserved_usd": format(TICKET, "f"),
        "fee_cap_usd": format(MAX_FEE, "f"),
        "live_ready": False,
        "send": False,
    })
    evidence = {
        "stop_price_usd": snap["stop_price_usd"],
        "trail_arm_price_usd": snap["trail_arm_price_usd"],
        "trail_floor_if_armed_at_threshold": snap["trail_floor_if_armed_at_threshold"],
        "trail_breakeven_guaranteed": False,
    }
    moment = time.time()

    def work(conn):
        current = conn.execute("SELECT stage FROM memecoin_cases WHERE id = ?", (row["id"],)).fetchone()
        if current is None or current["stage"] != "analyst_passed":
            return {"ok": False, "reason": "stage", "case_id": row["id"], "stage": None if current is None else current["stage"]}
        if _halted_conn(conn):
            _log(conn, row["id"], row["mint"], "executor", "kill", "kill_switch", None, {"reason": "kill_switch"}, moment)
            return {"ok": False, "reason": "kill_switch", "case_id": row["id"], "stage": "analyst_passed", "mint": row["mint"]}
        if _loss_conn(conn) >= MAX_LOSS:
            conn.execute("UPDATE memecoin_cases SET stage = 'rejected', updated_at = ? WHERE id = ?", (moment, row["id"]))
            _log(conn, row["id"], row["mint"], "executor", "kill", "stop_new_entries", None, {"reason": "stop_new_entries"}, moment)
            return {"ok": False, "reason": "stop_new_entries", "case_id": row["id"], "stage": "rejected", "mint": row["mint"]}
        if _held_conn(conn) + TICKET > ALLOCATION:
            conn.execute("UPDATE memecoin_cases SET stage = 'rejected', updated_at = ? WHERE id = ?", (moment, row["id"]))
            _log(conn, row["id"], row["mint"], "executor", "kill", "allocation", None, {"reason": "allocation"}, moment)
            return {"ok": False, "reason": "allocation", "case_id": row["id"], "stage": "rejected", "mint": row["mint"]}
        conn.execute(
            "UPDATE memecoin_cases SET stage = 'protected', snapshot_json = ?, updated_at = ? WHERE id = ? AND stage = 'analyst_passed'",
            (json.dumps(snap, sort_keys=True), moment, row["id"]),
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
    """Book one fixture fill. The receipt has to match the fixture route."""
    row = _case(case_id)
    if row is None or row["stage"] != "entry_ready":
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
        "tokens_out": tokens,
        "cost_usd": snap["ticket_usd"],
        "fee_usd": format(fee, "f"),
        "fee_counted_once": True,
    }
    snap["peak_price_usd"] = snap["entry_price_usd"]
    snap["trail_armed"] = False
    return _advance(row, "filled", "executor", snap["fill"], snap)


def mark(case_id: str, price_usd: str, *, source: str, fetched_at: float, now: float | None = None) -> dict:
    """Apply the persisted stop and trail. A new loss total does not block this."""
    moment = time.time() if now is None else now
    row = _case(case_id)
    if row is None or row["stage"] != "filled":
        return _kill(case_id, None, "risk", "stage", None)
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
        proceeds = (TICKET * price / entry)
        exit_fee = _money(snap["route"]["fee_usd"])
        realized = proceeds - TICKET - exit_fee - exit_fee
        snap["exit"] = {"proceeds_usd": format(proceeds, "f"), "fee_usd": format(exit_fee, "f"), "realized_pnl": format(realized, "f"), "fee_counted_once": True}
        snap["realized_pnl"] = format(realized, "f")
        snap["reserved_usd"] = "0.00"
        evidence = evidence | snap["exit"]
    target = "filled" if action == "hold" else "exited"

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


def _advance(row, stage: str, name: str, evidence: dict, snap: dict | None = None) -> dict:
    moment = time.time()
    body = _snapshot(row) if snap is None else snap

    def work(conn):
        current = conn.execute("SELECT stage FROM memecoin_cases WHERE id = ?", (row["id"],)).fetchone()
        if current is None or current["stage"] != row["stage"]:
            return {"ok": False, "reason": "stage", "case_id": row["id"], "stage": None if current is None else current["stage"]}
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


def _held_conn(conn) -> Decimal:
    total = Decimal("0")
    for item in conn.execute("SELECT snapshot_json FROM memecoin_cases WHERE stage IN ('protected', 'entry_ready', 'filled')"):
        total += _money(json.loads(item["snapshot_json"]).get("reserved_usd")) or Decimal("0")
    return total


def _loss_conn(conn) -> Decimal:
    total = Decimal("0")
    for item in conn.execute("SELECT snapshot_json FROM memecoin_cases WHERE stage = 'exited'"):
        realized = _num(json.loads(item["snapshot_json"]).get("realized_pnl"))
        if realized is not None and realized < 0:
            total -= realized
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


def _log(conn, case_id, mint, stage: str, verdict: str, reason, source, evidence, moment: float) -> None:
    conn.execute(
        "INSERT INTO memecoin_log (ts, case_id, mint, stage, verdict, reason, source, evidence_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (moment, case_id, mint, stage, verdict, reason, source, json.dumps(evidence, sort_keys=True)),
    )


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
