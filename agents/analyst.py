"""Analyst, agent 3 of 5. Jev answers four noul questions. Code applies the gate.

RULES MODE uses the DexScreener formulas in the prompt. A Jev failure never
falls back to rules; it rejects "no answer".
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import db
import fields
import links
from bus import Desk
from config import load_secrets

log = logging.getLogger("analyst")

JEV_URL = "https://api.typesafe.ai/v1/systemone"
QUESTIONS = ("alive_6h", "organic", "flush", "chase")
# Bracket in the agent text. STEP 10 does not give it a key.
STATE_MAX = 3000
JEV_RETRY_S = 2
JEV_QUEUE_S = 60


def gate(answers: dict, settings: dict | None = None) -> dict:
    cfg = settings or _settings()
    failed: list[str] = []
    values: dict[str, float | None] = {}
    for name in QUESTIONS:
        raw = answers.get(name)
        if raw is None or not _in_unit(raw):
            failed.append("bad answer" if raw is not None else "no answer")
            values[name] = None
            continue
        values[name] = float(raw)
    if values.get("alive_6h") is not None and values["alive_6h"] < cfg["min_alive_6h"]:
        failed.append(f"alive_6h {values['alive_6h']:.2f}")
    if values.get("organic") is not None and values["organic"] < cfg["min_organic"]:
        failed.append(f"organic {values['organic']:.2f}")
    if values.get("chase") is not None and values["chase"] > cfg["max_chase"]:
        failed.append(f"chase {values['chase']:.2f}")
    score = None
    if all(values.get(name) is not None for name in QUESTIONS):
        score = round(100 * (
            values["alive_6h"] + values["organic"] + values["flush"] + (1 - values["chase"])
        ) / 4)
        if score < cfg["min_score"]:
            failed.append(f"score {score}")
    passed = not failed and score is not None
    # A bad/missing answer is the reason; don't also list the threshold.
    if any(item in ("bad answer", "no answer") for item in failed):
        failed = [item for item in failed if item in ("bad answer", "no answer")]
    return {
        "answers": {name: values.get(name) for name in QUESTIONS},
        "score": 0 if score is None else score,
        "passed": passed,
        "failed": failed,
    }


def rules_scores(pair: dict, bundled: int = 0, settings: dict | None = None) -> dict:
    """STEP 10 rules formulas. bundled_penalty applies from 3 wallets."""
    cfg = settings or _rule_settings()
    volume = _num(fields.get(pair, fields.DEXSCREENER["volume.h1"])) or 0.0
    buys = _num(fields.get(pair, fields.DEXSCREENER["txns.h1.buys"])) or 0.0
    buys_m5 = _num(fields.get(pair, fields.DEXSCREENER["txns.m5.buys"])) or 0.0
    sells_m5 = _num(fields.get(pair, fields.DEXSCREENER["txns.m5.sells"])) or 0.0
    change_m5 = _num(fields.get(pair, fields.DEXSCREENER["priceChange.m5"])) or 0.0
    change_h1 = _num(fields.get(pair, fields.DEXSCREENER["priceChange.h1"])) or 0.0
    organic = _clamp(buys / cfg["organic_buys_h1"] if cfg["organic_buys_h1"] else 0.0)
    if bundled >= 3:
        organic *= cfg["bundled_penalty"]
    if change_m5 <= -cfg["flush_drop_m5_pct"] and buys_m5 >= sells_m5:
        flush = 1.0
    elif change_m5 < 0:
        flush = 0.5
    else:
        flush = 0.0
    return {
        "alive_6h": _clamp(volume / cfg["alive_volume_h1"] if cfg["alive_volume_h1"] else 0.0),
        "organic": _clamp(organic),
        "flush": flush,
        "chase": _clamp(change_h1 / cfg["chase_h1_pct"] if cfg["chase_h1_pct"] else 0.0),
    }


def build_state(payload: dict, limit: int = STATE_MAX) -> str:
    pair = payload.get("pair") or {}
    layers = {layer["n"]: layer.get("value") for layer in payload.get("layers") or []}
    age = payload.get("age_min")
    age_txt = f"{age:.0f}" if isinstance(age, (int, float)) else "?"
    text = (
        f"Coin {payload.get('name') or '?'} on Solana, graduated {age_txt} min ago. "
        f"Liquidity ${_money(_num(fields.get(pair, fields.DEXSCREENER['liquidity.usd'])))}. "
        f"Market cap ${_money(_num(fields.get(pair, fields.DEXSCREENER['marketCap'])))}. "
        f"Last 5 min: {_int(fields.get(pair, fields.DEXSCREENER['txns.m5.buys']))} buys, "
        f"{_int(fields.get(pair, fields.DEXSCREENER['txns.m5.sells']))} sells, "
        f"volume ${_money(_num(fields.get(pair, fields.DEXSCREENER['volume.m5'])))}, "
        f"price {_num(fields.get(pair, fields.DEXSCREENER['priceChange.m5'])) or 0}%. "
        f"Last 1h: {_int(fields.get(pair, fields.DEXSCREENER['txns.h1.buys']))} buys, "
        f"{_int(fields.get(pair, fields.DEXSCREENER['txns.h1.sells']))} sells, "
        f"volume ${_money(_num(fields.get(pair, fields.DEXSCREENER['volume.h1'])))}, "
        f"price {_num(fields.get(pair, fields.DEXSCREENER['priceChange.h1'])) or 0}%. "
        f"Top 10 hold {layers.get(4, 'n/a')}. Dev holds {layers.get(3, 'n/a')}. "
        f"LP {layers.get(5, 'n/a')}. Bundled {layers.get(7, 'n/a')}. "
        f"Delegations {layers.get(8, 'n/a')}."
    )
    return text[:limit]


class Analyst:
    def __init__(self, desk: Desk) -> None:
        self.desk = desk
        self.asked: set[str] = set()

    async def run(self) -> None:
        import asyncio
        while True:
            db.beat("analyst", self.desk.now())
            try:
                payload = await asyncio.wait_for(self.desk.bus.analyst.get(), timeout=1)
            except asyncio.TimeoutError:
                continue
            await self.judge(payload)

    async def judge(self, payload: dict) -> dict:
        db.beat("analyst", self.desk.now())
        mint = payload.get("mint")
        if mint in self.asked:
            return {"mint": mint, "passed": False, "failed": ["already asked"], "skipped": True}
        mode = (db.get_setting("analyst_mode", "jev") or "jev").lower()
        pair = payload.get("pair") or {}
        state = build_state(payload)
        model = None
        input_tokens = 0
        if mode == "rules":
            scores = rules_scores(pair, int(payload.get("bundled") or 0))
            label = "Analyst (rules)"
            model = "rules"
        else:
            response = await self._ask_jev(state)
            if response is None:
                scores = {}
                label = "Analyst"
            else:
                scores = _noul_map(response)
                model = response.get("model")
                input_tokens = _input_tokens(response) or 0
                label = "Analyst"
        if mint:
            self.asked.add(mint)
        decision = gate(scores) if scores or mode == "rules" else {
            "answers": {name: None for name in QUESTIONS},
            "score": 0,
            "passed": False,
            "failed": ["no answer"],
        }
        if mode != "rules" and not scores:
            decision = {
                "answers": {name: None for name in QUESTIONS},
                "score": 0,
                "passed": False,
                "failed": ["no answer"],
            }
        decision.update({
            "agent": "analyst",
            "mint": mint,
            "model": model,
            "inputTokens": input_tokens,
            "label": label,
        })
        self._store(decision)
        log.info(
            "mint=%s state=%s answers=%s score=%s verdict=%s tokens=%s",
            mint, hashlib.sha256(state.encode()).hexdigest()[:12],
            decision["answers"], decision["score"], decision["passed"], input_tokens,
        )
        if decision["passed"]:
            onward = dict(payload)
            onward.update(decision)
            onward["analyst_price"] = _num(fields.get(pair, fields.DEXSCREENER["priceUsd"]))
            await self.desk.bus.executor.put(onward)
        else:
            reason = decision["failed"][0] if decision["failed"] else "no answer"
            name = payload.get("symbol") or payload.get("name") or mint
            subtitle = f"{label} · {reason}"
            db.record_activity(
                self.desk.now(), "rejected", mint, "analyst",
                f"Rejected {name}", subtitle, "Skipped",
            )
            if db.flag_on("push_rejects"):
                url = links.coin_url(mint, "solana")
                self.desk.notifier.push(f"Rejected {name}", subtitle, url, [{"label": "Open on FOMO", "url": url}])
        return decision

    async def _ask_jev(self, state: str) -> dict | None:
        secret = load_secrets().get("TYPESAFE_API_KEY")
        if not secret:
            log.warning("jev key is not set")
            return None
        body = {
            "model": "jev-latest",
            "state": state,
            "questions": {
                "alive_6h": {"type": "noul", "instructions": "This coin is still actively traded 6 hours from now"},
                "organic": {"type": "noul", "instructions": "Most buyers in the last 30 minutes are separate real wallets"},
                "flush": {"type": "noul", "instructions": "Price just flushed down and buyers are absorbing it"},
                "chase": {"type": "noul", "instructions": "This entry is chasing a green candle that already ran"},
            },
        }
        timeout = db.get_float("timeout_s") or 10

        async def post():
            return await self.desk.http.post_json(
                JEV_URL, body,
                headers={"Authorization": f"Bearer {secret}"},
                timeout=timeout,
            )

        try:
            last = await post()
        except Exception as exc:
            if _is_429(None, exc):
                last = await self._hold_429(post)
            else:
                last = None
        else:
            if _is_429(last, None):
                last = await self._hold_429(post)
        if isinstance(last, dict) and last.get("answers"):
            return last
        # One retry after 2s for a non-429 HTTP error or a body with no answers.
        # A 429 that already used its 60s hold is not retried again here.
        if last is not None and _is_429(last, None):
            return None
        await self._pause(JEV_RETRY_S)
        try:
            last = await post()
        except Exception as exc:
            if _is_429(None, exc):
                last = await self._hold_429(post)
            else:
                last = None
        else:
            if _is_429(last, None):
                last = await self._hold_429(post)
        if isinstance(last, dict) and last.get("answers"):
            return last
        log.warning("jev request failed")
        return None

    async def _hold_429(self, post) -> dict | None:
        """Keep the coin queued until it gets through, or 60s passes."""
        started = self.desk.now()
        while True:
            if self.desk.now() - started >= JEV_QUEUE_S:
                log.warning("jev 429 held %ss", JEV_QUEUE_S)
                return {"error": {"code": 429}}
            remaining = JEV_QUEUE_S - (self.desk.now() - started)
            await self._pause(min(JEV_RETRY_S, remaining))
            try:
                result = await post()
            except Exception as exc:
                if _is_429(None, exc):
                    continue
                return None
            if _is_429(result, None):
                continue
            return result if isinstance(result, dict) else None

    async def _pause(self, seconds: float) -> None:
        await self.desk.sleep(seconds)

    def _store(self, decision: dict) -> None:
        answers = decision["answers"]
        db.execute(
            """
            INSERT INTO verdicts (
                mint, ts, alive_6h, organic, flush, chase, score, "pass", reason, input_tokens
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision.get("mint"),
                self.desk.now(),
                _bit(answers.get("alive_6h"), db.get_float("min_alive_6h"), True),
                _bit(answers.get("organic"), db.get_float("min_organic"), True),
                1 if answers.get("flush") is not None else 0,
                _bit(answers.get("chase"), db.get_float("max_chase"), False),
                decision.get("score"),
                1 if decision["passed"] else 0,
                decision["failed"][0] if decision["failed"] else None,
                decision.get("inputTokens"),
            ),
        )


def _is_429(result, exc: BaseException | None) -> bool:
    if exc is not None and "429" in str(exc):
        return True
    if not isinstance(result, dict):
        return False
    error = result.get("error")
    if isinstance(error, dict) and error.get("code") == 429:
        return True
    return result.get("status") == 429


def _settings() -> dict[str, float]:
    return {
        "min_alive_6h": db.get_float("min_alive_6h"),
        "min_organic": db.get_float("min_organic"),
        "max_chase": db.get_float("max_chase"),
        "min_score": db.get_float("min_score"),
    }


def _rule_settings() -> dict[str, float]:
    return {
        "alive_volume_h1": db.get_float("alive_volume_h1"),
        "organic_buys_h1": db.get_float("organic_buys_h1"),
        "bundled_penalty": db.get_float("bundled_penalty"),
        "flush_drop_m5_pct": db.get_float("flush_drop_m5_pct"),
        "chase_h1_pct": db.get_float("chase_h1_pct"),
    }


def _bit(value: float | None, threshold: float, minimum: bool) -> int:
    if value is None:
        return 0
    if minimum:
        return 1 if value >= threshold else 0
    return 1 if value <= threshold else 0


def _in_unit(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return 0.0 <= number <= 1.0


def _noul_map(response: Any) -> dict:
    if not isinstance(response, dict):
        return {}
    answers = response.get("answers") or {}
    out = {}
    for name in QUESTIONS:
        node = answers.get(name)
        if isinstance(node, dict):
            raw = node.get("noul")
        else:
            raw = node
        if raw is None:
            continue
        if not _in_unit(raw):
            return {}
        out[name] = float(raw)
    return out


def _input_tokens(response: Any) -> int | None:
    if not isinstance(response, dict):
        return None
    usage = response.get("usage") or {}
    for key in ("input_tokens", "inputTokens", "prompt_tokens"):
        if usage.get(key) is not None:
            return int(usage[key])
    return None


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int:
    number = _num(value)
    return int(number) if number is not None else 0


def _money(value: float | None) -> str:
    return "?" if value is None else f"{value:.0f}"


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
