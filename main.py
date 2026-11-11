"""One asyncio process for the five agents and the single PumpPortal socket.

`python main.py` starts them. The web service does not, unless agents_enabled
is 1 or DESK_AGENTS=1.
"""

from __future__ import annotations

import asyncio
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import db
from agents.analyst import Analyst
from agents.auditor import Auditor
from agents.executor import Executor
from agents.risk import Risk
from agents.scanner import Scanner, pumpportal_loop
from bus import Desk
from config import install_secret_redaction
from notifier import Notifier


class HttpxAdapter:
    def __init__(self, client) -> None:
        self.client = client

    async def get_json(self, url: str):
        response = await self.client.get(url, timeout=20)
        if response.status_code == 429:
            return {"error": {"code": 429}}
        response.raise_for_status()
        return response.json()

    async def post_json(self, url: str, body: dict, headers: dict | None = None):
        import solana_rpc
        ok, reason, data = await _post_json(self.client, url, body, headers)
        del url, body, headers
        if not ok:
            solana_rpc._refuse(reason)
        return data


async def _post_json(client, url, body, headers):
    """One POST. Returns a reason code. This frame is gone before the caller raises."""
    import httpx
    response = None
    try:
        response = await client.post(url, json=body, headers=headers, timeout=30)
    except httpx.TimeoutException:
        return False, "rpc_timeout", None
    except httpx.HTTPError:
        return False, "rpc_transport", None
    code = response.status_code
    if code == 429:
        return True, None, {"error": {"code": 429}}
    if code != 200:
        return False, "rpc_http_%s" % code, None
    try:
        parsed = response.json()
    except Exception:
        return False, "rpc_bad_json", None
    return True, None, parsed


def _attach_log(name: str) -> None:
    logger = logging.getLogger(name)
    if any(isinstance(handler, TimedRotatingFileHandler) for handler in logger.handlers):
        return
    directory = Path("logs")
    directory.mkdir(parents=True, exist_ok=True)
    handler = TimedRotatingFileHandler(directory / f"{name}.log", when="midnight", backupCount=7)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


async def run_desk() -> None:
    install_secret_redaction()
    db.init_db()
    for name in ("scanner", "auditor", "analyst", "executor", "risk", "notifier"):
        _attach_log(name)
    import httpx

    async with httpx.AsyncClient() as client:
        desk = Desk(HttpxAdapter(client), Notifier())
        scanner = Scanner(desk)
        auditor = Auditor(desk)
        analyst = Analyst(desk)
        executor = Executor(desk)
        risk = Risk(desk)
        await asyncio.gather(
            scanner.run(),
            pumpportal_loop(scanner),
            auditor.run(),
            analyst.run(),
            executor.run(),
            risk.run(),
        )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_desk())


if __name__ == "__main__":
    main()