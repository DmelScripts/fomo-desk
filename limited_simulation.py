"""Manual, synthetic SOL/USDC rehearsal. No network, signer, or agent startup.

All inputs except the retry identifier are server-owned. Controls and the
PAPER reservation run in the same transaction, including resumed requests.
"""

from __future__ import annotations

import json
import os
import re
import time
from decimal import Decimal

from fastapi import HTTPException

import db
import phantom_runner
import phantom_transport
import risk_limits
import solana_tx

_PREFIX = "sol-usdc-sim-"
_ID = re.compile(r"[A-Za-z0-9_-]{1,67}\Z")
_LIMITS = {"max_total_loss_usd": "10.00", "max_round_trip_fee_usd": "0.20"}
_ASSUMPTIONS = {
    "market": "SOL/USDC",
    "ticket_usd": "8.00",
    "sol_price_usd": "100.00",
    "entry_price": "1.00",
    "exit_price": "1.00",
    "round_trip_fee_usd": "0.20",
    "slippage_percent": "1.00",
    "price_source": "synthetic_prices",
    "fee_source": "synthetic_assumption",
}


def _labels() -> dict:
    return {
        "labels": ["simulation", "synthetic_prices", "no_real_orders"],
        "mode": "simulation",
        "price_source": "synthetic_prices",
        "order_type": "no_real_orders",
        "assumptions": dict(_ASSUMPTIONS),
        "send": False,
        "signer_called": False,
        "live_ready": False,
        "executable": False,
        "network_used": False,
        "agents_started": False,
    }


def _setting(conn, key):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _controls(conn) -> tuple[dict, bool, str | None]:
    policy = risk_limits.view(conn=conn)
    row = conn.execute("SELECT value FROM phantom_control WHERE key='halt'").fetchone()
    halted = row is None or row["value"] != "0"
    if _setting(conn, "mode") != "PAPER":
        reason = "paper_mode_required"
    elif _setting(conn, "agents_enabled") != "0" or os.environ.get("DESK_AGENTS") == "1":
        reason = "agents_must_be_off"
    elif phantom_runner.SIZE != Decimal("8.00") or phantom_runner.BANKROLL != Decimal("100.00") or phantom_runner.USDC != solana_tx.USDC_MINT:
        reason = "simulation_scope_changed"
    elif not policy["approved"]:
        reason = policy["approval_state"]
    elif policy["limits"] != _LIMITS:
        reason = "selected_limits_required"
    elif not policy["ledger_valid"]:
        reason = "ledger_invalid"
    elif halted:
        reason = "simulation_paused"
    else:
        reason = None
    return policy, halted, reason


def _decorate(result: dict, intent_id: str) -> dict:
    return {**result, **_labels(), "intent_id": intent_id}


def _matches_scope(row) -> bool:
    expected = {
        "wallet_id": phantom_transport.REQUIRED_WALLET_ID,
        "wallet": phantom_transport.REQUIRED_WALLET,
        "chain": "solana:mainnet", "amount_usd": "8.00", "fee_usd": "0.20",
        "fx_sol_usd": "100.00",
    }
    pair = phantom_runner._pair({"chain": "solana:mainnet", "buy_mint": solana_tx.USDC_MINT})
    return (
        all(row[key] == value for key, value in expected.items())
        and row["pair_json"] == pair and row["slippage"] == 1
        and all(row[key] in (None, "1.00") for key in ("entry_price", "exit_price"))
    )


def status() -> dict:
    def work(conn):
        policy, halted, reason = _controls(conn)
        if reason is None:
            assessment = risk_limits.assess("0.20", conn=conn)
            if not assessment["allowed"]:
                reason = assessment["reason"]
        row = conn.execute(
            "SELECT * FROM phantom_reservations "
            "WHERE substr(intent_id, 1, ?)=? AND result_json IS NOT NULL "
            "ORDER BY updated_at DESC, intent_id DESC LIMIT 1",
            (len(_PREFIX), _PREFIX),
        ).fetchone()
        latest = None
        if row is not None and _matches_scope(row):
            try:
                result = json.loads(row["result_json"])
                if isinstance(result, dict):
                    latest = _decorate(result, row["intent_id"][len(_PREFIX):])
            except (ValueError, TypeError):
                pass
        return {
            **_labels(), "ready": reason is None, "halted": halted,
            "reason": reason, "risk_limits": policy, "latest_result": latest,
        }

    return db.exclusive(work)


def run(intent_id: str) -> dict:
    """Complete one flat-price synthetic round trip, or replay its saved result."""
    if not isinstance(intent_id, str) or not _ID.fullmatch(intent_id):
        raise HTTPException(status_code=400, detail="intent_id must contain 1-67 letters, digits, underscores or hyphens")
    stored_id = _PREFIX + intent_id
    spec = phantom_runner._spec({
        "intent_id": stored_id,
        "wallet_id": phantom_transport.REQUIRED_WALLET_ID,
        "wallet": phantom_transport.REQUIRED_WALLET,
        "chain": "solana:mainnet",
        "sell_native": True,
        "buy_mint": solana_tx.USDC_MINT,
        "slippage": 1,
        "sol_usd": "100.00",
        "fx_at": time.time(),
        "quote_source": "paper",
        "fee_usd": "0.20",
    })
    spec.update(cycle="round_trip", entry_price="1.00", exit_price="1.00")

    def work(conn):
        _policy, _halted, reason = _controls(conn)
        if reason is not None:
            raise HTTPException(status_code=409, detail=reason)
        row = phantom_runner._row(conn, stored_id)
        if row is not None:
            # The generic PAPER endpoint also accepts retry IDs. A collision
            # must not relabel another rehearsal's inputs as our fixed inputs.
            if not _matches_scope(row):
                raise HTTPException(status_code=409, detail="simulation_intent_conflict")
        result = phantom_runner._drive(conn, spec, phantom_runner.PaperRuntime())
        return _decorate(result, intent_id)

    return db.exclusive(work)
