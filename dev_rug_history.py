"""Past-rug check. No documented source is registered.

The fetched RugCheck swagger does not name a creator rug-count field, and
Helius getAssetsByCreator returns assets rather than a rug history. A reader
can be plugged in later. Until a reader verifies the history, the filter
rejects with dev_rug_history_unverified. A verified count above zero rejects
with dev_rug_history.
"""

from __future__ import annotations

_READER = None


def set_source(reader) -> None:
    """Install one reader. None restores the unconfigured source."""
    global _READER
    _READER = reader if callable(reader) else None


def reset_source() -> None:
    global _READER
    _READER = None


def assess(reading, mint=None) -> dict:
    """A checked count is the existing gate. Anything else asks the reader."""
    if _checked(reading):
        if reading["rugs"] > 0:
            return {"ok": False, "verified": True, "reason": "dev_rug_history", "rugs": reading["rugs"]}
        return {"ok": True, "verified": True, "reason": None, "rugs": reading["rugs"], "source": reading["source"]}
    verdict = _ask(mint)
    if verdict.get("verified") is not True:
        return {"ok": False, "verified": False, "reason": "dev_rug_history_unverified", "rugs": None, "source": verdict.get("source")}
    rugs = verdict.get("rugs")
    if isinstance(rugs, bool) or not isinstance(rugs, int) or rugs < 0:
        return {"ok": False, "verified": False, "reason": "dev_rug_history_unverified", "rugs": None}
    if rugs > 0:
        return {"ok": False, "verified": True, "reason": "dev_rug_history", "rugs": rugs, "source": verdict.get("source")}
    return {"ok": True, "verified": True, "reason": None, "rugs": 0, "source": verdict.get("source")}


def _checked(reading) -> bool:
    if not isinstance(reading, dict) or reading.get("status") != "checked":
        return False
    source = reading.get("source")
    if not isinstance(source, str) or source == "" or source == "dexscreener":
        return False
    rugs = reading.get("rugs")
    seen = reading.get("tokens_seen")
    if isinstance(rugs, bool) or not isinstance(rugs, int) or rugs < 0:
        return False
    if isinstance(seen, bool) or not isinstance(seen, int) or seen < 0:
        return False
    return True


def _ask(mint) -> dict:
    if _READER is None:
        return {"verified": False, "reason": "dev_rug_history_unverified", "source": None}
    try:
        result = _READER(mint)
    except Exception:
        return {"verified": False, "reason": "dev_rug_history_unverified", "source": None}
    if not isinstance(result, dict):
        return {"verified": False, "reason": "dev_rug_history_unverified", "source": None}
    return result
