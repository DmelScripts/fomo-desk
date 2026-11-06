"""Executor, agent 4 of 5. Arithmetic and one Buy button. Never an order.

PAPER fills at the signal price when the push goes out.
LIVE waits for the Bought tap. No tap inside confirm_window_min (5) is "missed".
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import db
import links
from bus import Desk

log = logging.getLogger("executor")


def size_ticket(
    stack_usd: float,
    stack_pct: float,
    min_ticket_usd: float,
    below_min: str,
    liquidity_usd: float | None,
    max_pool_pct: float,
    free_usd: float,
) -> tuple[float | None, str | None]:
    """8% of stack, then cap at 2% of pool, then the $47.50 floor."""
    raw = stack_usd * stack_pct / 100.0
    size = raw
    if liquidity_usd is not None and max_pool_pct >= 0:
        size = min(size, liquidity_usd * max_pool_pct / 100.0)
    if size < min_ticket_usd:
        if str(below_min).lower() != "bump":
            return None, "under $47.50"
        if free_usd < min_ticket_usd:
            return None, "no free stack"
        size = min_ticket_usd
    if size > free_usd:
        return None, "no free stack"
    return round(size + 1e-9, 2), None


def ticket_warning(stack_usd: float, stack_pct: float, min_ticket_usd: float, below_min: str) -> str | None:
    """STEP 10 ticket check. min_stack is $593.75 at the defaults."""
    if stack_pct <= 0:
        return None
    min_stack = min_ticket_usd / (stack_pct / 100.0)
    if stack_usd >= min_stack:
        return None
    if str(below_min).lower() == "bump":
        pct = min_ticket_usd / stack_usd * 100.0 if stack_usd else 0
        return f"tickets bumped to ${min_ticket_usd:.2f} = {pct:.2f}% of stack"
    return f"every coin will be skipped, stack under ${min_stack:.2f}"


def paper_balance() -> float:
    start = db.get_float("paper_start_stack")
    closed = db.query(
        "SELECT COALESCE(SUM(pnl_usd), 0) AS n FROM positions WHERE mode = 'PAPER' AND closed_at IS NOT NULL"
    )
    return start + float(closed[0]["n"] or 0)


def open_notional(mode: str) -> float:
    rows = db.query(
        "SELECT COALESCE(SUM(size_usd), 0) AS n FROM positions WHERE mode = ? AND closed_at IS NULL",
        (mode,),
    )
    return float(rows[0]["n"] or 0)


def paper_night_done() -> bool:
    """Fail closed. STEP 11 requires one full PAPER night before LIVE.

    A closed PAPER position is not that night. The prompt never says how
    long a night is, and inventing a duration would let LIVE turn on too
    early. This stays False until a night is defined. Typing LIVE is still
    required, and it is not enough on its own.
    """
    return False


def allow_live(typed: str | None) -> bool:
    return typed == "LIVE" and paper_night_done()


def set_mode(mode: str, typed: str | None = None) -> bool:
    if mode == "PAPER":
        db.set_setting("mode", "PAPER", "owner")
        return True
    if mode == "LIVE" and allow_live(typed):
        db.set_setting("mode", "LIVE", "owner")
        return True
    return False


class Executor:
    def __init__(self, desk: Desk) -> None:
        self.desk = desk
        self.sent: set[str] = set()
        self.pending: dict[str, dict] = {}

    async def run(self) -> None:
        import asyncio
        self._warn()
        while True:
            db.beat("executor", self.desk.now())
            self._expire_live()
            try:
                payload = await asyncio.wait_for(self.desk.bus.executor.get(), timeout=1)
            except asyncio.TimeoutError:
                continue
            await self.handle(payload)

    def _warn(self) -> None:
        warning = ticket_warning(
            db.get_float("paper_start_stack"),
            db.get_float("stack_pct"),
            db.get_float("min_ticket_usd"),
            db.get_setting("below_min", "bump") or "bump",
        )
        if warning:
            log.warning("%s", warning)
            self.desk.notifier.push("Executor", warning, links.SIGNUP_URL, [])

    async def handle(self, payload: dict) -> dict:
        db.beat("executor", self.desk.now())
        mint = payload.get("mint")
        if mint in self.sent:
            return {"mint": mint, "status": "skipped", "reason": "already sent"}
        mode = (db.get_setting("mode", "PAPER") or "PAPER").upper()
        if mode == "LIVE" and not paper_night_done():
            log.warning("LIVE refused; a full PAPER night is not proven")
            return {
                "agent": "executor",
                "mint": mint,
                "status": "skipped",
                "reason": "LIVE locked",
                "link": links.coin_url(mint, "solana"),
                "mode": "PAPER",
            }
        stack = paper_balance() if mode == "PAPER" else db.get_float("paper_start_stack")
        if mode == "LIVE" and db.get_setting("stack"):
            stack = db.get_float("stack")
        free = stack - open_notional(mode)
        fresh_liq = payload.get("fresh_liquidity", payload.get("liquidity_usd"))
        fresh_price = payload.get("fresh_price", payload.get("price"))
        scanner_liq = payload.get("liquidityUsd", payload.get("liquidity_usd"))
        analyst_price = payload.get("analyst_price", payload.get("price"))
        reason = _recheck(scanner_liq, fresh_liq, analyst_price, fresh_price)
        size = None
        if reason is None:
            if fresh_price is None:
                reason = "no price"
            else:
                size, reason = size_ticket(
                    stack,
                    db.get_float("stack_pct"),
                    db.get_float("min_ticket_usd"),
                    db.get_setting("below_min", "bump") or "bump",
                    fresh_liq,
                    db.get_float("max_pool_pct"),
                    free,
                )
        name = payload.get("symbol") or payload.get("name") or mint
        url = links.coin_url(mint, "solana")
        now = self.desk.now()
        if reason or size is None:
            self._signal(mint, now, fresh_price, None, url, mode, "skipped", reason)
            self.sent.add(mint)
            self.desk.bus.publish("signal", {
                "agent": "executor", "mint": mint, "signalPrice": fresh_price, "sizeUsd": None,
                "link": url, "mode": mode if mode in ("PAPER", "LIVE") else "PAPER",
                "status": "skipped", "reason": reason,
            })
            log.info("mint=%s stack=%s size=%s cap=%s status=skipped reason=%s", mint, stack, size, None, reason)
            return {"agent": "executor", "mint": mint, "status": "skipped", "reason": reason, "link": url, "mode": mode}
        pct = size / stack * 100.0 if stack else 0
        stop = db.get_float("stop_pct")
        trail = db.get_float("trail_pct")
        score = payload.get("score")
        prefix = "PAPER · " if mode == "PAPER" else ""
        title = f"{prefix}Buy {name} · ${size:.2f} · {pct:.2f}% of stack"
        body = f"Auditor 8/8 · Analyst {score} · stop -{_fmt(stop)}% · trail {_fmt(trail)}%"
        pushed = self._push(title, body, url, mint)
        status = "bought" if mode == "PAPER" else "sent"
        if not pushed and mode == "LIVE":
            self._signal(mint, now, fresh_price, size, url, mode, "skipped", "push failed")
            log.warning("push failed")
            return {"mint": mint, "status": "skipped", "reason": "push failed", "link": url, "mode": mode}
        if not pushed:
            log.warning("push failed")
        self._signal(mint, now, fresh_price, size, url, mode, "sent" if mode == "LIVE" else "bought", None)
        self.sent.add(mint)
        if mode == "PAPER":
            self._fill(mint, name, now, float(fresh_price), size, pct, stop, mode)
            status = "bought"
            self.desk.bus.publish("fill", {"agent": "executor", "mint": mint, "price": float(fresh_price), "sizeUsd": size, "mode": mode})
        else:
            self.pending[mint] = {"at": now, "price": float(fresh_price), "size": size, "name": name, "pct": pct}
            status = "sent"
        log.info("mint=%s stack=%s raw=%s final=%s mode=%s status=%s link=%s", mint, stack, stack * db.get_float("stack_pct") / 100, size, mode, status, url)
        result = {
            "agent": "executor",
            "mint": mint,
            "signalPrice": float(fresh_price),
            "sizeUsd": size,
            "stackPct": pct,
            "link": url,
            "mode": mode,
            "status": status,
            "reason": None,
            "stop_price": float(fresh_price) * (1 - stop / 100.0),
        }
        self.desk.bus.publish("signal", {key: result[key] for key in ("agent", "mint", "signalPrice", "sizeUsd", "stackPct", "link", "mode", "status", "reason")})
        return result

    def mark_bought(self, mint: str, now: float | None = None) -> dict:
        now = self.desk.now() if now is None else now
        pending = self.pending.get(mint)
        if not pending:
            return {"status": "missed", "reason": "no pending signal"}
        window = db.get_float("confirm_window_min") * 60.0
        if now - pending["at"] > window:
            self.pending.pop(mint, None)
            db.execute("UPDATE signals SET status = 'missed' WHERE mint = ? AND status = 'sent'", (mint,))
            return {"status": "missed"}
        stop = db.get_float("stop_pct")
        self._fill(mint, pending["name"], now, pending["price"], pending["size"], pending["pct"], stop, "LIVE")
        db.execute("UPDATE signals SET status = 'bought' WHERE mint = ? AND status = 'sent'", (mint,))
        self.pending.pop(mint, None)
        return {"status": "bought"}

    def mark_skip(self, mint: str, now: float | None = None) -> None:
        now = self.desk.now() if now is None else now
        self.pending.pop(mint, None)
        db.execute(
            'INSERT INTO skips (mint, "until") VALUES (?, ?)',
            (mint, _next_report(now)),
        )
        db.execute("UPDATE signals SET status = 'skipped' WHERE mint = ?", (mint,))

    def _expire_live(self) -> None:
        window = db.get_float("confirm_window_min") * 60.0
        now = self.desk.now()
        for mint, pending in list(self.pending.items()):
            if now - pending["at"] > window:
                self.pending.pop(mint, None)
                db.execute("UPDATE signals SET status = 'missed' WHERE mint = ? AND status = 'sent'", (mint,))

    def _fill(self, mint, name, now, price, size, pct, stop_pct, mode) -> None:
        stop_price = price * (1 - stop_pct / 100.0)
        db.execute(
            """
            INSERT INTO positions (
                mint, opened_at, entry_price, size_usd, peak_price, stop_price, trail_active,
                closed_at, exit_price, exit_reason, pnl_usd, fees_usd, mode
            ) VALUES (?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, NULL, NULL, ?)
            """,
            (mint, now, price, size, price, stop_price, mode),
        )
        clock = datetime.fromtimestamp(now, timezone.utc).strftime("%H:%M")
        db.record_activity(
            now, "bought", mint, "executor",
            f"Bought {name}", f"Executor · {clock}", f"${size:.2f} · {pct:.2f}% of stack",
        )
        self.desk.bus.risk.put_nowait({"mint": mint, "name": name, "price": price})

    def _signal(self, mint, now, price, size, url, mode, status, reason) -> None:
        db.execute(
            """
            INSERT INTO signals (mint, ts, price, size_usd, stack_pct, link, mode, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (mint, now, price, size, db.get_float("stack_pct"), url, mode if mode in ("PAPER", "LIVE") else "PAPER", status if status != "skipped" else "skipped"),
        )

    def _push(self, title, body, url, mint) -> bool:
        buttons = [
            {"label": "Buy on FOMO", "url": url},
            {"label": "Bought", "callback": f"bought:{mint}"},
            {"label": "Skip", "callback": f"skip:{mint}"},
        ]
        # Telegram is not built. The notifier sink is the channel. Three tries.
        for _ in range(3):
            try:
                self.desk.notifier.push(title, body, url, buttons)
                return True
            except Exception:
                continue
        return False


def _recheck(scanner_liq, fresh_liq, analyst_price, fresh_price) -> str | None:
    if scanner_liq and fresh_liq is not None:
        drop = (float(scanner_liq) - float(fresh_liq)) / float(scanner_liq) * 100.0
        if drop > db.get_float("recheck_liq_drop_pct"):
            return "liquidity dropping"
    if analyst_price and fresh_price is not None and float(analyst_price) > 0:
        rise = (float(fresh_price) - float(analyst_price)) / float(analyst_price) * 100.0
        if rise > db.get_float("recheck_price_up_pct"):
            return "ran before the push"
    return None


def _fmt(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.1f}"


def _next_report(now: float) -> float:
    """Skips last until the next report_time. The morning page itself is STEP 12."""
    import datetime as dt
    clock = db.get_setting("report_time", "08:00") or "08:00"
    hour, minute = (int(part) for part in clock.split(":"))
    moment = dt.datetime.fromtimestamp(now, dt.timezone.utc)
    target = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= moment:
        target = target + dt.timedelta(days=1)
    return target.timestamp()
