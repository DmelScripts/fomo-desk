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
_TTL_S = 12 * 60 * 60
_CONTACT = "mailto:desk@localhost"
_REASONS = frozenset({
    "BadTtl",
    "BadUrgency",
    "BadWebPushRequest",
    "BadWebPushTopic",
    "VapidPkHashMismatch",
    "IdleTimeout",
    "BadAuthorizationHeader",
    "BadJwtToken",
    "BadVapidPublicKey",
    "BadPath",
    "MethodNotAllowed",
    "PayloadTooLarge",
    "TooManyRequests",
    "InternalServerError",
    "ServiceUnavailable",
    "Shutdown",
})


class PushStatus(Exception):
    def __init__(self, status: int | None, reason: str | None = None) -> None:
        self.status = status if isinstance(status, int) else None
        self.reason = reason if reason in _REASONS else None


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
        detail = "Web push is on. A test push reports how many phones accepted it. The private key is not shown."
    contact = _contact_flags()
    return {
        "ready": ready,
        "channel": channel,
        "detail": detail,
        "keys_match": _keys_match(),
        "contact_is_mailto": contact["contact_is_mailto"],
        "contact_host_public": contact["contact_host_public"],
    }


def _pywebpush_send(subscription: dict, data: str, private_key: str) -> None:
    import pywebpush

    try:
        pywebpush.webpush(
            subscription_info=subscription,
            data=data,
            vapid_private_key=private_key,
            vapid_claims={"sub": _CONTACT},
            ttl=_TTL_S,
            verbose=False,
        )
    except pywebpush.WebPushException as exc:
        raise PushStatus(exc.status_code, _reason(getattr(exc, "response", None))) from None
    except Exception:
        raise PushStatus(None) from None


def deliver(line: str, url: str | None, *, sender=None) -> dict:
    """Send one notification to every saved phone. url is used exactly as given."""
    result = {"ok": False, "configured": False, "sent": 0, "removed": 0, "failed": 0, "errors": []}
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
        result["ok"] = False
        result["reason"] = "no subscriptions"
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
                _fail(result, exc)
        except Exception:
            _fail(result, PushStatus(None))
    result["ok"] = result["failed"] == 0
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
        "errors": [],
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
                _fail(result, exc)
        except Exception:
            _mark(event_id, endpoint, "failed")
            _fail(result, PushStatus(None))
        else:
            _mark(event_id, endpoint, "sent")
            result["sent"] += 1
    result["ok"] = result["failed"] == 0
    return result


def _fail(result: dict, exc: PushStatus) -> None:
    result["failed"] += 1
    result["errors"].append({"status": exc.status, "reason": exc.reason})
    log.warning(
        "web push failed status=%s reason=%s",
        "none" if exc.status is None else exc.status,
        exc.reason or "none",
    )


def _reason(response: object) -> str | None:
    """Apple's reason token, or nothing. The response body is not kept."""
    if response is None:
        return None
    try:
        text = response.text  # type: ignore[attr-defined]
    except Exception:
        return None
    if not isinstance(text, str) or len(text) > 500:
        return None
    try:
        data = json.loads(text)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    reason = data.get("reason")
    if isinstance(reason, str) and reason in _REASONS:
        return reason
    return None


def _contact_flags() -> dict:
    text = _CONTACT.lower()
    mailto = text.startswith("mailto:") and "@" in text
    host = text.split("@", 1)[1] if mailto else ""
    public = bool(host) and host != "localhost" and "." in host
    return {"contact_is_mailto": mailto, "contact_host_public": public}


def _keys_match() -> bool | None:
    """True when the private key's public point equals the public file.

    The key material is not returned. A value of None means one side is missing.
    """
    private = _private()
    public = _public()
    if private is None or public is None:
        return None
    try:
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        from py_vapid import Vapid

        point = Vapid.from_string(private_key=private).public_key.public_bytes(
            Encoding.X962,
            PublicFormat.UncompressedPoint,
        )
        padded = public + ("=" * ((4 - len(public) % 4) % 4))
        import base64
        return base64.urlsafe_b64decode(padded.encode("ascii")) == point
    except Exception:
        return False


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
