"""Desk settings. Secrets live outside the repo in ~/.desk-secrets/env."""

from __future__ import annotations

import json
from pathlib import Path

DESK_ROOT = Path(__file__).resolve().parent
SETTINGS_SEED_PATH = DESK_ROOT / "settings.seed.json"
SECRETS_ENV_PATH = Path.home() / ".desk-secrets" / "env"
DEFAULT_RPC_URL = "https://api.mainnet-beta.solana.com"


def load_seed() -> dict:
    if SETTINGS_SEED_PATH.exists():
        return json.loads(SETTINGS_SEED_PATH.read_text())
    return {"fomo_handle": None, "analyst_mode": "jev", "rpc_url": DEFAULT_RPC_URL}


def secrets_path() -> Path:
    """Absolute path to the server-only env file. Do not read this into logs."""
    return SECRETS_ENV_PATH
