"""Desk settings.

Secret values come from the environment first, then ~/.desk-secrets/env.
Never print or log them.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DESK_ROOT = Path(__file__).resolve().parent
SETTINGS_SEED_PATH = DESK_ROOT / "settings.seed.json"
SECRETS_ENV_PATH = Path.home() / ".desk-secrets" / "env"
DEFAULT_RPC_URL = "https://api.mainnet-beta.solana.com"

SECRET_KEYS = (
    "TYPESAFE_API_KEY",
    "VAPID_PRIVATE_KEY",
    "APP_TOKEN",
    "TG_TOKEN",
)


def load_seed() -> dict:
    if SETTINGS_SEED_PATH.exists():
        return json.loads(SETTINGS_SEED_PATH.read_text())
    return {"fomo_handle": None, "analyst_mode": "jev", "rpc_url": DEFAULT_RPC_URL}


def secrets_path() -> Path:
    """Absolute path to the server-only env file. Do not read this into logs."""
    return SECRETS_ENV_PATH


def _file_secrets() -> dict[str, str]:
    path = secrets_path()
    try:
        text = path.read_text()
    except OSError:
        return {}
    found: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].strip()
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            found[key] = value
    return found


def load_secrets() -> dict[str, str | None]:
    """Environment wins over the secrets file. Missing keys are None.

    Callers must not log the returned values.
    """
    file_values = _file_secrets()
    loaded: dict[str, str | None] = {}
    for key in SECRET_KEYS:
        env_value = os.environ.get(key)
        if env_value:
            loaded[key] = env_value
        else:
            loaded[key] = file_values.get(key) or None
    return loaded
