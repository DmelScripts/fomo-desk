"""Send Web Push to db.push_subs. The private key is read, never created or returned.

410 Gone deletes that subscription. Other failures leave it in place.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import db
from config import DESK_ROOT, load_secrets

log = logging.getLogger("webpush")
_PUBLIC_PATH = DESK_ROOT / "web" / "vapid_public.txt"
_LEASE_S = 120


class PushStatus(Exception):
    def __init__(self, status: int | None) -> None:
        self.status = status


def _private() -> str | None:
    value = load_secrets().get("VAPID_PRIVATE_KEY")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _public() -> str | None:
    try:
        text = Path(_PUBLIC_PATH).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def selected_channel() -> str:
    """Web push is the channel when it is selected, or when Telegram was skipped.

    A stored telegram value with no TG_TOKEN is the skipped-Telegram case.
    A present Telegram token keeps that setting, and this module does not send.
    """
    choice = (db.get_setting("notifier", "telegram") or "telegram").strip().lower()
    if choice == "webpush":
        return "webpush"
    if choice == "telegram" and not load_secrets().get("TG_TOKEN"):
        return "webpush"
    return "telegram"


def status() -> dict:
    """Booleans and a sentence. The private key is not copied into the result."""
    channel = selected_channel()
    has_private = _private() is not None
    has_public = _public() is not None
    ready = channel == "webpush" and has_private and has_public
    if channel != "webpush":
        detail = "The notifier setting is telegram. Web push is off. Telegram delivery is not running."
    elif not has_public:
        detail = "The public VAPID key file is missing. Nothing can be sent."
    elif not has_private:
        detail = "Web push is the channel. The server VAPID secret is not set, so nothing can be sent."
    else:
        detail = "Web push is on. Send test push reaches saved phones. The private key is not shown."
    return {"ready": ready, "channel": channel, "detail": detail}


def _pywebpush_send(subscription: dict, data: str, private_key: str) -> None:
    import pywebpush

    try:
        pywebpush.webpush(
            subscription_info=subscription,
            data=data,
            vapid_private_key=private_key,
            vapid_claims={"sub": "mailto:desk@localhost"},
            verbose=False,
        )
    except pywebpush.WebPushException as exc:
        raise PushStatus(exc.status_code) from None
    except Exception:
        raise PushStatus(None) from None


def deliver(line: str, url: str | None, *, sender=None) -> dict:
    """Send one notification to every saved phone. url is used exactly as given."""
    result = {"ok": False, "configured": False, "sent": 0, "removed": 0, "failed": 0}
    if selected_channel() != "webpush" or _private() is None or _public() is None:
        return result
    result["configured"] = True
    result["ok"] = True
    try:
        rows = db.query("SELECT endpoint, p256dh, auth FROM push_subs")
    except Exception:
        log.warning("web push subscriptions could not be read")
        return result
    if not rows:
        return result
    send = sender or _pywebpush_send
    private_key = _private()
    if private_key is None:
        result["configured"] = False
        result["ok"] = False
        return result
    body = json.dumps({
        "title": "Grok Bot · now",
        "body": line,
        "url": url or "",
    }, separators=(",", ":"))
    for row in rows:
        endpoint = row["endpoint"]
        if not endpoint:
            continue
        subscription = {
            "endpoint": endpoint,
            "keys": {"p256dh": row["p256dh"], "auth": row["auth"]},
        }
        try:
            send(subscription, body, private_key)
            result["sent"] += 1
        except PushStatus as exc:
            if exc.status == 410:
                db.execute("DELETE FROM push_subs WHERE endpoint = ?", (endpoint,))
                result["removed"] += 1
            else:
                result["failed"] += 1
                log.warning("web push failed")
        except Exception:
            result["failed"] += 1
            log.warning("web push failed")
    return result


def deliver_once(event_id: str, line: str, url: str, *, sender=None) -> dict:
    """Send one event. A phone already marked sent is not sent again.

    A 410 removes that subscription. Any other failure stays retryable.
    """
    result = {
        "ok": False,
        "configured": False,
        "sent": 0,
        "failed": 0,
        "removed": 0,
        "skipped": 0,
        "reason": None,
    }
    if selected_channel() != "webpush" or _private() is None or _public() is None:
        result["reason"] = "not configured"
        return result
    result["configured"] = True
    result["ok"] = True
    try:
        rows = db.query("SELECT endpoint, p256dh, auth FROM push_subs")
    except Exception:
        log.warning("web push subscriptions could not be read")
        result["ok"] = False
        result["reason"] = "not read"
        return result
    if not rows:
        result["reason"] = "no subscriptions"
        return result
    send = sender or _pywebpush_send
    private_key = _private()
    if private_key is None:
        result["configured"] = False
        result["ok"] = False
        result["reason"] = "not configured"
        return result
    body = json.dumps({
        "title": "Grok Bot · now",
        "body": line,
        "url": url or "",
    }, separators=(",", ":"))
    now = time.time()
    for row in rows:
        endpoint = row["endpoint"]
        if not endpoint:
            continue
        if not _claim(event_id, endpoint, now):
            result["skipped"] += 1
            continue
        subscription = {
            "endpoint": endpoint,
            "keys": {"p256dh": row["p256dh"], "auth": row["auth"]},
        }
        try:
            send(subscription, body, private_key)
        except PushStatus as exc:
            if exc.status == 410:
                db.execute("DELETE FROM push_subs WHERE endpoint = ?", (endpoint,))
                _mark(event_id, endpoint, "gone")
                result["removed"] += 1
            else:
                _mark(event_id, endpoint, "failed")
                result["failed"] += 1
                log.warning("web push failed")
        except Exception:
            _mark(event_id, endpoint, "failed")
            result["failed"] += 1
            log.warning("web push failed")
        else:
            _mark(event_id, endpoint, "sent")
            result["sent"] += 1
    result["ok"] = result["failed"] == 0
    return result


def _claim(event_id: str, endpoint: str, now: float) -> bool:
    """True when this caller should send. Sent and in-flight claims are skipped."""
    def work(conn):
        row = conn.execute(
            "SELECT status, updated_at FROM push_outbox WHERE event_id = ? AND endpoint = ?",
            (event_id, endpoint),
        ).fetchone()
        if row is not None:
            status = row["status"]
            if status in {"sent", "gone"}:
                return False
            if status == "sending" and now - float(row["updated_at"]) < _LEASE_S:
                return False
        conn.execute(
            """
            INSERT INTO push_outbox (event_id, endpoint, status, updated_at)
            VALUES (?, ?, 'sending', ?)
            ON CONFLICT(event_id, endpoint) DO UPDATE SET
                status = 'sending',
                updated_at = excluded.updated_at
            """,
            (event_id, endpoint, now),
        )
        return True
    return bool(db.exclusive(work))


def _mark(event_id: str, endpoint: str, status: str) -> None:
    db.execute(
        """
        UPDATE push_outbox SET status = ?, updated_at = ? WHERE event_id = ? AND endpoint = ?
        """,
        (status, time.time(), event_id, endpoint),
    )
