"""Risk, agent 5 of 5. Exits and the Sell button. Never an order.

Every poll, in order: peak, stop, arm trail, trail exit, timeout.
The trail flag never turns off. A looser stop setting does not move an open
stop down. Exit price is the polled price, even if it gapped past the line.
"""

from __future__ import annotations

import logging

import db
import fees
import links
from bus import Desk, set_risk_price_mints

log = logging.getLogger("risk")

# "No price for [4] polls" is a bracket in the agent text, not a STEP 10 key.
NO_PRICE_POLLS = 4
# "429: back off [30]s" is the same.
BACKOFF_S = 30


class Risk:
    def __init__(self, desk: Desk) -> None:
        self.desk = desk
        self.state: dict[str, dict] = {}

    async def run(self) -> None:
        import asyncio
        while True:
            db.beat("risk", self.desk.now())
            try:
                await self.poll_open()
            except Exception as exc:
                if "429" in str(exc):
                    log.warning("429; backing off %ss, missed polls", BACKOFF_S)
                    await asyncio.sleep(BACKOFF_S)
            await asyncio.sleep(db.get_float("risk.poll_s"))

    async def poll_open(self) -> list[dict]:
        db.beat("risk", self.desk.now())
        rows = db.query("SELECT * FROM positions WHERE closed_at IS NULL")
        set_risk_price_mints({row["mint"] for row in rows if row["mint"]})
        return [self.poll_position(dict(row), self.desk.now()) for row in rows]

    def poll_position(self, row: dict, now: float, price: float | None = None, liquidity: float | None = None) -> dict:
        db.beat("risk", now)
        mint = row["mint"]
        state = self._state(row)
        entry = float(row["entry_price"] or 0)
        name = state["name"]
        url = links.coin_url(mint, "solana")
        if row.get("closed_at") is None and row.get("exit_price") is not None and (row.get("mode") or "PAPER") == "LIVE":
            self._remind(row, state, now, url, name)
            return {"action": "exiting", "mint": mint}

        if price is None:
            state["miss"] = state.get("miss", 0) + 1
            if state["miss"] == NO_PRICE_POLLS:
                self.desk.notifier.push(
                    f"{name}: no price for 1 min",
                    "Risk · no price",
                    url,
                    _sell_buttons(url, mint),
                )
            return {"action": "no-price", "mint": mint, "miss": state["miss"]}

        state["miss"] = 0
        if liquidity is not None and float(liquidity) <= 0:
            last = state.get("last_price", entry)
            return self._exit(row, state, now, last, "pulled", url, name)

        state["last_price"] = price
        self._tighten(state, entry, row)
        peak = max(float(row["peak_price"] or entry), state.get("peak") or entry, price)
        state["peak"] = peak
        if price >= entry * (1 + db.get_float("trail_start_pct") / 100.0):
            state["trail"] = True
        trail_on = bool(state.get("trail") or row.get("trail_active"))
        state["trail"] = trail_on
        stop_line = entry * (1 - state["stop_pct"] / 100.0)
        trail_line = peak * (1 - db.get_float("trail_pct") / 100.0)
        db.execute(
            "UPDATE positions SET peak_price = ?, stop_price = ?, trail_active = ? WHERE id = ?",
            (peak, stop_line, 1 if trail_on else 0, row["id"]),
        )
        db.execute(
            "INSERT INTO prices (mint, ts, price, volume_m5) VALUES (?, ?, ?, NULL)",
            (mint, now, price),
        )
        action = "hold"
        if price <= stop_line:
            action = "stop"
        elif trail_on and price <= trail_line:
            action = "trail"
        elif now - float(row["opened_at"]) >= db.get_float("timeout_h") * 3600.0 and price < entry * (1 + db.get_float("timeout_min_gain_pct") / 100.0):
            action = "timeout"
        log.info("poll %s price=%s peak=%s stop=%s trail=%s action=%s", mint, price, peak, stop_line, trail_line if trail_on else None, action)
        if action == "hold":
            return {"action": "hold", "mint": mint, "stop_pct": state["stop_pct"], "trail_line": trail_line if trail_on else None}
        return self._exit(row, state, now, price, action, url, name)

    def mark_sold(self, mint: str, now: float | None = None) -> None:
        now = self.desk.now() if now is None else now
        db.execute(
            "UPDATE positions SET closed_at = ? WHERE mint = ? AND closed_at IS NULL",
            (now, mint),
        )

    def _state(self, row: dict) -> dict:
        mint = row["mint"]
        if mint in self.state:
            return self.state[mint]
        entry = float(row["entry_price"] or 0)
        stop_price = float(row["stop_price"] or 0)
        if entry and stop_price:
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
            "trail": bool(row.get("trail_active")),
            "last_price": float(row["peak_price"] or entry),
            "reminded": False,
            "exit_at": None,
            "reason": None,
        }
        return self.state[mint]

    def _tighten(self, state: dict, entry: float, row: dict) -> None:
        setting = db.get_float("stop_pct")
        if setting < state["stop_pct"] - 1e-9:
            state["stop_pct"] = setting
            row["stop_price"] = entry * (1 - setting / 100.0)

    def _exit(self, row, state, now, price, reason, url, name) -> dict:
        entry = float(row["entry_price"] or 0)
        size = float(row["size_usd"] or 0)
        pnl, fee = fees.pnl_usd(size, entry, price)
        pct = ((price - entry) / entry * 100.0) if entry else 0.0
        mode = (row.get("mode") or "PAPER").upper()
        title, kind, subtitle, value = _copy(reason, name, pnl, pct, state["stop_pct"])
        peak = state.get("peak") or price
        if mode == "PAPER":
            db.execute(
                """
                UPDATE positions
                SET closed_at = ?, exit_price = ?, exit_reason = ?, pnl_usd = ?, fees_usd = ?,
                    peak_price = ?, trail_active = ?
                WHERE id = ?
                """,
                (now, price, reason, pnl, fee, peak, 1 if state.get("trail") else 0, row["id"]),
            )
            status = "closed"
        else:
            db.execute(
                """
                UPDATE positions
                SET exit_price = ?, exit_reason = ?, pnl_usd = ?, fees_usd = ?, peak_price = ?, trail_active = ?
                WHERE id = ?
                """,
                (price, reason, pnl, fee, peak, 1 if state.get("trail") else 0, row["id"]),
            )
            state["exit_at"] = now
            status = "exiting"
        db.record_activity(now, kind, row["mint"], "risk", title, subtitle, value)
        self.desk.notifier.push(title, subtitle, url, _sell_buttons(url, row["mint"]))
        return {
            "agent": "risk",
            "mint": row["mint"],
            "name": name,
            "reason": reason,
            "action": reason,
            "entryPrice": entry,
            "exitPrice": price,
            "exit_price": price,
            "peakPrice": peak,
            "pnlUsd": pnl,
            "pnl_usd": pnl,
            "pct": pct,
            "feesUsd": fee,
            "fees_usd": fee,
            "stopPct": state["stop_pct"],
            "stop_pct": state["stop_pct"],
            "mode": mode,
            "status": status,
            "link": url,
        }

    def _remind(self, row, state, now, url, name) -> None:
        wait = db.get_float("risk.confirm_window_min") * 60.0
        if state.get("reminded") or state.get("exit_at") is None:
            return
        if now - state["exit_at"] < wait:
            return
        state["reminded"] = True
        self.desk.notifier.push(f"{name} still open", "still open", url, _sell_buttons(url, row["mint"]))


def _copy(reason: str, name: str, pnl: float, pct: float, stop_pct: float) -> tuple[str, str, str, str]:
    if reason == "stop":
        loss = abs(pnl)
        title = f"{name} hit the -{_num(stop_pct)}% stop · -${loss:.0f}"
        return title, "stopped", f"Risk · -{_num(stop_pct)}% stop", f"-${loss:.0f}"
    if reason == "timeout":
        title = f"Sell {name}, dead money · {pct:.0f}% in {_num(db.get_float('timeout_h'))}h"
        return title, "sold", "Risk · timeout", title
    if reason == "pulled":
        return f"Sold {name} · liquidity pulled", "sold", "Risk · pulled", f"{pnl:.2f}"
    title = f"Sold {name} · +${pnl:.0f} ({pct:+.0f}%)"
    return title, "sold", "Risk · trailing stop", f"+${pnl:.0f} / {pct:+.0f}%"


def _sell_buttons(url: str, mint: str) -> list[dict]:
    return [
        {"label": "Sell on FOMO", "url": url},
        {"label": "Sold", "callback": f"sold:{mint}"},
    ]


def _num(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.1f}"
