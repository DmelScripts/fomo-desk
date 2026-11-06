"""In-process queues. Scanner -> Auditor -> Analyst -> Executor -> Risk."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable


class Bus:
    def __init__(self) -> None:
        self.auditor: asyncio.Queue = asyncio.Queue()
        self.analyst: asyncio.Queue = asyncio.Queue()
        self.executor: asyncio.Queue = asyncio.Queue()
        self.risk: asyncio.Queue = asyncio.Queue()
        self.events: list[dict[str, Any]] = []

    def publish(self, topic: str, payload: Any) -> None:
        self.events.append({"topic": topic, "payload": payload})


class Desk:
    """Shared handles. Agents do not call each other except through the bus."""

    def __init__(
        self,
        http: Any,
        notifier: Any,
        bus: Bus | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.http = http
        self.notifier = notifier
        self.bus = bus or Bus()
        self.now = now or time.time


def is_rpc_error(payload: Any) -> bool:
    return isinstance(payload, dict) and bool(payload.get("error"))


async def call_with_retries(
    fn: Callable[[], Awaitable[Any]],
    tries: int,
    delay: float,
    sleep: Callable[[float], Awaitable[None]],
) -> Any:
    """Retry a source `tries` times, `delay` seconds apart. None means no data."""
    last = None
    for attempt in range(max(1, tries)):
        try:
            last = await fn()
        except Exception:
            last = None
        if last is not None and not is_rpc_error(last):
            return last
        if attempt < tries - 1:
            await sleep(delay)
    return None
