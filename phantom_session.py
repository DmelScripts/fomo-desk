"""Sign with the server Phantom session for the allowlisted wallet.

This does not broadcast and it does not start a login. Sending stays on Helius.
A missing or stale session fails closed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import phantom_auth
import phantom_transport

_SCRIPT = Path(__file__).resolve().parent / "phantom_sign_only.mjs"
_RUN = subprocess.run


def ready() -> bool:
    """True only for a fresh addresses check of the allowlisted wallet."""
    if not phantom_auth.server_auth_closed():
        return False
    view = phantom_auth.account_view()
    return (
        view.get("evidence_source") == "addresses"
        and view.get("matches_allowlist") is True
        and view.get("observed_wallet_id") == phantom_transport.REQUIRED_WALLET_ID
        and view.get("observed_wallet") == phantom_transport.REQUIRED_WALLET
    )


def sign(message_b64: str) -> dict:
    """Return the signed transaction. The caller sends it."""
    if not isinstance(message_b64, str) or message_b64 == "":
        raise RuntimeError("message_refused")
    if not ready():
        raise RuntimeError("phantom_session_missing")
    signed = _sign_transaction(message_b64)
    if not isinstance(signed, str) or signed == "" or "\n" in signed:
        raise RuntimeError("signature_refused")
    return {"signed_tx": signed}


sign.ready = ready


def _sign_transaction(message_b64: str) -> str:
    home = phantom_auth._home()
    binary = phantom_auth._binary()
    env = phantom_auth._child_env(home, binary)
    env["DESK_SESSION_DIR"] = str(Path(home) / ".phantom-mcp")
    env["DESK_WALLET_ID"] = phantom_transport.REQUIRED_WALLET_ID
    env["DESK_WALLET"] = phantom_transport.REQUIRED_WALLET
    if subprocess.run is not _RUN:
        raise RuntimeError("phantom_session_missing")
    try:
        done = _RUN(
            ["node", str(_SCRIPT)],
            input=message_b64.encode(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd=str(Path(__file__).resolve().parent),
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("phantom_session_missing") from None
    if done.returncode != 0:
        raise RuntimeError("phantom_session_missing")
    text = done.stdout.decode("utf-8", "replace").strip()
    if text == "" or any(char.isspace() for char in text):
        raise RuntimeError("signature_refused")
    return text
