"""Executor, agent 4 of 5. Sizes a ticket and sends one Buy button.

This module never places an order, never holds a key, and never touches funds.
PAPER writes a simulated position. LIVE only pushes a Buy link and waits for
the owner's tap. LIVE is refused until a closed PAPER position exists and the
caller passes the exact string LIVE.

The executor section of the prompt file is missing. Sizing follows the
surviving test paragraph, seeded as stack_pct 8, min_ticket_usd 47.50,
undersize bump, max_pool_pct 2:

    raw = stack * stack_pct / 100
    under the minimum: bump raises it to the minimum, skip skips the coin
    then cap at liquidity * max_pool_pct / 100
"""

from __future__ import annotations

import logging

import db
import links
from bus import Desk

log = logging.getLogger("executor")


def size_ticket(
    stack_usd: float,
    stack_pct: float,
    min_ticket_usd: float,
    undersize: str,
    liquidity_usd: float | None,
    max_pool_pct: float,
) -> tuple[float | None, str | None]:
    raw = stack_usd * stack_pct / 100.0
    if raw < min_ticket_usd:
        if str(undersize).lower() == "skip":
            return None, "skip"
        size = min_ticket_usd
    else:
        size = raw
    if liquidity_usd is not None and max_pool_pct >= 0:
        cap = liquidity_usd * max_pool_pct / 100.0
        if size > cap:
            size = cap
    if size < min_ticket_usd:
        return None, "skip"
    return round(size, 2), None


def startup_warning(stack_usd: float, stack_pct: float, min_ticket_usd: float, undersize: str) -> str | None:
    raw = stack_usd * stack_pct / 100.0
    if str(undersize).lower() == "skip" and raw < min_ticket_usd:
        return "every coin will be skipped"
    return None


def paper_night_done() -> bool:
    rows = db.query(
        """
        SELECT COUNT(*) AS n FROM positions
        WHERE mode = 'PAPER' AND closed_at IS NOT NULL
        """
    )
    return bool(rows and rows[0]["n"])


def allow_live(typed: str | None) -> bool:
    """LIVE only after a closed PAPER position exists and the owner typed LIVE."""
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

    async def run(self) -> None:
        import asyncio
        warning = startup_warning(
            db.get_float("stack_usd"),
            db.get_float("stack_pct"),
            db.get_float("min_ticket_usd"),
            db.get_setting("undersize", "bump") or "bump",
        )
        if warning:
            log.warning("%s", warning)
            self.desk.notifier.push("Executor", warning, links.SIGNUP_URL, [])
        while True:
            db.beat("executor", self.desk.now())
            try:
                payload = await asyncio.wait_for(self.desk.bus.executor.get(), timeout=1)
            except asyncio.TimeoutError:
                continue
            await self.handle(payload)

    async def handle(self, payload: dict) -> dict:
        db.beat("executor", self.desk.now())
        stack = db.get_float("stack_usd")
        pct = db.get_float("stack_pct")
        minimum = db.get_float("min_ticket_usd")
        undersize = db.get_setting("undersize", "bump") or "bump"
        liquidity = payload.get("liquidity_usd")
        size, skip = size_ticket(
            stack, pct, minimum, undersize, liquidity, db.get_float("max_pool_pct"),
        )
        mint = payload.get("mint")
        name = payload.get("symbol") or payload.get("name") or mint
        url = links.coin_url(mint, "solana")
        now = self.desk.now()
        warning = startup_warning(stack, pct, minimum, undersize)
        if skip or size is None:
            db.execute(
                """
                INSERT INTO signals (
                    mint, ts, price, size_usd, stack_pct, link, mode, status
                ) VALUES (?, ?, ?, ?, ?, ?, 'PAPER', 'skipped')
                """,
                (mint, now, payload.get("price"), None, pct, url),
            )
            db.record_activity(
                now, "rejected", mint, "executor",
                f"Rejected {name}", "Executor · " + (warning or "under the minimum"), "Skipped",
            )
            return {"status": "skipped", "warning": warning, "link": url}
        mode = (db.get_setting("mode", "PAPER") or "PAPER").upper()
        if mode == "LIVE" and not paper_night_done():
            mode = "PAPER"
            log.warning("LIVE refused; no closed PAPER position yet")
        price = payload.get("price") or 0
        stop_pct = db.get_float("stop_pct")
        if mode == "LIVE":
            db.execute(
                """
                INSERT INTO signals (
                    mint, ts, price, size_usd, stack_pct, link, mode, status
                ) VALUES (?, ?, ?, ?, ?, ?, 'LIVE', 'waiting')
                """,
                (mint, now, price, size, pct, url),
            )
            self.desk.notifier.push(
                f"Buy {name}",
                f"Executor · ${size:.2f}",
                url,
                [{"label": "Buy on FOMO", "url": url}],
            )
            log.info("LIVE link only for %s", mint)
            return {"status": "waiting", "mode": "LIVE", "size_usd": size, "link": url}
        stop_price = float(price) * (1 - stop_pct / 100.0) if price else None
        db.execute(
            """
            INSERT INTO signals (
                mint, ts, price, size_usd, stack_pct, link, mode, status
            ) VALUES (?, ?, ?, ?, ?, ?, 'PAPER', 'filled')
            """,
            (mint, now, price, size, pct, url),
        )
        db.execute(
            """
            INSERT INTO positions (
                mint, opened_at, entry_price, size_usd, peak_price, stop_price, trail_active,
                closed_at, exit_price, exit_reason, pnl_usd, fees_usd, mode
            ) VALUES (?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, NULL, NULL, 'PAPER')
            """,
            (mint, now, price, size, price, stop_price),
        )
        db.record_activity(
            now, "bought", mint, "executor",
            f"Bought {name}", f"Executor · ${size:.2f}", f"stop_pct={stop_pct}",
        )
        self.desk.notifier.push(
            f"Buy {name}",
            f"Executor · ${size:.2f}",
            url,
            [{"label": "Buy on FOMO", "url": url}],
        )
        await self.desk.bus.risk.put({"mint": mint, "name": name, "price": price})
        return {"status": "filled", "mode": "PAPER", "size_usd": size, "link": url, "stop_price": stop_price}
