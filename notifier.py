"""Owner notifier. Telegram was skipped; this desk uses web push.

Replaces telegram.py from the master prompt layout. Same push text, FOMO
links via links.py, and command surface. Skeleton: not implemented yet.
"""

from __future__ import annotations


class Notifier:
    def __init__(self, *args, **kwargs):
        raise NotImplementedError("notifier.py is a STEP 3 skeleton")

    def push(self, text: str, *, coin_url: str | None = None) -> None:
        raise NotImplementedError("notifier.py is a STEP 3 skeleton")
