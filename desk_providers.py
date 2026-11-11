"""The desk's own readers. A caller body is not one of these.

Keys come from the environment. They are not logged, stored, or put in a
failure reason. DexScreener and RugCheck need no key. Solana Tracker uses
SOLANA_TRACKER_API_KEY. Helius DAS uses HELIUS_API_KEY. Hermes uses
PYTH_API_KEY. Nothing here signs, simulates, or sends.
"""

from __future__ import annotations

import json
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


def set_gap(seconds: float | None) -> None:
    global _GAP
    _GAP = seconds
    _LAST.clear()


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
    if not _safe_address(mint):
        raise cross_source.FetchFailed("mint")
    if kind == "dex":
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
    if not isinstance(body, dict):
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
    except (TimeoutError, urllib.error.URLError):
        raise cross_source.FetchFailed(provider + "_timeout")
    except Exception:
        raise cross_source.FetchFailed(provider + "_status")
    if not isinstance(result, dict) or type(result.get("status")) is not int:
        raise cross_source.FetchFailed(provider + "_unparsed")
    return result.get("status"), result.get("body")


def _urllib(spec: dict) -> dict:
    headers = {key: value for key, value in spec["headers"].items() if not key.startswith("_")}
    url = spec["url"]
    key = spec["headers"].get("_helius_key")
    if isinstance(key, str) and key != "":
        url = url + "?api-key=" + key
    data = None if spec.get("json") is None else json.dumps(spec["json"]).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=spec["method"])
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "body": None}
    parsed = None
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else None
    except Exception:
        parsed = None
    return {"status": status, "body": parsed}


def _pace(provider: str) -> None:
    gap = GAPS[provider] if _GAP is None else _GAP
    if gap <= 0:
        return
    now = time.monotonic()
    last = _LAST.get(provider, 0.0)
    if now - last < gap:
        raise cross_source.FetchFailed(provider + "_rate_limited")
    _LAST[provider] = now


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
