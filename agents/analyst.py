"""Analyst, agent 3 of 5. Jev answers. Code applies the gate.

Rules mode is a stand-in. The prompt's rules-mode formula was truncated;
only "rows read Analyst (rules)" survived. The four nouls are derived from
separate DexScreener numbers so the same gate runs as in Jev mode:

  alive_6h = min(1, volume.h1 / 10000)
  organic  = h1 buys / (h1 buys + h1 sells)   (0 if there are no trades)
  flush     = min(1, max(-priceChange.m5, 0) / 20)
  chase     = min(1, max(priceChange.h1, 0) / 100)

verdicts.alive_6h / organic / flush / chase are INTEGER 0/1 in STEP 4, so the
database stores whether that gate passed. Flush has no threshold in the
prompt; its bit is 1 when a numeric answer was used in the score. The raw
0-1 nouls stay on the in-memory verdict and in the activity subtitle.
"""

from __future__ import annotations

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


def gate(answers: dict, settings: dict | None = None) -> dict:
    cfg = settings or _settings()
    failed: list[str] = []
    values = {}
    for name in QUESTIONS:
        raw = answers.get(name)
        if raw is None:
            failed.append(f"no data: {name}")
            values[name] = None
            continue
        values[name] = float(raw)
    if values.get("alive_6h") is not None and values["alive_6h"] < cfg["alive_6h_min"]:
        failed.append(f"alive_6h {values['alive_6h']:.2f}")
    if values.get("organic") is not None and values["organic"] < cfg["organic_min"]:
        failed.append(f"organic {values['organic']:.2f}")
    if values.get("chase") is not None and values["chase"] > cfg["chase_max"]:
        failed.append(f"chase {values['chase']:.2f}")
    score = None
    if all(values.get(name) is not None for name in QUESTIONS):
        score = round(100 * (
            values["alive_6h"] + values["organic"] + values["flush"] + (1 - values["chase"])
        ) / 4)
        if score < cfg["score_min"]:
            failed.append(f"score {score}")
    elif not any(item.startswith("no data") for item in failed):
        failed.append("no data: score")
    return {
        "answers": {name: values.get(name) for name in QUESTIONS},
        "score": score if score is not None else 0,
        "passed": not failed and score is not None,
        "failed": failed,
    }


def rules_scores(pair: dict) -> dict:
    """Stand-in rules mode. See the module docstring for the formula."""
    h1_volume = _num(fields.get(pair, fields.DEXSCREENER["volume.h1"])) or 0.0
    buys = _num(fields.get(pair, fields.DEXSCREENER["txns.h1.buys"])) or 0.0
    sells = _num(fields.get(pair, fields.DEXSCREENER["txns.h1.sells"])) or 0.0
    change_m5 = _num(fields.get(pair, fields.DEXSCREENER["priceChange.m5"])) or 0.0
    change_h1 = _num(fields.get(pair, fields.DEXSCREENER["priceChange.h1"])) or 0.0
    trades = buys + sells
    organic = (buys / trades) if trades else 0.0
    return {
        "alive_6h": _clamp(h1_volume / 10000.0),
        "organic": _clamp(organic),
        "flush": _clamp(max(-change_m5, 0.0) / 20.0),
        "chase": _clamp(max(change_h1, 0.0) / 100.0),
    }


def build_state(payload: dict, limit: int = 3000) -> str:
    pair = payload.get("pair") or {}
    layers = {layer["n"]: layer for layer in payload.get("layers") or []}
    def layer_value(number: int) -> str:
        layer = layers.get(number)
        return layer["value"] if layer else "n/a"

    age = payload.get("age_min")
    age_txt = f"{age:.0f}" if isinstance(age, (int, float)) else "?"
    liq = _num(fields.get(pair, fields.DEXSCREENER["liquidity.usd"]))
    mcap = _num(fields.get(pair, fields.DEXSCREENER["marketCap"]))
    text = (
        f"Coin {payload.get('name') or '?'} on Solana, graduated {age_txt} min ago. "
        f"Liquidity ${_money(liq)}. Market cap ${_money(mcap)}. "
        f"Last 5 min: {_int(fields.get(pair, fields.DEXSCREENER['txns.m5.buys']))} buys, "
        f"{_int(fields.get(pair, fields.DEXSCREENER['txns.m5.sells']))} sells, "
        f"volume ${_money(_num(fields.get(pair, fields.DEXSCREENER['volume.m5'])))}, "
        f"price {_num(fields.get(pair, fields.DEXSCREENER['priceChange.m5'])) or 0}%. "
        f"Last 1h: {_int(fields.get(pair, fields.DEXSCREENER['txns.h1.buys']))} buys, "
        f"{_int(fields.get(pair, fields.DEXSCREENER['txns.h1.sells']))} sells, "
        f"volume ${_money(_num(fields.get(pair, fields.DEXSCREENER['volume.h1'])))}, "
        f"price {_num(fields.get(pair, fields.DEXSCREENER['priceChange.h1'])) or 0}%. "
        f"Top 10 hold {layer_value(4)}. Dev {layer_value(3)}. LP {layer_value(5)}. "
        f"Bundled {layer_value(7)}. Delegations {layer_value(8)}."
    )
    if len(text) > limit:
        return text[:limit]
    return text


class Analyst:
    def __init__(self, desk: Desk) -> None:
        self.desk = desk

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
        mode = (db.get_setting("analyst_mode", "jev") or "jev").lower()
        state = build_state(payload, int(db.get_float("state_max_chars") or 3000))
        model = None
        input_tokens = None
        if mode == "rules":
            scores = rules_scores(payload.get("pair") or {})
            label = "Analyst (rules)"
        else:
            response = await self._ask_jev(state)
            scores = _noul_map(response)
            model = response.get("model") if isinstance(response, dict) else None
            input_tokens = _input_tokens(response)
            label = "Analyst"
        decision = gate(scores)
        decision.update({
            "agent": "analyst",
            "mint": payload.get("mint"),
            "model": model,
            "inputTokens": input_tokens,
            "mode": mode,
            "label": label,
        })
        self._store(decision, label)
        if decision["passed"]:
            onward = dict(payload)
            onward.update(decision)
            await self.desk.bus.executor.put(onward)
        else:
            reason = decision["failed"][0] if decision["failed"] else "rejected"
            name = payload.get("symbol") or payload.get("name") or payload.get("mint")
            url = links.coin_url(payload.get("mint"), "solana")
            db.record_activity(
                self.desk.now(), "rejected", payload.get("mint"), "analyst",
                f"Rejected {name}", f"{label} · {reason}", "Skipped",
            )
            self.desk.notifier.push(
                f"Rejected {name}",
                f"{label} · {reason}",
                url,
                [{"label": "Open on FOMO", "url": url}],
            )
        return decision

    async def _ask_jev(self, state: str) -> dict:
        secret = load_secrets().get("TYPESAFE_API_KEY")
        if not secret:
            log.warning("jev key is not set")
            return {}
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
        try:
            payload = await self.desk.http.post_json(
                JEV_URL,
                body,
                headers={"Authorization": f"Bearer {secret}"},
            )
        except Exception:
            log.warning("jev request failed")
            return {}
        return payload if isinstance(payload, dict) else {}

    def _store(self, decision: dict, label: str) -> None:
        answers = decision["answers"]
        bits = {
            "alive_6h": _bit(answers.get("alive_6h"), db.get_float("alive_6h_min"), minimum=True),
            "organic": _bit(answers.get("organic"), db.get_float("organic_min"), minimum=True),
            "chase": _bit(answers.get("chase"), db.get_float("chase_max"), minimum=False),
            "flush": 1 if answers.get("flush") is not None else 0,
        }
        db.execute(
            """
            INSERT INTO verdicts (
                mint, ts, alive_6h, organic, flush, chase, score, "pass", reason, input_tokens
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision.get("mint"),
                self.desk.now(),
                bits["alive_6h"],
                bits["organic"],
                bits["flush"],
                bits["chase"],
                decision.get("score"),
                1 if decision["passed"] else 0,
                decision["failed"][0] if decision["failed"] else None,
                decision.get("inputTokens"),
            ),
        )


def _settings() -> dict[str, float]:
    return {
        "alive_6h_min": db.get_float("alive_6h_min"),
        "organic_min": db.get_float("organic_min"),
        "chase_max": db.get_float("chase_max"),
        "score_min": db.get_float("score_min"),
    }


def _bit(value: float | None, threshold: float, minimum: bool) -> int:
    if value is None:
        return 0
    if minimum:
        return 1 if value >= threshold else 0
    return 1 if value <= threshold else 0


def _noul_map(response: Any) -> dict:
    if not isinstance(response, dict):
        return {}
    answers = response.get("answers") or {}
    out = {}
    for name in QUESTIONS:
        node = answers.get(name) or {}
        if isinstance(node, dict) and node.get("noul") is not None:
            out[name] = float(node["noul"])
        elif isinstance(node, (int, float)):
            out[name] = float(node)
    return out


def _input_tokens(response: Any) -> int | None:
    if not isinstance(response, dict):
        return None
    usage = response.get("usage") or {}
    for key in ("input_tokens", "inputTokens", "prompt_tokens"):
        if key in usage and usage[key] is not None:
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
    if value is None:
        return "?"
    return f"{value:.0f}"


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
