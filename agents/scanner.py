"""Scanner, agent 1 of 5. Counts and cuts.

Pool comes from the DexScreener pumpswap pairAddress. The migration message's
pool is the label "pump-amm" and it carries no creator (fields.py). Creator
and the balance stored at migration come from the RugCheck report.
"""

from __future__ import annotations

import asyncio
import json
import logging
import unicodedata
from typing import Any

import db
import fields
import links
from bus import Desk, call_with_retries, set_scanner_price_mints

log = logging.getLogger("scanner")


def _paused(name: str) -> bool:
    from api import is_paused
    return is_paused(name)

PUMPPORTAL_URL = "wss://pumpportal.fun/api/data"
DEX_URL = "https://api.dexscreener.com/tokens/v1/solana/"
RUGCHECK_REPORT = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
# Bracketed in the agent text, not given their own STEP 10 names.
WS_SILENT_S = 120
WS_PING_S = 10
DEX_BACKOFF_S = 30
RETRY_GAP_S = 2


async def sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def format_liq(usd: float) -> str:
    return f"liq ${usd / 1000:.1f}K"


def format_top10(pct: float) -> str:
    return f"top 10 hold {pct:.0f}%"


def format_h1(pct: float) -> str:
    return f"{pct:+.0f}% in 1h"


def normalize_name(value: str | None) -> str:
    """Lowercase, strip spaces, digits and emoji."""
    if not value:
        return ""
    out = []
    for char in value.casefold():
        if char.isspace() or char.isdigit():
            continue
        if unicodedata.category(char).startswith("So"):
            continue
        out.append(char)
    return "".join(out)


def copycat_of(name: str | None, symbol: str | None, runners: list[dict]) -> str | None:
    """Exact match, or one normalized name inside the other, vs today's runners."""
    keys = [item for item in (normalize_name(name), normalize_name(symbol)) if item]
    if not keys:
        return None
    for runner in runners:
        others = [item for item in (normalize_name(runner.get("name")), normalize_name(runner.get("symbol"))) if item]
        label = runner.get("symbol") or runner.get("name") or "X"
        for key in keys:
            for other in others:
                if key == other or key in other or other in key:
                    return label
    return None


def kill_reason(snapshot: dict, settings: dict | None = None) -> dict:
    """First kill in the prompt's order. Wait is internal; pass and kill are decisions.

    A missing PumpSwap pair or a missing top-10 is not a listed kill. The coin
    stays in the room. After max_wait_min without a pass the reason is
    "never qualified".
    """
    cfg = settings or _settings()
    age = snapshot.get("age_min")
    room = snapshot.get("room_min", age)
    expired = room is not None and room > cfg["max_wait_min"]

    def give_up() -> dict:
        if expired:
            return {"verdict": "kill", "reason": "never qualified"}
        return {"verdict": "wait", "reason": None}

    if not snapshot.get("has_pair"):
        return give_up()

    liquidity = snapshot.get("liquidity_usd")
    if liquidity is None:
        return give_up()
    if liquidity < cfg["min_liquidity_usd"]:
        return {"verdict": "kill", "reason": format_liq(liquidity)}

    top10 = snapshot.get("top10_pct")
    if top10 is None:
        return give_up()
    if top10 > cfg["max_top10_pct"]:
        return {"verdict": "kill", "reason": format_top10(top10)}

    h1 = snapshot.get("h1_change_pct")
    if h1 is not None and h1 > cfg["max_h1_change_pct"]:
        return {"verdict": "kill", "reason": format_h1(h1)}

    copied = copycat_of(snapshot.get("name"), snapshot.get("symbol"), snapshot.get("runners") or [])
    if copied:
        return {"verdict": "kill", "reason": f"copycat of {copied}"}

    if age is None or age < cfg["min_pool_age_min"]:
        return give_up()
    return {"verdict": "pass", "reason": None}


def parse_largest_accounts(payload: Any, supply_ui: float | None, exclude: set[str]) -> float | None:
    """Top-10 percent from getTokenLargestAccounts.

    fields.py records no success paths: the captured body is a JSON-RPC 429.
    A body with result.value (the Solana RPC shape) is readable. An error, or
    a body with no accounts, is no data and must not become a pass.
    Pool and bonding-curve accounts are dropped before the sum. The RPC
    sample has no owner field, so an account is dropped when its address or
    owner equals an excluded pubkey.
    """
    if not isinstance(payload, dict) or payload.get("error") or not supply_ui:
        return None
    accounts = fields.get(payload, fields.RPC_GET_TOKEN_LARGEST_ACCOUNTS.get("accounts"))
    if accounts is None:
        value = payload.get("result")
        if isinstance(value, dict):
            accounts = value.get("value")
    if not isinstance(accounts, list) or not accounts:
        return None
    amounts = []
    for account in accounts:
        if not isinstance(account, dict):
            continue
        address = account.get("address")
        owner = account.get("owner")
        if address in exclude or owner in exclude:
            continue
        amount = account.get("uiAmount")
        if amount is None and account.get("amount") is not None and account.get("decimals") is not None:
            amount = float(account["amount"]) / (10 ** int(account["decimals"]))
        if amount is None:
            continue
        amounts.append(float(amount))
    if not amounts:
        return None
    return 100.0 * sum(sorted(amounts, reverse=True)[:10]) / float(supply_ui)


def _settings() -> dict[str, float]:
    return {
        "min_liquidity_usd": db.get_float("min_liquidity_usd"),
        "max_top10_pct": db.get_float("max_top10_pct"),
        "max_h1_change_pct": db.get_float("max_h1_change_pct"),
        "min_pool_age_min": db.get_float("min_pool_age_min"),
        "max_wait_min": db.get_float("max_wait_min"),
        "runner_h24_pct": db.get_float("runner_h24_pct"),
    }


class Scanner:
    def __init__(self, desk: Desk) -> None:
        self.desk = desk
        self.runners: list[dict] = []
        self.waiting: dict[str, dict] = {}
        self.passed: set[str] = set()
        self.room_started: dict[str, float] = {}

    async def run(self) -> None:
        while True:
            db.beat("scanner", self.desk.now())
            if not _paused("scanner"):
                await self._poll_room()
            await asyncio.sleep(db.get_float("poll_s"))

    async def _poll_room(self) -> None:
        mints = list(self.waiting)
        set_scanner_price_mints(set(mints))
        if not mints:
            return
        pairs = await self._dex_batch(mints)
        for mint in mints:
            message = self.waiting.get(mint)
            if message is not None:
                await self.ingest(message, pair=pairs.get(mint))

    async def ingest(self, message: dict, runners: list[dict] | None = None, pair: dict | None = None) -> dict:
        db.beat("scanner", self.desk.now())
        mint = fields.get(message, fields.MIGRATION["mint"])
        if _paused("scanner"):
            if mint:
                self.waiting[mint] = message
                self.room_started.setdefault(mint, self.desk.now())
            return {"mint": mint, "verdict": "wait", "reason": "paused"}
        if mint in self.passed:
            return {"mint": mint, "verdict": "pass", "reason": None, "duplicate": True}
        now = self.desk.now()
        self.room_started.setdefault(mint, now)
        signature = fields.get(message, fields.MIGRATION["signature"])
        creator = fields.get(message, fields.MIGRATION["creator"]) if fields.MIGRATION["creator"] else None
        if pair is None:
            pair = await self._dex_pair(mint)
        report = await self._report(mint)
        if not creator and isinstance(report, dict):
            creator = fields.get(report, fields.RUGCHECK_REPORT["creator"])
        creator_balance = None
        if isinstance(report, dict):
            creator_balance = fields.get(report, fields.RUGCHECK_REPORT.get("creatorBalance"))
        name = symbol = pool = None
        liquidity = h1 = h24 = price = volume_m5 = None
        curve = None
        if pair:
            pool = fields.get(pair, fields.DEXSCREENER["pairAddress"])
            name = fields.get(pair, fields.DEXSCREENER["baseToken.name"])
            symbol = fields.get(pair, fields.DEXSCREENER["baseToken.symbol"])
            liquidity = _num(fields.get(pair, fields.DEXSCREENER["liquidity.usd"]))
            h1 = _num(fields.get(pair, fields.DEXSCREENER["priceChange.h1"]))
            h24 = _num(fields.get(pair, fields.DEXSCREENER["priceChange.h24"]))
            price = _num(fields.get(pair, fields.DEXSCREENER["priceUsd"]))
            volume_m5 = _num(fields.get(pair, fields.DEXSCREENER["volume.m5"]))
        if not name and isinstance(report, dict):
            name = fields.get(report, fields.RUGCHECK_REPORT.get("tokenMeta.name"))
            symbol = fields.get(report, fields.RUGCHECK_REPORT.get("tokenMeta.symbol"))
        db.execute(
            """
            INSERT INTO coins (
                mint, name, symbol, pool, migrated_at, creator, creator_balance_at_migration, holders
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(mint) DO UPDATE SET
                name = COALESCE(excluded.name, coins.name),
                symbol = COALESCE(excluded.symbol, coins.symbol),
                pool = COALESCE(excluded.pool, coins.pool),
                creator = COALESCE(excluded.creator, coins.creator),
                creator_balance_at_migration = COALESCE(
                    excluded.creator_balance_at_migration, coins.creator_balance_at_migration
                )
            """,
            (mint, name, symbol, pool, self.room_started[mint], creator, creator_balance),
        )
        if price is not None:
            db.execute(
                "INSERT INTO prices (mint, ts, price, volume_m5) VALUES (?, ?, ?, ?)",
                (mint, now, price, volume_m5),
            )
        age_min = (now - self.room_started[mint]) / 60.0
        top10 = None
        if pair and liquidity is not None and liquidity >= db.get_float("min_liquidity_usd"):
            top10 = await self._top10(mint, {item for item in (pool, curve) if item})
        if h24 is not None and h24 >= db.get_float("runner_h24_pct"):
            self.runners.append({"mint": mint, "name": name, "symbol": symbol})
        result = kill_reason({
            "liquidity_usd": liquidity,
            "top10_pct": top10,
            "h1_change_pct": h1,
            "name": name,
            "symbol": symbol,
            "age_min": age_min,
            "room_min": age_min,
            "has_pair": pair is not None,
            "runners": runners if runners is not None else self._runners_for(mint),
        })
        if result["verdict"] == "wait":
            if mint:
                self.waiting[mint] = message
            return {"mint": mint, "pool": pool, "creator": creator, **result}
        self.waiting.pop(mint, None)
        db.execute(
            """
            INSERT INTO scans (
                mint, ts, liquidity_usd, top10_pct, h1_change_pct, age_min, verdict, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (mint, now, liquidity, top10, h1, age_min, result["verdict"], result["reason"]),
        )
        log.info(
            "ts=%s mint=%s verdict=%s reason=%s liq=%s top10=%s h1=%s age=%s sig=%s",
            now, mint, result["verdict"], result["reason"], liquidity, top10, h1, age_min, signature,
        )
        self.desk.bus.publish("scan", {
            "agent": "scanner", "mint": mint, "name": name, "symbol": symbol, "pool": pool,
            "liquidityUsd": liquidity, "top10Pct": top10, "h1ChangePct": h1, "ageMin": age_min,
            "verdict": result["verdict"], "reason": result["reason"], "ts": now,
        })
        label = symbol or name or mint
        if result["verdict"] == "kill":
            db.record_activity(
                now, "rejected", mint, "scanner",
                f"Rejected {label}", f"Scanner · {result['reason']}", "Skipped",
            )
            if db.flag_on("push_rejects"):
                url = links.coin_url(mint, "solana")
                self.desk.notifier.push(
                    f"Rejected {label}", f"Scanner · {result['reason']}", url,
                    [{"label": "Open on FOMO", "url": url}],
                )
        else:
            self.passed.add(mint)
            await self.desk.bus.auditor.put({
                "agent": "scanner",
                "mint": mint,
                "name": name,
                "symbol": symbol,
                "pool": pool,
                "creator": creator,
                "creator_balance_at_migration": creator_balance,
                "migrated_at": self.room_started[mint],
                "price": price,
                "liquidityUsd": liquidity,
                "liquidity_usd": liquidity,
                "top10Pct": top10,
                "top10_pct": top10,
                "h1ChangePct": h1,
                "h1_change_pct": h1,
                "ageMin": age_min,
                "age_min": age_min,
                "verdict": "pass",
                "reason": None,
                "pair": pair,
                "report": report,
            })
        return {"mint": mint, "pool": pool, "creator": creator, **result}

    def _runners_for(self, mint: str) -> list[dict]:
        return [runner for runner in self.runners if runner.get("mint") != mint]

    async def _dex_pair(self, mint: str) -> dict | None:
        pairs = await self._dex_batch([mint])
        return pairs.get(mint)

    async def _dex_batch(self, mints: list[str]) -> dict[str, dict]:
        if not mints:
            return {}
        try:
            payload = await self.desk.http.get_json(DEX_URL + ",".join(mints))
        except Exception as exc:
            if getattr(exc, "status", None) == 429 or "429" in str(exc):
                log.warning("dexscreener 429; keeping the waiting room")
                await sleep(DEX_BACKOFF_S)
            else:
                log.warning("dexscreener failed")
            return {}
        found: dict[str, dict] = {}
        if isinstance(payload, dict):
            payload = payload.get("pairs") or []
        if not isinstance(payload, list):
            return {}
        for mint in mints:
            own = [item for item in payload if isinstance(item, dict) and fields.get(item, "baseToken.address") == mint]
            pair = fields.dexscreener_select_pair(own or payload)
            if pair and (not own or fields.get(pair, "baseToken.address") in (None, mint)):
                if own:
                    found[mint] = fields.dexscreener_select_pair(own) or pair
                elif len(mints) == 1:
                    found[mint] = pair
        return found

    async def _report(self, mint: str) -> dict | None:
        try:
            payload = await self.desk.http.get_json(RUGCHECK_REPORT.format(mint=mint))
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    async def _top10(self, mint: str, exclude: set[str]) -> float | None:
        tries = int(db.get_float("retries") or 3)

        async def once():
            return await self._rpc("getTokenLargestAccounts", [mint])

        payload = await call_with_retries(once, tries, RETRY_GAP_S, sleep)
        supply = await self._supply(mint)
        return parse_largest_accounts(payload, supply, exclude)

    async def _supply(self, mint: str) -> float | None:
        payload = await self._rpc("getTokenSupply", [mint])
        if not isinstance(payload, dict) or payload.get("error"):
            return None
        return _num(fields.get(payload, fields.RPC_GET_TOKEN_SUPPLY["uiAmount"]))

    async def _rpc(self, method: str, params: list) -> Any:
        import solana_rpc
        return solana_rpc.call({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})


async def migration_silence(ws) -> str:
    """A quiet socket that answers a ping stays up. A failed ping is a reconnect.

    Only the reconnect is counted toward the hourly alert. Trade streams are
    not subscribed.
    """
    try:
        pong = await ws.ping()
        await asyncio.wait_for(pong, timeout=WS_PING_S)
    except Exception:
        log.warning("pumpportal ping failed; reconnecting")
        db.note_ws_reconnect(kind="reconnect")
        return "reconnect"
    log.info("pumpportal quiet for %ss; socket still open", WS_SILENT_S)
    db.note_ws_reconnect(kind="silent")
    return "stay"


async def pumpportal_loop(scanner: Scanner) -> None:
    """One PumpPortal socket for the desk. Trade streams are not subscribed."""
    import websockets

    backoff = 1
    while True:
        try:
            async with websockets.connect(PUMPPORTAL_URL) as ws:
                backoff = 1
                await ws.send(json.dumps({"method": "subscribeMigration"}))
                log.info("pumpportal migration socket open")
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=WS_SILENT_S)
                    except asyncio.TimeoutError:
                        if await migration_silence(ws) == "reconnect":
                            break
                        continue
                    try:
                        message = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(message, dict) and message.get("mint"):
                        await scanner.ingest(message)
        except Exception:
            log.warning("pumpportal closed; backoff %ss", backoff)
            db.note_ws_reconnect(kind="reconnect")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
