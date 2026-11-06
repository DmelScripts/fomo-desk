"""Auditor, agent 2 of 5. Eight layers, stop at the first red.

Layer 5 uses markets[].lp.lpLockedPct. The captured RugCheck report has no
burned field. Layer 7's captured graph has `links`, not `edges`; links are
the edges. A graph object with neither key is "no data: bundles". An empty
list has no groups and is "None".
"""

from __future__ import annotations

import logging
from typing import Any

import db
import fields
import links
from agents.scanner import RETRY_GAP_S, parse_largest_accounts
from bus import Desk, call_with_retries

log = logging.getLogger("auditor")

RUGCHECK_REPORT = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
RUGCHECK_GRAPH = "https://api.rugcheck.xyz/v1/tokens/{mint}/insiders/graph"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
# Layer 5 bracket that STEP 10 does not give its own key.
LIQUIDITY_DROP_PCT = 50

LAYER_NAMES = {
    1: "Mint authority",
    2: "Freeze authority",
    3: "Dev wallet",
    4: "Top 10",
    5: "Liquidity",
    6: "Pool age",
    7: "Bundled wallets",
    8: "Delegations",
}


async def sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)


def judge(bundle: dict, settings: dict | None = None, now: float | None = None) -> dict:
    cfg = settings or _settings()
    now = _now() if now is None else now
    layers: list[dict] = []
    stopped = False
    for number in range(1, 9):
        if stopped:
            layers.append(_layer(number, False, "not checked", None))
            continue
        layer = _CHECKS[number](bundle, cfg, now)
        layers.append(layer)
        _log_layer(bundle.get("mint"), layer)
        if not layer["pass"]:
            stopped = True
    checked = [layer for layer in layers if layer["value"] != "not checked"]
    reds = [layer for layer in checked if not layer["pass"]]
    passed = len(checked) == 8 and not reds
    return {
        "agent": "auditor",
        "mint": bundle.get("mint"),
        "passed": passed,
        "layersChecked": len(checked),
        "layers": layers,
        "holders": bundle.get("holders"),
        "bundled": bundle.get("bundled"),
        "reason": reds[0]["value"] if reds else None,
    }


class Auditor:
    def __init__(self, desk: Desk) -> None:
        self.desk = desk
        self.rejected: set[str] = set()
        self.layer8_kills = 0

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
        mint = payload["mint"]
        if mint in self.rejected:
            return {"mint": mint, "passed": False, "reason": "already rejected", "layers": []}
        if not db.flag_on("enabled"):
            onward = dict(payload)
            onward["passed"] = True
            onward["layersChecked"] = 0
            await self.desk.bus.analyst.put(onward)
            return onward
        result = await self.audit(mint, payload=payload, now=self.desk.now())
        for key in ("name", "symbol", "price", "liquidity_usd", "pair", "migrated_at"):
            result.setdefault(key, payload.get(key))
        result["liquidityUsd"] = payload.get("liquidityUsd", payload.get("liquidity_usd"))
        self._store(result)
        if result["passed"]:
            await self.desk.bus.analyst.put(result)
        else:
            self.rejected.add(mint)
            label = payload.get("symbol") or payload.get("name") or mint
            db.record_activity(
                self.desk.now(), "rejected", mint, "auditor",
                f"Rejected {label}", f"Auditor · {result['reason']}", "Skipped",
            )
            if db.flag_on("push_rejects"):
                url = links.coin_url(mint, "solana")
                self.desk.notifier.push(
                    f"Rejected {label}", f"Auditor · {result['reason']}", url,
                    [{"label": "Open on FOMO", "url": url}],
                )
            if result["reason"] == "no data: delegations":
                self.layer8_kills += 1
                self.desk.notifier.push(
                    "Auditor",
                    "layer 8 down, every coin is red until rpc_url changes",
                    links.coin_url(mint, "solana"),
                    [],
                )
        return result

    async def audit(self, mint: str, payload: dict | None = None, now: float | None = None) -> dict:
        payload = payload or {}
        bundle = await self.fetch_bundle(mint, payload.get("pool"))
        bundle["scanner_liquidity"] = payload.get("liquidityUsd", payload.get("liquidity_usd"))
        bundle["migrated_at"] = payload.get("migrated_at")
        bundle["creator_balance_at_migration"] = payload.get("creator_balance_at_migration")
        if payload.get("creator"):
            bundle["creator"] = payload.get("creator")
        if payload.get("pair") and bundle.get("liquidity_now") is None:
            bundle["liquidity_now"] = _num(fields.get(payload["pair"], fields.DEXSCREENER["liquidity.usd"]))
        result = judge(bundle, now=now if now is not None else self.desk.now())
        result["mint"] = mint
        return result

    async def fetch_bundle(self, mint: str, pool: str | None) -> dict:
        tries = int(db.get_float("retries") or 3)
        account = await call_with_retries(lambda: self._rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}]), tries, RETRY_GAP_S, sleep)
        supply = await call_with_retries(lambda: self._rpc("getTokenSupply", [mint]), tries, RETRY_GAP_S, sleep)
        report = await call_with_retries(lambda: self.desk.http.get_json(RUGCHECK_REPORT.format(mint=mint)), tries, RETRY_GAP_S, sleep)
        graph = await call_with_retries(lambda: self.desk.http.get_json(RUGCHECK_GRAPH.format(mint=mint)), tries, RETRY_GAP_S, sleep)
        program = await call_with_retries(lambda: self._program_accounts(mint), tries, RETRY_GAP_S, sleep)
        largest = await call_with_retries(lambda: self._rpc("getTokenLargestAccounts", [mint]), tries, RETRY_GAP_S, sleep)
        creator = fields.get(report, fields.RUGCHECK_REPORT["creator"]) if isinstance(report, dict) else None
        dev_balance = None
        if creator:
            owned = await call_with_retries(
                lambda: self._rpc("getTokenAccountsByOwner", [creator, {"mint": mint}, {"encoding": "jsonParsed"}]),
                tries, RETRY_GAP_S, sleep,
            )
            dev_balance = _owner_balance(owned) if not _missing(owned) else None
        if pool is None and isinstance(report, dict):
            pool = _pool_from_report(report)
        supply_ui = None if _missing(supply) else _num(fields.get(supply, fields.RPC_GET_TOKEN_SUPPLY["uiAmount"]))
        return {
            "mint": mint,
            "pool": pool,
            "account": account,
            "supply": supply,
            "report": report,
            "graph": graph,
            "program_accounts": program,
            "largest": largest,
            "supply_ui": supply_ui,
            "creator": creator,
            "dev_balance_ui": dev_balance,
            "holders": _holder_count(program) if isinstance(program, list) else None,
        }

    def _store(self, result: dict) -> None:
        now = self.desk.now()
        if result.get("holders") is not None:
            db.execute("UPDATE coins SET holders = ? WHERE mint = ?", (result["holders"], result["mint"]))
        for layer in result["layers"]:
            passed = None if layer["value"] == "not checked" else (1 if layer["pass"] else 0)
            db.execute(
                """
                INSERT INTO audits (mint, ts, layer, name, value_text, "pass", source)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (result["mint"], now, str(layer["n"]), layer["name"], layer["value"], passed, layer.get("source")),
            )
            self.desk.bus.publish("audit_layer", layer)

    async def _program_accounts(self, mint: str) -> list | None:
        accounts: list = []
        for program in (TOKEN_PROGRAM, TOKEN_2022):
            payload = await self._rpc(
                "getProgramAccounts",
                [program, {"encoding": "jsonParsed", "filters": [{"memcmp": {"offset": 0, "bytes": mint}}]}],
            )
            if _missing(payload):
                return None
            accounts.extend(_result_list(payload))
        return accounts

    async def _rpc(self, method: str, params: list) -> Any:
        return await self.desk.http.post_json(
            db.get_setting("rpc_url"),
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )


def _settings() -> dict[str, float]:
    return {
        "max_dev_pct": db.get_float("max_dev_pct"),
        "max_top10_pct": db.get_float("max_top10_pct"),
        "min_lp_locked_pct": db.get_float("min_lp_locked_pct"),
        "min_pool_age_min": db.get_float("min_pool_age_min"),
        "max_bundled": db.get_float("max_bundled"),
        "max_delegated_accounts": db.get_float("max_delegated_accounts"),
    }


def _now() -> float:
    import time
    return time.time()


def _layer(number: int, passed: bool, value: str, source: str | None) -> dict:
    return {"n": number, "name": LAYER_NAMES[number], "pass": passed, "value": value, "source": source}


def _no(number: int, name: str, source: str | None) -> dict:
    return _layer(number, False, f"no data: {name}", source)


def _missing(payload: Any) -> bool:
    if payload is None:
        return True
    return isinstance(payload, dict) and bool(payload.get("error"))


def _found(obj: Any, path: str | None) -> tuple[bool, Any]:
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
    if _missing(account):
        return _no(1, "mint authority", "rpc")
    found, value = _found(account, fields.RPC_GET_ACCOUNT_INFO["mintAuthority"])
    if not found:
        return _no(1, "mint authority", "rpc")
    if value:
        return _layer(1, False, "mint not revoked", "rpc")
    return _layer(1, True, "Revoked", "rpc")


def _layer_freeze(bundle, cfg, now) -> dict:
    account = bundle.get("account")
    if _missing(account):
        return _no(2, "freeze authority", "rpc")
    found, value = _found(account, fields.RPC_GET_ACCOUNT_INFO["freezeAuthority"])
    if not found:
        return _no(2, "freeze authority", "rpc")
    if value:
        return _layer(2, False, "freeze not revoked", "rpc")
    return _layer(2, True, "Revoked", "rpc")


def _layer_dev(bundle, cfg, now) -> dict:
    creator = bundle.get("creator")
    if not creator and isinstance(bundle.get("report"), dict):
        creator = fields.get(bundle["report"], fields.RUGCHECK_REPORT["creator"])
    if not creator:
        return _layer(3, False, "no data: dev", "rpc")
    supply = bundle.get("supply_ui")
    if supply is None and not _missing(bundle.get("supply")):
        supply = _num(fields.get(bundle.get("supply"), fields.RPC_GET_TOKEN_SUPPLY["uiAmount"]))
    if not supply:
        return _layer(3, False, "no supply", "rpc")
    if "dev_balance_ui" not in bundle or bundle.get("dev_balance_ui") is None and bundle.get("dev_balance_known") is not True:
        if bundle.get("dev_balance_ui") is None and not bundle.get("dev_balance_known"):
            return _no(3, "dev", "rpc")
    balance = float(bundle.get("dev_balance_ui") or 0)
    pct = 100.0 * balance / float(supply)
    if pct >= cfg["max_dev_pct"]:
        return _layer(3, False, f"dev holds {pct:.0f}%", "rpc")
    baseline = bundle.get("creator_balance_at_migration")
    if baseline is not None and balance < float(baseline) - 1e-9:
        return _layer(3, False, "dev sold", "rpc")
    return _layer(3, True, f"{pct:.1f}% · sold 0", "rpc")


def _layer_top10(bundle, cfg, now) -> dict:
    supply = bundle.get("supply_ui")
    if supply is None and not _missing(bundle.get("supply")):
        supply = _num(fields.get(bundle.get("supply"), fields.RPC_GET_TOKEN_SUPPLY["uiAmount"]))
    exclude = {item for item in (bundle.get("pool"), bundle.get("curve")) if item}
    pct = parse_largest_accounts(bundle.get("largest"), supply, exclude)
    if pct is None and isinstance(bundle.get("largest_accounts"), list):
        pct = parse_largest_accounts({"result": {"value": bundle["largest_accounts"]}}, supply, exclude)
    if pct is None:
        return _no(4, "top 10", "rpc")
    # The layer text is "sum < 25%", so 25 exactly is red.
    if pct >= cfg["max_top10_pct"]:
        return _layer(4, False, f"top 10 hold {pct:.0f}%", "rpc")
    return _layer(4, True, f"{pct:.1f}%", "rpc")


def _layer_lp(bundle, cfg, now) -> dict:
    """Locked percent stands in for the missing burned field on this report."""
    report = bundle.get("report")
    if _missing(report):
        return _layer(5, False, "no data: liquidity", "rugcheck")
    market = fields.rugcheck_market_for_pool(report, bundle.get("pool"))
    if market is None:
        return _layer(5, False, "no data: liquidity", "rugcheck")
    locked = fields.get(market, "lp.lpLockedPct")
    burned = fields.get(market, "lp.lpBurnedPct")
    if locked is None and burned is None:
        return _layer(5, False, "no data: liquidity", "rugcheck")
    locked_n = float(locked) if locked is not None else 0.0
    burned_n = float(burned) if burned is not None else 0.0
    if max(locked_n, burned_n) < cfg["min_lp_locked_pct"]:
        return _layer(5, False, "liquidity unlocked", "rugcheck")
    scanner_liq = bundle.get("scanner_liquidity")
    current = bundle.get("liquidity_now")
    if scanner_liq and current is not None:
        drop = (float(scanner_liq) - float(current)) / float(scanner_liq) * 100.0
        if drop > LIQUIDITY_DROP_PCT:
            return _layer(5, False, "liquidity pulled", "rugcheck")
    if burned_n >= cfg["min_lp_locked_pct"]:
        return _layer(5, True, f"{burned_n:.0f}% burned", "rugcheck")
    return _layer(5, True, f"{locked_n:.0f}% locked", "rugcheck")


def _layer_age(bundle, cfg, now) -> dict:
    migrated = bundle.get("migrated_at")
    if migrated is None:
        return _no(6, "pool age", "rpc")
    age_min = (now - float(migrated)) / 60.0
    if age_min < cfg["min_pool_age_min"]:
        return _layer(6, False, "pool too young", "rpc")
    return _layer(6, True, f"{age_min:.0f}m", "rpc")


def _layer_bundles(bundle, cfg, now) -> dict:
    graph = bundle.get("graph")
    if graph is None or (isinstance(graph, dict) and graph.get("error")):
        return _layer(7, False, "no data: bundles", "rugcheck")
    try:
        size = largest_holder_group(graph)
    except NoEdges:
        return _layer(7, False, "no data: bundles", "rugcheck")
    bundle["bundled"] = size
    if size >= cfg["max_bundled"]:
        return _layer(7, False, f"{int(size)} bundled wallets", "rugcheck")
    return _layer(7, True, "None", "rugcheck")


def _layer_delegations(bundle, cfg, now) -> dict:
    program = bundle.get("program_accounts")
    if program is None or (isinstance(program, dict) and program.get("error")):
        return _layer(8, False, "no data: delegations", "rpc")
    groups: dict[str, int] = {}
    for account in program:
        info = _token_info(account)
        if not info:
            continue
        delegate = info.get("delegate")
        amount = (info.get("tokenAmount") or {}).get("uiAmount")
        try:
            holding = amount is not None and float(amount) > 0
        except (TypeError, ValueError):
            holding = False
        if delegate and holding:
            groups[str(delegate)] = groups.get(str(delegate), 0) + 1
    bundle["holders"] = _holder_count(program)
    if not groups:
        return _layer(8, True, "None", "rpc")
    delegate, count = max(groups.items(), key=lambda item: item[1])
    if count > cfg["max_delegated_accounts"]:
        short = delegate[:4] + "..." + delegate[-3:]
        return _layer(8, False, f"{count} wallets → {short}", "rpc")
    return _layer(8, True, "None", "rpc")


_CHECKS = {
    1: _layer_mint,
    2: _layer_freeze,
    3: _layer_dev,
    4: _layer_top10,
    5: _layer_lp,
    6: _layer_age,
    7: _layer_bundles,
    8: _layer_delegations,
}


class NoEdges(Exception):
    pass


def largest_holder_group(graph: Any) -> int:
    """Holders joined by edges. `links` is the captured name for edges."""
    if graph == []:
        return 0
    networks = graph
    if isinstance(graph, dict):
        if "nodes" in graph or "edges" in graph or "links" in graph:
            networks = [graph]
        else:
            networks = graph.get("payload")
            if networks is None:
                raise NoEdges()
    if not isinstance(networks, list):
        raise NoEdges()
    if not networks:
        return 0
    saw_edge_key = False
    best = 0
    for net in networks:
        if not isinstance(net, dict):
            continue
        edges = net.get("edges")
        if edges is None and "links" in net:
            edges = net.get("links")
        if "edges" in net or "links" in net:
            saw_edge_key = True
        elif net.get("nodes"):
            continue
        holders = set()
        for node in net.get("nodes") or []:
            if not isinstance(node, dict) or not node.get("id"):
                continue
            holdings = node.get("holdings")
            try:
                if holdings is not None and float(holdings) > 0:
                    holders.add(node["id"])
            except (TypeError, ValueError):
                continue
        if not holders:
            continue
        parent = {node: node for node in holders}

        def find(item: str) -> str:
            while parent[item] != item:
                parent[item] = parent[parent[item]]
                item = parent[item]
            return item

        for link in edges or []:
            if not isinstance(link, dict):
                continue
            source, target = link.get("source"), link.get("target")
            if source in parent and target in parent and find(source) != find(target):
                parent[find(target)] = find(source)
        sizes: dict[str, int] = {}
        for node in holders:
            root = find(node)
            sizes[root] = sizes.get(root, 0) + 1
        best = max(best, max(sizes.values()))
    if not saw_edge_key and any(isinstance(net, dict) and net.get("nodes") for net in networks):
        raise NoEdges()
    return best


def _token_info(account: dict) -> dict | None:
    try:
        return account["account"]["data"]["parsed"]["info"]
    except (KeyError, TypeError):
        return None


def _holder_count(program: Any) -> int:
    if not isinstance(program, list):
        return 0
    count = 0
    for account in program:
        info = _token_info(account)
        if not info:
            continue
        amount = (info.get("tokenAmount") or {}).get("uiAmount")
        try:
            if amount is not None and float(amount) > 0:
                count += 1
        except (TypeError, ValueError):
            continue
    return count


def _owner_balance(payload: Any) -> float:
    total = 0.0
    for account in _result_list(payload):
        info = _token_info(account)
        if not info:
            continue
        amount = (info.get("tokenAmount") or {}).get("uiAmount")
        if amount is not None:
            total += float(amount)
    return total


def _result_list(payload: Any) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        result = payload.get("result")
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and isinstance(result.get("value"), list):
            return result["value"]
    return []


def _pool_from_report(report: dict) -> str | None:
    markets = fields.get(report, "markets")
    if not isinstance(markets, list):
        return None
    for market in markets:
        if isinstance(market, dict) and market.get("marketType") == "pump_fun_amm":
            return market.get("pubkey")
    return None


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _log_layer(mint: str | None, layer: dict) -> None:
    log.info("mint=%s layer=%s value=%s pass=%s", mint, layer["n"], layer["value"], layer["pass"])
