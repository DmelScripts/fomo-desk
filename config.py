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

# STEP 10 names and defaults, plus agents_enabled (kept at '0' so a deploy
# does not start the agents). fomo_handle is filled from settings.seed.json.
# poll_s and confirm_window_min are listed twice with different defaults
# (scanner 60 vs risk 15, executor 5 vs risk 2). The flat settings table
# cannot hold both under one name, so the risk copies use the risk. prefix.
SETTINGS_DEFAULTS = {
    "agents_enabled": "0",
    "analyst_mode": "jev",
    "min_liquidity_usd": "40000",
    "max_top10_pct": "25",
    "max_h1_change_pct": "80",
    "poll_s": "60",
    "min_pool_age_min": "10",
    "max_wait_min": "60",
    "runner_h24_pct": "300",
    "enabled": "true",
    "max_dev_pct": "5",
    "min_lp_locked_pct": "99",
    "max_bundled": "5",
    "max_delegated_accounts": "10",
    "retries": "3",
    "min_alive_6h": "0.70",
    "min_organic": "0.65",
    "max_chase": "0.30",
    "min_score": "70",
    "timeout_s": "10",
    "alive_volume_h1": "25000",
    "organic_buys_h1": "150",
    "bundled_penalty": "0.5",
    "flush_drop_m5_pct": "5",
    "chase_h1_pct": "80",
    "stack_pct": "8",
    "max_pool_pct": "2",
    "min_ticket_usd": "47.50",
    "below_min": "bump",
    "recheck_liq_drop_pct": "20",
    "recheck_price_up_pct": "15",
    "confirm_window_min": "5",
    "stop_pct": "6",
    "trail_start_pct": "20",
    "trail_pct": "12",
    "timeout_h": "3",
    "timeout_min_gain_pct": "5",
    "mode": "PAPER",
    "paper_start_stack": "200",
    "report_time": "08:00",
    "notifier": "telegram",
    "rpc_url": "https://api.mainnet-beta.solana.com",
    "push_rejects": "false",
    "fees_ref_discount": "0.10",
    "risk.poll_s": "15",
    "risk.confirm_window_min": "2",
}

# Local bounds. STEP 10 says out-of-range values are refused and the range is
# named, but it does not list the numeric limits. These are the bounds the
# API enforces. Secret keys are not settings.
SETTING_RULES = {
    "min_liquidity_usd": ("number", 0, 1_000_000_000),
    "max_top10_pct": ("number", 0, 100),
    "max_h1_change_pct": ("number", 0, 100_000),
    "poll_s": ("number", 5, 3600),
    "min_pool_age_min": ("number", 0, 24 * 60),
    "max_wait_min": ("number", 1, 24 * 60),
    "runner_h24_pct": ("number", 0, 100_000),
    "enabled": ("bool",),
    "max_dev_pct": ("number", 0, 100),
    "min_lp_locked_pct": ("number", 0, 100),
    "max_bundled": ("number", 1, 10_000),
    "max_delegated_accounts": ("number", 1, 10_000),
    "retries": ("number", 1, 10),
    "analyst_mode": ("enum", ("jev", "rules")),
    "min_alive_6h": ("number", 0, 1),
    "min_organic": ("number", 0, 1),
    "max_chase": ("number", 0, 1),
    "min_score": ("number", 0, 100),
    "timeout_s": ("number", 1, 120),
    "alive_volume_h1": ("number", 1, 1_000_000_000),
    "organic_buys_h1": ("number", 1, 1_000_000),
    "bundled_penalty": ("number", 0, 1),
    "flush_drop_m5_pct": ("number", 0, 100),
    "chase_h1_pct": ("number", 1, 100_000),
    "stack_pct": ("number", 0.1, 100),
    "max_pool_pct": ("number", 0.1, 100),
    "min_ticket_usd": ("number", 5, 100_000),
    "below_min": ("enum", ("bump", "skip")),
    "recheck_liq_drop_pct": ("number", 0, 100),
    "recheck_price_up_pct": ("number", 0, 100_000),
    "confirm_window_min": ("number", 1, 120),
    "stop_pct": ("number", 0.1, 100),
    "trail_start_pct": ("number", 0.1, 100_000),
    "trail_pct": ("number", 0.1, 100),
    "timeout_h": ("number", 0.1, 168),
    "timeout_min_gain_pct": ("number", 0, 100_000),
    "mode": ("enum", ("PAPER", "LIVE")),
    "paper_start_stack": ("number", 0, 1_000_000_000),
    "report_time": ("time",),
    "notifier": ("enum", ("telegram", "webpush")),
    "rpc_url": ("url",),
    "push_rejects": ("bool",),
    "fees_ref_discount": ("number", 0, 1),
    "risk.poll_s": ("number", 5, 3600),
    "risk.confirm_window_min": ("number", 1, 120),
    "fomo_handle": ("text", 1, 64),
}

SETTING_GROUPS = {
    "scanner": [
        "min_liquidity_usd", "max_top10_pct", "max_h1_change_pct", "poll_s",
        "min_pool_age_min", "max_wait_min", "runner_h24_pct",
    ],
    "auditor": [
        "enabled", "max_dev_pct", "max_top10_pct", "min_lp_locked_pct",
        "min_pool_age_min", "max_bundled", "max_delegated_accounts", "retries",
    ],
    "analyst": [
        "analyst_mode", "min_alive_6h", "min_organic", "max_chase", "min_score",
        "timeout_s", "alive_volume_h1", "organic_buys_h1", "bundled_penalty",
        "flush_drop_m5_pct", "chase_h1_pct",
    ],
    "executor": [
        "stack_pct", "max_pool_pct", "min_ticket_usd", "below_min",
        "recheck_liq_drop_pct", "recheck_price_up_pct", "confirm_window_min",
    ],
    "risk": [
        "risk.poll_s", "stop_pct", "trail_start_pct", "trail_pct", "timeout_h",
        "timeout_min_gain_pct", "risk.confirm_window_min",
    ],
    "desk": [
        "mode", "paper_start_stack", "report_time", "notifier", "rpc_url",
        "push_rejects", "fees_ref_discount", "fomo_handle",
    ],
}


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


def install_secret_redaction() -> None:
    """Drop a logging filter that redacts secret env values if they ever appear."""
    import logging

    class _Redact(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            secrets = []
            for key in ("TYPESAFE_API_KEY", "APP_TOKEN", "VAPID_PRIVATE_KEY", "TG_TOKEN"):
                value = os.environ.get(key)
                if value:
                    secrets.append(value)
            if not secrets:
                return True

            def scrub(text: str) -> str:
                for secret in secrets:
                    if secret in text:
                        text = text.replace(secret, "[REDACTED]")
                return text

            record.msg = scrub(record.getMessage())
            record.args = ()
            if record.exc_text:
                record.exc_text = scrub(record.exc_text)
            return True

    # A filter on the root logger does not run for a child logger that has
    # its own handler. Wrap Logger.handle so every record is scrubbed first.
    if getattr(logging.Logger, "_desk_redact", False):
        return
    original = logging.Logger.handle

    def handle(self, record):
        _Redact().filter(record)
        return original(self, record)

    logging.Logger.handle = handle
    logging.Logger._desk_redact = True


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
