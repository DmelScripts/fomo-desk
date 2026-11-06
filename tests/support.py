"""Shared fixtures for the offline agent tests."""
import json
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).resolve().parent / "fixtures"


def load(name: str):
    return json.loads((FIX / name).read_text())


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
