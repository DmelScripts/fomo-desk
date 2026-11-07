"""Desk commands. The owner types them. This process does not read a chat.

FOMO URLs come from links.py. A command never places an order.
"""

from __future__ import annotations

import re
import time

import db
import links
from db import AGENT_NAMES

HELP = "\n".join([
    "buy X — resend tonight's Buy button, or audit a mint that was never pushed",
    "sell X — Sell on FOMO for an open position",
    "why X — which agent stopped it",
    "pause NAME — or resume NAME",
    "set stop to -8% — a setting by name or alias",
    "stack 1240 — LIVE stack",
    "status",
    "paper",
    "live LIVE",
    "invite",
    "report",
])

# Longest phrase wins. These are the names a command accepts besides the stored key.
ALIASES = (
    ("risk confirm window", "risk.confirm_window_min"),
    ("confirm window", "confirm_window_min"),
    ("risk confirm", "risk.confirm_window_min"),
    ("timeout min gain", "timeout_min_gain_pct"),
    ("analyst timeout", "timeout_s"),
    ("analyst mode", "analyst_mode"),
    ("bundled penalty", "bundled_penalty"),
    ("organic buys", "organic_buys_h1"),
    ("alive volume", "alive_volume_h1"),
    ("recheck price", "recheck_price_up_pct"),
    ("recheck liq", "recheck_liq_drop_pct"),
    ("paper stack", "paper_start_stack"),
    ("report time", "report_time"),
    ("push rejects", "push_rejects"),
    ("fomo handle", "fomo_handle"),
    ("min liquidity", "min_liquidity_usd"),
    ("min ticket", "min_ticket_usd"),
    ("below min", "below_min"),
    ("max pool", "max_pool_pct"),
    ("stack pct", "stack_pct"),
    ("trail start", "trail_start_pct"),
    ("pool age", "min_pool_age_min"),
    ("risk poll", "risk.poll_s"),
    ("chase h1", "chase_h1_pct"),
    ("top 10", "max_top10_pct"),
    ("timeout", "timeout_h"),
    ("liquidity", "min_liquidity_usd"),
    ("runner", "runner_h24_pct"),
    ("handle", "fomo_handle"),
    ("retries", "retries"),
    ("bundled", "max_bundled"),
    ("delegated", "max_delegated_accounts"),
    ("flush", "flush_drop_m5_pct"),
    ("score", "min_score"),
    ("alive", "min_alive_6h"),
    ("organic", "min_organic"),
    ("chase", "max_chase"),
    ("trail", "trail_pct"),
    ("stop", "stop_pct"),
    ("poll", "poll_s"),
    ("fees", "fees_ref_discount"),
    ("notifier", "notifier"),
    ("dev", "max_dev_pct"),
    ("lp", "min_lp_locked_pct"),
    ("h1", "max_h1_change_pct"),
    ("wait", "max_wait_min"),
    ("rpc", "rpc_url"),
)

LABELS = {
    "stop_pct": "stop",
    "trail_pct": "trail",
    "trail_start_pct": "trail start",
    "timeout_h": "timeout",
    "timeout_s": "analyst timeout",
    "stack_pct": "stack pct",
    "paper_start_stack": "paper stack",
    "risk.poll_s": "risk poll",
    "risk.confirm_window_min": "risk confirm",
    "confirm_window_min": "confirm window",
    "analyst_mode": "analyst mode",
}


def audit_only(mint: str) -> dict:
    """Run the auditor and return its result. Do not enqueue analyst or executor.

    Tests replace this. The default path reads public market data for that mint.
    """
    import asyncio

    from agents.auditor import Auditor
    from bus import Desk
    from notifier import Notifier

    auditor = Auditor(Desk(_LiveHttp(), Notifier(mode="PAPER")))
    return asyncio.run(auditor.audit(mint))


class _LiveHttp:
    async def get_json(self, url, timeout=20):
        import httpx
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=timeout)
            if response.status_code >= 400:
                return None
            return response.json()

    async def post_json(self, url, payload, headers=None, timeout=20):
        import httpx
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers, timeout=timeout)
            if response.status_code >= 400:
                return None
            return response.json()


def run(text: str) -> dict:
    folded = re.sub(r"\s+", " ", (text or "").strip())
    if folded.startswith("/"):
        folded = folded[1:].strip()
    lower = folded.lower()
    if not lower or lower in {"help", "?"}:
        return _ok(HELP)
    if lower == "status":
        return _status()
    if lower == "invite":
        return _ok(links.SIGNUP_URL)
    if lower == "report":
        return _ok("No morning page has been written.")
    if lower == "paper":
        from agents.executor import set_mode
        set_mode("PAPER")
        return _ok("mode PAPER")
    if lower == "live" or lower.startswith("live "):
        return _live(folded)
    stack = re.match(r"(?i)stack\s+(\S+)$", folded)
    if stack:
        return _stack(stack.group(1))
    setting = re.match(r"(?i)set\s+(.+?)\s+to\s+(.+)$", folded)
    if setting:
        return _set(setting.group(1).strip().lower(), setting.group(2).strip())
    agent = re.match(r"(?i)(pause|resume)\s+([a-z .]+)$", folded)
    if agent:
        return _pause(agent.group(1).lower(), agent.group(2).strip().lower())
    coin = re.match(r"(?i)(buy|sell|why)\s+(.+)$", folded)
    if coin:
        return _coin_command(coin.group(1).lower(), coin.group(2).strip())
    return _ok(HELP)


def _ok(text: str, coins=None, buttons=None, refused: bool = False) -> dict:
    return {
        "ok": True,
        "refused": refused,
        "text": text,
        "coins": coins or [],
        "buttons": buttons or [],
    }


def _coin_row(mint: str, name=None, symbol=None) -> dict:
    return {
        "mint": mint,
        "name": name,
        "symbol": symbol,
        "coin_url": links.coin_url(mint, "solana"),
    }


def _label(row) -> str:
    if not row:
        return ""
    return row["symbol"] or row["name"] or row["mint"]


def _resolve(query: str) -> dict:
    exact = db.query("SELECT mint, name, symbol FROM coins WHERE mint = ?", (query,))
    if len(exact) == 1:
        return {"row": exact[0], "mint": exact[0]["mint"]}
    if _is_mint(query):
        return {"row": exact[0] if exact else None, "mint": query}
    key = query.casefold()
    rows = db.query(
        "SELECT mint, name, symbol FROM coins WHERE name IS NOT NULL OR symbol IS NOT NULL"
    )
    found = [row for row in rows if (row["name"] or "").casefold() == key or (row["symbol"] or "").casefold() == key]
    if not found:
        return {"ask": query}
    if len(found) > 1:
        return {"choices": found}
    return {"row": found[0], "mint": found[0]["mint"]}


def _is_mint(value: str) -> bool:
    if len(value) < 32 or len(value) > 44:
        return False
    alphabet = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
    return all(char in alphabet for char in value)


def _choices(rows) -> dict:
    lines = ["Which coin?"]
    coins = []
    for row in rows:
        coin = _coin_row(row["mint"], row["name"], row["symbol"])
        coins.append(coin)
        lines.append(f"{_label(row)}\n{coin['coin_url']}")
    return _ok("\n".join(lines), coins=coins, refused=True)


def _ask(query: str) -> dict:
    return _ok(f"I don't know {query}. Send the mint.", refused=True)


def _coin_command(kind: str, query: str) -> dict:
    found = _resolve(query)
    if found.get("ask"):
        return _ask(found["ask"])
    if found.get("choices"):
        return _choices(found["choices"])
    mint = found["mint"]
    row = found.get("row")
    if kind == "buy":
        return _buy(mint, row)
    if kind == "sell":
        return _sell(mint, row)
    return _why(mint, row)


def _buy(mint: str, row) -> dict:
    from api import _night_start
    start = _night_start()
    pushed = db.query(
        """
        SELECT mint, ts, price, size_usd, stack_pct, link, mode, status
        FROM signals
        WHERE mint = ? AND ts >= ? AND status IN ('sent', 'bought')
        ORDER BY ts DESC LIMIT 1
        """,
        (mint, start),
    )
    coin = _coin_row(mint, row["name"] if row else None, row["symbol"] if row else None)
    if pushed:
        return _resend(pushed[0], coin)
    if not db.flag_on("enabled"):
        text = f"Auditor is off. not sized, not on the desk\n{coin['coin_url']}"
        return _ok(text, coins=[coin], buttons=[{"label": "Buy on FOMO", "url": coin["coin_url"]}])
    try:
        result = audit_only(mint)
    except Exception:
        text = f"The audit could not be read. not sized, not on the desk\n{coin['coin_url']}"
        return _ok(text, coins=[coin], buttons=[{"label": "Buy on FOMO", "url": coin["coin_url"]}], refused=True)
    _save_layers(mint, result)
    lines = ["not sized, not on the desk", _label(row) or mint]
    for layer in result.get("layers") or []:
        lines.append(f"{layer.get('n')} {layer.get('name')}: {layer.get('value')}")
    lines.append(coin["coin_url"])
    return _ok("\n".join(lines), coins=[coin], buttons=[{"label": "Buy on FOMO", "url": coin["coin_url"]}])


def _resend(signal, coin: dict) -> dict:
    """Send tonight's Buy button again. Does not write a signal or a position."""
    from notifier import Notifier
    url = signal["link"] or coin["coin_url"]
    name = coin["symbol"] or coin["name"] or coin["mint"]
    size = signal["size_usd"]
    pct = signal["stack_pct"]
    if size is None:
        title = f"Buy {name}"
    else:
        pct_text = "" if pct is None else f" · {float(pct):.2f}% of stack"
        title = f"Buy {name} · ${float(size):.2f}{pct_text}"
    stop = db.get_float("stop_pct")
    trail = db.get_float("trail_pct")
    body = f"stop -{_num(stop)}% · trail {_num(trail)}%"
    mode = (db.get_setting("mode", "PAPER") or "PAPER").upper()
    shown = f"PAPER · {title}" if mode == "PAPER" else title
    Notifier(mode=mode).push(title, body, url, [{"label": "Buy on FOMO", "url": url}], activity=False)
    return _ok(f"{shown}\n{body}\n{url}", coins=[coin], buttons=[{"label": "Buy on FOMO", "url": url}])


def _sell(mint: str, row) -> dict:
    coin = _coin_row(mint, row["name"] if row else None, row["symbol"] if row else None)
    open_row = db.query(
        "SELECT id, size_usd FROM positions WHERE mint = ? AND closed_at IS NULL ORDER BY opened_at DESC LIMIT 1",
        (mint,),
    )
    if not open_row:
        return _ok(f"No open position for {_label(row) or mint}.\n{coin['coin_url']}", coins=[coin])
    size = open_row[0]["size_usd"]
    size_text = "" if size is None else f" · ${float(size):.2f}"
    text = f"Sell {_label(row) or mint}{size_text}\n{coin['coin_url']}"
    return _ok(text, coins=[coin], buttons=[{"label": "Sell on FOMO", "url": coin["coin_url"]}])


def _why(mint: str, row) -> dict:
    coin = _coin_row(mint, row["name"] if row else None, row["symbol"] if row else None)
    events = []
    for item in db.query(
        "SELECT closed_at AS ts, exit_reason, pnl_usd FROM positions WHERE mint = ? AND exit_reason IS NOT NULL",
        (mint,),
    ):
        value = item["exit_reason"]
        if item["pnl_usd"] is not None:
            value = f"{value} {float(item['pnl_usd']):.2f}"
        events.append((item["ts"] or 0, "Risk", value))
    for item in db.query(
        """
        SELECT ts, name, value_text FROM audits
        WHERE mint = ? AND "pass" = 0 AND value_text != 'not checked'
        """,
        (mint,),
    ):
        events.append((item["ts"] or 0, "Auditor", f"{item['name']}: {item['value_text']}"))
    for item in db.query(
        'SELECT ts, reason FROM verdicts WHERE mint = ? AND "pass" = 0',
        (mint,),
    ):
        events.append((item["ts"] or 0, "Analyst", item["reason"] or "rejected"))
    for item in db.query(
        "SELECT ts, reason FROM scans WHERE mint = ? AND verdict = 'kill'",
        (mint,),
    ):
        events.append((item["ts"] or 0, "Scanner", item["reason"] or "killed"))
    label = _label(row) or mint
    if not events:
        return _ok(f"Nothing on the desk stopped {label}.\n{coin['coin_url']}", coins=[coin])
    ts, agent, value = max(events, key=lambda item: item[0])
    when = _clock(ts) if ts else "unknown time"
    return _ok(f"{agent} stopped {label} at {when}: {value}\n{coin['coin_url']}", coins=[coin])


def _pause(action: str, name: str) -> dict:
    from api import _paused
    cleaned = name.replace(" ", "")
    if cleaned not in AGENT_NAMES:
        return _ok("Unknown agent. Use scanner, auditor, analyst, executor, or risk.", refused=True)
    if action == "pause":
        if cleaned == "risk" and db.query("SELECT id FROM positions WHERE closed_at IS NULL"):
            return _ok("Risk cannot be paused while a position is open.", refused=True)
        _paused.add(cleaned)
        return _ok(f"{cleaned} paused")
    _paused.discard(cleaned)
    return _ok(f"{cleaned} resumed")


def _set(phrase: str, value: str) -> dict:
    from fastapi import HTTPException
    from api import _check_setting
    key = _alias(phrase)
    if key is None:
        return _ok(f"Unknown setting {phrase}.", refused=True)
    if key == "mode":
        upper = value.upper()
        if upper == "LIVE":
            return _ok("Type LIVE on its own line as: live LIVE. Nothing was changed.", refused=True)
        if upper == "PAPER":
            from agents.executor import set_mode
            set_mode("PAPER")
            return _ok("mode PAPER")
        if value.lower() in {"jev", "rules"}:
            key = "analyst_mode"
        else:
            return _ok("mode must be PAPER or LIVE.", refused=True)
    stored_value = value
    if key == "stop_pct":
        stored_value = value.replace("%", "").strip()
        if stored_value.startswith("-"):
            stored_value = stored_value[1:]
    try:
        stored = _check_setting(key, stored_value)
    except HTTPException as exc:
        return _ok(str(exc.detail), refused=True)
    old = db.get_setting(key)
    db.set_setting(key, stored, "command")
    return _ok(f"{LABELS.get(key, key)} {_face(key, old)} -> {_face(key, stored)}")


def _stack(raw: str) -> dict:
    from fastapi import HTTPException
    from api import _check_setting
    try:
        stored = _check_setting("stack", raw.replace("$", "").replace(",", ""))
    except HTTPException as exc:
        return _ok(str(exc.detail), refused=True)
    old = db.get_setting("stack")
    db.set_setting("stack", stored, "command")
    previous = "unset" if old in (None, "") else old
    return _ok(f"LIVE stack {previous} -> {stored}")


def _live(folded: str) -> dict:
    from agents.executor import set_mode
    confirmed = bool(re.search(r"\bLIVE\b", folded))
    if not confirmed:
        return _ok("Type LIVE to switch. Nothing was changed.", refused=True)
    if not set_mode("LIVE", typed="LIVE"):
        return _ok("LIVE locked. A full PAPER night is not on record.", refused=True)
    return _ok("mode LIVE")


def _status() -> dict:
    from agents.executor import paper_balance
    mode = db.get_setting("mode", "PAPER") or "PAPER"
    lines = [mode, f"Balance ${paper_balance():.2f}"]
    rows = db.query(
        """
        SELECT p.mint, p.size_usd, c.name, c.symbol
        FROM positions p LEFT JOIN coins c ON c.mint = p.mint
        WHERE p.closed_at IS NULL
        ORDER BY p.opened_at
        """
    )
    coins = []
    buttons = []
    if not rows:
        lines.append("No open position.")
    for row in rows:
        coin = _coin_row(row["mint"], row["name"], row["symbol"])
        coins.append(coin)
        buttons.append({"label": "Sell on FOMO", "url": coin["coin_url"]})
        size = "" if row["size_usd"] is None else f" · ${float(row['size_usd']):.2f}"
        lines.append(f"{_label(row) or row['mint']}{size}\n{coin['coin_url']}")
    return _ok("\n".join(lines), coins=coins, buttons=buttons)


def _alias(phrase: str) -> str | None:
    from config import SETTING_RULES
    cleaned = re.sub(r"\s+", " ", phrase.strip().lower())
    if cleaned in SETTING_RULES:
        return cleaned
    for alias, key in ALIASES:
        if cleaned == alias:
            return key
    return None


def _face(key: str, value) -> str:
    if value in (None, ""):
        return "unset"
    if key == "stop_pct":
        return f"-{_num(float(value))}%"
    if key in {"trail_pct", "trail_start_pct", "stack_pct", "max_top10_pct", "max_dev_pct", "min_lp_locked_pct"}:
        return f"{_num(float(value))}%"
    return str(value)


def _save_layers(mint: str, result: dict) -> None:
    now = time.time()
    for layer in result.get("layers") or []:
        value = layer.get("value")
        passed = None if value == "not checked" or layer.get("pass") is None else (1 if layer.get("pass") else 0)
        db.execute(
            """
            INSERT INTO audits (mint, ts, layer, name, value_text, "pass", source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (mint, now, str(layer.get("n")), layer.get("name"), value, passed, layer.get("source")),
        )


def _clock(ts: float) -> str:
    from datetime import datetime
    from config import DESK_TZ
    return datetime.fromtimestamp(float(ts), DESK_TZ).strftime("%Y-%m-%d %H:%M")


def _num(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.1f}"
