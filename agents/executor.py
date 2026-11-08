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


def _paused(name: str) -> bool:
    from api import is_paused
    return is_paused(name)


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
    return (
        f"every coin will be skipped, stack under ${min_stack:.2f}. "
        "Switch below_min to bump, or raise the stack."
    )


def paper_balance() -> float:
    """Simulated equity. Cash plus open marks. No invented price."""
    return paper_books()["equity"]


def _entry_fee(row) -> float:
    stored = row["entry_fee_usd"] if "entry_fee_usd" in row.keys() else None
    if stored is not None:
        return float(stored)
    import fees
    return fees.fee_usd(float(row["size_usd"] or 0))


def _last_price(mint: str):
    rows = db.query(
        "SELECT price FROM prices WHERE mint = ? ORDER BY ts DESC LIMIT 1",
        (mint,),
    )
    if not rows or rows[0]["price"] is None:
        return None
    return float(rows[0]["price"])


def paper_books() -> dict:
    """PAPER cash is what can be spent. Equity adds a mark only when a price exists."""
    import fees
    start = db.get_float("paper_start_stack")
    closed = db.query(
        "SELECT COALESCE(SUM(pnl_usd), 0) AS n FROM positions WHERE mode = 'PAPER' AND closed_at IS NOT NULL"
    )
    realized = float(closed[0]["n"] or 0)
    opens = db.query(
        "SELECT * FROM positions WHERE mode = 'PAPER' AND closed_at IS NULL"
    )
    reserved = 0.0
    marks = 0.0
    unrealized = 0.0
    priced = True
    for row in opens:
        size = float(row["size_usd"] or 0)
        entry_fee = _entry_fee(row)
        reserved += size + entry_fee
        price = _last_price(row["mint"])
        entry = float(row["entry_price"] or 0)
        if price is None or entry <= 0:
            priced = False
            marks += size
            continue
        sold = fees.exit_notional(size, entry, price)
        exit_fee = fees.fee_usd(sold)
        marks += sold - exit_fee
        unrealized += (sold - exit_fee) - (size + entry_fee)
    cash = start + realized - reserved
    return {
        "cash": cash,
        "equity": cash + marks,
        "realized": realized,
        "unrealized": None if opens and not priced else unrealized,
    }


def empty_night_reason(mode: str | None = None) -> str | None:
    """Skip-all configuration must not start a night. Bump is allowed."""
    mode = (mode or db.get_setting("mode", "PAPER") or "PAPER").upper()
    below = db.get_setting("below_min", "bump") or "bump"
    if str(below).lower() != "skip":
        return None
    if mode == "LIVE" and db.get_setting("stack"):
        stack = db.get_float("stack")
    else:
        stack = db.get_float("paper_start_stack")
    return ticket_warning(
        stack,
        db.get_float("stack_pct"),
        db.get_float("min_ticket_usd"),
        below,
    )


def paper_night_status() -> dict:
    """Fail closed. Nothing the client sends can set done."""
    closed = db.query(
        "SELECT COUNT(*) AS n FROM positions WHERE mode = 'PAPER' AND closed_at IS NOT NULL"
    )[0]["n"]
    return {
        "done": False,
        "reason": "A full PAPER night is not defined. One closed trade is not enough.",
        "closed_paper_trades": int(closed or 0),
    }


def open_notional(mode: str) -> float:
    rows = db.query(
        "SELECT COALESCE(SUM(size_usd), 0) AS n FROM positions WHERE mode = ? AND closed_at IS NULL",
        (mode,),
    )
    return float(rows[0]["n"] or 0)


def paper_night_done() -> bool:
    """Fail closed. STEP 11 requires one full PAPER night before LIVE.

    The prompt does not define how long a night is. This does not invent
    one, and it does not read a client flag. A closed trade is not proof.
    """
    return False


def _already_filled(mint: str) -> bool:
    if not mint:
        return False
    return bool(db.query(
        "SELECT id FROM signals WHERE mint = ? AND status IN ('sent', 'bought')",
        (mint,),
    ))


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
        self._recover()

    def _recover(self) -> None:
        """A restart must not fill a mint that already has a push or a position."""
        try:
            rows = db.query(
                "SELECT mint, status, ts, price, size_usd, stack_pct FROM signals"
            )
        except Exception:
            return
        for row in rows:
            if row["status"] == "reserved":
                db.execute(
                    "UPDATE signals SET status = 'skipped' WHERE mint = ? AND status = 'reserved'",
                    (row["mint"],),
                )
                continue
            if row["status"] in ("sent", "bought") and row["mint"]:
                self.sent.add(row["mint"])
            if row["status"] == "sent" and row["mint"] and row["price"] is not None and row["size_usd"] is not None:
                self.pending[row["mint"]] = {
                    "at": float(row["ts"] or 0),
                    "price": float(row["price"]),
                    "size": float(row["size_usd"]),
                    "name": row["mint"],
                    "pct": float(row["stack_pct"] or 0),
                }

    async def run(self) -> None:
        import asyncio
        self._warn()
        while True:
            db.beat("executor", self.desk.now())
            if _paused("executor"):
                await self.desk.sleep(0.05)
                continue
            if empty_night_reason():
                await self.desk.sleep(1)
                continue
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
        if mint in self.sent or _already_filled(mint):
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
        blocked = empty_night_reason(mode)
        if blocked:
            return {
                "agent": "executor",
                "mint": mint,
                "status": "skipped",
                "reason": blocked,
                "link": links.coin_url(mint, "solana"),
                "mode": mode if mode in ("PAPER", "LIVE") else "PAPER",
            }
        if mode == "PAPER":
            stack = db.get_float("paper_start_stack")
            free = paper_books()["cash"]
        else:
            stack = db.get_float("stack") if db.get_setting("stack") else db.get_float("paper_start_stack")
            free = stack - open_notional("LIVE")
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
            self._signal(mint, now, fresh_price, None, url, mode, "skipped", reason, None, None)
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
        taken = self._take(mint, name, now, float(fresh_price), size, pct, stop, mode, title, body, url)
        if taken != "ok":
            self.sent.add(mint)
            return {"mint": mint, "status": "skipped", "reason": taken, "link": url, "mode": mode}
        pushed = self._push(title, body, url, mint)
        status = "bought" if mode == "PAPER" else "sent"
        if not pushed and mode == "LIVE":
            self._release(mint, "push failed")
            log.warning("push failed")
            return {"mint": mint, "status": "skipped", "reason": "push failed", "link": url, "mode": mode}
        if not pushed:
            log.warning("push failed")
        self.sent.add(mint)
        if mode == "PAPER":
            self.desk.bus.publish("fill", {"agent": "executor", "mint": mint, "price": float(fresh_price), "sizeUsd": size, "mode": mode})
        else:
            self.pending[mint] = {"at": now, "price": float(fresh_price), "size": size, "name": name, "pct": pct}
            status = "sent"
        import token_execution
        chain = token_execution.supported_buy(mint)
        result_chain = chain
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
            "chain": result_chain,
            "chain_send": False,
        }
        self.desk.bus.publish("signal", {key: result[key] for key in ("agent", "mint", "signalPrice", "sizeUsd", "stackPct", "link", "mode", "status", "reason")})
        return result

    def _take(self, mint, name, now, price, size, pct, stop_pct, mode, title, body, url) -> str:
        """Claim the mint and the cash in one transaction. A retry cannot fill twice."""
        import fees
        entry_fee = fees.fee_usd(size) if mode == "PAPER" else 0.0
        stop_price = price * (1 - stop_pct / 100.0)
        stored_mode = mode if mode in ("PAPER", "LIVE") else "PAPER"

        def work(conn):
            existing = conn.execute(
                "SELECT id FROM signals WHERE mint = ? AND status IN ('reserved', 'sent', 'bought')",
                (mint,),
            ).fetchone()
            if existing:
                return "already sent"
            if conn.execute(
                "SELECT id FROM positions WHERE mint = ? AND closed_at IS NULL",
                (mint,),
            ).fetchone():
                return "already sent"
            if stored_mode == "PAPER":
                start = conn.execute(
                    "SELECT value FROM settings WHERE key = 'paper_start_stack'"
                ).fetchone()
                realized = conn.execute(
                    "SELECT COALESCE(SUM(pnl_usd), 0) FROM positions WHERE mode = 'PAPER' AND closed_at IS NOT NULL"
                ).fetchone()[0]
                spent = 0.0
                for row in conn.execute(
                    "SELECT size_usd, entry_fee_usd FROM positions WHERE mode = 'PAPER' AND closed_at IS NULL"
                ):
                    row_size = float(row["size_usd"] or 0)
                    row_fee = row["entry_fee_usd"]
                    spent += row_size + (float(row_fee) if row_fee is not None else fees.fee_usd(row_size))
                cash = float(start["value"]) + float(realized) - spent
                if cash + 1e-9 < size + entry_fee:
                    return "no free stack"
                status = "bought"
            else:
                status = "sent"
            conn.execute(
                """
                INSERT INTO signals (
                    mint, ts, price, size_usd, stack_pct, link, mode, status, push_title, push_body
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (mint, now, price, size, db.get_float("stack_pct"), url, stored_mode, status, title, body),
            )
            if stored_mode == "PAPER":
                conn.execute(
                    """
                    INSERT INTO positions (
                        mint, opened_at, entry_price, size_usd, peak_price, stop_price, trail_active,
                        closed_at, exit_price, exit_reason, pnl_usd, fees_usd, mode, entry_fee_usd
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, NULL, NULL, ?, ?)
                    """,
                    (mint, now, price, size, price, stop_price, stored_mode, entry_fee),
                )
            return "ok"

        outcome = db.exclusive(work)
        if outcome != "ok":
            return outcome
        if stored_mode == "PAPER":
            clock = datetime.fromtimestamp(now, timezone.utc).strftime("%H:%M")
            db.record_activity(
                now, "bought", mint, "executor",
                f"Bought {name}", f"Executor · {clock}", f"${size:.2f} · {pct:.2f}% of stack",
            )
            self.desk.bus.risk.put_nowait({"mint": mint, "name": name, "price": price})
        return "ok"

    def _release(self, mint: str, reason: str) -> None:
        db.execute(
            "UPDATE signals SET status = 'skipped' WHERE mint = ? AND status = 'sent'",
            (mint,),
        )
        self.sent.discard(mint)
        self.pending.pop(mint, None)

    def mark_bought(self, mint: str, now: float | None = None) -> dict:
        now = self.desk.now() if now is None else now
        if db.query("SELECT id FROM positions WHERE mint = ? AND closed_at IS NULL", (mint,)):
            return {"status": "open", "reason": "already open"}
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
        import fees
        stop_price = price * (1 - stop_pct / 100.0)
        entry_fee = fees.fee_usd(size)
        db.execute(
            """
            INSERT INTO positions (
                mint, opened_at, entry_price, size_usd, peak_price, stop_price, trail_active,
                closed_at, exit_price, exit_reason, pnl_usd, fees_usd, mode, entry_fee_usd
            ) VALUES (?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, NULL, NULL, ?, ?)
            """,
            (mint, now, price, size, price, stop_price, mode, entry_fee),
        )
        clock = datetime.fromtimestamp(now, timezone.utc).strftime("%H:%M")
        db.record_activity(
            now, "bought", mint, "executor",
            f"Bought {name}", f"Executor · {clock}", f"${size:.2f} · {pct:.2f}% of stack",
        )
        self.desk.bus.risk.put_nowait({"mint": mint, "name": name, "price": price})

    def _signal(self, mint, now, price, size, url, mode, status, reason, push_title=None, push_body=None) -> None:
        db.execute(
            """
            INSERT INTO signals (
                mint, ts, price, size_usd, stack_pct, link, mode, status, push_title, push_body
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mint, now, price, size, db.get_float("stack_pct"), url,
                mode if mode in ("PAPER", "LIVE") else "PAPER",
                status if status != "skipped" else "skipped",
                push_title, push_body,
            ),
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
    """Skips last until the next Vietnam report_time. The page itself is STEP 12."""
    from config import report_boundary
    clock = db.get_setting("report_time", "08:00") or "08:00"
    return report_boundary(now, clock, upcoming=True)
