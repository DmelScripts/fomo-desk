"""Push sink. Telegram was skipped. Real web push is STEP 9.

notifier.push(title, body, url, buttons) records the push. It does not build
FOMO URLs; callers pass the string links.py already returned.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import db

log = logging.getLogger("notifier")


class Notifier:
    def __init__(self, mode: str | None = None) -> None:
        self.pushes: list[dict] = []
        self.mode = mode

    def push(self, title, body, url, buttons) -> None:
        mode = self.mode or db.get_setting("mode", "PAPER") or "PAPER"
        shown = str(title)
        if str(mode).upper() == "PAPER" and not shown.startswith("PAPER · "):
            shown = "PAPER · " + shown
        record = {
            "title": shown,
            "body": body,
            "url": url,
            "buttons": list(buttons or []),
            "ts": time.time(),
        }
        self.pushes.append(record)
        self._write_log(record)
        kind = _kind_for(shown, body, record["buttons"])
        if kind is None:
            # activity.kind only allows bought|sold|stopped|rejected.
            # "no price" and "still open" stay in the pushes sink and the log.
            return
        mint = _mint_from_url(url)
        db.record_activity(
            record["ts"],
            kind,
            mint,
            _agent_for(shown, body),
            shown,
            body,
            url,
        )

    def _write_log(self, record: dict) -> None:
        try:
            path = Path("logs") / "pushes.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, default=str) + "\n")
        except OSError:
            log.warning("push log was not writable")


def _mint_from_url(url) -> str | None:
    if not url:
        return None
    try:
        values = parse_qs(urlparse(str(url)).query).get("address") or []
    except ValueError:
        return None
    return values[0] if values else None


def _kind_for(title: str, body, buttons) -> str | None:
    text = f"{title} {body or ''}"
    if "Rejected" in text:
        return "rejected"
    if "hit the" in text and "stop" in text:
        return "stopped"
    if "dead money" in text or text.startswith("PAPER · Sell") or "Sold " in text or text.startswith("Sell "):
        return "sold"
    labels = " ".join(str(button.get("label", "")) for button in buttons if isinstance(button, dict))
    if "Buy on FOMO" in labels:
        return "bought"
    return None


def _agent_for(title: str, body) -> str:
    text = f"{title} {body or ''}"
    for name in ("Scanner", "Auditor", "Analyst", "Executor", "Risk"):
        if name in text:
            return name.lower()
    return "desk"
