"""Plan A JSON API. APP_TOKEN is required on every /api route.

The token is read from Authorization: Bearer. The URL fragment is not sent
to the server; the app will attach the header later. A token in the query
string is rejected so it is not treated as a credential.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import time
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

import links
from agents.executor import paper_balance, set_mode
from bus import covered_price_mints, hub
from config import SECRET_KEYS, SETTING_GROUPS, SETTING_RULES, load_secrets, report_boundary
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


def _agent_count(name: str) -> int:
    if name == "scanner":
        return query("SELECT COUNT(*) AS n FROM scans")[0]["n"]
    if name == "auditor":
        return query("SELECT COUNT(DISTINCT mint) AS n FROM audits")[0]["n"]
    if name == "analyst":
        return query("SELECT COUNT(*) AS n FROM verdicts")[0]["n"]
    if name == "executor":
        return query("SELECT COUNT(*) AS n FROM signals")[0]["n"]
    return query(
        "SELECT COUNT(*) AS n FROM positions WHERE exit_reason IS NOT NULL"
    )[0]["n"]


def _cards() -> list[dict]:
    enabled = agents_enabled()
    cards = []
    for name in AGENT_NAMES:
        count = int(_agent_count(name))
        paused = name in _paused
        if name == "scanner" and count == 0:
            line = "Listening for graduations"
        elif name == "auditor" and count == 0:
            line = "No coin taken apart yet"
        elif name == "risk" and query("SELECT id FROM positions WHERE closed_at IS NULL"):
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
        if name == "risk":
            card["stop_pct"] = get_float("stop_pct")
            card["trail_pct"] = get_float("trail_pct")
        if name == "executor":
            card["stack_pct"] = get_float("stack_pct")
        cards.append(card)
    return cards


def _state() -> dict:
    balance = paper_balance()
    baseline = _balance_before(_night_start())
    today = 0.0 if baseline == 0 else (balance - baseline) / baseline * 100.0
    multiplier = 1.0 if baseline == 0 else balance / baseline
    return {
        "balance": balance,
        "today_pct": today,
        "multiplier": multiplier,
        "mode": get_setting("mode", "PAPER"),
        "agents": _cards(),
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
        marks = query("SELECT ts, kind, title, mint FROM activity ORDER BY ts")
    else:
        points = query("SELECT ts, value FROM balance WHERE ts >= ? ORDER BY ts", (start,))
        marks = query("SELECT ts, kind, title, mint FROM activity WHERE ts >= ? ORDER BY ts", (start,))
    return {
        "range": range_name,
        "start": points[0]["value"] if points else None,
        "points": [{"ts": row["ts"], "value": row["value"]} for row in points],
        "marks": [
            {
                "ts": row["ts"],
                "kind": row["kind"],
                "title": row["title"],
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
            "mode": pos["mode"],
            "open": pos["closed_at"] is None,
        }
        trail = None
        if pos["trail_active"] and pos["peak_price"] is not None:
            trail = float(pos["peak_price"]) * (1 - get_float("trail_pct") / 100.0)
        exit_lines = {"stop": pos["stop_price"], "trail": trail}
    price = last[0]["price"] if last else None
    change = None
    if price is not None and position and position["entry_price"]:
        change = (float(price) - float(position["entry_price"])) / float(position["entry_price"]) * 100.0
    holders = coin["holders"]
    return {
        "mint": mint,
        "name": coin["name"],
        "symbol": coin["symbol"],
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
    return {
        "mint": mint,
        "layers": layers,
        "passed": bool(layers) and not reds and len(layers) == 8,
        "reason": reds[0]["value"] if reds else None,
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
    return {"agent": name, "decisions": items}


def _account() -> dict:
    handle = get_setting("fomo_handle")
    profile = links.profile_url(handle) if handle else None
    return {
        "mode": get_setting("mode", "PAPER"),
        "stack": get_float("paper_start_stack"),
        "profile_url": profile,
        "signup_url": links.SIGNUP_URL,
    }


def _public_settings() -> dict:
    groups = {}
    for group, keys in SETTING_GROUPS.items():
        groups[group] = []
        for key in keys:
            rule = SETTING_RULES.get(key)
            item = {"key": key, "value": get_setting(key)}
            if rule and rule[0] == "number":
                item["min"] = rule[1]
                item["max"] = rule[2]
            elif rule and rule[0] == "enum":
                item["values"] = list(rule[1])
            groups[group].append(item)
    return {"groups": groups}


def _check_setting(key: str, value: Any) -> str:
    if key in SECRET_KEYS or key == "agents_enabled":
        raise HTTPException(status_code=400, detail="refused")
    rule = SETTING_RULES.get(key)
    if rule is None:
        raise HTTPException(status_code=400, detail="unknown setting")
    if key == "mode" and str(value).upper() == "LIVE":
        raise HTTPException(status_code=400, detail="LIVE requires POST /api/mode with confirm")
    kind = rule[0]
    if kind == "number":
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"{key} must be a number from {rule[1]} to {rule[2]}")
        if number < rule[1] or number > rule[2]:
            raise HTTPException(status_code=400, detail=f"{key} must be from {rule[1]} to {rule[2]}")
        text = str(int(number)) if number == int(number) and key not in {"min_alive_6h", "min_organic", "max_chase", "bundled_penalty", "fees_ref_discount", "min_ticket_usd"} else str(value)
        return text
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
    if key not in {"paper_start_stack", "stack_pct", "below_min", "min_ticket_usd"}:
        return None
    from agents.executor import ticket_warning
    return ticket_warning(
        get_float("paper_start_stack"),
        get_float("stack_pct"),
        get_float("min_ticket_usd"),
        get_setting("below_min", "bump") or "bump",
    )


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


@router.post("/api/push")
def push(request: Request, body: dict) -> dict:
    require_app_token(request)
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
