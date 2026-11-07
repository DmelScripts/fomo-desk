"""Plan A JSON API. APP_TOKEN is required on every /api route.

The token is read from Authorization: Bearer. The URL fragment is not sent
to the server; the app will attach the header later. A token in the query
string is rejected so it is not treated as a credential.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import math
import os
import time
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

import links
from agents.executor import paper_balance, set_mode
from bus import covered_price_mints, hub
from config import SECRET_KEYS, SETTING_COPY, SETTING_GROUPS, SETTING_RULES, load_secrets, report_boundary
from db import AGENT_NAMES, agents_enabled, get_float, get_setting, query, set_setting

router = APIRouter()

# STEP 4: api.py polls prices every 60s. That number is not a separate STEP 10 key.
API_PRICE_POLL_S = 60
BALANCE_POLL_S = 60
DEX_URL = "https://api.dexscreener.com/tokens/v1/solana/"
_paused: set[str] = set()


def paused_agents() -> set[str]:
    return set(_paused)


def is_paused(name: str) -> bool:
    """Whether a loop should skip new work.

    Risk keeps guarding while any position is open, even if a pause was
    stored earlier. The API also refuses that pause.
    """
    if name == "risk" and query("SELECT id FROM positions WHERE closed_at IS NULL"):
        return False
    return name in _paused


def _unauthorized() -> None:
    raise HTTPException(status_code=401, detail="unauthorized")


def _unavailable() -> None:
    raise HTTPException(status_code=503, detail="unavailable")


def require_app_token(request: Request) -> None:
    """Fail closed. The same response is used for a bad client token.

    A missing server token is 503 so the process is not left open. The body
    does not name the secret or echo the header.
    """
    leaked = ("t", "token", "app_token", "APP_TOKEN")
    if any(key in request.query_params for key in leaked):
        _unauthorized()
    expected = load_secrets().get("APP_TOKEN")
    if not expected:
        _unavailable()
    header = request.headers.get("authorization") or ""
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not presented or presented != presented.strip():
        _unauthorized()
    try:
        match = hmac.compare_digest(presented, expected)
    except (TypeError, ValueError):
        match = False
    if not match:
        _unauthorized()


def _night_start(now: float | None = None) -> float:
    """Start of the current night. report_time is Asia/Ho_Chi_Minh, not UTC."""
    now = time.time() if now is None else now
    clock = get_setting("report_time", "08:00") or "08:00"
    return report_boundary(now, clock, upcoming=False)


def _balance_before(ts: float) -> float:
    """PAPER stack plus closes strictly before ts. No invented fills."""
    start = get_float("paper_start_stack")
    rows = query(
        """
        SELECT COALESCE(SUM(pnl_usd), 0) AS n FROM positions
        WHERE mode = 'PAPER' AND closed_at IS NOT NULL AND closed_at < ?
        """,
        (ts,),
    )
    return start + float(rows[0]["n"] or 0)


def api_price_mints(now: float | None = None) -> list[str]:
    """Coins pushed, audited, or opened tonight that Scanner and Risk do not own.

    Open positions always belong to Risk, even when that loop is not running.
    Scanner's waiting room is whatever it last claimed. One list, one batch.
    """
    start = _night_start(now)
    rows = query(
        """
        SELECT mint FROM signals WHERE ts >= ?
        UNION
        SELECT mint FROM audits WHERE ts >= ?
        UNION
        SELECT mint FROM positions WHERE opened_at >= ?
        """,
        (start, start, start),
    )
    owned = covered_price_mints()
    open_rows = query("SELECT mint FROM positions WHERE closed_at IS NULL")
    owned.update(row["mint"] for row in open_rows if row["mint"])
    found = []
    for row in rows:
        mint = row["mint"]
        if mint and mint not in owned and mint not in found:
            found.append(mint)
    return found


def write_price_batch(payload: Any, now: float | None = None) -> int:
    """Write one price row per mint from a single DexScreener response."""
    import fields

    now = time.time() if now is None else now
    pairs = payload.get("pairs") if isinstance(payload, dict) else payload
    if not isinstance(pairs, list):
        pairs = []
    by_mint: dict[str, dict] = {}
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        mint = fields.get(pair, "baseToken.address")
        if not mint:
            continue
        chosen = fields.dexscreener_select_pair([item for item in pairs if fields.get(item, "baseToken.address") == mint])
        if chosen is not None:
            by_mint[mint] = chosen
    count = 0
    for mint, pair in by_mint.items():
        price = fields.get(pair, fields.DEXSCREENER["priceUsd"])
        volume = fields.get(pair, fields.DEXSCREENER["volume.m5"])
        try:
            price_n = float(price)
        except (TypeError, ValueError):
            continue
        volume_n = None
        try:
            if volume is not None:
                volume_n = float(volume)
        except (TypeError, ValueError):
            volume_n = None
        from db import execute
        execute(
            "INSERT INTO prices (mint, ts, price, volume_m5) VALUES (?, ?, ?, ?)",
            (mint, now, price_n, volume_n),
        )
        count += 1
    return count


def record_balance_point(now: float | None = None) -> dict:
    """Snapshot the simulated PAPER balance. This does not create a trade."""
    now = time.time() if now is None else now
    value = paper_balance()
    from db import execute
    execute("INSERT INTO balance (ts, value) VALUES (?, ?)", (now, value))
    point = {"ts": now, "value": value}
    hub.publish("balance", point)
    return point


async def price_loop(stop: asyncio.Event, fetch=None) -> None:
    """One DexScreener batch per tick, only for mints this writer owns."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=API_PRICE_POLL_S)
            return
        except asyncio.TimeoutError:
            pass
        mints = api_price_mints()
        if mints and fetch is not None:
            payload = await fetch(DEX_URL + ",".join(mints))
            write_price_batch(payload)
        record_balance_point()


def _coin_url(mint: str | None) -> str | None:
    if not mint:
        return None
    return links.coin_url(mint, "solana")


def _agent_count(name: str, since: float) -> int:
    """Tonight's count. report_time is the Vietnam night boundary."""
    if name == "scanner":
        return query("SELECT COUNT(*) AS n FROM scans WHERE ts >= ?", (since,))[0]["n"]
    if name == "auditor":
        return query("SELECT COUNT(DISTINCT mint) AS n FROM audits WHERE ts >= ?", (since,))[0]["n"]
    if name == "analyst":
        return query("SELECT COUNT(*) AS n FROM verdicts WHERE ts >= ?", (since,))[0]["n"]
    if name == "executor":
        return query("SELECT COUNT(*) AS n FROM signals WHERE ts >= ?", (since,))[0]["n"]
    return query(
        """
        SELECT COUNT(*) AS n FROM positions
        WHERE exit_reason IS NOT NULL AND closed_at IS NOT NULL AND closed_at >= ?
        """,
        (since,),
    )[0]["n"]


def _coin_label(name: str | None, symbol: str | None, mint: str | None) -> str | None:
    if symbol:
        return symbol
    if name:
        return name
    return mint


def _cards() -> list[dict]:
    enabled = agents_enabled()
    since = _night_start()
    open_positions = query("SELECT id FROM positions WHERE closed_at IS NULL")
    cards = []
    for name in AGENT_NAMES:
        count = int(_agent_count(name, since))
        paused = is_paused(name)
        if name == "scanner" and count == 0:
            line = "Listening for graduations"
        elif name == "auditor" and count == 0:
            line = "No coin taken apart yet"
        elif name == "risk" and open_positions:
            line = "Guarding"
        else:
            line = None
        card = {
            "name": name,
            "running": bool(enabled and not paused),
            "paused": paused,
            "count": count,
            "line": line,
        }
        if name == "scanner":
            rows = query(
                """
                SELECT s.ts, s.mint, s.verdict, s.reason, s.liquidity_usd, c.name, c.symbol
                FROM scans s LEFT JOIN coins c ON c.mint = s.mint
                WHERE s.ts >= ?
                ORDER BY s.ts DESC LIMIT 5
                """,
                (since,),
            )
            card["feed"] = [
                {
                    "ts": row["ts"],
                    "mint": row["mint"],
                    "name": _coin_label(row["name"], row["symbol"], row["mint"]),
                    "verdict": row["verdict"],
                    "reason": row["reason"],
                    "liquidity_usd": row["liquidity_usd"],
                    "coin_url": _coin_url(row["mint"]),
                }
                for row in rows
            ]
        elif name == "auditor":
            rejected = query(
                """
                SELECT COUNT(DISTINCT mint) AS n FROM audits
                WHERE ts >= ? AND "pass" = 0 AND value_text != 'not checked'
                """,
                (since,),
            )[0]["n"]
            card["rejected"] = int(rejected or 0)
            latest = query("SELECT mint, ts FROM audits WHERE ts >= ? ORDER BY ts DESC LIMIT 1", (since,))
            layers = []
            if latest:
                layer_rows = query(
                    """
                    SELECT layer, name, value_text, "pass" AS ok FROM audits
                    WHERE mint = ? AND ts = ? ORDER BY layer
                    """,
                    (latest[0]["mint"], latest[0]["ts"]),
                )
                for row in layer_rows:
                    layers.append({
                        "n": int(row["layer"]) if str(row["layer"]).isdigit() else row["layer"],
                        "name": row["name"],
                        "pass": None if row["ok"] is None else bool(row["ok"]),
                        "value": row["value_text"],
                    })
                reds = [layer for layer in layers if layer["pass"] is False and layer["value"] != "not checked"]
                if layers and not reds and len(layers) == 8:
                    card["line"] = "8/8 → Executor"
                elif reds:
                    card["line"] = reds[0]["value"]
            card["layers"] = layers
        elif name == "analyst":
            rows = query(
                'SELECT score, "pass" AS ok FROM verdicts WHERE ts >= ? ORDER BY ts DESC LIMIT 1',
                (since,),
            )
            card["score"] = rows[0]["score"] if rows else None
            card["min_score"] = get_float("min_score")
        elif name == "executor":
            card["stack_pct"] = get_float("stack_pct")
            rows = query(
                "SELECT size_usd, stack_pct, status FROM signals WHERE ts >= ? ORDER BY ts DESC LIMIT 1",
                (since,),
            )
            card["last_size_usd"] = rows[0]["size_usd"] if rows else None
            card["last_stack_pct"] = rows[0]["stack_pct"] if rows else None
            card["last_status"] = rows[0]["status"] if rows else None
        elif name == "risk":
            card["stop_pct"] = get_float("stop_pct")
            card["trail_pct"] = get_float("trail_pct")
            card["pause_locked"] = bool(open_positions)
            if not open_positions:
                last = query(
                    """
                    SELECT kind, subtitle FROM activity
                    WHERE agent = 'risk' AND ts >= ? ORDER BY ts DESC LIMIT 1
                    """,
                    (since,),
                )
                if last:
                    subtitle = last[0]["subtitle"] or ""
                    if "trail" in subtitle.lower():
                        card["line"] = "Trailed out"
                    elif last[0]["kind"] == "stopped":
                        card["line"] = "Stopped"
        cards.append(card)
    return cards


def _state(now: float | None = None) -> dict:
    from agents.executor import paper_books, paper_night_status
    books = paper_books()
    balance = books["equity"]
    baseline = _balance_before(_night_start(time.time() if now is None else now))
    today = 0.0 if baseline == 0 else (balance - baseline) / baseline * 100.0
    multiplier = 1.0 if baseline == 0 else balance / baseline
    return {
        "balance": balance,
        "cash": books["cash"],
        "realized": books["realized"],
        "unrealized": books["unrealized"],
        "today_pct": today,
        "multiplier": multiplier,
        "mode": get_setting("mode", "PAPER"),
        "agents": _cards(),
        "open_positions": int(query("SELECT COUNT(*) AS n FROM positions WHERE closed_at IS NULL")[0]["n"] or 0),
        "paper_night": paper_night_status(),
        "fixture": os.environ.get("DESK_FIXTURE") == "1",
    }


def _window(range_name: str, now: float) -> float | None:
    spans = {"1H": 3600, "4H": 4 * 3600, "1D": 86400, "1W": 7 * 86400}
    if range_name == "ALL":
        return None
    if range_name == "LIVE":
        return _night_start(now)
    return now - spans[range_name]


def _chart(range_name: str, now: float | None = None) -> dict:
    allowed = {"LIVE", "1H", "4H", "1D", "1W", "ALL"}
    if range_name not in allowed:
        raise HTTPException(status_code=400, detail="range must be LIVE, 1H, 4H, 1D, 1W, or ALL")
    now = time.time() if now is None else now
    start = _window(range_name, now)
    if start is None:
        points = query("SELECT ts, value FROM balance ORDER BY ts")
        marks = query("SELECT ts, kind, title, subtitle, value, mint FROM activity ORDER BY ts")
    else:
        points = query("SELECT ts, value FROM balance WHERE ts >= ? ORDER BY ts", (start,))
        marks = query(
            "SELECT ts, kind, title, subtitle, value, mint FROM activity WHERE ts >= ? ORDER BY ts",
            (start,),
        )
    return {
        "range": range_name,
        "start": points[0]["value"] if points else None,
        "points": [{"ts": row["ts"], "value": row["value"]} for row in points],
        "marks": [
            {
                "ts": row["ts"],
                "kind": row["kind"],
                "title": row["title"],
                "subtitle": row["subtitle"],
                "value": row["value"],
                "mint": row["mint"],
                "coin_url": _coin_url(row["mint"]),
            }
            for row in marks
        ],
    }


def _activity(before: str | None) -> dict:
    cursor = None
    if before not in (None, ""):
        try:
            cursor = float(before)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="before must be a unix timestamp")
    if cursor is None:
        rows = query("SELECT * FROM activity ORDER BY ts DESC, id DESC LIMIT 30")
    else:
        rows = query(
            "SELECT * FROM activity WHERE ts < ? ORDER BY ts DESC, id DESC LIMIT 30",
            (cursor,),
        )
    body = []
    for row in rows:
        body.append({
            "id": row["id"],
            "ts": row["ts"],
            "kind": row["kind"],
            "mint": row["mint"],
            "agent": row["agent"],
            "title": row["title"],
            "subtitle": row["subtitle"],
            "value": row["value"],
            "coin_url": _coin_url(row["mint"]),
        })
    return {"rows": body}


def _candles(mint: str) -> list[dict]:
    rows = query(
        "SELECT ts, price, volume_m5 FROM prices WHERE mint = ? ORDER BY ts",
        (mint,),
    )
    buckets: dict[int, dict] = {}
    previous_volume = None
    for row in rows:
        bucket = int(row["ts"] // 60) * 60
        price = row["price"]
        slot = buckets.get(bucket)
        if slot is None:
            buckets[bucket] = {"t": bucket, "o": price, "h": price, "l": price, "c": price, "v": None}
            slot = buckets[bucket]
        slot["h"] = max(slot["h"], price)
        slot["l"] = min(slot["l"], price)
        slot["c"] = price
        if row["volume_m5"] is not None and previous_volume is not None:
            slot["v"] = float(row["volume_m5"]) - previous_volume
        if row["volume_m5"] is not None:
            previous_volume = float(row["volume_m5"])
    return list(buckets.values())


def _coin(mint: str) -> dict:
    coins = query("SELECT * FROM coins WHERE mint = ?", (mint,))
    if not coins:
        raise HTTPException(status_code=404, detail="not found")
    coin = coins[0]
    last = query(
        "SELECT price, volume_m5, ts FROM prices WHERE mint = ? ORDER BY ts DESC LIMIT 1",
        (mint,),
    )
    scan = query(
        "SELECT liquidity_usd FROM scans WHERE mint = ? ORDER BY ts DESC LIMIT 1",
        (mint,),
    )
    position_rows = query(
        "SELECT * FROM positions WHERE mint = ? ORDER BY opened_at DESC LIMIT 1",
        (mint,),
    )
    position = None
    exit_lines = None
    if position_rows:
        pos = position_rows[0]
        position = {
            "id": pos["id"],
            "opened_at": pos["opened_at"],
            "entry_price": pos["entry_price"],
            "size_usd": pos["size_usd"],
            "peak_price": pos["peak_price"],
            "closed_at": pos["closed_at"],
            "exit_price": pos["exit_price"],
            "exit_reason": pos["exit_reason"],
            "pnl_usd": pos["pnl_usd"],
            "fees_usd": pos["fees_usd"],
            "entry_fee_usd": pos["entry_fee_usd"] if "entry_fee_usd" in pos.keys() else None,
            "mode": pos["mode"],
            "open": pos["closed_at"] is None,
        }
        if position["open"]:
            from agents.executor import _entry_fee, _last_price
            import fees as fee_mod
            entry_fee = _entry_fee(pos)
            position["entry_fee_usd"] = entry_fee
            mark = _last_price(mint)
            entry = float(pos["entry_price"] or 0)
            size = float(pos["size_usd"] or 0)
            if mark is None or entry <= 0:
                position["unrealized_usd"] = None
            else:
                sold = fee_mod.exit_notional(size, entry, mark)
                position["unrealized_usd"] = (sold - fee_mod.fee_usd(sold)) - (size + entry_fee)
        else:
            position["unrealized_usd"] = None
        trail = None
        if pos["trail_active"] and pos["peak_price"] is not None and pos["trail_giveback_pct"] is not None:
            trail = float(pos["peak_price"]) * (1 - float(pos["trail_giveback_pct"]) / 100.0)
        current = trail if trail is not None else pos["stop_price"]
        carried = None
        if pos["entry_price"] and pos["stop_price"] is not None and float(pos["entry_price"]) != 0:
            carried = (1 - float(pos["stop_price"]) / float(pos["entry_price"])) * 100.0
        position["stop_pct"] = carried
        timeout_left = None
        if position["open"] and pos["opened_at"] is not None:
            timeout_left = float(pos["opened_at"]) + get_float("timeout_h") * 3600.0 - time.time()
        position["timeout_left_s"] = timeout_left
        if position["open"]:
            exit_lines = {
                "stop": pos["stop_price"],
                "trail": trail,
                "current": current,
                "label": "Risk trailing stop" if trail is not None else "Stop",
            }
    price = last[0]["price"] if last else None
    change = None
    if price is not None and position and position["open"] and position["entry_price"]:
        change = (float(price) - float(position["entry_price"])) / float(position["entry_price"]) * 100.0
    holders = coin["holders"]
    age_min = None
    if coin["migrated_at"] is not None:
        age_min = max(0.0, (time.time() - float(coin["migrated_at"])) / 60.0)
    audit_body = _audit(mint)
    shield = None
    if audit_body["layers"]:
        passed_n = sum(1 for layer in audit_body["layers"] if layer["pass"] is True)
        shield = f"{passed_n}/{len(audit_body['layers'])}"
    return {
        "mint": mint,
        "name": coin["name"],
        "symbol": coin["symbol"],
        "chain": "solana",
        "age_min": age_min,
        "shield": shield,
        "price": price,
        "change_since_entry_pct": change,
        "candles": _candles(mint),
        "stats": {
            "liquidity_usd": scan[0]["liquidity_usd"] if scan else None,
            "market_cap": None,
            "volume_h24": None,
            "holders": holders if holders is not None else "n/a",
        },
        "position": position,
        "exit_lines": exit_lines,
        "coin_url": _coin_url(mint),
    }


def _audit(mint: str) -> dict:
    rows = query(
        "SELECT * FROM audits WHERE mint = ? ORDER BY ts DESC, id DESC",
        (mint,),
    )
    if not rows:
        return {"mint": mint, "layers": [], "passed": None, "reason": None, "coin_url": _coin_url(mint)}
    latest = rows[0]["ts"]
    layers = []
    for row in rows:
        if row["ts"] != latest:
            continue
        layers.append({
            "n": int(row["layer"]) if str(row["layer"]).isdigit() else row["layer"],
            "name": row["name"],
            "pass": None if row["pass"] is None else bool(row["pass"]),
            "value": row["value_text"],
            "source": row["source"],
        })
    layers.sort(key=lambda item: item["n"])
    reds = [layer for layer in layers if layer["pass"] is False and layer["value"] != "not checked"]
    passed = bool(layers) and not reds and len(layers) == 8
    named = query("SELECT name, symbol FROM coins WHERE mint = ?", (mint,))
    label = _coin_label(named[0]["name"] if named else None, named[0]["symbol"] if named else None, mint)
    sized = query("SELECT size_usd FROM signals WHERE mint = ? ORDER BY ts DESC LIMIT 1", (mint,))
    return {
        "mint": mint,
        "name": label,
        "layers": layers,
        "passed": passed,
        "reason": reds[0]["value"] if reds else None,
        "size_usd": sized[0]["size_usd"] if sized else None,
        "coin_url": _coin_url(mint),
    }


def _decisions(name: str) -> dict:
    if name not in AGENT_NAMES:
        raise HTTPException(status_code=404, detail="not found")
    if name == "scanner":
        rows = query("SELECT ts, mint, verdict, reason FROM scans ORDER BY ts DESC LIMIT 20")
        items = [
            {"ts": row["ts"], "mint": row["mint"], "verdict": row["verdict"], "reason": row["reason"], "coin_url": _coin_url(row["mint"])}
            for row in rows
        ]
    elif name == "analyst":
        rows = query("SELECT ts, mint, \"pass\" AS ok, reason, score FROM verdicts ORDER BY ts DESC LIMIT 20")
        items = [
            {"ts": row["ts"], "mint": row["mint"], "verdict": "pass" if row["ok"] else "reject", "reason": row["reason"], "score": row["score"], "coin_url": _coin_url(row["mint"])}
            for row in rows
        ]
    elif name == "executor":
        rows = query("SELECT ts, mint, status, size_usd, price FROM signals ORDER BY ts DESC LIMIT 20")
        items = [
            {"ts": row["ts"], "mint": row["mint"], "verdict": row["status"], "reason": None, "size_usd": row["size_usd"], "coin_url": _coin_url(row["mint"])}
            for row in rows
        ]
    elif name == "risk":
        rows = query(
            "SELECT ts, mint, title, subtitle, kind FROM activity WHERE agent = 'risk' ORDER BY ts DESC LIMIT 20"
        )
        items = [
            {"ts": row["ts"], "mint": row["mint"], "verdict": row["kind"], "reason": row["subtitle"], "coin_url": _coin_url(row["mint"])}
            for row in rows
        ]
    else:
        rows = query(
            """
            SELECT ts, mint, value_text, "pass" AS ok FROM audits
            WHERE id IN (SELECT MAX(id) FROM audits GROUP BY mint, ts)
            ORDER BY ts DESC LIMIT 20
            """
        )
        items = [
            {"ts": row["ts"], "mint": row["mint"], "verdict": "pass" if row["ok"] else "reject", "reason": row["value_text"], "coin_url": _coin_url(row["mint"])}
            for row in rows
        ]
    found = _names([item.get("mint") for item in items])
    for item in items:
        row = found.get(item.get("mint"))
        item["name"] = _coin_label(row["name"] if row else None, row["symbol"] if row else None, item.get("mint"))
    return {"agent": name, "decisions": items}


def _names(mints: list) -> dict:
    unique = [mint for mint in dict.fromkeys(mints) if mint]
    if not unique:
        return {}
    marks = ",".join("?" for _ in unique)
    rows = query(f"SELECT mint, name, symbol FROM coins WHERE mint IN ({marks})", tuple(unique))
    return {row["mint"]: row for row in rows}


def _web_push_status() -> dict:
    import webpush
    return webpush.status()


def _secret_flags(name: str) -> dict:
    """Presence only. The secret value is not copied into the result."""
    from config import load_secrets
    loaded = load_secrets().get(name)
    return {"configured": bool(loaded), "host_locked": bool(os.environ.get(name))}


def _phantom_status() -> dict:
    from phantom_runner import public_status
    return public_status()


def listed_quotes() -> list:
    from quotes import listed
    return listed()


def _account() -> dict:
    handle = get_setting("fomo_handle")
    profile = links.profile_url(handle) if handle else None
    jev = _secret_flags("TYPESAFE_API_KEY")
    telegram = _secret_flags("TG_TOKEN")
    if jev["host_locked"]:
        jev_detail = "A Jev key is set on the host. This screen cannot show it or replace it."
    elif jev["configured"]:
        jev_detail = "A Jev key is saved on the server. It is not shown here."
    else:
        jev_detail = "No Jev key is saved. Jev calls cannot run until a key is added."
    if telegram["configured"]:
        telegram_detail = "A Telegram token is stored on the server and is not shown. Delivery and commands are not running."
    else:
        telegram_detail = "Telegram was skipped. Delivery and commands are not built."
    return {
        "mode": get_setting("mode", "PAPER"),
        "stack": get_float("paper_start_stack"),
        "profile_url": profile,
        "signup_url": links.SIGNUP_URL,
        "analyst_mode": get_setting("analyst_mode", "jev"),
        "integrations": {
            "email": {"ready": False, "detail": "Email delivery is not configured."},
            "telegram": {"ready": False, "configured": telegram["configured"], "detail": telegram_detail},
            "web_push": _web_push_status(),
        },
        "jev": {
            "configured": jev["configured"],
            "host_locked": jev["host_locked"],
            "detail": jev_detail,
        },
        "quotes": listed_quotes(),
        "phantom": _phantom_status(),
    }


def _public_settings() -> dict:
    groups = {}
    for group, keys in SETTING_GROUPS.items():
        groups[group] = []
        for key in keys:
            rule = SETTING_RULES.get(key)
            label, note = SETTING_COPY.get(key, (key, ""))
            item = {"key": key, "label": label, "note": note, "value": get_setting(key)}
            if rule and rule[0] == "number":
                item["min"] = rule[1]
                item["max"] = rule[2]
            elif rule and rule[0] == "enum":
                item["values"] = list(rule[1])
            elif rule and rule[0] == "bool":
                item["kind"] = "bool"
            groups[group].append(item)
    return {"groups": groups, "ticket": _ticket_note("paper_start_stack")}


def _check_setting(key: str, value: Any) -> str:
    if key in SECRET_KEYS or key == "agents_enabled":
        raise HTTPException(status_code=400, detail="refused")
    rule = SETTING_RULES.get(key)
    if rule is None:
        raise HTTPException(status_code=400, detail="unknown setting")
    if key == "mode":
        raise HTTPException(status_code=400, detail="mode changes use POST /api/mode")
    kind = rule[0]
    if kind == "number":
        if isinstance(value, bool):
            raise HTTPException(status_code=400, detail=f"{key} must be a number from {rule[1]} to {rule[2]}")
        try:
            number = float(str(value).strip())
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"{key} must be a number from {rule[1]} to {rule[2]}")
        if not math.isfinite(number) or number < rule[1] or number > rule[2]:
            raise HTTPException(status_code=400, detail=f"{key} must be a number from {rule[1]} to {rule[2]}")
        whole = {"min_alive_6h", "min_organic", "max_chase", "bundled_penalty", "fees_ref_discount", "min_ticket_usd"}
        if number == int(number) and key not in whole:
            return str(int(number))
        return str(number)
    if kind == "enum":
        text = str(value)
        if text not in rule[1]:
            raise HTTPException(status_code=400, detail=f"{key} must be one of {', '.join(rule[1])}")
        return text
    if kind == "bool":
        text = str(value).strip().lower()
        if text not in {"1", "0", "true", "false", "on", "off", "yes", "no"}:
            raise HTTPException(status_code=400, detail=f"{key} must be true or false")
        return "true" if text in {"1", "true", "on", "yes"} else "false"
    if kind == "time":
        text = str(value)
        parts = text.split(":")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            raise HTTPException(status_code=400, detail="report_time must be HH:MM")
        hour, minute = int(parts[0]), int(parts[1])
        if hour > 23 or minute > 59:
            raise HTTPException(status_code=400, detail="report_time must be HH:MM")
        return f"{hour:02d}:{minute:02d}"
    if kind == "url":
        parsed = urlparse(str(value))
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise HTTPException(status_code=400, detail="rpc_url must be an http(s) URL")
        return str(value)
    text = str(value).strip()
    if len(text) < rule[1] or len(text) > rule[2]:
        raise HTTPException(status_code=400, detail=f"{key} must be {rule[1]} to {rule[2]} characters")
    return text


def _ticket_note(key: str) -> str | None:
    """PAPER uses paper_start_stack. LIVE stack is a different number."""
    from agents.executor import ticket_warning
    watched = {"paper_start_stack", "stack_pct", "below_min", "min_ticket_usd", "stack"}
    if key not in watched:
        return None
    pct = get_float("stack_pct")
    ticket = get_float("min_ticket_usd")
    below = get_setting("below_min", "bump") or "bump"
    if key == "stack":
        raw = get_setting("stack")
        if raw in (None, ""):
            return None
        note = ticket_warning(float(raw), pct, ticket, below)
        return None if note is None else "LIVE stack: " + note
    return ticket_warning(get_float("paper_start_stack"), pct, ticket, below)


@router.get("/api/state")
def state(request: Request) -> dict:
    require_app_token(request)
    return _state()


@router.get("/api/chart")
def chart(request: Request, range: str = "LIVE") -> dict:
    require_app_token(request)
    return _chart(range, time.time())


@router.get("/api/activity")
def activity(request: Request, before: str | None = None) -> dict:
    require_app_token(request)
    return _activity(before)


@router.get("/api/coin/{mint}")
def coin(mint: str, request: Request) -> dict:
    require_app_token(request)
    return _coin(mint)


@router.get("/api/audit/{mint}")
def audit(mint: str, request: Request) -> dict:
    require_app_token(request)
    return _audit(mint)


@router.get("/api/agents/{name}/decisions")
def decisions(name: str, request: Request) -> dict:
    require_app_token(request)
    return _decisions(name)


@router.get("/api/account")
def account(request: Request) -> dict:
    require_app_token(request)
    return _account()


@router.post("/api/account/key")
def account_key(request: Request, body: dict) -> dict:
    """Save or clear the Jev key. The value is never written back to the client."""
    require_app_token(request)
    name = body.get("name")
    if name != "TYPESAFE_API_KEY":
        raise HTTPException(status_code=400, detail="only the Jev key can be changed here")
    from config import set_file_secret
    if body.get("clear") is True:
        try:
            set_file_secret(name, None)
        except PermissionError:
            raise HTTPException(status_code=409, detail="host secret is set")
        return {"ok": True, "configured": False}
    value = body.get("value")
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise HTTPException(status_code=400, detail="value is required")
    try:
        set_file_secret(name, value.strip())
    except PermissionError:
        raise HTTPException(status_code=409, detail="host secret is set")
    except ValueError:
        raise HTTPException(status_code=400, detail="value is required")
    return {"ok": True, "configured": True}


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@router.get("/api/search")
def search(request: Request, q: str = "") -> dict:
    require_app_token(request)
    needle = q.strip()
    if not needle:
        return {"rows": []}
    like = _like(needle)
    rows = query(
        """
        SELECT mint, name, symbol FROM coins
        WHERE mint LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\' OR symbol LIKE ? ESCAPE '\\'
        ORDER BY name
        LIMIT 20
        """,
        (like, like, like),
    )
    return {
        "rows": [
            {
                "mint": row["mint"],
                "name": row["name"],
                "symbol": row["symbol"],
                "coin_url": _coin_url(row["mint"]),
            }
            for row in rows
        ]
    }


@router.get("/api/settings")
def settings_get(request: Request) -> dict:
    require_app_token(request)
    return _public_settings()


@router.post("/api/settings")
def settings_post(request: Request, body: dict) -> dict:
    require_app_token(request)
    key = body.get("key")
    if not isinstance(key, str) or "value" not in body:
        raise HTTPException(status_code=400, detail="key and value are required")
    stored = _check_setting(key, body.get("value"))
    set_setting(key, stored, "api")
    result = {"ok": True, "key": key, "value": stored}
    note = _ticket_note(key)
    if note:
        result["ticket"] = note
    if key == "report_time":
        result["schedule"] = {
            "report_time": stored,
            "timezone": "Asia/Ho_Chi_Minh",
            "server_reschedules_routines": False,
        }
    return result


@router.post("/api/agents/{name}/pause")
def pause(name: str, request: Request) -> dict:
    require_app_token(request)
    if name not in AGENT_NAMES:
        raise HTTPException(status_code=404, detail="not found")
    if name == "risk" and query("SELECT id FROM positions WHERE closed_at IS NULL"):
        raise HTTPException(status_code=409, detail="risk cannot be paused while a position is open")
    _paused.add(name)
    return {"ok": True, "agent": name, "paused": True}


@router.post("/api/agents/{name}/resume")
def resume(name: str, request: Request) -> dict:
    require_app_token(request)
    if name not in AGENT_NAMES:
        raise HTTPException(status_code=404, detail="not found")
    _paused.discard(name)
    return {"ok": True, "agent": name, "paused": False}


@router.post("/api/mode")
def mode(request: Request, body: dict) -> dict:
    require_app_token(request)
    wanted = str(body.get("mode") or "")
    if wanted == "PAPER":
        set_mode("PAPER")
        return {"ok": True, "mode": "PAPER"}
    if wanted == "LIVE":
        if body.get("confirm") != "LIVE":
            raise HTTPException(status_code=400, detail="LIVE requires confirm")
        if not set_mode("LIVE", typed="LIVE"):
            return JSONResponse(status_code=409, content={"ok": False, "error": "LIVE locked", "mode": get_setting("mode", "PAPER")})
        return {"ok": True, "mode": "LIVE"}
    raise HTTPException(status_code=400, detail="mode must be PAPER or LIVE")


@router.post("/api/quote/request")
def quote_request(request: Request, body: dict) -> dict:
    """Owner stores a PAPER quote request. This process does not call Phantom."""
    require_app_token(request)
    from quotes import create_request
    return create_request(body)


@router.post("/api/quote/result")
def quote_result(request: Request, body: dict) -> dict:
    """Owner records a manually imported quote. Execution fields are refused."""
    require_app_token(request)
    from quotes import record_result
    return record_result(body)


@router.post("/api/phantom/login")
def phantom_login_start(request: Request, body: dict | None = None) -> JSONResponse:
    """Start one device login. The body cannot choose a command."""
    require_app_token(request)
    if body not in (None, {}):
        raise HTTPException(status_code=400, detail="unexpected field")
    from phantom_auth import public_login, start
    try:
        start()
    except RuntimeError:
        raise HTTPException(status_code=409, detail="phantom package is not available")
    return JSONResponse(public_login(), headers={"Cache-Control": "no-store"})


@router.get("/api/phantom/login")
def phantom_login_view(request: Request) -> JSONResponse:
    require_app_token(request)
    from phantom_auth import public_login
    return JSONResponse(public_login(), headers={"Cache-Control": "no-store"})


@router.post("/api/phantom/login/cancel")
def phantom_login_cancel(request: Request, body: dict | None = None) -> JSONResponse:
    require_app_token(request)
    if body not in (None, {}):
        raise HTTPException(status_code=400, detail="unexpected field")
    from phantom_auth import cancel
    return JSONResponse(cancel(), headers={"Cache-Control": "no-store"})


@router.post("/api/phantom/login/check")
def phantom_login_check(request: Request, body: dict | None = None) -> JSONResponse:
    """Read wallet_addresses after approval. A 401 does not start login."""
    require_app_token(request)
    if body not in (None, {}):
        raise HTTPException(status_code=400, detail="unexpected field")
    from phantom_auth import check
    return JSONResponse(check(), headers={"Cache-Control": "no-store"})


@router.post("/api/phantom/balance")
def phantom_balance(request: Request, body: dict | None = None) -> JSONResponse:
    """Read Solana balances for the allowlisted wallet. This does not send."""
    require_app_token(request)
    if body not in (None, {}):
        raise HTTPException(status_code=400, detail="unexpected field")
    from phantom_read import balance
    try:
        result = balance()
    except RuntimeError:
        raise HTTPException(status_code=409, detail="phantom package is not available")
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/api/phantom/quote")
def phantom_quote_check(request: Request, body: dict | None = None) -> JSONResponse:
    """Request one execute:false quote. The result cannot be sent."""
    require_app_token(request)
    if body not in (None, {}):
        raise HTTPException(status_code=400, detail="unexpected field")
    from phantom_read import quote
    try:
        result = quote()
    except RuntimeError:
        raise HTTPException(status_code=409, detail="phantom package is not available")
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/api/phantom/run")
def phantom_run(request: Request, body: dict) -> dict:
    """PAPER runner only. It cannot send or switch the desk to LIVE."""
    require_app_token(request)
    from phantom_runner import run_intent
    return run_intent(body)


@router.post("/api/phantom/halt")
def phantom_halt(request: Request, body: dict) -> dict:
    """Revoke or release the kill switch. Send stays off."""
    require_app_token(request)
    from phantom_runner import set_halt
    return set_halt(body)


@router.post("/api/phantom/reconcile")
def phantom_reconcile(request: Request, body: dict) -> dict:
    """Close an unknown submit without quoting or sending again."""
    require_app_token(request)
    from phantom_runner import reconcile_unknown
    return reconcile_unknown(body)


@router.post("/api/push")
async def push(request: Request) -> dict:
    """Save one phone. The body is a JSON object even if the browser labeled it text."""
    require_app_token(request)
    raw = await request.body()
    if len(raw) > 4000:
        raise HTTPException(status_code=400, detail="endpoint, p256dh, and auth are required")
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="endpoint, p256dh, and auth are required")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="endpoint, p256dh, and auth are required")
    endpoint = str(body.get("endpoint") or "")
    p256dh = body.get("p256dh")
    auth = body.get("auth")
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.netloc or not p256dh or not auth:
        raise HTTPException(status_code=400, detail="endpoint, p256dh, and auth are required")
    from db import execute
    execute(
        """
        INSERT INTO push_subs (endpoint, p256dh, auth, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(endpoint) DO UPDATE SET
            p256dh = excluded.p256dh,
            auth = excluded.auth
        """,
        (endpoint, str(p256dh), str(auth), time.time()),
    )
    return {"ok": True}


@router.post("/api/command")
def command(request: Request, body: dict) -> dict:
    """One owner command. The same text works from the Account tab or a caller."""
    require_app_token(request)
    text = body.get("text") if isinstance(body, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=400, detail="text is required")
    if len(text) > 500:
        raise HTTPException(status_code=400, detail="text is too long")
    import commands
    return commands.run(text)


@router.post("/api/notify")
def notify(request: Request, body: dict) -> dict:
    """Owner delivery for a saved report, a verified restart, or one labelled check."""
    require_app_token(request)
    from notify import send
    return send(body)


@router.post("/api/push/test")
def push_test(request: Request) -> dict:
    """Owner action. Sends only when web push is configured. The key is not returned."""
    require_app_token(request)
    import webpush
    state = webpush.status()
    if not state["ready"]:
        raise HTTPException(status_code=409, detail="web push is not configured")
    result = webpush.deliver("Test push", links.SIGNUP_URL)
    return {
        "ok": bool(result.get("ok")) and result["failed"] == 0 and result["sent"] > 0,
        "sent": result["sent"],
        "removed": result["removed"],
        "failed": result["failed"],
        "errors": result.get("errors") or [],
        "reason": result.get("reason"),
    }


@router.get("/api/health")
def desk_health(request: Request) -> dict:
    require_app_token(request)
    from health import diagnose
    return diagnose()


@router.post("/api/health/recover")
async def desk_recover(request: Request, body: dict) -> dict:
    require_app_token(request)
    name = str(body.get("agent") or "")
    if name not in set(AGENT_NAMES) | {"process"}:
        raise HTTPException(status_code=400, detail="unknown agent")
    from health import supervisor
    return await supervisor.recover(name)


@router.get("/api/report")
def report_get(request: Request) -> dict:
    require_app_token(request)
    from report import gather
    return gather()


@router.post("/api/report/page")
def report_page(request: Request, body: dict) -> dict:
    require_app_token(request)
    from report import save_page
    result = save_page(str(body.get("window_id") or ""), str(body.get("url") or ""))
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "not saved")
    return result


@router.get("/api/stream")
async def stream(request: Request) -> StreamingResponse:
    require_app_token(request)
    queue = hub.subscribe()

    async def generate():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event, payload = await asyncio.wait_for(queue.get(), timeout=1)
                except asyncio.TimeoutError:
                    continue
                data = json.dumps(payload, default=str)
                yield f"event: {event}\ndata: {data}\n\n"
        finally:
            hub.unsubscribe(queue)

    return StreamingResponse(generate(), media_type="text/event-stream")
