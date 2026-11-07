"""Auth-only Phantom device login for the deployment home.

This starts `phantom login --display-mode text` and keeps the session files
in that home. It does not quote, send, fund, or change the wallet allowlist.
A 401 does not start another login.
"""

from __future__ import annotations

import json
import logging
import os
import re
import select
import signal
import subprocess
import threading
import time
from pathlib import Path

import db
import phantom_transport

log = logging.getLogger("phantom_auth")

LOGIN_TIMEOUT_S = 600
_URL = re.compile(r"https://[^\s\x1b\"']+/device-connect\?[^\s\x1b\"']+")
_CODE = re.compile(r"Code:\s*([A-Za-z0-9-]+)")
_LOCK = threading.Lock()
_FLOW: dict = {
    "state": "idle",
    "approval_url": None,
    "user_code": None,
    "expires_at": None,
    "observed_wallet_id": None,
    "observed_wallet": None,
    "process": None,
}


def account_view() -> dict:
    with _LOCK:
        state = _FLOW["state"]
    return {
        "auth_flow": state if state != "idle" else phantom_transport.AUTH,
        "live_ready": False,
        "executable": False,
        "send": False,
        "observed_wallet_id": _FLOW["observed_wallet_id"],
        "observed_wallet": _FLOW["observed_wallet"],
        "matches_allowlist": _matches(),
        "required_wallet_id": phantom_transport.REQUIRED_WALLET_ID,
        "required_wallet": phantom_transport.REQUIRED_WALLET,
    }


def public_login() -> dict:
    body = account_view()
    with _LOCK:
        if _FLOW["state"] == "pending":
            body["approval_url"] = _FLOW["approval_url"]
            body["user_code"] = _FLOW["user_code"]
            body["expires_at"] = _FLOW["expires_at"]
    return body


def start(timeout_s: float = LOGIN_TIMEOUT_S) -> dict:
    """Begin one device login. A login already waiting is not started again."""
    binary = _binary()
    home = _home()
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    with _LOCK:
        proc = _FLOW["process"]
        already = _FLOW["state"] == "pending" and proc is not None and proc.poll() is None
        if not already:
            _FLOW.update({
                "state": "pending",
                "approval_url": None,
                "user_code": None,
                "expires_at": time.time() + timeout_s,
                "process": None,
            })
    if already:
        log.info("phantom login pending")
        return public_login()
    log.info("phantom login starting")
    child = subprocess.Popen(
        [binary, "login", "--display-mode", "text"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_child_env(home),
        cwd=str(home),
        start_new_session=True,
    )
    with _LOCK:
        _FLOW["process"] = child
    threading.Thread(target=_watch, args=(child, home, timeout_s), daemon=True).start()
    deadline = time.time() + 2
    while time.time() < deadline and _FLOW["approval_url"] is None and child.poll() is None:
        time.sleep(0.05)
    return public_login()


def cancel() -> dict:
    with _LOCK:
        proc = _FLOW["process"]
        _FLOW["state"] = "cancelled"
        _FLOW["approval_url"] = None
        _FLOW["user_code"] = None
    _kill(proc)
    log.info("phantom login cancelled")
    return public_login()


def check() -> dict:
    """Ask the official addresses command. This does not start a login."""
    if _FLOW["state"] == "pending":
        return public_login()
    if _FLOW["state"] not in {"approved_unverified", "observed", "mismatch", "auth_expired"}:
        return public_login()
    _verify()
    return public_login()


def _watch(child: subprocess.Popen, home: Path, timeout_s: float) -> None:
    raw = ""
    deadline = time.time() + timeout_s
    streams = [item for item in (child.stderr, child.stdout) if item is not None]
    while time.time() < deadline and child.poll() is None:
        ready, _, _ = select.select(streams, [], [], 0.1)
        for stream in ready:
            try:
                chunk = os.read(stream.fileno(), 4096)
            except OSError:
                chunk = b""
            if not chunk:
                continue
            raw += chunk.decode("utf-8", "replace")
            url, code = _approval(raw)
            if url:
                with _LOCK:
                    if _FLOW["state"] == "pending":
                        _FLOW["approval_url"] = url
                        _FLOW["user_code"] = code
    for stream in streams:
        while True:
            ready, _, _ = select.select([stream], [], [], 0)
            if not ready:
                break
            try:
                chunk = os.read(stream.fileno(), 4096)
            except OSError:
                break
            if not chunk:
                break
            raw += chunk.decode("utf-8", "replace")
    url, code = _approval(raw)
    if url:
        with _LOCK:
            if _FLOW["state"] == "pending":
                _FLOW["approval_url"] = url
                _FLOW["user_code"] = code
    if child.poll() is None:
        _kill(child)
        with _LOCK:
            if _FLOW["state"] == "pending":
                _FLOW["state"] = "expired"
                _FLOW["approval_url"] = None
                _FLOW["user_code"] = None
        log.info("phantom login expired")
        return
    code = child.wait()
    _kill(child)
    with _LOCK:
        if _FLOW["state"] != "pending":
            return
        if code != 0 or _FLOW["approval_url"] is None:
            _FLOW["state"] = "failed"
            _FLOW["approval_url"] = None
            _FLOW["user_code"] = None
            log.info("phantom login failed")
            return
        _FLOW["state"] = "approved_unverified"
        _FLOW["approval_url"] = None
        _FLOW["user_code"] = None
    log.info("phantom login approved_unverified")
    _verify()


def _verify() -> None:
    binary = _binary()
    home = _home()
    try:
        done = subprocess.run(
            [binary, "wallet", "addresses"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_child_env(home),
            cwd=str(home),
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        with _LOCK:
            _FLOW["state"] = "failed"
        log.info("phantom login failed")
        return
    text = (done.stdout + done.stderr).decode("utf-8", "replace")
    if done.returncode != 0 or "401" in text or "AUTH_EXPIRED" in text or "Failed to get wallet addresses" in text:
        with _LOCK:
            _FLOW["state"] = "auth_expired"
            _FLOW["approval_url"] = None
            _FLOW["user_code"] = None
        log.info("phantom login auth_expired")
        return
    wallet_id, address = _identity(text)
    with _LOCK:
        _FLOW["observed_wallet_id"] = wallet_id
        _FLOW["observed_wallet"] = address
        if wallet_id == phantom_transport.REQUIRED_WALLET_ID and address == phantom_transport.REQUIRED_WALLET:
            _FLOW["state"] = "observed"
        else:
            _FLOW["state"] = "mismatch"
    log.info("phantom login %s", _FLOW["state"])


def _approval(text: str) -> tuple[str | None, str | None]:
    found = _URL.search(text)
    if not found:
        return None, None
    url = found.group(0)
    code = _CODE.search(text)
    user_code = code.group(1) if code else None
    if user_code is None and "user_code=" in url:
        user_code = url.split("user_code=", 1)[1].split("&", 1)[0]
    return url, user_code


def _identity(text: str) -> tuple[str | None, str | None]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None, None
    try:
        body = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None, None
    wallet_id = body.get("walletId") if isinstance(body.get("walletId"), str) else None
    address = None
    for row in body.get("addresses") or []:
        if isinstance(row, dict) and str(row.get("addressType", "")).lower() == "solana":
            if isinstance(row.get("address"), str):
                address = row["address"]
            break
    return wallet_id, address


def _matches():
    if _FLOW["state"] not in {"observed", "mismatch"}:
        return None
    return (
        _FLOW["observed_wallet_id"] == phantom_transport.REQUIRED_WALLET_ID
        and _FLOW["observed_wallet"] == phantom_transport.REQUIRED_WALLET
    )


def _binary() -> str:
    binary = os.environ.get("DESK_PHANTOM_BIN") or "phantom"
    if binary != "phantom" and not os.path.isabs(binary):
        raise RuntimeError("phantom binary path is not usable")
    return binary


def _home() -> Path:
    configured = os.environ.get("DESK_PHANTOM_HOME")
    if configured:
        return Path(configured)
    return Path(db._data_dir()) / "phantom-mcp"


def _child_env(home: Path) -> dict:
    env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home),
        "XDG_DATA_HOME": str(home),
        "PATH": os.environ.get("PATH", ""),
    }
    return env


def _kill(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    for stream in (proc.stdout, proc.stderr):
        if stream is not None and not stream.closed:
            stream.close()


def reset_for_tests() -> None:
    with _LOCK:
        proc = _FLOW["process"]
        _FLOW.update({
            "state": "idle",
            "approval_url": None,
            "user_code": None,
            "expires_at": None,
            "observed_wallet_id": None,
            "observed_wallet": None,
            "process": None,
        })
    _kill(proc)
