"""PAPER runner for a Phantom-shaped order. It does not send.

The $100 figure is an application cap. It is not a wallet policy and it does
not replace the PAPER 200 ledger. Eight percent of that cap is $8. The FOMO
$47.50 ticket bump is not used.
"""

from __future__ import annotations

import json
import os
import time
from decimal import Decimal

from fastapi import HTTPException

import db
import phantom_transport

BANKROLL = Decimal("100.00")
SIZE = Decimal("8.00")
FX_MAX_AGE_S = 120
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
OBSERVED_QUOTE_ID = "e09cf162-9f00-40fa-9c36-0714a7c5fa94"
_OPEN = {"reserved", "quoted", "sim_open", "submit_unknown"}
_DONE = {"sim_closed", "released", "rejected", "halted"}
_KEYS = {
    "intent_id", "wallet_id", "wallet", "chain", "sell_native", "buy_mint",
    "slippage", "sol_usd", "fx_at", "quote_source", "fee_usd",
}
_BLOCKED = {
    "execute", "execution", "signature", "rawtransaction", "transactiondata",
    "transaction", "transactions", "session", "stamper", "privatekey", "secret",
    "broadcast", "signed", "signedtransaction", "tool", "toolargs",
}


def public_status() -> dict:
    return {
        "authority": "PAPER",
        "live_ready": False,
        "send": False,
        "executable": False,
        "transport": phantom_transport.TRANSPORT,
        "auth": phantom_transport.AUTH,
        "package": phantom_transport.PINNED_PACKAGE,
        "cli": phantom_transport.PINNED_CLI,
        "bankroll_usd": "100.00",
        "bankroll_note": "Application control. Not a wallet policy.",
        "size_usd": "8.00",
        "fomo_bump_applies": False,
        "loss_policy": "not_approved",
        "intake": "qualified_signal",
        "signals_connected": True,
        "scheduled_exits_connected": True,
        "signals_follow_existing_filters": True,
        "halted": halted(),
        **allocation(),
        "required_wallet_id": phantom_transport.REQUIRED_WALLET_ID,
        "required_wallet": phantom_transport.REQUIRED_WALLET,
        "alerts": _alerts(),
    }


def halted() -> bool:
    rows = db.query("SELECT value FROM phantom_control WHERE key = 'halt'")
    if not rows:
        return True
    return rows[0]["value"] != "0"


def set_halt(body: dict) -> dict:
    _reject_tree(body)
    if not isinstance(body, dict) or set(body) != {"halted"} or not isinstance(body.get("halted"), bool):
        raise HTTPException(status_code=400, detail="halted must be a boolean")
    _write_halt(body["halted"])
    return public_status()


def run_intent(body: dict, runtime=None, cycle: str = "round_trip", prices: dict | None = None) -> dict:
    _reject_tree(body)
    if not isinstance(body, dict) or set(body) != _KEYS:
        raise HTTPException(status_code=400, detail="unexpected field")
    if db.get_setting("mode", "PAPER") != "PAPER":
        raise HTTPException(status_code=409, detail="LIVE locked")
    intent_id = body.get("intent_id")
    rows = db.query(
        "SELECT status FROM phantom_reservations WHERE intent_id = ?",
        (intent_id,),
    )
    status = rows[0]["status"] if rows else None
    spec = _spec(body, resume=status in {"sim_open", "submit_unknown", "reserved", "quoted"})
    spec["cycle"] = cycle
    spec["entry_price"] = str((prices or {}).get("entry") or "1")
    spec["exit_price"] = str((prices or {}).get("exit") or spec["entry_price"])
    chosen = runtime or _runtime()
    paper = getattr(chosen, "name", "") == "paper_simulation"
    if status in _DONE or status == "submit_unknown":
        return db.exclusive(lambda conn: _drive(conn, spec, chosen))
    if status == "sim_open":
        if not paper:
            _alert("session_halt", phantom_transport.AUTH)
            _write_halt(True)
            return _stopped("sim_open", "exit_blocked_auth")
        return db.exclusive(lambda conn: _drive(conn, spec, chosen))
    if halted():
        return _stopped("halted", "kill_switch")
    if not paper:
        _alert("session_halt", phantom_transport.AUTH)
        _write_halt(True)
        return _stopped("halted", phantom_transport.AUTH)
    return db.exclusive(lambda conn: _drive(conn, spec, chosen))


def reconcile_unknown(body: dict) -> dict:
    _reject_tree(body)
    if not isinstance(body, dict) or set(body) != {"intent_id"}:
        raise HTTPException(status_code=400, detail="unexpected field")
    intent_id = body.get("intent_id")
    if not isinstance(intent_id, str) or not intent_id:
        raise HTTPException(status_code=400, detail="unknown intent")

    def work(conn):
        row = _row(conn, intent_id)
        if row is None:
            return _stopped("missing", "unknown intent")
        if row["status"] != "submit_unknown":
            return _stopped(row["status"], "not_unknown")
        result = _result(row, "released", "reconciled_without_resubmit")
        result["resubmit"] = False
        _save(conn, intent_id, "released", "0.00", row["entry_quote_id"], row["exit_quote_id"], result)
        return result

    return db.exclusive(work)


def _runtime():
    if os.environ.get("DESK_PHANTOM_PAPER") == "1":
        return PaperRuntime()
    return phantom_transport


class PaperRuntime:
    name = "paper_simulation"

    def quote(self, spec: dict) -> dict:
        if spec["quote_source"] == "observed":
            return _observed_quote()
        return {
            "origin": "paper_simulation",
            "quote_id": f"paper-{spec['side']}-{spec['intent_id']}",
            "provider_expiry": None,
            "provider_expiry_unknown": True,
            "validity": "paper_local",
            "paper_valid_until": time.time() + 60,
            "executable": False,
            "sim_fill_allowed": True,
            "wallet_id": spec["wallet_id"],
            "wallet": spec["wallet"],
            "chain": spec["chain"],
            "sell_native": True,
            "buy_mint": spec["buy_mint"],
            "amount_usd": spec["amount_usd"],
            "fee_usd": spec["fee_usd"],
            "slippage": spec["slippage"],
            "entry_price": spec.get("entry_price") or "1",
            "exit_price": spec.get("exit_price") or spec.get("entry_price") or "1",
            "provider": "synthetic_paper",
            "live_capable": False,
        }


def _drive(conn, spec: dict, runtime) -> dict:
    row = _row(conn, spec["intent_id"])
    if row is not None:
        if row["status"] == "submit_unknown":
            result = _result(row, "submit_unknown", "reconcile_before_resubmit")
            result["resubmit"] = False
            return result
        if row["status"] in _DONE and row["result_json"]:
            return json.loads(row["result_json"])
        if row["status"] == "sim_open":
            spec = {
                **spec,
                "amount_usd": row["amount_usd"],
                "fee_usd": row["fee_usd"],
                "reserved": row["reserved_usd"],
                "entry_price": row["entry_price"] or spec.get("entry_price") or "1",
            }
            if spec.get("cycle") == "entry":
                stored = json.loads(row["result_json"]) if row["result_json"] else _result(row, "sim_open", "entered")
                return stored
            return _exit(conn, spec, runtime, row)
        if row["status"] == "reserved":
            return _enter(conn, spec, runtime)
        return _result(row, row["status"], "in_progress")
    if _over(conn, spec["reserved"]):
        result = _result_new(spec, "rejected", "bankroll")
        _insert(conn, spec, "rejected", "0.00", result)
        return result
    _insert(conn, spec, "reserved", spec["reserved"], None)
    return _enter(conn, spec, runtime)


def _enter(conn, spec: dict, runtime) -> dict:
    quote = runtime.quote({**spec, "side": "entry"})
    ok, reason = _gate(quote, spec)
    if not ok:
        result = _result_new(spec, "released", reason)
        result["quote_id"] = quote.get("quote_id")
        result["provider_expiry_unknown"] = bool(quote.get("provider_expiry_unknown"))
        _save(conn, spec["intent_id"], "released", "0.00", quote.get("quote_id"), None, result)
        return result
    _save(conn, spec["intent_id"], "sim_open", spec["reserved"], quote["quote_id"], None, None, entry_price=spec.get("entry_price") or "1", mark_price=spec.get("entry_price") or "1")
    row = _row(conn, spec["intent_id"])
    if spec.get("cycle") == "entry":
        entry_fee, exit_fee = _fee_split(spec["fee_usd"])
        result = _result(row, "sim_open", "entered")
        result["entry_quote_id"] = quote["quote_id"]
        result["entry_fee_usd"] = _text(entry_fee)
        result["exit_fee_usd"] = _text(exit_fee)
        result["entry_price"] = spec.get("entry_price") or "1"
        result["provider"] = "synthetic_paper"
        result["live_capable"] = False
        result["fill"] = None
        _save(conn, spec["intent_id"], "sim_open", spec["reserved"], quote["quote_id"], None, result, entry_price=result["entry_price"], mark_price=result["entry_price"])
        return result
    return _exit(conn, spec, runtime, row)


def _exit(conn, spec: dict, runtime, row) -> dict:
    quote = runtime.quote({**spec, "side": "exit"})
    ok, reason = _gate(quote, spec)
    entry_id = row["entry_quote_id"]
    if not ok or quote.get("quote_id") == entry_id:
        result = _result(row, "sim_open", reason if not ok else "reused_quote")
        result["resubmit"] = False
        _save(conn, spec["intent_id"], "sim_open", spec["reserved"], entry_id, None, result)
        return result
    entry_price = Decimal(spec.get("entry_price") or "1")
    exit_price = Decimal(str(quote.get("exit_price") or spec.get("exit_price") or entry_price))
    entry_fee, exit_fee = _fee_split(spec["fee_usd"])
    notional = Decimal(spec["amount_usd"])
    proceeds = notional * exit_price / entry_price
    realized = proceeds - notional - entry_fee - exit_fee
    result = _result_new(spec, "sim_closed", "simulated_fill")
    result["entry_quote_id"] = entry_id
    result["exit_quote_id"] = quote["quote_id"]
    result["reconciled"] = True
    result["fill"] = "simulated"
    result["provider"] = "synthetic_paper"
    result["live_capable"] = False
    result["entry_price"] = _text(entry_price)
    result["exit_price"] = _text(exit_price)
    result["entry_fee_usd"] = _text(entry_fee)
    result["exit_fee_usd"] = _text(exit_fee)
    result["realized_pnl"] = _text(realized)
    _save(
        conn, spec["intent_id"], "sim_closed", "0.00", entry_id, quote["quote_id"], result,
        realized=_text(realized), entry_price=_text(entry_price), exit_price=_text(exit_price),
        mark_price=_text(exit_price),
    )
    return result


def _gate(quote: dict, spec: dict) -> tuple[bool, str]:
    if quote.get("executable") is True:
        return False, "real_send_blocked"
    if quote.get("quote_id") == OBSERVED_QUOTE_ID or quote.get("origin") == "observed":
        return False, "observed_nonexecutable"
    if quote.get("origin") != "paper_simulation" or quote.get("validity") != "paper_local":
        return False, "not_paper"
    if quote.get("provider_expiry") is not None:
        return False, "invented_provider_expiry"
    until = quote.get("paper_valid_until")
    if not isinstance(until, (int, float)) or isinstance(until, bool) or float(until) <= time.time():
        return False, "paper_validity"
    if quote.get("sim_fill_allowed") is not True:
        return False, "sim_not_allowed"
    same = (
        quote.get("wallet_id") == spec["wallet_id"]
        and quote.get("wallet") == spec["wallet"]
        and quote.get("chain") == spec["chain"]
        and quote.get("sell_native") is True
        and quote.get("buy_mint") == spec["buy_mint"]
        and quote.get("amount_usd") == spec["amount_usd"]
        and quote.get("fee_usd") == spec["fee_usd"]
        and quote.get("slippage") == spec["slippage"]
    )
    if not same:
        return False, "binding"
    return True, "paper_sim"


def _spec(body: dict, resume: bool = False) -> dict:
    intent_id = body.get("intent_id")
    if not isinstance(intent_id, str) or not intent_id or len(intent_id) > 80:
        raise HTTPException(status_code=400, detail="intent id is not usable")
    if body.get("wallet_id") != phantom_transport.REQUIRED_WALLET_ID:
        raise HTTPException(status_code=400, detail="wallet id is not bound")
    if body.get("wallet") != phantom_transport.REQUIRED_WALLET:
        raise HTTPException(status_code=400, detail="wallet address is not bound")
    if body.get("chain") != "solana:mainnet" or body.get("sell_native") is not True:
        raise HTTPException(status_code=400, detail="pair is not bound")
    if body.get("buy_mint") != USDC:
        raise HTTPException(status_code=400, detail="pair is not bound")
    if body.get("quote_source") not in {"paper", "observed"}:
        raise HTTPException(status_code=400, detail="quote source is not usable")
    slippage = body.get("slippage")
    if isinstance(slippage, bool) or not isinstance(slippage, (int, float)) or not 0 <= float(slippage) <= 100:
        raise HTTPException(status_code=400, detail="slippage is not usable")
    sol_usd = _money(body.get("sol_usd"), "fx")
    fx_at = body.get("fx_at")
    now = time.time()
    fx_ok = not isinstance(fx_at, bool) and isinstance(fx_at, (int, float)) and now - FX_MAX_AGE_S <= float(fx_at) <= now + 5
    if not resume and not fx_ok:
        raise HTTPException(status_code=400, detail="fx quote is missing or stale")
    if resume and (isinstance(fx_at, bool) or not isinstance(fx_at, (int, float))):
        fx_at = now
    fee = _money(body.get("fee_usd"), "fee")
    reserved = SIZE + fee
    return {
        "intent_id": intent_id,
        "wallet_id": phantom_transport.REQUIRED_WALLET_ID,
        "wallet": phantom_transport.REQUIRED_WALLET,
        "chain": "solana:mainnet",
        "sell_native": True,
        "buy_mint": USDC,
        "quote_source": body["quote_source"],
        "slippage": float(slippage),
        "sol_usd": format(sol_usd, "f"),
        "fx_at": float(fx_at),
        "amount_usd": format(SIZE, "f"),
        "fee_usd": format(fee, "f"),
        "reserved": format(reserved, "f"),
    }


def _money(value, label: str) -> Decimal:
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail=label + " quote is missing or stale")
    try:
        number = Decimal(value)
    except Exception:
        raise HTTPException(status_code=400, detail=label + " quote is missing or stale")
    if not number.is_finite() or number <= 0:
        raise HTTPException(status_code=400, detail=label + " quote is missing or stale")
    return number


def _over(conn, reserved: str) -> bool:
    return _charges(conn) + Decimal(reserved) > BANKROLL


def _charges(conn) -> Decimal:
    rows = conn.execute(
        "SELECT status, reserved_usd, realized_pnl FROM phantom_reservations"
    ).fetchall()
    total = Decimal("0")
    for row in rows:
        if row["status"] in _OPEN:
            total += Decimal(row["reserved_usd"] or "0")
        elif row["status"] == "sim_closed" and row["realized_pnl"] is not None:
            total -= Decimal(row["realized_pnl"])
    return total


def allocation() -> dict:
    rows = db.query("SELECT status, reserved_usd, realized_pnl, amount_usd, entry_price, mark_price FROM phantom_reservations")
    realized = Decimal("0")
    open_hold = Decimal("0")
    unrealized = Decimal("0")
    for row in rows:
        if row["status"] in _OPEN:
            open_hold += Decimal(row["reserved_usd"] or "0")
            if row["entry_price"] and row["mark_price"]:
                unrealized += Decimal(row["amount_usd"] or "0") * (Decimal(row["mark_price"]) / Decimal(row["entry_price"]) - 1)
        elif row["status"] == "sim_closed" and row["realized_pnl"] is not None:
            realized += Decimal(row["realized_pnl"])
    available = BANKROLL + realized - open_hold
    return {
        "available_usd": _text(available),
        "realized_pnl": _text(realized),
        "open_hold_usd": _text(open_hold),
        "unrealized_pnl": _text(unrealized),
        "unrealized_spendable": False,
    }


def mark_open(intent_id: str, mark_price: str) -> dict:
    price = _money(mark_price, "mark")
    db.execute(
        "UPDATE phantom_reservations SET mark_price = ? WHERE intent_id = ? AND status = 'sim_open'",
        (_text(price), intent_id),
    )
    return allocation()


def on_qualified_signal(payload: dict) -> dict:
    """A buy the existing executor already qualified. This does not loosen filters."""
    try:
        if not isinstance(payload, dict) or payload.get("qualified") is not True:
            return _stopped("skipped", "not_qualified")
        if db.get_setting("mode", "PAPER") != "PAPER":
            return _stopped("skipped", "not_paper")
        mint = payload.get("mint")
        price = payload.get("price")
        if not isinstance(mint, str) or not mint:
            return _stopped("rejected", "not_qualified")
        if isinstance(price, bool) or not isinstance(price, (int, float)) or float(price) <= 0:
            return _stopped("rejected", "not_qualified")
        if not isinstance(payload.get("sol_usd"), str) or isinstance(payload.get("fx_at"), bool) or not isinstance(payload.get("fx_at"), (int, float)):
            return _stopped("rejected", "fx quote is missing or stale")
        fee = payload.get("phantom_fee_usd") if isinstance(payload.get("phantom_fee_usd"), str) else "0.20"
        body = {
            "intent_id": "sig-" + mint,
            "wallet_id": phantom_transport.REQUIRED_WALLET_ID,
            "wallet": phantom_transport.REQUIRED_WALLET,
            "chain": "solana:mainnet",
            "sell_native": True,
            "buy_mint": USDC,
            "slippage": 1,
            "sol_usd": payload["sol_usd"],
            "fx_at": payload["fx_at"],
            "quote_source": "paper",
            "fee_usd": fee,
        }
        text = _text(Decimal(str(price)))
        return run_intent(body, cycle="entry", prices={"entry": text, "exit": text})
    except HTTPException as exc:
        return _stopped("rejected", str(exc.detail))
    except Exception:
        return _stopped("rejected", "phantom_entry_failed")


def on_scheduled_exit(outcome: dict) -> dict:
    """A PAPER exit the risk agent already decided. This does not invent a stop."""
    try:
        if not isinstance(outcome, dict) or str(outcome.get("mode") or "").upper() != "PAPER":
            return _stopped("skipped", "not_paper")
        if outcome.get("status") != "closed":
            return _stopped("skipped", "not_an_exit")
        mint = outcome.get("mint")
        if not isinstance(mint, str) or not mint:
            return _stopped("skipped", "not_an_exit")
        intent_id = "sig-" + mint
        rows = db.query("SELECT * FROM phantom_reservations WHERE intent_id = ?", (intent_id,))
        if not rows or rows[0]["status"] != "sim_open":
            return _stopped("no_phantom_position", "no open allocation")
        row = rows[0]
        exit_price = outcome.get("exitPrice", outcome.get("exit_price"))
        if isinstance(exit_price, bool) or not isinstance(exit_price, (int, float, str)) or Decimal(str(exit_price)) <= 0:
            return _stopped("sim_open", "exit price missing")
        body = {
            "intent_id": intent_id,
            "wallet_id": row["wallet_id"],
            "wallet": row["wallet"],
            "chain": row["chain"],
            "sell_native": True,
            "buy_mint": USDC,
            "slippage": row["slippage"],
            "sol_usd": row["fx_sol_usd"],
            "fx_at": time.time(),
            "quote_source": "paper",
            "fee_usd": row["fee_usd"],
        }
        return run_intent(body, cycle="exit", prices={"entry": row["entry_price"] or "1", "exit": str(exit_price)})
    except HTTPException as exc:
        return _stopped("sim_open", str(exc.detail))
    except Exception:
        return _stopped("sim_open", "phantom_exit_failed")


def _observed_quote() -> dict:
    return {
        "origin": "observed",
        "quote_id": OBSERVED_QUOTE_ID,
        "provider_expiry": None,
        "provider_expiry_unknown": True,
        "executable": False,
        "sim_fill_allowed": False,
        "sell_amount_base": "10000000",
        "buy_amount_base": "891179",
        "slippage": 1,
        "fees_present": True,
    }


def _insert(conn, spec: dict, status: str, reserved: str, result) -> None:
    now = time.time()
    conn.execute(
        """
        INSERT INTO phantom_reservations (
            intent_id, status, wallet_id, wallet, chain, pair_json, amount_usd, fee_usd,
            reserved_usd, slippage, entry_quote_id, exit_quote_id, fx_sol_usd, fx_at,
            result_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?)
        """,
        (
            spec["intent_id"], status, spec["wallet_id"], spec["wallet"], spec["chain"],
            _pair(spec), spec["amount_usd"], spec["fee_usd"], reserved, spec["slippage"],
            spec["sol_usd"], spec["fx_at"], json.dumps(result) if result else None, now, now,
        ),
    )


def _save(conn, intent_id: str, status: str, reserved: str, entry, exit_id, result, realized=None, entry_price=None, exit_price=None, mark_price=None) -> None:
    conn.execute(
        """
        UPDATE phantom_reservations
        SET status = ?, reserved_usd = ?, entry_quote_id = ?, exit_quote_id = ?,
            result_json = ?, realized_pnl = COALESCE(?, realized_pnl),
            entry_price = COALESCE(?, entry_price), exit_price = COALESCE(?, exit_price),
            mark_price = COALESCE(?, mark_price), updated_at = ?
        WHERE intent_id = ?
        """,
        (
            status, reserved, entry, exit_id,
            json.dumps(result) if result is not None else None, realized,
            entry_price, exit_price, mark_price, time.time(), intent_id,
        ),
    )


def _fee_split(total: str) -> tuple[Decimal, Decimal]:
    fee = Decimal(total)
    entry_fee = (fee / 2).quantize(Decimal("0.01"))
    return entry_fee, fee - entry_fee


def _text(number: Decimal) -> str:
    return format(number.quantize(Decimal("0.01")), "f")


def _row(conn, intent_id: str):
    return conn.execute(
        "SELECT * FROM phantom_reservations WHERE intent_id = ?",
        (intent_id,),
    ).fetchone()


def _pair(spec: dict) -> str:
    return json.dumps(
        {"sell_native": True, "buy_mint": spec["buy_mint"], "chain": spec["chain"]},
        sort_keys=True,
        separators=(",", ":"),
    )


def _result_new(spec: dict, status: str, reason: str) -> dict:
    return _base(status, reason) | {
        "intent_id": spec["intent_id"],
        "amount_usd": spec["amount_usd"],
        "fee_usd": spec["fee_usd"],
        "wallet": spec["wallet"],
        "wallet_id": spec["wallet_id"],
    }


def _result(row, status: str, reason: str) -> dict:
    return _base(status, reason) | {
        "intent_id": row["intent_id"],
        "amount_usd": row["amount_usd"],
        "fee_usd": row["fee_usd"],
        "wallet": row["wallet"],
        "wallet_id": row["wallet_id"],
        "entry_quote_id": row["entry_quote_id"],
        "exit_quote_id": row["exit_quote_id"],
    }


def _base(status: str, reason: str) -> dict:
    return {
        "status": status,
        "reason": reason,
        "send": False,
        "executable": False,
        "live_ready": False,
        "transport": phantom_transport.TRANSPORT,
        "authority": "PAPER",
        "fomo_bump_applies": False,
        "resubmit": False,
    }


def _stopped(status: str, reason: str) -> dict:
    return _base(status, reason)


def _write_halt(halted_now: bool) -> None:
    db.execute(
        """
        INSERT INTO phantom_control (key, value, updated_at) VALUES ('halt', ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        ("1" if halted_now else "0", time.time()),
    )


def _alert(kind: str, detail: str) -> None:
    db.execute(
        "INSERT INTO phantom_alerts (ts, kind, detail) VALUES (?, ?, ?)",
        (time.time(), kind, detail[:200]),
    )


def _alerts() -> list[dict]:
    rows = db.query("SELECT ts, kind, detail FROM phantom_alerts ORDER BY id DESC LIMIT 5")
    return [{"ts": row["ts"], "kind": row["kind"], "detail": row["detail"]} for row in rows]


def _reject_tree(value) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "").replace("_", "")
            if normalized in _BLOCKED:
                raise HTTPException(status_code=400, detail="execution fields are refused")
            _reject_tree(item)
    elif isinstance(value, list):
        for item in value:
            _reject_tree(item)
