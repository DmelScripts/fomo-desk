"""Push sink. Callers pass the links.py URL. This module does not build one.

When web push is the channel, the same URL is handed to webpush.deliver.
Telegram delivery is not built.
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

    def push(self, title, body, url, buttons, *, activity: bool = True) -> None:
        mode = self.mode or db.get_setting("mode", "PAPER") or "PAPER"
        shown = str(title)
        if str(mode).upper() == "PAPER" and not shown.startswith("PAPER · "):
            shown = "PAPER · " + shown
        stored_buttons = [dict(button) for button in (buttons or [])]
        # A channel with no button keeps the exact links.py URL as its own line.
        # Buttons keep the same string; this does not rebuild or wrap it.
        body_out = body
        has_button_url = any(button.get("url") for button in stored_buttons)
        if url and not has_button_url:
            body_out = f"{body}\n{url}" if body else str(url)
        record = {
            "title": shown,
            "body": body_out,
            "url": url,
            "buttons": stored_buttons,
            "ts": time.time(),
        }
        self.pushes.append(record)
        self._write_log(record)
        _fanout(shown, record["url"])
        if not activity:
            return
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


def _fanout(line: str, url) -> None:
    """Hand the already-built URL to web push. Failures stay out of the activity row."""
    try:
        import webpush
        webpush.deliver(line, None if url is None else str(url))
    except Exception:
        log.warning("web push was not sent")


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
