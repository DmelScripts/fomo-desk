"""Desk settings.

Secret values come from the environment first, then ~/.desk-secrets/env.
Never print or log them.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# report_time is a clock on this zone. Stored rows stay unix UTC instants.
DESK_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

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
# Names the log filter reads. The provider keys are not written to the secrets file.
REDACT_ENV_KEYS = SECRET_KEYS + (
    "PYTH_API_KEY",
    "HELIUS_API_KEY",
    "SOLANA_TRACKER_API_KEY",
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
    # Telegram was skipped. New databases use the channel that actually sends.
    "notifier": "webpush",
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
    # LIVE stack from the "stack N" command. Unset until the owner sets it.
    "stack": ("number", 0, 1_000_000_000),
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
        "mode", "paper_start_stack", "stack", "report_time", "notifier", "rpc_url",
        "push_rejects", "fees_ref_discount", "fomo_handle",
    ],
}

# Labels for the Account tab. Bounds live in SETTING_RULES. The prompt requires
# a sane range and does not list the numbers; those ranges are local choices.
# Percent fields that describe a share of a position or pool stay inside 0–100.
# Probabilities stay inside 0–1. Polls cannot be faster than 5s. A ticket cannot
# be below the $5 fee step. agents_enabled is not a setting.
SETTING_COPY = {
    "min_liquidity_usd": ("Minimum liquidity", "USD. Scanner kills a pool below this."),
    "max_top10_pct": ("Max top 10 holders", "Shared by scanner and auditor. One value."),
    "max_h1_change_pct": ("Max 1h price change", "Percent. Scanner only."),
    "poll_s": ("Scanner poll", "Seconds. Risk uses risk.poll_s, not this."),
    "min_pool_age_min": ("Minimum pool age", "Minutes. Shared by scanner and auditor."),
    "max_wait_min": ("Maximum wait", "Minutes in the waiting room."),
    "runner_h24_pct": ("Runner 24h change", "Percent that marks a name as a runner."),
    "enabled": ("Take apart before buying", "auditor.enabled. Turning this off asks twice."),
    "max_dev_pct": ("Max dev holding", "Percent of supply."),
    "min_lp_locked_pct": ("Minimum LP locked", "Percent."),
    "max_bundled": ("Max bundled wallets", "Largest funded-together group."),
    "max_delegated_accounts": ("Max delegated accounts", "Accounts on one delegate."),
    "retries": ("Source retries", "Tries before a layer is no data."),
    "analyst_mode": ("Analyst mode", "jev uses the saved key. rules does not."),
    "min_alive_6h": ("Minimum alive", "0 to 1."),
    "min_organic": ("Minimum organic", "0 to 1."),
    "max_chase": ("Maximum chase", "0 to 1."),
    "min_score": ("Minimum score", "0 to 100."),
    "timeout_s": ("Analyst timeout", "Seconds. Not the risk timeout."),
    "alive_volume_h1": ("Rules alive volume", "USD of 1h volume for a score of 1."),
    "organic_buys_h1": ("Rules organic buys", "1h buys for a score of 1."),
    "bundled_penalty": ("Rules bundled penalty", "Multiplier from this many bundled wallets."),
    "flush_drop_m5_pct": ("Rules flush drop", "Percent down in 5 minutes."),
    "chase_h1_pct": ("Rules chase 1h", "Percent. Not max_chase."),
    "stack_pct": ("Stack percent", "Percent of the stack for one ticket."),
    "max_pool_pct": ("Max pool percent", "Percent of pool liquidity."),
    "min_ticket_usd": ("Minimum ticket", "USD. The fee step starts at 5."),
    "below_min": ("Below the minimum", "bump raises the ticket. skip sends nothing."),
    "recheck_liq_drop_pct": ("Recheck liquidity drop", "Percent."),
    "recheck_price_up_pct": ("Recheck price rise", "Percent."),
    "confirm_window_min": ("Executor confirm window", "Minutes. Risk uses risk.confirm_window_min."),
    "risk.poll_s": ("Risk poll", "Seconds. Not the scanner poll."),
    "stop_pct": ("Stop", "Percent below entry. A position keeps its own stop."),
    "trail_start_pct": ("Trail starts", "Percent above entry."),
    "trail_pct": ("Trail giveback", "Percent off the peak. An armed trail only tightens."),
    "timeout_h": ("Risk timeout", "Hours. Not the analyst timeout."),
    "timeout_min_gain_pct": ("Timeout minimum gain", "Percent. Below this, a timeout can fire."),
    "risk.confirm_window_min": ("Risk confirm window", "Minutes. Not the executor window."),
    "mode": ("Mode", "Use the PAPER / LIVE switch. LIVE stays locked here."),
    "paper_start_stack": ("PAPER starting stack", "USD. Not the LIVE stack and not the home balance."),
    "stack": ("LIVE stack", "USD. Used only in LIVE. Unset until you set it."),
    "report_time": ("Report time", "Vietnam clock, HH:MM."),
    "notifier": ("Notifier", "webpush is the channel. Telegram is not connected."),
    "rpc_url": ("Solana RPC", "HTTPS URL."),
    "push_rejects": ("Push rejects", "Also push auditor and analyst rejects."),
    "fees_ref_discount": ("Referral discount", "Fraction off the fee. 0.10 is 10%."),
    "fomo_handle": ("FOMO username", "From the signup step."),
}


def report_boundary(now: float, report_time: str = "08:00", *, upcoming: bool = False) -> float:
    """Last or next report_time in Asia/Ho_Chi_Minh, as a unix instant.

    08:00 in Vietnam is 01:00 UTC. Exactly 08:00 starts the new night.
    """
    hour, minute = (int(part) for part in report_time.split(":"))
    moment = datetime.fromtimestamp(now, DESK_TZ)
    mark = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if upcoming:
        if mark <= moment:
            mark = mark + timedelta(days=1)
    elif mark > moment:
        mark = mark - timedelta(days=1)
    return mark.timestamp()


def load_seed() -> dict:
    if SETTINGS_SEED_PATH.exists():
        return json.loads(SETTINGS_SEED_PATH.read_text())
    return {"fomo_handle": None, "analyst_mode": "jev", "rpc_url": DEFAULT_RPC_URL}


def secrets_path() -> Path:
    """Absolute path to the server-only env file. Do not read this into logs.

    DESK_SECRETS_PATH redirects the file for a test process. It does not
    change host environment variables.
    """
    override = os.environ.get("DESK_SECRETS_PATH")
    if override:
        return Path(override)
    return SECRETS_ENV_PATH


def set_file_secret(key: str, value: str | None) -> None:
    """Write or remove one secret in the secrets file.

    A host environment value wins in load_secrets, so this refuses to run
    while that variable is set. The value is never returned or logged.
    """
    if key not in SECRET_KEYS:
        raise ValueError("unknown secret")
    if os.environ.get(key):
        raise PermissionError("host")
    if value is not None and ("\n" in value or "\r" in value):
        raise ValueError("invalid secret")
    path = secrets_path()
    try:
        text = path.read_text()
    except OSError:
        text = ""
    kept: list[str] = []
    replaced = False
    for line in text.splitlines():
        stripped = line.strip()
        raw = stripped[7:].strip() if stripped.startswith("export ") else stripped
        if raw.startswith(key + "="):
            replaced = True
            if value:
                kept.append(f"{key}={value}")
            continue
        kept.append(line)
    if value and not replaced:
        kept.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(kept)
    if body:
        body += "\n"
    path.write_text(body)
    try:
        path.chmod(0o600)
    except OSError:
        pass


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
    import re

    query_secret = re.compile(r"(api-key=)[^&\s\"']+", re.IGNORECASE)

    class _Redact(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            secrets = []
            for key in REDACT_ENV_KEYS:
                value = os.environ.get(key)
                if value:
                    secrets.append(value)

            def scrub(text: str) -> str:
                for secret in secrets:
                    if secret in text:
                        text = text.replace(secret, "[REDACTED]")
                return query_secret.sub(r"\1[REDACTED]", text)

            record.msg = scrub(record.getMessage())
            record.args = ()
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = scrub(record.exc_text)
            record.exc_info = None
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
