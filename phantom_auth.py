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
from urllib.parse import parse_qs, urlparse

import db
import phantom_transport

log = logging.getLogger("phantom_auth")

LOGIN_TIMEOUT_S = 600
AUTH_FRESH_S = 120
MAX_CAPTURE = 65536
APPROVAL_ORIGIN = "https://connect.phantom.app"
APPROVAL_PATH = "/device-connect"
_URL = re.compile(r"https://[^\s\x1b\"'<>]+")
_CODE = re.compile(r"Code:\s*([A-Za-z0-9-]{4,64})")
_LOCK = threading.Lock()
_IDENTITY = {"observed", "mismatch", "auth_error", "unable_to_verify"}
_SOURCES = {"addresses", "balance", "quote"}
_EVIDENCE_KEY = "auth_evidence"
_FLOW: dict = {
    "state": "idle",
    "approval_url": None,
    "user_code": None,
    "expires_at": None,
    "observed_wallet_id": None,
    "observed_wallet": None,
    "process": None,
    "evidence_fresh": False,
    "evidence_source": None,
    "observed_at": None,
}


def account_view() -> dict:
    """Report auth evidence. A cached match is not a fresh check or live authority."""
    with _LOCK:
        state = _FLOW["state"]
        observed_id = _FLOW["observed_wallet_id"]
        observed_wallet = _FLOW["observed_wallet"]
        matches = _matches()
        observed_at = _FLOW.get("observed_at")
        evidence_source = _FLOW.get("evidence_source")
    cached = _cached_evidence() if state == "idle" else None
    if state == "idle" and cached:
        auth_flow = "cached_" + cached["state"]
        evidence = "cached"
        cached_match = cached["state"] == "observed"
        matches = None
        observed_id = None
        observed_wallet = None
        observed_at = None
        evidence_source = None
    elif state == "idle":
        auth_flow = phantom_transport.AUTH
        evidence = "none"
        cached_match = None
        observed_at = None
        evidence_source = None
    else:
        auth_flow = state
        evidence = "fresh" if state in _IDENTITY else "process"
        cached_match = None
        if evidence != "fresh":
            observed_at = None
            evidence_source = None
    return {
        "auth_flow": auth_flow,
        "evidence": evidence,
        "cached_match": cached_match,
        "live_ready": False,
        "executable": False,
        "send": False,
        "transport": phantom_transport.TRANSPORT,
        "execution_transport": "unimplemented",
        "observed_wallet_id": observed_id,
        "observed_wallet": observed_wallet,
        "matches_allowlist": matches,
        "evidence_source": evidence_source,
        "observed_at": observed_at,
        "required_wallet_id": phantom_transport.REQUIRED_WALLET_ID,
        "required_wallet": phantom_transport.REQUIRED_WALLET,
    }


def note_evidence(state: str, source: str) -> None:
    """Remember a balance or quote result. This does not identify the wallet."""
    if state not in {"observed", "mismatch", "auth_error"} or source not in {"balance", "quote"}:
        return
    with _LOCK:
        _FLOW["state"] = state
        _FLOW["evidence_fresh"] = True
        _FLOW["evidence_source"] = source
        _FLOW["approval_url"] = None
        _FLOW["user_code"] = None
        _FLOW["verify"] = None
        _FLOW["observed_wallet_id"] = None
        _FLOW["observed_wallet"] = None
        _FLOW["observed_at"] = None
    _persist_evidence(state, source)


def public_login() -> dict:
    body = account_view()
    with _LOCK:
        if _FLOW["state"] == "pending":
            body["approval_url"] = _FLOW["approval_url"]
            body["user_code"] = _FLOW["user_code"]
            body["expires_at"] = _FLOW["expires_at"]
        if _FLOW.get("verify"):
            body["verify"] = _FLOW["verify"]
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
    _stop_orphan(home, binary)
    log.info("phantom login starting")
    try:
        child = subprocess.Popen(
        [binary, "login", "--display-mode", "text"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_child_env(home, binary),
        cwd=str(home),
        start_new_session=True,
        )
    except OSError:
        with _LOCK:
            _FLOW["state"] = "failed"
            _FLOW["process"] = None
        log.info("phantom login failed")
        raise RuntimeError("phantom package is not available")
    _write_pid(home, child.pid)
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
    proc = _FLOW["process"]
    if _FLOW["state"] == "pending" and proc is not None and proc.poll() is None:
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
            raw = _append(raw, chunk)
            url, code, rejected = _approval(raw)
            if rejected:
                _fail_closed(child)
                return
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
            raw = _append(raw, chunk)
    url, code, rejected = _approval(raw)
    if rejected:
        _fail_closed(child)
        return
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
    global _VERIFIED_BY_RUN
    if subprocess.run is not _RUN:
        _VERIFIED_BY_RUN = False
        return
    binary = _binary()
    home = _home()
    try:
        done = _RUN(
            [binary, "wallet", "addresses", "--format", "json"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_child_env(home, binary),
            cwd=str(home),
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        with _LOCK:
            _FLOW["state"] = "failed"
        log.info("phantom login failed")
        return
    text = (done.stdout + done.stderr).decode("utf-8", "replace")[:MAX_CAPTURE]
    _apply_classification(_classify(text, done.returncode), from_run=True)


def _append(raw: str, chunk: bytes) -> str:
    if len(raw) >= MAX_CAPTURE:
        return raw
    return raw + chunk.decode("utf-8", "replace")[: MAX_CAPTURE - len(raw)]


def _approval(text: str) -> tuple[str | None, str | None, bool]:
    found = _URL.findall(text)
    if not found:
        return None, None, False
    if any(not _allowed(url) for url in found):
        return None, None, True
    url = found[0]
    parsed = urlparse(url)
    query_code = parse_qs(parsed.query).get("user_code", [None])[0]
    line = _CODE.search(text)
    user_code = line.group(1) if line else query_code
    if user_code != query_code:
        return None, None, True
    return url, user_code, False


def _allowed(url: str) -> bool:
    if any(char in url for char in " \t\r\n<>\"'"):
        return False
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "connect.phantom.app":
        return False
    if parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.fragment:
        return False
    if parsed.path != APPROVAL_PATH:
        return False
    code = parse_qs(parsed.query).get("user_code", [None])[0]
    return isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9-]{4,64}", code) is not None


def _fail_closed(child: subprocess.Popen) -> None:
    _kill(child)
    with _LOCK:
        if _FLOW["state"] == "pending":
            _FLOW["state"] = "failed"
            _FLOW["approval_url"] = None
            _FLOW["user_code"] = None
            _FLOW["process"] = None
    log.info("phantom login failed")


def _apply_classification(result: dict, from_run: bool = False) -> None:
    global _VERIFIED_BY_RUN
    _VERIFIED_BY_RUN = bool(from_run) and result.get("state") == "observed" and subprocess.run is _RUN
    with _LOCK:
        _FLOW["verify"] = result["verify"]
        _FLOW["approval_url"] = None
        _FLOW["user_code"] = None
        if result["state"] in {"observed", "mismatch"}:
            _FLOW["observed_wallet_id"] = result["wallet_id"]
            _FLOW["observed_wallet"] = result["address"]
        else:
            _FLOW["observed_wallet_id"] = None
            _FLOW["observed_wallet"] = None
        _FLOW["state"] = result["state"]
        _FLOW["evidence_fresh"] = result["state"] in _IDENTITY
        _FLOW["evidence_source"] = "addresses" if result["state"] in _IDENTITY else None
        _FLOW["observed_at"] = time.time() if result["state"] in {"observed", "mismatch"} and result.get("wallet_id") and result.get("address") else None
    if result["state"] in _IDENTITY:
        _persist_evidence(result["state"], "addresses")
    log.info("phantom login %s", result["state"])


def _classify(text: str, returncode: int) -> dict:
    """Classify addresses output. Mismatch requires both identity fields."""
    body, shape = _payload(text)
    if shape == "error_wrapper" or _auth_text(text) or returncode != 0:
        return _unknown("auth_error", "error_wrapper" if shape == "error_wrapper" else "auth", body)
    if shape == "empty":
        return _unknown("unable_to_verify", "empty", None)
    if body is None:
        return _unknown("unable_to_verify", "malformed", None)
    wallet_id, address, fields = _fields(body)
    verify = {"error_category": None, "shape": shape, "fields": fields}
    if fields["walletId"] != "string" or fields["solana"] != "string":
        verify["error_category"] = "unable_to_verify"
        return {"state": "unable_to_verify", "wallet_id": None, "address": None, "verify": verify}
    state = "observed" if (
        wallet_id == phantom_transport.REQUIRED_WALLET_ID
        and address == phantom_transport.REQUIRED_WALLET
    ) else "mismatch"
    verify["error_category"] = state
    return {"state": state, "wallet_id": wallet_id, "address": address, "verify": verify}


def _unknown(state: str, shape: str, body) -> dict:
    fields = _field_types(body) if isinstance(body, dict) else {
        "walletId": "missing", "addresses": "missing", "solana": "missing",
    }
    return {
        "state": state,
        "wallet_id": None,
        "address": None,
        "verify": {"error_category": state, "shape": shape, "fields": fields},
    }


def _payload(text: str) -> tuple[dict | None, str]:
    stripped = text.strip()
    if not stripped:
        return None, "empty"
    parsed = _json_value(stripped)
    if isinstance(parsed, dict):
        if parsed.get("ok") is False or ("error" in parsed and "walletId" not in parsed and "data" not in parsed):
            return parsed, "error_wrapper"
        if parsed.get("ok") is True and isinstance(parsed.get("data"), dict):
            return parsed["data"], "json_envelope"
        if "walletId" in parsed or "addresses" in parsed:
            return parsed, "json_schema"
        return parsed, "malformed"
    toon = _toon_payload(stripped)
    if toon is not None:
        if "code" in toon and "walletId" not in toon:
            return toon, "error_wrapper"
        return toon, "toon"
    return None, "malformed"


def _json_value(text: str):
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, dict):
        return value
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _toon_payload(text: str) -> dict | None:
    wallet_id = None
    rows = []
    in_addresses = False
    saw_wallet = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("walletId:"):
            saw_wallet = True
            wallet_id = stripped.split(":", 1)[1].strip()
            in_addresses = False
            continue
        if stripped.startswith("addresses[") and "{addressType,address}" in stripped:
            in_addresses = True
            continue
        if in_addresses and "," in stripped and not stripped.endswith(":"):
            kind, _, address = stripped.partition(",")
            rows.append({"addressType": kind.strip(), "address": address.strip()})
            continue
        if stripped.endswith(":") and not stripped.startswith("addresses["):
            in_addresses = False
    if not saw_wallet and not rows and "code:" not in text:
        return None
    if "code:" in text and not saw_wallet:
        return {"code": "present"}
    return {"walletId": wallet_id, "addresses": rows}


def _fields(body: dict) -> tuple[str | None, str | None, dict]:
    fields = _field_types(body)
    wallet_id = body.get("walletId") if fields["walletId"] == "string" else None
    address = None
    solana = [
        row.get("address") for row in body.get("addresses") or []
        if isinstance(row, dict) and str(row.get("addressType", "")).lower() == "solana"
    ]
    if len(solana) == 1 and fields["solana"] == "string":
        address = solana[0]
    return wallet_id, address, fields


def _field_types(body: dict) -> dict:
    wallet = body.get("walletId")
    addresses = body.get("addresses")
    solana = []
    if isinstance(addresses, list):
        solana = [
            row.get("address") for row in addresses
            if isinstance(row, dict) and str(row.get("addressType", "")).lower() == "solana"
        ]
    return {
        "walletId": "string" if _valid_wallet_id(wallet) else ("missing" if wallet is None else "invalid"),
        "addresses": "array" if isinstance(addresses, list) else ("missing" if addresses is None else "invalid"),
        "solana": "string" if len(solana) == 1 and _valid_solana(solana[0]) else (
            "missing" if not solana else "invalid"
        ),
    }


def _valid_wallet_id(value) -> bool:
    return isinstance(value, str) and re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        value,
    ) is not None


def _valid_solana(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", value) is not None


def _auth_text(text: str) -> bool:
    lowered = text.lower()
    return (
        "auth_expired" in lowered
        or "failed to get wallet addresses" in lowered
        or "status code 401" in lowered
        or "http 401" in lowered
    )


def _persist_evidence(state: str, source: str) -> None:
    if state not in _IDENTITY or source not in _SOURCES:
        return
    payload = json.dumps(
        {"state": state, "source": source, "at": round(time.time(), 3)},
        separators=(",", ":"),
    )
    db.execute(
        """
        INSERT INTO phantom_control (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (_EVIDENCE_KEY, payload, time.time()),
    )


def _cached_evidence() -> dict | None:
    rows = db.query("SELECT value FROM phantom_control WHERE key = ?", (_EVIDENCE_KEY,))
    if not rows:
        return None
    try:
        body = json.loads(rows[0]["value"])
    except json.JSONDecodeError:
        return None
    if not isinstance(body, dict) or set(body) != {"state", "source", "at"}:
        return None
    if body["state"] not in _IDENTITY or body["source"] not in _SOURCES:
        return None
    if isinstance(body["at"], bool) or not isinstance(body["at"], (int, float)):
        return None
    return {"state": body["state"], "source": body["source"], "at": body["at"]}


_RUN = subprocess.run
_VERIFIED_BY_RUN = False


def reverify_on_startup() -> dict:
    """Read the existing Phantom session. This does not log in and does not sign."""
    try:
        _verify()
    except Exception:
        return {"ok": False, "reason": "verify_unavailable", "sign": False, "send": False}
    closed = server_auth_closed()
    return {"ok": closed, "reason": None if closed else "auth_open", "sign": False, "send": False}


def server_auth_closed(now: float | None = None) -> bool:
    """The readiness gate. A cached, stale, or stand-in identity check stays open."""
    if subprocess.run is not _RUN or _VERIFIED_BY_RUN is not True:
        return False
    try:
        view = account_view()
    except Exception:
        return False
    if view.get("evidence_source") != "addresses":
        return False
    if view["auth_flow"] != "observed" or view["evidence"] != "fresh" or view["matches_allowlist"] is not True:
        return False
    if view["cached_match"] is True:
        return False
    if view["observed_wallet_id"] != phantom_transport.REQUIRED_WALLET_ID or view["observed_wallet"] != phantom_transport.REQUIRED_WALLET:
        return False
    stamp = view.get("observed_at")
    moment = time.time() if now is None else now
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return False
    if isinstance(moment, bool) or not isinstance(moment, (int, float)):
        return False
    age = moment - stamp
    return 0 <= age <= AUTH_FRESH_S


def _matches():
    if _FLOW["state"] not in {"observed", "mismatch"}:
        return None
    return (
        _FLOW["observed_wallet_id"] == phantom_transport.REQUIRED_WALLET_ID
        and _FLOW["observed_wallet"] == phantom_transport.REQUIRED_WALLET
    )


def _binary() -> str:
    override = os.environ.get("DESK_PHANTOM_BIN")
    if override:
        if not os.path.isabs(override) or any(char in override for char in "\n\r\x00"):
            raise RuntimeError("phantom package is not available")
        path = Path(override)
    else:
        path = Path(__file__).resolve().parent / "node_modules" / ".bin" / "phantom"
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError("phantom package is not available")
    return str(path)


def _home() -> Path:
    configured = os.environ.get("DESK_PHANTOM_HOME")
    if configured:
        return Path(configured)
    return Path(db._data_dir()) / "phantom-mcp"


def _child_env(home: Path, binary: str | None = None) -> dict:
    parts = ["/usr/bin", "/bin"]
    node_bin = Path(__file__).resolve().parent / ".tools" / "node" / "bin"
    if node_bin.is_dir():
        parts.insert(0, str(node_bin))
    if binary:
        parts.insert(0, str(Path(binary).resolve().parent))
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home),
        "XDG_DATA_HOME": str(home),
        "PATH": os.pathsep.join(parts),
    }


def _write_pid(home: Path, pid: int) -> None:
    path = home / "login.pid"
    path.write_text(str(pid), encoding="utf-8")
    os.chmod(path, 0o600)


def _stop_orphan(home: Path, binary: str) -> None:
    path = home / "login.pid"
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8")[:32].strip()
    if not text.isdigit():
        return
    pid = int(text)
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError:
        return
    if binary not in command or " login" not in f" {command}":
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


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
    global _VERIFIED_BY_RUN
    _VERIFIED_BY_RUN = False
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
            "verify": None,
            "evidence_fresh": False,
            "evidence_source": None,
            "observed_at": None,
        })
    _kill(proc)
