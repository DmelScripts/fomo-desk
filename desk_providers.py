"""The desk's own readers. A caller body is not one of these.

Keys come from the environment. They are not logged, stored, or put in a
failure reason. DexScreener and RugCheck need no key. Solana Tracker uses
SOLANA_TRACKER_API_KEY. Helius DAS uses HELIUS_API_KEY. Hermes uses
PYTH_API_KEY. Nothing here signs, simulates, or sends.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import cross_source
import dexscreener_public
import pyth_sponsored

USER_AGENT = "fomo-desk/1.0"
TIMEOUT_S = 8
GAPS = {
    "dexscreener": 0.4,
    "rugcheck": 0.5,
    "solana_tracker": 0.4,
    "helius": 0.4,
    "hermes": 1.0,
}
_LAST: dict[str, float] = {}
_GAP: float | None = None
_TRANSPORT = None
_INSTALLED = False
_SECRETS: list[str] = []
_NOW = time.monotonic
_SLEEP = time.sleep
_DECISION_TIMEOUT = TIMEOUT_S
_LOCKS = {name: threading.Lock() for name in GAPS}
_CALL = threading.local()


def configured() -> dict:
    """Which clients have what they need. Booleans only. Never a key."""
    return {
        "dexscreener": True,
        "rugcheck": True,
        "solana_tracker": _present("SOLANA_TRACKER_API_KEY"),
        "helius": _present("HELIUS_API_KEY"),
        "hermes": _present("PYTH_API_KEY"),
    }


def set_transport(transport) -> None:
    """A test double. Production uses urllib and does not keep a response."""
    global _TRANSPORT
    _TRANSPORT = transport if callable(transport) else None
    _LAST.clear()
    _reset_deadline()


def set_gap(seconds: float | None) -> None:
    global _GAP
    _GAP = seconds
    _LAST.clear()
    _reset_deadline()


def set_clock(now, sleep) -> None:
    """Tests advance time here. Production uses the monotonic clock and time.sleep."""
    global _NOW, _SLEEP
    _NOW = now if callable(now) else time.monotonic
    _SLEEP = sleep if callable(sleep) else time.sleep
    _LAST.clear()
    _reset_deadline()


def set_decision_timeout(seconds: float | None) -> None:
    global _DECISION_TIMEOUT
    _DECISION_TIMEOUT = TIMEOUT_S if seconds is None else seconds
    _reset_deadline()


def _reset_deadline() -> None:
    if hasattr(_CALL, "deadline"):
        del _CALL.deadline


def install() -> None:
    """Wire the production reader when the desk has not already installed one."""
    global _INSTALLED
    if cross_source.fetcher_installed():
        return
    cross_source.set_fetcher(production_fetch)
    _INSTALLED = True


def shutdown() -> None:
    global _INSTALLED
    if not _INSTALLED:
        return
    cross_source.clear_fetcher()
    _INSTALLED = False
    _LAST.clear()


def production_fetch(kind, mint, pair_id=None):
    _start_decision()
    if not _safe_address(mint):
        raise cross_source.FetchFailed("mint")
    if kind == "dex":
        # token-pairs/v1 returns a JSON array of pair objects. The saved
        # dex_pair files are the other endpoint, latest/dex/pairs, whose body
        # is {schemaVersion, pairs}. The live mint read was tokens/v1 and is
        # that same array. select_pair sees {pairs: array} either way.
        return _json("dexscreener", "GET", dexscreener_public.pairs_url(mint), _plain())
    if kind == "rugcheck":
        return _json("rugcheck", "GET", "https://api.rugcheck.xyz/v1/tokens/" + mint + "/report", _plain())
    if kind == "solana_tracker_token":
        return _json("solana_tracker", "GET", "https://data.solanatracker.io/tokens/" + mint, _tracker())
    if kind == "holders_chart":
        return _json("solana_tracker", "GET", "https://data.solanatracker.io/holders/chart/" + mint, _tracker())
    if kind == "deployer":
        creator = _creator(mint)
        return _json("solana_tracker", "GET", "https://data.solanatracker.io/deployer/" + creator, _tracker())
    if kind == "das":
        creator = _creator(mint)
        return _json("helius", "POST", "https://mainnet.helius-rpc.com/", _helius(), _das_body(creator))
    raise cross_source.FetchFailed("provider_unparsed")


def fetch_hermes(now: float) -> dict:
    """One Hermes SOL/USD read. A 401 or 403 is price_unknown. No on-chain substitute."""
    _start_decision()
    if not _present("PYTH_API_KEY"):
        return {"ok": False, "reason": "price_unknown", "cause": "pyth_api_key_missing", "onchain_substituted": False, "live": False}
    try:
        status, body = _exchange("hermes", "GET", pyth_sponsored.HERMES_LATEST_URL, pyth_sponsored.hermes_headers(), None)
    except cross_source.FetchFailed as exc:
        return {"ok": False, "reason": "price_unknown", "cause": exc.reason, "onchain_substituted": False, "live": False}
    return pyth_sponsored.read_hermes_response({"status": status, "body": body}, now)


def _json(provider: str, method: str, url: str, headers: dict, payload=None):
    status, body = _exchange(provider, method, url, headers, payload)
    if status == 403:
        raise cross_source.FetchFailed("hermes_unauthorized" if provider == "hermes" else provider + "_forbidden")
    if status == 429:
        raise cross_source.FetchFailed(provider + "_rate_limited")
    if status != 200:
        raise cross_source.FetchFailed(provider + "_status")
    if provider == "dexscreener" and "/token-pairs/v1/" in url and isinstance(body, list):
        if any(type(item) is not dict for item in body):
            raise cross_source.FetchFailed("dexscreener_unparsed")
        return {"pairs": body}
    if type(body) is not dict:
        raise cross_source.FetchFailed(provider + "_unparsed")
    return body


def _exchange(provider: str, method: str, url: str, headers: dict, payload):
    _pace(provider)
    spec = {"provider": provider, "method": method, "url": url, "headers": dict(headers), "json": payload}
    try:
        if callable(_TRANSPORT):
            result = _TRANSPORT(spec)
        else:
            result = _urllib(spec)
    except cross_source.FetchFailed:
        raise
    except TimeoutError:
        raise cross_source.FetchFailed(provider + "_timeout") from None
    except Exception:
        raise cross_source.FetchFailed(provider + "_status") from None
    if not isinstance(result, dict) or type(result.get("status")) is not int:
        raise cross_source.FetchFailed(provider + "_unparsed")
    return result.get("status"), result.get("body")


def _urllib(spec: dict) -> dict:
    headers = {key: value for key, value in spec["headers"].items() if not key.startswith("_")}
    url = spec["url"]
    key = spec["headers"].get("_helius_key")
    data = None if spec.get("json") is None else json.dumps(spec["json"]).encode("utf-8")
    if isinstance(key, str) and key != "":
        import solana_rpc
        keyed = url + "?api-key=" + key
        reason = None
        try:
            return solana_rpc.exchange(keyed, data or b"", timeout=_remaining(), headers=headers)
        except solana_rpc.RpcRefused as exc:
            reason = exc.reason
        del keyed, key, data, url
        if reason == "rpc_bad_json":
            raise cross_source.FetchFailed("rpc_bad_json")
        if reason == "rpc_timeout":
            raise cross_source.FetchFailed(spec["provider"] + "_timeout")
        raise cross_source.FetchFailed(spec["provider"] + "_status")
    request = urllib.request.Request(url, data=data, headers=headers, method=spec["method"])
    status, raw = _read(request, _remaining())
    if status == "timeout":
        raise cross_source.FetchFailed(spec["provider"] + "_timeout")
    if type(status) is not int:
        raise cross_source.FetchFailed(spec["provider"] + "_status")
    parsed = None
    if isinstance(raw, (bytes, bytearray)) and raw:
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            parsed = None
    return {"status": status, "body": parsed}


def _read(request, timeout: float):
    """Open one request. Failures stay in this frame so the URL is not an exception."""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except TimeoutError:
        return "timeout", None
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), TimeoutError):
            return "timeout", None
        return "status", None
    except Exception:
        return "status", None


def _start_decision() -> None:
    timeout = _DECISION_TIMEOUT if _DECISION_TIMEOUT is not None else TIMEOUT_S
    _CALL.deadline = _NOW() + timeout


def _pace(provider: str) -> None:
    """Reserve the next slot, then sleep outside the lock. A 429 is separate."""
    gap = GAPS[provider] if _GAP is None else _GAP
    with _LOCKS[provider]:
        now = _NOW()
        deadline = getattr(_CALL, "deadline", None)
        if deadline is None:
            timeout = _DECISION_TIMEOUT if _DECISION_TIMEOUT is not None else TIMEOUT_S
            deadline = now + timeout
            _CALL.deadline = deadline
        last = _LAST.get(provider)
        slot = now if last is None or gap <= 0 else max(now, last + gap)
        if slot > deadline + 1e-9:
            raise cross_source.FetchFailed(provider + "_rate_limited")
        _LAST[provider] = slot
    wait = slot - _NOW()
    if wait > 1e-9:
        _SLEEP(wait)


def _remaining() -> float:
    deadline = getattr(_CALL, "deadline", None)
    if deadline is None:
        return TIMEOUT_S
    return max(0.001, deadline - _NOW())


def _creator(mint: str) -> str:
    body = _json("solana_tracker", "GET", "https://data.solanatracker.io/tokens/" + mint, _tracker())
    token = body.get("token") if isinstance(body.get("token"), dict) else {}
    creation = token.get("creation") if isinstance(token.get("creation"), dict) else {}
    creator = creation.get("creator")
    if not _safe_address(creator):
        raise cross_source.FetchFailed("solana_tracker_unparsed")
    return creator


def _das_body(creator: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAssetsByCreator",
        "params": {"creatorAddress": creator, "onlyVerified": False, "page": 1, "limit": 20},
    }


def _plain() -> dict:
    return {"Accept": "application/json", "User-Agent": USER_AGENT}


def _tracker() -> dict:
    key = _secret("SOLANA_TRACKER_API_KEY")
    if key is None:
        raise cross_source.FetchFailed("solana_tracker_api_key_missing")
    return {"Accept": "application/json", "User-Agent": USER_AGENT, "x-api-key": key}


def _helius() -> dict:
    key = _secret("HELIUS_API_KEY")
    if key is None:
        raise cross_source.FetchFailed("helius_api_key_missing")
    return {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": USER_AGENT, "_helius_key": key}


def _present(name: str) -> bool:
    return _secret(name) is not None


def _secret(name: str) -> str | None:
    import os
    value = os.environ.get(name)
    if type(value) is not str or value.strip() == "":
        return None
    text = value.strip()
    if text not in _SECRETS:
        _SECRETS.append(text)
    return text


def _safe_address(value) -> bool:
    if type(value) is not str or len(value) < 32 or "/" in value or "?" in value or "&" in value:
        return False
    return True
