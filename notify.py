"""Owner notifications for the two routines, plus one labelled verification.

Report and restart text are taken from stored facts. A caller cannot
supply either message or the morning-page URL.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from fastapi import HTTPException

import db
import webpush

_EVENT = re.compile(r"^[A-Za-z0-9._-]{1,40}$")
_FORBIDDEN_QUERY = {"t", "token", "app_token", "APP_TOKEN"}
_LINE_LIMIT = 240


def send(body: dict) -> dict:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    kind = body.get("kind")
    if kind == "report":
        return _report(body)
    if kind == "restart":
        return _restart(body)
    if kind == "verify":
        return _verify(body)
    raise HTTPException(status_code=400, detail="kind must be report, restart, or verify")


def _report(body: dict) -> dict:
    _reject_extra(body, {"kind", "window_id"})
    window_id = body.get("window_id")
    if not isinstance(window_id, str) or not window_id.strip():
        raise HTTPException(status_code=400, detail="window_id is required")
    rows = db.query("SELECT url FROM reports WHERE window_id = ?", (window_id,))
    if not rows or not rows[0]["url"]:
        raise HTTPException(status_code=409, detail="no saved page for this window")
    url = rows[0]["url"]
    line = "Morning report"
    return _dispatch("report:" + window_id, line, url)


def _restart(body: dict) -> dict:
    _reject_extra(body, {"kind", "event_id"})
    event_id = body.get("event_id")
    if not isinstance(event_id, str) or not event_id.startswith("restart-"):
        raise HTTPException(status_code=400, detail="event_id is required")
    rows = db.query(
        "SELECT push_text FROM restart_events WHERE event_id = ?",
        (event_id,),
    )
    if not rows:
        raise HTTPException(status_code=409, detail="no verified restart")
    return _dispatch("restart:" + event_id, rows[0]["push_text"], "")


def _verify(body: dict) -> dict:
    _reject_extra(body, {"kind", "event", "note", "url"})
    event = body.get("event")
    if not isinstance(event, str) or _EVENT.fullmatch(event) is None:
        raise HTTPException(status_code=400, detail="event must be 1 to 40 letters, numbers, dot, underscore, or dash")
    note = body.get("note") or ""
    if not isinstance(note, str) or len(note) > 80 or any(char in note for char in "\r\n"):
        raise HTTPException(status_code=400, detail="note must be one line of at most 80 characters")
    lowered = note.lower()
    if "restarted" in lowered or "http" in lowered or "morning report" in lowered:
        raise HTTPException(status_code=400, detail="note cannot look like a restart or a report")
    line = "Verification only. Not a restart and not a morning report."
    if note.strip():
        line = f"{line} {note.strip()}"
    if len(line) > _LINE_LIMIT:
        raise HTTPException(status_code=400, detail="note is too long")
    url = body.get("url") or ""
    if url:
        _https(url)
    return _dispatch("verify:" + event, line, url)


def _dispatch(event_id: str, line: str, url: str) -> dict:
    if len(line) > _LINE_LIMIT:
        raise HTTPException(status_code=400, detail="message is too long")
    if url:
        _https(url)
    result = webpush.deliver_once(event_id, line, url)
    if result.get("reason") == "not configured":
        raise HTTPException(status_code=409, detail="web push is not configured")
    payload = {
        "ok": bool(result.get("ok")),
        "event_id": event_id,
        "sent": result["sent"],
        "failed": result["failed"],
        "removed": result["removed"],
        "skipped": result["skipped"],
        "errors": result.get("errors") or [],
    }
    if result.get("reason"):
        payload["reason"] = result["reason"]
    return payload


def _reject_extra(body: dict, allowed: set[str]) -> None:
    extra = set(body) - allowed
    if extra:
        raise HTTPException(status_code=400, detail="unexpected field")


def _https(url: str) -> None:
    if not isinstance(url, str) or len(url) > 400:
        raise HTTPException(status_code=400, detail="url must be https")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="url must be https")
    keys = set(parse_qs(parsed.query))
    if keys & _FORBIDDEN_QUERY:
        raise HTTPException(status_code=400, detail="url must not carry a token")
