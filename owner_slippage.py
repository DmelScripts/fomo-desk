"""Owner slippage ceiling from the 8 Nov 2026 16:28 Asia/Saigon decision.

The words were "Set maximum slippage to 0.5% per swap." That is 50 basis
points on each swap, entry and exit. It is not 0.5 basis points and it is not
50 percent. Exits and retries do not get a wider number. A caller, a test
allowance, or an older stored value cannot raise it. This module does not send.
"""

from __future__ import annotations

import json

import db

BPS = 50
PERCENT = "0.5"
STATEMENT = "Set maximum slippage to 0.5% per swap."
APPROVED_AT = "2026-11-08T16:28:00+07:00"
TIMEZONE = "Asia/Saigon"
SOURCE = "owner_decision_2026-11-08T16:28:00+07:00"
SCOPE = "per_swap"
KEY = "memecoin_max_slippage_bps"
VERSION = 1


def target() -> dict:
    """The code target. It is not a production deploy and it does not send."""
    return {
        "bps": BPS,
        "percent": PERCENT,
        "statement": STATEMENT,
        "approved_at": APPROVED_AT,
        "timezone": TIMEZONE,
        "source": SOURCE,
        "scope": SCOPE,
        "applies_to": ["entry", "exit"],
        "wider_for_exit": False,
        "wider_for_retry": False,
        "version": VERSION,
        "record_state": "approved",
        "deployed": False,
        "production": False,
        "live": False,
        "send": False,
        "sign": False,
    }


def persist() -> dict:
    """Re-read the startup record. This does not overwrite a conflict or a revocation."""
    apply_on_startup()
    return configuration()


def apply_on_startup(conn=None) -> dict:
    """Install the approved record on first boot or upgrade. Leave every other row.

    Called from init_db, which server startup runs. The write happens only
    while the schema version is still below 12 and the key is empty. An exact
    row is not rewritten. A revoked, conflicting, or later-deleted row is not
    put back to approved.
    """
    import time
    own = conn is None
    if own:
        conn = db.connect()
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (KEY,)).fetchone()
        raw = None if row is None else row["value"]
        decision = _classify(raw, migrated=version >= 12)
        if decision["action"] == "install":
            payload = json.dumps(target(), sort_keys=True)
            now = time.time()
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (KEY, payload, now),
            )
            conn.execute(
                'INSERT INTO change_log (ts, key, "old", "new", who) VALUES (?, ?, ?, ?, ?)',
                (now, KEY, None, payload, SOURCE),
            )
            decision = decision | {"applied": True, "written": True, "state": "already_applied"}
        else:
            decision = decision | {"written": False}
        if own:
            conn.commit()
        return decision
    finally:
        if own:
            conn.close()


def configuration() -> dict:
    """Code target beside the row startup actually left in the database."""
    raw = _raw_setting()
    decision = _classify(raw, migrated=_migration_done())
    stored_bps = None
    if isinstance(decision.get("body"), dict):
        stored_bps = decision["body"].get("bps")
    return {
        "code_target_bps": BPS,
        "code_target_percent": PERCENT,
        "statement": STATEMENT,
        "approved_at": APPROVED_AT,
        "timezone": TIMEZONE,
        "source": SOURCE,
        "scope": SCOPE,
        "version": VERSION,
        "applies_to": ["entry", "exit"],
        "wider_for_exit": False,
        "wider_for_retry": False,
        "applied": decision["state"] == "already_applied",
        "applied_bps": BPS if decision["state"] == "already_applied" else None,
        "record_state": decision["state"],
        "startup_action": decision["action"],
        "stored_bps": stored_bps,
        "approved_bps": BPS if decision["state"] == "already_applied" else None,
        "approved": decision["state"] == "already_applied",
        "persisted": decision["state"] == "already_applied",
        "owner_slippage_unapproved": decision["state"] != "already_applied",
        "deployed": False,
        "production": False,
        "live": False,
        "live_ready": False,
        "send": False,
        "sign": False,
    }


def ceiling_bps() -> int | None:
    """50 when the stored record is the owner decision. Any other row is absent."""
    try:
        approved = _approved_record()
    except Exception:
        return None
    return BPS if approved is not None else None


def check_route(slippage_bps, min_out, amm_est_out) -> dict:
    """Reject an instruction above 50 bps or a minimum under that ceiling's floor.

    The required minimum uses the approved 50 basis points and rounds up.
    A smaller declared bps, or a test allowance, cannot lower that minimum.
    """
    refused = {
        "ok": False,
        "ceiling_bps": BPS,
        "owner_ceiling_bps": BPS,
        "send": False,
        "sign": False,
        "live_ready": False,
    }
    if isinstance(slippage_bps, bool) or not isinstance(slippage_bps, int) or slippage_bps < 0:
        return refused | {"reason": "slippage_unapproved"}
    if slippage_bps > BPS:
        return refused | {"reason": "slippage_exceeds", "slippage_bps": slippage_bps}
    if isinstance(amm_est_out, bool) or not isinstance(amm_est_out, int) or amm_est_out <= 0:
        return refused | {"reason": "quote_unbound"}
    if isinstance(min_out, bool) or not isinstance(min_out, int) or min_out <= 0:
        return refused | {"reason": "min_below_amm"}
    floor = minimum_units(amm_est_out, BPS)
    if min_out < floor:
        return refused | {"reason": "min_below_amm", "allowance_floor": floor, "slippage_bps": slippage_bps, "declared_bps_ignored_for_floor": slippage_bps != BPS}
    return {
        "ok": True,
        "reason": None,
        "ceiling_bps": BPS,
        "slippage_bps": slippage_bps,
        "allowance_floor": floor,
        "send": False,
        "sign": False,
        "live_ready": False,
    }


def minimum_units(estimate: int, bps: int = BPS) -> int:
    """Smallest output whose shortfall is at most bps. The division rounds up.

    min_out * 10000 >= estimate * (10000 - bps). For estimate 10001 and 50
    bps that is 9951, not the truncated 9950. Estimate 1 cannot become 0.
    """
    if isinstance(estimate, bool) or isinstance(bps, bool) or not isinstance(estimate, int) or not isinstance(bps, int):
        raise ValueError("slippage floor is not usable")
    if estimate <= 0 or not 0 <= bps <= BPS:
        raise ValueError("slippage floor is not usable")
    return (estimate * (10_000 - bps) + 9_999) // 10_000


def effective_test_allowance(allowance):
    """A test allowance cannot exceed 50. None stays unset. Above 50 is clamped."""
    if allowance is None:
        return None
    if isinstance(allowance, bool) or not isinstance(allowance, int) or allowance < 0 or allowance > 10_000:
        return "unbound"
    if allowance > BPS:
        return BPS
    return allowance


def _migration_done() -> bool:
    try:
        conn = db.connect()
    except Exception:
        return False
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0] >= 12
    except Exception:
        return False
    finally:
        conn.close()


def _classify(raw, migrated: bool = False) -> dict:
    if raw in (None, ""):
        if migrated:
            return {"action": "preserve", "state": "absent_after_migration", "applied": False, "reason": "removed", "body": None}
        return {"action": "install", "state": "first_install", "applied": False, "body": None}
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return {"action": "preserve", "state": "conflict", "applied": False, "reason": "unreadable", "body": None}
    if not isinstance(body, dict):
        return {"action": "preserve", "state": "conflict", "applied": False, "reason": "unreadable", "body": None}
    if body.get("record_state") == "revoked" or body.get("state") == "revoked":
        return {"action": "preserve", "state": "revoked", "applied": False, "reason": "revoked", "body": body}
    if body == target():
        return {"action": "unchanged", "state": "already_applied", "applied": True, "body": body}
    return {"action": "preserve", "state": "conflict", "applied": False, "reason": "conflict", "body": body}


def _raw_setting():
    try:
        return db.get_setting(KEY, "")
    except Exception:
        return ""


def _approved_record() -> dict | None:
    raw = db.get_setting(KEY, "")
    decision = _classify(raw, migrated=_migration_done())
    if decision["state"] != "already_applied":
        return None
    return decision["body"]
