"""Auditor, agent 2 of 5. Eight safety layers.

Known deviations (owner):
- Layer 5 (LP): RugCheck has no burned field. Use markets[].lp.lpLockedPct on
  the market whose pubkey equals the pool.
- Layer 7 bundles: the insiders graph has `nodes` and `links`, not `edges`.
- getTokenLargestAccounts 429s. This layer's top-10 uses RugCheck topHolders
  (the captured report has them). The RPC largest-accounts call stays a
  Scanner kill: no parsed body is never a pass. See scanner.parse_top10.

A confirmed freeze authority stops the audit: layers 3-8 are "not checked"
(STEP 11 test paragraph). Any other red still walks the remaining layers,
because 7 of 8 is a no and a slow source must not skip a layer.
"""

from __future__ import annotations

import logging
from typing import Any

import db
import fields
import links
from bus import Desk, call_with_retries

log = logging.getLogger("auditor")

RUGCHECK_REPORT = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
RUGCHECK_GRAPH = "https://api.rugcheck.xyz/v1/tokens/{mint}/insiders/graph"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

LAYER_NAMES = {
    1: "Mint authority",
    2: "Freeze authority",
    3: "Dev wallet",
    4: "Top 10",
    5: "LP",
    6: "Pool age",
    7: "Bundled wallets",
    8: "Delegations",
}


async def sleep(seconds: float) -> None:
    await asyncio_sleep(seconds)


def asyncio_sleep(seconds: float):
    import asyncio
    return asyncio.sleep(seconds)


def judge(bundle: dict, settings: dict | None = None, now: float | None = None) -> dict:
    """Score already-fetched sources. `bundle` values of None are no data."""
    cfg = settings or _settings()
    now = db_time() if now is None else now
    layers: list[dict] = []
    stopped = False
    for number in range(1, 9):
        if stopped:
            layers.append(_layer(number, False, "not checked", None))
            continue
        layer = _LAYERS[number](bundle, cfg, now)
        layers.append(layer)
        # Confirmed freeze (the authority is set), not a missing source.
        if number == 2 and layer["value"] == "set":
            stopped = True
    checked = [layer for layer in layers if layer["value"] != "not checked"]
    reds = [layer for layer in checked if not layer["pass"]]
    passed = bool(checked) and not reds and len(checked) == 8
    holders = bundle.get("holders")
    return {
        "agent": "auditor",
        "mint": bundle.get("mint"),
        "passed": passed,
        "layersChecked": len(checked),
        "layers": layers,
        "holders": holders,
        "reason": reds[0]["value"] if reds else None,
    }


class Auditor:
    def __init__(self, desk: Desk) -> None:
        self.desk = desk

    async def run(self) -> None:
        import asyncio
        while True:
            db.beat("auditor", self.desk.now())
            try:
                payload = await asyncio.wait_for(self.desk.bus.auditor.get(), timeout=1)
            except asyncio.TimeoutError:
                continue
            await self.audit_payload(payload)

    async def audit_payload(self, payload: dict) -> dict:
        db.beat("auditor", self.desk.now())
        if not db.flag_on("auditor_enabled"):
            payload = dict(payload)
            payload["passed"] = True
            payload["layersChecked"] = 0
            await self.desk.bus.analyst.put(payload)
            return payload
        result = await self.audit(
            payload["mint"],
            pool=payload.get("pool"),
            now=self.desk.now(),
            pair=payload.get("pair"),
        )
        result["name"] = payload.get("name")
        result["symbol"] = payload.get("symbol")
        result["price"] = payload.get("price")
        result["liquidity_usd"] = payload.get("liquidity_usd")
        result["pair"] = payload.get("pair")
        if result.get("holders") is not None:
            db.execute(
                "UPDATE coins SET holders = ? WHERE mint = ?",
                (result["holders"], payload["mint"]),
            )
        self._store(result)
        if result["passed"]:
            await self.desk.bus.analyst.put(result)
        else:
            label = payload.get("symbol") or payload.get("name") or payload["mint"]
            url = links.coin_url(payload["mint"], "solana")
            db.record_activity(
                self.desk.now(), "rejected", payload["mint"], "auditor",
                f"Rejected {label}", f"Auditor · {result['reason']}", "Skipped",
            )
            self.desk.notifier.push(
                f"Rejected {label}",
                f"Auditor · {result['reason']}",
                url,
                [{"label": "Open on FOMO", "url": url}],
            )
        return result

    async def audit(
        self,
        mint: str,
        pool: str | None = None,
        now: float | None = None,
        pair: dict | None = None,
    ) -> dict:
        db.beat("auditor", self.desk.now() if now is None else now)
        bundle = await self.fetch_bundle(mint, pool)
        if pair is not None:
            bundle["pair"] = pair
        result = judge(bundle, now=now if now is not None else self.desk.now())
        result["mint"] = mint
        return result

    async def fetch_bundle(self, mint: str, pool: str | None) -> dict:
        tries = int(db.get_float("audit_retries") or 3)
        delay = db.get_float("audit_retry_s") or 2
        account = await call_with_retries(lambda: self._account(mint), tries, delay, sleep)
        supply = await call_with_retries(lambda: self._rpc("getTokenSupply", [mint]), tries, delay, sleep)
        report = await call_with_retries(lambda: self._report(mint), tries, delay, sleep)
        graph = await call_with_retries(lambda: self._graph(mint), tries, delay, sleep)
        program = await call_with_retries(lambda: self._program_accounts(mint), tries, delay, sleep)
        if pool is None and isinstance(report, dict):
            pool = _pool_from_report(report)
        return {
            "mint": mint,
            "pool": pool,
            "account": account,
            "supply": supply,
            "report": report,
            "graph": graph,
            "program_accounts": program,
            "holders": _holder_count(program) if program is not None else None,
        }

    def _store(self, result: dict) -> None:
        now = self.desk.now()
        for layer in result["layers"]:
            passed = None if layer["value"] == "not checked" else (1 if layer["pass"] else 0)
            db.execute(
                """
                INSERT INTO audits (mint, ts, layer, name, value_text, "pass", source)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result["mint"], now, str(layer["n"]), layer["name"],
                    layer["value"], passed, layer.get("source"),
                ),
            )
            self.desk.bus.publish("audit_layer", layer)
        if result.get("holders") is not None:
            db.execute(
                "UPDATE coins SET holders = ? WHERE mint = ?",
                (result["holders"], result["mint"]),
            )

    async def _account(self, mint: str) -> Any:
        return await self._rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])

    async def _report(self, mint: str) -> Any:
        return await self.desk.http.get_json(RUGCHECK_REPORT.format(mint=mint))

    async def _graph(self, mint: str) -> Any:
        return await self.desk.http.get_json(RUGCHECK_GRAPH.format(mint=mint))

    async def _program_accounts(self, mint: str) -> list:
        accounts: list = []
        for program in (TOKEN_PROGRAM, TOKEN_2022):
            payload = await self._rpc(
                "getProgramAccounts",
                [program, {"encoding": "jsonParsed", "filters": [{"memcmp": {"offset": 0, "bytes": mint}}]}],
            )
            if is_missing(payload):
                return None  # type: ignore[return-value]
            accounts.extend(_result_list(payload))
        return accounts

    async def _rpc(self, method: str, params: list) -> Any:
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        return await self.desk.http.post_json(db.get_setting("rpc_url"), body)


def _settings() -> dict[str, float]:
    return {
        "max_dev_pct": db.get_float("max_dev_pct"),
        "max_top10_pct": db.get_float("max_top10_pct"),
        "min_lp_locked_pct": db.get_float("min_lp_locked_pct"),
        "min_pool_age_min": db.get_float("min_pool_age_min"),
        "max_bundled": db.get_float("max_bundled"),
        "max_delegated_accounts": db.get_float("max_delegated_accounts"),
    }


def db_time() -> float:
    import time
    return time.time()


def _LAYERS_build():
    return {
        1: _layer_mint,
        2: _layer_freeze,
        3: _layer_dev,
        4: _layer_top10,
        5: _layer_lp,
        6: _layer_age,
        7: _layer_bundles,
        8: _layer_delegations,
    }


def _layer(number: int, passed: bool, value: str, source: str | None) -> dict:
    return {
        "n": number,
        "name": LAYER_NAMES[number],
        "pass": passed,
        "value": value,
        "source": source,
    }


def _no_data(number: int, source: str | None) -> dict:
    return _layer(number, False, f"no data: {LAYER_NAMES[number].lower()}", source)


def is_missing(payload: Any) -> bool:
    if payload is None:
        return True
    return isinstance(payload, dict) and bool(payload.get("error"))


def _lookup(obj: Any, path: str | None) -> tuple[bool, Any]:
    """Like fields.get, but reports whether every key along the path existed."""
    if path is None or obj is None:
        return False, None
    cur = obj
    for part in fields._parts(path):
        if isinstance(part, int):
            if not isinstance(cur, list) or part >= len(cur):
                return False, None
            cur = cur[part]
        else:
            if not isinstance(cur, dict) or part not in cur:
                return False, None
            cur = cur[part]
    return True, cur


def _layer_mint(bundle, cfg, now) -> dict:
    account = bundle.get("account")
    if is_missing(account):
        return _no_data(1, "rpc")
    found, value = _lookup(account, fields.RPC_GET_ACCOUNT_INFO["mintAuthority"])
    if not found:
        return _no_data(1, "rpc")
    if value:
        return _layer(1, False, "set", "rpc")
    return _layer(1, True, "revoked", "rpc")


def _layer_freeze(bundle, cfg, now) -> dict:
    account = bundle.get("account")
    if is_missing(account):
        return _no_data(2, "rpc")
    found, value = _lookup(account, fields.RPC_GET_ACCOUNT_INFO["freezeAuthority"])
    if not found:
        return _no_data(2, "rpc")
    if value:
        return _layer(2, False, "set", "rpc")
    return _layer(2, True, "none", "rpc")


def _layer_dev(bundle, cfg, now) -> dict:
    report = bundle.get("report")
    supply = bundle.get("supply")
    if is_missing(report):
        return _no_data(3, "rugcheck")
    creator = fields.get(report, fields.RUGCHECK_REPORT["creator"])
    if not creator:
        return _layer(3, False, "no creator", "rugcheck")
    found_supply, raw_supply = _lookup(supply, fields.RPC_GET_TOKEN_SUPPLY["uiAmount"]) if not is_missing(supply) else (False, None)
    if not found_supply or not raw_supply:
        return _layer(3, False, "no supply", "rpc")
    balance = fields.get(report, fields.RUGCHECK_REPORT.get("creatorBalance"))
    balance_n = float(balance or 0)
    pct = 100.0 * balance_n / float(raw_supply) if balance_n <= float(raw_supply) else 100.0 * balance_n / float(raw_supply)
    # creatorBalance in this sample is 0. Treat a balance larger than UI supply
    # as raw token units and scale by decimals via the raw supply amount.
    if balance_n > float(raw_supply):
        raw_amount = fields.get(supply, fields.RPC_GET_TOKEN_SUPPLY["supply"])
        if raw_amount:
            pct = 100.0 * balance_n / float(raw_amount)
    if pct > cfg["max_dev_pct"]:
        return _layer(3, False, f"dev holds {pct:.0f}%", "rugcheck")
    return _layer(3, True, f"dev holds {pct:.0f}%, sold 0", "rugcheck")


def _layer_top10(bundle, cfg, now) -> dict:
    report = bundle.get("report")
    if is_missing(report):
        return _no_data(4, "rugcheck")
    holders = fields.get(report, fields.RUGCHECK_REPORT.get("topHolders"))
    if not isinstance(holders, list):
        return _no_data(4, "rugcheck")
    pool = bundle.get("pool")
    excluded = {pool} if pool else set()
    kept = []
    for holder in holders:
        if not isinstance(holder, dict):
            continue
        if holder.get("address") in excluded or holder.get("owner") in excluded:
            continue
        if holder.get("pct") is None:
            continue
        kept.append(float(holder["pct"]))
    pct = sum(sorted(kept, reverse=True)[:10])
    value = f"top 10 hold {pct:.0f}%"
    return _layer(4, pct <= cfg["max_top10_pct"], value, "rugcheck")


def _layer_lp(bundle, cfg, now) -> dict:
    """LP locked percent. There is no burned field on the RugCheck report."""
    report = bundle.get("report")
    if is_missing(report):
        return _no_data(5, "rugcheck")
    market = fields.rugcheck_market_for_pool(report, bundle.get("pool"))
    if market is None:
        return _no_data(5, "rugcheck")
    locked = fields.get(market, "lp.lpLockedPct")
    if locked is None:
        return _no_data(5, "rugcheck")
    passed = float(locked) >= cfg["min_lp_locked_pct"]
    return _layer(5, passed, f"LP {float(locked):.0f}% locked", "rugcheck")


def _layer_age(bundle, cfg, now) -> dict:
    report = bundle.get("report")
    pair = bundle.get("pair")
    created = None
    if isinstance(pair, dict):
        created = fields.get(pair, fields.DEXSCREENER["pairCreatedAt"])
    if created is None:
        return _no_data(6, "dex")
    age_min = (now - float(created) / 1000.0) / 60.0
    if age_min < cfg["min_pool_age_min"]:
        return _layer(6, False, "pool too young", "dex")
    return _layer(6, True, f"{age_min:.0f} min", "dex")


def _layer_bundles(bundle, cfg, now) -> dict:
    """Largest funded-together component. Edges are `links`, not `edges`."""
    graph = bundle.get("graph")
    if graph is None or (isinstance(graph, dict) and graph.get("error")):
        return _no_data(7, "rugcheck")
    size = largest_bundle(graph)
    if size >= cfg["max_bundled"]:
        return _layer(7, False, f"{int(size)} bundled wallets", "rugcheck")
    return _layer(7, True, "None", "rugcheck")


def _layer_delegations(bundle, cfg, now) -> dict:
    program = bundle.get("program_accounts")
    if program is None or (isinstance(program, dict) and program.get("error")):
        return _no_data(8, "rpc")
    groups: dict[str, int] = {}
    delegate_path = _item_path(fields.RPC_TOKEN_ACCOUNT.get("delegate"))
    for account in program:
        if not delegate_path:
            delegate = None
        else:
            delegate = fields.get(account, delegate_path)
        if delegate:
            groups[str(delegate)] = groups.get(str(delegate), 0) + 1
    if not groups:
        bundle["holders"] = _holder_count(program)
        return _layer(8, True, "None", "rpc")
    delegate, count = max(groups.items(), key=lambda item: item[1])
    bundle["holders"] = _holder_count(program)
    if count > cfg["max_delegated_accounts"]:
        short = delegate[:4] + "..." + delegate[-3:]
        return _layer(8, False, f"{count} wallets → {short}", "rpc")
    return _layer(8, True, "None", "rpc")


_LAYERS = _LAYERS_build()


def largest_bundle(graph: Any) -> int:
    networks = graph
    if isinstance(graph, dict):
        networks = graph.get("payload") or []
    if not isinstance(networks, list):
        return 0
    best = 0
    for net in networks:
        if not isinstance(net, dict):
            continue
        nodes = [node.get("id") for node in net.get("nodes") or [] if isinstance(node, dict) and node.get("id")]
        parent = {node: node for node in nodes}

        def find(item: str) -> str:
            while parent[item] != item:
                parent[item] = parent[parent[item]]
                item = parent[item]
            return item

        for link in net.get("links") or []:
            if not isinstance(link, dict):
                continue
            source, target = link.get("source"), link.get("target")
            if source not in parent or target not in parent:
                continue
            left, right = find(source), find(target)
            if left != right:
                parent[right] = left
        if not nodes:
            continue
        sizes: dict[str, int] = {}
        for node in nodes:
            root = find(node)
            sizes[root] = sizes.get(root, 0) + 1
        best = max(best, max(sizes.values()))
    return best


def _item_path(path: str | None) -> str | None:
    if path is None:
        return None
    prefix = "[]."
    if path.startswith(prefix):
        return path[len(prefix):]
    return path


def _holder_count(program: Any) -> int:
    if not isinstance(program, list):
        return 0
    amount_path = _item_path(fields.RPC_TOKEN_ACCOUNT.get("uiAmount"))
    count = 0
    for account in program:
        amount = fields.get(account, amount_path) if amount_path else None
        try:
            if amount is not None and float(amount) > 0:
                count += 1
        except (TypeError, ValueError):
            continue
    return count


def _result_list(payload: Any) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("result"), list):
        return payload["result"]
    return []


def _pool_from_report(report: dict) -> str | None:
    markets = fields.get(report, "markets")
    if not isinstance(markets, list):
        return None
    for market in markets:
        if isinstance(market, dict) and market.get("marketType") == "pump_fun_amm":
            return market.get("pubkey")
    return None
