"""Owner approval of one exact exit rule set. It is not LIVE consent.

The approved numbers are the stated exit triggers for the $100 allocation.
They are not a guaranteed loss limit and not an aggregate or daily drawdown
policy. Changing a threshold does not keep the approval, and this module
cannot change PAPER, the kill switch, or the cap.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal

import db

_KEY = "exit_policy_approval"
_RULES = {
    "stop_pct_from_entry": "6",
    "trail_arm_profit_pct": "12",
    "trail_drop_from_peak_pct": "12",
    "timeout_h": "3",
    "timeout_min_profit_pct": "5",
    "allocation_usd": "100.00",
    "ticket_usd": "8.00",
}
_SETTINGS = (
    ("stop_pct", "6"),
    ("trail_start_pct", "12"),
    ("trail_pct", "12"),
    ("timeout_h", "3"),
    ("timeout_min_gain_pct", "5"),
)
_STATEMENT = (
    "I approve the stated exit rules for the $100 trading allocation, "
    "but do not enable LIVE yet."
)


def view() -> dict:
    row = _load()
    matches = _settings_match()
    approved = bool(row and row.get("approved") is True and row.get("rules") == _RULES and row.get("revoked") is False)
    return {
        "approved": approved,
        "effective": approved and matches,
        "matches_settings": matches,
        "rules": dict(_RULES) if approved else None,
        "revoked": bool(row and row.get("revoked") is True),
        "live_consent": False,
        "aggregate_loss_policy": False,
        "guaranteed_loss_limit": False,
        "loss_policy": "not_approved",
    }


def set_approval(approved: bool) -> dict:
    if approved and not _settings_match():
        raise ValueError("thresholds do not match the approved exit rules")
    db.execute("DELETE FROM phantom_control WHERE key = 'live_trading_consent'")
    payload = {
        "approved": approved,
        "revoked": not approved,
        "rules": dict(_RULES),
        "statement": _STATEMENT,
        "live_consent": False,
        "aggregate_loss_policy": False,
        "guaranteed_loss_limit": False,
        "at": round(time.time(), 3),
    }
    db.execute(
        """
        INSERT INTO phantom_control (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (_KEY, json.dumps(payload, separators=(",", ":"), sort_keys=True), time.time()),
    )
    return view()


def _settings_match() -> bool:
    import phantom_runner
    if phantom_runner.BANKROLL != Decimal("100.00") or phantom_runner.SIZE != Decimal("8.00"):
        return False
    for key, expected in _SETTINGS:
        if not _same(db.get_setting(key), expected):
            return False
    return True


def _same(stored, expected: str) -> bool:
    try:
        left = Decimal(str(stored))
        right = Decimal(expected)
    except Exception:
        return False
    return left == right


def _load() -> dict | None:
    rows = db.query("SELECT value FROM phantom_control WHERE key = ?", (_KEY,))
    if not rows:
        return None
    try:
        body = json.loads(rows[0]["value"])
    except json.JSONDecodeError:
        return None
    if not isinstance(body, dict):
        return None
    rules = body.get("rules")
    if rules != _RULES:
        return {"approved": False, "revoked": True, "rules": None}
    if body.get("live_consent") is True or body.get("aggregate_loss_policy") is True or body.get("guaranteed_loss_limit") is True:
        return {"approved": False, "revoked": True, "rules": None}
    return body
