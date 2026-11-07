"""Isolated PAPER replay of the live PumpPortal migration stream.

This file does not start a replay when imported. The production web service
does not call it. Start it only with DESK_REPLAY=1 and a dedicated data
directory whose name contains "replay". Stop it with SIGTERM or SIGINT.
The default length is 7200 seconds. Web push stays off unless
DESK_REPLAY_NOTIFY=1.

The five agents run on that socket. DexScreener, RugCheck, and the public
Solana RPC are the inputs. No trade subscription, no wallet, and no LIVE.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import db
from agents.executor import paper_night_done
from agents.scanner import DEX_URL, PUMPPORTAL_URL, RUGCHECK_REPORT
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


def prepare() -> str:
    """Create the isolated database and keep it in PAPER. Agents are not a setting here.

    With no Jev key this process uses rules. It does not read or print the key.
    """
    db.init_db()
    if (db.get_setting("mode") or "PAPER") != "PAPER":
        db.set_setting("mode", "PAPER", "replay")
    if db.get_setting("mode") != "PAPER":
        raise SystemExit("refusing: mode is not PAPER")
    if db.get_setting("agents_enabled") == "1":
        db.set_setting("agents_enabled", "0", "replay")
    if paper_night_done():
        raise SystemExit("refusing: paper night is unlocked")
    if os.environ.get("TYPESAFE_API_KEY"):
        mode = db.get_setting("analyst_mode", "jev") or "jev"
    else:
        db.set_setting("analyst_mode", "rules", "replay")
        mode = "rules"
    return mode


def write_provenance(directory: Path, analyst_mode: str, seconds: float) -> dict:
    """Facts about this run. No secrets, no endpoints, no key material."""
    rpc = db.get_setting("rpc_url") or ""
    host = urlparse(rpc).hostname
    record = {
        "started_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "replay_seconds": seconds,
        "mode": db.get_setting("mode"),
        "agents_enabled": db.get_setting("agents_enabled"),
        "paper_night_done": paper_night_done(),
        "analyst_mode": analyst_mode,
        "analyst_mode_declared": "rules" if analyst_mode == "rules" else "configured",
        "notify": os.environ.get("DESK_NOTIFY") != "0",
        "source": PUMPPORTAL_URL,
        "source_method": "subscribeMigration",
        "trade_subscriptions": False,
        "dex": DEX_URL,
        "rugcheck": RUGCHECK_REPORT,
        "rpc_host": host,
        "live_orders": False,
        "wallet": False,
        "data_dir_name": directory.name,
    }
    (directory / "provenance.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def export_activity(directory: Path) -> dict:
    """Every activity row, plus counts. An empty list is an honest zero."""
    rows = []
    try:
        for row in db.query(
            "SELECT ts, kind, mint, agent, title, subtitle, value FROM activity ORDER BY ts, id"
        ):
            rows.append({key: row[key] for key in row.keys()})
    except Exception:
        rows = []
    counts = {}
    for table in ("scans", "audits", "verdicts", "signals", "positions", "coins"):
        try:
            counts[table] = int(db.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"] or 0)
        except Exception:
            counts[table] = None
    payload = {"exported_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "activity": rows, "counts": counts}
    (directory / "activity.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


async def _bounded(directory: Path, seconds: float) -> None:
    task = asyncio.create_task(run_desk())

    async def snapshot() -> None:
        while True:
            await asyncio.sleep(30)
            export_activity(directory)

    snap = asyncio.create_task(snapshot())
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
    snap.cancel()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    export_activity(directory)


def main() -> None:
    directory = guard()
    if os.environ.get("DESK_REPLAY_NOTIFY") != "1":
        os.environ["DESK_NOTIFY"] = "0"
    analyst_mode = prepare()
    seconds = float(os.environ.get("REPLAY_SECONDS") or "7200")
    if seconds <= 0:
        raise SystemExit("refusing: REPLAY_SECONDS must be positive")
    directory.mkdir(parents=True, exist_ok=True)
    write_provenance(directory, analyst_mode, seconds)
    export_activity(directory)
    asyncio.run(_bounded(directory, seconds))


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        if exc.code not in (None, 0):
            print(exc, file=sys.stderr)
        raise
