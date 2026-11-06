"""Scanner, agent 1 of 5. Counts and cuts.

Known deviation (owner): PumpPortal migration messages carry no creator and
`pool` is the label "pump-amm". Pool is the DexScreener pumpswap pairAddress.
Creator is RugCheck report `creator`.

getTokenLargestAccounts is often a JSON-RPC 429 on the public RPC. fields.py
has no success-body paths for that call (the captured sample is the error).
After the auditor retry rule (3 times, 2s apart), no parsed top-10 is a kill.
No data is never a pass.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import db
import fields
import links
from bus import Desk, call_with_retries

log = logging.getLogger("scanner")

PUMPPORTAL_URL = "wss://pumpportal.fun/api/data"
DEX_URL = "https://api.dexscreener.com/tokens/v1/solana/"
RUGCHECK_REPORT = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"


async def sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def format_liq(usd: float) -> str:
    return f"liq ${usd / 1000:.1f}K"


def format_top10(pct: float) -> str:
    return f"top 10 hold {pct:.0f}%"


def format_h1(pct: float) -> str:
    return f"{pct:+.0f}% in 1h"


def kill_reason(snapshot: dict, settings: dict | None = None) -> dict:
    """First matching kill, in the prompt's order. verdict is pass, kill, or wait.

    max_wait applies only while there is still no PumpSwap pair. The prompt's
    reason string for that wait was truncated; this uses "waited {n}m".
    A coin that already clears the kill list is passed even if it is older
    than max_wait (it should have left the room at min_pool_age).
    """
    cfg = settings or _settings()
    age = snapshot.get("age_min")
    has_pair = bool(snapshot.get("has_pair"))
    if not has_pair:
        if age is not None and age > cfg["max_wait_min"]:
            return {"verdict": "kill", "reason": f"waited {int(age)}m"}
        return {"verdict": "wait", "reason": None}

    liquidity = snapshot.get("liquidity_usd")
    if liquidity is None:
        return {"verdict": "wait", "reason": None}
    if liquidity < cfg["min_liquidity_usd"]:
        return {"verdict": "kill", "reason": format_liq(liquidity)}

    top10 = snapshot.get("top10_pct")
    if top10 is None:
        # Captured getTokenLargestAccounts body is an RPC 429. Do not pass.
        return {"verdict": "kill", "reason": "no data: top 10"}
    if top10 > cfg["max_top10_pct"]:
        return {"verdict": "kill", "reason": format_top10(top10)}

    h1 = snapshot.get("h1_change_pct")
    if h1 is not None and h1 > cfg["max_h1_change_pct"]:
        return {"verdict": "kill", "reason": format_h1(h1)}

    copied = _copycat(snapshot.get("name"), snapshot.get("symbol"), snapshot.get("runners") or [])
    if copied:
        return {"verdict": "kill", "reason": f"copycat of {copied}"}

    if age is not None and age < cfg["min_pool_age_min"]:
        return {"verdict": "wait", "reason": None}
    return {"verdict": "pass", "reason": None}


def _copycat(name: str | None, symbol: str | None, runners: list[dict]) -> str | None:
    name_key = (name or "").casefold()
    symbol_key = (symbol or "").casefold()
    for runner in runners:
        other_name = (runner.get("name") or "").casefold()
        other_symbol = (runner.get("symbol") or "").casefold()
        same_name = bool(name_key) and name_key == other_name
        same_symbol = bool(symbol_key) and symbol_key == other_symbol
        if same_name or same_symbol:
            return runner.get("symbol") or runner.get("name") or "X"
    return None


def _settings() -> dict[str, float]:
    return {
        "min_liquidity_usd": db.get_float("min_liquidity_usd"),
        "max_top10_pct": db.get_float("max_top10_pct"),
        "max_h1_change_pct": db.get_float("max_h1_change_pct"),
        "min_pool_age_min": db.get_float("min_pool_age_min"),
        "max_wait_min": db.get_float("max_wait_min"),
        "runner_h24_pct": db.get_float("runner_h24_pct"),
    }


def parse_top10(payload: Any, supply_ui: float | None, exclude: set[str]) -> float | None:
    """Read largest accounts only through fields.py.

    The map's paths are None because the captured call was a 429, so a body
    cannot be turned into a percentage. Error bodies are no data. Returning
    None is a kill, never a pass.
    """
    if not isinstance(payload, dict) or payload.get("error"):
        return None
    accounts_path = fields.RPC_GET_TOKEN_LARGEST_ACCOUNTS.get("accounts")
    if not accounts_path or not supply_ui:
        return None
    accounts = fields.get(payload, accounts_path)
    if not isinstance(accounts, list) or not accounts:
        return None
    amount_path = fields.RPC_GET_TOKEN_LARGEST_ACCOUNTS.get("uiAmount")
    address_path = fields.RPC_GET_TOKEN_LARGEST_ACCOUNTS.get("address")
    if not amount_path or not address_path:
        return None
    ranked = []
    for account in accounts:
        address = fields.get(account, address_path)
        amount = fields.get(account, amount_path)
        if address in exclude or amount is None:
            continue
        ranked.append(float(amount))
    if not ranked:
        return None
    top = sorted(ranked, reverse=True)[:10]
    return 100.0 * sum(top) / float(supply_ui)


class Scanner:
    def __init__(self, desk: Desk) -> None:
        self.desk = desk
        self.runners: list[dict] = []
        self.waiting: dict[str, dict] = {}

    async def run(self) -> None:
        """Re-read the waiting room every poll_s. The socket delivers new migrations."""
        while True:
            db.beat("scanner", self.desk.now())
            for message in list(self.waiting.values()):
                await self.ingest(message)
            await asyncio.sleep(db.get_float("poll_s"))

    async def ingest(self, message: dict, runners: list[dict] | None = None) -> dict:
        """Handle one migration message. Returns the evaluation."""
        db.beat("scanner", self.desk.now())
        mint = fields.get(message, fields.MIGRATION["mint"])
        signature = fields.get(message, fields.MIGRATION["signature"])
        creator = fields.get(message, fields.MIGRATION["creator"]) if fields.MIGRATION["creator"] else None
        now = self.desk.now()
        pair = await self._dex_pair(mint)
        report = await self._report(mint)
        if not creator and isinstance(report, dict):
            creator = fields.get(report, fields.RUGCHECK_REPORT["creator"])
        creator_balance = None
        if isinstance(report, dict):
            creator_balance = fields.get(report, fields.RUGCHECK_REPORT.get("creatorBalance"))
        name = symbol = None
        pool = None
        liquidity = h1 = h24 = price = volume_m5 = age_min = None
        created_ms = None
        if pair:
            pool = fields.get(pair, fields.DEXSCREENER["pairAddress"])
            name = fields.get(pair, fields.DEXSCREENER["baseToken.name"])
            symbol = fields.get(pair, fields.DEXSCREENER["baseToken.symbol"])
            liquidity = _num(fields.get(pair, fields.DEXSCREENER["liquidity.usd"]))
            h1 = _num(fields.get(pair, fields.DEXSCREENER["priceChange.h1"]))
            h24 = _num(fields.get(pair, fields.DEXSCREENER["priceChange.h24"]))
            price = _num(fields.get(pair, fields.DEXSCREENER["priceUsd"]))
            volume_m5 = _num(fields.get(pair, fields.DEXSCREENER["volume.m5"]))
            created_ms = fields.get(pair, fields.DEXSCREENER["pairCreatedAt"])
            if created_ms:
                age_min = (now - float(created_ms) / 1000.0) / 60.0
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
            (mint, name, symbol, pool, now, creator, creator_balance),
        )
        if price is not None:
            db.execute(
                "INSERT INTO prices (mint, ts, price, volume_m5) VALUES (?, ?, ?, ?)",
                (mint, now, price, volume_m5),
            )
        top10 = None
        if pair and liquidity is not None and liquidity >= db.get_float("min_liquidity_usd"):
            top10 = await self._top10(mint, pool)
        snapshot = {
            "liquidity_usd": liquidity,
            "top10_pct": top10,
            "h1_change_pct": h1,
            "name": name,
            "symbol": symbol,
            "age_min": age_min,
            "has_pair": pair is not None,
            "runners": runners if runners is not None else self._runners_for(mint),
        }
        if h24 is not None and h24 >= db.get_float("runner_h24_pct"):
            self.runners.append({"mint": mint, "name": name, "symbol": symbol})
        result = kill_reason(snapshot)
        db.execute(
            """
            INSERT INTO scans (
                mint, ts, liquidity_usd, top10_pct, h1_change_pct, age_min, verdict, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (mint, now, liquidity, top10, h1, age_min, result["verdict"], result["reason"]),
        )
        if mint:
            if result["verdict"] == "wait":
                self.waiting[mint] = message
            else:
                self.waiting.pop(mint, None)
        if result["verdict"] == "kill":
            url = links.coin_url(mint, "solana")
            label = symbol or name or mint
            db.record_activity(
                now, "rejected", mint, "scanner",
                f"Rejected {label}", f"Scanner · {result['reason']}", "Skipped",
            )
            self.desk.notifier.push(
                f"Rejected {label}",
                f"Scanner · {result['reason']}",
                url,
                [{"label": "Open on FOMO", "url": url}],
            )
            log.info("kill %s %s sig=%s", mint, result["reason"], signature)
        elif result["verdict"] == "pass":
            payload = {
                "agent": "scanner",
                "mint": mint,
                "name": name,
                "symbol": symbol,
                "pool": pool,
                "creator": creator,
                "price": price,
                "liquidity_usd": liquidity,
                "top10_pct": top10,
                "h1_change_pct": h1,
                "age_min": age_min,
                "pair": pair,
                "report": report,
            }
            await self.desk.bus.auditor.put(payload)
            log.info("pass %s", mint)
        return {"mint": mint, "pool": pool, "creator": creator, **result, "snapshot": snapshot}

    def _runners_for(self, mint: str) -> list[dict]:
        out = []
        for runner in self.runners:
            if runner.get("mint") != mint:
                copied = dict(runner)
                copied["self_mint"] = mint
                out.append(copied)
        return out

    async def _dex_pair(self, mint: str) -> dict | None:
        if not mint:
            return None
        try:
            payload = await self.desk.http.get_json(DEX_URL + mint)
        except Exception:
            log.warning("dexscreener failed for %s", mint)
            return None
        return fields.dexscreener_select_pair(payload)

    async def _report(self, mint: str) -> dict | None:
        try:
            payload = await self.desk.http.get_json(RUGCHECK_REPORT.format(mint=mint))
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    async def _top10(self, mint: str, pool: str | None) -> float | None:
        tries = int(db.get_float("audit_retries") or 3)
        delay = db.get_float("audit_retry_s") or 2

        async def once():
            return await self._rpc(
                "getTokenLargestAccounts",
                [mint],
            )

        payload = await call_with_retries(once, tries, delay, sleep)
        supply = await self._supply(mint)
        exclude = {pool} if pool else set()
        return parse_top10(payload, supply, exclude)

    async def _supply(self, mint: str) -> float | None:
        payload = await self._rpc("getTokenSupply", [mint])
        if not isinstance(payload, dict) or payload.get("error"):
            return None
        return _num(fields.get(payload, fields.RPC_GET_TOKEN_SUPPLY["uiAmount"]))

    async def _rpc(self, method: str, params: list) -> Any:
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        return await self.desk.http.post_json(db.get_setting("rpc_url"), body)


async def pumpportal_loop(scanner: Scanner) -> None:
    """The desk's single PumpPortal socket. Trade streams are not subscribed."""
    import websockets

    while True:
        try:
            async with websockets.connect(PUMPPORTAL_URL) as ws:
                await ws.send(json.dumps({"method": "subscribeMigration"}))
                async for raw in ws:
                    try:
                        message = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(message, dict) and message.get("mint"):
                        await scanner.ingest(message)
        except Exception:
            log.warning("pumpportal socket closed; backing off")
            await asyncio.sleep(db.get_float("rpc_backoff_s") or 30)


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
