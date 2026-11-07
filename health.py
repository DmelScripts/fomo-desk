"""Point-in-time health for the hourly desk-health routine.

This process does not schedule that routine. A sample older than two
minutes is stale at the moment of the request. Nothing here watches
continuously.
"""

from __future__ import annotations

import asyncio
import time

import db
from db import AGENT_NAMES

STALE_S = 120
HOUR_S = 3600
PROCESS = "process"


class Supervisor:
    """Restarts a task this process already owns. It does not enable agents."""

    def __init__(self) -> None:
        self.tasks: dict[str, asyncio.Task] = {}
        self.factories: dict[str, object] = {}
        self.started: dict[str, float] = {}
        self._lock = asyncio.Lock()

    def register(self, name: str, factory) -> None:
        self.factories[name] = factory

    def running(self, name: str) -> bool:
        task = self.tasks.get(name)
        return task is not None and not task.done()

    async def recover(self, name: str, now: float | None = None) -> dict:
        async with self._lock:
            return await self._recover(name, time.time() if now is None else now)

    async def _recover(self, name: str, now: float) -> dict:
        intended = intended_state(name)
        if intended != "running":
            return {
                "ok": False,
                "restarted": False,
                "agent": name,
                "intended": intended,
                "reason": "not meant to be running",
                "sent": False,
            }
        if name not in self.factories:
            return {
                "ok": False,
                "restarted": False,
                "agent": name,
                "intended": intended,
                "reason": "this process has no task to restart",
                "sent": False,
            }
        age = _age(name, now)
        recent = now - self.started.get(name, 0) <= STALE_S
        heartbeat_fresh = age is not None and age <= STALE_S
        if self.running(name) and (recent or heartbeat_fresh):
            return {
                "ok": True,
                "restarted": False,
                "agent": name,
                "intended": intended,
                "reason": "already running",
                "sent": False,
            }
        current = self.tasks.get(name)
        if current is not None and not current.done():
            current.cancel()
            try:
                await current
            except asyncio.CancelledError:
                pass
        if self.running(name):
            return {
                "ok": True,
                "restarted": False,
                "agent": name,
                "reason": "already running",
                "sent": False,
            }
        self.started[name] = now
        self.tasks[name] = asyncio.create_task(self.factories[name]())
        down_min = None if age is None else max(2, int(round(age / 60.0)))
        span = "unknown" if down_min is None else str(down_min)
        push_text = f"Restarted {name}, down {span} min"
        event_id = _restart_id(name, now)
        db.execute(
            """
            INSERT INTO restart_events (event_id, agent, down_min, push_text, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (event_id, name, down_min, push_text, now),
        )
        return {
            "ok": True,
            "restarted": True,
            "agent": name,
            "intended": intended,
            "down_min": down_min,
            "event_id": event_id,
            "push_text": push_text,
            "sent": False,
        }


supervisor = Supervisor()


def _restart_id(name: str, now: float) -> str:
    import secrets
    return f"restart-{name}-{int(now)}-{secrets.token_hex(4)}"


def _age(name: str, now: float):
    ages = db.heartbeat_ages(now)
    if name == PROCESS:
        return ages.get(PROCESS)
    return ages.get(name)


def intended_state(name: str) -> str:
    """off, paused, or running. Recovery must not change this."""
    if name == PROCESS:
        return "running"
    if not db.agents_enabled():
        return "off"
    from api import is_paused
    if name == "risk":
        # An open position keeps Risk guarding. The pause flag is not cleared.
        if db.query("SELECT id FROM positions WHERE closed_at IS NULL"):
            return "running"
    if is_paused(name):
        return "paused"
    return "running"


def observed(age) -> str:
    if age is None:
        return "missing"
    if age > STALE_S:
        return "stale"
    return "fresh"


def diagnose(now: float | None = None) -> dict:
    now = time.time() if now is None else now
    ages = db.heartbeat_ages(now)
    ages.setdefault(PROCESS, None)
    agents = []
    for name in list(AGENT_NAMES) + [PROCESS]:
        age = ages.get(name)
        intended = intended_state(name)
        state = observed(age)
        agents.append({
            "name": name,
            "intended": intended,
            "observed": state,
            "age_s": age,
            "recoverable": intended == "running" and state in {"stale", "missing"},
        })
    reconnects = db.ws_reconnects_since(now - HOUR_S)
    open_positions = int(db.query(
        "SELECT COUNT(*) AS n FROM positions WHERE closed_at IS NULL"
    )[0]["n"] or 0)
    return {
        "mode": db.get_setting("mode", "PAPER"),
        "agents_enabled": db.agents_enabled(),
        "open_positions": open_positions,
        "stale_after_s": STALE_S,
        "check": "point_in_time",
        "continuous": False,
        "note": "Age is measured at this request. The desk-health routine samples hourly and does not watch every 2 minutes.",
        "agents": agents,
        "websocket": {
            "source": "pumpportal",
            "reconnects_trailing_hour": reconnects,
            "alert": reconnects > 10,
            "window_s": HOUR_S,
        },
        "schedule": _schedule(),
    }


def _schedule() -> dict:
    return {
        "report_time": db.get_setting("report_time", "08:00"),
        "timezone": "Asia/Ho_Chi_Minh",
        "server_reschedules_routines": False,
        "note": "This process does not schedule Grok Bot routines. If report_time changes, update the night-report routine.",
    }


async def process_heartbeat(stop: asyncio.Event) -> None:
    while not stop.is_set():
        db.beat(PROCESS)
        try:
            await asyncio.wait_for(stop.wait(), timeout=30)
            return
        except asyncio.TimeoutError:
            pass
