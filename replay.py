"""Isolated PAPER replay of the live PumpPortal migration stream.

This file does not start a replay when imported. The production web service
does not call it. Start it only with DESK_REPLAY=1 and a dedicated data
directory whose name contains "replay". Stop it with SIGTERM or SIGINT.
The default length is 7200 seconds. Web push stays off unless
DESK_REPLAY_NOTIFY=1.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

import db
from main import run_desk


def guard() -> Path:
    """Return the replay directory, or exit before any socket is opened."""
    if os.environ.get("DESK_REPLAY") != "1":
        raise SystemExit("refusing: set DESK_REPLAY=1 to start a replay")
    raw = os.environ.get("DESK_DATA_DIR") or ""
    if not raw.strip():
        raise SystemExit("refusing: set DESK_DATA_DIR to a dedicated replay directory")
    path = Path(raw).expanduser().resolve()
    blocked = {
        Path("/var/data").resolve(),
        Path("/var/data/desk.db").resolve(),
        Path("./data").resolve(),
        Path("data").resolve(),
    }
    if path in blocked or path.name.lower() == "desk.db":
        raise SystemExit("refusing: that path is the desk database, not a replay")
    if "replay" not in path.name.lower():
        raise SystemExit("refusing: DESK_DATA_DIR's directory name must contain replay")
    return path


def prepare() -> None:
    """Create the isolated database and keep it in PAPER. Agents are not a setting here."""
    db.init_db()
    if (db.get_setting("mode") or "PAPER") != "PAPER":
        db.set_setting("mode", "PAPER", "replay")
    if db.get_setting("mode") != "PAPER":
        raise SystemExit("refusing: mode is not PAPER")
    if db.get_setting("agents_enabled") == "1":
        db.set_setting("agents_enabled", "0", "replay")


async def _bounded(seconds: float) -> None:
    task = asyncio.create_task(run_desk())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(name, stop.set)
        except NotImplementedError:
            pass
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def main() -> None:
    guard()
    if os.environ.get("DESK_REPLAY_NOTIFY") != "1":
        os.environ["DESK_NOTIFY"] = "0"
    prepare()
    seconds = float(os.environ.get("REPLAY_SECONDS") or "7200")
    if seconds <= 0:
        raise SystemExit("refusing: REPLAY_SECONDS must be positive")
    asyncio.run(_bounded(seconds))


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        if exc.code not in (None, 0):
            print(exc, file=sys.stderr)
        raise
