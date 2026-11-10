"""Shared fixtures for the offline agent tests."""
import json
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).resolve().parent / "fixtures"


def load(name: str):
    return json.loads((FIX / name).read_text())


def rugcheck_or_pair(request, pair):
    """Summary fixture for the keyless LP URL. Every other HTTP body stays the pair."""
    url = request.get("url") if isinstance(request, dict) else ""
    if isinstance(url, str) and url.endswith("/report/summary"):
        return {"status": 200, "body": {"lpLockedPct": 100, "score": 1, "score_normalised": 1}}
    return {"status": 200, "body": pair}


def arm_clean_creator(mint: str) -> None:
    """Record one Helius page that contains only this mint. No socket."""
    import dev_rug_history
    dev_rug_history.remember(mint, {
        "id": mint,
        "creators": [{"address": "CreatorFor" + mint[:16], "share": 100, "verified": True}],
        "authorities": [],
    }, {
        "jsonrpc": "2.0",
        "result": {
            "total": 1, "page": 1, "limit": dev_rug_history.MAX_TOKENS,
            "items": [{"id": mint, "interface": "FungibleToken"}],
        },
    })


def arm_holder_chart(start: int = 100, end: int = 170) -> None:
    """A fetched chart whose delta is 70 percent of the start count. The key is a test stand-in."""
    import public_filter_docs
    os.environ[public_filter_docs.KEY_ENV] = "test-tracker-key"
    public_filter_docs.remember_chart({
        "holders": [
            {"holders": start, "time": 1_700_000_000},
            {"holders": end, "time": 1_700_086_400},
        ],
    })


def clear_filter_fixtures() -> None:
    import dev_rug_history
    import public_filter_docs
    dev_rug_history.clear_recorded()
    dev_rug_history.clear_cache()
    public_filter_docs.clear_chart()
    os.environ.pop(public_filter_docs.KEY_ENV, None)
    os.environ.pop(dev_rug_history.KEY_ENV, None)


def temp_desk_env():
    tmp = tempfile.TemporaryDirectory()
    previous = os.environ.get("DESK_DATA_DIR")
    os.environ["DESK_DATA_DIR"] = tmp.name

    def restore():
        if previous is None:
            os.environ.pop("DESK_DATA_DIR", None)
        else:
            os.environ["DESK_DATA_DIR"] = previous
        tmp.cleanup()

    return tmp, restore
