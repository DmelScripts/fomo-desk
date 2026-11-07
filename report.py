"""Facts for the previous Vietnam night. This does not write a Page.

The night-report routine builds the Page. Gathering does not send a push
or clear skips. Skips clear only after a Page URL is saved, and only the
skips that belonged to that window.
"""

from __future__ import annotations

import time
from datetime import datetime
from urllib.parse import urlparse

import db
import links
from config import DESK_TZ, report_boundary

WINDOW_HOURS = 24


def window(now: float | None = None) -> dict:
    """The completed 08:00-to-08:00 night before the current one."""
    now = time.time() if now is None else now
    clock = db.get_setting("report_time", "08:00") or "08:00"
    end = report_boundary(now, clock, upcoming=False)
    start = end - WINDOW_HOURS * 3600
    start_label = datetime.fromtimestamp(start, DESK_TZ).strftime("%Y-%m-%dT%H:%M")
    end_label = datetime.fromtimestamp(end, DESK_TZ).strftime("%Y-%m-%dT%H:%M")
    window_id = f"{start_label}+07:00/{end_label}+07:00"
    return {"id": window_id, "start": start, "end": end, "timezone": "Asia/Ho_Chi_Minh", "report_time": clock}


def gather(now: float | None = None) -> dict:
    """Read the night. Does not notify and does not delete skips."""
    span = window(now)
    start, end = span["start"], span["end"]
    trades = _trades(start, end)
    coins = _coins(start, end)
    kills = _kills(start, end)
    refusals = _refusals(start, end)
    analyst = _analyst(start, end)
    jev = _jev(start, end)
    fees = _fees(trades)
    ran = bool(trades["red"] or trades["green"] or trades["unpriced"] or coins or kills or refusals or analyst)
    if not ran:
        status = "not_run"
    elif not trades["red"] and not trades["green"] and not trades["unpriced"]:
        status = "no_trades"
    else:
        status = "ok"
    return {
        "window": span,
        "status": status,
        "page_available": False,
        "agents_enabled": db.agents_enabled(),
        "trades": trades,
        "coins_seen": coins,
        "scanner_kills": kills,
        "auditor_refusals": refusals,
        "analyst_rejects": analyst,
        "jev": jev,
        "fees_usd": fees,
        "footer": "New to FOMO? " + links.SIGNUP_URL,
        "schedule": {
            "report_time": span["report_time"],
            "timezone": span["timezone"],
            "server_reschedules_routines": False,
        },
    }


def last_page() -> str | None:
    rows = db.query(
        "SELECT url FROM reports WHERE url IS NOT NULL ORDER BY acked_at DESC LIMIT 1"
    )
    if not rows:
        return None
    return rows[0]["url"]


def save_page(window_id: str, url: str, now: float | None = None) -> dict:
    """Store a Page URL. Clear that window's skips once, never the next night's."""
    parsed = urlparse(url or "")
    if parsed.scheme != "https" or not parsed.netloc:
        return {"ok": False, "error": "url must be https"}
    span = _span_from_id(window_id)
    if span is None:
        return {"ok": False, "error": "unknown window"}
    now = time.time() if now is None else now

    def work(conn):
        row = conn.execute(
            "SELECT skips_cleared FROM reports WHERE window_id = ?",
            (window_id,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO reports (window_id, url, acked_at, skips_cleared) VALUES (?, ?, ?, 0)",
                (window_id, url, now),
            )
            cleared = _clear_window_skips(conn, span["end"])
            conn.execute(
                "UPDATE reports SET skips_cleared = 1, url = ?, acked_at = ? WHERE window_id = ?",
                (url, now, window_id),
            )
            return {"ok": True, "saved": True, "duplicate": False, "skips_cleared": cleared}
        if row["skips_cleared"]:
            conn.execute(
                "UPDATE reports SET url = ?, acked_at = ? WHERE window_id = ?",
                (url, now, window_id),
            )
            return {"ok": True, "saved": True, "duplicate": True, "skips_cleared": 0}
        cleared = _clear_window_skips(conn, span["end"])
        conn.execute(
            "UPDATE reports SET url = ?, acked_at = ?, skips_cleared = 1 WHERE window_id = ?",
            (url, now, window_id),
        )
        return {"ok": True, "saved": True, "duplicate": False, "skips_cleared": cleared}

    return db.exclusive(work)


def _clear_window_skips(conn, end: float) -> int:
    """Skips due at or before this night's close. Later nights stay."""
    rows = conn.execute(
        'SELECT mint, "until" FROM skips WHERE "until" <= ?',
        (end,),
    ).fetchall()
    conn.execute('DELETE FROM skips WHERE "until" <= ?', (end,))
    return len(rows)


def _span_from_id(window_id: str) -> dict | None:
    if "/" not in window_id or not window_id.endswith("+07:00"):
        return None
    try:
        start_label, end_label = window_id.split("/", 1)
        start = datetime.strptime(start_label, "%Y-%m-%dT%H:%M%z").timestamp()
        end = datetime.strptime(end_label, "%Y-%m-%dT%H:%M%z").timestamp()
    except ValueError:
        return None
    if end - start != WINDOW_HOURS * 3600:
        return None
    return {"start": start, "end": end}


def _trades(start: float, end: float) -> dict:
    rows = db.query(
        """
        SELECT p.mint, p.entry_price, p.exit_price, p.size_usd, p.pnl_usd, p.fees_usd,
               p.exit_reason, p.closed_at, c.name, c.symbol
        FROM positions p LEFT JOIN coins c ON c.mint = p.mint
        WHERE p.closed_at >= ? AND p.closed_at < ?
        ORDER BY p.closed_at
        """,
        (start, end),
    )
    red, green, unpriced = [], [], []
    for row in rows:
        item = _trade(row)
        if row["pnl_usd"] is None:
            unpriced.append(item)
        elif float(row["pnl_usd"]) < 0:
            red.append(item)
        else:
            green.append(item)
    red.sort(key=lambda item: item["pnl_usd"])
    return {"red": red, "green": green, "unpriced": unpriced}


def _trade(row) -> dict:
    entry = row["entry_price"]
    exit_price = row["exit_price"]
    pct = None
    if entry not in (None, 0) and exit_price is not None:
        pct = (float(exit_price) - float(entry)) / float(entry) * 100.0
    return {
        "mint": row["mint"],
        "name": row["name"],
        "symbol": row["symbol"],
        "coin_url": links.coin_url(row["mint"], "solana") if row["mint"] else None,
        "entry_price": entry,
        "exit_price": exit_price,
        "size_usd": row["size_usd"],
        "pnl_usd": row["pnl_usd"],
        "pct": pct,
        "exit_reason": row["exit_reason"],
        "fees_usd": row["fees_usd"],
    }


def _coins(start: float, end: float) -> list[dict]:
    found = []
    seen = set()
    queries = (
        ("SELECT DISTINCT mint FROM scans WHERE ts >= ? AND ts < ?", (start, end)),
        ("SELECT DISTINCT mint FROM audits WHERE ts >= ? AND ts < ?", (start, end)),
        ("SELECT DISTINCT mint FROM verdicts WHERE ts >= ? AND ts < ?", (start, end)),
        ("SELECT DISTINCT mint FROM signals WHERE ts >= ? AND ts < ?", (start, end)),
        ("SELECT DISTINCT mint FROM positions WHERE opened_at >= ? AND opened_at < ?", (start, end)),
        ("SELECT DISTINCT mint FROM positions WHERE closed_at >= ? AND closed_at < ?", (start, end)),
    )
    for sql, params in queries:
        for row in db.query(sql, params):
            mint = row["mint"]
            if not mint or mint in seen:
                continue
            seen.add(mint)
            named = db.query("SELECT name, symbol FROM coins WHERE mint = ?", (mint,))
            name = named[0]["name"] if named else None
            symbol = named[0]["symbol"] if named else None
            found.append({
                "mint": mint,
                "name": name,
                "symbol": symbol,
                "coin_url": links.coin_url(mint, "solana"),
            })
    return found


def _kills(start: float, end: float) -> list[dict]:
    rows = db.query(
        """
        SELECT COALESCE(reason, '') AS reason, COUNT(*) AS n
        FROM scans
        WHERE verdict = 'kill' AND ts >= ? AND ts < ?
        GROUP BY COALESCE(reason, '')
        ORDER BY n DESC
        """,
        (start, end),
    )
    return [{"reason": row["reason"], "count": int(row["n"])} for row in rows]


def _refusals(start: float, end: float) -> list[dict]:
    rows = db.query(
        """
        SELECT name, ts FROM audits
        WHERE "pass" = 0 AND value_text != 'not checked' AND ts >= ? AND ts < ?
        ORDER BY ts
        """,
        (start, end),
    )
    grouped: dict[str, list[float]] = {}
    for row in rows:
        grouped.setdefault(row["name"] or "", []).append(row["ts"])
    return [{"layer": name, "times": times, "count": len(times)} for name, times in grouped.items()]


def _analyst(start: float, end: float) -> list[dict]:
    rows = db.query(
        """
        SELECT reason, COUNT(*) AS n FROM verdicts
        WHERE "pass" = 0 AND ts >= ? AND ts < ?
        GROUP BY reason
        ORDER BY n DESC
        """,
        (start, end),
    )
    return [{"rule": row["reason"], "count": int(row["n"])} for row in rows]


def _jev(start: float, end: float) -> dict:
    rows = db.query(
        "SELECT input_tokens FROM verdicts WHERE ts >= ? AND ts < ?",
        (start, end),
    )
    if not rows:
        tokens = 0
        known = True
    elif any(row["input_tokens"] is None for row in rows):
        tokens = None
        known = False
    else:
        tokens = int(sum(row["input_tokens"] for row in rows))
        known = True
    return {
        "input_tokens": tokens,
        "tokens_known": known,
        "cost_usd": None,
        "cost": "unknown",
    }


def _fees(trades: dict) -> dict:
    known = []
    missing = False
    for item in trades["red"] + trades["green"] + trades["unpriced"]:
        if item["fees_usd"] is None:
            missing = True
        else:
            known.append(float(item["fees_usd"]))
    return {"total": sum(known), "complete": not missing}
