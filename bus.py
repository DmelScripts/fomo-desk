"""In-process queues. Scanner -> Auditor -> Analyst -> Executor -> Risk."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable


class EventHub:
    """Process-wide SSE fanout. Payloads must not contain secrets."""

    def __init__(self) -> None:
        self.subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers.discard(queue)

    def publish(self, event: str, payload: Any) -> None:
        stale = []
        for queue in self.subscribers:
            try:
                queue.put_nowait((event, payload))
            except asyncio.QueueFull:
                stale.append(queue)
        for queue in stale:
            self.subscribers.discard(queue)


hub = EventHub()

# Mints whose prices are owned by Scanner or Risk. The API writer must skip them.
_scanner_price_mints: set[str] = set()
_risk_price_mints: set[str] = set()


def set_scanner_price_mints(mints: set[str]) -> None:
    global _scanner_price_mints
    _scanner_price_mints = set(mints)


def set_risk_price_mints(mints: set[str]) -> None:
    global _risk_price_mints
    _risk_price_mints = set(mints)


def covered_price_mints() -> set[str]:
    return set(_scanner_price_mints) | set(_risk_price_mints)


class Bus:
    def __init__(self) -> None:
        self.auditor: asyncio.Queue = asyncio.Queue()
        self.analyst: asyncio.Queue = asyncio.Queue()
        self.executor: asyncio.Queue = asyncio.Queue()
        self.risk: asyncio.Queue = asyncio.Queue()
        self.events: list[dict[str, Any]] = []

    def publish(self, topic: str, payload: Any) -> None:
        self.events.append({"topic": topic, "payload": payload})
        hub.publish(topic, payload)


class Desk:
    """Shared handles. Agents do not call each other except through the bus."""

    def __init__(
        self,
        http: Any,
        notifier: Any,
        bus: Bus | None = None,
        now: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.http = http
        self.notifier = notifier
        self.bus = bus or Bus()
        self.now = now or time.time
        self.sleep = sleep or asyncio.sleep


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
