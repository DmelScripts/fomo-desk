"""Phantom MCP transport. This process does not log in, sign, or broadcast.

The pinned server can be started with an empty home. A running process is not
a session and it is not permission to quote or send. Fresh auth would be a
device login that creates a new agent wallet. Nothing here reaches the wallet
already used for the manual quote.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

PINNED_PACKAGE = "@phantom/mcp-server@1.2.9"
PINNED_CLI = "@phantom/cli@3.0.0"
AUTH = "AWAITING_OWNER_AUTH_PROCEDURE"
TRANSPORT = "BLOCKED"
REQUIRED_WALLET_ID = "d76451f8-288c-4ab8-b6de-1cfd84403873"
REQUIRED_WALLET = "GLmLe3gpVw87nSeA8YY93megzW6gDSeavHys3BKQ61fh"


def status() -> dict:
    return {
        "transport": TRANSPORT,
        "auth": AUTH,
        "send": False,
        "live_ready": False,
        "executable": False,
        "package": PINNED_PACKAGE,
        "cli": PINNED_CLI,
        "login": "displayMode text device authorization. Not started.",
        "wallet_binding": "wallet id and Solana address are both required before any quote",
        "required_wallet_id": REQUIRED_WALLET_ID,
        "required_wallet": REQUIRED_WALLET,
        "detail": (
            "Render has no Phantom session. A new login creates a different agent wallet. "
            "The buy walletId argument is not authorization. Persistent files are not a session."
        ),
    }


def quote(_spec: dict) -> dict:
    """No quote is fetched. The historical no-expiry quote stays non-executable."""
    blocked = status()
    blocked["quote"] = None
    blocked["reason"] = AUTH
    return blocked


def probe_stdio(command: list[str], home: str) -> dict:
    """Start a process with a clean home and send it nothing.

    The command is killed. No login, tool call, or broadcast is written.
    """
    result = status()
    result["probe"] = {"started": False, "stdin_written": False, "tool_call": False}
    if not isinstance(command, list) or not all(isinstance(part, str) and part for part in command):
        result["probe"]["reason"] = "refused_command"
        return result
    text = " ".join(command).lower()
    if "login" in text or "execute" in text or "buy" in text:
        result["probe"]["reason"] = "refused_command"
        return result
    root = Path(home).resolve()
    if not str(root).startswith("/tmp/"):
        result["probe"]["reason"] = "refused_home"
        return result
    root.mkdir(parents=True, exist_ok=True)
    env = {
        "HOME": str(root),
        "XDG_CONFIG_HOME": str(root),
        "XDG_DATA_HOME": str(root),
        "PATH": os.environ.get("PATH", ""),
    }
    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(root),
            start_new_session=True,
        )
    except OSError:
        result["probe"]["reason"] = "did_not_start"
        return result
    time.sleep(0.2)
    result["probe"]["started"] = proc.poll() is None
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        proc.kill()
    try:
        proc.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
    return result
