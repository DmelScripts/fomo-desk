"""Send Web Push to db.push_subs. The private key is read, never created or returned.

410 Gone deletes that subscription. Other failures leave it in place.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import db
from config import DESK_ROOT, load_secrets

log = logging.getLogger("webpush")
_PUBLIC_PATH = DESK_ROOT / "web" / "vapid_public.txt"


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
