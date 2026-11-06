"""Risk, agent 5 of 5. Exit rules and the Sell button.

Never places an order. PAPER closes in the database at the polled price.
LIVE marks the position exiting and waits for the owner's Sold tap.

Stop price is always below entry until a trail is armed. The trail line only
moves up and never sits below entry. A looser stop setting does not move an
open stop down. Exit price is the polled price, even if it gapped past the line.
"""

from __future__ import annotations

import logging
from typing import Any

import db
import fees
import links
from bus import Desk

log = logging.getLogger("risk")


def hard_stop(entry: float, stop_pct: float) -> float:
    return entry * (1 - stop_pct / 100.0)


def trail_line(entry: float, peak: float, arm_pct: float, trail_pct: float, previous: float | None) -> float | None:
    """Arm at +arm_pct. The line ratchets up and never falls below entry."""
    if entry <= 0 or peak < entry * (1 + arm_pct / 100.0):
        return previous if previous is not None else None
    proposed = max(entry, peak * (1 - trail_pct / 100.0))
    if previous is None:
        return proposed
    return max(previous, proposed)


class Risk:
    def __init__(self, desk: Desk) -> None:
        self.desk = desk
        self.state: dict[str, dict] = {}

    async def run(self) -> None:
        import asyncio
        while True:
            db.beat("risk", self.desk.now())
            await self.poll_open()
            await asyncio.sleep(db.get_float("risk_poll_s"))

    async def poll_open(self) -> list[dict]:
        db.beat("risk", self.desk.now())
        rows = db.query(
            """
            SELECT * FROM positions
            WHERE closed_at IS NULL
            """
        )
        results = []
        for row in rows:
            results.append(self.poll_position(dict(row), self.desk.now()))
        return results

    def poll_position(self, row: dict, now: float, price: float | None = None, liquidity: float | None = None) -> dict:
        """One poll. `price` None means this poll had no price."""
        db.beat("risk", now)
        mint = row["mint"]
        state = self._state(row)
        entry = float(row["entry_price"] or 0)
        name = state["name"]
        url = links.coin_url(mint, "solana")
        mode = row.get("mode") or "PAPER"

        if row.get("exit_reason") == "exiting":
            self._remind(row, state, now, url, name)
            return {"action": "exiting", "mint": mint}

        if price is None:
            state["miss"] = state.get("miss", 0) + 1
            if state["miss"] == int(db.get_float("no_price_polls") or 4):
                self.desk.notifier.push(
                    f"{name}: no price for 1 min",
                    "Risk · no price",
                    url,
                    _sell_buttons(url, mint),
                )
            return {"action": "no-price", "mint": mint, "miss": state["miss"]}

        state["miss"] = 0
        if liquidity is not None and float(liquidity) <= 0:
            return self._exit(row, state, now, state.get("last_price") or price, "pulled", url, name)

        state["last_price"] = price
        peak = max(float(row["peak_price"] or entry), price, state.get("peak") or entry)
        state["peak"] = peak
        self._tighten(state, entry)
        armed = trail_line(
            entry, peak, db.get_float("trail_arm_pct"), db.get_float("trail_pct"), state.get("trail_line"),
        )
        if armed is not None:
            state["trail_line"] = armed
        exit_line = state["trail_line"] if state.get("trail_line") is not None else hard_stop(entry, state["stop_pct"])
        db.execute(
            """
            UPDATE positions
            SET peak_price = ?, stop_price = ?, trail_active = ?
            WHERE id = ?
            """,
            (peak, exit_line, 1 if state.get("trail_line") is not None else 0, row["id"]),
        )
        db.execute(
            "INSERT INTO prices (mint, ts, price, volume_m5) VALUES (?, ?, ?, NULL)",
            (mint, now, price),
        )
        log.info(
            "poll %s price=%s peak=%s stop=%s trail=%s",
            mint, price, peak, hard_stop(entry, state["stop_pct"]), state.get("trail_line"),
        )
        if price <= exit_line:
            reason = "trail" if state.get("trail_line") is not None else "stop"
            return self._exit(row, state, now, price, reason, url, name)
        timeout_s = db.get_float("timeout_h") * 3600.0
        if now - float(row["opened_at"]) >= timeout_s:
            return self._exit(row, state, now, price, "timeout", url, name)
        return {"action": "hold", "mint": mint, "exit_line": exit_line, "stop_pct": state["stop_pct"]}

    def mark_sold(self, mint: str, now: float | None = None) -> None:
        """Owner tapped Sold. Still no order; this only closes the row."""
        now = self.desk.now() if now is None else now
        db.execute(
            """
            UPDATE positions
            SET closed_at = ?, exit_reason = COALESCE(NULLIF(exit_reason, 'exiting'), exit_reason)
            WHERE mint = ? AND closed_at IS NULL
            """,
            (now, mint),
        )

    def _state(self, row: dict) -> dict:
        mint = row["mint"]
        if mint in self.state:
            return self.state[mint]
        entry = float(row["entry_price"] or 0)
        stop_price = float(row["stop_price"] or 0)
        if entry and stop_price and not row.get("trail_active"):
            stop_pct = (1 - stop_price / entry) * 100.0
        else:
            stop_pct = db.get_float("stop_pct")
        coin = db.query("SELECT symbol, name FROM coins WHERE mint = ?", (mint,))
        name = mint
        if coin:
            name = coin[0]["symbol"] or coin[0]["name"] or mint
        self.state[mint] = {
            "stop_pct": stop_pct,
            "name": name,
            "miss": 0,
            "peak": float(row["peak_price"] or entry),
            "trail_line": float(row["stop_price"]) if row.get("trail_active") else None,
            "last_price": float(row["peak_price"] or entry),
            "reminded": False,
        }
        return self.state[mint]

    def _tighten(self, state: dict, entry: float) -> None:
        setting = db.get_float("stop_pct")
        if setting < state["stop_pct"] - 1e-9:
            state["stop_pct"] = setting
            if state.get("trail_line") is None:
                # Tighter only. A larger stop_pct (looser) never reaches here.
                return

    def _exit(self, row, state, now, price, reason, url, name) -> dict:
        entry = float(row["entry_price"] or 0)
        size = float(row["size_usd"] or 0)
        pnl, fee = fees.pnl_usd(size, entry, price)
        pct = ((price - entry) / entry * 100.0) if entry else 0.0
        mode = (row.get("mode") or "PAPER").upper()
        stop_pct = state["stop_pct"]
        title, kind, subtitle = _copy(reason, name, pnl, pct, stop_pct)
        if mode == "PAPER":
            db.execute(
                """
                UPDATE positions
                SET closed_at = ?, exit_price = ?, exit_reason = ?, pnl_usd = ?, fees_usd = ?,
                    peak_price = ?, stop_price = ?, trail_active = ?
                WHERE id = ?
                """,
                (
                    now, price, reason, pnl, fee, state.get("peak") or price,
                    state.get("trail_line") or hard_stop(entry, stop_pct),
                    1 if state.get("trail_line") is not None else 0,
                    row["id"],
                ),
            )
            status = "closed"
        else:
            db.execute(
                """
                UPDATE positions
                SET exit_price = ?, exit_reason = 'exiting', pnl_usd = ?, fees_usd = ?
                WHERE id = ?
                """,
                (price, pnl, fee, row["id"]),
            )
            state["exit_at"] = now
            state["reason"] = reason
            status = "exiting"
        db.record_activity(now, kind, row["mint"], "risk", title, subtitle, f"{pnl:.2f}")
        self.desk.notifier.push(title, subtitle, url, _sell_buttons(url, row["mint"]))
        return {
            "action": reason,
            "mint": row["mint"],
            "status": status,
            "exit_price": price,
            "pnl_usd": pnl,
            "fees_usd": fee,
            "pct": pct,
            "stop_pct": stop_pct,
            "link": url,
        }

    def _remind(self, row, state, now, url, name) -> None:
        wait = db.get_float("sold_remind_min") * 60.0
        if state.get("reminded"):
            return
        if state.get("exit_at") is None or now - state["exit_at"] < wait:
            return
        state["reminded"] = True
        self.desk.notifier.push(
            f"{name} still open",
            "still open",
            url,
            _sell_buttons(url, row["mint"]),
        )


def _copy(reason: str, name: str, pnl: float, pct: float, stop_pct: float) -> tuple[str, str, str]:
    if reason == "stop":
        loss = abs(pnl)
        title = f"{name} hit the -{_num(stop_pct)}% stop · -${loss:.0f}"
        return title, "stopped", f"Risk · -{_num(stop_pct)}% stop"
    if reason == "timeout":
        title = f"Sell {name}, dead money · {pct:.0f}% in {_num(db.get_float('timeout_h'))}h"
        return title, "sold", "Risk · timeout"
    if reason == "pulled":
        return f"Sold {name} · liquidity pulled", "sold", "Risk · pulled"
    sign = "+" if pnl >= 0 else "-"
    title = f"Sold {name} · {sign}${abs(pnl):.0f} ({pct:+.0f}%)"
    return title, "sold", "Risk · trailing stop"


def _sell_buttons(url: str, mint: str) -> list[dict]:
    return [
        {"label": "Sell on FOMO", "url": url},
        {"label": "Sold", "callback": f"sold:{mint}"},
    ]


def _num(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.1f}"
